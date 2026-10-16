"""Check ABC, the ``Findings`` builder and the shared ``CheckContext`` (PLAN.md §6.7).

Checks are pure classifiers: they look at :class:`~mcpcheck.client.RawReply`
objects and call ``f.must(...)`` / ``f.should(...)`` / ``f.best_practice(...)`` /
``f.info(...)`` / ``f.inconclusive(...)``; the status is derived here, never by
the check itself.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, ClassVar

from mcpcheck import wire
from mcpcheck.checks import codes
from mcpcheck.client import RawClient, RawReply
from mcpcheck.models import CheckError, CheckName, CheckResult, Era, Finding, Level, Status

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from mcpcheck.client import ClientConfig, Transcript

# re-exported for check authors
__all__ = [
    "Check",
    "CheckContext",
    "Era",
    "Findings",
    "Level",
    "Status",
    "clip_summary",
    "walk_budget_reason",
]

SUMMARY_MAX = 100
MESSAGE_MAX = 500

LIST_ITEM_KEYS: dict[str, str] = {
    "tools/list": "tools",
    "prompts/list": "prompts",
    "resources/list": "resources",
    "resources/templates/list": "resourceTemplates",
}


def clip_summary(text: str) -> str:
    """Fit a summary into the ≤100-character, no-trailing-period contract."""
    text = " ".join(text.split()).rstrip(".")
    if len(text) > SUMMARY_MAX:
        text = text[: SUMMARY_MAX - 1].rstrip() + "…"
    return text


def _clip_message(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MESSAGE_MAX else text[: MESSAGE_MAX - 1].rstrip() + "…"


class Findings:
    """Collects findings for one check and derives the :class:`CheckResult`."""

    def __init__(self, check: Check, *, strict: bool = False) -> None:
        self.check = check
        self.strict = strict
        self.failures: list[Finding] = []
        self.warnings: list[Finding] = []
        self.infos: list[Finding] = []
        self._summary: str | None = None
        self._details: list[str] = []
        self._requests: int | None = None
        self._inconclusive: list[str] = []
        self._error: CheckError | None = None
        self.traceback: str | None = None
        self._t0 = time.perf_counter()

    # ------------------------------------------------------------------ recording
    def _record(
        self,
        severity: codes.Severity,
        code: str,
        message: str,
        *,
        subject: str,
        spec_url: str | None,
        evidence: Mapping[str, Any] | None,
        tool: str | None,
    ) -> Finding:
        info = codes.lookup(code)
        if info.severity != severity:
            msg = f"code {code!r} is registered as {info.severity!r}, not {severity!r}"
            raise ValueError(msg)
        url = spec_url if spec_url is not None else info.spec_url
        if url is None and severity in ("must", "should"):
            msg = f"{severity} finding {code!r} needs a spec_url"
            raise ValueError(msg)
        finding = Finding(
            code=code,
            level=info.level,
            subject=subject,
            message=_clip_message(message),
            spec_url=url,
            evidence=dict(evidence) if evidence is not None else None,
            tool=tool,
        )
        if severity == "must":
            self.failures.append(finding)
        elif severity == "info":
            self.infos.append(finding)
        else:
            self.warnings.append(finding)
        return finding

    def must(
        self,
        code: str,
        message: str,
        *,
        subject: str,
        spec_url: str | None = None,
        evidence: Mapping[str, Any] | None = None,
        tool: str | None = None,
    ) -> None:
        """Record a MUST violation (→ ``failures[]``, status ``fail``)."""
        self._record(
            "must", code, message, subject=subject, spec_url=spec_url, evidence=evidence, tool=tool
        )

    def should(
        self,
        code: str,
        message: str,
        *,
        subject: str,
        spec_url: str | None = None,
        evidence: Mapping[str, Any] | None = None,
        tool: str | None = None,
    ) -> None:
        """Record a SHOULD violation (→ ``warnings[]``, status ``warn``)."""
        self._record(
            "should",
            code,
            message,
            subject=subject,
            spec_url=spec_url,
            evidence=evidence,
            tool=tool,
        )

    def best_practice(
        self,
        code: str,
        message: str,
        *,
        subject: str,
        evidence: Mapping[str, Any] | None = None,
        spec_url: str | None = None,
        tool: str | None = None,
    ) -> None:
        """Record an mcpcheck opinion (→ ``warnings[]``, status ``warn``)."""
        self._record(
            "best-practice",
            code,
            message,
            subject=subject,
            spec_url=spec_url,
            evidence=evidence,
            tool=tool,
        )

    def info(
        self,
        code: str,
        message: str,
        *,
        subject: str,
        evidence: Mapping[str, Any] | None = None,
        spec_url: str | None = None,
        tool: str | None = None,
    ) -> None:
        """Record an observation (→ ``info[]``; never affects the status)."""
        self._record(
            "info", code, message, subject=subject, spec_url=spec_url, evidence=evidence, tool=tool
        )

    def inconclusive(self, reason: str) -> None:
        """Mark the check inconclusive; the first reason becomes the summary unless one is set."""
        self._inconclusive.append(reason)

    def summary(self, text: str) -> None:
        """Set the one-line summary (≤100 chars, no trailing period)."""
        self._summary = clip_summary(text)

    def details(self, text: str) -> None:
        """Append a details paragraph."""
        self._details.append(text.strip())

    def requests(self, n: int) -> None:
        """Override the request count (otherwise derived from the transcript)."""
        self._requests = n

    def error(
        self, message: str, *, type_: str = "CheckError", traceback: str | None = None
    ) -> None:
        """Record a crash/timeout (status ``error``, exit 2)."""
        self._error = CheckError(type=type_, message=message)
        self.traceback = traceback

    # ------------------------------------------------------------------ result
    @property
    def status(self) -> Status:
        """Status derived per §6.7: error > fail > inconclusive > warn > pass."""
        if self._error is not None:
            return Status.ERROR
        if self.failures:
            return Status.FAIL
        if self._inconclusive:
            return Status.INCONCLUSIVE
        if self.warnings:
            return Status.WARN
        return Status.PASS

    def to_result(self, *, requests: int | None = None) -> CheckResult:
        """Freeze into a :class:`CheckResult` (duration measured since construction)."""
        status = self.status
        summary = self._summary
        if summary is None:
            if status is Status.ERROR and self._error is not None:
                summary = f"Error: {self._error.message}"
            elif status is Status.INCONCLUSIVE:
                summary = self._inconclusive[0]
            elif status is Status.FAIL:
                summary = self.failures[0].message
            elif status is Status.WARN:
                summary = f"{self.check.title} ({len(self.warnings)} warning{'s' if len(self.warnings) != 1 else ''})"
            else:
                summary = self.check.title
        details = list(self._details)
        details.extend(r for r in self._inconclusive[1:])
        count = self._requests if self._requests is not None else (requests or 0)
        result = CheckResult(
            name=CheckName(self.check.name),
            title=self.check.title,
            status=status,
            summary=clip_summary(summary),
            details="\n".join(d for d in details if d),
            spec_url=self.check.spec_url,
            failures=list(self.failures),
            warnings=list(self.warnings),
            info=list(self.infos),
            duration_ms=max(0, int((time.perf_counter() - self._t0) * 1000)),
            requests=count,
            error=self._error,
        )
        return result.with_strict(self.strict)


class Check(ABC):
    """A conformance check. Subclasses set the four class attributes and implement ``run``."""

    name: ClassVar[str]
    title: ClassVar[str]
    spec_url: ClassVar[str]
    order: ClassVar[int]

    @abstractmethod
    async def run(self, ctx: CheckContext, f: Findings) -> None:
        """Observe the server through ``ctx`` and record findings on ``f``."""


class CheckContext:
    """Per-run shared state: config, transcript, the ``main`` client and cached list walks."""

    def __init__(self, cfg: ClientConfig, transcript: Transcript, raw: RawClient) -> None:
        self.cfg = cfg
        self.transcript = transcript
        self.raw = raw
        self.era: Era = Era.UNKNOWN
        self.protocol_version: str | None = None
        self.server_info: dict[str, Any] | None = None
        self.server_supported_versions: list[str] | None = None
        self.gate_blocked: str | None = None
        self.current_check: str = ""
        self._pages: dict[str, list[RawReply]] = {}
        self._walk_stops: dict[str, str] = {}
        self._seeded_page0: RawReply | None = None
        self._discover: RawReply | None = None

    # ------------------------------------------------------------------ connections
    @asynccontextmanager
    async def fresh(self, label: str, *, send_token: bool = True) -> AsyncIterator[RawClient]:
        """A brand-new connection pool (fresh TCP connection); tampering kwargs allowed."""
        if label == "main":
            msg = "fresh clients must not be labelled 'main'"
            raise ValueError(msg)
        client = RawClient(self.cfg, self.transcript, label=label, send_token=send_token)
        try:
            yield client
        finally:
            await client.aclose()

    # ------------------------------------------------------------------ cached fetches
    def seed_tools_page0(self, reply: RawReply) -> None:
        """Store the handshake's fresh-connection ``tools/list`` reply as page 0."""
        self._seeded_page0 = reply
        self._pages.pop("tools/list", None)

    async def _walk(self, method: str) -> list[RawReply]:
        if method in self._pages:
            return self._pages[method]
        pages: list[RawReply] = []
        seen_cursors: set[str] = set()
        cursor: str | None = None
        total_bytes = 0
        first = self._seeded_page0 if method == "tools/list" else None
        while len(pages) < wire.MAX_PAGES:
            if first is not None:
                reply, first = first, None
            else:
                params = {"cursor": cursor} if cursor is not None else None
                reply = await self.raw.post(
                    method, params, purpose=f"{method} page {len(pages)}", check=self.current_check
                )
            pages.append(reply)
            total_bytes += len(reply.raw)
            outcome = wire.classify_reply(
                reply, sent_origin=self.cfg.origin is not None, sent_token=bool(self.cfg.token)
            )
            if not outcome.ok:
                break
            result = reply.result or {}
            nxt = result.get("nextCursor")
            if not isinstance(nxt, str):
                break  # absent / null → end; other types reported by the cache check
            if nxt in seen_cursors:
                break  # loop guard; reported by the calling check
            if total_bytes > wire.WALK_BYTE_BUDGET:
                # pages under the per-request cap can still add up to gigabytes; stop and
                # let the callers report the listing as incomplete (never a server defect)
                self._walk_stops[method] = walk_budget_reason(total_bytes, len(pages))
                break
            seen_cursors.add(nxt)
            cursor = nxt
        else:
            self._walk_stops[method] = f"{wire.MAX_PAGES} pages (MAX_PAGES) fetched"
        self._pages[method] = pages
        return pages

    def walk_stopped(self, method: str) -> str | None:
        """Why the cached walk of ``method`` stopped before the server ended pagination, if it did.

        ``None`` when the walk ended normally (no ``nextCursor``, a non-ok page or a
        repeated cursor); otherwise a short reason naming the page or byte budget.
        """
        return self._walk_stops.get(method)

    async def tools_list_pages(self) -> list[RawReply]:
        """Every ``tools/list`` page (cached; page 0 may be the handshake's reply)."""
        return await self._walk("tools/list")

    async def prompts_list_pages(self) -> list[RawReply]:
        """Every ``prompts/list`` page (cached)."""
        return await self._walk("prompts/list")

    async def resources_list_pages(self) -> list[RawReply]:
        """Every ``resources/list`` page (cached)."""
        return await self._walk("resources/list")

    async def resource_templates_list_pages(self) -> list[RawReply]:
        """Every ``resources/templates/list`` page (cached)."""
        return await self._walk("resources/templates/list")

    async def list_pages(self, method: str) -> list[RawReply]:
        """Pages of any of the four list methods by name."""
        if method not in LIST_ITEM_KEYS:
            msg = f"not a list method: {method!r}"
            raise ValueError(msg)
        return await self._walk(method)

    async def discover(self) -> RawReply:
        """The cached ``server/discover`` reply (one POST on ``main``)."""
        if self._discover is None:
            self._discover = await self.raw.post(
                "server/discover",
                wait_for_eof=True,
                purpose="server/discover",
                check=self.current_check,
            )
        return self._discover

    async def tools(self) -> list[dict[str, Any]] | None:
        """Raw tool dicts from every ``ok`` page; ``None`` when page 0 was not ``ok``."""
        pages = await self.tools_list_pages()
        return items_of(pages, "tools")

    async def capabilities(self) -> dict[str, Any] | None:
        """``result.capabilities`` of ``server/discover`` when it is an object, else ``None``."""
        reply = await self.discover()
        result = reply.result
        if result is None:
            return None
        caps = result.get("capabilities")
        return caps if isinstance(caps, dict) else None


def walk_budget_reason(total_bytes: int, pages: int) -> str:
    """Human wording for a walk stopped by :data:`~mcpcheck.wire.WALK_BYTE_BUDGET`."""
    mib = total_bytes / (1024 * 1024)
    budget = wire.WALK_BYTE_BUDGET // (1024 * 1024)
    return f"{mib:.0f} MiB over {pages} page(s) exceeded mcpcheck's {budget} MiB walk budget"


def items_of(pages: list[RawReply], key: str) -> list[dict[str, Any]] | None:
    """Concatenate ``result[key]`` object items over ``ok`` pages; ``None`` if page 0 is not ``ok``."""
    if not pages:
        return None
    items: list[dict[str, Any]] = []
    for i, page in enumerate(pages):
        result = page.result
        if result is None:
            if i == 0:
                return None
            break
        values = result.get(key)
        if isinstance(values, list):
            items.extend(v for v in values if isinstance(v, dict))
    return items
