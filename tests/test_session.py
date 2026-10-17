"""``session`` check tests (PLAN.md §7.2 part 9, §10.4 / §10.5 rows).

L1: ScriptedServer over ``httpx2.MockTransport`` (what mcpcheck *sent*, every verdict
path, both JSON and SSE replies). L2: the real fixture apps in-process over
``httpx2.ASGITransport`` with one client port per connection. L3: uvicorn over TCP for
fresh-connection and timeout hazards only.
"""

from __future__ import annotations

import json
import time
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import anyio
import httpx2
import pytest

from mcpcheck import runner, spec_urls, wire
from mcpcheck.checks import select
from mcpcheck.checks.base import CheckContext, Findings
from mcpcheck.checks.session import (
    BOGUS_PREFIX,
    SessionCheck,
    amend_result,
    canon,
    first_diff_pointer,
    identity,
    scan_session_ids,
)
from mcpcheck.client import ClientConfig, Exchange, RawClient, Transcript
from mcpcheck.models import Level, Status
from tests.conftest import ASGI_BASE_URL, ASGI_CLIENT_PORT, fetch_request_log
from tests.fixtures import misbehaving_server, reference_server
from tests.fixtures.misbehaving_server import SESSION_ID, Toggles
from tests.helpers.scripted import (
    ScriptedServer,
    conformant_server,
    error_response,
    jsonrpc_error,
    list_result,
    ok,
    sse_body,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from mcpcheck.models import CheckResult, Finding

pytestmark = pytest.mark.anyio

URL = "http://127.0.0.1:1/mcp"
PASS_SUMMARY = "No `Mcp-Session-Id` issued or expected; lists identical across 3 connections"
ALLOWED_METHODS = {"tools/list", "prompts/list", "resources/list", "server/discover"}


# --------------------------------------------------------------------------- harness


class TransportContext(CheckContext):
    """A ``CheckContext`` whose connections run over injected transports (one per label)."""

    def __init__(
        self,
        cfg: ClientConfig,
        transcript: Transcript,
        factory: Callable[[str], httpx2.AsyncBaseTransport],
    ) -> None:
        super().__init__(
            cfg, transcript, RawClient(cfg, transcript, label="main", transport=factory("main"))
        )
        self._factory = factory
        self.labels: list[str] = []

    @asynccontextmanager
    async def fresh(self, label: str, *, send_token: bool = True) -> AsyncIterator[RawClient]:
        assert label != "main"
        self.labels.append(label)
        client = RawClient(
            self.cfg,
            self.transcript,
            label=label,
            send_token=send_token,
            transport=self._factory(label),
        )
        try:
            yield client
        finally:
            await client.aclose()


async def run_session(ctx: CheckContext, *, gate: bool = True) -> CheckResult:
    """Run the check the way ``runner.run_checks`` does (optionally after the internal gate)."""
    if gate:
        await runner._internal_gate(ctx)  # mirrors the runner
    check = SessionCheck()
    f = Findings(check, strict=ctx.cfg.strict)
    ctx.current_check = check.name
    ctx.transcript.current_check = check.name
    before = len(ctx.transcript)
    try:
        with anyio.fail_after(ctx.cfg.check_budget):
            await check.run(ctx, f)
    finally:
        ctx.transcript.current_check = ""
        await ctx.raw.aclose()
    return f.to_result(requests=len(ctx.transcript) - before)


async def run_scripted(
    server: ScriptedServer,
    cfg: ClientConfig | None = None,
    *,
    gate: bool = True,
    before: Callable[[CheckContext], Awaitable[None]] | None = None,
) -> CheckResult:
    ctx = TransportContext(
        cfg or ClientConfig(url=URL), Transcript(), lambda _l: server.transport()
    )
    if before is not None:
        await before(ctx)
    return await run_session(ctx, gate=gate)


async def run_asgi(
    app: Any, *, lifespan: bool, gate: bool = True, **cfg_kw: Any
) -> tuple[CheckResult, Transcript]:
    """Run against an ASGI app; every connection label gets its own client port."""
    ports: dict[str, int] = {}

    def factory(label: str) -> httpx2.AsyncBaseTransport:
        port = ports.setdefault(label, ASGI_CLIENT_PORT + len(ports))
        return httpx2.ASGITransport(app, client=("127.0.0.1", port))

    transcript = Transcript()
    cfg = ClientConfig(url=ASGI_BASE_URL + "/mcp", **cfg_kw)
    async with AsyncExitStack() as stack:
        if lifespan:
            await stack.enter_async_context(app.router.lifespan_context(app))
        result = await run_session(TransportContext(cfg, transcript, factory), gate=gate)
    return result, transcript


def codes(result: CheckResult) -> set[str]:
    return {x.code for x in (*result.failures, *result.warnings)}


def finding(result: CheckResult, code: str) -> Finding:
    for x in (*result.failures, *result.warnings, *result.info):
        if x.code == code:
            return x
    raise AssertionError(f"{code} not in {codes(result)}")


def ev(result: CheckResult, code: str) -> dict[str, Any]:
    e = finding(result, code).evidence
    assert e is not None
    return e


def tool(name: str, description: str = "d") -> dict[str, Any]:
    return {"name": name, "description": description, "inputSchema": {"type": "object"}}


ALPHA_BETA = [tool("alpha"), tool("beta")]


@dataclass
class ConnectionAwareServer(ScriptedServer):
    """A ScriptedServer whose routes can tell connections apart (``current`` = transport index)."""

    connections: int = 0
    current: int = 0
    log: list[int] = field(default_factory=list)

    def transport(self) -> httpx2.MockTransport:
        index = self.connections
        self.connections += 1

        async def handler(req: httpx2.Request) -> httpx2.Response:
            self.current = index
            self.log.append(index)
            return await self.handler(req)

        return httpx2.MockTransport(handler)


def discover_result(caps: dict[str, Any]) -> dict[str, Any]:
    return {
        "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "scripted", "version": "0"}},
        "resultType": "complete",
        "supportedVersions": ["2026-07-28"],
        "capabilities": caps,
        "ttlMs": 60000,
        "cacheScope": "public",
    }


def _tools_routes(
    tools_for: Callable[[dict[str, Any], httpx2.Request], list[dict[str, Any]]],
    *,
    sse: bool,
    headers: Callable[[httpx2.Request], dict[str, str]] | None,
) -> dict[str, Any]:
    """Conformant discover/list routes whose ``tools/list`` items come from ``tools_for``."""

    def hdrs(req: httpx2.Request) -> dict[str, str] | None:
        return headers(req) if headers else None

    def tools_route(body: dict[str, Any], req: httpx2.Request) -> httpx2.Response:
        return ok(body, list_result(tools_for(body, req), "tools"), sse=sse, headers=hdrs(req))

    def plain(key: str) -> Any:
        return lambda b, r: ok(b, list_result([], key), sse=sse, headers=hdrs(r))

    caps: dict[str, Any] = {"tools": {}, "prompts": {}, "resources": {}}
    return {
        "server/discover": lambda b, r: ok(b, discover_result(caps), sse=sse, headers=hdrs(r)),
        "tools/list": tools_route,
        "prompts/list": plain("prompts"),
        "resources/list": plain("resources"),
    }


def tools_server(
    tools_for: Callable[[dict[str, Any], httpx2.Request], list[dict[str, Any]]],
    *,
    sse: bool = False,
    headers: Callable[[httpx2.Request], dict[str, str]] | None = None,
) -> ScriptedServer:
    return ScriptedServer(routes=_tools_routes(tools_for, sse=sse, headers=headers))


def aware_tools_server(
    tools_for: Callable[[dict[str, Any], httpx2.Request], list[dict[str, Any]]],
) -> ConnectionAwareServer:
    return ConnectionAwareServer(routes=_tools_routes(tools_for, sse=False, headers=None))


# --------------------------------------------------------------------------- pure helpers


def test_canon_identity_and_pointer() -> None:
    assert canon({"b": 1, "a": "é"}) == '{"a":"é","b":1}'
    assert identity({"name": "x"}, "tools/list") == "x"
    assert identity({"uri": "demo://a"}, "resources/list") == "demo://a"
    assert identity({"nope": 1}, "tools/list") == '{"nope":1}'
    assert first_diff_pointer({"a": 1}, {"a": 1}) == ""
    assert first_diff_pointer({"a": 1, "description": "x"}, {"a": 1, "description": "y"}) == (
        "/description"
    )
    assert first_diff_pointer({"a": {"b/c": [1, 2]}}, {"a": {"b/c": [1, 3]}}) == "/a/b~1c/1"
    assert first_diff_pointer({"a": [1]}, {"a": [1, 2]}) == "/a/1"
    assert first_diff_pointer({"a": 1}, {"b": 1}) == "/a"


def test_scan_session_ids_splits_minted_echoed_and_skips_legacy() -> None:
    def exchange(
        seq: int, method: str, *, sent: list[str], got: list[str], legacy: bool = False
    ) -> Exchange:
        return Exchange(
            seq=seq,
            connection="c",
            check="x",
            purpose="",
            verb="POST",
            url=URL,
            rpc_method=method,
            request_headers=[],
            request_body="",
            status=200,
            response_headers=[("mcp-session-id", v) for v in got],
            response_body="",
            elapsed_ms=1,
            legacy=legacy,
            session_ids_sent=sent,
            session_ids_received=got,
        )

    minted, echoed = scan_session_ids(
        [
            exchange(1, "tools/list", sent=[], got=["m1"]),
            exchange(2, "tools/list", sent=["b"], got=["b"]),
            exchange(3, "tools/list", sent=["b"], got=["other"]),
            exchange(4, "initialize", sent=[], got=["legacy"], legacy=True),
            exchange(5, "prompts/list", sent=[], got=[]),
        ],
        {1: "Mcp-Session-Id"},
    )
    assert [(o.seq, o.value, o.header_as_sent) for o in minted] == [
        (1, "m1", "Mcp-Session-Id"),
        (3, "other", None),
    ]
    assert [(o.seq, o.value) for o in echoed] == [(2, "b")]


# --------------------------------------------------------------------------- L1: pass paths


@pytest.mark.parametrize("sse", [False, True])
async def test_pass_scripted_and_what_was_sent(sse: bool) -> None:
    server = conformant_server(sse=sse)
    result = await run_scripted(server)
    assert result.status is Status.PASS
    assert result.summary == PASS_SUMMARY
    assert not result.failures
    assert not result.warnings
    # gate on main, discover on main, then 3 x (tools, resources, prompts, tools), then bogus
    per_connection = ["tools/list", "resources/list", "prompts/list", "tools/list"]
    assert server.methods() == ["tools/list", "server/discover", *per_connection * 3, "tools/list"]
    assert result.requests == len(server.seen) - 1  # the gate belongs to the runner
    for seen in server.seen:
        assert seen.headers["mcp-method"] == seen.method
        assert seen.headers["mcp-protocol-version"] == wire.PROTOCOL_VERSION
        assert seen.headers["accept"] == wire.ACCEPT_VALUE
        assert seen.meta is not None
        assert seen.meta["io.modelcontextprotocol/protocolVersion"] == wire.PROTOCOL_VERSION
        assert "io.modelcontextprotocol/clientCapabilities" in seen.meta
        assert seen.body is not None
        assert set(seen.body["params"]) == {"_meta"}  # single page: never a cursor
    bogus = server.seen[-1]
    sid = bogus.headers["mcp-session-id"]
    assert sid.startswith(BOGUS_PREFIX)
    assert len(sid) == len(BOGUS_PREFIX) + 16
    assert all("mcp-session-id" not in s.headers for s in server.seen[:-1])
    assert "3 fresh connection(s) + 1 bogus-id probe" in result.details
    assert "tools/list, resources/list, prompts/list" in result.details


def paged_server(
    *, sse: bool, cursors: tuple[str, ...], pages: tuple[list[dict[str, Any]], ...]
) -> ScriptedServer:
    """``tools/list`` served in pages chained by ``cursors`` (``cursors[i]`` leads to ``pages[i+1]``)."""

    def tools_route(body: dict[str, Any], _req: httpx2.Request) -> httpx2.Response:
        cursor = body["params"].get("cursor")
        index = 0 if cursor is None else cursors.index(cursor) + 1
        extra = {"nextCursor": cursors[index]} if index < len(cursors) else {}
        return ok(body, list_result(pages[index], "tools", **extra), sse=sse)

    server = conformant_server(sse=sse)
    server.routes["tools/list"] = tools_route
    return server


@pytest.mark.parametrize("sse", [False, True])
async def test_pass_paged(sse: bool) -> None:
    server = paged_server(sse=sse, cursors=("c1",), pages=([tool("alpha")], [tool("beta")]))
    result = await run_scripted(server)
    assert result.status is Status.PASS, result.summary
    tools_calls = [s for s in server.seen if s.method == "tools/list"]
    cursors = [s.body["params"].get("cursor") for s in tools_calls if s.body]
    # gate page 0, main page 1 (sample 0), 3 x 2 walks x 2 pages, bogus 2 pages
    assert cursors.count("c1") == 1 + 6 + 1
    assert cursors.count(None) == 1 + 6 + 1
    bogus_pages = [s for s in tools_calls if "mcp-session-id" in s.headers]
    assert [s.body["params"].get("cursor") for s in bogus_pages if s.body] == [None, "c1"]
    assert len({s.headers["mcp-session-id"] for s in bogus_pages}) == 1


@pytest.mark.parametrize("sse", [False, True])
async def test_pass_empty_string_cursor(sse: bool) -> None:
    server = paged_server(sse=sse, cursors=("",), pages=([tool("alpha")], [tool("beta")]))
    result = await run_scripted(server)
    assert result.status is Status.PASS, result.summary
    sent = [s.body["params"] for s in server.seen if s.method == "tools/list" and s.body]
    assert {k: v for k, v in sent[-1].items() if k != "_meta"} == {"cursor": ""}
    assert sum(1 for p in sent if p.get("cursor") == "") == 8


@pytest.mark.parametrize("sse", [False, True])
async def test_pass_empty_lists(sse: bool) -> None:
    result = await run_scripted(conformant_server(sse=sse, tools=[]))
    assert result.status is Status.PASS
    assert result.summary == PASS_SUMMARY


async def test_capabilities_decide_secondary_lists() -> None:
    server = conformant_server()
    server.routes["server/discover"] = lambda b, _r: ok(b, discover_result({"tools": {}}))
    result = await run_scripted(server)
    assert result.status is Status.PASS
    assert set(server.methods()) == {"tools/list", "server/discover"}
    assert "lists compared: tools/list." in result.details


async def test_unknown_capabilities_try_once_and_treat_32601_as_unsupported() -> None:
    server = conformant_server()
    del server.routes["server/discover"]  # → 404 -32601, capabilities unknown
    del server.routes["resources/list"]  # → 404 -32601 on the first attempt only
    result = await run_scripted(server)
    assert result.status is Status.PASS
    methods = server.methods()
    assert methods.count("resources/list") == 1
    assert methods.count("prompts/list") == 3
    assert "not implemented: resources/list" in result.details


async def test_duplicate_names_compared_as_multisets() -> None:
    server = conformant_server(tools=[tool("dup"), tool("dup"), tool("x")])
    result = await run_scripted(server)
    assert result.status is Status.PASS


async def test_sse_streams_closed_after_response_frame() -> None:
    """S14: a trailing keepalive after the response must not be waited for."""

    def tools_route(body: dict[str, Any], _r: httpx2.Request) -> httpx2.Response:
        msg = {"jsonrpc": "2.0", "id": body["id"], "result": list_result(ALPHA_BETA, "tools")}
        content = sse_body(msg) + b": ping\n\n" + b": ping\n\n"
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=content)

    server = conformant_server(sse=True)
    server.routes["tools/list"] = tools_route
    result = await run_scripted(server)
    assert result.status is Status.PASS


