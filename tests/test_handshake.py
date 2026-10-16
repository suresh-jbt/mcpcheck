"""Tests for the ``handshake`` check (PLAN.md §7.1 part 9, §10.4/§10.5 rows).

L1: verdict logic over ``ScriptedServer`` (MockTransport), asserting what mcpcheck
*sent*. L2: real wire shapes through the ASGI fixtures (SDK reference app and the
misbehaving toggles/variants). L3: hazards over TCP (timeouts, lingering SSE,
fresh connections, request log, connect refused).
"""

from __future__ import annotations

import itertools
import time
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

import anyio
import httpx2
import pytest

from mcpcheck import __version__, spec_urls, wire
from mcpcheck.checks.base import Check, CheckContext, Era, Findings, Status, clip_summary
from mcpcheck.checks.handshake import LEGACY_PROBE_ID, LEGACY_PROBE_VERSION, HandshakeCheck
from mcpcheck.client import ClientConfig, RawClient, Transcript
from mcpcheck.runner import gate_reason, run_checks
from tests.conftest import ASGI_BASE_URL, fetch_request_log, free_port
from tests.fixtures import misbehaving_server, reference_server
from tests.fixtures.misbehaving_server import SESSION_ID, Toggles
from tests.helpers.scripted import (
    ScriptedServer,
    error_response,
    jsonrpc_error,
    list_result,
    ok,
    sse_body,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from mcpcheck.models import CheckResult, Finding, Report

pytestmark = pytest.mark.anyio

URL = "http://127.0.0.1:1/mcp"
TOOLS: list[dict[str, Any]] = [{"name": "echo", "inputSchema": {"type": "object"}}]
LEGACY_INIT_RESULT: dict[str, Any] = {
    "protocolVersion": "2025-11-25",
    "capabilities": {"tools": {}},
    "serverInfo": {"name": "scripted", "version": "0.0.1"},
}
LEGACY_SESSION_REJECT = (400, -32600, "Bad Request: Missing session ID")


# --------------------------------------------------------------------------- harness


class _TransportContext(CheckContext):
    """A ``CheckContext`` whose connections all go through an injected transport."""

    def __init__(
        self,
        cfg: ClientConfig,
        transcript: Transcript,
        make_transport: Callable[[], httpx2.AsyncBaseTransport],
    ) -> None:
        self._make_transport = make_transport
        super().__init__(
            cfg, transcript, RawClient(cfg, transcript, label="main", transport=make_transport())
        )

    @asynccontextmanager
    async def fresh(self, label: str, *, send_token: bool = True) -> AsyncIterator[RawClient]:
        client = RawClient(
            self.cfg,
            self.transcript,
            label=label,
            send_token=send_token,
            transport=self._make_transport(),
        )
        try:
            yield client
        finally:
            await client.aclose()


@dataclass
class Run:
    """Outcome of one handshake run in the harness."""

    result: CheckResult
    ctx: CheckContext
    transcript: Transcript

    @property
    def findings(self) -> list[Finding]:
        return [*self.result.failures, *self.result.warnings, *self.result.info]

    @property
    def codes(self) -> set[str]:
        return {f.code for f in self.findings}

    def finding(self, code: str) -> Finding:
        return next(f for f in self.findings if f.code == code)

    def evidence(self, code: str) -> dict[str, Any]:
        ev = self.finding(code).evidence
        assert ev is not None
        return ev

    @property
    def blocked(self) -> bool:
        return gate_reason(self.result) is not None


async def run_with(
    make_transport: Callable[[], httpx2.AsyncBaseTransport], cfg: ClientConfig
) -> Run:
    transcript = Transcript()
    ctx = _TransportContext(cfg, transcript, make_transport)
    check = HandshakeCheck()
    f = Findings(check, strict=cfg.strict)
    ctx.current_check = check.name
    transcript.current_check = check.name
    try:
        with anyio.fail_after(cfg.check_budget):
            await check.run(ctx, f)
    finally:
        await ctx.raw.aclose()
    return Run(f.to_result(requests=len(transcript)), ctx, transcript)


async def run_scripted(server: ScriptedServer, **cfg_kw: Any) -> Run:
    return await run_with(server.transport, ClientConfig(url=URL, **cfg_kw))


async def run_asgi(app: Any, *, lifespan: bool, **cfg_kw: Any) -> Run:
    ports = itertools.count(40001)

    def make() -> httpx2.AsyncBaseTransport:
        return httpx2.ASGITransport(app, client=("127.0.0.1", next(ports)))

    async with AsyncExitStack() as stack:
        if lifespan:
            await stack.enter_async_context(app.router.lifespan_context(app))
        return await run_with(make, ClientConfig(url=ASGI_BASE_URL + "/mcp", **cfg_kw))


async def run_reference(**cfg_kw: Any) -> Run:
    build = {k: cfg_kw.pop(k) for k in list(cfg_kw) if k in _REFERENCE_BUILD_KEYS}
    return await run_asgi(reference_server.build_app(**build), lifespan=True, **cfg_kw)


_REFERENCE_BUILD_KEYS = frozenset(
    {"json_response", "stateless_http", "bare", "empty", "require_token", "origin_check"}
)


async def run_misbehaving(toggles: Toggles, **cfg_kw: Any) -> Run:
    return await run_asgi(misbehaving_server.build_app(toggles), lifespan=False, **cfg_kw)


class _Dependent(Check):
    """A stand-in dependent check (named ``cache`` so the result model accepts it)."""

    name: ClassVar[str] = "cache"
    title: ClassVar[str] = "dependent stand-in"
    spec_url: ClassVar[str] = spec_urls.SPEC_CACHEABLE_RESULTS
    order: ClassVar[int] = 70

    async def run(self, ctx: CheckContext, f: Findings) -> None:
        f.summary(f"ran; page0 tools={await ctx.tools()!r}"[:100])


async def run_tcp(url: str, **cfg_kw: Any) -> Report:
    return await run_checks(ClientConfig(url=url, **cfg_kw), [HandshakeCheck(), _Dependent()])


def tools_server(
    *,
    sse: bool = False,
    result: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> ScriptedServer:
    res = result if result is not None else list_result(TOOLS)
    return ScriptedServer(routes={"tools/list": lambda b, _r: ok(b, res, sse=sse, headers=headers)})


def legacy_initialize(
    body: dict[str, Any], _req: httpx2.Request, *, session: str | None = SESSION_ID
) -> httpx2.Response:
    headers = {"Mcp-Session-Id": session} if session else {}
    return ok(body, LEGACY_INIT_RESULT, headers=headers)


def verbs_and_methods(log: list[dict[str, Any]]) -> list[tuple[str, str | None]]:
    return [(e["verb"], e["method"]) for e in log]


# =========================================================================== L1: pass paths


@pytest.mark.parametrize("sse", [False, True])
async def test_pass_scripted_and_exact_request(sse: bool) -> None:
    server = tools_server(sse=sse)
    run = await run_scripted(server)
    assert run.result.status is Status.PASS
    assert run.result.summary == "Server does not require `initialize`"
    assert run.codes == set()
    assert run.ctx.era is Era.UNKNOWN
    assert run.ctx.protocol_version == "2026-07-28"
    assert run.ctx.server_info == {"name": "scripted", "version": "0.0.1"}
    assert "legacy initialize: not probed (--legacy-probes)" in run.result.details
    assert run.result.requests == 1
    assert not run.blocked
    # exactly the §7.1 step-0 request, on the fresh "handshake" connection
    seen = server.seen[0]
    assert server.methods() == ["tools/list"]
    assert seen.request.method == "POST"
    h = seen.headers
    assert h["accept"] == "application/json, text/event-stream"
    assert h["content-type"] == "application/json"
    assert h["mcp-protocol-version"] == "2026-07-28"
    assert h["mcp-method"] == "tools/list"
    assert h["user-agent"] == f"mcpcheck/{__version__} (+https://github.com/suresh-jbt/mcp-check)"
    assert "origin" not in h
    assert "authorization" not in h
    assert "mcp-session-id" not in h
    assert seen.body == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
                "io.modelcontextprotocol/clientInfo": {"name": "mcpcheck", "version": __version__},
            }
        },
    }
    assert [e.connection for e in run.transcript] == ["handshake"]
    # page 0 is seeded: the dependents' walk does not re-request it
    assert await run.ctx.tools() == TOOLS
    assert server.methods() == ["tools/list"]


