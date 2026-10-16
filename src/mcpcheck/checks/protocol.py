"""The ``protocol`` check (PLAN.md §7.8): header, version and method MUSTs.

Each probe is one ``tools/list`` (``server/discover`` on tool-less servers) on a
fresh connection with exactly one deliberate defect that a conformant server
must reject without dispatching anything: a mismatched or missing
``MCP-Protocol-Version`` header, an unsupported version, a missing or
mis-cased ``Mcp-Method`` header, a ``_meta`` envelope without
``clientCapabilities``, an unknown method and a bogus cursor. With ``--probe``
other than ``none`` three ``tools/call`` probes name a tool that does not exist
(so nothing can execute): ``Mcp-Name`` omitted, ``Mcp-Name`` mismatching the
body and ``Mcp-Name`` Base64-encoded (T:502-505, T:583-585, T:621-624); plus one
``tools/call`` whose ``Mcp-Param-*`` header is omitted while its arguments are
proven invalid against the tool's own ``inputSchema``, and (``--legacy-probes``
only) one headers-only ``GET``.

Verified against the SDK reference server (mcp 2.2.0 and 2.3.0): a handshake-era
header value or a header-less request is routed to the legacy transport and
answered ``200`` with a 2025-shaped result (the dual-era MAY of T:277-281), so
the mismatch probe uses the non-handshake value ``2026-01-01`` and a
legacy-shaped ``200`` to the header-less probe is reported as an ``info``.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar

from mcpcheck import spec_urls, wire
from mcpcheck.checks import codes
from mcpcheck.checks.base import Check
from mcpcheck.wire import OutcomeKind, classify_reply

if TYPE_CHECKING:
    from mcpcheck.checks.base import CheckContext, Findings
    from mcpcheck.client import RawReply

# --------------------------------------------------------------------------- codes added here
# (codes.py is shared and read-only during the checks milestone; §7.8 pre-registers the rest)

CODE_DUAL_ERA = codes.register(
    "protocol.dual_era_server",
    "info",
    "Header-less request served by a legacy path (dual-era MAY, T:277-281)",
    spec_urls.SPEC_PROTOCOL_VERSION_HEADER,
)
CODE_HEADERLESS_REJECTION_MALFORMED = codes.register(
    "protocol.headerless_rejection_malformed",
    "should",
    "Header-less request rejected without 400 + -32020",
    spec_urls.SPEC_SERVER_VALIDATION,
)
CODE_MISMATCH_AS_UNSUPPORTED = codes.register(
    "protocol.mismatch_rejected_as_unsupported",
    "info",
    "Header/_meta mismatch rejected with -32022 (version check ran first)",
    spec_urls.SPEC_PROTOCOL_VERSION_HEADER,
)
CODE_UNSUPPORTED_WRONG_CODE = codes.register(
    "protocol.unsupported_wrong_code",
    "must",
    "Unsupported version rejected without -32022",
    spec_urls.SPEC_PROTOCOL_VERSION_HEADER,
)
CODE_UNSUPPORTED_OMITS_SERVED = codes.register(
    "protocol.unsupported_supported_omits_served",
    "should",
    "-32022 data.supported omits the version the server is serving",
    spec_urls.SPEC_SCHEMA_UNSUPPORTED_VERSION,
)
CODE_METHOD_HEADER_WRONG_REJECTION = codes.register(
    "protocol.method_header_wrong_rejection",
    "must",
    "Mcp-Method defect rejected without 400 + -32020",
    spec_urls.SPEC_SERVER_VALIDATION,
)
CODE_INVALID_CURSOR_IGNORED = codes.register(
    "protocol.invalid_cursor_ignored",
    "info",
    "Unknown cursor served as a normal page (no MUST violated)",
    spec_urls.SPEC_PAGINATION_ERRORS,
)
CODE_PARAM_HEADER_NOT_OBSERVABLE = codes.register(
    "protocol.param_header_not_observable",
    "info",
    "Arguments rejected before Mcp-Param-* validation could be observed",
    spec_urls.SPEC_PARAM_HEADERS,
)
CODE_MISSING_NAME_ACCEPTED = codes.register(
    "protocol.missing_mcp_name_accepted",
    "must",
    "tools/call without Mcp-Name accepted (required standard header missing)",
    spec_urls.SPEC_SERVER_VALIDATION,
)
CODE_NAME_MISMATCH_ACCEPTED = codes.register(
    "protocol.name_header_mismatch_accepted",
    "must",
    "Mcp-Name differing from params.name accepted",
    spec_urls.SPEC_SERVER_VALIDATION,
)
CODE_NAME_NOT_DECODED = codes.register(
    "protocol.name_header_not_decoded",
    "must",
    "Base64-encoded Mcp-Name rejected as a mismatch (servers MUST decode before comparing)",
    spec_urls.SPEC_VALUE_ENCODING,
)
CODE_NAME_WRONG_REJECTION = codes.register(
    "protocol.name_header_wrong_rejection",
    "must",
    "Mcp-Name defect rejected without 400 + -32020",
    spec_urls.SPEC_SERVER_VALIDATION,
)

# --------------------------------------------------------------------------- constants

MISMATCH_HEADER_VERSION = "2026-01-01"
"""Header value for the mismatch probe: not a handshake-era version, so SDK servers reject it."""
UNSUPPORTED_VERSION = "2099-01-01"
"""A version no server implements (and none can mistake for a legacy one)."""
UNKNOWN_METHOD = "mcpcheck/nonexistent"
INVALID_CURSOR = "mcpcheck-invalid-cursor-7f3a"
UNEXPECTED_ARGUMENT = "mcpcheck_nonexistent_argument_7f3a"
OTHER_NAME = "mcpcheck-other-name"
"""``Mcp-Name`` value that can never match ``params.name`` in the mismatch probe."""
LEGACY_SHAPE_KEYS = ("resultType", "ttlMs", "cacheScope")
"""A 2026-07-28 list/discover result carries these; a 2025-shaped one carries none."""
FORBIDDEN_ERROR_CODES: frozenset[int] = frozenset({-32002, -32042})
RESERVED_RANGE = (-32099, -32020)
X_ACCEL_BUFFERING = "x-accel-buffering"
EVIDENCE_LIMIT = 500
LEGACY_SESSION_ERROR_ID = "server-error"
_SESSION_WORD = re.compile(r"session", re.IGNORECASE)

_INFRA_KINDS = frozenset(
    {
        OutcomeKind.TRANSPORT,
        OutcomeKind.REDIRECTED,
        OutcomeKind.AUTH_REQUIRED,
        OutcomeKind.FORBIDDEN,
        OutcomeKind.SERVER_ERROR,
    }
)
_HEADER_PROPERTY_TYPES = ("string", "integer", "boolean")
_SAMPLE_VALUES: dict[str, Any] = {"string": "mcpcheck", "integer": 1, "boolean": True}


class _Verdict(str, Enum):
    """How a reply relates to the ``status + code`` rejection a probe expects."""

    PASS = "pass"  # noqa: S105 — a verdict label, not a password
    BARE_400 = "bare_400"  # HTTP 400 without a JSON-RPC body (intermediary, T:631-635)
    ACCEPTED = "accepted"  # 2xx without a JSON-RPC error: the defect went unnoticed
    WRONG_STATUS = "wrong_status"  # expected code, other HTTP status
    WRONG_CODE = "wrong_code"  # a JSON-RPC error with another code
    OTHER = "other"  # non-2xx without a JSON-RPC error (HTML 404, 405, …)


@dataclass(frozen=True)
class _Probe:
    """One sent probe: its evidence skeleton and the reply."""

    n: int
    subject: str
    diff: dict[str, Any]
    reply: RawReply

    def evidence(self, **extra: Any) -> dict[str, Any]:
        """``{sent_headers_diff, http_status, rpc_code, data, …}`` for a finding."""
        reply = self.reply
        ev: dict[str, Any] = {
            "probe": self.n,
            "sent_headers_diff": self.diff,
            "http_status": reply.status,
            "rpc_code": None,
        }
        rpc = _first_rpc_error(reply)
        if rpc is not None:
            ev["rpc_code"] = rpc[0]
            ev["rpc_message"] = rpc[1][:200]
            if rpc[2] is not None:
                ev["data"] = _clip_json(rpc[2])
        if reply.media_type:
            ev["content_type"] = reply.media_type
        ev.update(extra)
        return ev


# --------------------------------------------------------------------------- pure helpers


def _first_rpc_error(reply: RawReply) -> tuple[int, str, Any] | None:
    """The first JSON-RPC error object in the body regardless of its id."""
    for msg in reply.messages:
        err = wire.rpc_error_of(msg)
        if err is not None:
            return err
    return None


def _has_result(reply: RawReply) -> bool:
    return any("result" in m for m in reply.messages)


def _clip_json(value: Any) -> Any:
    """Keep small JSON values as-is; render large ones as a clipped string."""
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = repr(value)
    if len(text) <= EVIDENCE_LIMIT:
        return value
    return text[: EVIDENCE_LIMIT - 1] + "…"


def _describe(reply: RawReply) -> str:
    """``HTTP 400 -32020`` / ``HTTP 200 result`` / ``HTTP 404 text/html`` for messages."""
    rpc = _first_rpc_error(reply)
    if rpc is not None:
        return f"HTTP {reply.status} with JSON-RPC {rpc[0]}"
    if _has_result(reply):
        return f"HTTP {reply.status} with a result"
    media = reply.media_type or "no content-type"
    return f"HTTP {reply.status} ({media}, no JSON-RPC body)"


def _judge(reply: RawReply, *, status: int, code: int) -> _Verdict:
    """Compare a reply with the ``status + code`` rejection the spec demands."""
    rpc = _first_rpc_error(reply)
    if rpc is not None:
        if rpc[0] == code:
            return _Verdict.PASS if reply.status == status else _Verdict.WRONG_STATUS
        return _Verdict.WRONG_CODE
    if 200 <= reply.status <= 299:
        return _Verdict.ACCEPTED
    if reply.status == 400 and status == 400:
        return _Verdict.BARE_400
    return _Verdict.OTHER


def is_legacy_shaped(result: Mapping[str, Any]) -> bool:
    """A result without any 2026-07-28 envelope field came from a 2025-era code path."""
    return not any(key in result for key in LEGACY_SHAPE_KEYS)


def base64_sentinel(value: str) -> str:
    """The ``=?base64?…?=`` form of a header value (T:491-509), regardless of whether it needs it."""
    return f"=?base64?{base64.b64encode(value.encode('utf-8')).decode('ascii')}?="


def is_legacy_session_rejection(reply: RawReply) -> bool:
    """A 4xx whose JSON-RPC error is the mcp 1.x session gate (``handshake`` fingerprint ``legacy_session``)."""
    rpc = _first_rpc_error(reply)
    if rpc is None or rpc[0] not in (wire.INVALID_REQUEST, wire.INVALID_PARAMS):
        return False
    if any(wire.is_response(m) and m.get("id") == LEGACY_SESSION_ERROR_ID for m in reply.messages):
        return True
    return bool(_SESSION_WORD.search(rpc[1]))


def is_forbidden_error_code(code: int) -> bool:
    """``-32002``/``-32042`` or an undefined code from the MCP-reserved sub-range (B:124-146)."""
    if code in FORBIDDEN_ERROR_CODES:
        return True
    low, high = RESERVED_RANGE
    return low <= code <= high and code not in wire.MODERN_ERROR_CODES


def is_probe_eligible(tool: Mapping[str, Any], policy: str) -> bool:
    """``safe``: only tools annotated read-only or non-destructive; ``all``: every tool."""
    if policy == "none":
        return False
    if policy == "all":
        return True
    annotations = tool.get("annotations")
    if not isinstance(annotations, Mapping):
        return False
    return annotations.get("readOnlyHint") is True or annotations.get("destructiveHint") is False


def _proven_invalid(schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> bool:
    """True only when jsonschema proves ``arguments`` invalid against ``schema``."""
    from jsonschema import Draft202012Validator
    from jsonschema.validators import validator_for

    try:
        cls = validator_for(dict(schema), default=Draft202012Validator)
        cls.check_schema(dict(schema))
        return not cls(dict(schema)).is_valid(dict(arguments))
    except Exception:  # noqa: BLE001 — hostile schemas (RecursionError, bad $schema) just disqualify the tool
        return False


def select_param_probe(
    tools: list[dict[str, Any]], policy: str
) -> tuple[str, dict[str, Any], str] | None:
    """Pick ``(tool name, proven-invalid arguments, header token)`` for probe 9, or ``None``.

    The arguments carry the ``x-mcp-header`` property (so the server expects an
    ``Mcp-Param-*`` header) and are proven invalid against the tool's own
    ``inputSchema`` so that a server which fails to enforce the header still
    cannot execute anything.
    """
    for tool in tools:
        name = tool.get("name")
        schema = tool.get("inputSchema")
        if not isinstance(name, str) or not name or not isinstance(schema, Mapping):
            continue
        if not is_probe_eligible(tool, policy):
            continue
        properties = schema.get("properties")
        if not isinstance(properties, Mapping):
            continue
        for prop_name, prop in properties.items():
            if not isinstance(prop, Mapping) or not isinstance(prop_name, str):
                continue
            token = prop.get("x-mcp-header")
            prop_type = prop.get("type")
            if not isinstance(token, str) or prop_type not in _HEADER_PROPERTY_TYPES:
                continue
            value: Any = _SAMPLE_VALUES[prop_type]
            enum = prop.get("enum")
            if isinstance(enum, list) and enum and isinstance(enum[0], type(value)):
                value = enum[0]
            candidates = ({prop_name: value}, {prop_name: value, UNEXPECTED_ARGUMENT: True})
            for arguments in candidates:
                if _proven_invalid(schema, arguments):
                    return name, dict(arguments), token
    return None


# --------------------------------------------------------------------------- the check


class ProtocolCheck(Check):
    """Protocol headers and version errors validated."""

    name: ClassVar[str] = "protocol"
    title: ClassVar[str] = "Protocol headers and version errors validated"
    spec_url: ClassVar[str] = spec_urls.SPEC_PROTOCOL_VERSION_HEADER
    order: ClassVar[int] = 40

    async def run(self, ctx: CheckContext, f: Findings) -> None:
        """Send the §7.8 probe table on fresh connections and judge each reply."""
        tools = await ctx.tools()
        base = "tools/list" if tools is not None else "server/discover"
        not_evaluated: list[tuple[str, str]] = []
        observed: list[str] = []
        probes: list[_Probe] = []

        async def send(
            n: int,
            subject: str,
            diff: dict[str, Any],
            method: str,
            params: dict[str, Any] | None = None,
            **kw: Any,
        ) -> _Probe | None:
            async with ctx.fresh(f"protocol-{n}") as raw:
                reply = await raw.post(method, params, purpose=f"probe {n}: {subject}", **kw)
            probe = _Probe(n, f"probe {n}: {subject}", diff, reply)
            probes.append(probe)
            outcome = classify_reply(
                reply, sent_origin=ctx.cfg.origin is not None, sent_token=bool(ctx.cfg.token)
            )
            if outcome.kind in _INFRA_KINDS:
                not_evaluated.append((probe.subject, outcome.detail))
                observed.append(f"{probe.subject} → {outcome.detail}")
                return None
            observed.append(f"{probe.subject} → {_describe(reply)}")
            return probe

        # 1. header ≠ _meta (non-handshake header value; see module docstring)
        probe = await send(
            1,
            f"MCP-Protocol-Version {MISMATCH_HEADER_VERSION} vs _meta {wire.PROTOCOL_VERSION}",
            {wire.H_PROTOCOL_VERSION: MISMATCH_HEADER_VERSION},
            base,
            header_version=MISMATCH_HEADER_VERSION,
        )
        if probe is not None:
            self._judge_mismatch(f, probe)

        # 2. no MCP-Protocol-Version header at all
        probe = await send(
            2,
            "no MCP-Protocol-Version header",
            {wire.H_PROTOCOL_VERSION: None},
            base,
            header_version=None,
        )
        if probe is not None:
            self._judge_headerless(f, probe)

        # 3. header and _meta both an unsupported version
        probe = await send(
            3,
            f"protocol version {UNSUPPORTED_VERSION}",
            {
                wire.H_PROTOCOL_VERSION: UNSUPPORTED_VERSION,
                "_meta.protocolVersion": UNSUPPORTED_VERSION,
            },
            base,
            meta_version=UNSUPPORTED_VERSION,
        )
        if probe is not None:
            self._judge_unsupported(f, probe, ctx)

        # 4. no Mcp-Method header
        probe = await send(
            4,
            "no Mcp-Method header",
            {wire.H_METHOD: None},
            base,
            omit_headers=(wire.H_METHOD,),
        )
        if probe is not None:
            self._judge_method_header(f, probe, "protocol.missing_mcp_method_accepted")

        # 5. Mcp-Method value with the wrong case (values are case-sensitive, T:575-577)
        cased = base.title()
        probe = await send(
            5,
            f"Mcp-Method {cased}",
            {wire.H_METHOD: cased},
            base,
            headers={wire.H_METHOD: cased},
        )
        if probe is not None:
            self._judge_method_header(f, probe, "protocol.method_header_case_accepted")

        # 6. _meta without io.modelcontextprotocol/clientCapabilities
        probe = await send(
            6,
            "_meta without clientCapabilities",
            {"_meta.clientCapabilities": None},
            base,
            omit_meta_keys=(wire.META_CLIENT_CAPABILITIES,),
        )
        if probe is not None:
            self._judge_missing_meta(f, probe)

        # 7. unknown method (Mcp-Method mirrors it)
        probe = await send(
            7, f"method {UNKNOWN_METHOD}", {"method": UNKNOWN_METHOD}, UNKNOWN_METHOD
        )
        if probe is not None:
            self._judge_unknown_method(f, probe)

        # 8. invalid cursor (list methods only)
        if base == "tools/list":
            probe = await send(
                8,
                f"tools/list cursor {INVALID_CURSOR!r}",
                {"params.cursor": INVALID_CURSOR},
                base,
                {"cursor": INVALID_CURSOR},
            )
            if probe is not None:
                self._judge_cursor(f, probe)

        # 9. optional: tools/call with the Mcp-Param-* header omitted, arguments proven invalid
        selected = select_param_probe(tools or [], ctx.cfg.probe)
        if selected is not None:
            tool_name, arguments, token = selected
            header = f"Mcp-Param-{token}"
            probe = await send(
                9,
                f"tools/call {tool_name} without {header}",
                {header: None, "arguments": arguments},
                "tools/call",
                {"name": tool_name, "arguments": arguments},
            )
            if probe is not None:
                self._judge_param_header(f, probe, tool_name, header)
        elif ctx.cfg.probe == "none":
            observed.append("probe 9 (Mcp-Param-* omission) skipped: --probe none")
        else:
            observed.append(
                "probe 9 (Mcp-Param-* omission) skipped: no eligible tool with an x-mcp-header "
                "property and a provably invalid payload"
            )

        # 11-13. Mcp-Name validation on tools/call naming a tool that does not exist
        # (T:583-585 mismatch MUST, T:621-624 missing header, T:502-505 decode before compare)
        if ctx.cfg.probe == "none":
            observed.append("probes 11-13 (Mcp-Name) skipped: --probe none")
        else:
            unknown = {"name": wire.UNKNOWN_TOOL_NAME, "arguments": {}}
            probe = await send(
                11,
                f"tools/call {wire.UNKNOWN_TOOL_NAME} without Mcp-Name",
                {wire.H_NAME: None},
                "tools/call",
                unknown,
                omit_headers=(wire.H_NAME,),
            )
            if probe is not None:
                self._judge_name_header(f, probe, CODE_MISSING_NAME_ACCEPTED, "no Mcp-Name header")
            probe = await send(
                12,
                f"tools/call {wire.UNKNOWN_TOOL_NAME} with Mcp-Name {OTHER_NAME}",
                {wire.H_NAME: OTHER_NAME},
                "tools/call",
                unknown,
                headers={wire.H_NAME: OTHER_NAME},
            )
            if probe is not None:
                self._judge_name_header(
                    f, probe, CODE_NAME_MISMATCH_ACCEPTED, f"Mcp-Name {OTHER_NAME} ≠ params.name"
                )
            encoded = base64_sentinel(wire.UNKNOWN_TOOL_NAME)
            probe = await send(
                13,
                f"tools/call {wire.UNKNOWN_TOOL_NAME} with Base64-encoded Mcp-Name",
                {wire.H_NAME: encoded},
                "tools/call",
                unknown,
                headers={wire.H_NAME: encoded},
            )
            if probe is not None:
                self._judge_name_encoded(f, probe, encoded)

        # 10. --legacy-probes only: headers-only GET
        if ctx.cfg.legacy_probes:
            async with ctx.fresh("protocol-10") as raw:
                reply = await raw.http("GET", purpose="probe 10: headers-only GET")
            self._judge_legacy_get(f, reply, observed)

        # cross-cutting: forbidden error codes, X-Accel-Buffering on SSE
        self._scan_replies(f, probes)

        f.details("Probes:\n" + "\n".join(f"  {line}" for line in observed))
        if not_evaluated:
            f.inconclusive(not_evaluated[0][1])
            for subject, detail in not_evaluated[1:]:
                f.details(f"{subject}: {detail}")
        elif not f.failures and not f.warnings:
            f.summary("Header validation and version errors conform")

    # ------------------------------------------------------------------ probe verdicts

    @staticmethod
    def _judge_mismatch(f: Findings, probe: _Probe) -> None:
        reply = probe.reply
        verdict = _judge(reply, status=400, code=wire.HEADER_MISMATCH)
        rpc = _first_rpc_error(reply)
        head = f"MCP-Protocol-Version {MISMATCH_HEADER_VERSION} vs _meta {wire.PROTOCOL_VERSION}"
        if verdict is _Verdict.PASS:
            return
        if verdict is _Verdict.BARE_400:
            f.info(
                "protocol.bare_400_accepted",
                f"{head} rejected with a bare HTTP 400 (no JSON-RPC body; intermediary?)",
                subject=probe.subject,
                evidence=probe.evidence(),
            )
            return
        if verdict is _Verdict.ACCEPTED:
            f.must(
                "protocol.header_mismatch_accepted",
                f"Version/header mismatch accepted ({head} → HTTP {reply.status})",
                subject=probe.subject,
                evidence=probe.evidence(expected="HTTP 400 + -32020"),
            )
            return
        if (
            verdict is _Verdict.WRONG_CODE
            and rpc is not None
            and rpc[0] == wire.UNSUPPORTED_PROTOCOL_VERSION
            and reply.status == 400
        ):
            f.info(
                CODE_MISMATCH_AS_UNSUPPORTED,
                f"{head} rejected with -32022 (version support checked before header/body match)",
                subject=probe.subject,
                evidence=probe.evidence(),
            )
            return
        f.must(
            "protocol.header_mismatch_wrong_status",
            f"Version/header mismatch rejected with {_describe(reply)} instead of HTTP 400 + -32020",
            subject=probe.subject,
            evidence=probe.evidence(expected="HTTP 400 + -32020"),
        )

    @staticmethod
    def _judge_headerless(f: Findings, probe: _Probe) -> None:
        reply = probe.reply
        verdict = _judge(reply, status=400, code=wire.HEADER_MISMATCH)
        rpc = _first_rpc_error(reply)
        if verdict is _Verdict.PASS:
            return
        if verdict is _Verdict.BARE_400:
            f.info(
                "protocol.bare_400_accepted",
                "Header-less request rejected with a bare HTTP 400 (no JSON-RPC body; intermediary?)",
                subject=probe.subject,
                evidence=probe.evidence(),
            )
            return
        if verdict is _Verdict.ACCEPTED:
            result = reply.result
            if result is not None and is_legacy_shaped(result):
                f.info(
                    CODE_DUAL_ERA,
                    "Request without MCP-Protocol-Version served a 2025-shaped result: "
                    "dual-era server (permitted for pre-2025-06-18 clients, T:277-281)",
                    subject=probe.subject,
                    evidence=probe.evidence(result_keys=sorted(result)[:10]),
                )
                return
            f.should(
                "protocol.headerless_accepted",
                f"Request without MCP-Protocol-Version accepted as 2026-07-28 (HTTP {reply.status}, "
                "modern-shaped result) instead of HTTP 400 + -32020",
                subject=probe.subject,
                evidence=probe.evidence(expected="HTTP 400 + -32020"),
            )
            return
        if rpc is not None and rpc[0] == wire.METHOD_NOT_FOUND:
            f.info(
                CODE_DUAL_ERA,
                f"Request without MCP-Protocol-Version reached a legacy path that does not "
                f"implement {reply.method} ({_describe(reply)}): dual-era server",
                subject=probe.subject,
                evidence=probe.evidence(),
            )
            return
        if rpc is not None and 400 <= reply.status <= 499 and is_legacy_session_rejection(reply):
            # T:277-281: the header-less request MAY be treated as 2025-03-26, where a request
            # outside a session is rejected (mcp 1.x: id "server-error" / "Missing session ID")
            f.info(
                CODE_DUAL_ERA,
                f"Request without MCP-Protocol-Version reached the legacy session path "
                f"({_describe(reply)}, {rpc[1][:60]!r}): dual-era server",
                subject=probe.subject,
                evidence=probe.evidence(),
            )
            return
        f.should(
            CODE_HEADERLESS_REJECTION_MALFORMED,
            f"Request without MCP-Protocol-Version rejected with {_describe(reply)} "
            "instead of HTTP 400 + -32020",
            subject=probe.subject,
            evidence=probe.evidence(expected="HTTP 400 + -32020"),
        )

    @staticmethod
    def _judge_unsupported(f: Findings, probe: _Probe, ctx: CheckContext) -> None:
        reply = probe.reply
        verdict = _judge(reply, status=400, code=wire.UNSUPPORTED_PROTOCOL_VERSION)
        head = f"Unsupported protocol version {UNSUPPORTED_VERSION}"
        if verdict is _Verdict.PASS:
            rpc = _first_rpc_error(reply)
            data = rpc[2] if rpc is not None else None
            problems: list[str] = []
            supported: list[str] = []
            if not isinstance(data, Mapping):
                problems.append("data is missing or not an object")
            else:
                raw_supported = data.get("supported")
                if (
                    isinstance(raw_supported, list)
                    and raw_supported
                    and all(isinstance(v, str) for v in raw_supported)
                ):
                    supported = list(raw_supported)
                else:
                    problems.append("data.supported is not a non-empty list of strings")
                if data.get("requested") != UNSUPPORTED_VERSION:
                    problems.append(f"data.requested is not {UNSUPPORTED_VERSION!r}")
            if problems:
                f.must(
                    "protocol.unsupported_data_missing",
                    f"-32022 for {UNSUPPORTED_VERSION} lacks the required data: "
                    + "; ".join(problems),
                    subject=probe.subject,
                    evidence=probe.evidence(
                        expected={"supported": ["…"], "requested": UNSUPPORTED_VERSION}
                    ),
                )
            elif wire.PROTOCOL_VERSION not in supported:
                f.should(
                    CODE_UNSUPPORTED_OMITS_SERVED,
                    f"-32022 data.supported {supported!r} omits {wire.PROTOCOL_VERSION}, "
                    "which this server is serving",
                    subject=probe.subject,
                    evidence=probe.evidence(),
                )
            if supported and ctx.server_supported_versions is None:
                ctx.server_supported_versions = supported
            return
        if verdict is _Verdict.BARE_400:
            f.info(
                "protocol.bare_400_accepted",
                f"{head} rejected with a bare HTTP 400 (no JSON-RPC body; intermediary?)",
                subject=probe.subject,
                evidence=probe.evidence(),
            )
            return
        if verdict is _Verdict.ACCEPTED:
            f.must(
                "protocol.unsupported_version_not_rejected",
                f"{head} accepted (HTTP {reply.status}) instead of HTTP 400 + -32022",
                subject=probe.subject,
                evidence=probe.evidence(expected="HTTP 400 + -32022"),
            )
            return
        if verdict is _Verdict.WRONG_STATUS:
            f.must(
                "protocol.unsupported_wrong_status",
                f"{head} rejected with -32022 but HTTP {reply.status} instead of 400",
                subject=probe.subject,
                evidence=probe.evidence(expected="HTTP 400 + -32022"),
            )
            return
        f.must(
            CODE_UNSUPPORTED_WRONG_CODE,
            f"{head} rejected with {_describe(reply)} instead of HTTP 400 + -32022",
            subject=probe.subject,
            evidence=probe.evidence(expected="HTTP 400 + -32022"),
        )

    @staticmethod
    def _judge_method_header(f: Findings, probe: _Probe, accepted_code: str) -> None:
        reply = probe.reply
        verdict = _judge(reply, status=400, code=wire.HEADER_MISMATCH)
        defect = probe.subject.split(": ", 1)[-1]
        if verdict is _Verdict.PASS:
            return
        if verdict is _Verdict.BARE_400:
            f.info(
                "protocol.bare_400_accepted",
                f"Request with {defect} rejected with a bare HTTP 400 (no JSON-RPC body; intermediary?)",
                subject=probe.subject,
                evidence=probe.evidence(),
            )
            return
        if verdict is _Verdict.ACCEPTED:
            f.must(
                accepted_code,
                f"Request with {defect} accepted (HTTP {reply.status}) instead of HTTP 400 + -32020",
                subject=probe.subject,
                evidence=probe.evidence(expected="HTTP 400 + -32020"),
            )
            return
        f.must(
            CODE_METHOD_HEADER_WRONG_REJECTION,
            f"Request with {defect} rejected with {_describe(reply)} instead of HTTP 400 + -32020",
            subject=probe.subject,
            evidence=probe.evidence(expected="HTTP 400 + -32020"),
        )

    @staticmethod
    def _judge_name_header(f: Findings, probe: _Probe, accepted_code: str, defect: str) -> None:
        """Probes 11/12: a missing or mismatching ``Mcp-Name`` MUST be rejected with 400 + -32020."""
        reply = probe.reply
        verdict = _judge(reply, status=400, code=wire.HEADER_MISMATCH)
        if verdict is _Verdict.PASS:
            return
        if verdict is _Verdict.BARE_400:
            f.info(
                "protocol.bare_400_accepted",
                f"tools/call with {defect} rejected with a bare HTTP 400 (no JSON-RPC body; intermediary?)",
                subject=probe.subject,
                evidence=probe.evidence(),
            )
            return
        if verdict is _Verdict.ACCEPTED:
            f.must(
                accepted_code,
                f"tools/call with {defect} was processed (HTTP {reply.status}) instead of being "
                "rejected with HTTP 400 + -32020; the Mcp-Name header was not validated",
                subject=probe.subject,
                evidence=probe.evidence(expected="HTTP 400 + -32020"),
            )
            return
        f.must(
            CODE_NAME_WRONG_REJECTION,
            f"tools/call with {defect} rejected with {_describe(reply)} instead of HTTP 400 + -32020",
            subject=probe.subject,
            evidence=probe.evidence(expected="HTTP 400 + -32020"),
        )

    @staticmethod
    def _judge_name_encoded(f: Findings, probe: _Probe, encoded: str) -> None:
        """Probe 13: the Base64 sentinel decodes to ``params.name``, so -32020 means "not decoded"."""
        reply = probe.reply
        rpc = _first_rpc_error(reply)
        if rpc is not None and rpc[0] == wire.HEADER_MISMATCH:
            f.must(
                CODE_NAME_NOT_DECODED,
                f"tools/call with Mcp-Name {encoded} (the Base64 form of params.name) was rejected "
                f"with {_describe(reply)}; servers MUST decode an encoded Mcp-Name before comparing",
                subject=probe.subject,
                evidence=probe.evidence(expected="the same reply as for a plain Mcp-Name"),
            )

    @staticmethod
    def _judge_missing_meta(f: Findings, probe: _Probe) -> None:
        reply = probe.reply
        verdict = _judge(reply, status=400, code=wire.INVALID_PARAMS)
        head = f"Request without _meta {wire.META_CLIENT_CAPABILITIES}"
        if verdict is _Verdict.PASS:
            return
        if verdict is _Verdict.BARE_400:
            f.info(
                "protocol.bare_400_accepted",
                f"{head} rejected with a bare HTTP 400 (no JSON-RPC body; intermediary?)",
                subject=probe.subject,
                evidence=probe.evidence(),
            )
            return
        if verdict is _Verdict.ACCEPTED:
            f.must(
                "protocol.missing_meta_accepted",
                f"{head} accepted (HTTP {reply.status}) instead of HTTP 400 + -32602",
                subject=probe.subject,
                evidence=probe.evidence(expected="HTTP 400 + -32602"),
            )
            return
        if verdict is _Verdict.WRONG_STATUS:
            f.must(
                "protocol.missing_meta_wrong_status",
                f"{head} rejected with -32602 but HTTP {reply.status} instead of 400",
                subject=probe.subject,
                evidence=probe.evidence(expected="HTTP 400 + -32602"),
            )
            return
        f.must(
            "protocol.missing_meta_wrong_code",
            f"{head} rejected with {_describe(reply)} instead of HTTP 400 + -32602",
            subject=probe.subject,
            evidence=probe.evidence(expected="HTTP 400 + -32602"),
        )

    @staticmethod
    def _judge_unknown_method(f: Findings, probe: _Probe) -> None:
        reply = probe.reply
        verdict = _judge(reply, status=404, code=wire.METHOD_NOT_FOUND)
        head = f"Unknown method {UNKNOWN_METHOD}"
        if verdict is _Verdict.PASS:
            return
        if verdict is _Verdict.WRONG_STATUS:
            f.must(
                "protocol.unknown_method_wrong_status",
                f"{head} answered with -32601 but HTTP {reply.status} instead of 404",
                subject=probe.subject,
                evidence=probe.evidence(expected="HTTP 404 + -32601"),
            )
            return
        f.must(
            "protocol.unknown_method_not_jsonrpc",
            f"{head} answered with {_describe(reply)} instead of HTTP 404 + -32601",
            subject=probe.subject,
            evidence=probe.evidence(expected="HTTP 404 + -32601"),
        )

    @staticmethod
    def _judge_cursor(f: Findings, probe: _Probe) -> None:
        reply = probe.reply
        verdict = _judge(reply, status=400, code=wire.INVALID_PARAMS)
        if verdict in (_Verdict.PASS, _Verdict.WRONG_STATUS):
            return  # -32602 at any status satisfies the SHOULD (PG:113)
        if verdict is _Verdict.ACCEPTED:
            f.info(
                CODE_INVALID_CURSOR_IGNORED,
                f"Unknown cursor {INVALID_CURSOR!r} served as a normal page (HTTP {reply.status}); "
                "ignoring cursors violates no MUST",
                subject=probe.subject,
                evidence=probe.evidence(),
            )
            return
        if verdict is _Verdict.BARE_400:
            f.info(
                "protocol.bare_400_accepted",
                f"Unknown cursor {INVALID_CURSOR!r} rejected with a bare HTTP 400 (no JSON-RPC body)",
                subject=probe.subject,
                evidence=probe.evidence(),
            )
            return
        f.should(
            "protocol.invalid_cursor_not_rejected",
            f"Unknown cursor {INVALID_CURSOR!r} answered with {_describe(reply)} instead of -32602",
            subject=probe.subject,
            evidence=probe.evidence(expected="-32602"),
        )

    @staticmethod
    def _judge_param_header(f: Findings, probe: _Probe, tool_name: str, header: str) -> None:
        reply = probe.reply
        verdict = _judge(reply, status=400, code=wire.HEADER_MISMATCH)
        head = f"tools/call {tool_name} with the body value present but {header} omitted"
        if verdict is _Verdict.PASS:
            return
        if verdict is _Verdict.BARE_400:
            f.info(
                "protocol.bare_400_accepted",
                f"{head} rejected with a bare HTTP 400 (no JSON-RPC body; intermediary?)",
                subject=probe.subject,
                evidence=probe.evidence(),
                tool=tool_name,
            )
            return
        if verdict is _Verdict.ACCEPTED:
            f.must(
                "protocol.param_header_omission_accepted",
                f"{head} accepted (HTTP {reply.status}) instead of HTTP 400 + -32020",
                subject=probe.subject,
                evidence=probe.evidence(expected="HTTP 400 + -32020"),
                tool=tool_name,
            )
            return
        if verdict is _Verdict.WRONG_STATUS:
            f.must(
                "protocol.header_mismatch_wrong_status",
                f"{head} rejected with -32020 but HTTP {reply.status} instead of 400",
                subject=probe.subject,
                evidence=probe.evidence(expected="HTTP 400 + -32020"),
                tool=tool_name,
            )
            return
        f.info(
            CODE_PARAM_HEADER_NOT_OBSERVABLE,
            f"{head} answered with {_describe(reply)}: the arguments were rejected before the "
            "header check could be observed",
            subject=probe.subject,
            evidence=probe.evidence(),
            tool=tool_name,
        )

    @staticmethod
    def _judge_legacy_get(f: Findings, reply: RawReply, observed: list[str]) -> None:
        subject = "probe 10: headers-only GET"
        if reply.error is not None and reply.status == 0:
            observed.append(f"{subject} → {reply.error}")
            return
        allow = reply.headers.get("allow")
        if reply.status == 405:
            observed.append(f"{subject} → HTTP 405" + (f" Allow: {allow}" if allow else ""))
            return
        if 200 <= reply.status <= 299 and reply.is_sse:
            observed.append(f"{subject} → HTTP {reply.status} text/event-stream")
            f.info(
                "protocol.legacy_get_hosted",
                f"GET with MCP-Protocol-Version {wire.PROTOCOL_VERSION} opened an SSE stream "
                f"(HTTP {reply.status}); a modern-only server SHOULD answer 405",
                subject=subject,
                evidence={
                    "http_status": reply.status,
                    "content_type": reply.media_type,
                    "events_read": reply.sse_event_ids,
                },
            )
            return
        observed.append(
            f"{subject} → HTTP {reply.status} ({reply.media_type or 'no content-type'})"
        )

    # ------------------------------------------------------------------ cross-cutting scans

    @staticmethod
    def _scan_replies(f: Findings, probes: list[_Probe]) -> None:
        seen_codes: set[int] = set()
        accel_reported = False
        for probe in probes:
            reply = probe.reply
            for msg in reply.messages:
                err = wire.rpc_error_of(msg)
                if err is None or err[0] in seen_codes or not is_forbidden_error_code(err[0]):
                    continue
                seen_codes.add(err[0])
                why = (
                    "retired by 2026-07-28 (MUST NOT be emitted)"
                    if err[0] in FORBIDDEN_ERROR_CODES
                    else "undefined in the MCP-reserved range -32020…-32099"
                )
                f.must(
                    "protocol.forbidden_error_code",
                    f"Error code {err[0]} ({err[1][:80]!r}) is {why}",
                    subject=probe.subject,
                    evidence=probe.evidence(forbidden_code=err[0]),
                )
            if reply.is_sse and not accel_reported:
                value = reply.headers.get(X_ACCEL_BUFFERING)
                if value is None or value.strip().lower() != "no":
                    accel_reported = True
                    f.should(
                        "protocol.no_accel_buffering_header",
                        "SSE response lacks 'X-Accel-Buffering: no'"
                        + (f" (got {value!r})" if value is not None else ""),
                        subject=probe.subject,
                        evidence={"http_status": reply.status, X_ACCEL_BUFFERING: value},
                    )


__all__ = [
    "INVALID_CURSOR",
    "MISMATCH_HEADER_VERSION",
    "OTHER_NAME",
    "UNKNOWN_METHOD",
    "UNSUPPORTED_VERSION",
    "ProtocolCheck",
    "base64_sentinel",
    "is_forbidden_error_code",
    "is_legacy_session_rejection",
    "is_legacy_shaped",
    "is_probe_eligible",
    "select_param_probe",
]