# --------------------------------------------------------------------------- L1: session required


def session_gate_server(
    *, sse: bool, legacy_id: bool = False, message: str | None = None
) -> ScriptedServer:
    """Every request is rejected unless a known session id is presented (none ever is)."""

    def reject(body: dict[str, Any], req: httpx2.Request) -> httpx2.Response:
        rid: Any = "server-error" if legacy_id else body.get("id")
        if req.headers.get("mcp-session-id") is None:
            msg = jsonrpc_error(rid, -32600, message or "Bad Request: Missing session ID")
            status = 400
        else:
            msg = jsonrpc_error(rid, -32600, "Session not found")
            status = 404
        if sse:
            return httpx2.Response(
                status, headers={"content-type": "text/event-stream"}, content=sse_body(msg)
            )
        return httpx2.Response(status, json=msg)

    return ScriptedServer(catch_all=reject)


@pytest.mark.parametrize("sse", [False, True])
async def test_fail_required(sse: bool) -> None:
    server = session_gate_server(sse=sse)
    result = await run_scripted(server)
    assert result.status is Status.FAIL
    assert codes(result) == {"session.required", "session.bogus_id_rejected"}
    assert all(x.level is Level.MUST for x in result.failures)
    assert result.summary == (
        'Server requires `Mcp-Session-Id` on modern requests (HTTP 400, -32600 "…session ID") '
        "(+1 more)"
    )
    e = ev(result, "session.required")
    assert e["connection"] == 1
    assert e["http_status"] == 400
    assert e["rpc_code"] == -32600
    assert e["fingerprint"] == "session_word"
    b = ev(result, "session.bogus_id_rejected")
    assert b["connection"] == "bogus"
    assert b["http_status"] == 404
    assert b["value"].startswith(BOGUS_PREFIX)
    # one plain attempt, then the bogus probe: no further connections are opened
    assert server.methods() == ["tools/list", "server/discover", "tools/list", "tools/list"]
    assert "mcp-session-id" in server.seen[-1].headers