async def test_origin_and_token_only_with_flags() -> None:
    server = tools_server()
    await run_scripted(server)
    assert "origin" not in server.seen[0].headers
    assert "authorization" not in server.seen[0].headers
    server = tools_server()
    run = await run_scripted(server, origin="https://app.example.com", token="sekrit")
    assert run.result.status is Status.PASS
    assert server.seen[0].headers["origin"] == "https://app.example.com"
    assert server.seen[0].headers["authorization"] == "Bearer sekrit"
    # redacted in the transcript
    assert all("sekrit" not in v for _, v in run.transcript.exchanges[0].request_headers)


async def test_empty_tools_list_passes() -> None:
    run = await run_scripted(tools_server(result=list_result([])))
    assert run.result.status is Status.PASS
    assert await run.ctx.tools() == []


async def test_session_id_on_modern_reply_is_not_a_handshake_finding() -> None:
    run = await run_scripted(tools_server(headers={"Mcp-Session-Id": "abc"}))
    assert run.result.status is Status.PASS
    assert run.codes == set()
    assert "Mcp-Session-Id 'abc'" in run.result.details
    assert "session check" in run.result.details


@pytest.mark.parametrize("sse", [False, True])
async def test_pass_sse_notification_and_progress_before_result(sse: bool) -> None:
    extra = (
        {"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info"}},
        {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}},
    )
    server = ScriptedServer(
        routes={
            "tools/list": lambda b, _r: ok(b, list_result(TOOLS), sse=True, extra_messages=extra)
        }
    )
    run = await run_scripted(server)
    assert run.result.status is Status.PASS, run.result
    assert run.codes == set()


# =========================================================================== L1: legacy probe


@pytest.mark.parametrize("sse", [False, True])
async def test_pass_legacy_probes_dual_era(sse: bool) -> None:
    server = ScriptedServer(
        routes={
            "tools/list": lambda b, _r: ok(b, list_result(TOOLS), sse=sse),
            "initialize": legacy_initialize,
        }
    )
    run = await run_scripted(server, legacy_probes=True)
    assert run.result.status is Status.PASS
    assert run.result.summary == "Server does not require `initialize` (also serves legacy clients)"
    assert run.ctx.era is Era.DUAL_ERA
    assert run.codes == {"handshake.dual_era", "handshake.session_id_seen"}
    assert run.finding("handshake.dual_era").evidence == {
        "http_status": 200,
        "media_type": "application/json",
        "connection": "handshake",
        "session_id_seen": True,
        "legacy_protocol_version": "2025-11-25",
    }
    assert "answered with protocolVersion 2025-11-25" in run.result.details
    # exactly two POSTs, never a DELETE, the probe on its own fresh connection
    assert server.methods() == ["tools/list", "initialize"]
    assert {s.request.method for s in server.seen} == {"POST"}
    assert [e.connection for e in run.transcript] == ["handshake", "handshake-legacy"]
    assert run.transcript.exchanges[1].legacy is True
    # the legacy probe is header-less and _meta-less
    probe = server.seen[1]
    assert "mcp-protocol-version" not in probe.headers
    assert "mcp-method" not in probe.headers
    assert "mcp-session-id" not in probe.headers
    assert probe.headers["accept"] == "application/json, text/event-stream"
    assert probe.meta is None
    assert probe.body == {
        "jsonrpc": "2.0",
        "id": LEGACY_PROBE_ID,
        "method": "initialize",
        "params": {
            "protocolVersion": LEGACY_PROBE_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "mcpcheck", "version": __version__},
        },
    }


async def test_legacy_probe_without_session_id_has_no_session_info() -> None:
    server = ScriptedServer(
        routes={
            "tools/list": lambda b, _r: ok(b, list_result(TOOLS)),
            "initialize": lambda b, r: legacy_initialize(b, r, session=None),
        }
    )
    run = await run_scripted(server, legacy_probes=True)
    assert run.ctx.era is Era.DUAL_ERA
    assert run.codes == {"handshake.dual_era"}


async def test_legacy_probe_modern_only_error_names_no_version_warns() -> None:
    server = ScriptedServer(
        routes={
            "tools/list": lambda b, _r: ok(b, list_result(TOOLS)),
            "initialize": lambda b, _r: error_response(
                b, -32020, "mcp-protocol-version header is missing", status=400
            ),
        }
    )
    run = await run_scripted(server, legacy_probes=True)
    assert run.result.status is Status.WARN
    assert run.ctx.era is Era.MODERN_ONLY
    assert run.codes == {"handshake.initialize_error_names_no_version"}
    warning = run.finding("handshake.initialize_error_names_no_version")
    assert warning.level.value == "should"
    assert warning.subject == "initialize"
    assert warning.spec_url == spec_urls.SPEC_BACKCOMPAT_INIT
    assert run.result.summary == "Server does not require `initialize`"
    assert "legacy initialize: rejected (HTTP 400" in run.result.details


@pytest.mark.parametrize(
    "response",
    [
        lambda b: error_response(
            b, -32601, "Method not found: initialize (supported versions: 2026-07-28)", status=404
        ),
        lambda b: error_response(
            b, -32601, "Method not found", status=404, data={"supported": ["2026-07-28"]}
        ),
        lambda _b: httpx2.Response(
            400,
            text="This server speaks MCP 2026-07-28 only",
            headers={"content-type": "text/plain"},
        ),
    ],
)
async def test_legacy_probe_modern_only_error_naming_version_passes(
    response: Callable[[dict[str, Any]], httpx2.Response],
) -> None:
    server = ScriptedServer(
        routes={
            "tools/list": lambda b, _r: ok(b, list_result(TOOLS)),
            "initialize": lambda b, _r: response(b),
        }
    )
    run = await run_scripted(server, legacy_probes=True)
    assert run.result.status is Status.PASS
    assert run.ctx.era is Era.MODERN_ONLY
    assert run.codes == set()


@pytest.mark.parametrize("status", [401, 403, 500, 429])
async def test_legacy_probe_infrastructure_answer_leaves_era_unknown(status: int) -> None:
    server = ScriptedServer(
        routes={
            "tools/list": lambda b, _r: ok(b, list_result(TOOLS)),
            "initialize": lambda _b, _r: httpx2.Response(status),
        }
    )
    run = await run_scripted(server, legacy_probes=True)
    assert run.result.status is Status.PASS
    assert run.ctx.era is Era.UNKNOWN
    assert "legacy initialize: inconclusive" in run.result.details


async def test_legacy_probe_confirms_legacy_only_server() -> None:
    server = ScriptedServer(
        routes={
            "tools/list": lambda _b, _r: httpx2.Response(
                400, json=jsonrpc_error("server-error", -32600, "Bad Request: Missing session ID")
            ),
            "initialize": legacy_initialize,
        }
    )
    run = await run_scripted(server, legacy_probes=True)
    assert run.result.status is Status.FAIL
    assert run.ctx.era is Era.LEGACY_ONLY
    assert run.codes == {"handshake.initialize_required", "handshake.session_id_seen"}
    assert run.result.summary == (
        'Server requires `initialize` (HTTP 400, -32600 "Bad Request: Missing session ID")'
    )
    assert server.methods() == ["tools/list", "initialize"]


async def test_legacy_probe_rejected_after_non_modern_rejection_is_unknown_era() -> None:
    server = ScriptedServer(
        routes={
            "tools/list": lambda b, _r: error_response(b, -32000, "Server error", status=400),
            "initialize": lambda b, _r: error_response(b, -32601, "no (2026-07-28)", status=404),
        }
    )
    run = await run_scripted(server, legacy_probes=True)
    assert run.result.status is Status.FAIL
    assert run.ctx.era is Era.UNKNOWN


# =========================================================================== L1: era failures


