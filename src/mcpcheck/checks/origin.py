"""The ``origin`` check (PLAN.md §7.7): a forged ``Origin`` header is rejected with HTTP 403.

Five raw probes, each on its own fresh connection (T:57-58 reads "all incoming
connections"), all read-only:

* P1 ``origin:evil`` — ``server/discover`` with ``Origin: https://evil.example.com``
  (must be refused with 403, T:59-61);
* P2 ``origin:same`` — the same request with ``--origin`` or the origin derived from
  the target URL (control);
* P3 ``origin:none`` — the same request without an ``Origin`` header (control);
* P4 ``origin:null`` — ``Origin: null`` (opaque origin, best-practice only);
* P5 ``origin:preflight`` — one ``OPTIONS`` with the evil origin (CORS, info only).

The verdict compares P1 with the first control that was processed. Localhost
targets are not exempt: DNS rebinding is precisely the local-server threat
(SBP "Local MCP Server Compromise").
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar

from mcpcheck import spec_urls, wire
from mcpcheck.checks.base import Check

if TYPE_CHECKING:
    from mcpcheck.checks.base import CheckContext, Findings
    from mcpcheck.client import RawReply

PROBE_METHOD = "server/discover"
"""Body of P1-P4: always implemented on a 2026-07-28 server (DI:9-11), even without tools."""

NULL_ORIGIN = "null"
BODY_EXCERPT_CHARS = 200
EXPECTED_403 = "HTTP 403 Forbidden (body optional: JSON-RPC error without id)"
PREFLIGHT_REQUEST_HEADERS = ("content-type", wire.H_METHOD, wire.H_PROTOCOL_VERSION)

_SPEC_NOTE = (
    "Servers MUST validate the Origin header on all incoming connections and MUST answer "
    "HTTP 403 Forbidden when it is present and invalid "
    f"({spec_urls.SPEC_ORIGIN_FRAGMENT}). The threat is DNS rebinding: a page on a foreign "
    "origin drives a server on the victim's loopback interface from their browser "
    f"({spec_urls.SPEC_SECURITY_LOCAL}). Localhost targets are therefore not exempt."
)
_SDK_HINT = (
    "Python SDK note: MCPServer.streamable_http_app(host='0.0.0.0') or "
    "TransportSecuritySettings(enable_dns_rebinding_protection=False) disables the automatic "
    "Origin validation even when the process is bound to 127.0.0.1."
)


class ProbeOutcome(str, Enum):
    """How the server answered one probe (``(str, Enum)`` keeps Python 3.10 support)."""

    RESULT = "result"
    RPC_ERROR = "rpc_error"
    HTTP_REJECT = "http_reject"
    NO_RESULT_2XX = "no_result_2xx"
    REDIRECT = "redirect"
    TRANSPORT = "transport"


class Baseline(str, Enum):
    """What the control probes established."""

    RESULT = "result"  # a control probe got a JSON-RPC result
    RPC_LAYER = "rpc_layer"  # controls answered with a JSON-RPC error (e.g. legacy-only)
    AUTH = "auth"  # 401 / 403 on the controls: credentials or --origin missing
    DOWN = "down"  # nothing usable came back


@dataclass(frozen=True)
class Probe:
    """Classification of one probe reply (PLAN.md §7.7 part 3)."""

    id: str
    origin: str | None
    outcome: ProbeOutcome
    status: int
    rpc_code: int | None
    rpc_has_id: bool
    media_type: str
    www_authenticate: str | None
    location: str | None
    body_excerpt: str
    error: str | None
    elapsed_ms: int
    allow_origin: str | None = None
    allow_credentials: str | None = None

    @property
    def rejected(self) -> bool:
        """Anything but a JSON-RPC result."""
        return self.outcome is not ProbeOutcome.RESULT

    @property
    def timed_out(self) -> bool:
        """A transport failure that was a timeout rather than a closed connection."""
        err = self.error or ""
        return self.outcome is ProbeOutcome.TRANSPORT and (
            "budget" in err or "Timeout" in err or "timed out" in err
        )

    def processed_like(self, other: Probe) -> bool:
        """``outcome``, ``status`` and ``rpc_code`` identical (same code path on the server)."""
        return (
            self.outcome is other.outcome
            and self.status == other.status
            and self.rpc_code == other.rpc_code
        )

    def describe(self) -> str:
        """Short human description used in summaries, messages and details."""
        if self.outcome is ProbeOutcome.TRANSPORT:
            return f"no response ({self.error or 'transport failure'})"
        if self.outcome is ProbeOutcome.REDIRECT:
            return f"HTTP {self.status} redirect to {self.location or '?'}"
        if self.outcome is ProbeOutcome.RESULT:
            return f"HTTP {self.status} {self.media_type} JSON-RPC result"
        if self.outcome is ProbeOutcome.RPC_ERROR:
            return f"HTTP {self.status} JSON-RPC error {self.rpc_code}"
        if self.outcome is ProbeOutcome.NO_RESULT_2XX:
            return f"HTTP {self.status} without a JSON-RPC response"
        media = f" {self.media_type}" if self.media_type else ""
        return f"HTTP {self.status}{media}"

    def evidence(self, *, got: str, expected: str, method: str = PROBE_METHOD) -> dict[str, Any]:
        """The §7.7(6) evidence object."""
        ev: dict[str, Any] = {
            "request": {"method": method, "origin": self.origin, "fresh_connection": True},
            "http_status": self.status,
            "media_type": self.media_type,
            "got": got,
            "expected": expected,
        }
        if self.rpc_code is not None:
            ev["rpc_code"] = self.rpc_code
        if self.body_excerpt:
            ev["body_excerpt"] = self.body_excerpt
        if self.location:
            ev["location"] = self.location
        if self.error:
            ev["error"] = self.error
        return ev


def _excerpt(raw: bytes) -> str:
    text, _ = wire.decode_utf8(raw[: BODY_EXCERPT_CHARS * 4])
    if text is None:
        return f"<{len(raw)} bytes, not utf-8>"
    text = " ".join(text.split())
    return text if len(text) <= BODY_EXCERPT_CHARS else text[: BODY_EXCERPT_CHARS - 1] + "…"


def classify(reply: RawReply, probe_id: str, origin: str | None) -> Probe:
    """Reduce a :class:`~mcpcheck.client.RawReply` to a :class:`Probe` (pure)."""
    status = reply.status
    rpc: tuple[int, str, Any] | None = None
    rpc_has_id = False
    for msg in reply.messages:
        err = wire.rpc_error_of(msg)
        if err is not None:
            rpc = err
            rpc_has_id = msg.get("id") is not None
            break
    response = reply.response
    has_result = response is not None and "result" in response
    if reply.error is not None:
        outcome = ProbeOutcome.TRANSPORT
    elif reply.redirect_blocked is not None or 300 <= status <= 399:
        outcome = ProbeOutcome.REDIRECT
    elif 200 <= status <= 299 and has_result:
        outcome = ProbeOutcome.RESULT
    elif rpc is not None:
        outcome = ProbeOutcome.RPC_ERROR
    elif 200 <= status <= 299:
        outcome = ProbeOutcome.NO_RESULT_2XX
    else:
        outcome = ProbeOutcome.HTTP_REJECT
    location = reply.redirect_blocked or reply.headers.get("location")
    return Probe(
        id=probe_id,
        origin=origin,
        outcome=outcome,
        status=status,
        rpc_code=rpc[0] if rpc else None,
        rpc_has_id=rpc_has_id,
        media_type=reply.media_type,
        www_authenticate=reply.headers.get("www-authenticate"),
        location=location,
        body_excerpt=_excerpt(reply.raw) if reply.raw else "",
        error=reply.error,
        elapsed_ms=reply.elapsed_ms,
        allow_origin=reply.headers.get("access-control-allow-origin"),
        allow_credentials=reply.headers.get("access-control-allow-credentials"),
    )


def choose_baseline(p2: Probe, p3: Probe) -> tuple[Baseline, Probe | None]:
    """Pick the control: first of (P3, P2) with a result; otherwise AUTH / RPC_LAYER / DOWN."""
    for probe in (p3, p2):
        if probe.outcome is ProbeOutcome.RESULT:
            return Baseline.RESULT, probe
    if p3.status in (401, 407) or p2.status in (401, 407):
        return Baseline.AUTH, None
    scope = "insufficient_scope" in (p3.www_authenticate or "")
    if p3.status == 403 and (scope or p2.status == 403):
        return Baseline.AUTH, None
    if p3.outcome is ProbeOutcome.RPC_ERROR and p3.status not in (401, 403):
        return Baseline.RPC_LAYER, p3
    return Baseline.DOWN, None


class OriginCheck(Check):
    """Forged Origin header rejected with HTTP 403."""

    name: ClassVar[str] = "origin"
    title: ClassVar[str] = "Forged Origin header rejected with HTTP 403"
    spec_url: ClassVar[str] = spec_urls.SPEC_TRANSPORT
    order: ClassVar[int] = 80

    # ------------------------------------------------------------------ probes
    async def _post(self, ctx: CheckContext, probe: str, origin: str | None) -> Probe:
        headers: dict[str, str] = {"connection": "close"}
        omit: tuple[str, ...] = ()
        if origin is None:
            omit = (wire.H_ORIGIN,)
        else:
            headers[wire.H_ORIGIN] = origin
        async with ctx.fresh(f"origin-{probe}") as raw:
            reply = await raw.post(
                PROBE_METHOD,
                headers=headers,
                omit_headers=omit,
                purpose=f"origin:{probe}",
                check=self.name,
            )
        return classify(reply, f"origin:{probe}", origin)

    async def _preflight(self, ctx: CheckContext) -> Probe:
        request_headers = list(PREFLIGHT_REQUEST_HEADERS)
        if ctx.cfg.token:
            request_headers.append(wire.H_AUTHORIZATION)
        async with ctx.fresh("origin-preflight") as raw:
            reply = await raw.http(
                "OPTIONS",
                headers={
                    wire.H_ORIGIN: wire.EVIL_ORIGIN,
                    "access-control-request-method": "POST",
                    "access-control-request-headers": ",".join(request_headers),
                    "connection": "close",
                },
                purpose="origin:preflight",
                check=self.name,
            )
        return classify(reply, "origin:preflight", wire.EVIL_ORIGIN)

    # ------------------------------------------------------------------ run
    async def run(self, ctx: CheckContext, f: Findings) -> None:
        """Send P1-P5 on fresh connections and judge P1 against the control (§7.7)."""
        same_origin = ctx.cfg.origin or wire.derive_origin(ctx.cfg.url)
        p1 = await self._post(ctx, "evil", wire.EVIL_ORIGIN)
        p2 = await self._post(ctx, "same", same_origin)
        p3 = await self._post(ctx, "none", None)
        p4 = await self._post(ctx, "null", NULL_ORIGIN)
        p5 = await self._preflight(ctx)

        f.details(_probe_table(p1, p2, p3, p4, p5))
        baseline, control = choose_baseline(p2, p3)

        if control is None:
            self._inconclusive(f, baseline, p1, p2, p3, sent_token=bool(ctx.cfg.token))
            f.details(_SPEC_NOTE)
            return

        parts: list[str] = [self._judge_evil(f, p1, control, baseline)]
        if baseline is Baseline.RESULT:
            parts.extend(self._judge_controls(f, p2, p3, control, same_origin))
        if not f.failures:  # "null accepted" is subsumed when every Origin is accepted
            self._judge_null(f, p4, control, baseline, parts)
        self._judge_preflight(f, p5)

        if f.failures:
            f.details(_SDK_HINT)
        elif f.warnings:
            f.summary("; ".join(parts))
        else:
            f.summary("Origin header validated")
        f.details(_SPEC_NOTE)

    # ------------------------------------------------------------------ verdicts
    def _inconclusive(
        self,
        f: Findings,
        baseline: Baseline,
        p1: Probe,
        p2: Probe,
        p3: Probe,
        *,
        sent_token: bool,
    ) -> None:
        statuses = {p1.status, p2.status, p3.status}
        if baseline is Baseline.AUTH:
            if 401 in statuses or 407 in statuses:
                code = 401 if 401 in statuses else 407
                hint = "token rejected" if sent_token else "pass --token"
                where = "every probe" if statuses == {code} else "the control probes"
                f.inconclusive(f"Not evaluated: HTTP {code} on {where} ({hint})")
            else:
                where = "every probe" if statuses == {403} else "the control probes"
                hint = "try --origin <allowed origin> or check credentials"
                f.inconclusive(f"Not evaluated: HTTP 403 on {where} ({hint})")
            f.details(
                "The requests without a foreign Origin were refused too, so the Origin "
                "validation could not be separated from authentication (basic/authorization"
                f"#error-handling: {spec_urls.SPEC_AUTH_ERRORS})."
            )
            return
        f.inconclusive(f"Not evaluated: no baseline response ({p3.describe()})")
        f.details(
            "Neither the request without Origin nor the same-origin request produced a "
            "JSON-RPC response, so the evil-Origin reply cannot be compared with anything."
        )

    def _judge_evil(self, f: Findings, p1: Probe, control: Probe, baseline: Baseline) -> str:
        """Verdict for P1; returns the summary fragment describing it."""
        subject = p1.id
        if p1.outcome is ProbeOutcome.RESULT:
            f.must(
                "origin.evil_accepted",
                f"The server processed a request carrying a foreign Origin; a page on "
                f"{wire.EVIL_ORIGIN}, or any hostname DNS-rebound to this server, can drive it "
                "from a victim's browser",
                subject=subject,
                spec_url=spec_urls.SPEC_ORIGIN_FRAGMENT,
                evidence=p1.evidence(
                    got=f"JSON-RPC result for {PROBE_METHOD}", expected=EXPECTED_403
                ),
            )
            f.summary(
                f"Evil Origin accepted: {wire.EVIL_ORIGIN} got HTTP {p1.status} + result "
                "(expected 403)"
            )
            return "Evil Origin accepted"
        if baseline is Baseline.RPC_LAYER and p1.processed_like(control):
            f.must(
                "origin.not_validated",
                f"Identical reply ({p1.describe()}) with and without Origin {wire.EVIL_ORIGIN}: "
                "the Origin header is not validated before the request is processed",
                subject=subject,
                spec_url=spec_urls.SPEC_ORIGIN_FRAGMENT,
                evidence=p1.evidence(
                    got=f"{p1.describe()}, same as the control without Origin",
                    expected=EXPECTED_403,
                ),
            )
            f.summary(
                f"Origin not validated: identical reply with and without Origin {wire.EVIL_ORIGIN}"
            )
            return "Origin not validated"
        if p1.status == 403 and p1.outcome in (ProbeOutcome.HTTP_REJECT, ProbeOutcome.RPC_ERROR):
            if p1.outcome is ProbeOutcome.RPC_ERROR and p1.rpc_has_id:
                f.info(
                    "origin.evil_403_body_has_id",
                    "The 403 body is a JSON-RPC error response carrying an id; the spec allows "
                    "a JSON-RPC error body on a 403 only without an id",
                    subject=subject,
                    evidence=p1.evidence(
                        got=f"403 body: JSON-RPC error {p1.rpc_code} with id",
                        expected="JSON-RPC error response that has no id (or no body)",
                    ),
                )
            return "Evil Origin rejected (403)"
        if p1.outcome in (ProbeOutcome.HTTP_REJECT, ProbeOutcome.RPC_ERROR):
            how = (
                f"a JSON-RPC error {p1.rpc_code} on HTTP {p1.status}"
                if p1.outcome is ProbeOutcome.RPC_ERROR
                else f"HTTP {p1.status}"
            )
            f.must(
                "origin.evil_rejected_wrong_status",
                f"The foreign Origin {wire.EVIL_ORIGIN} was refused with {how}; the security "
                "property held (the request was not processed) but an invalid Origin MUST be "
                "answered with HTTP 403 Forbidden",
                subject=subject,
                spec_url=spec_urls.SPEC_ORIGIN_FRAGMENT,
                evidence=p1.evidence(got=p1.describe(), expected=EXPECTED_403),
            )
            if p1.outcome is ProbeOutcome.RPC_ERROR and 200 <= p1.status <= 299:
                f.summary(
                    f"Evil Origin rejected with a JSON-RPC error on HTTP {p1.status} "
                    "(spec requires 403)"
                )
            else:
                f.summary(f"Evil Origin rejected with HTTP {p1.status} (spec requires 403)")
            return f"Evil Origin rejected with HTTP {p1.status}"
        if p1.outcome is ProbeOutcome.NO_RESULT_2XX:
            f.should(
                "origin.evil_rejection_unconfirmed",
                f"The evil-Origin request got {p1.describe()}: not a result, but not the "
                "required 403 either, so the rejection cannot be confirmed",
                subject=subject,
                spec_url=spec_urls.SPEC_ORIGIN_FRAGMENT,
                evidence=p1.evidence(got=p1.describe(), expected=EXPECTED_403),
            )
            return f"Evil Origin got HTTP {p1.status} without a result (rejection unconfirmed)"
        if p1.outcome is ProbeOutcome.REDIRECT:
            f.should(
                "origin.evil_redirected",
                f"The evil-Origin request was redirected ({p1.describe()}); mcpcheck never "
                "follows redirects on probes, so the rejection cannot be confirmed",
                subject=subject,
                spec_url=spec_urls.SPEC_ORIGIN_FRAGMENT,
                evidence=p1.evidence(got=p1.describe(), expected=EXPECTED_403),
            )
            return f"Evil Origin request redirected ({p1.status}, rejection unconfirmed)"
        if p1.timed_out:
            f.should(
                "origin.evil_timeout",
                f"The evil-Origin request produced no response within the budget "
                f"({p1.error}); dropping the request is not the required 403",
                subject=subject,
                spec_url=spec_urls.SPEC_ORIGIN_FRAGMENT,
                evidence=p1.evidence(got=p1.describe(), expected=EXPECTED_403),
            )
            return "Evil Origin request timed out (no 403)"
        f.should(
            "origin.evil_connection_closed",
            f"The connection carrying the evil Origin was closed without a complete response "
            f"({p1.error}); closing the connection is not the required 403",
            subject=subject,
            spec_url=spec_urls.SPEC_ORIGIN_FRAGMENT,
            evidence=p1.evidence(got=p1.describe(), expected=EXPECTED_403),
        )
        return "Evil Origin connection closed (no 403)"

    def _judge_controls(
        self, f: Findings, p2: Probe, p3: Probe, control: Probe, same_origin: str
    ) -> list[str]:
        parts: list[str] = []
        if p2.rejected and control is p3:
            f.best_practice(
                "origin.same_origin_rejected",
                f"Origin {same_origin} (the server's own origin) was refused with "
                f"{p2.describe()} while the request without Origin was processed; if the "
                "server is meant for a web client pass its origin with --origin <allowed>",
                subject=p2.id,
                evidence=p2.evidence(
                    got=p2.describe(),
                    expected="processed like the control (spec is silent; hint: --origin)",
                ),
            )
            parts.append(f"same-origin {same_origin} also rejected ({_short(p2)})")
        if p3.rejected and control is p2:
            f.best_practice(
                "origin.no_origin_rejected",
                f"Requests without an Origin header were refused with {p3.describe()}; "
                "non-browser clients (including the official SDKs) send no Origin, so this is "
                "hardening beyond the spec that locks them out",
                subject=p3.id,
                evidence=p3.evidence(
                    got=p3.describe(),
                    expected="processed like the same-origin request (spec is silent)",
                ),
            )
            parts.append(f"requests without Origin rejected ({_short(p3)})")
        return parts

    def _judge_null(
        self, f: Findings, p4: Probe, control: Probe, baseline: Baseline, parts: list[str]
    ) -> None:
        accepted = p4.outcome is ProbeOutcome.RESULT or (
            baseline is Baseline.RPC_LAYER and p4.processed_like(control)
        )
        if not accepted:
            return
        f.best_practice(
            "origin.null_accepted",
            '"Origin: null" (the opaque origin a browser sends from sandboxed iframes, '
            "data: URLs and some redirects) was processed like a trusted origin; treat it as "
            "foreign",
            subject=p4.id,
            evidence=p4.evidence(
                got=p4.describe(), expected="HTTP 403 Forbidden (treat null as foreign)"
            ),
        )
        parts.append('"Origin: null" accepted')

    def _judge_preflight(self, f: Findings, p5: Probe) -> None:
        allow_origin = p5.allow_origin
        if allow_origin not in (wire.EVIL_ORIGIN, "*"):
            return
        credentials = (p5.allow_credentials or "").lower() == "true"
        f.info(
            "origin.cors_preflight_permissive",
            f"The CORS preflight answered Access-Control-Allow-Origin: {allow_origin}"
            f"{' with credentials' if credentials else ''}; a browser on "
            f"{wire.EVIL_ORIGIN} would be allowed to read responses if the POST itself were "
            "accepted",
            subject=p5.id,
            evidence=p5.evidence(
                method="OPTIONS",
                got=f"HTTP {p5.status}, access-control-allow-origin: {allow_origin}",
                expected="no CORS grant for foreign origins",
            ),
        )


# --------------------------------------------------------------------------- helpers


def _short(p: Probe) -> str:
    if p.outcome is ProbeOutcome.RPC_ERROR:
        return f"HTTP {p.status}, JSON-RPC {p.rpc_code}"
    if p.outcome in (ProbeOutcome.HTTP_REJECT, ProbeOutcome.NO_RESULT_2XX):
        return str(p.status)
    return p.describe()


def _probe_table(p1: Probe, p2: Probe, p3: Probe, p4: Probe, p5: Probe) -> str:
    acao = p5.allow_origin
    acac = p5.allow_credentials
    cors = f"Access-Control-Allow-Origin: {acao}" if acao else "no Access-Control-Allow-Origin"
    if acac:
        cors += f", Access-Control-Allow-Credentials: {acac}"
    rows = [
        f"Origin {wire.EVIL_ORIGIN} → {p1.describe()}",
        f"Origin {p2.origin} → {p2.describe()}",
        f"no Origin → {p3.describe()}",
        f"Origin null → {p4.describe()}",
        f"OPTIONS preflight with Origin {wire.EVIL_ORIGIN} → {p5.describe()}, {cors}",
    ]
    return f"Probes ({PROBE_METHOD}, one fresh connection each): " + "; ".join(rows) + "."


__all__ = [
    "Baseline",
    "OriginCheck",
    "Probe",
    "ProbeOutcome",
    "choose_baseline",
    "classify",
]