async def test_fail_required_legacy_fingerprint_without_session_word() -> None:
    server = session_gate_server(sse=False, legacy_id=True, message="Bad Request")
    result = await run_scripted(server)
    assert result.status is Status.FAIL
    assert "session.required" in codes(result)
    assert ev(result, "session.required")["fingerprint"] == "legacy_session"


async def test_fail_required_on_5xx_with_session_message() -> None:
    server = conformant_server()
    server.routes["tools/list"] = lambda b, _r: error_response(
        b, -32000, "no session bound to this connection", status=503
    )
    result = await run_scripted(server)
    assert result.status is Status.FAIL
    assert "session.required" in codes(result)
    assert ev(result, "session.required")["http_status"] == 503


async def test_fail_bogus_rejected_only() -> None:
    def tools_route(body: dict[str, Any], req: httpx2.Request) -> httpx2.Response:
        if "mcp-session-id" in req.headers:
            return error_response(body, -32600, "Session not found", status=404)
        return ok(body, list_result(ALPHA_BETA, "tools"))

    server = conformant_server()
    server.routes["tools/list"] = tools_route
    result = await run_scripted(server)
    assert result.status is Status.FAIL
    assert codes(result) == {"session.bogus_id_rejected"}
    assert (
        result.summary
        == 'Server enforces `Mcp-Session-Id` (bogus id → HTTP 404 "Session not found")'
    )
    assert ev(result, "session.bogus_id_rejected")["header_as_sent"] == "mcp-session-id"