@pytest.mark.parametrize("sse", [False, True])
async def test_fail_legacy_stateful_1x_shape(sse: bool) -> None:
    """mcp 1.30 stateful: 400, id "server-error", -32600 "Bad Request: Missing session ID"."""
    status, code, message = LEGACY_SESSION_REJECT
    msg = jsonrpc_error("server-error", code, message)
    server = ScriptedServer(
        routes={
            "tools/list": lambda _b, _r: httpx2.Response(
                status,
                content=sse_body(msg) if sse else None,
                json=None if sse else msg,
                headers={"content-type": "text/event-stream"} if sse else None,
            )
        }
    )
    run = await run_scripted(server)
    assert run.result.status is Status.FAIL
    assert run.codes == {"handshake.initialize_required"}
    finding = run.finding("handshake.initialize_required")
    assert finding.level.value == "must"
    assert finding.subject == "tools/list"
    assert finding.spec_url == spec_urls.SPEC_STATELESSNESS
    assert finding.message == (
        "Server rejected a complete 2026-07-28 request with a non-modern error; "
        "it requires the legacy initialize handshake"
    )
    assert finding.evidence is not None
    assert finding.evidence["fingerprint"] == "legacy_session"
    assert finding.evidence["http_status"] == 400
    assert finding.evidence["rpc_code"] == -32600
    assert finding.evidence["rpc_message"] == "Bad Request: Missing session ID"
    assert finding.evidence["hint"] == "see basic/transports/streamable-http#backward-compatibility"
    assert finding.evidence["connection"] == "handshake"
    assert "expected" in finding.evidence
    assert run.result.summary == (
        'Server requires `initialize` (HTTP 400, -32600 "Bad Request: Missing session ID")'
    )
    assert run.ctx.era is Era.LEGACY_ONLY
    assert run.ctx.protocol_version is None
    assert run.blocked
    assert gate_reason(run.result) == run.result.summary
    assert server.methods() == ["tools/list"]  # no initialize without --legacy-probes


async def test_fail_legacy_version_reject_parses_versions_from_message() -> None:
    message = (
        "Bad Request: Unsupported protocol version (supported versions: "
        "2024-11-05, 2025-03-26, 2025-06-18, 2025-11-25)"
    )
    server = ScriptedServer(
        routes={"tools/list": lambda b, _r: error_response(b, -32600, message, status=400)}
    )
    run = await run_scripted(server)
    assert run.result.status is Status.FAIL
    finding = run.finding("handshake.initialize_required")
    assert finding.evidence is not None
    assert finding.evidence["fingerprint"] == "legacy_version_reject"
    assert finding.evidence["supported_versions_source"] == "message"
    assert run.ctx.server_supported_versions == [
        "2024-11-05",
        "2025-03-26",
        "2025-06-18",
        "2025-11-25",
    ]
    assert run.ctx.era is Era.LEGACY_ONLY
    assert run.blocked


@pytest.mark.parametrize(
    ("status", "code", "message", "fingerprint"),
    [
        (404, -32600, "Session not found", "legacy_session"),
        (200, -32602, "Received request before initialization was complete", "not_initialized"),
        (400, -32602, "Invalid request parameters: not initialized", "not_initialized"),
        (400, -32602, "Invalid params", "non_modern_error"),
        (400, -32000, "Server error", "non_modern_error"),
        (200, -32601, "Method not found", "non_modern_error"),
    ],
)
async def test_fail_non_modern_rejection_fingerprints(
    status: int, code: int, message: str, fingerprint: str
) -> None:
    server = ScriptedServer(
        routes={
            "tools/list": lambda b, _r: error_response(b, code, message, status=status, sse=True)
        }
    )
    run = await run_scripted(server)
    assert run.result.status is Status.FAIL
    assert run.codes == {"handshake.initialize_required"}
    finding = run.finding("handshake.initialize_required")
    assert finding.evidence is not None
    assert finding.evidence["fingerprint"] == fingerprint
    assert run.result.summary == clip_summary(
        f'Server requires `initialize` (HTTP {status}, {code} "{message}")'
    )
    assert run.blocked


@pytest.mark.parametrize(
    ("response", "fingerprint", "what"),
    [
        (httpx2.Response(400), "empty_400", "empty body"),
        (httpx2.Response(400, text="   "), "empty_400", "empty body"),
        (
            httpx2.Response(
                400, text="<h1>Bad Request</h1>", headers={"content-type": "text/html"}
            ),
            "non_modern_error",
            "text/html body",
        ),
        (
            httpx2.Response(400, text="nope", headers={"content-type": "text/plain"}),
            "non_modern_error",
            "text/plain body",
        ),
    ],
)
async def test_fail_400_without_modern_error(
    response: httpx2.Response, fingerprint: str, what: str
) -> None:
    server = ScriptedServer(routes={"tools/list": lambda _b, _r: response})
    run = await run_scripted(server)
    assert run.result.status is Status.FAIL
    assert run.codes == {"handshake.initialize_required"}
    assert run.finding("handshake.initialize_required").evidence == {
        "http_status": 400,
        **(
            {"media_type": wire.classify_content_type(response.headers["content-type"])[0]}
            if "content-type" in response.headers
            else {}
        ),
        "connection": "handshake",
        "fingerprint": fingerprint,
        "expected": (
            'HTTP 200 with a result (resultType "complete"), '
            "or a recognized modern error (-32020/-32021/-32022)"
        ),
        "got": f"HTTP 400, {what}",
        "hint": "see basic/transports/streamable-http#backward-compatibility",
    }
    assert run.result.summary == f"Server requires `initialize` (HTTP 400, {what})"


@pytest.mark.parametrize("status", [429, 402, 408, 418])
async def test_inconclusive_other_4xx_without_body(status: int) -> None:
    server = ScriptedServer(routes={"tools/list": lambda _b, _r: httpx2.Response(status)})
    run = await run_scripted(server)
    assert run.result.status is Status.INCONCLUSIVE
    assert run.codes == set()
    assert run.result.summary.startswith(f"Not evaluated: HTTP {status}")
    assert run.blocked


# =========================================================================== L1: modern errors


@pytest.mark.parametrize("sse", [False, True])
@pytest.mark.parametrize(
    ("data", "extra_codes", "supports"),
    [
        ({"supported": ["2027-01-01"], "requested": "2026-07-28"}, set(), "2027-01-01"),
        (
            {"supported": ["2027-01-01", "2026-07-28"], "requested": "2026-07-28"},
            {"handshake.contradictory_unsupported"},
            "2027-01-01, 2026-07-28",
        ),
        ("nope", {"handshake.unsupported_data_malformed"}, "not stated"),
        ({"supported": ["2027-01-01"]}, {"handshake.unsupported_data_malformed"}, "not stated"),
        (None, {"handshake.unsupported_data_malformed"}, "not stated"),
    ],
)
async def test_fail_unsupported_version(
    sse: bool, data: Any, extra_codes: set[str], supports: str
) -> None:
    server = ScriptedServer(
        routes={
            "tools/list": lambda b, _r: error_response(
                b, -32022, "Unsupported protocol version", status=400, data=data, sse=sse
            )
        }
    )
    run = await run_scripted(server)
    assert run.result.status is Status.FAIL
    assert run.codes == {"handshake.version_unsupported", *extra_codes}
    assert run.result.summary == f"Modern server does not support 2026-07-28 (supports: {supports})"
    finding = run.finding("handshake.version_unsupported")
    assert finding.spec_url == spec_urls.SPEC_PROTOCOL_VERSION_HEADER
    if isinstance(data, dict) and "requested" in data:
        assert run.ctx.server_supported_versions == data["supported"]
    else:
        assert run.ctx.server_supported_versions is None
    assert run.ctx.protocol_version is None
    assert run.ctx.era is Era.UNKNOWN
    assert run.blocked


async def test_fail_unsupported_version_at_wrong_http_status() -> None:
    server = ScriptedServer(
        routes={
            "tools/list": lambda b, _r: error_response(
                b,
                -32022,
                "Unsupported protocol version",
                status=200,
                data={"supported": ["2027-01-01"], "requested": "2026-07-28"},
            )
        }
    )
    run = await run_scripted(server)
    assert run.result.status is Status.FAIL
    assert run.codes == {"handshake.version_unsupported"}
    finding = run.finding("handshake.version_unsupported")
    assert finding.evidence is not None
    assert finding.evidence["http_status"] == 200
    assert finding.evidence["expected_http_status"] == 400
    assert "(HTTP 200 instead of 400)" in finding.message
    assert run.blocked


