"""The ``errors`` check (PLAN.md §7.5): invalid tool calls are rejected with useful errors.

One ``tools/call`` per probe-eligible tool with arguments **proven** invalid
against the tool's own ``inputSchema`` (see :mod:`mcpcheck.probe_policy`),
plus one call to a non-existent tool. The reply must be one of the two
conformant error shapes — a JSON-RPC error or a ``CallToolResult`` with
``isError: true`` — and its text should be something a model could act on.
No tool is ever called with valid input; unannotated tools are never called
under the default ``--probe safe``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, ClassVar

import anyio
import pydantic

from mcpcheck import spec_urls, wire
from mcpcheck.checks.base import Check
from mcpcheck.probe_policy import (
    ENUM_SENTINEL,
    STRATEGY_MISSING_REQUIRED,
    ToolPlan,
    param_headers,
    plan_tools,
)
from mcpcheck.wire import OutcomeKind, classify_reply

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from mcpcheck.checks.base import CheckContext, Findings
    from mcpcheck.client import RawReply

logger = logging.getLogger("mcpcheck.errors")

UNKNOWN_SUBJECT = "(unknown tool)"
UNKNOWN_STRATEGY = "unknown-tool"
ACTIONABLE_RATIO = 0.8
GOT_LIMIT = 2000
MESSAGE_LIMIT = 160
EXPECTED_ERROR_SHAPE = (
    "JSON-RPC error (e.g. -32602) or CallToolResult with isError:true — "
    "servers MUST validate all tool inputs"
)

STRATEGY_TEXT: dict[str, str] = {
    STRATEGY_MISSING_REQUIRED: "all required properties omitted",
    "wrong-type-required": "every required property wrong-typed",
    "wrong-type-optional": "optional properties wrong-typed",
    "unexpected-property": "an unexpected property although additionalProperties is false",
}

# ---------------------------------------------------------------- step 5 heuristics (A1-A5)

GENERIC_MESSAGES: frozenset[str] = frozenset(
    {
        "error",
        "failed",
        "failure",
        "internal",
        "internal error",
        "unknown",
        "unknown error",
        "bad request",
        "invalid",
        "invalid params",
        "invalid request",
        "invalid arguments",
        "something went wrong",
        "an error occurred",
        "exception",
        "server error",
        "tool error",
        "validation error",
        "validation failed",
    }
)
HTTP_STATUS_RE = re.compile(r"^(http\s*)?[1-5]\d{2}(\s+[a-z ]+)?$")
STACK_TRACE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"traceback \(most recent call last\)", re.IGNORECASE),
    re.compile(r'^\s*file ".+", line \d+', re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*at \S+ \(.+:\d+:\d+\)", re.MULTILINE),
    re.compile(r"\bstack ?trace\b", re.IGNORECASE),
    re.compile(r"^[a-z_][\w.]*(exception|error): ", re.IGNORECASE),
)
SCHEMA_KEYWORD_RE = re.compile(
    r"\b(required|expected|must be|should be|missing|invalid value|not a valid|type|integer|"
    r"string|boolean|number|enum|one of|format|minimum|maximum|unknown tool)\b"
)
TWO_SENTENCES_RE = re.compile(r"[.!?]\s+[A-Z]")
A5_MAX_LEN = 200


def _normalise(text: str) -> str:
    return text.lower().replace("_", "").replace("-", "")


def judge_message(
    text: str, *, tool_name: str, properties: Sequence[str], jsonrpc: bool
) -> list[str]:
    """Return the failed heuristics (``A1``…``A5``) for an error message.

    A1 length ≥ 20; A2 not a generic phrase or a bare HTTP status; A3 no stack
    trace; A4 mentions a property, the tool name or a schema keyword; A5 (JSON-RPC
    errors only, E6) a single concise sentence. A message is *actionable* when
    A1-A4 pass; A5 is reported separately.
    """
    failed: list[str] = []
    stripped = text.strip()
    lower = stripped.lower()
    if len(stripped) < 20:
        failed.append("A1")
    generic = lower.rstrip(".!")
    if generic in GENERIC_MESSAGES or HTTP_STATUS_RE.match(lower):
        failed.append("A2")
    if any(rx.search(stripped) for rx in STACK_TRACE_RES):
        failed.append("A3")
    norm = _normalise(stripped)
    mentions = any(_normalise(p) and _normalise(p) in norm for p in properties)
    mentions = mentions or (bool(tool_name) and _normalise(tool_name) in norm)
    if not (mentions or SCHEMA_KEYWORD_RE.search(lower)):
        failed.append("A4")
    if jsonrpc and (
        "\n" in stripped or len(stripped) > A5_MAX_LEN or TWO_SENTENCES_RE.search(stripped)
    ):
        failed.append("A5")
    return failed


def is_actionable(failed: Sequence[str]) -> bool:
    """A1-A4 all pass (A5 is a separate SHOULD)."""
    return not any(h in failed for h in ("A1", "A2", "A3", "A4"))


# ---------------------------------------------------------------- probe record


@dataclass
class _Probe:
    """Everything observed for one ``tools/call`` probe (becomes ``evidence.probe``)."""

    tool: str
    subject: str
    strategy: str
    arguments: dict[str, Any]
    request_id: str
    http_status: int = 0
    media_type: str = ""
    shape: str = ""
    error_code: int | None = None
    heuristics_failed: list[str] = field(default_factory=list)
    inconclusive: str | None = None
    auth: bool = False
    message: str | None = None
    actionable: bool | None = None
    got: Any = None

    def evidence(self, **extra: Any) -> dict[str, Any]:
        """The ``evidence`` object of a finding about this probe."""
        ev: dict[str, Any] = {
            "probe": {
                "strategy": self.strategy,
                "arguments": self.arguments,
                "request_id": self.request_id,
                "http_status": self.http_status,
                "media_type": self.media_type,
                "shape": self.shape,
                "error_code": self.error_code,
                "heuristics_failed": list(self.heuristics_failed),
            }
        }
        if self.got is not None:
            ev["got"] = _clip_got(self.got)
        ev.update(extra)
        return ev


def _clip_got(got: Any) -> Any:
    if isinstance(got, str):
        return got if len(got) <= GOT_LIMIT else got[: GOT_LIMIT - 1] + "…"
    text = json.dumps(got, ensure_ascii=False, separators=(",", ":"))
    return got if len(text) <= GOT_LIMIT else text[: GOT_LIMIT - 1] + "…"


def _clip_message(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MESSAGE_LIMIT else text[: MESSAGE_LIMIT - 1] + "…"


def _retry_after_seconds(value: str | None) -> float | None:
    """Parse ``Retry-After`` (delta-seconds or HTTP-date); ``None`` when absent/unparsable."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _text_of(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    parts = [
        item["text"]
        for item in content
        if isinstance(item, dict)
        and item.get("type") == "text"
        and isinstance(item.get("text"), str)
    ]
    return "\n".join(parts)