async def test_bogus_rejected_for_any_reason_when_plain_request_succeeded() -> None:
    def tools_route(body: dict[str, Any], req: httpx2.Request) -> httpx2.Response:
        if "mcp-session-id" in req.headers:
            return httpx2.Response(500, text="boom")
        return ok(body, list_result(ALPHA_BETA, "tools"))

    server = conformant_server()
    server.routes["tools/list"] = tools_route
    result = await run_scripted(server)
    assert result.status is Status.FAIL
    assert codes(result) == {"session.bogus_id_rejected"}
    assert result.summary == "Server enforces `Mcp-Session-Id` (bogus id → HTTP 500)"


# --------------------------------------------------------------------------- L1: minted / echoed


@pytest.mark.parametrize("sse", [False, True])
async def test_warn_minted(sse: bool) -> None:
    server = tools_server(
        lambda _b, _r: ALPHA_BETA, sse=sse, headers=lambda _r: {"Mcp-Session-Id": "s-123"}
    )
    result = await run_scripted(server)
    assert result.status is Status.WARN
    assert codes(result) == {"session.session_id_minted"}
    assert result.summary == (
        "`Mcp-Session-Id` minted on modern responses (not required; SHOULD not mint) (+3 more)"
    )
    subjects = {w.subject for w in result.warnings}
    assert subjects == {"tools/list", "resources/list", "prompts/list", "server/discover"}
    e = ev(result, "session.session_id_minted")  # first warning: tools/list group
    assert finding(result, "session.session_id_minted").subject == "tools/list"
    assert e["header_as_sent"] == "Mcp-Session-Id"  # wire casing, not the lowercase lookup key
    assert e["value"] == "s-123"
    assert e["occurrences"] == 1 + 7  # gate + 3x2 fresh walks + bogus
    assert all(w.level is Level.SHOULD for w in result.warnings)
    assert "Mcp-Session-Id minted on" in result.details


@pytest.mark.parametrize("sse", [False, True])
async def test_warn_echoed(sse: bool) -> None:
    def echo(req: httpx2.Request) -> dict[str, str]:
        sid = req.headers.get("mcp-session-id")
        return {"Mcp-Session-ID": sid} if sid else {}

    server = tools_server(lambda _b, _r: ALPHA_BETA, sse=sse, headers=echo)
    result = await run_scripted(server)
    assert result.status is Status.WARN
    assert codes(result) == {"session.session_id_echoed"}
    assert result.summary == "Client-supplied `Mcp-Session-Id` echoed back (SHOULD be ignored)"
    e = ev(result, "session.session_id_echoed")
    assert e["connection"] == "bogus"
    assert e["value"] == server.seen[-1].headers["mcp-session-id"]
    assert e["header_as_sent"] == "Mcp-Session-ID"


async def test_transcript_scan_flags_other_checks() -> None:
    """A minted id on an exchange recorded by *another* check is attributed to its method."""
    server = tools_server(lambda _b, _r: ALPHA_BETA, headers=lambda _r: {"Mcp-Session-Id": "s"})

    async def discover_first(ctx: CheckContext) -> None:
        ctx.transcript.current_check = "discover"
        ctx.current_check = "discover"
        await ctx.discover()

    result = await run_scripted(server, before=discover_first)
    assert result.status is Status.WARN
    w = next(x for x in result.warnings if x.subject == "server/discover")
    assert w.code == "session.session_id_minted"
    assert w.evidence is not None
    assert w.evidence["connection"] == "main"
    assert server.methods().count("server/discover") == 1  # cached: never re-fetched


async def test_scan_ignores_legacy_exchanges() -> None:
    server = conformant_server()
    server.routes["initialize"] = lambda b, _r: ok(
        b, {"protocolVersion": "2025-11-25"}, headers={"Mcp-Session-Id": "legacy-1"}
    )

    async def legacy_probe(ctx: CheckContext) -> None:
        async with ctx.fresh("legacy") as raw:
            reply = await raw.post("initialize", meta_version=None, header_version=None)
        assert reply.session_ids == ["legacy-1"]
        assert ctx.transcript.exchanges[-1].legacy is True

    result = await run_scripted(server, before=legacy_probe)
    assert result.status is Status.PASS
    assert result.summary == PASS_SUMMARY


async def test_amend_result_appends_minted_warnings_from_later_exchanges() -> None:
    result = await run_scripted(conformant_server())
    assert result.status is Status.PASS
    transcript = Transcript()
    later = Exchange(
        seq=1,
        connection="errors-1",
        check="errors",
        purpose="",
        verb="POST",
        url=URL,
        rpc_method="tools/call",
        request_headers=[],
        request_body="",
        status=200,
        response_headers=[("mcp-session-id", "late")],
        response_body="",
        elapsed_ms=1,
        session_ids_received=["late"],
    )
    transcript.append(later)
    amended = amend_result(result, transcript)
    assert result.status is Status.PASS  # input untouched
    assert amended.status is Status.WARN
    assert [w.code for w in amended.warnings] == ["session.session_id_minted"]
    assert amended.warnings[0].subject == "tools/call"
    assert amended.summary.startswith("`Mcp-Session-Id` minted on modern responses")
    assert amend_result(amended, transcript) is amended  # idempotent
    # a failing result keeps its status and summary
    failing = result.model_copy(update={"status": Status.FAIL, "summary": "x"})
    assert amend_result(failing, transcript).status is Status.FAIL
    assert amend_result(failing, transcript).summary == "x"
    assert amend_result(result, Transcript()) is result


# --------------------------------------------------------------------------- L1: list variation


async def test_fail_vary_conn() -> None:
    def tools_for(_b: dict[str, Any], _r: httpx2.Request) -> list[dict[str, Any]]:
        extra = [tool(f"tool_conn_{server.current}")] if server.current > 0 else []
        return [*ALPHA_BETA, *extra]

    server = aware_tools_server(tools_for)
    result = await run_scripted(server)
    assert result.status is Status.FAIL
    assert "session.list_varies_per_connection" in codes(result)
    assert "session.list_varies_within_connection" not in codes(result)
    assert result.summary.startswith(
        "tools/list varies per connection (3 connections, 3 distinct tool sets)"
    )
    e = ev(result, "session.list_varies_per_connection")
    assert e["sets"] == {
        "1": ["alpha", "beta", "tool_conn_1"],
        "2": ["alpha", "beta", "tool_conn_2"],
        "3": ["alpha", "beta", "tool_conn_3"],
    }
    assert e["connection"] == 2
    assert e["http_status"] == 200
    assert finding(result, "session.list_varies_per_connection").spec_url == (
        spec_urls.SPEC_TOOLS_CAPABILITIES
    )
    assert "tools/list per connection:" in result.details