@pytest.mark.parametrize("sse", [False, True])
async def test_fail_header_mismatch_on_valid_request(sse: bool) -> None:
    server = ScriptedServer(
        routes={
            "tools/list": lambda b, _r: error_response(
                b, -32020, "Header mismatch: mcp-method", status=400, sse=sse
            )
        }
    )
    run = await run_scripted(server)
    assert run.result.status is Status.FAIL
    assert run.codes == {"handshake.header_mismatch_on_valid_request"}
    finding = run.finding("handshake.header_mismatch_on_valid_request")
    assert finding.spec_url == spec_urls.SPEC_SERVER_VALIDATION
    assert finding.evidence is not None
    assert "intermediary" in finding.evidence["hint"]
    assert run.result.summary == (
        "Server rejected a well-formed request with -32020 (intermediary stripping headers?)"
    )
    assert not run.blocked  # era confirmed modern: a shape failure, dependents run


@pytest.mark.parametrize("sse", [False, True])
async def test_inconclusive_missing_capability_does_not_block(sse: bool) -> None:
    server = ScriptedServer(
        routes={
            "tools/list": lambda b, _r: error_response(
                b,
                -32021,
                "Missing required client capability",
                status=400,
                data={"requiredCapabilities": ["sampling", "elicitation"]},
                sse=sse,
            )
        }
    )
    run = await run_scripted(server)
    assert run.result.status is Status.INCONCLUSIVE
    assert run.codes == {"handshake.client_capability_demanded"}
    warning = run.finding("handshake.client_capability_demanded")
    assert warning.level.value == "should"
    assert warning.evidence is not None
    assert warning.evidence["required_capabilities"] == ["sampling", "elicitation"]
    assert run.result.summary == (
        "Not evaluated: server demands a client capability (-32021: sampling, elicitation)"
    )
    assert run.ctx.protocol_version == "2026-07-28"
    assert not run.blocked


# =========================================================================== L1: shape failures


@pytest.mark.parametrize("sse", [False, True])
async def test_fail_legacy_shaped_result(sse: bool) -> None:
    run = await run_scripted(tools_server(sse=sse, result={"tools": TOOLS}))
    assert run.result.status is Status.FAIL
    assert run.codes == {"handshake.legacy_shaped_result", "handshake.server_info_missing"}
    finding = run.finding("handshake.legacy_shaped_result")
    assert finding.spec_url == spec_urls.SPEC_RESULT_RESPONSES
    assert finding.evidence is not None
    assert finding.evidence["got"] == ["tools"]
    assert run.result.summary == (
        "Server answered a 2026-07-28 request with a pre-2026 result (no resultType)"
    )
    assert run.ctx.protocol_version is None
    assert run.ctx.era is Era.UNKNOWN
    assert not run.blocked  # shape failures never block dependents
    assert await run.ctx.tools() == TOOLS  # page 0 still seeded


@pytest.mark.parametrize("result_type", ["input_required", "partial", 7, None])
async def test_fail_unexpected_result_type(result_type: Any) -> None:
    run = await run_scripted(tools_server(result=list_result(TOOLS, resultType=result_type)))
    assert run.result.status is Status.FAIL
    assert run.codes == {"handshake.unexpected_result_type"}
    assert run.result.summary == f"Unexpected resultType {result_type!r} on `tools/list`"
    assert run.ctx.protocol_version == "2026-07-28"
    assert not run.blocked


@pytest.mark.parametrize("tools", [{"a": 1}, "nope", None])
async def test_fail_tools_not_list(tools: Any) -> None:
    result = list_result([])
    result["tools"] = tools
    run = await run_scripted(tools_server(result=result))
    assert run.result.status is Status.FAIL
    assert run.codes == {"handshake.tools_not_list"}
    assert run.finding("handshake.tools_not_list").spec_url == spec_urls.SPEC_TOOLS_CAPABILITIES
    assert run.result.summary.startswith("result.tools is")


@pytest.mark.parametrize("sse", [False, True])
@pytest.mark.parametrize(
    ("meta", "got", "server_info"),
    [
        (None, "absent", None),
        ({}, "absent", None),
        (
            {"io.modelcontextprotocol/serverInfo": "x"},
            "io.modelcontextprotocol/serverInfo is str, not an object",
            None,
        ),
        (
            {"io.modelcontextprotocol/serverInfo": {"name": "only-name"}},
            "io.modelcontextprotocol/serverInfo without string name/version",
            {"name": "only-name"},
        ),
        (
            {"io.modelcontextprotocol/serverInfo": {"name": "n", "version": 1}},
            "io.modelcontextprotocol/serverInfo without string name/version",
            {"name": "n", "version": 1},
        ),
    ],
)
async def test_warn_serverinfo_missing(
    sse: bool, meta: Any, got: str, server_info: dict[str, Any] | None
) -> None:
    result = list_result(TOOLS)
    if meta is None:
        del result["_meta"]
    else:
        result["_meta"] = meta
    run = await run_scripted(tools_server(sse=sse, result=result))
    assert run.result.status is Status.WARN
    assert run.codes == {"handshake.server_info_missing"}
    warning = run.finding("handshake.server_info_missing")
    assert warning.level.value == "should"
    assert warning.spec_url == spec_urls.SPEC_META
    assert warning.evidence is not None
    assert warning.evidence["got"] == got
    assert run.ctx.server_info == server_info
    assert run.result.summary == "Server does not require `initialize`"
    assert not run.blocked


@pytest.mark.parametrize("sse", [False, True])
@pytest.mark.parametrize("wrong_id", ["other", 2, "1", None])
async def test_fail_wrong_or_null_id(sse: bool, wrong_id: Any) -> None:
    msg = {"jsonrpc": "2.0", "id": wrong_id, "result": list_result(TOOLS)}
    server = ScriptedServer(
        routes={
            "tools/list": lambda _b, _r: httpx2.Response(
                200,
                content=sse_body(msg) if sse else None,
                json=None if sse else msg,
                headers={"content-type": "text/event-stream"} if sse else None,
            )
        }
    )
    run = await run_scripted(server)
    assert run.result.status is Status.FAIL
    assert run.codes == {"handshake.id_mismatch"}
    finding = run.finding("handshake.id_mismatch")
    assert finding.spec_url == spec_urls.SPEC_RESULT_RESPONSES
    assert finding.evidence is not None
    assert finding.evidence["got"] == wrong_id
    assert finding.evidence["expected"] == 1
    assert run.result.summary.startswith(f"Response id {wrong_id!r} differs from the request id 1")
    assert run.ctx.protocol_version == "2026-07-28"  # the result itself is 2026-shaped
    assert not run.blocked


@pytest.mark.parametrize("result", ["complete", 3, [], None])
async def test_fail_result_not_an_object(result: Any) -> None:
    server = ScriptedServer(
        routes={
            "tools/list": lambda b, _r: httpx2.Response(
                200, json={"jsonrpc": "2.0", "id": b["id"], "result": result}
            )
        }
    )
    run = await run_scripted(server)
    assert run.result.status is Status.FAIL
    assert run.codes == {"handshake.not_jsonrpc"}
    assert run.result.summary == "Non-MCP response to `tools/list` (result is not an object)"


async def test_fail_result_and_error_together() -> None:
    server = ScriptedServer(
        routes={
            "tools/list": lambda b, _r: httpx2.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": b["id"],
                    "result": list_result(TOOLS),
                    "error": {"code": -32603, "message": "also failed"},
                },
            )
        }
    )
    run = await run_scripted(server)
    assert run.result.status is Status.FAIL
    assert run.codes == {"handshake.not_jsonrpc"}
    assert run.result.summary == "Non-MCP response to `tools/list` (both result and error)"
    assert run.blocked  # no JSON-RPC response to judge: dependents would only repeat the noise
    assert gate_reason(run.result) == run.result.summary


