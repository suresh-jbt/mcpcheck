"""The ``handshake`` check (PLAN.md §7.1): the era gate.

One complete 2026-07-28 ``tools/list`` on a **fresh** connection, without any prior
``initialize``, establishes whether the server serves modern per-request-metadata
clients statelessly (HS-2). The reply is classified with :func:`wire.classify_reply`
(§6.6, gate column) and then judged for shape (HS-3..HS-6, HS-13, HS-18). On
``-32601`` the check falls back to ``server/discover``. Only with
``--legacy-probes`` does it send one header-less legacy ``initialize`` on a second
fresh connection to tell ``dual-era`` from ``modern-only``; it never sends ``DELETE``.

The check writes ``ctx.era``, ``ctx.protocol_version``, ``ctx.server_info`` and
``ctx.server_supported_versions`` and seeds ``tools/list`` page 0 for every other
check; the runner's gate keys off the codes ``handshake.initialize_required``,
``handshake.version_unsupported`` and ``handshake.client_capability_demanded``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

import anyio
from pydantic import ValidationError

from mcpcheck import spec_urls, wire
from mcpcheck.checks.base import Check, Era
from mcpcheck.wire import Outcome, OutcomeKind, classify_reply

if TYPE_CHECKING:
    from mcpcheck.checks.base import CheckContext, Findings
    from mcpcheck.client import RawClient, RawReply

RETRY_DELAY_S = 1.0
"""Pause before the single retry of the primary request after an HTTP 5xx (§4, §9)."""
LEGACY_PROBE_VERSION = "2025-11-25"
"""``protocolVersion`` offered in the opt-in legacy ``initialize`` probe."""
LEGACY_PROBE_ID = 2
"""JSON-RPC id of the legacy ``initialize`` probe (the primary request is id 1)."""
RPC_MESSAGE_MAX = 300
EXPECTED_PRIMARY = (
    'HTTP 200 with a result (resultType "complete"), '
    "or a recognized modern error (-32020/-32021/-32022)"
)
HINT_BACKCOMPAT = "see basic/transports/streamable-http#backward-compatibility"
PASS_SUMMARY = "Server does not require `initialize`"  # noqa: S105 — a summary string, not a secret
HINT_INTERMEDIARY = (
    "mcpcheck sent matching MCP-Protocol-Version/Mcp-Method headers and _meta; "
    "an intermediary may be stripping or rewriting headers"
)
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_SUPPORTED_RE = re.compile(r"supported\s+versions?\s*:?", re.IGNORECASE)
# 4xx statuses without a JSON-RPC body that are infrastructure, never an era signal
_INFRA_4XX = frozenset({401, 402, 403, 407, 408, 413, 414, 415, 429, 431, 451})


@dataclass
class _Verdict:
    """What the primary exchange (and its discover fallback) established."""

    modern: bool | None = None
    """``True``: modern server confirmed; ``False``: legacy by the era rule; ``None``: unknown."""
    passed: bool = False
    summary: str | None = None
    fallback: bool = False
    """The ``tools/list`` reply was ``-32601``: try ``server/discover``."""
    via_discover: bool = False


def _id_matches(got: Any, want: int | str | None) -> bool:
    return bool(got == want and type(got) is type(want))


def _evidence(reply: RawReply, outcome: Outcome | None = None, **extra: Any) -> dict[str, Any]:
    """The §7.1(6) evidence object; ``None`` values are dropped."""
    ev: dict[str, Any] = {
        "http_status": reply.status,
        "media_type": reply.media_type or None,
        "rpc_code": outcome.rpc_code if outcome else None,
        "rpc_message": (outcome.rpc_message[:RPC_MESSAGE_MAX] or None) if outcome else None,
        "connection": "handshake",
    }
    if reply.session_ids:
        ev["session_id_seen"] = True
    ev.update(extra)
    return {k: v for k, v in ev.items() if v is not None}


def _fingerprint(reply: RawReply, outcome: Outcome) -> str:
    """Label a non-modern rejection (labels only; the era rule decides)."""
    if outcome.rpc_code is None:
        return "empty_400" if not reply.raw.strip() else "non_modern_error"
    msg = outcome.rpc_message.lower()
    resp = reply.any_response or {}
    # What the message *says* beats the id sentinel. Python mcp 1.x answers a version
    # rejection with id "server-error", so testing the sentinel first labelled the whole
    # 1.x ecosystem "legacy_session" and threw away the version list it had just named.
    if "unsupported protocol version" in msg:
        return "legacy_version_reject"
    if "not initialized" in msg or "before initialization" in msg:
        return "not_initialized"
    if (
        resp.get("id") == "server-error"
        or "missing session id" in msg
        or "session not found" in msg
        or "invalid session" in msg
    ):
        return "legacy_session"
    return "non_modern_error"


def _describe_value(value: Any) -> str:
    """``absent`` / ``null`` / ``a string`` / ``an empty array`` … for finding messages."""
    if value is None:
        return "absent or null"
    if isinstance(value, list):
        return "an empty array" if not value else "an array with non-string entries"
    return f"a {type(value).__name__}"


def _error_data(reply: RawReply) -> Any:
    """``error.data`` of the first JSON-RPC error message in the reply."""
    for msg in reply.messages:
        rpc = wire.rpc_error_of(msg)
        if rpc is not None:
            return rpc[2]
    return None


def _versions_in(text: str) -> list[str]:
    """Every distinct ``YYYY-MM-DD`` token in ``text``, in order of appearance."""
    seen: list[str] = []
    for m in _DATE_RE.findall(text):
        if m not in seen:
            seen.append(m)
    return seen


def _supported_versions_in(text: str) -> list[str]:
    """The versions a rejection message says the server *does* support.

    "Unsupported protocol version: 2026-07-28 (supported versions: 2025-11-25, ...)"
    names the rejected version first, so scanning the whole message reported the one
    version the server had just refused as supported. Prefer the list after "supported
    versions", and never include the version mcpcheck proposed.
    """
    match = _SUPPORTED_RE.search(text)
    scope = text[match.end() :] if match else text
    return [v for v in _versions_in(scope) if v != wire.PROTOCOL_VERSION]


def _error_text(reply: RawReply) -> str:
    """Message + data of the JSON-RPC error (or the raw body) for version-name scanning."""
    rpc = reply.rpc_error
    if rpc is not None:
        _code, message, data = rpc
        return f"{message} {json.dumps(data) if data is not None else ''}"
    text, _ = wire.decode_utf8(reply.raw[:4096])
    return text or ""


class HandshakeCheck(Check):
    """Server does not require initialize."""

    name: ClassVar[str] = "handshake"
    title: ClassVar[str] = "Server does not require initialize"
    spec_url: ClassVar[str] = spec_urls.SPEC_STATELESSNESS
    order: ClassVar[int] = 10

    async def run(self, ctx: CheckContext, f: Findings) -> None:
        """Send the primary request on a fresh connection, judge it, optionally probe legacy."""
        ctx.era = Era.UNKNOWN
        async with ctx.fresh("handshake") as raw:
            reply, outcome = await self._primary(ctx, raw, f)
            ctx.seed_tools_page0(reply)
            verdict = self._judge(ctx, f, reply, outcome, "tools/list")
            if verdict.fallback:
                verdict = await self._discover_fallback(ctx, raw, f, reply)
        legacy = None
        if ctx.cfg.legacy_probes:
            legacy = await self._legacy_probe(ctx, f, verdict)
        else:
            f.details("legacy initialize: not probed (--legacy-probes)")
        ctx.era = _era(verdict, legacy)
        if verdict.summary is not None:
            summary = verdict.summary
            if verdict.passed and legacy is True:
                summary = (
                    PASS_SUMMARY + " (no tools; via server/discover; also serves legacy clients)"
                    if verdict.via_discover
                    else PASS_SUMMARY + " (also serves legacy clients)"
                )
            f.summary(summary)

    # ------------------------------------------------------------------ requests
    async def _primary(
        self, ctx: CheckContext, raw: RawClient, f: Findings
    ) -> tuple[RawReply, Outcome]:
        reply = await raw.post("tools/list", wait_for_eof=True, purpose="primary request")
        outcome = self._classify(ctx, reply)
        if outcome.kind is OutcomeKind.SERVER_ERROR:
            first_status = reply.status
            await anyio.sleep(RETRY_DELAY_S)
            reply = await raw.post(
                "tools/list", wait_for_eof=True, purpose="primary request (retry after HTTP 5xx)"
            )
            outcome = self._classify(ctx, reply)
            if outcome.kind is OutcomeKind.SERVER_ERROR:
                f.details(
                    f"tools/list answered HTTP {first_status} and then HTTP {reply.status} "
                    f"after a {RETRY_DELAY_S:g} s pause; infrastructure is never a conformance failure"
                )
        return reply, outcome

    async def _discover_fallback(
        self, ctx: CheckContext, raw: RawClient, f: Findings, tools_reply: RawReply
    ) -> _Verdict:
        f.details("tools/list is not implemented (404 -32601); falling back to server/discover")
        reply = await raw.post(
            "server/discover", wait_for_eof=True, purpose="fallback after tools/list -32601"
        )
        outcome = self._classify(ctx, reply)
        if outcome.kind is OutcomeKind.METHOD_NOT_FOUND:
            f.must(
                "handshake.discover_missing",
                "Neither tools/list nor server/discover is implemented (404 -32601 for both); "
                "server/discover MUST be implemented by every 2026-07-28 server",
                subject="server/discover",
                evidence=_evidence(
                    reply, outcome, expected="HTTP 200 with a DiscoverResult", got="404 -32601"
                ),
            )
            return _Verdict(
                modern=None,
                summary="Neither `tools/list` nor `server/discover` is implemented (404 -32601)",
            )
        verdict = self._judge(ctx, f, reply, outcome, "server/discover")
        if verdict.passed:
            caps = (reply.result or {}).get("capabilities")
            if isinstance(caps, dict) and caps.get("tools") is not None:
                f.must(
                    "handshake.tools_declared_but_unimplemented",
                    "server/discover declares the tools capability but tools/list answered "
                    "404 -32601; a server that declares tools MUST respond to tools/list "
                    "(the set MAY be empty)",
                    subject="tools/list",
                    evidence=_evidence(
                        tools_reply,
                        self._classify(ctx, tools_reply),
                        expected="HTTP 200 with a ListToolsResult (tools MAY be [])",
                        got="404 -32601",
                        declared_capabilities=sorted(caps),
                    ),
                )
                verdict.passed = False
            else:
                verdict.via_discover = True
                verdict.summary = PASS_SUMMARY + " (no tools; verified via server/discover)"
        return verdict

    async def _legacy_probe(self, ctx: CheckContext, f: Findings, verdict: _Verdict) -> bool | None:
        """Send one header-less legacy ``initialize``; ``True`` answered, ``False`` rejected."""
        params = {
            "protocolVersion": LEGACY_PROBE_VERSION,
            "capabilities": {},
            "clientInfo": wire.client_info(),
        }
        async with ctx.fresh("handshake-legacy") as raw:
            reply = await raw.post(
                "initialize",
                params,
                meta_version=None,
                omit_headers=(wire.H_METHOD,),
                req_id=LEGACY_PROBE_ID,
                wait_for_eof=True,
                purpose="legacy initialize probe (--legacy-probes)",
            )
        outcome = self._classify(ctx, reply)
        result = reply.result
        legacy_version = result.get("protocolVersion") if result is not None else None
        if outcome.ok and isinstance(legacy_version, str):
            f.details(
                f"legacy initialize: answered with protocolVersion {legacy_version} "
                f"(HTTP {reply.status} {reply.media_type})"
            )
            if verdict.modern:
                f.info(
                    "handshake.dual_era",
                    f"Server also answers the legacy initialize handshake "
                    f"(protocolVersion {legacy_version}); it serves both eras on one endpoint",
                    subject="initialize",
                    evidence=_evidence(reply, outcome, legacy_protocol_version=legacy_version),
                )
            if reply.session_ids:
                f.info(
                    "handshake.session_id_seen",
                    "The legacy initialize minted an Mcp-Session-Id (legacy sessions are "
                    "allowed; mcpcheck never sends DELETE, so the session idles out)",
                    subject="initialize",
                    evidence=_evidence(reply, outcome, session_id=reply.session_ids[0]),
                )
            return True
        if (
            400 <= reply.status <= 499
            and reply.status not in _INFRA_4XX
            and reply.error is None
            and reply.redirect_blocked is None
        ):
            desc = (
                f"{outcome.rpc_code} {outcome.rpc_message[:80]!r}"
                if outcome.rpc_code is not None
                else (reply.media_type or "no") + " body"
            )
            f.details(f"legacy initialize: rejected (HTTP {reply.status}, {desc})")
            named = _versions_in(_error_text(reply))
            if not named:
                f.should(
                    "handshake.initialize_error_names_no_version",
                    "The error returned to a legacy initialize names no supported protocol "
                    "version; a modern-only server SHOULD name the versions it supports "
                    "there because legacy clients have no fall-forward mechanism",
                    subject="initialize",
                    evidence=_evidence(
                        reply,
                        outcome,
                        expected="an error message naming the supported versions (YYYY-MM-DD)",
                        got="no version named",
                    ),
                )
            return False
        f.details(f"legacy initialize: inconclusive ({outcome.detail})")
        return None

    # ------------------------------------------------------------------ judgement
    def _classify(self, ctx: CheckContext, reply: RawReply) -> Outcome:
        return classify_reply(
            reply, sent_origin=ctx.cfg.origin is not None, sent_token=bool(ctx.cfg.token)
        )

    def _judge(
        self, ctx: CheckContext, f: Findings, reply: RawReply, outcome: Outcome, subject: str
    ) -> _Verdict:
        """Apply the §7.1(4) verdict table to one reply."""
        kind = outcome.kind
        if kind is OutcomeKind.OK:
            return self._judge_result(ctx, f, reply, outcome, subject)
        if kind in (OutcomeKind.MODERN_REJECTION, OutcomeKind.RPC_ERROR):
            return self._judge_modern_error(ctx, f, reply, outcome, subject)
        if kind is OutcomeKind.METHOD_NOT_FOUND:
            return _Verdict(fallback=True)
        if kind is OutcomeKind.NON_MODERN_REJECTION:
            return self._judge_non_modern(ctx, f, reply, outcome, subject)
        if kind is OutcomeKind.NOT_JSONRPC_2XX:
            if outcome.extra.get("id_mismatch"):
                return self._judge_result(ctx, f, reply, outcome, subject)
            f.must(
                "handshake.not_jsonrpc",
                f"HTTP {reply.status} {reply.media_type or '(no content-type)'} to {subject} "
                f"is not a JSON-RPC response ({outcome.detail}); the server MUST answer "
                "application/json or text/event-stream carrying the JSON-RPC response",
                subject=subject,
                evidence=_evidence(
                    reply,
                    outcome,
                    expected=EXPECTED_PRIMARY,
                    got=outcome.detail,
                    parse_error=reply.parse_error,
                ),
            )
            return _Verdict(
                summary=f"Non-MCP response to `{subject}` "
                f"(HTTP {reply.status} {reply.media_type or 'no content-type'})"
            )
        # auth_required / forbidden / not_mcp_endpoint / redirected / server_error / transport
        f.inconclusive(outcome.detail)
        return _Verdict()

    def _judge_result(
        self, ctx: CheckContext, f: Findings, reply: RawReply, outcome: Outcome, subject: str
    ) -> _Verdict:
        response = reply.response or reply.any_response or {}
        ev_base = _evidence(reply, outcome)
        got_id = response.get("id")
        if not _id_matches(got_id, reply.request_id):
            f.must(
                "handshake.id_mismatch",
                f"Response id {got_id!r} differs from the request id {reply.request_id!r}; "
                "result responses MUST include the same id as the request",
                subject=subject,
                evidence={**ev_base, "got": got_id, "expected": reply.request_id},
            )
        result = response.get("result")
        if not isinstance(result, dict):
            f.must(
                "handshake.not_jsonrpc",
                f"The {subject} response carries no result object "
                f"(got {type(result).__name__}); a result response MUST include a result field",
                subject=subject,
                evidence={**ev_base, "got": type(result).__name__, "expected": "object"},
            )
            return _Verdict(summary=f"Non-MCP response to `{subject}` (result is not an object)")
        verdict = _Verdict(modern=True, passed=True, summary=PASS_SUMMARY)
        if "resultType" not in result:
            f.must(
                "handshake.legacy_shaped_result",
                f"The {subject} result has no resultType field; every 2026-07-28 result MUST "
                "include resultType (a pre-2026 result shape answered a 2026-07-28 request)",
                subject=subject,
                evidence={
                    **ev_base,
                    "got": sorted(result),
                    "expected": 'resultType "complete"',
                    "hint": HINT_BACKCOMPAT,
                },
            )
            verdict = _Verdict(
                modern=None,
                summary="Server answered a 2026-07-28 request with a pre-2026 result (no resultType)",
            )
        elif result.get("resultType") != "complete":
            f.must(
                "handshake.unexpected_result_type",
                f'resultType {result.get("resultType")!r} on {subject}; only "complete" is '
                f"valid here and any unrecognized resultType MUST be treated as invalid",
                subject=subject,
                evidence={**ev_base, "got": result.get("resultType"), "expected": "complete"},
            )
            verdict.passed = False
            verdict.summary = f"Unexpected resultType {result.get('resultType')!r} on `{subject}`"
        if verdict.modern:
            ctx.protocol_version = wire.PROTOCOL_VERSION
        if subject == "tools/list" and not isinstance(result.get("tools"), list):
            f.must(
                "handshake.tools_not_list",
                f"result.tools is {type(result.get('tools')).__name__}, not an array",
                subject=subject,
                evidence={
                    **ev_base,
                    "got": type(result.get("tools")).__name__,
                    "expected": "array of Tool",
                },
            )
            verdict.passed = False
            verdict.summary = verdict.summary or "result.tools is not an array"
        if subject == "server/discover":
            versions = result.get("supportedVersions")
            if isinstance(versions, list) and all(isinstance(v, str) for v in versions):
                ctx.server_supported_versions = list(versions)
                if wire.PROTOCOL_VERSION not in versions:
                    f.details(
                        f"server/discover supportedVersions {versions} omit 2026-07-28 although "
                        "the server answered a 2026-07-28 request (judged by the discover check)"
                    )
        self._judge_meta(ctx, f, result, subject, ev_base)
        self._judge_stream(f, reply, subject, ev_base)
        if reply.session_ids:
            f.details(
                f"Mcp-Session-Id {reply.session_ids[0]!r} was minted on the modern reply "
                "(judged by the session check)"
            )
        if f.failures:
            verdict.passed = False
            if verdict.summary == PASS_SUMMARY:
                verdict.summary = None  # the first failure message becomes the summary
        return verdict

    def _judge_meta(
        self,
        ctx: CheckContext,
        f: Findings,
        result: dict[str, Any],
        subject: str,
        ev_base: dict[str, Any],
    ) -> None:
        meta = result.get("_meta")
        info = meta.get(wire.META_SERVER_INFO) if isinstance(meta, dict) else None
        if (
            isinstance(info, dict)
            and isinstance(info.get("name"), str)
            and isinstance(info.get("version"), str)
        ):
            ctx.server_info = info
            return
        if isinstance(info, dict):
            ctx.server_info = info
            got = f"{wire.META_SERVER_INFO} without string name/version"
        elif info is None:
            got = "absent"
        else:
            got = f"{wire.META_SERVER_INFO} is {type(info).__name__}, not an object"
        f.should(
            "handshake.server_info_missing",
            f"{subject} result _meta lacks a well-formed {wire.META_SERVER_INFO} "
            "(SHOULD be present on every result with string name and version)",
            subject=subject,
            evidence={**ev_base, "got": got, "expected": '{"name": str, "version": str}'},
        )

    def _judge_stream(
        self, f: Findings, reply: RawReply, subject: str, ev_base: dict[str, Any]
    ) -> None:
        if not reply.is_sse:
            return
        for req in reply.server_requests:
            f.must(
                "handshake.server_request_on_stream",
                f"The SSE stream answering {subject} carried a JSON-RPC request "
                f"{req.get('method')!r} (id {req.get('id')!r}); the server MUST NOT send "
                "requests on the response stream",
                subject=subject,
                evidence={**ev_base, "got": req.get("method"), "expected": "notifications only"},
            )
        if reply.stream_terminated is False:
            f.should(
                "handshake.stream_not_terminated",
                f"The SSE stream stayed open {wire.SSE_TERMINATION_GRACE_S:g} s after the "
                f"response to {subject}; the server SHOULD close the stream after the final response",
                subject=subject,
                evidence={**ev_base, "got": "stream open", "expected": "stream closed"},
            )

    def _judge_modern_error(
        self, ctx: CheckContext, f: Findings, reply: RawReply, outcome: Outcome, subject: str
    ) -> _Verdict:
        code = outcome.rpc_code
        data = _error_data(reply)
        status_note = "" if reply.status == 400 else f" (HTTP {reply.status} instead of 400)"
        ev = _evidence(reply, outcome, expected=EXPECTED_PRIMARY, got=f"{reply.status} {code}")
        if reply.status != 400:
            ev["expected_http_status"] = 400
        if code == wire.HEADER_MISMATCH:
            f.must(
                "handshake.header_mismatch_on_valid_request",
                f"Server rejected a well-formed request with -32020 header mismatch{status_note}: "
                f"{outcome.rpc_message[:RPC_MESSAGE_MAX]!r}",
                subject=subject,
                evidence={**ev, "hint": HINT_INTERMEDIARY},
            )
            return _Verdict(
                modern=True,
                summary="Server rejected a well-formed request with -32020 (intermediary stripping headers?)",
            )
        if code == wire.MISSING_REQUIRED_CLIENT_CAPABILITY:
            required = data.get("requiredCapabilities") if isinstance(data, dict) else None
            names = (
                ", ".join(str(r) for r in required)
                if isinstance(required, list) and required
                else "unspecified"
            )
            f.should(
                "handshake.client_capability_demanded",
                f"Server demands client capabilities ({names}) that mcpcheck does not offer"
                f"{status_note}; a read-only conformance client cannot provide them",
                subject=subject,
                evidence={**ev, "required_capabilities": required},
            )
            # B:389-394: two MUSTs about the error itself are observable even though the
            # verdict stays inconclusive (the gate still lets dependents run)
            if not (
                isinstance(required, list)
                and required
                and all(isinstance(r, str) for r in required)
            ):
                f.must(
                    "handshake.capability_error_malformed",
                    f"-32021 data.requiredCapabilities is {_describe_value(required)}; it MUST "
                    "list the missing capabilities as a non-empty array of strings",
                    subject=subject,
                    evidence={**ev, "got": required, "expected": '["<capability>", ...]'},
                )
            if reply.status != 400:
                f.must(
                    "handshake.wrong_status_for_32021",
                    f"-32021 returned with HTTP {reply.status}; on HTTP the status MUST be 400",
                    subject=subject,
                    evidence={**ev, "expected": "HTTP 400"},
                )
            f.inconclusive(f"Not evaluated: server demands a client capability (-32021: {names})")
            ctx.protocol_version = wire.PROTOCOL_VERSION
            return _Verdict(modern=True)
        # -32022
        supported: list[str] | None = None
        malformed: str | None = None
        try:
            from mcp_types import UnsupportedProtocolVersionErrorData

            parsed = UnsupportedProtocolVersionErrorData.model_validate(data)
        except ValidationError as exc:
            malformed = f"{exc.error_count()} validation error(s)"
        else:
            supported = list(parsed.supported)
            ctx.server_supported_versions = supported
        supports = ", ".join(supported) if supported else "not stated"
        f.must(
            "handshake.version_unsupported",
            f"Server does not support protocol version 2026-07-28{status_note}; "
            f"-32022 data.supported: {supports}",
            subject=subject,
            evidence={**ev, "supported": supported},
        )
        if supported is not None and wire.PROTOCOL_VERSION in supported:
            f.must(
                "handshake.contradictory_unsupported",
                "Server answered -32022 UnsupportedProtocolVersion although data.supported "
                "lists 2026-07-28, the version requested",
                subject=subject,
                evidence={**ev, "supported": supported},
            )
        if malformed is not None:
            f.must(
                "handshake.unsupported_data_malformed",
                f"-32022 error data is not an UnsupportedProtocolVersionErrorData object "
                f"({malformed}); got {json.dumps(data)[:120]}",
                subject=subject,
                evidence={
                    **ev,
                    "got": data,
                    "expected": '{"supported": [str, ...], "requested": str}',
                },
            )
        return _Verdict(
            modern=True,
            summary=f"Modern server does not support 2026-07-28 (supports: {supports})",
        )

    def _judge_non_modern(
        self, ctx: CheckContext, f: Findings, reply: RawReply, outcome: Outcome, subject: str
    ) -> _Verdict:
        response = reply.any_response
        if 200 <= reply.status <= 299 and response is not None and "result" in response:
            f.must(
                "handshake.not_jsonrpc",
                f"The {subject} response carries both result and error "
                f"(error {outcome.rpc_code} {outcome.rpc_message[:80]!r}); a JSON-RPC response "
                "MUST contain exactly one of them",
                subject=subject,
                evidence=_evidence(
                    reply, outcome, got="result and error", expected="result xor error"
                ),
            )
            return _Verdict(summary=f"Non-MCP response to `{subject}` (both result and error)")
        if outcome.rpc_code is None and reply.status != 400:
            # 4xx without a JSON-RPC body other than 400 (e.g. 429): infrastructure
            f.inconclusive(outcome.detail)
            return _Verdict()
        fingerprint = _fingerprint(reply, outcome)
        if fingerprint == "legacy_version_reject":
            named = _supported_versions_in(outcome.rpc_message)
            if named:
                ctx.server_supported_versions = named
        if outcome.rpc_code is not None:
            what = f'{outcome.rpc_code} "{outcome.rpc_message[:RPC_MESSAGE_MAX]}"'
        elif not reply.raw.strip():
            what = "empty body"
        else:
            what = f"{reply.media_type or 'no content-type'} body"
        ev = _evidence(
            reply,
            outcome,
            fingerprint=fingerprint,
            expected=EXPECTED_PRIMARY,
            got=f"HTTP {reply.status}, {what}",
            hint=HINT_BACKCOMPAT,
        )
        if fingerprint == "legacy_version_reject" and ctx.server_supported_versions:
            ev["supported_versions_source"] = "message"
            ev["supported"] = ctx.server_supported_versions
        if fingerprint == "legacy_version_reject":
            # Saying "requires initialize" here was simply false: these servers serve
            # tools/list happily without one, they just do not speak 2026-07-28. Name the
            # versions they do speak -- that is the one fact the operator needs.
            speaks: list[str] = list(ctx.server_supported_versions or ())
            suffix = f" (speaks {', '.join(speaks)})" if speaks else ""
            f.must(
                "handshake.version_unsupported_legacy",
                "Server does not support 2026-07-28: it rejected a complete request for this "
                "revision" + (f" and names {', '.join(speaks)} instead" if speaks else ""),
                subject=subject,
                spec_url=spec_urls.SPEC_BACKCOMPAT_HTTP,
                evidence=ev,
            )
            return _Verdict(modern=False, summary=f"Server does not speak 2026-07-28{suffix}")
        f.must(
            "handshake.initialize_required",
            "Server rejected a complete 2026-07-28 request with a non-modern error; "
            "it requires the legacy initialize handshake",
            subject=subject,
            evidence=ev,
        )
        return _Verdict(
            modern=False, summary=f"Server requires `initialize` (HTTP {reply.status}, {what})"
        )


def _era(verdict: _Verdict, legacy: bool | None) -> Era:
    """Combine the modern verdict with the optional legacy probe (§7.1 step 5)."""
    if verdict.modern is True:
        if legacy is True:
            return Era.DUAL_ERA
        if legacy is False:
            return Era.MODERN_ONLY
        return Era.UNKNOWN
    if verdict.modern is False:
        return Era.UNKNOWN if legacy is False else Era.LEGACY_ONLY
    return Era.UNKNOWN


__all__ = ["HandshakeCheck"]