@pytest.mark.parametrize("sse", [False, True])
async def test_fail_vary_count(sse: bool) -> None:
    counter = {"n": 0}

    def tools_for(_b: dict[str, Any], _r: httpx2.Request) -> list[dict[str, Any]]:
        counter["n"] += 1
        return [*ALPHA_BETA, tool(f"tool_req_{counter['n']}")]

    result = await run_scripted(tools_server(tools_for, sse=sse))
    assert result.status is Status.FAIL
    assert {
        "session.list_varies_per_connection",
        "session.list_varies_within_connection",
        "session.list_varies_with_session_header",
    } <= {x.code for x in result.failures}
    within = ev(result, "session.list_varies_within_connection")
    assert within["connection"] == 1
    assert set(within["sets"]) == {"first", "second"}
    header = ev(result, "session.list_varies_with_session_header")
    assert header["connection"] == "bogus"
    assert header["value"].startswith(BOGUS_PREFIX)
    assert set(header["sets"]) == {"1", "bogus"}
    assert result.summary.endswith("more)")


async def test_fail_vary_with_session_header_only() -> None:
    def tools_for(_b: dict[str, Any], req: httpx2.Request) -> list[dict[str, Any]]:
        return [*ALPHA_BETA, tool("secret")] if "mcp-session-id" in req.headers else ALPHA_BETA

    result = await run_scripted(tools_server(tools_for))
    assert result.status is Status.FAIL
    assert codes(result) == {"session.list_varies_with_session_header"}
    assert result.summary == "tools/list differs when a bogus Mcp-Session-Id is sent"


async def test_fail_resources_vary_per_connection() -> None:
    server = aware_tools_server(lambda _b, _r: ALPHA_BETA)
    server.routes["resources/list"] = lambda b, _r: ok(
        b, list_result([{"uri": f"demo://{server.current}", "name": "n"}], "resources")
    )
    result = await run_scripted(server)
    assert result.status is Status.FAIL
    assert codes(result) == {"session.list_varies_per_connection"}
    f_ = finding(result, "session.list_varies_per_connection")
    assert f_.subject == "resources/list"
    assert f_.spec_url == spec_urls.SPEC_RESOURCES_CAPABILITIES
    assert "resource set differs" in f_.message
    assert (
        result.summary
        == "resources/list varies per connection (3 connections, 3 distinct resource sets)"
    )


@pytest.mark.parametrize("sse", [False, True])
async def test_warn_order(sse: bool) -> None:
    counter = {"n": 0}

    def tools_for(_b: dict[str, Any], _r: httpx2.Request) -> list[dict[str, Any]]:
        counter["n"] += 1
        return ALPHA_BETA if counter["n"] % 2 else list(reversed(ALPHA_BETA))

    result = await run_scripted(tools_server(tools_for, sse=sse))
    assert result.status is Status.WARN
    assert codes(result) == {"session.order_not_deterministic"}
    assert result.summary == "tools/list order differs across connections (set identical)"
    e = ev(result, "session.order_not_deterministic")
    assert sorted(e["order"].values()) == [["alpha", "beta"], ["beta", "alpha"]]
    assert finding(result, "session.order_not_deterministic").level is Level.SHOULD


@pytest.mark.parametrize("sse", [False, True])
async def test_warn_definition_unstable(sse: bool) -> None:
    counter = {"n": 0}

    def tools_for(_b: dict[str, Any], _r: httpx2.Request) -> list[dict[str, Any]]:
        counter["n"] += 1
        return [tool("alpha", f"revision {'A' if counter['n'] % 2 else 'B'}"), tool("beta")]

    result = await run_scripted(tools_server(tools_for, sse=sse))
    assert result.status is Status.WARN
    assert codes(result) == {"session.definition_unstable"}
    assert result.summary == "tools/list definitions differ across connections (same tool names)"
    e = ev(result, "session.definition_unstable")
    assert e["item"] == "alpha"
    assert e["first_diff_pointer"] == "/description"


async def test_order_difference_in_prompts_is_not_reported() -> None:
    """S9 (deterministic order) is a tools/list SHOULD only."""
    counter = {"n": 0}

    def prompts(b: dict[str, Any], _r: httpx2.Request) -> httpx2.Response:
        counter["n"] += 1
        items = [{"name": "p1"}, {"name": "p2"}]
        return ok(b, list_result(items if counter["n"] % 2 else items[::-1], "prompts"))

    server = conformant_server()
    server.routes["prompts/list"] = prompts
    result = await run_scripted(server)
    assert result.status is Status.PASS


async def test_warn_list_changed_during_run() -> None:
    def tools_for(_b: dict[str, Any], _r: httpx2.Request) -> list[dict[str, Any]]:
        return ALPHA_BETA if server.current > 0 else [tool("alpha")]

    server = aware_tools_server(tools_for)
    result = await run_scripted(server)
    assert result.status is Status.WARN
    assert codes(result) == {"session.list_changed_during_run"}
    assert finding(result, "session.list_changed_during_run").level is Level.BEST_PRACTICE
    assert ev(result, "session.list_changed_during_run")["sets"] == {
        "0": ["alpha"],
        "1": ["alpha", "beta"],
    }
    assert result.summary == "tools/list changed between the handshake and the session samples"


async def test_sample0_absent_without_gate_uses_main_walk() -> None:
    server = conformant_server()
    result = await run_scripted(server, gate=False)
    assert result.status is Status.PASS
    # without a seeded page 0 the run cache is filled by one walk on main at the end
    assert server.methods()[-1] == "tools/list"
    assert "mcp-session-id" not in server.seen[-1].headers


@pytest.mark.parametrize("sse", [False, True])
async def test_warn_cursor_loop(sse: bool) -> None:
    def tools_route(body: dict[str, Any], _r: httpx2.Request) -> httpx2.Response:
        page = ALPHA_BETA if body["params"].get("cursor") is None else [tool("gamma")]
        return ok(body, list_result(page, "tools", nextCursor="c1"), sse=sse)

    server = conformant_server(sse=sse)
    server.routes["tools/list"] = tools_route
    result = await run_scripted(server)
    assert result.status is Status.WARN
    assert codes(result) == {"session.cursor_loop"}
    assert result.summary == "tools/list pagination repeats a cursor"
    e = ev(result, "session.cursor_loop")
    assert e["cursor"] == "c1"
    assert e["pages"] == 2