@pytest.mark.parametrize(
    ("response", "summary"),
    [
        (
            httpx2.Response(200, text="<html>hi</html>", headers={"content-type": "text/html"}),
            "Non-MCP response to `tools/list` (HTTP 200 text/html)",
        ),
        (
            httpx2.Response(
                200, content=b"{not json", headers={"content-type": "application/json"}
            ),
            "Non-MCP response to `tools/list` (HTTP 200 application/json)",
        ),
        (
            httpx2.Response(200, content=b"[]", headers={"content-type": "application/json"}),
            "Non-MCP response to `tools/list` (HTTP 200 application/json)",
        ),
        (
            httpx2.Response(
                200,
                content=b'{"jsonrpc":"2.0","id":1,"result":{}}',
                headers={"content-type": "application/json; charset=latin-1"},
            ),
            "Non-MCP response to `tools/list` (HTTP 200 application/json)",
        ),
        (httpx2.Response(202), "Non-MCP response to `tools/list` (HTTP 202 no content-type)"),
        (
            httpx2.Response(200, content=b'{"jsonrpc":"2.0","id":1,"result":{}}'),
            "Non-MCP response to `tools/list` (HTTP 200 no content-type)",
        ),
        (
            httpx2.Response(
                200, content=b": ping\n\n", headers={"content-type": "text/event-stream"}
            ),
            "Non-MCP response to `tools/list` (HTTP 200 text/event-stream)",
        ),
        (
            httpx2.Response(
                200,
                content=sse_body({"jsonrpc": "2.0", "method": "notifications/message"}),
                headers={"content-type": "text/event-stream"},
            ),
            "Non-MCP response to `tools/list` (HTTP 200 text/event-stream)",
        ),
    ],
)
async def test_fail_not_jsonrpc(response: httpx2.Response, summary: str) -> None:
    server = ScriptedServer(routes={"tools/list": lambda _b, _r: response})
    run = await run_scripted(server)
    assert run.result.status is Status.FAIL
    assert run.codes == {"handshake.not_jsonrpc"}
    finding = run.finding("handshake.not_jsonrpc")
    assert finding.spec_url == spec_urls.SPEC_SENDING_MESSAGES
    assert run.result.summary == summary
    assert run.ctx.protocol_version is None
    # a non-JSON-RPC 2xx fails the handshake (exit 1) and gates the dependents: a web page or
    # REST API must not be reported as "header mismatch accepted" or "cache hints valid"
    assert run.blocked
    assert gate_reason(run.result) == summary


async def test_fail_sse_server_request_on_stream() -> None:
    server_request = {"jsonrpc": "2.0", "id": "srv-1", "method": "roots/list"}
    server = ScriptedServer(
        routes={
            "tools/list": lambda b, _r: ok(
                b, list_result(TOOLS), sse=True, extra_messages=(server_request,)
            )
        }
    )
    run = await run_scripted(server)
    assert run.result.status is Status.FAIL
    assert run.codes == {"handshake.server_request_on_stream"}
    finding = run.finding("handshake.server_request_on_stream")
    assert finding.spec_url == spec_urls.SPEC_RECEIVING_MESSAGES
    assert finding.evidence is not None
    assert finding.evidence["got"] == "roots/list"
    assert "roots/list" in finding.message
    assert not run.blocked


# =========================================================================== L1: discover fallback


def discover_result(**overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "scripted", "version": "0.0.1"}},
        "resultType": "complete",
        "supportedVersions": ["2026-07-28"],
        "capabilities": {"prompts": {}},
        "ttlMs": 60000,
        "cacheScope": "public",
    }
    result.update(overrides)
    return result


@pytest.mark.parametrize("sse", [False, True])
async def test_fallback_discover_no_tools(sse: bool) -> None:
    server = ScriptedServer(
        routes={"server/discover": lambda b, _r: ok(b, discover_result(), sse=sse)}
    )
    run = await run_scripted(server)
    assert run.result.status is Status.PASS
    assert run.codes == set()
    assert run.result.summary == (
        "Server does not require `initialize` (no tools; verified via server/discover)"
    )
    assert server.methods() == ["tools/list", "server/discover"]
    assert [e.connection for e in run.transcript] == ["handshake", "handshake"]
    assert run.ctx.protocol_version == "2026-07-28"
    assert run.ctx.server_supported_versions == ["2026-07-28"]
    assert run.ctx.server_info == {"name": "scripted", "version": "0.0.1"}
    assert "falling back to server/discover" in run.result.details
    assert not run.blocked
    # page 0 is the 404: dependents see "not implemented" without another request
    assert await run.ctx.tools() is None
    assert server.methods() == ["tools/list", "server/discover"]


async def test_fallback_discover_with_legacy_probe_summary() -> None:
    server = ScriptedServer(
        routes={
            "server/discover": lambda b, _r: ok(b, discover_result()),
            "initialize": legacy_initialize,
        }
    )
    run = await run_scripted(server, legacy_probes=True)
    assert run.result.status is Status.PASS
    assert run.result.summary == (
        "Server does not require `initialize` "
        "(no tools; via server/discover; also serves legacy clients)"
    )
    assert run.ctx.era is Era.DUAL_ERA


@pytest.mark.parametrize("sse", [False, True])
async def test_fail_discover_missing(sse: bool) -> None:
    server = ScriptedServer(
        routes={
            "server/discover": lambda b, _r: error_response(
                b, -32601, "Method not found", status=404, data="server/discover", sse=sse
            )
        }
    )
    run = await run_scripted(server)
    assert run.result.status is Status.FAIL
    assert run.codes == {"handshake.discover_missing"}
    finding = run.finding("handshake.discover_missing")
    assert finding.subject == "server/discover"
    assert finding.spec_url == spec_urls.SPEC_DISCOVER
    assert run.result.summary == (
        "Neither `tools/list` nor `server/discover` is implemented (404 -32601)"
    )
    assert run.ctx.era is Era.UNKNOWN
    assert not run.blocked  # not an era failure: dependents run and report their own 404s


@pytest.mark.parametrize("tools_capability", [{}, {"listChanged": True}])
async def test_fail_tools_declared_but_unimplemented(tools_capability: dict[str, Any]) -> None:
    server = ScriptedServer(
        routes={
            "server/discover": lambda b, _r: ok(
                b, discover_result(capabilities={"tools": tools_capability, "prompts": {}})
            )
        }
    )
    run = await run_scripted(server)
    assert run.result.status is Status.FAIL
    assert run.codes == {"handshake.tools_declared_but_unimplemented"}
    finding = run.finding("handshake.tools_declared_but_unimplemented")
    assert finding.subject == "tools/list"
    assert finding.spec_url == spec_urls.SPEC_TOOLS_CAPABILITIES
    assert finding.evidence is not None
    assert finding.evidence["http_status"] == 404
    assert finding.evidence["rpc_code"] == -32601
    assert finding.evidence["declared_capabilities"] == ["prompts", "tools"]
    assert run.ctx.protocol_version == "2026-07-28"
    assert not run.blocked


async def test_fallback_discover_legacy_shaped() -> None:
    result = discover_result()
    del result["resultType"]
    server = ScriptedServer(routes={"server/discover": lambda b, _r: ok(b, result)})
    run = await run_scripted(server)
    assert run.result.status is Status.FAIL
    assert run.codes == {"handshake.legacy_shaped_result"}
    assert run.finding("handshake.legacy_shaped_result").subject == "server/discover"
    assert run.ctx.protocol_version is None


async def test_fallback_discover_modern_error_is_judged_like_the_primary() -> None:
    server = ScriptedServer(
        routes={
            "server/discover": lambda b, _r: error_response(
                b,
                -32022,
                "Unsupported protocol version",
                status=400,
                data={"supported": ["2027-01-01"], "requested": "2026-07-28"},
            )
        }
    )
    run = await run_scripted(server)
    assert run.result.status is Status.FAIL
    assert run.codes == {"handshake.version_unsupported"}
    assert run.finding("handshake.version_unsupported").subject == "server/discover"
    assert run.blocked


async def test_fallback_discover_omitting_2026_is_noted_only() -> None:
    server = ScriptedServer(
        routes={
            "server/discover": lambda b, _r: ok(
                b, discover_result(supportedVersions=["2027-01-01"])
            )
        }
    )
    run = await run_scripted(server)
    assert run.result.status is Status.PASS
    assert run.ctx.server_supported_versions == ["2027-01-01"]
    assert "omit 2026-07-28" in run.result.details


# =========================================================================== L1: infrastructure


