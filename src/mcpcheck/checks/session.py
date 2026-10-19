"""The ``session`` check (PLAN.md §7.2): transport-level statelessness of the modern path.

Three fresh connections each run ``tools/list`` → ``resources/list`` → ``prompts/list``
→ ``tools/list`` (full ``nextCursor`` walks); a fourth fresh connection repeats the
``tools/list`` walk with a bogus ``Mcp-Session-Id``. The check then proves that no
session id is required, minted or echoed (S1, S4), that list results are identical
across connections and unaffected by other requests on a connection (S5-S8), that
tool order is deterministic (S9), and that pagination terminates (S13). Finally it
scans the whole transcript recorded so far (handshake, gate, discover, own traffic)
for ``Mcp-Session-Id`` response headers on modern exchanges.

Only list methods are sent; the bogus id can never act on server state because no
session was ever obtained or terminated.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from mcpcheck import spec_urls, wire
from mcpcheck.checks.base import (
    LIST_ITEM_KEYS,
    SUMMARY_MAX,
    Check,
    Findings,
    items_of,
    walk_budget_reason,
)
from mcpcheck.models import Era, Status
from mcpcheck.wire import Outcome, OutcomeKind, classify_reply

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from mcpcheck.checks.base import CheckContext
    from mcpcheck.client import Exchange, RawClient, RawReply, Transcript
    from mcpcheck.models import CheckResult, Finding

BOGUS_PREFIX = "mcpcheck-bogus-"
"""Prefix of the client-supplied id in the bogus-id probe (followed by 16 hex digits)."""
LEGACY_SESSION_ERROR_ID = "server-error"
"""mcp 1.x answers an un-dispatched request with this literal id (fingerprint ``legacy_session``)."""
BUDGET_FRACTION = 0.7
"""Stop opening further connections once this share of the check budget is spent."""
CODE_WALK_TRUNCATED = "session.walk_truncated"
"""Info: a list walk was cut short by mcpcheck's page or byte budget (compared on what was fetched)."""
MAX_IDS_IN_EVIDENCE = 50

TOOLS = "tools/list"
RESOURCES = "resources/list"
PROMPTS = "prompts/list"
SECONDARY_METHODS: tuple[str, ...] = (RESOURCES, PROMPTS)

IDENTITY_KEY: dict[str, str] = {TOOLS: "name", RESOURCES: "uri", PROMPTS: "name"}
CAPABILITY_KEY: dict[str, str] = {TOOLS: "tools", RESOURCES: "resources", PROMPTS: "prompts"}
NOUN: dict[str, str] = {TOOLS: "tool", RESOURCES: "resource", PROMPTS: "prompt"}
SPEC_FOR: dict[str, str] = {
    TOOLS: spec_urls.SPEC_TOOLS_CAPABILITIES,
    RESOURCES: spec_urls.SPEC_RESOURCES_CAPABILITIES,
    PROMPTS: spec_urls.SPEC_PROMPTS_CAPABILITIES,
}

_SESSION_WORD = re.compile(r"session", re.IGNORECASE)

# Highest priority first: the summary names the first code present, "+N more" the rest.
_PRIORITY: tuple[str, ...] = (
    "session.required",
    "session.bogus_id_rejected",
    "session.list_varies_per_connection",
    "session.list_varies_within_connection",
    "session.list_varies_with_session_header",
    "session.session_id_minted",
    "session.session_id_echoed",
    "session.definition_unstable",
    "session.order_not_deterministic",
    "session.cursor_loop",
    "session.partial_samples",
    "session.list_changed_during_run",
)


# --------------------------------------------------------------------------- pure helpers


def canon(item: Any) -> str:
    """Canonical JSON of an item (sorted keys, compact, UTF-8 preserved) for byte comparison."""
    return json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def identity(item: Mapping[str, Any], method: str) -> str:
    """The identity of a list item (``name`` / ``uri``), falling back to its canonical JSON."""
    value = item.get(IDENTITY_KEY[method])
    return value if isinstance(value, str) else canon(item)