# --------------------------------------------------------------------------- L1: inconclusive


async def test_inconclusive_401_and_pass_with_token() -> None:
    inner = conformant_server()

    def auth(body: dict[str, Any], req: httpx2.Request) -> httpx2.Response:
        if req.headers.get("authorization") != "Bearer sekrit":
            return httpx2.Response(401, headers={"www-authenticate": "Bearer"})
        return inner.routes[body["method"]](body, req)

    server = ScriptedServer(catch_all=auth)
    result = await run_scripted(server)
    assert result.status is Status.INCONCLUSIVE
    assert result.summary == "Not evaluated: HTTP 401 (pass --token)"
    assert not result.failures
    assert not result.warnings
    # one plain attempt and the bogus probe only
    assert server.methods() == ["tools/list", "server/discover", "tools/list", "tools/list"]

    server = ScriptedServer(catch_all=auth)
    result = await run_scripted(server, ClientConfig(url=URL, token="sekrit"))
    assert result.status is Status.PASS
    assert all(s.headers["authorization"] == "Bearer sekrit" for s in server.seen)


async def test_inconclusive_403_hint() -> None:
    server = ScriptedServer(
        catch_all=lambda _b, _r: httpx2.Response(403, text="Invalid Origin header")
    )
    result = await run_scripted(server)
    assert result.status is Status.INCONCLUSIVE
    assert result.summary == "Not evaluated: HTTP 403 (try --origin)"


async def test_inconclusive_tools_not_implemented() -> None:
    server = conformant_server()
    del server.routes["tools/list"]
    result = await run_scripted(server)
    assert result.status is Status.INCONCLUSIVE
    assert result.summary == "Not evaluated: tools/list not implemented (-32601)"


@pytest.mark.parametrize(
    ("code", "summary"),
    [
        (-32022, "Not evaluated: server does not support 2026-07-28"),
        (-32020, "Not evaluated: well-formed request rejected with -32020 (intermediary?)"),
        (-32021, "Not evaluated: server demands a client capability (-32021)"),
    ],
)
async def test_inconclusive_modern_rejection(code: int, summary: str) -> None:
    server = ScriptedServer(catch_all=lambda b, _r: error_response(b, code, "nope", status=400))
    result = await run_scripted(server)
    assert result.status is Status.INCONCLUSIVE
    assert result.summary == summary


async def test_not_jsonrpc_2xx_makes_sample_invalid() -> None:
    server = ScriptedServer(catch_all=lambda _b, _r: httpx2.Response(202))
    result = await run_scripted(server)
    assert result.status is Status.INCONCLUSIVE
    assert result.summary == "Not evaluated: tools/list → HTTP 202 to a JSON-RPC request"


async def test_inconclusive_5xx_without_session_message() -> None:
    server = ScriptedServer(catch_all=lambda _b, _r: httpx2.Response(503, text="down"))
    result = await run_scripted(server)
    assert result.status is Status.INCONCLUSIVE
    assert result.summary == "Not evaluated: HTTP 503"