@pytest.mark.parametrize(
    ("response", "summary"),
    [
        (
            httpx2.Response(401, headers={"www-authenticate": "Bearer"}),
            "Not evaluated: HTTP 401 (pass --token)",
        ),
        (httpx2.Response(407), "Not evaluated: HTTP 407 (pass --token)"),
        (
            httpx2.Response(403, text="Invalid Origin header"),
            "Not evaluated: HTTP 403 (try --origin)",
        ),
        (
            httpx2.Response(404, text="<h1>Not Found</h1>", headers={"content-type": "text/html"}),
            "Not evaluated: no MCP endpoint at URL (HTTP 404 text/html)",
        ),
        (
            httpx2.Response(405, headers={"allow": "GET"}),
            "Not evaluated: no MCP endpoint at URL (HTTP 405 (no content-type))",
        ),
        (
            httpx2.Response(302, headers={"location": "/mcp-redirected"}),
            "Not evaluated: redirects (302) to http://127.0.0.1:1/mcp-redirected",
        ),
        (
            httpx2.Response(307, headers={"location": "https://other.example.com/mcp"}),
            "Not evaluated: redirects (307) to https://other.example.com/mcp",
        ),
    ],
)
async def test_inconclusive_infrastructure_blocks_dependents(
    response: httpx2.Response, summary: str
) -> None:
    server = ScriptedServer(routes={"tools/list": lambda _b, _r: response})
    run = await run_scripted(server, token="sekrit" if response.status_code == 307 else None)
    assert run.result.status is Status.INCONCLUSIVE
    assert run.codes == set()
    assert run.result.summary == summary
    assert run.blocked
    assert gate_reason(run.result) == summary
    assert len(server.seen) == 1  # the cross-origin 307 is never followed: the token stays home
    assert run.ctx.era is Era.UNKNOWN


async def test_inconclusive_401_with_token_says_token_rejected() -> None:
    server = ScriptedServer(routes={"tools/list": lambda _b, _r: httpx2.Response(401)})
    run = await run_scripted(server, token="sekrit")
    assert run.result.summary == "Not evaluated: HTTP 401 (token rejected)"


async def test_forbidden_with_origin_sent() -> None:
    server = ScriptedServer(
        routes={"tools/list": lambda b, _r: error_response(b, -32600, "Forbidden", status=403)}
    )
    run = await run_scripted(server, origin="https://app.example.com")
    assert run.result.status is Status.INCONCLUSIVE
    assert run.result.summary == "Not evaluated: HTTP 403 (Origin rejected or insufficient scope)"


async def test_same_origin_307_is_followed() -> None:
    server = ScriptedServer(
        routes={"tools/list": lambda b, _r: ok(b, list_result(TOOLS))},
    )
    original = server.handler

    async def redirecting(req: httpx2.Request) -> httpx2.Response:
        if req.url.path == "/mcp":
            return httpx2.Response(307, headers={"location": "/mcp-redirected"})
        return await original(req)

    server.handler = redirecting  # type: ignore[method-assign]
    run = await run_scripted(server)
    assert run.result.status is Status.PASS
    assert server.seen[0].headers["mcp-method"] == "tools/list"


@pytest.mark.slow
async def test_inconclusive_500_after_one_retry_two_posts() -> None:
    server = ScriptedServer(routes={"tools/list": lambda _b, _r: httpx2.Response(503)})
    t0 = time.perf_counter()
    run = await run_scripted(server)
    elapsed = time.perf_counter() - t0
    assert run.result.status is Status.INCONCLUSIVE
    assert run.result.summary == "Not evaluated: HTTP 503"
    assert server.methods() == ["tools/list", "tools/list"]
    assert run.result.requests == 2
    assert 1.0 <= elapsed < 3.0
    assert "HTTP 503 and then HTTP 503" in run.result.details
    assert run.blocked