def _sdk_result_locs(result: Mapping[str, Any]) -> list[str]:
    """Locations the SDK validator rejects in a ``tools/call`` result (resultType excluded)."""
    from mcp_types.methods import validate_server_result

    result_type = result.get("resultType")
    branch = "InputRequiredResult" if result_type == "input_required" else "CallToolResult"
    try:
        validate_server_result("tools/call", wire.PROTOCOL_VERSION, dict(result))
    except pydantic.ValidationError as exc:
        locs: list[str] = []
        for err in exc.errors():
            loc = [str(p) for p in err.get("loc", ())]
            if loc and loc[0] in ("CallToolResult", "InputRequiredResult"):
                if loc[0] != branch:
                    continue  # the other union member's complaints are noise
                loc = loc[1:]
            if not loc or loc[0] == "resultType":
                continue
            locs.append(".".join(loc) + f" ({err.get('type')})")
        return sorted(set(locs))
    except Exception as exc:  # noqa: BLE001 — a validator crash must not take the check down
        logger.debug("validate_server_result raised %r", exc)
    return []


# ---------------------------------------------------------------- the check


class ErrorsCheck(Check):
    """Invalid tool calls are rejected with useful errors."""

    name: ClassVar[str] = "errors"
    title: ClassVar[str] = "Invalid tool calls are rejected with useful errors"
    spec_url: ClassVar[str] = spec_urls.SPEC_TOOL_ERRORS
    order: ClassVar[int] = 60

    async def run(self, ctx: CheckContext, f: Findings) -> None:
        """Probe eligible tools with schema-invalid arguments and judge the replies (§7.5)."""
        policy = ctx.cfg.probe
        if policy == "none":
            f.inconclusive("Skipped: --probe none")
            f.details("No tools/call was sent (--probe none).")
            return
        tools = await ctx.tools()
        if tools is None:
            pages = await ctx.tools_list_pages()
            reason = "tools/list not ok"
            if pages:
                outcome = classify_reply(
                    pages[0], sent_origin=ctx.cfg.origin is not None, sent_token=bool(ctx.cfg.token)
                )
                reason = outcome.detail
            f.inconclusive(f"Not evaluated: tool listing unavailable ({reason})")
            return
        if not tools:
            f.inconclusive("Server exposes no tools")
            return

        selected, skipped = plan_tools(tools, policy, ctx.cfg.max_tools)
        for plan in skipped:
            f.info(
                "errors.tool_skipped",
                f"Not probed: {plan.skip_reason}",
                subject=plan.name,
                tool=plan.name,
                evidence={
                    "reason": plan.skip_reason,
                    "readOnlyHint": plan.read_only,
                    "destructiveHint": plan.destructive,
                },
            )
        if selected and policy == "all":
            logger.warning(
                "--probe all: calling %d tool(s), including unannotated/destructive ones, "
                "with schema-invalid arguments",
                len(selected),
            )

        probes: list[_Probe] = []
        for i, plan in enumerate(selected):
            probes.append(await self._probe_tool(ctx, f, plan, i))
        unknown = await self._probe_unknown(ctx, f, len(selected))
        probes.append(unknown)

        self._finish(ctx, f, tools, selected, skipped, probes)

    # ------------------------------------------------------------ probing

    async def _post(
        self,
        ctx: CheckContext,
        name: str,
        arguments: Mapping[str, Any],
        rid: str,
        headers: Mapping[str, str],
        purpose: str,
    ) -> RawReply:
        reply = await ctx.raw.post(
            "tools/call",
            {"name": name, "arguments": dict(arguments)},
            req_id=rid,
            headers=dict(headers) or None,
            purpose=purpose,
            check=self.name,
        )
        if reply.status == 429 and reply.error is None:
            delay = _retry_after_seconds(reply.headers.get("retry-after"))
            if delay is not None and delay <= ctx.cfg.timeout:
                await anyio.sleep(delay)
                reply = await ctx.raw.post(
                    "tools/call",
                    {"name": name, "arguments": dict(arguments)},
                    req_id=rid,
                    headers=dict(headers) or None,
                    purpose=f"{purpose} (retry after 429)",
                    check=self.name,
                )
        return reply

    async def _probe_tool(self, ctx: CheckContext, f: Findings, plan: ToolPlan, i: int) -> _Probe:
        best = plan.best
        if best is None:  # pragma: no cover - plan_tools never selects a tool without a candidate
            msg = f"selected tool {plan.name!r} has no candidate"
            raise RuntimeError(msg)
        arguments, strategy = best
        headers = param_headers(plan.header_map, arguments) if plan.header_map else {}
        rid = f"errors:{i}:{plan.name}"
        probe = _Probe(
            tool=plan.name,
            subject=plan.name,
            strategy=strategy,
            arguments=dict(arguments),
            request_id=rid,
        )
        reply = await self._post(
            ctx, plan.name, arguments, rid, headers, f"tools/call {plan.name} ({strategy})"
        )
        self._classify(ctx, f, probe, reply, plan.properties)
        return probe

    async def _probe_unknown(self, ctx: CheckContext, f: Findings, i: int) -> _Probe:
        rid = f"errors:{i}:{wire.UNKNOWN_TOOL_NAME}"
        probe = _Probe(
            tool=wire.UNKNOWN_TOOL_NAME,
            subject=UNKNOWN_SUBJECT,
            strategy=UNKNOWN_STRATEGY,
            arguments={},
            request_id=rid,
        )
        reply = await self._post(
            ctx, wire.UNKNOWN_TOOL_NAME, {}, rid, {}, "tools/call non-existent tool"
        )
        self._classify(ctx, f, probe, reply, ())
        return probe

    # ------------------------------------------------------------ step 4 classification

    def _classify(
        self,
        ctx: CheckContext,
        f: Findings,
        probe: _Probe,
        reply: RawReply,
        properties: Sequence[str],
    ) -> None:
        probe.http_status = reply.status
        probe.media_type = reply.media_type
        outcome = classify_reply(
            reply, sent_origin=ctx.cfg.origin is not None, sent_token=bool(ctx.cfg.token)
        )
        subject, tool = probe.subject, probe.tool

        if outcome.kind is OutcomeKind.TRANSPORT:
            probe.shape = "timeout" if "budget" in (reply.error or "") else "transport"
            probe.inconclusive = outcome.detail
            return
        if outcome.kind is OutcomeKind.REDIRECTED:
            probe.shape = "redirected"
            probe.inconclusive = outcome.detail
            return
        if outcome.kind is OutcomeKind.AUTH_REQUIRED:
            probe.shape = "auth"
            probe.auth = True
            probe.inconclusive = outcome.detail
            challenge = reply.headers.get("www-authenticate")
            probe.got = {"www-authenticate": challenge} if challenge else None
            return

        response = reply.any_response
        if response is None:
            status = reply.status
            if 200 <= status <= 299:
                if status == 202 or reply.media_type not in (wire.MEDIA_JSON, wire.MEDIA_SSE):
                    probe.shape = "bad_content_type"
                    probe.got = _body_text(reply)
                    media = reply.media_type or "no content-type"
                    f.must(
                        "errors.bad_content_type",
                        f"tools/call answered HTTP {status} {media} without a JSON-RPC response "
                        "(servers MUST answer application/json or text/event-stream)",
                        subject=subject,
                        tool=tool,
                        evidence=probe.evidence(expected="application/json or text/event-stream"),
                    )
                    return
                probe.shape = "missing_response"
                probe.got = _body_text(reply)
                probe.inconclusive = (
                    f"Not evaluated: no JSON-RPC response ({reply.parse_error or 'empty body'})"
                )
                f.should(
                    "errors.missing_response",
                    f"tools/call answered HTTP {status} {reply.media_type} without a JSON-RPC response "
                    f"({reply.parse_error or 'empty body'})",
                    subject=subject,
                    tool=tool,
                    evidence=probe.evidence(expected="a JSON-RPC response carrying our request id"),
                )
                return
            probe.shape = "no_jsonrpc_body"
            probe.got = _body_text(reply)
            probe.inconclusive = (
                outcome.detail if outcome.inconclusive else f"Not evaluated: HTTP {status}"
            )
            return

        self._stream_findings(f, probe, reply)

        got_id = response.get("id")
        if not (got_id == probe.request_id and type(got_id) is type(probe.request_id)):
            if got_id is None and "error" in response and 400 <= reply.status <= 499:
                f.should(
                    "errors.id_null_on_4xx",
                    f"HTTP {reply.status} error carries id:null although the request was well-formed "
                    f"(expected id {probe.request_id!r})",
                    subject=subject,
                    tool=tool,
                    evidence=probe.evidence(got=response),
                )
            else:
                probe.shape = "id_mismatch"
                probe.got = response
                f.must(
                    "errors.id_mismatch",
                    f"Response id {got_id!r} does not match the request id {probe.request_id!r}",
                    subject=subject,
                    tool=tool,
                    evidence=probe.evidence(expected=probe.request_id),
                )
                return

        if "error" in response:
            sent = ctx.transcript.exchanges[-1].request_headers if ctx.transcript.exchanges else []
            self._classify_error(f, probe, response, properties, headers_sent=sent)
            return
        self._classify_result(f, probe, response, properties)

    def _stream_findings(self, f: Findings, probe: _Probe, reply: RawReply) -> None:
        subject, tool = probe.subject, probe.tool
        for msg in reply.server_requests:
            f.must(
                "errors.server_request_on_stream",
                f"Server sent request {msg.get('method')!r} (id {msg.get('id')!r}) on the tools/call "
                "stream; the stream MUST only carry the response and notifications",
                subject=subject,
                tool=tool,
                evidence=probe.evidence(got=msg),
            )
        for msg in reply.notifications:
            method = msg.get("method")
            if method == "notifications/message":
                f.must(
                    "errors.log_without_loglevel",
                    "Server sent notifications/message although the request carried no "
                    f"{wire.META_LOG_LEVEL}",
                    subject=subject,
                    tool=tool,
                    evidence=probe.evidence(got=msg),
                )
            elif method == "notifications/progress":
                f.info(
                    "errors.progress_notification",
                    "Server sent notifications/progress although no progressToken was sent",
                    subject=subject,
                    tool=tool,
                    evidence=probe.evidence(got=msg),
                )

    def _classify_error(
        self,
        f: Findings,
        probe: _Probe,
        response: Mapping[str, Any],
        properties: Sequence[str],
        *,
        headers_sent: Sequence[tuple[str, str]],
    ) -> None:
        subject, tool = probe.subject, probe.tool
        probe.got = response
        rpc = wire.rpc_error_of(response)
        if rpc is None or not rpc[1]:
            probe.shape = "malformed_error"
            f.must(
                "errors.malformed_error",
                "JSON-RPC error lacks an integer code or a non-empty string message",
                subject=subject,
                tool=tool,
                evidence=probe.evidence(
                    expected={"code": "integer", "message": "non-empty string"}
                ),
            )
            return
        code, message, _data = rpc
        probe.error_code = code
        if code == wire.HEADER_MISMATCH:
            probe.shape = "header_mismatch"
            probe.inconclusive = (
                "Not evaluated: -32020 on tools/call (header contract disagreement)"
            )
            f.info(
                "errors.header_mismatch",
                f"tools/call rejected with -32020 {message!r}; headers sent recorded",
                subject=subject,
                tool=tool,
                evidence=probe.evidence(headers_sent=[list(h) for h in headers_sent]),
            )
            return
        if code in (wire.MISSING_REQUIRED_CLIENT_CAPABILITY, wire.UNSUPPORTED_PROTOCOL_VERSION):
            probe.shape = "protocol_rejection"
            probe.inconclusive = f"Not evaluated: {code} on tools/call"
            f.info(
                "errors.protocol_rejection",
                f"tools/call rejected with {code} {message!r}",
                subject=subject,
                tool=tool,
                evidence=probe.evidence(),
            )
            return
        if code == wire.METHOD_NOT_FOUND:
            probe.shape = "method_not_found"
            probe.message = message
            f.best_practice(
                "errors.tools_call_not_implemented",
                f"tools/call answered -32601 {message!r} although the server lists tools",
                subject=subject,
                tool=tool,
                evidence=probe.evidence(),
            )
            return
        probe.shape = "jsonrpc_error"
        if code in (-32002, -32042):
            f.must(
                "errors.forbidden_error_code",
                f"Error code {code} MUST NOT be emitted by a 2026-07-28 server "
                f"({'resource not found, replaced by -32602' if code == -32002 else 'URL elicitation, 2025-11-25 only'})",
                subject=subject,
                tool=tool,
                evidence=probe.evidence(expected="-32602 for invalid params"),
            )
        elif -32099 <= code <= -32020:
            f.must(
                "errors.reserved_error_code",
                f"Error code {code} is in the range reserved for the MCP specification and is not defined by it",
                subject=subject,
                tool=tool,
                evidence=probe.evidence(
                    expected="a defined code (e.g. -32602) or one outside -32768..-32000"
                ),
            )
        elif -32019 <= code <= -32000:
            f.should(
                "errors.legacy_error_code",
                f"Error code {code} is in the legacy -32000..-32019 range new implementations SHOULD NOT use",
                subject=subject,
                tool=tool,
                evidence=probe.evidence(
                    expected="-32602 or an application-defined code outside -32768..-32000"
                ),
            )
        self._judge(f, probe, message, properties, jsonrpc=True)

    def _classify_result(
        self,
        f: Findings,
        probe: _Probe,
        response: Mapping[str, Any],
        properties: Sequence[str],
    ) -> None:
        subject, tool = probe.subject, probe.tool
        result = response.get("result")
        probe.got = result if isinstance(result, dict) else response
        if not isinstance(result, dict):
            probe.shape = "legacy_result"
            f.must(
                "errors.missing_result_type",
                "tools/call result is not an object and cannot carry resultType",
                subject=subject,
                tool=tool,
                evidence=probe.evidence(expected={"resultType": "complete"}),
            )
            return
        result_type = result.get("resultType")
        if not isinstance(result_type, str):
            probe.shape = "legacy_result"
            f.must(
                "errors.missing_result_type",
                "tools/call result lacks a string resultType (pre-2026 shape)",
                subject=subject,
                tool=tool,
                evidence=probe.evidence(expected={"resultType": "complete"}),
            )
        elif result_type == "input_required":
            probe.shape = "input_required"
            has_requests = "inputRequests" in result
            has_state = "requestState" in result
            if has_requests:
                f.must(
                    "errors.undeclared_input_requests",
                    "Server sent inputRequests although the request declared no client capabilities "
                    "(clientCapabilities: {})",
                    subject=subject,
                    tool=tool,
                    evidence=probe.evidence(
                        expected="no inputRequests the client has not declared support for"
                    ),
                )
            elif not has_state:
                f.must(
                    "errors.empty_input_required",
                    "InputRequiredResult carries neither inputRequests nor requestState",
                    subject=subject,
                    tool=tool,
                    evidence=probe.evidence(
                        expected="at least one of inputRequests or requestState"
                    ),
                )
            else:
                probe.inconclusive = (
                    "Not evaluated: input_required with requestState only (load shedding)"
                )
                f.info(
                    "errors.input_required_state_only",
                    "tools/call answered input_required with requestState only (legal load shedding)",
                    subject=subject,
                    tool=tool,
                    evidence=probe.evidence(),
                )
            self._secondary_validation(f, probe, result)
            return
        elif result_type != "complete":
            probe.shape = "unknown_result_type"
            f.must(
                "errors.unknown_result_type",
                f"Unrecognized resultType {result_type!r} (clients MUST treat it as invalid)",
                subject=subject,
                tool=tool,
                evidence=probe.evidence(expected="complete or input_required"),
            )
            return

        self._secondary_validation(f, probe, result)
        if result.get("isError") is True:
            if probe.shape != "legacy_result":
                probe.shape = "is_error_result"
            if "structuredContent" in result:
                f.info(
                    "errors.structured_content_present",
                    "Error result also carries structuredContent",
                    subject=subject,
                    tool=tool,
                    evidence=probe.evidence(),
                )
            self._judge(f, probe, _text_of(result.get("content")), properties, jsonrpc=False)
            return

        # success on invalid input
        if probe.shape != "legacy_result":
            probe.shape = "success"
        if probe.strategy == UNKNOWN_STRATEGY:
            f.best_practice(
                "errors.unknown_tool_executed",
                f"Call to the non-existent tool {wire.UNKNOWN_TOOL_NAME!r} returned a non-error result",
                subject=subject,
                tool=tool,
                evidence=probe.evidence(expected=EXPECTED_ERROR_SHAPE),
            )
            return
        hard = probe.strategy == STRATEGY_MISSING_REQUIRED or any(
            v == ENUM_SENTINEL for v in probe.arguments.values()
        )
        what = STRATEGY_TEXT.get(probe.strategy, probe.strategy)
        if hard:
            f.must(
                "errors.accepted_invalid_input",
                f"Tool returned a non-error result for arguments that violate its inputSchema ({what})",
                subject=subject,
                tool=tool,
                evidence=probe.evidence(expected=EXPECTED_ERROR_SHAPE),
            )
        else:
            f.should(
                "errors.accepted_wrong_type",
                f"Tool returned a non-error result for wrong-typed arguments ({what}); lenient "
                "coercion is defensible but the inputSchema says otherwise",
                subject=subject,
                tool=tool,
                evidence=probe.evidence(expected=EXPECTED_ERROR_SHAPE),
            )

    def _secondary_validation(self, f: Findings, probe: _Probe, result: Mapping[str, Any]) -> None:
        if not isinstance(result.get("resultType"), str):
            return  # already a MUST finding (missing_result_type); the validator adds nothing
        locs = _sdk_result_locs(result)
        content = result.get("content")
        if (
            result.get("resultType") == "complete"
            and content is not None
            and not isinstance(content, list)
        ):
            locs = sorted({*locs, "content (not an array)"})
        if locs:
            f.should(
                "errors.malformed_result",
                f"CallToolResult rejected by the SDK validator at {', '.join(locs)}",
                subject=probe.subject,
                tool=probe.tool,
                evidence=probe.evidence(locations=locs),
            )

    def _judge(
        self,
        f: Findings,
        probe: _Probe,
        text: str,
        properties: Sequence[str],
        *,
        jsonrpc: bool,
    ) -> None:
        probe.message = text
        failed = judge_message(text, tool_name=probe.tool, properties=properties, jsonrpc=jsonrpc)
        probe.heuristics_failed = failed
        probe.actionable = is_actionable(failed)
        if jsonrpc and "A5" in failed:
            f.should(
                "errors.verbose_error_message",
                f"JSON-RPC error message is not a concise single sentence: {_clip_message(text)!r}",
                subject=probe.subject,
                tool=probe.tool,
                evidence=probe.evidence(expected="a concise single sentence (E6)"),
            )

    # ------------------------------------------------------------ verdict and summary

    def _finish(
        self,
        ctx: CheckContext,
        f: Findings,
        tools: Sequence[Mapping[str, Any]],
        selected: Sequence[ToolPlan],
        skipped: Sequence[ToolPlan],
        probes: Sequence[_Probe],
    ) -> None:
        judged = [p for p in probes if p.actionable is not None]
        actionable = sum(1 for p in judged if p.actionable)
        failing = [p for p in judged if not p.actionable]
        inconclusive = [p for p in probes if p.inconclusive is not None]

        f.details(
            f"Probed {len(selected)} tool(s) with schema-invalid arguments plus one non-existent "
            f"tool ({len(tools)} listed, {len(skipped)} skipped, --probe {ctx.cfg.probe})."
        )
        for probe in inconclusive:
            f.details(f"`{probe.subject}`: {probe.inconclusive}")

        ratio_warning = False
        if judged and actionable / len(judged) < ACTIONABLE_RATIO:
            ratio_warning = True
            f.best_practice(
                "errors.non_actionable_messages",
                f"{len(failing)} of {len(judged)} probed tools return non-actionable error messages "
                f"({actionable}/{len(judged)} actionable, threshold {ACTIONABLE_RATIO:.0%})",
                subject="tools/call",
                evidence={
                    "judged": len(judged),
                    "actionable": actionable,
                    "failing": [p.subject for p in failing],
                },
            )
            for probe in failing:
                f.best_practice(
                    "errors.non_actionable_message",
                    f"Error message fails heuristics {', '.join(probe.heuristics_failed)}: "
                    f"{_clip_message(probe.message or '')!r}",
                    subject=probe.subject,
                    tool=probe.tool,
                    evidence=probe.evidence(),
                )
        elif failing:
            f.details(
                "Non-actionable error messages (below the warning threshold): "
                + ", ".join(f"`{p.subject}` ({'/'.join(p.heuristics_failed)})" for p in failing)
            )

        if f.failures:
            f.summary(self._failure_summary(f, probes))
            return

        if not selected:
            listed = len(tools)
            if ctx.cfg.probe == "safe":
                f.inconclusive(
                    f"No probe-safe tools ({listed} listed; annotate readOnlyHint/destructiveHint "
                    "or use --probe all)"
                )
            else:
                f.inconclusive(
                    f"No testable tools ({listed} listed; permissive or invalid inputSchemas)"
                )
            return

        tool_probes = [p for p in probes if p.strategy != UNKNOWN_STRATEGY]
        if tool_probes and all(p.inconclusive is not None for p in tool_probes):
            if any(p.auth for p in tool_probes):
                status = next(p.http_status for p in tool_probes if p.auth)
                f.inconclusive(f"Inconclusive: HTTP {status} on tools/call (check --token)")
            else:
                f.inconclusive(tool_probes[0].inconclusive or "Not evaluated")
            return

        if ratio_warning:
            f.summary(f"{len(failing)} of {len(judged)} probed tools return non-actionable errors")
        elif f.warnings:
            slugs: list[str] = []
            for w in f.warnings:
                slug = w.code.split(".", 1)[1]
                if slug not in slugs:
                    slugs.append(slug)
            n = len(f.warnings)
            f.summary(
                f"Tool errors actionable; {n} warning{'s' if n != 1 else ''} ({', '.join(slugs)})"
            )
        else:
            f.summary("Tool errors carry actionable messages")

    @staticmethod
    def _failure_summary(f: Findings, probes: Sequence[_Probe]) -> str:
        codes = [x.code for x in f.failures]
        executed = codes.count("errors.accepted_invalid_input")
        if executed:
            return f"{executed} tool(s) executed on schema-invalid input"
        first = f.failures[0]
        tool = first.tool or first.subject
        probe = next((p for p in probes if p.subject == first.subject), None)
        if first.code == "errors.forbidden_error_code":
            return f"Forbidden error code {probe.error_code if probe else ''} from `{tool}`"
        if first.code == "errors.reserved_error_code":
            return f"Reserved error code {probe.error_code if probe else ''} from `{tool}`"
        if first.code == "errors.bad_content_type":
            media = (probe.media_type if probe else "") or "no content-type"
            return f"`tools/call` returned {media} (not JSON-RPC)"
        if first.code == "errors.missing_result_type":
            return f"`{tool}` result lacks resultType"
        if first.code == "errors.undeclared_input_requests":
            return f"`{tool}` requested undeclared client input"
        if first.code == "errors.empty_input_required":
            return f"`{tool}` returned an empty input_required result"
        if first.code == "errors.unknown_result_type":
            return f"`{tool}` returned an unrecognized resultType"
        return first.message


def _body_text(reply: RawReply) -> str | None:
    if not reply.raw:
        return None
    text, _err = wire.decode_utf8(reply.raw[:GOT_LIMIT])
    return text if text is not None else f"<{len(reply.raw)} bytes, not utf-8>"


__all__ = ["ErrorsCheck", "is_actionable", "judge_message"]