def _escape_pointer(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def first_diff_pointer(a: Any, b: Any, path: str = "") -> str:
    """RFC 6901 pointer of the first difference between two JSON values (``""`` = root)."""
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            sub = f"{path}/{_escape_pointer(key)}"
            if key not in a or key not in b:
                return sub
            if a[key] != b[key]:
                return first_diff_pointer(a[key], b[key], sub)
        return path
    if isinstance(a, list) and isinstance(b, list):
        for i, (x, y) in enumerate(zip(a, b, strict=False)):
            if x != y:
                return first_diff_pointer(x, y, f"{path}/{i}")
        if len(a) != len(b):
            return f"{path}/{min(len(a), len(b))}"
        return path
    return path


def _clip_ids(ids: Sequence[str]) -> list[str]:
    if len(ids) <= MAX_IDS_IN_EVIDENCE:
        return list(ids)
    return [*ids[:MAX_IDS_IN_EVIDENCE], f"… (+{len(ids) - MAX_IDS_IN_EVIDENCE} more)"]


def _wire_casing(raw: Iterable[tuple[bytes, bytes]]) -> str | None:
    """The ``Mcp-Session-Id`` header name exactly as the server spelled it."""
    for name, _ in raw:
        if name.lower() == wire.H_SESSION_ID.encode("ascii"):
            return name.decode("latin-1")
    return None


def _quote(text: str, limit: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tail(text: str, room: int) -> str:
    """Fit ``text`` into ``room`` characters keeping its tail (``Bad Request: X`` → ``…X``)."""
    text = " ".join(text.split())
    room = max(room, 8)
    if len(text) <= room:
        return text
    _, sep, after = text.rpartition(": ")
    if sep and len(after) + 1 <= room:
        return "…" + after
    tail = text[-(room - 1) :]
    _, space, words = tail.partition(" ")
    return "…" + (words if space and words else tail)


# --------------------------------------------------------------------------- samples


@dataclass
class ListSample:
    """One complete ``nextCursor`` walk of a list method on one connection."""

    method: str
    connection: int | str
    pages: list[RawReply]
    outcome: Outcome
    items: list[dict[str, Any]] | None
    cursor_loop: str | None = None
    """A ``nextCursor`` the server repeated (a real pagination defect)."""
    truncated: str | None = None
    """Why mcpcheck stopped the walk itself (page or byte budget); never a server defect."""

    @property
    def ok(self) -> bool:
        """Whether every page answered ``ok`` and items could be collected."""
        return self.outcome.ok and self.items is not None

    @property
    def reply(self) -> RawReply:
        """The reply that ended the walk (the failing page, or the last page)."""
        return self.pages[-1]

    @property
    def http_status(self) -> int:
        """HTTP status of :attr:`reply`."""
        return self.reply.status

    @property
    def order(self) -> list[str]:
        """Item identities in server order."""
        return [identity(it, self.method) for it in self.items or []]

    @property
    def keys(self) -> list[str]:
        """Sorted item identities (a multiset: duplicates are kept)."""
        return sorted(self.order)

    @property
    def defs(self) -> list[str]:
        """Sorted canonical JSON of every item."""
        return sorted(canon(it) for it in self.items or [])

    def by_identity(self) -> dict[str, list[dict[str, Any]]]:
        """Items grouped by identity (lists, to keep duplicates)."""
        grouped: dict[str, list[dict[str, Any]]] = {}
        for it in self.items or []:
            grouped.setdefault(identity(it, self.method), []).append(it)
        return grouped


@dataclass
class ConnectionSample:
    """Everything observed on one fresh connection."""

    connection: int
    label: str
    tools_first: ListSample | None = None
    tools_second: ListSample | None = None
    others: dict[str, ListSample] = field(default_factory=dict)
    client_port: str | None = None

    @property
    def valid(self) -> bool:
        """Both ``tools/list`` walks answered ``ok``."""
        return (
            self.tools_first is not None
            and self.tools_first.ok
            and self.tools_second is not None
            and self.tools_second.ok
        )

    @property
    def failure(self) -> ListSample | None:
        """The first ``tools/list`` walk that did not answer ``ok``."""
        for sample in (self.tools_first, self.tools_second):
            if sample is not None and not sample.ok:
                return sample
        return None

    def first_of(self, method: str) -> ListSample | None:
        """The first walk of ``method`` on this connection."""
        return self.tools_first if method == TOOLS else self.others.get(method)


@dataclass
class _Observation:
    """One ``Mcp-Session-Id`` response header on a modern exchange."""

    seq: int
    connection: str
    method: str
    http_status: int
    value: str
    header_as_sent: str | None


def scan_session_ids(
    exchanges: Iterable[Exchange], casings: Mapping[int, str] | None = None
) -> tuple[list[_Observation], list[_Observation]]:
    """Split modern POST exchanges carrying ``Mcp-Session-Id`` into (minted, echoed).

    Legacy-era exchanges (header-less / ``initialize``) are excluded (S10). A received
    value we sent ourselves is *echoed*; any other value is *minted*. ``casings`` maps
    an exchange ``seq`` to the header name as spelled on the wire (the transcript
    itself stores lowercase names).
    """
    minted: list[_Observation] = []
    echoed: list[_Observation] = []
    for e in exchanges:
        if e.legacy or e.verb != "POST" or not e.rpc_method or not e.session_ids_received:
            continue
        sent = set(e.session_ids_sent)
        for value in e.session_ids_received:
            obs = _Observation(
                seq=e.seq,
                connection=e.connection,
                method=e.rpc_method,
                http_status=e.status,
                value=value,
                header_as_sent=(casings or {}).get(e.seq),
            )
            (echoed if value in sent else minted).append(obs)
    return minted, echoed


# --------------------------------------------------------------------------- the check


@dataclass
class _State:
    """Mutable per-run state (one ``SessionCheck`` instance may be reused)."""

    t0: float
    bogus_value: str
    methods: list[str]
    unsupported: set[str] = field(default_factory=set)
    samples: list[ConnectionSample] = field(default_factory=list)
    bogus: ListSample | None = None
    casings: dict[int, str] = field(default_factory=dict)
    summaries: dict[str, tuple[str, str]] = field(default_factory=dict)
    details: list[str] = field(default_factory=list)
    inconclusive: str | None = None
    bytes_total: int = 0
    """Body bytes fetched by every walk of this check (bounded by ``WALK_BYTE_BUDGET``)."""

    def note(self, code: str, template: str, message: str = "") -> None:
        """Remember the summary for ``code`` (first occurrence wins).

        ``template`` may contain ``{msg}``: the server's error message is fitted into
        whatever room the 100-character summary leaves (tail kept, like ``…Missing
        session ID``).
        """
        self.summaries.setdefault(code, (template, message))


class SessionCheck(Check):
    """No Mcp-Session-Id; list results stable across connections."""

    name: ClassVar[str] = "session"
    title: ClassVar[str] = "No Mcp-Session-Id; list results stable across connections"
    spec_url: ClassVar[str] = spec_urls.SPEC_EARLIER_REVISIONS
    order: ClassVar[int] = 20

    # ------------------------------------------------------------------ entry point
    async def run(self, ctx: CheckContext, f: Findings) -> None:
        """Sample three fresh connections plus a bogus-id probe and judge statelessness."""
        st = _State(
            t0=time.perf_counter(),
            bogus_value=BOGUS_PREFIX + secrets.token_hex(8),
            methods=await self._methods(ctx),
        )
        await self._collect(ctx, st)
        first = st.samples[0].tools_first if st.samples else None
        required = first is not None and not first.ok and _looks_session_required(first)
        if first is not None and not first.ok and not required:
            st.inconclusive = _not_evaluated(first)
        if required and first is not None:
            self._report_required(f, st, first)
        self._judge_bogus(ctx, f, st, required=required)
        if not required and st.inconclusive is None:
            await self._judge_samples(ctx, f, st)
        self._judge_session_ids(ctx, f, st)
        self._finish(f, st)

    # ------------------------------------------------------------------ traffic
    async def _methods(self, ctx: CheckContext) -> list[str]:
        """``tools/list`` always; the other lists when declared or when capabilities are unknown."""
        caps = await ctx.capabilities()
        methods = [TOOLS]
        methods.extend(m for m in SECONDARY_METHODS if caps is None or CAPABILITY_KEY[m] in caps)
        return methods

    async def _collect(self, ctx: CheckContext, st: _State) -> None:
        for k in range(1, wire.SESSION_CONNECTIONS + 1):
            if k > 1 and self._over_budget(ctx, st):
                why = (
                    f"the {wire.WALK_BYTE_BUDGET // (1024 * 1024)} MiB byte budget was exhausted"
                    if st.bytes_total > wire.WALK_BYTE_BUDGET
                    else f"{BUDGET_FRACTION:.0%} of the check budget "
                    f"({ctx.cfg.check_budget:g}s) was spent"
                )
                st.details.append(f"Stopped after {k - 1} connection(s): {why}.")
                break
            async with ctx.fresh(f"session-{k}") as raw:
                sample = await self._sample_connection(ctx, raw, k, st)
            st.samples.append(sample)
            if k == 1 and not sample.valid:
                break  # inconclusive or session required: no point in more connections
        async with ctx.fresh(f"session-{wire.SESSION_CONNECTIONS + 1}") as raw:
            st.bogus = await self._walk(
                ctx,
                raw,
                TOOLS,
                "bogus",
                st,
                headers={wire.H_SESSION_ID: st.bogus_value},
                purpose="bogus Mcp-Session-Id probe",
            )

    async def _sample_connection(
        self, ctx: CheckContext, raw: RawClient, k: int, st: _State
    ) -> ConnectionSample:
        sample = ConnectionSample(connection=k, label=raw.label)
        sample.tools_first = await self._walk(ctx, raw, TOOLS, k, st, purpose="tools/list (first)")
        sample.client_port = sample.tools_first.pages[0].headers.get("x-client-port")
        if not sample.tools_first.ok:
            return sample
        for method in st.methods[1:]:
            if method in st.unsupported:
                continue
            walk = await self._walk(ctx, raw, method, k, st, purpose=method)
            if walk.outcome.kind is OutcomeKind.METHOD_NOT_FOUND:
                st.unsupported.add(method)
            sample.others[method] = walk
        sample.tools_second = await self._walk(
            ctx, raw, TOOLS, k, st, purpose="tools/list (second)"
        )
        return sample

    async def _walk(
        self,
        ctx: CheckContext,
        raw: RawClient,
        method: str,
        connection: int | str,
        st: _State,
        *,
        purpose: str,
        headers: Mapping[str, str] | None = None,
    ) -> ListSample:
        """Full ``nextCursor`` walk of ``method``; SSE streams are closed after the response (S14)."""
        pages: list[RawReply] = []
        seen: set[str] = set()
        cursor: str | None = None
        loop: str | None = None
        truncated: str | None = None
        while True:
            params = {"cursor": cursor} if cursor is not None else None
            reply = await raw.post(
                method,
                params,
                headers=headers,
                purpose=f"{purpose} page {len(pages)}",
                check=self.name,
            )
            pages.append(reply)
            st.bytes_total += len(reply.raw)
            if reply.session_ids:
                casing = _wire_casing(reply.headers.raw)
                if casing is not None:
                    st.casings[len(ctx.transcript)] = casing
            outcome = self._classify(ctx, reply)
            if not outcome.ok:
                break
            nxt = (reply.result or {}).get("nextCursor")
            if not isinstance(nxt, str):
                break
            if nxt in seen:
                loop = nxt  # the server repeated a cursor: a pagination defect (S13)
                break
            if len(pages) >= wire.MAX_PAGES:
                truncated = f"{wire.MAX_PAGES} pages (MAX_PAGES)"
                break
            if st.bytes_total > wire.WALK_BYTE_BUDGET:
                truncated = walk_budget_reason(st.bytes_total, len(pages))
                break
            seen.add(nxt)
            cursor = nxt
        items = items_of(pages, LIST_ITEM_KEYS[method]) if outcome.ok else None
        return ListSample(
            method, connection, pages, outcome, items, cursor_loop=loop, truncated=truncated
        )

    @staticmethod
    def _classify(ctx: CheckContext, reply: RawReply) -> Outcome:
        return classify_reply(
            reply, sent_origin=ctx.cfg.origin is not None, sent_token=bool(ctx.cfg.token)
        )

    @staticmethod
    def _over_budget(ctx: CheckContext, st: _State) -> bool:
        return (
            time.perf_counter() - st.t0 > BUDGET_FRACTION * ctx.cfg.check_budget
            or st.bytes_total > wire.WALK_BYTE_BUDGET
        )

    # ------------------------------------------------------------------ verdicts
    def _report_required(self, f: Findings, st: _State, first: ListSample) -> None:
        reply = first.reply
        code, message = _rpc_of(reply)
        f.must(
            "session.required",
            f"modern tools/list without Mcp-Session-Id was rejected with HTTP {reply.status}"
            f"{_rpc_text(code, message)}; a 2026-07-28 server MUST NOT require a session",
            subject="Mcp-Session-Id",
            spec_url=spec_urls.SPEC_STATELESSNESS,
            evidence={
                "connection": 1,
                "method": TOOLS,
                "http_status": reply.status,
                "rpc_code": code,
                "rpc_message": message,
                "fingerprint": "legacy_session" if _legacy_fingerprint(reply) else "session_word",
            },
        )
        rpc = f", {code}" if code is not None else ""
        st.note(
            "session.required",
            f"Server requires `Mcp-Session-Id` on modern requests (HTTP {reply.status}{rpc}"
            + (' "{msg}")' if message else ")"),
            message,
        )

    def _judge_bogus(self, ctx: CheckContext, f: Findings, st: _State, *, required: bool) -> None:
        bogus = st.bogus
        if bogus is None:
            return
        reply = bogus.reply
        if bogus.ok:
            return  # content comparisons happen in _judge_samples
        if bogus.outcome.kind is OutcomeKind.TRANSPORT:
            st.details.append(f"Bogus-id probe got no usable answer: {bogus.outcome.detail}.")
            if not required and st.inconclusive is None:
                st.inconclusive = _not_evaluated(bogus)
            return
        sample1_ok = bool(st.samples) and st.samples[0].valid
        if not (sample1_ok or _looks_session_required(bogus)):
            return  # the plain request failed for the same infrastructure reason
        code, message = _rpc_of(reply)
        # 2026-07-28 asks for this only of servers that speak *nothing else*: "A server
        # that supports only this revision ... SHOULD ... ignore [an Mcp-Session-Id]"
        # (transports#earlier-streamable-http-revisions). There is no MUST about sessions
        # anywhere in the revision, and a dual-era server is obeying 2025-11-25, which
        # requires exactly this 404 -- so failing it would be wrong twice over.
        dual_era = ctx.era is Era.DUAL_ERA
        report = f.info if dual_era else f.should
        why = (
            "the server also serves pre-2026 clients, and 2025-11-25 requires it to reject "
            "an id it never issued; noted only because a 2026-07-28-only server SHOULD "
            "ignore the header instead"
            if dual_era
            else "a server that supports only 2026-07-28 SHOULD ignore an id it never issued"
        )
        report(
            "session.bogus_id_rejected_dual_era" if dual_era else "session.bogus_id_rejected",
            f"tools/list with an unknown Mcp-Session-Id was rejected with HTTP {reply.status}"
            f"{_rpc_text(code, message)}; {why}",
            subject="Mcp-Session-Id",
            spec_url=spec_urls.SPEC_EARLIER_REVISIONS,
            evidence={
                "connection": "bogus",
                "method": TOOLS,
                "http_status": reply.status,
                "rpc_code": code,
                "rpc_message": message,
                "header_as_sent": wire.H_SESSION_ID,
                "value": st.bogus_value,
            },
        )
        st.note(
            "session.bogus_id_rejected_dual_era" if dual_era else "session.bogus_id_rejected",
            f"Server enforces `Mcp-Session-Id` (bogus id → HTTP {reply.status}"
            + (' "{msg}")' if message else ")"),
            message,
        )

    async def _judge_samples(self, ctx: CheckContext, f: Findings, st: _State) -> None:
        valid = [s for s in st.samples if s.valid]
        if len(valid) < 2:
            failed = next((s for s in st.samples if not s.valid), None)
            failure = failed.failure if failed is not None else None
            st.inconclusive = (
                _not_evaluated(failure)
                if failure is not None
                else "Not evaluated: fewer than 2 fresh connections completed within the budget"
            )
            return
        if len(valid) < wire.SESSION_CONNECTIONS:
            self._report_partial(f, st, valid)
        for method in st.methods:
            self._compare_method(f, st, valid, method)
        self._compare_cursor_loops(f, st)
        self._report_truncated(f, st)
        await self._compare_sample0(ctx, f, st, valid[0])

    def _report_partial(self, f: Findings, st: _State, valid: list[ConnectionSample]) -> None:
        failed = [s for s in st.samples if not s.valid]
        missing = wire.SESSION_CONNECTIONS - len(st.samples)
        reasons = [
            f"connection {s.connection}: {s.failure.outcome.detail}"
            for s in failed
            if s.failure is not None
        ]
        if missing:
            reasons.append(f"{missing} connection(s) not attempted (check budget)")
        first_failed = failed[0] if failed else None
        f.should(
            "session.partial_samples",
            f"only {len(valid)} of {wire.SESSION_CONNECTIONS} fresh connections answered "
            f"tools/list ({'; '.join(reasons)})",
            subject=TOOLS,
            spec_url=spec_urls.SPEC_STATELESSNESS,
            evidence={
                "connection": first_failed.connection if first_failed else None,
                "method": TOOLS,
                "http_status": (
                    first_failed.failure.http_status
                    if first_failed and first_failed.failure
                    else None
                ),
                "valid_connections": [s.connection for s in valid],
            },
        )
        st.note(
            "session.partial_samples",
            f"Only {len(valid)} of {wire.SESSION_CONNECTIONS} fresh connections answered; "
            "lists compared on those",
        )

    def _compare_method(
        self, f: Findings, st: _State, valid: list[ConnectionSample], method: str
    ) -> None:
        samples: list[tuple[int | str, ListSample]] = []
        for s in valid:
            walk = s.first_of(method)
            if walk is not None and walk.ok:
                samples.append((s.connection, walk))
        if not samples:
            return
        ref_conn, ref = samples[0]
        spec = SPEC_FOR[method]
        noun = NOUN[method]
        # (a) identical sets across fresh connections (S6/S7/S8)
        sets = {str(c): _clip_ids(ls.keys) for c, ls in samples}
        differing = [(c, ls) for c, ls in samples[1:] if ls.keys != ref.keys]
        if differing:
            c, ls = differing[0]
            distinct = len({tuple(ls.keys) for _, ls in samples})
            f.must(
                "session.list_varies_per_connection",
                f"{noun} set differs between fresh connections although the same "
                "Authorization was presented",
                subject=method,
                spec_url=spec,
                evidence={
                    "connection": c,
                    "method": method,
                    "http_status": ls.http_status,
                    "sets": sets,
                },
            )
            st.note(
                "session.list_varies_per_connection",
                f"{method} varies per connection ({len(samples)} connections, "
                f"{distinct} distinct {noun} sets)",
            )
            st.details.append(f"{method} per connection: {json.dumps(sets, ensure_ascii=False)}")
        all_samples: list[tuple[int | str, str, ListSample]] = [
            (c, "first", ls) for c, ls in samples
        ]
        if method == TOOLS:
            # (b) no side effect of other requests on the same connection (S6)
            for s in valid:
                a, b = s.tools_first, s.tools_second
                if a is None or b is None or not b.ok:
                    continue
                all_samples.append((s.connection, "second", b))
                if a.keys != b.keys and "session.list_varies_within_connection" not in st.summaries:
                    f.must(
                        "session.list_varies_within_connection",
                        "tool set changed between two tools/list calls on one connection "
                        "after resources/prompts requests in between",
                        subject=method,
                        spec_url=spec,
                        evidence={
                            "connection": s.connection,
                            "method": method,
                            "http_status": b.http_status,
                            "sets": {"first": _clip_ids(a.keys), "second": _clip_ids(b.keys)},
                        },
                    )
                    st.note(
                        "session.list_varies_within_connection",
                        f"{method} changed within one connection after other requests",
                    )
            # (c) the bogus-id probe sees the same set (S1 + S6)
            bogus = st.bogus
            if bogus is not None and bogus.ok:
                all_samples.append(("bogus", "first", bogus))
                if bogus.keys != ref.keys:
                    f.must(
                        "session.list_varies_with_session_header",
                        "tool set differs when an unknown Mcp-Session-Id is sent; the header "
                        "MUST be ignored and the set MUST NOT vary per connection",
                        subject=method,
                        spec_url=spec,
                        evidence={
                            "connection": "bogus",
                            "method": method,
                            "http_status": bogus.http_status,
                            "header_as_sent": wire.H_SESSION_ID,
                            "value": st.bogus_value,
                            "sets": {
                                str(ref_conn): _clip_ids(ref.keys),
                                "bogus": _clip_ids(bogus.keys),
                            },
                        },
                    )
                    st.note(
                        "session.list_varies_with_session_header",
                        f"{method} differs when a bogus Mcp-Session-Id is sent",
                    )
        # (d) same identities, different bytes (conservative SHOULD)
        self._compare_definitions(f, st, method, ref, ref_conn, all_samples)
        # (e) deterministic order (S9, tools only)
        if method == TOOLS:
            self._compare_order(f, st, method, ref, ref_conn, all_samples)

    def _compare_definitions(
        self,
        f: Findings,
        st: _State,
        method: str,
        ref: ListSample,
        ref_conn: int | str,
        all_samples: list[tuple[int | str, str, ListSample]],
    ) -> None:
        ref_items = ref.by_identity()
        for c, which, ls in all_samples:
            if ls is ref or ls.keys != ref.keys or ls.defs == ref.defs:
                continue
            other_items = ls.by_identity()
            item_id, pointer = "", ""
            for ident in sorted(ref_items):
                mine, theirs = ref_items[ident], other_items.get(ident, [])
                if [canon(x) for x in mine] != [canon(x) for x in theirs]:
                    item_id = ident
                    pointer = first_diff_pointer(mine[0], theirs[0]) if theirs else ""
                    break
            f.should(
                "session.definition_unstable",
                f"{NOUN[method]} {item_id!r} has the same identity but different definition "
                f"bytes {_where(ref_conn, c, which)}",
                subject=method,
                spec_url=SPEC_FOR[method],
                evidence={
                    "connection": c,
                    "method": method,
                    "http_status": ls.http_status,
                    "item": item_id,
                    "first_diff_pointer": pointer,
                },
            )
            st.note(
                "session.definition_unstable",
                f"{method} definitions differ across connections (same {NOUN[method]} names)",
            )
            return

    def _compare_order(
        self,
        f: Findings,
        st: _State,
        method: str,
        ref: ListSample,
        ref_conn: int | str,
        all_samples: list[tuple[int | str, str, ListSample]],
    ) -> None:
        for c, which, ls in all_samples:
            if ls is ref or ls.defs != ref.defs or ls.order == ref.order:
                continue
            f.should(
                "session.order_not_deterministic",
                f"tools/list returned the same tools in a different order {_where(ref_conn, c, which)}"
                "; servers SHOULD return tools in a deterministic order",
                subject=method,
                spec_url=SPEC_FOR[method],
                evidence={
                    "connection": c,
                    "method": method,
                    "http_status": ls.http_status,
                    "order": {
                        str(ref_conn): _clip_ids(ref.order),
                        (str(c) if which == "first" else f"{c} (second)"): _clip_ids(ls.order),
                    },
                },
            )
            st.note(
                "session.order_not_deterministic",
                f"{method} order differs across connections (set identical)",
            )
            return

    @staticmethod
    def _all_walks(st: _State) -> list[ListSample]:
        walks: list[ListSample] = []
        for s in st.samples:
            walks.extend(x for x in (s.tools_first, s.tools_second, *s.others.values()) if x)
        if st.bogus is not None:
            walks.append(st.bogus)
        return walks

    def _compare_cursor_loops(self, f: Findings, st: _State) -> None:
        reported: set[str] = set()
        for ls in self._all_walks(st):
            if ls.cursor_loop is None or ls.method in reported:
                continue
            reported.add(ls.method)
            f.should(
                "session.cursor_loop",
                f"{ls.method} pagination repeated nextCursor {ls.cursor_loop!r} on connection "
                f"{ls.connection}; the walk was stopped",
                subject=ls.method,
                spec_url=spec_urls.SPEC_PAGINATION_GUIDELINES,
                evidence={
                    "connection": ls.connection,
                    "method": ls.method,
                    "http_status": ls.http_status,
                    "cursor": ls.cursor_loop,
                    "pages": len(ls.pages),
                },
            )
            st.note("session.cursor_loop", f"{ls.method} pagination repeats a cursor")

    def _report_truncated(self, f: Findings, st: _State) -> None:
        """A walk cut short by mcpcheck's own budget is disclosed, never held against the server."""
        reported: set[str] = set()
        for ls in self._all_walks(st):
            if ls.truncated is None or ls.method in reported:
                continue
            reported.add(ls.method)
            f.info(
                CODE_WALK_TRUNCATED,
                f"{ls.method} still had a nextCursor after {ls.truncated} on connection "
                f"{ls.connection}; lists were compared on the pages fetched",
                subject=ls.method,
                spec_url=spec_urls.SPEC_PAGINATION_GUIDELINES,
                evidence={
                    "connection": ls.connection,
                    "method": ls.method,
                    "http_status": ls.http_status,
                    "pages": len(ls.pages),
                    "stopped": ls.truncated,
                },
            )
            st.details.append(
                f"{ls.method}: pagination stopped by mcpcheck after {ls.truncated}; "
                "comparisons cover the pages fetched."
            )

    async def _compare_sample0(
        self, ctx: CheckContext, f: Findings, st: _State, first: ConnectionSample
    ) -> None:
        """``ctx.tools()`` (handshake/gate cache) vs sample 1: tools MAY change over time."""
        tools0 = await ctx.tools()
        ref = first.tools_first
        if tools0 is None or ref is None:
            return
        keys0 = sorted(identity(t, TOOLS) for t in tools0)
        if keys0 == ref.keys:
            return
        f.best_practice(
            "session.list_changed_during_run",
            "the tool set returned to the handshake differs from the first fresh-connection "
            "sample (tools MAY change over time, but not within one short run)",
            subject=TOOLS,
            spec_url=SPEC_FOR[TOOLS],
            evidence={
                "connection": None,
                "method": TOOLS,
                "http_status": ref.http_status,
                "sets": {"0": _clip_ids(keys0), str(first.connection): _clip_ids(ref.keys)},
            },
        )
        st.note(
            "session.list_changed_during_run",
            "tools/list changed between the handshake and the session samples",
        )

    def _judge_session_ids(self, ctx: CheckContext, f: Findings, st: _State) -> None:
        minted, echoed = scan_session_ids(ctx.transcript, st.casings)
        for method, obs in _group_by_method(minted).items():
            _add_minted(f, method, obs)
        if minted:
            st.note(
                "session.session_id_minted",
                "`Mcp-Session-Id` minted on modern responses (not required; SHOULD not mint)",
            )
            st.details.append(
                "Mcp-Session-Id minted on "
                f"{len(minted)} modern response(s) to {', '.join(sorted(_group_by_method(minted)))}."
            )
        if echoed:
            first = echoed[0]
            f.should(
                "session.session_id_echoed",
                f"the client-supplied Mcp-Session-Id {first.value!r} was echoed back on the "
                f"response to {first.method} (HTTP {first.http_status}); a server that does not "
                "use sessions SHOULD ignore the header",
                subject="Mcp-Session-Id",
                spec_url=spec_urls.SPEC_EARLIER_REVISIONS,
                evidence={
                    "connection": "bogus",
                    "method": first.method,
                    "http_status": first.http_status,
                    "header_as_sent": first.header_as_sent,
                    "value": first.value,
                    "occurrences": len(echoed),
                },
            )
            st.note(
                "session.session_id_echoed",
                "Client-supplied `Mcp-Session-Id` echoed back (SHOULD be ignored)",
            )

    # ------------------------------------------------------------------ summary
    def _finish(self, f: Findings, st: _State) -> None:
        ports = [s.client_port for s in st.samples if s.client_port]
        where = f" (client ports {', '.join(ports)})" if len(ports) == len(st.samples) else ""
        compared = [m for m in st.methods if m not in st.unsupported]
        st.details.insert(
            0,
            f"{len(st.samples)} fresh connection(s){where} + 1 bogus-id probe; "
            f"lists compared: {', '.join(compared)}"
            + (f"; not implemented: {', '.join(sorted(st.unsupported))}" if st.unsupported else "")
            + ".",
        )
        for line in st.details:
            f.details(line)
        present = [code for code in _PRIORITY if code in st.summaries]
        if st.inconclusive is not None:
            f.inconclusive(st.inconclusive)
            return
        if not present:
            n = sum(1 for s in st.samples if s.valid)
            f.summary(
                f"No `Mcp-Session-Id` issued or expected; lists identical across {n} connections"
            )
            return
        total = len(f.failures) + len(f.warnings)
        extra = f" (+{total - 1} more)" if total > 1 else ""
        template, message = st.summaries[present[0]]
        room = SUMMARY_MAX - len(template.replace("{msg}", "")) - len(extra)
        f.summary(template.replace("{msg}", _tail(message, room)) + extra)


# --------------------------------------------------------------------------- module helpers


def _rpc_of(reply: RawReply) -> tuple[int | None, str]:
    """``(code, message)`` of the first JSON-RPC error in the reply, id-agnostic."""
    for msg in reply.messages:
        err = wire.rpc_error_of(msg)
        if err is not None:
            return err[0], err[1]
    return None, ""


def _where(ref_conn: int | str, c: int | str, which: str) -> str:
    """Describe which two samples differ, for finding messages."""
    if c == ref_conn:
        return f"between the first and second tools/list call on connection {c}"
    if c == "bogus":
        return f"on connection {ref_conn} and the bogus-id probe"
    suffix = " (second tools/list call)" if which == "second" else ""
    return f"on connection {ref_conn} and connection {c}{suffix}"


def _rpc_text(code: int | None, message: str) -> str:
    """``, -32600 "message"`` for finding messages (empty when there was no JSON-RPC error)."""
    parts = []
    if code is not None:
        parts.append(str(code))
    if message:
        parts.append(f'"{_quote(message)}"')
    return ", " + " ".join(parts) if parts else ""


def _legacy_fingerprint(reply: RawReply) -> bool:
    return any(
        wire.is_response(m) and m.get("id") == LEGACY_SESSION_ERROR_ID for m in reply.messages
    )


def _looks_session_required(sample: ListSample) -> bool:
    """A rejection that names sessions, or carries the mcp 1.x ``server-error`` id."""
    if sample.ok or sample.outcome.kind is OutcomeKind.TRANSPORT:
        return False
    _, message = _rpc_of(sample.reply)
    return bool(_SESSION_WORD.search(message)) or _legacy_fingerprint(sample.reply)


def _not_evaluated(sample: ListSample) -> str:
    outcome = sample.outcome
    if outcome.kind is OutcomeKind.METHOD_NOT_FOUND:
        return f"Not evaluated: {sample.method} not implemented (-32601)"
    if outcome.detail.startswith("Not evaluated"):
        return outcome.detail
    return f"Not evaluated: {sample.method} → {outcome.detail}"


def _group_by_method(observations: Iterable[_Observation]) -> dict[str, list[_Observation]]:
    grouped: dict[str, list[_Observation]] = {}
    for obs in observations:
        grouped.setdefault(obs.method, []).append(obs)
    return grouped


def _add_minted(f: Findings, method: str, obs: list[_Observation]) -> None:
    first = obs[0]
    values = sorted({o.value for o in obs})
    # the transcript stores lowercase header names: take the wire casing from any
    # occurrence whose reply the session check saw itself
    casing = next((o.header_as_sent for o in obs if o.header_as_sent), None)
    f.should(
        "session.session_id_minted",
        f"response to {method} carried Mcp-Session-Id although none was sent "
        f"({len(obs)} occurrence{'s' if len(obs) != 1 else ''}); a server supporting only "
        "2026-07-28 SHOULD not mint session ids",
        subject=method,
        spec_url=spec_urls.SPEC_EARLIER_REVISIONS,
        evidence={
            "connection": first.connection,
            "method": method,
            "http_status": first.http_status,
            "header_as_sent": casing,
            "value": first.value,
            "values": _clip_ids(values),
            "occurrences": len(obs),
        },
    )


def amend_result(result: CheckResult, transcript: Transcript) -> CheckResult:
    """Append ``session.session_id_minted`` warnings found by re-scanning ``transcript``.

    Intended for the runner after all checks ran (PLAN.md §7.2 step 3g): exchanges
    recorded by later checks may reveal minting the session check could not see.
    Methods already reported in ``result.warnings`` are skipped; a ``pass`` result
    becomes ``warn``. Returns a new result (the input is not modified).
    """
    minted, _ = scan_session_ids(transcript)
    already = {w.subject for w in result.warnings if w.code == "session.session_id_minted"}
    new_groups = {m: obs for m, obs in _group_by_method(minted).items() if m not in already}
    if not new_groups:
        return result
    f = Findings(SessionCheck())
    for method, obs in new_groups.items():
        _add_minted(f, method, obs)
    warnings: list[Finding] = [*result.warnings, *f.warnings]
    status = Status.WARN if result.status is Status.PASS else result.status
    summary = result.summary
    if result.status is Status.PASS:
        summary = "`Mcp-Session-Id` minted on modern responses (not required; SHOULD not mint)"
        if len(warnings) > 1:
            summary += f" (+{len(warnings) - 1} more)"
    return result.model_copy(update={"warnings": warnings, "status": status, "summary": summary})


__all__ = [
    "BOGUS_PREFIX",
    "CODE_WALK_TRUNCATED",
    "ConnectionSample",
    "ListSample",
    "SessionCheck",
    "amend_result",
    "canon",
    "first_diff_pointer",
    "identity",
    "scan_session_ids",
]