@pytest.mark.slow
async def test_5xx_then_ok_passes_with_two_requests() -> None:
    calls = itertools.count()

    def flaky(body: dict[str, Any], _req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(500) if next(calls) == 0 else ok(body, list_result(TOOLS))

    server = ScriptedServer(routes={"tools/list": flaky})
    run = await run_scripted(server)
    assert run.result.status is Status.PASS
    assert run.result.requests == 2
    assert server.methods() == ["tools/list", "tools/list"]


async def test_inconclusive_huge_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mcpcheck.client.MAX_BODY_BYTES", 2048)
    big = list_result([{"name": f"t{i}", "inputSchema": {"type": "object"}} for i in range(200)])
    run = await run_scripted(tools_server(result=big))
    assert run.result.status is Status.INCONCLUSIVE
    assert run.result.summary == "Not evaluated: body exceeds the 0 MiB cap"
    assert run.blocked


async def test_inconclusive_transport_error() -> None:
    def boom(_req: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadError("connection reset")

    transport = httpx2.MockTransport(boom)
    run = await run_with(lambda: transport, ClientConfig(url=URL))
    assert run.result.status is Status.INCONCLUSIVE
    assert run.result.summary == "Not evaluated: ReadError: connection reset"
    assert run.blocked


# =========================================================================== L2: reference server


@pytest.mark.parametrize("json_response", [False, True])
async def test_pass_reference(json_response: bool) -> None:
    run = await run_reference(json_response=json_response)
    assert run.result.status is Status.PASS, run.result
    assert run.codes == set()
    assert run.result.summary == "Server does not require `initialize`"
    assert run.ctx.era is Era.UNKNOWN
    assert run.ctx.protocol_version == "2026-07-28"
    assert run.ctx.server_info == {
        "name": reference_server.SERVER_NAME,
        "version": reference_server.SERVER_VERSION,
    }
    assert run.result.requests == 1
    exchange = run.transcript.exchanges[0]
    assert exchange.connection == "handshake"
    assert exchange.status == 200
    tools = await run.ctx.tools()
    assert tools is not None
    assert [t["name"] for t in tools] == list(reference_server.TOOL_NAMES)
    assert len(run.transcript) == 1  # page 0 came from the handshake


@pytest.mark.parametrize("stateless_http", [True, False])
async def test_pass_reference_legacy_probes_dual_era(stateless_http: bool) -> None:
    run = await run_reference(stateless_http=stateless_http, legacy_probes=True)
    assert run.result.status is Status.PASS, run.result
    assert run.ctx.era is Era.DUAL_ERA
    assert run.result.summary == "Server does not require `initialize` (also serves legacy clients)"
    expected = {"handshake.dual_era"}
    if not stateless_http:
        expected.add("handshake.session_id_seen")  # --stateful mints a legacy session
    assert run.codes == expected
    assert run.evidence("handshake.dual_era")["legacy_protocol_version"] == "2025-11-25"
    assert [e.verb for e in run.transcript] == ["POST", "POST"]
    assert [e.rpc_method for e in run.transcript] == ["tools/list", "initialize"]
    assert run.result.requests == 2


async def test_reference_bare_passes_via_discover() -> None:
    run = await run_reference(bare=True)
    assert run.result.status is Status.PASS, run.result
    assert run.result.summary == (
        "Server does not require `initialize` (no tools; verified via server/discover)"
    )
    assert run.ctx.server_supported_versions == ["2026-07-28"]
    assert [e.rpc_method for e in run.transcript] == ["tools/list", "server/discover"]


async def test_reference_empty_passes_with_empty_tools() -> None:
    run = await run_reference(empty=True)
    assert run.result.status is Status.PASS
    assert await run.ctx.tools() == []


async def test_reference_require_token() -> None:
    run = await run_reference(require_token="t0k3n")
    assert run.result.status is Status.INCONCLUSIVE
    assert run.result.summary == "Not evaluated: HTTP 401 (pass --token)"
    assert run.blocked
    run = await run_reference(require_token="t0k3n", token="t0k3n")
    assert run.result.status is Status.PASS
    run = await run_reference(require_token="t0k3n", token="wrong")
    assert run.result.summary == "Not evaluated: HTTP 401 (token rejected)"


async def test_reference_evil_origin_is_inconclusive_not_fail() -> None:
    run = await run_reference(origin=wire.EVIL_ORIGIN)
    assert run.result.status is Status.INCONCLUSIVE
    assert run.result.summary == "Not evaluated: HTTP 403 (Origin rejected or insufficient scope)"


# =========================================================================== L2: misbehaving server


async def test_misbehaving_default_passes() -> None:
    run = await run_misbehaving(Toggles())
    assert run.result.status is Status.PASS, run.result
    assert run.codes == set()
    assert run.ctx.server_info == misbehaving_server.SERVER_INFO


async def test_misbehaving_sse_passes() -> None:
    run = await run_misbehaving(Toggles(sse=True))
    assert run.result.status is Status.PASS, run.result
    assert run.transcript.exchanges[0].response_headers


async def test_row_require_initialize() -> None:
    """§10.4: require_initialize → handshake.initialize_required fail/must, dependents blocked."""
    run = await run_misbehaving(Toggles(require_initialize=True))
    assert run.result.status is Status.FAIL
    assert run.codes == {"handshake.initialize_required"}
    finding = run.finding("handshake.initialize_required")
    assert finding.level.value == "must"
    assert finding.evidence is not None
    assert finding.evidence["fingerprint"] == "legacy_session"
    assert finding.evidence["http_status"] == 400
    assert finding.evidence["rpc_code"] == -32600
    assert run.result.summary == (
        'Server requires `initialize` (HTTP 400, -32600 "Bad Request: Missing session ID")'
    )
    assert run.ctx.era is Era.LEGACY_ONLY
    assert run.blocked


async def test_row_require_initialize_with_legacy_probe() -> None:
    run = await run_misbehaving(Toggles(require_initialize=True), legacy_probes=True)
    assert run.result.status is Status.FAIL
    assert run.ctx.era is Era.LEGACY_ONLY
    assert run.codes == {"handshake.initialize_required", "handshake.session_id_seen"}
    assert run.evidence("handshake.session_id_seen")["session_id"] == SESSION_ID
    assert [e.rpc_method for e in run.transcript] == ["tools/list", "initialize"]


async def test_row_legacy_shape() -> None:
    """§10.4: legacy_shape → handshake.legacy_shaped_result fail/must."""
    run = await run_misbehaving(Toggles(legacy_shape=True))
    assert run.result.status is Status.FAIL
    assert "handshake.legacy_shaped_result" in run.codes
    assert run.finding("handshake.legacy_shaped_result").level.value == "must"
    assert not run.blocked
    tools = await run.ctx.tools()
    assert tools is not None
    assert len(tools) == 2


async def test_require_session_id_is_also_initialize_required() -> None:
    run = await run_misbehaving(Toggles(require_session_id=True))
    assert run.result.status is Status.FAIL
    assert run.codes == {"handshake.initialize_required"}
    assert run.evidence("handshake.initialize_required")["fingerprint"] == "legacy_session"


async def test_serve_headerless_as_legacy_is_dual_era_pass() -> None:
    run = await run_misbehaving(Toggles(serve_headerless_as_legacy=True))
    assert run.result.status is Status.PASS
    assert run.ctx.era is Era.UNKNOWN
    run = await run_misbehaving(Toggles(serve_headerless_as_legacy=True), legacy_probes=True)
    assert run.result.status is Status.PASS
    assert run.ctx.era is Era.DUAL_ERA
    assert run.codes == {"handshake.dual_era"}


async def test_modern_only_misbehaving_with_legacy_probe_warns() -> None:
    run = await run_misbehaving(Toggles(), legacy_probes=True)
    assert run.result.status is Status.WARN
    assert run.ctx.era is Era.MODERN_ONLY
    assert run.codes == {"handshake.initialize_error_names_no_version"}


async def test_mint_session_id_is_not_a_handshake_finding() -> None:
    run = await run_misbehaving(Toggles(mint_session_id=True))
    assert run.result.status is Status.PASS
    assert run.codes == set()
    assert SESSION_ID in run.result.details


@pytest.mark.parametrize(
    ("variant", "status", "codes"),
    [
        ("wrong-id", Status.FAIL, {"handshake.id_mismatch"}),
        ("null-id", Status.FAIL, {"handshake.id_mismatch"}),
        ("jsonrpc-version-wrong", Status.PASS, set()),
        ("result-and-error", Status.FAIL, {"handshake.not_jsonrpc"}),
        ("html-200", Status.FAIL, {"handshake.not_jsonrpc"}),
        ("json-no-content-type", Status.FAIL, {"handshake.not_jsonrpc"}),
        ("json-array", Status.FAIL, {"handshake.not_jsonrpc"}),
        ("status-202", Status.FAIL, {"handshake.not_jsonrpc"}),
        ("sse-empty", Status.FAIL, {"handshake.not_jsonrpc"}),
        ("sse-notification-message", Status.PASS, set()),
        ("sse-progress-then-result", Status.PASS, set()),
        ("sse-two-responses", Status.PASS, set()),
        ("sse-server-request", Status.FAIL, {"handshake.server_request_on_stream"}),
        ("error-32020", Status.FAIL, {"handshake.header_mismatch_on_valid_request"}),
        ("error-32021", Status.INCONCLUSIVE, {"handshake.client_capability_demanded"}),
        ("error-32022", Status.FAIL, {"handshake.version_unsupported"}),
        (
            "error-32022-contradiction",
            Status.FAIL,
            {"handshake.version_unsupported", "handshake.contradictory_unsupported"},
        ),
        (
            "error-32022-bad-data",
            Status.FAIL,
            {"handshake.version_unsupported", "handshake.unsupported_data_malformed"},
        ),
        ("error-32022-wrong-status", Status.FAIL, {"handshake.version_unsupported"}),
        ("error-32602", Status.FAIL, {"handshake.initialize_required"}),
        ("error-32042", Status.FAIL, {"handshake.initialize_required"}),
        ("error-32000", Status.FAIL, {"handshake.initialize_required"}),
        ("legacy-version-reject", Status.FAIL, {"handshake.initialize_required"}),
    ],
)
async def test_wire_variants(variant: str, status: Status, codes: set[str]) -> None:
    run = await run_misbehaving(Toggles(discover_variant=variant))
    assert run.result.status is status, run.result
    assert run.codes == codes
    era_fail = codes & {"handshake.initialize_required", "handshake.version_unsupported"}
    assert run.blocked is (bool(era_fail) or codes == {"handshake.not_jsonrpc"})
    if variant == "legacy-version-reject":
        assert run.ctx.server_supported_versions == [
            "2024-11-05",
            "2025-03-26",
            "2025-06-18",
            "2025-11-25",
        ]
    if variant == "error-32021":
        assert run.evidence("handshake.client_capability_demanded")["required_capabilities"] == [
            "sampling"
        ]


async def test_scenario_no_tools_passes_via_discover() -> None:
    run = await run_misbehaving(Toggles(pages=(), discover_variant="caps-no-tools"))
    assert run.result.status is Status.PASS, run.result
    assert run.result.summary == (
        "Server does not require `initialize` (no tools; verified via server/discover)"
    )
    assert run.ctx.server_supported_versions == ["2026-07-28"]


async def test_scenario_nothing_fails_discover_missing() -> None:
    run = await run_misbehaving(Toggles(pages=(), no_discover=True))
    assert run.result.status is Status.FAIL
    assert run.codes == {"handshake.discover_missing"}
    assert not run.blocked


async def test_scenario_tools_declared_404() -> None:
    run = await run_misbehaving(Toggles(pages=()))
    assert run.result.status is Status.FAIL
    assert run.codes == {"handshake.tools_declared_but_unimplemented"}


@pytest.mark.parametrize(
    ("toggles", "status", "codes", "summary"),
    [
        (Toggles(status=401), Status.INCONCLUSIVE, set(), "Not evaluated: HTTP 401 (pass --token)"),
        (
            Toggles(status=403, body=b"nope"),
            Status.INCONCLUSIVE,
            set(),
            "Not evaluated: HTTP 403 (try --origin)",
        ),
        (
            Toggles(status=404, content_type="text/html", body=b"<h1>x</h1>"),
            Status.INCONCLUSIVE,
            set(),
            "Not evaluated: no MCP endpoint at URL (HTTP 404 text/html)",
        ),
        (
            Toggles(status=302),
            Status.INCONCLUSIVE,
            set(),
            "Not evaluated: redirects (302) to http://127.0.0.1:8000/mcp-redirected",
        ),
        (
            Toggles(status=202),
            Status.FAIL,
            {"handshake.not_jsonrpc"},
            "Non-MCP response to `tools/list` (HTTP 202 no content-type)",
        ),
        (
            Toggles(status=400, body=b""),
            Status.FAIL,
            {"handshake.initialize_required"},
            "Server requires `initialize` (HTTP 400, empty body)",
        ),
        (
            Toggles(status=200, content_type="application/json", body=b"{not json"),
            Status.FAIL,
            {"handshake.not_jsonrpc"},
            "Non-MCP response to `tools/list` (HTTP 200 application/json)",
        ),
    ],
)
async def test_raw_overrides(
    toggles: Toggles, status: Status, codes: set[str], summary: str
) -> None:
    run = await run_misbehaving(toggles)
    assert run.result.status is status, run.result
    assert run.codes == codes
    assert run.result.summary == summary


@pytest.mark.slow
async def test_raw_override_500_retries_once() -> None:
    run = await run_misbehaving(Toggles(status=500))
    assert run.result.status is Status.INCONCLUSIVE
    assert run.result.summary == "Not evaluated: HTTP 500"
    assert run.result.requests == 2
    assert run.blocked


# =========================================================================== L3: TCP hazards


@pytest.mark.integration
async def test_tcp_reference_pass_and_dependent_runs(reference_url: str) -> None:
    report = await run_tcp(reference_url)
    handshake, dependent = report.checks
    assert handshake.status is Status.PASS, handshake
    assert handshake.requests == 1
    assert dependent.status is Status.PASS
    assert dependent.summary.startswith("ran; page0 tools=")
    assert report.era is Era.UNKNOWN
    assert report.protocol_version == "2026-07-28"
    assert report.server_info == {
        "name": reference_server.SERVER_NAME,
        "version": reference_server.SERVER_VERSION,
    }
    assert report.summary.exit_code == 0


@pytest.mark.integration
async def test_tcp_require_initialize_gates_dependents(
    misbehaving_url: Callable[[Toggles], str],
) -> None:
    url = misbehaving_url(Toggles(require_initialize=True))
    report = await run_tcp(url)
    handshake, dependent = report.checks
    assert handshake.status is Status.FAIL
    assert handshake.failures[0].code == "handshake.initialize_required"
    assert dependent.status is Status.INCONCLUSIVE
    assert dependent.info[0].code == "runner.not_run"
    assert dependent.summary == f"Not run: {handshake.summary}"
    assert report.era is Era.LEGACY_ONLY
    assert report.summary.exit_code == 1
    assert verbs_and_methods(fetch_request_log(url)) == [("POST", "tools/list")]


@pytest.mark.integration
async def test_tcp_legacy_probe_request_log_fresh_connections(
    misbehaving_url: Callable[[Toggles], str],
) -> None:
    url = misbehaving_url(Toggles(serve_headerless_as_legacy=True))
    report = await run_tcp(url, legacy_probes=True)
    handshake = report.checks[0]
    assert handshake.status is Status.PASS, handshake
    assert report.era is Era.DUAL_ERA
    log = fetch_request_log(url)
    assert verbs_and_methods(log) == [("POST", "tools/list"), ("POST", "initialize")]
    assert "DELETE" not in {e["verb"] for e in log}
    assert log[0]["client_port"] != log[1]["client_port"]  # two fresh connections
    assert "mcp-protocol-version" not in log[1]["headers"]
    assert "mcp-method" not in log[1]["headers"]
    assert log[0]["headers"]["mcp-protocol-version"] == "2026-07-28"


@pytest.mark.integration
async def test_tcp_origin_header_only_with_flag(
    misbehaving_url: Callable[[Toggles], str],
) -> None:
    url = misbehaving_url(Toggles())
    await run_tcp(url)
    log = fetch_request_log(url)
    assert "origin" not in log[0]["headers"]
    origin = wire.derive_origin(url)
    report = await run_tcp(url, origin=origin)
    assert report.checks[0].status is Status.PASS
    log = fetch_request_log(url)
    assert log[-1]["headers"]["origin"] == origin


@pytest.mark.integration
@pytest.mark.slow
async def test_tcp_timeout_budget(misbehaving_url: Callable[[Toggles], str]) -> None:
    url = misbehaving_url(Toggles(slow_seconds=5.0))
    t0 = time.perf_counter()
    report = await run_tcp(url, timeout=1.0)
    elapsed = time.perf_counter() - t0
    handshake, dependent = report.checks
    assert handshake.status is Status.INCONCLUSIVE
    assert handshake.summary == "Not evaluated: request exceeded the 1s total budget"
    assert dependent.status is Status.INCONCLUSIVE
    assert elapsed < 3.0
    assert report.summary.exit_code == 0


@pytest.mark.integration
@pytest.mark.slow
async def test_tcp_endless_sse_bounded_by_budget(
    misbehaving_url: Callable[[Toggles], str],
) -> None:
    url = misbehaving_url(Toggles(endless_sse=True))
    t0 = time.perf_counter()
    report = await run_tcp(url, timeout=1.0)
    elapsed = time.perf_counter() - t0
    assert report.checks[0].status is Status.INCONCLUSIVE
    assert "total budget" in report.checks[0].summary
    assert elapsed < 3.0


@pytest.mark.integration
@pytest.mark.slow
async def test_tcp_warn_sse_lingers(misbehaving_url: Callable[[Toggles], str]) -> None:
    url = misbehaving_url(Toggles(discover_variant="sse-not-terminated"))
    t0 = time.perf_counter()
    report = await run_tcp(url)
    elapsed = time.perf_counter() - t0
    handshake = report.checks[0]
    assert handshake.status is Status.WARN, handshake
    assert [w.code for w in handshake.warnings] == ["handshake.stream_not_terminated"]
    assert handshake.warnings[0].level.value == "should"
    assert elapsed < 5.0  # the 2 s termination grace, not the 60 s stream
    assert report.checks[1].status is Status.PASS  # a SHOULD does not block dependents


@pytest.mark.integration
async def test_tcp_connect_refused_is_error_exit_2() -> None:
    url = f"http://127.0.0.1:{free_port()}/mcp"
    t0 = time.perf_counter()
    report = await run_tcp(url)
    elapsed = time.perf_counter() - t0
    handshake, dependent = report.checks
    assert handshake.status is Status.ERROR
    assert handshake.error is not None
    assert handshake.error.type == "ServerUnreachable"
    assert "ConnectError" in handshake.error.message
    assert dependent.status is Status.ERROR
    assert report.summary.exit_code == 2
    assert elapsed < 2.0


@pytest.mark.integration
async def test_tcp_strict_exit_codes(misbehaving_url: Callable[[Toggles], str]) -> None:
    url = misbehaving_url(Toggles(status=401))
    report = await run_tcp(url)
    assert report.summary.exit_code == 0
    assert "--token" in report.checks[0].summary
    report = await run_tcp(url, strict=True)
    assert report.summary.exit_code == 1


# --------------------------------------------------------------------------- -32021 shape MUSTs (B:389-394)


async def test_capability_demand_without_data_at_http_200_reports_both_musts() -> None:
    server = ScriptedServer(
        routes={
            "tools/list": lambda b, _r: error_response(
                b, -32021, "Missing required client capability", status=200
            )
        }
    )
    run = await run_scripted(server)
    # the verdict about the server's tools cannot be reached, but the error itself is judged
    assert run.result.status is Status.FAIL
    assert run.codes == {
        "handshake.client_capability_demanded",
        "handshake.capability_error_malformed",
        "handshake.wrong_status_for_32021",
    }
    malformed = run.finding("handshake.capability_error_malformed")
    assert malformed.level.value == "must"
    assert "absent or null" in malformed.message
    assert (
        run.evidence("handshake.capability_error_malformed")["expected"] == '["<capability>", ...]'
    )
    assert run.evidence("handshake.wrong_status_for_32021")["http_status"] == 200
    assert not run.blocked  # -32021 confirms the modern era: dependents still run


async def test_capability_demand_with_empty_list_is_malformed_only() -> None:
    server = ScriptedServer(
        routes={
            "tools/list": lambda b, _r: error_response(
                b, -32021, "Missing capability", status=400, data={"requiredCapabilities": []}
            )
        }
    )
    run = await run_scripted(server)
    assert run.codes == {
        "handshake.client_capability_demanded",
        "handshake.capability_error_malformed",
    }
    assert "an empty array" in run.finding("handshake.capability_error_malformed").message