async def test_inconclusive_too_large(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcpcheck import client as client_mod

    monkeypatch.setattr(client_mod, "MAX_BODY_BYTES", 64)
    result = await run_scripted(conformant_server())
    assert result.status is Status.INCONCLUSIVE
    assert result.summary == "Not evaluated: body exceeds the 0 MiB cap"


def connection_failure_server(failing: set[int], status: int = 503) -> ConnectionAwareServer:
    def tools_for(_b: dict[str, Any], _r: httpx2.Request) -> list[dict[str, Any]]:
        return ALPHA_BETA

    server = aware_tools_server(tools_for)
    inner = server.routes["tools/list"]

    def maybe_fail(body: dict[str, Any], req: httpx2.Request) -> httpx2.Response:
        if server.current in failing:
            return httpx2.Response(status, text="down")
        return inner(body, req)

    server.routes["tools/list"] = maybe_fail
    return server


async def test_warn_partial_samples() -> None:
    result = await run_scripted(connection_failure_server({3}))
    assert result.status is Status.WARN
    assert codes(result) == {"session.partial_samples"}
    assert result.summary == "Only 2 of 3 fresh connections answered; lists compared on those"
    e = ev(result, "session.partial_samples")
    assert e["connection"] == 3
    assert e["http_status"] == 503
    assert e["valid_connections"] == [1, 2]


async def test_inconclusive_when_fewer_than_two_samples() -> None:
    result = await run_scripted(connection_failure_server({2, 3}))
    assert result.status is Status.INCONCLUSIVE
    assert result.summary == "Not evaluated: HTTP 503"
    assert not result.warnings


async def test_second_tools_list_failure_invalidates_the_sample() -> None:
    counter = {"n": 0}

    def tools_route(body: dict[str, Any], _r: httpx2.Request) -> httpx2.Response:
        counter["n"] += 1
        # gate=1, c1 first=2, c1 second=3 → fail the second call of connection 1 only
        if counter["n"] == 3:
            return httpx2.Response(500, text="flaky")
        return ok(body, list_result(ALPHA_BETA, "tools"))

    server = conformant_server()
    server.routes["tools/list"] = tools_route
    result = await run_scripted(server)
    assert result.status is Status.INCONCLUSIVE
    assert result.summary == "Not evaluated: HTTP 500"
    assert server.methods().count("tools/list") == 1 + 2 + 1  # gate, c1 x2, bogus


@pytest.mark.slow
async def test_budget_guard_stops_opening_connections() -> None:
    server = conformant_server()

    async def slow_transport_handler(req: httpx2.Request) -> httpx2.Response:
        await anyio.sleep(0.16)
        return await server.handler(req)

    cfg = ClientConfig(url=URL, timeout=0.2)  # check budget 2 s → guard at 1.4 s
    ctx = TransportContext(
        cfg, Transcript(), lambda _l: httpx2.MockTransport(slow_transport_handler)
    )
    result = await run_session(ctx)
    assert result.status is Status.WARN
    assert codes(result) == {"session.partial_samples"}
    assert "not attempted (check budget)" in finding(result, "session.partial_samples").message
    assert "Stopped after 2 connection(s)" in result.details
    assert ctx.labels == ["session-1", "session-2", "session-4"]


# --------------------------------------------------------------------------- L2: ASGI fixtures


@pytest.mark.parametrize(
    "build_kwargs",
    [
        {},
        {"json_response": True},
        {"stateless_http": False},
        {"empty": True},
        {"output_schema": False},
    ],
    ids=["sse", "json", "stateful", "empty", "no-output-schema"],
)
async def test_pass_sdk_reference(build_kwargs: dict[str, Any]) -> None:
    result, transcript = await run_asgi(reference_server.build_app(**build_kwargs), lifespan=True)
    assert result.status is Status.PASS, (result.summary, codes(result))
    assert result.summary == PASS_SUMMARY
    assert {e.rpc_method for e in transcript} <= ALLOWED_METHODS
    assert all(not e.session_ids_received for e in transcript)
    labels = {e.connection for e in transcript}
    assert labels == {"main", "session-1", "session-2", "session-3", "session-4"}


async def test_pass_sdk_reference_with_token() -> None:
    result, _ = await run_asgi(reference_server.build_app(require_token="t0k"), lifespan=True)
    assert result.status is Status.INCONCLUSIVE
    assert result.summary == "Not evaluated: HTTP 401 (pass --token)"
    # the SDK app's lifespan can only be entered once: build a second app for the token run
    app = reference_server.build_app(require_token="t0k")
    result, _ = await run_asgi(app, lifespan=True, token="t0k")
    assert result.status is Status.PASS


async def test_pass_sdk_bare_is_inconclusive() -> None:
    result, _ = await run_asgi(reference_server.build_app(bare=True), lifespan=True)
    assert result.status is Status.INCONCLUSIVE
    assert result.summary == "Not evaluated: tools/list not implemented (-32601)"


@pytest.mark.parametrize(
    "toggles",
    [
        Toggles(),
        Toggles(sse=True),
        Toggles(legacy_shape=True),
        Toggles(scope_varies_across_pages=True),
    ],
    ids=["default", "sse", "legacy_shape", "two-pages"],
)
async def test_pass_misbehaving_baseline(toggles: Toggles) -> None:
    result, _ = await run_asgi(misbehaving_server.build_app(toggles), lifespan=False)
    assert result.status is Status.PASS, (result.summary, codes(result))
    assert result.summary == PASS_SUMMARY
    assert "client ports 40002, 40003, 40004" in result.details


@pytest.mark.parametrize(
    ("toggles", "status", "expected", "level"),
    [
        (
            Toggles(require_session_id=True),
            Status.FAIL,
            {"session.required", "session.bogus_id_rejected"},
            Level.MUST,
        ),
        (
            Toggles(require_initialize=True),
            Status.FAIL,
            {"session.required", "session.bogus_id_rejected"},
            Level.MUST,
        ),
        (Toggles(mint_session_id=True), Status.WARN, {"session.session_id_minted"}, Level.SHOULD),
        (Toggles(echo_session_id=True), Status.WARN, {"session.session_id_echoed"}, Level.SHOULD),
        (
            Toggles(vary_tools_per_connection=True),
            Status.FAIL,
            {"session.list_varies_per_connection"},
            Level.MUST,
        ),
        (
            Toggles(vary_tools_per_request=True),
            Status.FAIL,
            {
                "session.list_varies_per_connection",
                "session.list_varies_within_connection",
                "session.list_varies_with_session_header",
            },
            Level.MUST,
        ),
        (
            Toggles(nondeterministic_order=True),
            Status.WARN,
            {"session.order_not_deterministic"},
            Level.SHOULD,
        ),
        (
            Toggles(vary_description=True),
            Status.WARN,
            {"session.definition_unstable"},
            Level.SHOULD,
        ),
        (Toggles(loop_cursor=True), Status.WARN, {"session.cursor_loop"}, Level.SHOULD),
    ],
    ids=[
        "require_session_id",
        "require_initialize",
        "mint_session_id",
        "echo_session_id",
        "vary_tools_per_connection",
        "vary_tools_per_request",
        "nondeterministic_order",
        "vary_description",
        "loop_cursor",
    ],
)
async def test_misbehaving_toggle_rows(
    toggles: Toggles, status: Status, expected: set[str], level: Level
) -> None:
    result, _ = await run_asgi(misbehaving_server.build_app(toggles), lifespan=False)
    assert result.status is status, (result.summary, codes(result))
    assert expected <= codes(result), codes(result)
    for code in expected:
        assert finding(result, code).level is level


async def test_misbehaving_require_initialize_fingerprint() -> None:
    result, _ = await run_asgi(
        misbehaving_server.build_app(Toggles(require_initialize=True)), lifespan=False
    )
    e = ev(result, "session.required")
    assert e["fingerprint"] == "legacy_session"
    assert e["rpc_message"] == "Bad Request: Missing session ID"
    assert result.summary == (
        'Server requires `Mcp-Session-Id` on modern requests (HTTP 400, -32600 "…session ID") '
        "(+1 more)"
    )
    b = ev(result, "session.bogus_id_rejected")
    assert (b["http_status"], b["rpc_message"]) == (404, "Session not found")


async def test_misbehaving_minted_evidence_shows_wire_casing() -> None:
    result, transcript = await run_asgi(
        misbehaving_server.build_app(Toggles(mint_session_id=True)), lifespan=False
    )
    assert codes(result) == {"session.session_id_minted"}
    e = ev(result, "session.session_id_minted")
    assert e["header_as_sent"] == "Mcp-Session-Id"
    assert e["value"] == SESSION_ID
    assert {w.subject for w in result.warnings} == ALLOWED_METHODS
    assert all(e.session_ids_received == [SESSION_ID] for e in transcript)


async def test_misbehaving_echo_evidence() -> None:
    result, transcript = await run_asgi(
        misbehaving_server.build_app(Toggles(echo_session_id=True)), lifespan=False
    )
    assert codes(result) == {"session.session_id_echoed"}
    bogus = [e for e in transcript if e.session_ids_sent]
    assert len(bogus) == 1
    assert bogus[0].connection == "session-4"
    assert ev(result, "session.session_id_echoed")["value"] == bogus[0].session_ids_sent[0]


async def test_misbehaving_vary_conn_evidence() -> None:
    result, _ = await run_asgi(
        misbehaving_server.build_app(Toggles(vary_tools_per_connection=True)), lifespan=False
    )
    e = ev(result, "session.list_varies_per_connection")
    assert e["sets"]["1"] == ["echo_required", "lookup_weather", "tool_conn_1"]
    assert e["sets"]["2"] == ["echo_required", "lookup_weather", "tool_conn_2"]
    assert e["sets"]["3"] == ["echo_required", "lookup_weather", "tool_conn_3"]
    assert result.summary.startswith(
        "tools/list varies per connection (3 connections, 3 distinct tool sets)"
    )


async def test_misbehaving_vary_description_pointer() -> None:
    result, _ = await run_asgi(
        misbehaving_server.build_app(Toggles(vary_description=True)), lifespan=False
    )
    e = ev(result, "session.definition_unstable")
    assert e["item"] == "lookup_weather"
    assert e["first_diff_pointer"] == "/description"


async def test_misbehaving_401_is_inconclusive() -> None:
    result, _ = await run_asgi(misbehaving_server.build_app(Toggles(status=401)), lifespan=False)
    assert result.status is Status.INCONCLUSIVE
    assert result.summary == "Not evaluated: HTTP 401 (pass --token)"


async def test_asgi_request_log_contains_only_allowed_requests() -> None:
    app = misbehaving_server.build_app()
    result, _ = await run_asgi(app, lifespan=False)
    assert result.status is Status.PASS
    log = app.state.fixture.log
    assert {(e["verb"], e["method"]) for e in log} <= {("POST", m) for m in ALLOWED_METHODS}
    ports = {e["client_port"] for e in log}
    assert ports == {ASGI_CLIENT_PORT + i for i in range(5)}
    bogus = [e for e in log if "mcp-session-id" in e["headers"]]
    assert len(bogus) == 1
    assert bogus[0]["headers"]["mcp-session-id"].startswith(BOGUS_PREFIX)
    assert bogus[0]["client_port"] == ASGI_CLIENT_PORT + 4


# --------------------------------------------------------------------------- L3: TCP hazards


@pytest.mark.integration
async def test_fresh_connections(misbehaving_url: Callable[[Toggles], str]) -> None:
    url = misbehaving_url(Toggles())
    transcript = Transcript()
    report = await runner.run_checks(
        ClientConfig(url=url), select(["session"]), transcript=transcript
    )
    (result,) = report.checks
    assert result.status is Status.PASS, (result.summary, codes(result))
    assert report.summary.exit_code == 0
    log = fetch_request_log(url)
    posts = [e for e in log if e["verb"] == "POST"]
    assert {e["method"] for e in posts} <= ALLOWED_METHODS
    # main (gate + discover) + 3 fresh + bogus = 5 distinct TCP connections
    assert len({e["client_port"] for e in posts}) == 5
    bogus = [e for e in posts if "mcp-session-id" in e["headers"]]
    assert len(bogus) == 1
    assert sum(1 for e in posts if e["client_port"] == bogus[0]["client_port"]) == 1
    # The run's first POST is the gate, on the shared "main" connection; every other
    # port except the bogus-session probe is a connection the session check opened and
    # must name in its details. Identify main by order, not by port number: Linux
    # randomises ephemeral ports, so the first connection is not the lowest-numbered one.
    main_port = min(posts, key=lambda e: e["n"])["client_port"]
    fresh_ports = {e["client_port"] for e in posts} - {main_port, bogus[0]["client_port"]}
    assert len(fresh_ports) == wire.SESSION_CONNECTIONS, sorted(fresh_ports)
    assert all(str(p) in result.details for p in sorted(fresh_ports))
    assert result.requests == len(posts) - 1


@pytest.mark.integration
@pytest.mark.slow
async def test_inconclusive_timeout(misbehaving_url: Callable[[Toggles], str]) -> None:
    url = misbehaving_url(Toggles(slow_seconds=2))
    t0 = time.perf_counter()
    with anyio.fail_after(10):
        report = await runner.run_checks(ClientConfig(url=url, timeout=0.5), select(["session"]))
    elapsed = time.perf_counter() - t0
    (result,) = report.checks
    assert result.status is Status.INCONCLUSIVE
    assert result.summary == "Not evaluated: request exceeded the 0.5s total budget"
    assert elapsed < 6, elapsed


@pytest.mark.integration
async def test_tcp_mint_toggle_through_runner(misbehaving_url: Callable[[Toggles], str]) -> None:
    url = misbehaving_url(Toggles(mint_session_id=True))
    report = await runner.run_checks(ClientConfig(url=url), select(["session"]))
    (result,) = report.checks
    assert result.status is Status.WARN
    assert codes(result) == {"session.session_id_minted"}
    assert (
        json.loads(report.model_dump_json())["checks"][0]["warnings"][0]["evidence"][
            "header_as_sent"
        ]
        == "Mcp-Session-Id"
    )


# --------------------------------------------------------------------------- L1: walks cut short by mcpcheck


def _endless_pages(body: dict[str, Any], _r: httpx2.Request) -> httpx2.Response:
    cursor = body["params"].get("cursor")
    n = 0 if cursor is None else int(cursor[1:]) + 1
    return ok(body, list_result([tool(f"t{n}")], "tools", nextCursor=f"c{n}"))


async def test_max_pages_is_an_info_not_a_cursor_loop() -> None:
    """More than MAX_PAGES pages is mcpcheck's limit, not a repeated cursor (no SHOULD)."""
    server = conformant_server()
    server.routes["tools/list"] = _endless_pages
    result = await run_scripted(server)
    assert result.status is Status.PASS, result.summary
    assert codes(result) == set()
    info = finding(result, "session.walk_truncated")
    assert info.level is Level.BEST_PRACTICE
    assert f"{wire.MAX_PAGES} pages (MAX_PAGES)" in info.message
    assert ev(result, "session.walk_truncated")["pages"] == wire.MAX_PAGES
    assert "pagination stopped by mcpcheck" in result.details
    assert result.summary.startswith("No `Mcp-Session-Id` issued or expected")


async def test_byte_budget_stops_the_check_inconclusively(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wire, "WALK_BYTE_BUDGET", 1500)
    server = conformant_server()
    server.routes["tools/list"] = _endless_pages
    result = await run_scripted(server)
    # the budget is exhausted on the first connection: no second sample, nothing held against the server
    assert result.status is Status.INCONCLUSIVE
    assert result.failures == []
    assert result.warnings == []
    assert "fewer than 2 fresh connections" in result.summary
    info = finding(result, "session.walk_truncated") if result.info else None
    assert info is None or "walk budget" in info.message
    assert result.requests < 20
