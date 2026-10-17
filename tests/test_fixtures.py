"""Fixture-server tests (milestone M2).

Both fixture servers must answer the raw modern ladder conformantly (under
``httpx2.ASGITransport`` and over TCP), every misbehaving toggle must flip only its
own wire behaviour, no uvicorn thread may leak, and the standalone ``--port 0`` entry
points must print their URL and serve.
"""

from __future__ import annotations

import contextlib
import json
import re
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import httpx2
import pytest
from mcp.shared.inbound import encode_header_value

from tests.conftest import (
    ASGI_CLIENT_PORT,
    ThreadServer,
    asgi_client,
    fetch_request_log,
    modern_body,
    modern_headers,
    reset_request_log,
    start_misbehaving_server,
)
from tests.fixtures import misbehaving_server, reference_server
from tests.fixtures.misbehaving_server import (
    CATALOGUE_TOOL_NAMES,
    DISCOVER_PAYLOAD_VARIANTS,
    SESSION_ID,
    Toggles,
    toggles_from_cli,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

REPO_ROOT = Path(__file__).resolve().parents[1]
ANY_HOST = "0.0.0.0"  # noqa: S104 - the SDK footgun under test, never bound
EVIL = "https://evil.example.com"
LIST_METHODS = ("tools/list", "prompts/list", "resources/list", "resources/templates/list")
ITEM_KEY = {
    "tools/list": "tools",
    "prompts/list": "prompts",
    "resources/list": "resources",
    "resources/templates/list": "resourceTemplates",
}
SERVER_INFO_KEY = "io.modelcontextprotocol/serverInfo"

pytestmark = pytest.mark.anyio


# ----------------------------------------------------------------- helpers


def media_type(response: httpx2.Response) -> str:
    return response.headers.get("content-type", "").split(";")[0].strip()


def parse_reply(response: httpx2.Response) -> dict[str, Any]:
    """Return the single JSON-RPC message of a JSON or SSE reply."""
    if media_type(response) == "text/event-stream":
        messages = [
            json.loads(line[len("data: ") :])
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        replies = [m for m in messages if "result" in m or "error" in m]
        assert len(replies) == 1, response.text
        message: dict[str, Any] = replies[0]
        return message
    assert media_type(response) == "application/json", response.headers
    body: dict[str, Any] = response.json()
    return body


async def post(
    client: httpx2.AsyncClient,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
    body: Any = None,
    rid: Any = 1,
) -> httpx2.Response:
    return await client.post(
        "/mcp",
        headers=headers if headers is not None else modern_headers(method),
        json=body if body is not None else modern_body(method, params, rid=rid),
    )


def same_origin(client: httpx2.AsyncClient) -> str:
    url = client.base_url
    return f"{url.scheme}://{url.netloc.decode()}"


# ----------------------------------------------------------------- ladder (both servers)

LadderKind = str
LADDER_KINDS = (
    "reference-asgi",
    "reference-json-asgi",
    "misbehaving-asgi",
    "reference-tcp",
    "misbehaving-tcp",
)


@pytest.fixture(params=LADDER_KINDS)
async def ladder(
    request: pytest.FixtureRequest,
    anyio_backend: str,
    reference_url: str,
    reference_url_json: str,
    misbehaving_url: Callable[[Toggles], str],
) -> AsyncIterator[tuple[str, httpx2.AsyncClient]]:
    kind: str = request.param
    flavour = kind.split("-")[0]
    if kind == "reference-asgi":
        async with asgi_client(reference_server.build_app()) as client:
            yield flavour, client
    elif kind == "reference-json-asgi":
        async with asgi_client(reference_server.build_app(json_response=True)) as client:
            yield flavour, client
    elif kind == "misbehaving-asgi":
        async with asgi_client(misbehaving_server.build_app(), lifespan=False) as client:
            yield flavour, client
    else:
        url = reference_url if flavour == "reference" else misbehaving_url(Toggles())
        async with httpx2.AsyncClient(
            base_url=url.removesuffix("/mcp"), timeout=httpx2.Timeout(10)
        ) as client:
            yield flavour, client


async def test_ladder_tools_list_modern_shape(ladder: tuple[str, httpx2.AsyncClient]) -> None:
    flavour, client = ladder
    response = await post(client, "tools/list")
    assert response.status_code == 200
    assert media_type(response) == "application/json"
    assert "mcp-session-id" not in response.headers
    reply = parse_reply(response)
    assert reply["id"] == 1
    result = reply["result"]
    assert result["resultType"] == "complete"
    assert isinstance(result["ttlMs"], int)
    assert result["ttlMs"] >= 0
    assert result["cacheScope"] == "public"
    info = result["_meta"][SERVER_INFO_KEY]
    assert info == {"name": f"mcpcheck-{flavour}-server", "version": "0.1.0"}
    tools = result["tools"]
    assert [t["name"] for t in tools] == ["lookup_weather", "echo_required"]
    for tool in tools:
        assert tool["inputSchema"]["type"] == "object"
        assert tool["outputSchema"]["type"] == "object"
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["annotations"]["destructiveHint"] is False
    assert tools[1]["inputSchema"]["required"] == ["message", "repeat"]
    assert "nextCursor" not in result


async def test_ladder_discover(ladder: tuple[str, httpx2.AsyncClient]) -> None:
    flavour, client = ladder
    response = await post(client, "server/discover")
    assert response.status_code == 200
    result = parse_reply(response)["result"]
    assert result["resultType"] == "complete"
    assert result["supportedVersions"] == ["2026-07-28"]
    assert set(result["capabilities"]) >= {"tools", "prompts", "resources"}
    assert isinstance(result["ttlMs"], int)
    assert result["ttlMs"] > 0
    assert result["cacheScope"] == "public"
    assert result["_meta"][SERVER_INFO_KEY]["name"] == f"mcpcheck-{flavour}-server"
    assert isinstance(result["instructions"], str)


@pytest.mark.parametrize("method", LIST_METHODS[1:])
async def test_ladder_other_lists_carry_hints(
    ladder: tuple[str, httpx2.AsyncClient], method: str
) -> None:
    _, client = ladder
    result = parse_reply(await post(client, method))["result"]
    assert isinstance(result[ITEM_KEY[method]], list)
    assert result["resultType"] == "complete"
    assert isinstance(result["ttlMs"], int)
    assert result["cacheScope"] == "public"
    expected_len = {"prompts/list": 1, "resources/list": 1, "resources/templates/list": 0}[method]
    assert len(result[ITEM_KEY[method]]) == expected_len


async def test_ladder_evil_origin_403(ladder: tuple[str, httpx2.AsyncClient]) -> None:
    _, client = ladder
    response = await post(
        client, "server/discover", headers=modern_headers("server/discover", origin=EVIL)
    )
    assert response.status_code == 403
    assert "content-type" not in response.headers
    assert response.text == "Invalid Origin header"
    null = await post(
        client, "server/discover", headers=modern_headers("server/discover", origin="null")
    )
    assert null.status_code == 403


async def test_ladder_same_origin_accepted(ladder: tuple[str, httpx2.AsyncClient]) -> None:
    _, client = ladder
    response = await post(
        client,
        "server/discover",
        headers=modern_headers("server/discover", origin=same_origin(client)),
    )
    assert response.status_code == 200


async def test_ladder_headerless_request(ladder: tuple[str, httpx2.AsyncClient]) -> None:
    flavour, client = ladder
    response = await post(
        client,
        "tools/list",
        headers={
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
        },
        body={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    reply = parse_reply(response)
    if flavour == "reference":
        # SDK: dual-era by construction, legacy 2025 shape (no hints, no resultType).
        assert response.status_code == 200
        assert "resultType" not in reply["result"]
        assert "ttlMs" not in reply["result"]
        assert [t["name"] for t in reply["result"]["tools"]] == ["lookup_weather", "echo_required"]
    else:
        # Modern-only server: MUST reject (T:277-281).
        assert response.status_code == 400
        assert reply["error"]["code"] == -32020


async def test_ladder_unsupported_version_32022(ladder: tuple[str, httpx2.AsyncClient]) -> None:
    _, client = ladder
    response = await post(
        client,
        "tools/list",
        headers=modern_headers("tools/list", protocol_version="2099-01-01"),
        body=modern_body("tools/list", protocol_version="2099-01-01"),
    )
    assert response.status_code == 400
    error = parse_reply(response)["error"]
    assert error["code"] == -32022
    assert error["data"] == {"supported": ["2026-07-28"], "requested": "2099-01-01"}


async def test_ladder_header_body_mismatch_32020(ladder: tuple[str, httpx2.AsyncClient]) -> None:
    _, client = ladder
    response = await post(
        client, "tools/list", headers=modern_headers("tools/list", protocol_version="2026-01-01")
    )
    assert response.status_code == 400
    assert parse_reply(response)["error"]["code"] == -32020


async def test_ladder_get_405(ladder: tuple[str, httpx2.AsyncClient]) -> None:
    _, client = ladder
    response = await client.get("/mcp", headers=modern_headers("tools/list"))
    assert response.status_code == 405
    assert response.headers["allow"] == "POST"
    delete = await client.delete("/mcp", headers=modern_headers("tools/list"))
    assert delete.status_code == 405


async def test_ladder_unknown_method_404_32601(ladder: tuple[str, httpx2.AsyncClient]) -> None:
    _, client = ladder
    response = await post(client, "mcpcheck/nonexistent")
    assert response.status_code == 404
    assert parse_reply(response)["error"]["code"] == -32601


async def test_ladder_missing_meta_32602(ladder: tuple[str, httpx2.AsyncClient]) -> None:
    _, client = ladder
    body = modern_body("tools/list")
    del body["params"]["_meta"]["io.modelcontextprotocol/clientCapabilities"]
    response = await post(client, "tools/list", body=body)
    assert response.status_code == 400
    error = parse_reply(response)["error"]
    assert error["code"] == -32602
    assert "io.modelcontextprotocol/clientCapabilities" in error["message"]


async def test_ladder_mcp_method_header_required(ladder: tuple[str, httpx2.AsyncClient]) -> None:
    _, client = ladder
    headers = modern_headers("tools/list")
    del headers["mcp-method"]
    response = await post(client, "tools/list", headers=headers)
    assert response.status_code == 400
    assert parse_reply(response)["error"]["code"] == -32020
    cased = await post(client, "tools/list", headers=modern_headers("Tools/List"))
    assert cased.status_code == 400
    assert parse_reply(cased)["error"]["code"] == -32020


async def test_ladder_invalid_tool_args_is_error(ladder: tuple[str, httpx2.AsyncClient]) -> None:
    _, client = ladder
    response = await post(
        client,
        "tools/call",
        {"name": "echo_required", "arguments": {}},
        headers=modern_headers("tools/call", mcp_name="echo_required"),
    )
    assert response.status_code == 200
    result = parse_reply(response)["result"]
    assert result["isError"] is True
    assert result["resultType"] == "complete"
    text = result["content"][0]["text"]
    assert "echo_required" in text
    assert "message" in text
    wrong_type = await post(
        client,
        "tools/call",
        {"name": "lookup_weather", "arguments": {"city": 12345}},
        headers=modern_headers("tools/call", mcp_name="lookup_weather"),
    )
    assert parse_reply(wrong_type)["result"]["isError"] is True


async def test_ladder_unknown_tool_is_error(ladder: tuple[str, httpx2.AsyncClient]) -> None:
    _, client = ladder
    response = await post(
        client,
        "tools/call",
        {"name": "mcpcheck__nonexistent_tool_7f3a", "arguments": {}},
        headers=modern_headers("tools/call", mcp_name="mcpcheck__nonexistent_tool_7f3a"),
    )
    assert response.status_code == 200
    result = parse_reply(response)["result"]
    assert result["isError"] is True
    assert "mcpcheck__nonexistent_tool_7f3a" in result["content"][0]["text"]


async def test_ladder_bad_json_32700(ladder: tuple[str, httpx2.AsyncClient]) -> None:
    _, client = ladder
    response = await client.post("/mcp", headers=modern_headers("tools/list"), content=b"{not json")
    assert response.status_code == 400
    assert parse_reply(response)["error"]["code"] == -32700


async def test_ladder_bogus_session_id_ignored(ladder: tuple[str, httpx2.AsyncClient]) -> None:
    _, client = ladder
    response = await post(
        client,
        "tools/list",
        headers=modern_headers("tools/list", mcp_session_id="mcpcheck-bogus-0123456789abcdef"),
    )
    assert response.status_code == 200
    assert "mcp-session-id" not in response.headers
    assert parse_reply(response)["result"]["resultType"] == "complete"


# ----------------------------------------------------------------- reference server variants


async def test_reference_no_output_schema() -> None:
    async with asgi_client(reference_server.build_app(output_schema=False)) as client:
        tools = parse_reply(await post(client, "tools/list"))["result"]["tools"]
    assert [t["name"] for t in tools] == ["lookup_weather", "echo_required"]
    assert all("outputSchema" not in t for t in tools)


async def test_reference_empty_declares_capabilities() -> None:
    async with asgi_client(reference_server.build_app(empty=True)) as client:
        discover = parse_reply(await post(client, "server/discover"))["result"]
        tools = parse_reply(await post(client, "tools/list"))["result"]
    assert set(discover["capabilities"]) == {"tools", "prompts", "resources"}
    assert tools["tools"] == []
    assert tools["cacheScope"] == "public"


async def test_reference_bare_lowlevel() -> None:
    async with asgi_client(reference_server.build_app(bare=True)) as client:
        discover = parse_reply(await post(client, "server/discover"))
        tools = await post(client, "tools/list")
    assert discover["result"]["capabilities"] == {}
    assert discover["result"]["supportedVersions"] == ["2026-07-28"]
    assert tools.status_code == 404
    assert parse_reply(tools)["error"]["code"] == -32601


@pytest.mark.parametrize(
    "kwargs", [{"origin_check": False}, {"host": ANY_HOST}], ids=["disabled", "host-0.0.0.0"]
)
async def test_reference_origin_not_validated(kwargs: dict[str, Any]) -> None:
    async with asgi_client(reference_server.build_app(**kwargs)) as client:
        response = await post(
            client, "server/discover", headers=modern_headers("server/discover", origin=EVIL)
        )
    assert response.status_code == 200
    assert parse_reply(response)["result"]["resultType"] == "complete"


async def test_reference_require_token() -> None:
    async with asgi_client(reference_server.build_app(require_token="s3cret")) as client:
        denied = await post(client, "tools/list")
        allowed = await post(
            client,
            "tools/list",
            headers=modern_headers("tools/list", authorization="Bearer s3cret"),
        )
    assert denied.status_code == 401
    assert denied.headers["www-authenticate"].startswith("Bearer")
    assert denied.content == b""
    assert allowed.status_code == 200


async def test_reference_stateful_dual_era() -> None:
    async with asgi_client(reference_server.build_app(stateless_http=False)) as client:
        modern = await post(client, "tools/list")
        init = await client.post(
            "/mcp",
            headers={
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"},
                },
            },
        )
    assert modern.status_code == 200
    assert "mcp-session-id" not in modern.headers
    assert init.status_code == 200
    assert init.headers.get("mcp-session-id")
    assert parse_reply(init)["result"]["protocolVersion"] == "2025-11-25"


async def test_reference_json_response_mode_headerless_is_json() -> None:
    async with asgi_client(reference_server.build_app(json_response=True)) as client:
        response = await client.post(
            "/mcp",
            headers={
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
    assert response.status_code == 200
    assert media_type(response) == "application/json"


async def test_reference_progress_token_switches_to_sse(
    reference_client: httpx2.AsyncClient,
) -> None:
    body = modern_body(
        "tools/call", {"name": "echo_required", "arguments": {"message": "hi", "repeat": 2}}
    )
    body["params"]["_meta"]["progressToken"] = "p1"
    response = await post(
        reference_client,
        "tools/call",
        headers=modern_headers("tools/call", mcp_name="echo_required"),
        body=body,
    )
    assert response.status_code == 200
    assert media_type(response) == "text/event-stream"
    assert "notifications/progress" in response.text
    assert parse_reply(response)["result"]["structuredContent"] == {"text": "hi hi", "repeat": 2}


# ----------------------------------------------------------------- misbehaving: probe table

Summary = dict[str, Any]
Probe = Callable[[httpx2.AsyncClient, httpx2.AsyncClient], Awaitable[Summary]]


def _rpc_summary(response: httpx2.Response) -> Summary:
    summary: Summary = {"status": response.status_code}
    try:
        reply = parse_reply(response)
    except (AssertionError, ValueError):
        return summary
    if "error" in reply:
        summary["code"] = reply["error"]["code"]
    else:
        summary["result"] = True
    return summary


async def p_tools_list(c: httpx2.AsyncClient, _: httpx2.AsyncClient) -> Summary:
    r = await post(c, "tools/list")
    s: Summary = {
        "status": r.status_code,
        "media": media_type(r),
        "session": r.headers.get("mcp-session-id"),
    }
    try:
        reply = parse_reply(r)
    except (AssertionError, ValueError):
        return s
    s["id"] = reply.get("id")
    if "result" in reply:
        res = reply["result"]
        s.update(
            keys=sorted(res),
            names=[t["name"] for t in res.get("tools", [])],
            ttl=res.get("ttlMs"),
            scope=res.get("cacheScope"),
            info=SERVER_INFO_KEY in res.get("_meta", {}),
        )
    else:
        s["code"] = reply["error"]["code"]
    return s


async def p_tools_list_again(c: httpx2.AsyncClient, _: httpx2.AsyncClient) -> Summary:
    r = await post(c, "tools/list")
    s: Summary = {"status": r.status_code}
    try:
        res = parse_reply(r).get("result")
    except (AssertionError, ValueError):
        return s
    if isinstance(res, dict):
        tools = res.get("tools", [])
        s["names"] = [t["name"] for t in tools]
        s["first_description"] = tools[0]["description"] if tools else None
    return s


async def p_tools_list_conn2(_: httpx2.AsyncClient, c2: httpx2.AsyncClient) -> Summary:
    r = await post(c2, "tools/list")
    s: Summary = {"status": r.status_code}
    try:
        res = parse_reply(r).get("result")
    except (AssertionError, ValueError):
        return s
    if isinstance(res, dict):
        s["names"] = [t["name"] for t in res.get("tools", [])]
    return s


async def p_discover(c: httpx2.AsyncClient, _: httpx2.AsyncClient) -> Summary:
    r = await post(c, "server/discover")
    s: Summary = {"status": r.status_code, "media": media_type(r)}
    try:
        reply = parse_reply(r)
    except (AssertionError, ValueError):
        return s
    s["id"] = reply.get("id")
    if "result" in reply:
        res = reply["result"]
        caps = res.get("capabilities")
        s.update(
            keys=sorted(res),
            versions=res.get("supportedVersions"),
            caps=sorted(caps) if isinstance(caps, dict) else caps,
            info=SERVER_INFO_KEY in res.get("_meta", {})
            if isinstance(res.get("_meta"), dict)
            else None,
            ttl=res.get("ttlMs"),
            scope=res.get("cacheScope"),
        )
    else:
        s["code"] = reply["error"]["code"]
    return s


def _list_probe(method: str) -> Probe:
    async def probe(c: httpx2.AsyncClient, _: httpx2.AsyncClient) -> Summary:
        r = await post(c, method)
        s: Summary = {"status": r.status_code}
        try:
            res = parse_reply(r).get("result")
        except (AssertionError, ValueError):
            return s
        if isinstance(res, dict):
            s["hints"] = sorted(k for k in res if k in {"ttlMs", "cacheScope", "resultType"})
            s["ttl"] = res.get("ttlMs")
            s["scope"] = res.get("cacheScope")
        return s

    return probe


async def p_evil_origin(c: httpx2.AsyncClient, _: httpx2.AsyncClient) -> Summary:
    r = await post(c, "server/discover", headers=modern_headers("server/discover", origin=EVIL))
    return _rpc_summary(r)


async def p_null_origin(c: httpx2.AsyncClient, _: httpx2.AsyncClient) -> Summary:
    r = await post(c, "server/discover", headers=modern_headers("server/discover", origin="null"))
    return _rpc_summary(r)


async def p_same_origin(c: httpx2.AsyncClient, _: httpx2.AsyncClient) -> Summary:
    r = await post(c, "tools/list", headers=modern_headers("tools/list", origin=same_origin(c)))
    return {"status": r.status_code}


async def p_headerless(c: httpx2.AsyncClient, _: httpx2.AsyncClient) -> Summary:
    r = await post(
        c,
        "tools/list",
        headers={
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
        },
        body={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    return _rpc_summary(r)


async def p_initialize_headerless(c: httpx2.AsyncClient, _: httpx2.AsyncClient) -> Summary:
    r = await post(
        c,
        "initialize",
        headers={
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
        },
        body={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        },
    )
    return _rpc_summary(r)


async def p_legacy_header_version(c: httpx2.AsyncClient, _: httpx2.AsyncClient) -> Summary:
    r = await post(
        c, "tools/list", headers=modern_headers("tools/list", protocol_version="2025-11-25")
    )
    return _rpc_summary(r)


async def p_unsupported_version(c: httpx2.AsyncClient, _: httpx2.AsyncClient) -> Summary:
    r = await post(
        c,
        "tools/list",
        headers=modern_headers("tools/list", protocol_version="2099-01-01"),
        body=modern_body("tools/list", protocol_version="2099-01-01"),
    )
    s = _rpc_summary(r)
    with contextlib.suppress(AssertionError, ValueError, KeyError):
        s["data_keys"] = sorted(parse_reply(r)["error"].get("data", {}) or {})
    return s


async def p_missing_meta(c: httpx2.AsyncClient, _: httpx2.AsyncClient) -> Summary:
    body = modern_body("tools/list")
    del body["params"]["_meta"]["io.modelcontextprotocol/clientCapabilities"]
    return _rpc_summary(await post(c, "tools/list", body=body))


async def p_missing_mcp_method(c: httpx2.AsyncClient, _: httpx2.AsyncClient) -> Summary:
    headers = modern_headers("tools/list")
    del headers["mcp-method"]
    return _rpc_summary(await post(c, "tools/list", headers=headers))


async def p_unknown_method(c: httpx2.AsyncClient, _: httpx2.AsyncClient) -> Summary:
    return _rpc_summary(await post(c, "mcpcheck/nonexistent"))


async def p_invalid_cursor(c: httpx2.AsyncClient, _: httpx2.AsyncClient) -> Summary:
    return _rpc_summary(await post(c, "tools/list", {"cursor": "mcpcheck-invalid-cursor-7f3a"}))


async def p_bogus_session(c: httpx2.AsyncClient, _: httpx2.AsyncClient) -> Summary:
    r = await post(
        c,
        "tools/list",
        headers=modern_headers("tools/list", mcp_session_id="mcpcheck-bogus-0123456789abcdef"),
    )
    s = _rpc_summary(r)
    s["session"] = r.headers.get("mcp-session-id")
    return s


async def p_call_invalid(c: httpx2.AsyncClient, _: httpx2.AsyncClient) -> Summary:
    r = await post(
        c,
        "tools/call",
        {"name": "echo_required", "arguments": {}},
        headers=modern_headers("tools/call", mcp_name="echo_required"),
    )
    s = _rpc_summary(r)
    try:
        res = parse_reply(r).get("result")
    except (AssertionError, ValueError):
        return s
    if isinstance(res, dict):
        s["is_error"] = res.get("isError")
        s["text"] = (res.get("content") or [{}])[0].get("text")
    return s


async def p_call_unknown(c: httpx2.AsyncClient, _: httpx2.AsyncClient) -> Summary:
    r = await post(
        c,
        "tools/call",
        {"name": "nope", "arguments": {}},
        headers=modern_headers("tools/call", mcp_name="nope"),
    )
    s = _rpc_summary(r)
    try:
        res = parse_reply(r).get("result")
    except (AssertionError, ValueError):
        return s
    if isinstance(res, dict):
        s["is_error"] = res.get("isError")
    return s


async def p_get(c: httpx2.AsyncClient, _: httpx2.AsyncClient) -> Summary:
    r = await c.get("/mcp", headers=modern_headers("tools/list"))
    return {"status": r.status_code, "allow": r.headers.get("allow")}


async def p_options(c: httpx2.AsyncClient, _: httpx2.AsyncClient) -> Summary:
    r = await c.options(
        "/mcp",
        headers={
            "origin": EVIL,
            "access-control-request-method": "POST",
            "access-control-request-headers": "content-type,mcp-method",
        },
    )
    return {"status": r.status_code, "acao": r.headers.get("access-control-allow-origin")}


PROBES: dict[str, Probe] = {
    "tools_list": p_tools_list,
    "tools_list_again": p_tools_list_again,
    "tools_list_conn2": p_tools_list_conn2,
    "discover": p_discover,
    "prompts_list": _list_probe("prompts/list"),
    "resources_list": _list_probe("resources/list"),
    "templates_list": _list_probe("resources/templates/list"),
    "evil_origin": p_evil_origin,
    "null_origin": p_null_origin,
    "same_origin": p_same_origin,
    "headerless": p_headerless,
    "initialize_headerless": p_initialize_headerless,
    "legacy_header_version": p_legacy_header_version,
    "unsupported_version": p_unsupported_version,
    "missing_meta": p_missing_meta,
    "missing_mcp_method": p_missing_mcp_method,
    "unknown_method": p_unknown_method,
    "invalid_cursor": p_invalid_cursor,
    "bogus_session": p_bogus_session,
    "call_invalid": p_call_invalid,
    "call_unknown": p_call_unknown,
    "get": p_get,
    "options": p_options,
}
ALL = frozenset(PROBES)
LISTS = frozenset({"tools_list", "prompts_list", "resources_list", "templates_list"})
TOOL_LISTINGS = frozenset({"tools_list", "tools_list_again", "tools_list_conn2"})
MODERN_OK = frozenset(
    {
        "tools_list",
        "tools_list_again",
        "tools_list_conn2",
        "discover",
        "prompts_list",
        "resources_list",
        "templates_list",
        "same_origin",
        "invalid_cursor",
        "bogus_session",
        "call_invalid",
        "call_unknown",
    }
)
"""Probes that reach dispatch on the conformant baseline (header validation passed)."""
MODERN_ALL = MODERN_OK | {
    "legacy_header_version",
    "unsupported_version",
    "missing_meta",
    "missing_mcp_method",
    "unknown_method",
}
"""Every probe carrying an MCP-Protocol-Version header without an Origin."""
SESSION_GATED = MODERN_ALL | {"headerless", "initialize_headerless"}
NO_ORIGIN = (MODERN_ALL - {"same_origin"}) | {"headerless", "initialize_headerless"}
"""Probes that send no Origin header at all."""


async def run_probes(toggles: Toggles) -> dict[str, Summary]:
    app = misbehaving_server.build_app(toggles)
    async with (
        asgi_client(app, lifespan=False, client_port=ASGI_CLIENT_PORT) as c1,
        asgi_client(app, lifespan=False, client_port=ASGI_CLIENT_PORT + 1) as c2,
    ):
        return {name: await probe(c1, c2) for name, probe in PROBES.items()}


TwoPage = (
    {
        "resultType": "complete",
        "tools": [misbehaving_server.DEFAULT_TOOLS[0]],
        "ttlMs": 5,
        "cacheScope": "public",
        "nextCursor": "c1",
    },
    {
        "resultType": "complete",
        "tools": [misbehaving_server.DEFAULT_TOOLS[1]],
        "ttlMs": 5,
        "cacheScope": "public",
    },
)

CASES: list[tuple[str, Toggles, frozenset[str]]] = [
    ("legacy_shape", Toggles(legacy_shape=True), LISTS),
    ("mint_session_id", Toggles(mint_session_id=True), frozenset({"tools_list", "bogus_session"})),
    ("echo_session_id", Toggles(echo_session_id=True), frozenset({"bogus_session"})),
    ("require_session_id", Toggles(require_session_id=True), SESSION_GATED),
    ("require_initialize", Toggles(require_initialize=True), SESSION_GATED),
    (
        "vary_tools_per_connection",
        Toggles(vary_tools_per_connection=True),
        frozenset({"tools_list_conn2"}),
    ),
    ("vary_tools_per_request", Toggles(vary_tools_per_request=True), TOOL_LISTINGS),
    (
        "nondeterministic_order",
        Toggles(nondeterministic_order=True),
        frozenset({"tools_list_again"}),
    ),
    ("vary_description", Toggles(vary_description=True), frozenset({"tools_list_again"})),
    ("no_discover", Toggles(no_discover=True), frozenset({"discover"})),
    ("discover_no_server_info", Toggles(discover_no_server_info=True), frozenset({"discover"})),
    ("discover_wrong_versions", Toggles(discover_wrong_versions=True), frozenset({"discover"})),
    ("capability_mismatch", Toggles(capability_mismatch=True), frozenset({"discover"})),
    ("discover_variant=no-hints", Toggles(discover_variant="no-hints"), frozenset({"discover"})),
    (
        "discover_variant=scope-user",
        Toggles(discover_variant="scope-user"),
        frozenset({"discover"}),
    ),
    (
        "discover_variant=wrong-id",
        Toggles(discover_variant="wrong-id"),
        frozenset({"tools_list", "discover"}),
    ),
    (
        "discover_variant=error-32020",
        Toggles(discover_variant="error-32020"),
        MODERN_OK | {"unknown_method"},
    ),
    (
        "tools=custom",
        Toggles(tools=(misbehaving_server.CATALOGUE_TOOLS[0],)),
        TOOL_LISTINGS | {"call_invalid"},
    ),
    (
        "pages=()",
        Toggles(pages=()),
        TOOL_LISTINGS | {"invalid_cursor", "same_origin", "bogus_session"},
    ),
    (
        "pages=two",
        Toggles(pages=TwoPage),
        frozenset({"tools_list", "tools_list_again", "tools_list_conn2"}),
    ),
    ("loop_cursor", Toggles(loop_cursor=True), frozenset({"tools_list", "invalid_cursor"})),
    ("error_style=jsonrpc", Toggles(error_style="jsonrpc"), frozenset({"call_invalid"})),
    ("error_style=traceback", Toggles(error_style="traceback"), frozenset({"call_invalid"})),
    ("error_style=bare", Toggles(error_style="bare"), frozenset({"call_invalid"})),
    (
        "error_style=success",
        Toggles(error_style="success"),
        frozenset({"call_invalid", "call_unknown"}),
    ),
    ("error_shapes_catalogue", Toggles(error_shapes_catalogue=True), TOOL_LISTINGS),
    ("omit_ttl", Toggles(omit_ttl=True), LISTS),
    ("negative_ttl", Toggles(negative_ttl=True), LISTS),
    ("float_ttl", Toggles(float_ttl=60000.0), LISTS),
    ("omit_cache_scope", Toggles(omit_cache_scope=True), LISTS),
    ("cache_scope=user", Toggles(cache_scope="user"), LISTS),
    ("scope_varies_across_pages", Toggles(scope_varies_across_pages=True), TOOL_LISTINGS),
    ("user_specific_public", Toggles(user_specific_public=True), TOOL_LISTINGS),
    (
        "origin_mode=reject_400_jsonrpc",
        Toggles(origin_mode="reject_400_jsonrpc"),
        frozenset({"evil_origin", "null_origin"}),
    ),
    (
        "origin_mode=reject_200_jsonrpc_error",
        Toggles(origin_mode="reject_200_jsonrpc_error"),
        frozenset({"evil_origin", "null_origin"}),
    ),
    (
        "origin_mode=allowlist_app_origin",
        Toggles(origin_mode="allowlist_app_origin"),
        frozenset({"same_origin"}),
    ),
    ("origin_mode=require_origin", Toggles(origin_mode="require_origin"), NO_ORIGIN),
    ("origin_mode=accept_null", Toggles(origin_mode="accept_null"), frozenset({"null_origin"})),
    (
        "origin_mode=accept_all",
        Toggles(origin_mode="accept_all"),
        frozenset({"evil_origin", "null_origin"}),
    ),
    (
        "origin_mode=accept_all_sse",
        Toggles(origin_mode="accept_all_sse"),
        frozenset({"evil_origin", "null_origin", "tools_list", "discover"}),
    ),
    (
        "origin_mode=legacy_only",
        Toggles(origin_mode="legacy_only"),
        ALL - {"get", "options", "unknown_method"},
    ),
    (
        "origin_mode=redirect_evil",
        Toggles(origin_mode="redirect_evil"),
        frozenset({"evil_origin", "null_origin"}),
    ),
    # origin_mode=reset_evil / hang_evil are TCP-only (ASGITransport cannot model a dropped connection).
    ("origin_mode=cors_reflect", Toggles(origin_mode="cors_reflect"), frozenset({"options"})),
    (
        "ignore_version_mismatch",
        Toggles(ignore_version_mismatch=True),
        frozenset({"legacy_header_version"}),
    ),
    (
        "serve_headerless_as_legacy",
        Toggles(serve_headerless_as_legacy=True),
        frozenset({"headerless", "initialize_headerless"}),
    ),
    (
        "unknown_method_status=200",
        Toggles(unknown_method_status=200),
        frozenset({"unknown_method"}),
    ),
    (
        "legacy_error_codes",
        Toggles(legacy_error_codes=True),
        frozenset({"unknown_method", "invalid_cursor"}),
    ),
    ("accept_missing_meta", Toggles(accept_missing_meta=True), frozenset({"missing_meta"})),
    ("ignore_cursor", Toggles(ignore_cursor=True), frozenset({"invalid_cursor"})),
    ("sse", Toggles(sse=True), frozenset({"tools_list", "discover"})),
    ("x_accel_buffering=False (no SSE)", Toggles(x_accel_buffering=False), frozenset()),
    ("status=401", Toggles(status=401), ALL),
    ("status=307", Toggles(status=307), ALL),
]


@pytest.fixture(scope="module")
def baseline() -> dict[str, Summary]:
    return anyio.run(run_probes, Toggles(), backend="asyncio")


async def test_baseline_is_conformant(baseline: dict[str, Summary]) -> None:
    b = baseline
    assert b["tools_list"]["status"] == 200
    assert b["tools_list"]["names"] == [
        "lookup_weather",
        "echo_required",
    ]
    assert b["tools_list"]["session"] is None
    assert b["tools_list"]["info"] is True
    assert (
        b["tools_list_again"]["names"] == b["tools_list_conn2"]["names"] == b["tools_list"]["names"]
    )
    assert b["discover"]["versions"] == ["2026-07-28"]
    assert b["discover"]["caps"] == [
        "prompts",
        "resources",
        "tools",
    ]
    assert b["evil_origin"] == {"status": 403}
    assert b["null_origin"] == {"status": 403}
    assert b["same_origin"] == {"status": 200}
    assert b["headerless"] == {"status": 400, "code": -32020}
    assert b["initialize_headerless"] == {"status": 400, "code": -32020}
    assert b["legacy_header_version"] == {"status": 400, "code": -32020}
    assert b["unsupported_version"] == {
        "status": 400,
        "code": -32022,
        "data_keys": ["requested", "supported"],
    }
    assert b["missing_meta"] == {"status": 400, "code": -32602}
    assert b["missing_mcp_method"] == {"status": 400, "code": -32020}
    assert b["unknown_method"] == {"status": 404, "code": -32601}
    assert b["invalid_cursor"] == {"status": 400, "code": -32602}
    assert b["bogus_session"] == {"status": 200, "result": True, "session": None}
    assert b["call_invalid"]["is_error"] is True
    assert "echo_required" in b["call_invalid"]["text"]
    assert b["call_unknown"]["is_error"] is True
    assert b["get"] == {"status": 405, "allow": "POST"}
    assert b["options"] == {"status": 405, "acao": None}


@pytest.mark.parametrize(("label", "toggles", "expected_changed"), CASES, ids=[c[0] for c in CASES])
async def test_toggle_flips_only_its_behaviour(
    baseline: dict[str, Summary], label: str, toggles: Toggles, expected_changed: frozenset[str]
) -> None:
    observed = await run_probes(toggles)
    changed = {name for name in PROBES if observed[name] != baseline[name]}
    assert changed == set(expected_changed), {
        name: (baseline[name], observed[name]) for name in changed ^ set(expected_changed)
    }


async def test_default_toggles_pass_each_probe_identically_twice(
    baseline: dict[str, Summary],
) -> None:
    assert await run_probes(Toggles()) == baseline


# ----------------------------------------------------------------- misbehaving: specifics


async def test_session_toggles_wire_details() -> None:
    async with asgi_client(
        misbehaving_server.build_app(Toggles(mint_session_id=True)), lifespan=False
    ) as c:
        r = await post(c, "tools/list")
        assert r.headers["mcp-session-id"] == SESSION_ID
        assert (b"Mcp-Session-Id", SESSION_ID.encode()) in r.headers.raw
    async with asgi_client(
        misbehaving_server.build_app(Toggles(echo_session_id=True)), lifespan=False
    ) as c:
        r = await post(
            c, "tools/list", headers=modern_headers("tools/list", mcp_session_id="bogus-1")
        )
        assert r.headers["mcp-session-id"] == "bogus-1"
        assert "mcp-session-id" not in (await post(c, "tools/list")).headers
    async with asgi_client(
        misbehaving_server.build_app(Toggles(require_initialize=True)), lifespan=False
    ) as c:
        rejected = await post(c, "tools/list")
        assert rejected.status_code == 400
        assert rejected.json() == {
            "jsonrpc": "2.0",
            "id": "server-error",
            "error": {"code": -32600, "message": "Bad Request: Missing session ID"},
        }
        bogus = await post(
            c, "tools/list", headers=modern_headers("tools/list", mcp_session_id="bogus")
        )
        assert bogus.status_code == 404
        assert bogus.json()["error"]["message"] == "Session not found"
        init = await c.post(
            "/mcp",
            headers={
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"},
                },
            },
        )
        assert init.status_code == 200
        assert init.headers["mcp-session-id"] == SESSION_ID
        assert init.json()["result"]["protocolVersion"] == "2025-11-25"
        with_session = await post(
            c, "tools/list", headers=modern_headers("tools/list", mcp_session_id=SESSION_ID)
        )
        assert with_session.status_code == 200
        assert "resultType" not in with_session.json()["result"]  # mcp 1.x shape behind the session
    async with asgi_client(
        misbehaving_server.build_app(Toggles(require_session_id=True)), lifespan=False
    ) as c:
        rejected = await post(c, "tools/list")
        assert rejected.status_code == 400
        assert rejected.json()["id"] == 1
        assert "session" in rejected.json()["error"]["message"].lower()


async def test_vary_toggles_details() -> None:
    app = misbehaving_server.build_app(Toggles(vary_tools_per_connection=True))
    async with (
        asgi_client(app, lifespan=False, client_port=50001) as c1,
        asgi_client(app, lifespan=False, client_port=50002) as c2,
    ):
        first = [t["name"] for t in parse_reply(await post(c1, "tools/list"))["result"]["tools"]]
        again = [t["name"] for t in parse_reply(await post(c1, "tools/list"))["result"]["tools"]]
        other = [t["name"] for t in parse_reply(await post(c2, "tools/list"))["result"]["tools"]]
    assert first == again == ["lookup_weather", "echo_required"]
    assert other == ["lookup_weather", "echo_required", "tool_conn_1"]
    async with asgi_client(
        misbehaving_server.build_app(Toggles(nondeterministic_order=True)), lifespan=False
    ) as c:
        a = [t["name"] for t in parse_reply(await post(c, "tools/list"))["result"]["tools"]]
        b = [t["name"] for t in parse_reply(await post(c, "tools/list"))["result"]["tools"]]
    assert a == list(reversed(b))
    assert sorted(a) == sorted(b)
    async with asgi_client(
        misbehaving_server.build_app(Toggles(vary_tools_per_request=True)), lifespan=False
    ) as c:
        a = [t["name"] for t in parse_reply(await post(c, "tools/list"))["result"]["tools"]]
        b = [t["name"] for t in parse_reply(await post(c, "tools/list"))["result"]["tools"]]
    assert a[-1] == "tool_req_0"
    assert b[-1] == "tool_req_1"


async def test_pagination_default_two_pages_and_cursor_rules() -> None:
    async with asgi_client(
        misbehaving_server.build_app(Toggles(scope_varies_across_pages=True)), lifespan=False
    ) as c:
        page0 = parse_reply(await post(c, "tools/list"))["result"]
        page1 = parse_reply(await post(c, "tools/list", {"cursor": page0["nextCursor"]}))["result"]
        bad = await post(c, "tools/list", {"cursor": "nope"})
    assert [t["name"] for t in page0["tools"]] == ["lookup_weather"]
    assert [t["name"] for t in page1["tools"]] == ["echo_required"]
    assert "nextCursor" not in page1
    assert (page0["cacheScope"], page1["cacheScope"]) == ("public", "private")
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == -32602
    async with asgi_client(
        misbehaving_server.build_app(Toggles(loop_cursor=True)), lifespan=False
    ) as c:
        p0 = parse_reply(await post(c, "tools/list"))["result"]
        p1 = parse_reply(await post(c, "tools/list", {"cursor": p0["nextCursor"]}))["result"]
    assert p0["nextCursor"] == p1["nextCursor"] == "c1"
    async with asgi_client(
        misbehaving_server.build_app(Toggles(ignore_cursor=True)), lifespan=False
    ) as c:
        r = parse_reply(await post(c, "tools/list", {"cursor": "whatever"}))["result"]
    assert [t["name"] for t in r["tools"]] == [
        "lookup_weather",
        "echo_required",
    ]
    assert "nextCursor" not in r


async def test_raw_pages_served_by_cursor_chain() -> None:
    pages = (
        {"tools": [], "nextCursor": "c1"},
        {
            "tools": [],
            "resultType": "complete",
            "ttlMs": 1,
            "cacheScope": "public",
            "nextCursor": "",
        },
        {"tools": [], "resultType": "complete", "ttlMs": 1, "cacheScope": "private"},
    )
    async with asgi_client(misbehaving_server.build_app(Toggles(pages=pages)), lifespan=False) as c:
        p0 = parse_reply(await post(c, "tools/list"))["result"]
        p1 = parse_reply(await post(c, "tools/list", {"cursor": "c1"}))["result"]
        p2 = parse_reply(await post(c, "tools/list", {"cursor": ""}))["result"]
        bad = await post(c, "tools/list", {"cursor": "zzz"})
    assert p0 == pages[0]
    assert p1 == pages[1]
    assert p2 == pages[2]
    assert bad.status_code == 400
    async with asgi_client(misbehaving_server.build_app(Toggles(pages=())), lifespan=False) as c:
        r = await post(c, "tools/list")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == -32601


async def test_cache_toggles_values() -> None:
    async with asgi_client(
        misbehaving_server.build_app(Toggles(negative_ttl=True)), lifespan=False
    ) as c:
        assert parse_reply(await post(c, "tools/list"))["result"]["ttlMs"] == -1
    async with asgi_client(
        misbehaving_server.build_app(Toggles(float_ttl=60000.0)), lifespan=False
    ) as c:
        r = await post(c, "tools/list")
        assert '"ttlMs":60000.0' in r.text
    async with asgi_client(
        misbehaving_server.build_app(Toggles(omit_ttl=True, omit_cache_scope=True)), lifespan=False
    ) as c:
        res = parse_reply(await post(c, "prompts/list"))["result"]
        assert "ttlMs" not in res
        assert "cacheScope" not in res
        assert res["resultType"] == "complete"
    async with asgi_client(
        misbehaving_server.build_app(Toggles(user_specific_public=True)), lifespan=False
    ) as c:
        names = [t["name"] for t in parse_reply(await post(c, "tools/list"))["result"]["tools"]]
    assert names[-1] == "list_my_orders"


@pytest.mark.parametrize("variant", sorted(DISCOVER_PAYLOAD_VARIANTS))
async def test_discover_payload_variants(variant: str) -> None:
    drop, overrides = DISCOVER_PAYLOAD_VARIANTS[variant]
    async with asgi_client(
        misbehaving_server.build_app(Toggles(discover_variant=variant)), lifespan=False
    ) as c:
        r = await post(c, "server/discover")
    assert r.status_code == 200
    result = parse_reply(r)["result"]
    for key in drop:
        assert key not in result
    for key, value in overrides.items():
        assert result[key] == json.loads(json.dumps(value))


def test_discover_variant_count_and_validation() -> None:
    assert len(DISCOVER_PAYLOAD_VARIANTS) == 43  # the plan's 42 plus caps-no-tools
    with pytest.raises(ValueError, match="discover_variant"):
        Toggles(discover_variant="nope")
    with pytest.raises(ValueError, match="origin_mode"):
        Toggles(origin_mode="nope")


@pytest.mark.parametrize(
    ("variant", "status", "code"),
    [
        ("error-32020", 400, -32020),
        ("error-32021", 400, -32021),
        ("error-32022", 400, -32022),
        ("error-32022-wrong-status", 200, -32022),
        ("error-32602", 400, -32602),
        ("error-32042", 400, -32042),
        ("legacy-version-reject", 400, -32600),
    ],
)
async def test_wire_error_variants_apply_to_every_modern_request(
    variant: str, status: int, code: int
) -> None:
    async with asgi_client(
        misbehaving_server.build_app(Toggles(discover_variant=variant)), lifespan=False
    ) as c:
        for method in ("server/discover", "tools/list"):
            r = await post(c, method)
            assert r.status_code == status
            assert parse_reply(r)["error"]["code"] == code


async def test_wire_envelope_variants() -> None:
    async def discover(variant: str) -> httpx2.Response:
        async with asgi_client(
            misbehaving_server.build_app(Toggles(discover_variant=variant)), lifespan=False
        ) as c:
            return await post(c, "server/discover")

    assert (await discover("wrong-id")).json()["id"] == "other"
    assert (await discover("null-id")).json()["id"] is None
    assert (await discover("jsonrpc-version-wrong")).json()["jsonrpc"] == "1.0"
    both = (await discover("result-and-error")).json()
    assert "result" in both
    assert "error" in both
    html = await discover("html-200")
    assert html.status_code == 200
    assert media_type(html) == "text/html"
    noct = await discover("json-no-content-type")
    assert "content-type" not in noct.headers
    assert noct.json()["result"]["resultType"] == "complete"
    assert isinstance((await discover("json-array")).json(), list)
    assert (await discover("status-202")).status_code == 202
    for variant, marker in (
        ("sse-notification-message", "notifications/message"),
        ("sse-server-request", '"method":"roots/list"'),
        ("sse-progress-then-result", "notifications/progress"),
    ):
        r = await discover(variant)
        assert media_type(r) == "text/event-stream"
        assert marker in r.text
        assert parse_reply(r)["result"]["resultType"] == "complete"
    two = await discover("sse-two-responses")
    assert two.text.count("event: message") == 2
    empty = await discover("sse-empty")
    assert media_type(empty) == "text/event-stream"
    assert "data:" not in empty.text


async def test_catalogue_tools_shapes() -> None:
    toggles = Toggles(error_shapes_catalogue=True)
    async with asgi_client(misbehaving_server.build_app(toggles), lifespan=False) as c:
        listed = [t["name"] for t in parse_reply(await post(c, "tools/list"))["result"]["tools"]]
        assert listed == ["lookup_weather", "echo_required", *CATALOGUE_TOOL_NAMES]

        async def call(name: str, arguments: dict[str, Any], **headers: str) -> httpx2.Response:
            return await post(
                c,
                "tools/call",
                {"name": name, "arguments": arguments},
                headers=modern_headers("tools/call", mcp_name=encode_header_value(name), **headers),
            )

        good = parse_reply(await call("err_good", {}))["result"]
        assert good["isError"] is True
        assert "err_good" in good["content"][0]["text"]
        r = await call("err_rpc_32602_200", {})
        assert r.status_code == 200
        assert r.json()["error"]["code"] == -32602
        r = await call("err_rpc_32602_400", {})
        assert r.status_code == 400
        assert r.json()["error"]["code"] == -32602
        r = await call("err_rpc_nullid_400", {})
        assert r.status_code == 400
        assert r.json()["id"] is None
        for name, code in (
            ("err_rpc_32000", -32000),
            ("err_rpc_32042", -32042),
            ("err_rpc_32050", -32050),
            ("err_rpc_32603", -32603),
        ):
            assert (await call(name, {})).json()["error"]["code"] == code
        assert parse_reply(await call("err_generic", {}))["result"]["content"][0]["text"] == "error"
        assert (
            "Traceback"
            in parse_reply(await call("err_traceback", {}))["result"]["content"][0]["text"]
        )
        assert parse_reply(await call("err_empty_content", {}))["result"]["content"] == []
        assert "resultType" not in parse_reply(await call("err_no_resulttype", {}))["result"]
        assert "structuredContent" in parse_reply(await call("err_structured", {}))["result"]
        garbage = parse_reply(await call("ok_garbage", {}))["result"]
        assert garbage["isError"] is False
        assert parse_reply(await call("input_required_state", {}))["result"] == {
            "resultType": "input_required",
            "requestState": {"token": "abc"},
        }
        assert "inputRequests" in parse_reply(await call("input_required_requests", {}))["result"]
        assert parse_reply(await call("input_required_empty", {}))["result"] == {
            "resultType": "input_required"
        }
        r = await call("http_500_html", {})
        assert r.status_code == 500
        assert media_type(r) == "text/html"
        r = await call("http_403_text", {})
        assert r.status_code == 403
        assert media_type(r) == "text/plain"
        r = await call("ctype_text", {})
        assert r.status_code == 200
        assert media_type(r) == "text/plain"
        r = await call("sse_iserror", {})
        assert media_type(r) == "text/event-stream"
        assert parse_reply(r)["result"]["isError"] is True
        r = await call("sse_notification_message", {})
        assert "notifications/message" in r.text
        assert parse_reply(r)["result"]["isError"] is True
        # hdr_tool: body value without the Mcp-Param-Region header -> 400/-32020; with it -> validated.
        r = await call("hdr_tool", {"q": 1, "region": "eu"})
        assert r.status_code == 400
        assert r.json()["error"]["code"] == -32020
        r = await call("hdr_tool", {"q": 1, "region": "eu"}, mcp_param_region="eu")
        assert parse_reply(r)["result"]["isError"] is True
        r = await call("hdr_tool", {})
        assert parse_reply(r)["result"]["isError"] is True
        assert (
            parse_reply(await call("destructive_norequired", {"q": 12345}))["result"]["isError"]
            is True
        )
        assert (
            parse_reply(await call("readonly_permissive", {"anything": 1}))["result"]["isError"]
            is False
        )
        uni = await call("工具_unicode", {})
        assert parse_reply(uni)["result"]["isError"] is True
        assert parse_reply(await call("wrongtype_ok", {"q": 12345}))["result"]["isError"] is False
        assert parse_reply(await call("wrongtype_ok", {"q": "ok"}))["result"]["isError"] is False
        # Mcp-Name must match (raw non-ASCII header value is not even sendable).
        mismatch = await post(
            c,
            "tools/call",
            {"name": "err_good", "arguments": {}},
            headers=modern_headers("tools/call", mcp_name="other"),
        )
        assert mismatch.status_code == 400
        assert mismatch.json()["error"]["code"] == -32020
    listing = next(
        t for t in misbehaving_server.CATALOGUE_TOOLS if t["name"] == "destructive_norequired"
    )
    assert "annotations" not in listing
    assert len(CATALOGUE_TOOL_NAMES) == 28


async def test_error_style_shapes() -> None:
    async def call(toggles: Toggles) -> httpx2.Response:
        async with asgi_client(misbehaving_server.build_app(toggles), lifespan=False) as c:
            return await post(
                c,
                "tools/call",
                {"name": "echo_required", "arguments": {}},
                headers=modern_headers("tools/call", mcp_name="echo_required"),
            )

    jsonrpc = await call(Toggles(error_style="jsonrpc"))
    assert jsonrpc.status_code == 400
    assert jsonrpc.json()["error"]["code"] == -32602
    assert "echo_required" in jsonrpc.json()["error"]["message"]
    assert parse_reply(await call(Toggles(error_style="success")))["result"]["isError"] is False
    assert (
        parse_reply(await call(Toggles(error_style="bare")))["result"]["content"][0]["text"]
        == "error"
    )
    assert (
        "Traceback"
        in parse_reply(await call(Toggles(error_style="traceback")))["result"]["content"][0]["text"]
    )
    legacy = await call(Toggles(error_style="jsonrpc", legacy_error_codes=True))
    assert legacy.json()["error"]["code"] == -32042


async def test_origin_modes_details() -> None:
    async def evil(toggles: Toggles, origin: str | None = EVIL) -> httpx2.Response:
        async with asgi_client(misbehaving_server.build_app(toggles), lifespan=False) as c:
            headers = modern_headers("server/discover", **({"origin": origin} if origin else {}))
            return await post(c, "server/discover", headers=headers)

    r = await evil(Toggles(origin_mode="reject_400_jsonrpc"))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32600
    assert r.json()["id"] == 1
    r = await evil(Toggles(origin_mode="reject_200_jsonrpc_error"))
    assert r.status_code == 200
    assert "error" in r.json()
    r = await evil(Toggles(origin_mode="accept_all_sse"))
    assert r.status_code == 200
    assert media_type(r) == "text/event-stream"
    r = await evil(Toggles(origin_mode="redirect_evil"))
    assert r.status_code == 302
    assert r.headers["location"] == "/mcp-redirected"
    r = await evil(Toggles(origin_mode="allowlist_app_origin"), "https://app.example.com")
    assert r.status_code == 200
    r = await evil(Toggles(origin_mode="require_origin"), None)
    assert r.status_code == 403
    r = await evil(Toggles(origin_mode="legacy_only"), None)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == -32601
    async with asgi_client(
        misbehaving_server.build_app(Toggles(origin_mode="cors_reflect")), lifespan=False
    ) as c:
        pre = await c.options(
            "/mcp", headers={"origin": EVIL, "access-control-request-method": "POST"}
        )
        assert pre.status_code == 204
        assert pre.headers["access-control-allow-origin"] == EVIL
        assert (
            await post(c, "server/discover", headers=modern_headers("server/discover", origin=EVIL))
        ).status_code == 403


async def test_raw_overrides() -> None:
    async def get(toggles: Toggles) -> httpx2.Response:
        async with asgi_client(misbehaving_server.build_app(toggles), lifespan=False) as c:
            return await post(c, "tools/list")

    r = await get(Toggles(status=401))
    assert r.status_code == 401
    assert r.content == b""
    r = await get(Toggles(status=404, content_type="text/html", body=b"<html>nope</html>"))
    assert r.status_code == 404
    assert media_type(r) == "text/html"
    assert r.text == "<html>nope</html>"
    r = await get(Toggles(status=400))
    assert r.status_code == 400
    assert r.content == b""
    r = await get(Toggles(status=200, content_type="application/json", body=b"{not json"))
    assert r.status_code == 200
    assert r.text == "{not json"
    r = await get(Toggles(status=307))
    assert r.status_code == 307
    assert r.headers["location"] == "/mcp-redirected"
    r = await get(Toggles(status=302, body=b"https://other.example.com/mcp"))
    assert r.status_code == 302
    assert r.headers["location"] == "https://other.example.com/mcp"
    r = await get(Toggles(status=202))
    assert r.status_code == 202


async def test_redirect_target_serves_the_endpoint() -> None:
    async with asgi_client(misbehaving_server.build_app(), lifespan=False) as c:
        r = await c.post(
            "/mcp-redirected", headers=modern_headers("tools/list"), json=modern_body("tools/list")
        )
    assert r.status_code == 200
    assert parse_reply(r)["result"]["resultType"] == "complete"


async def test_sse_toggle_and_accel_header() -> None:
    async with asgi_client(misbehaving_server.build_app(Toggles(sse=True)), lifespan=False) as c:
        r = await post(c, "tools/list")
        assert media_type(r) == "text/event-stream"
        assert r.headers["x-accel-buffering"] == "no"
        assert r.text.startswith("event: message\ndata: ")
        assert parse_reply(r)["result"]["resultType"] == "complete"
        err = await post(c, "mcpcheck/nonexistent")
        assert err.status_code == 404
        assert parse_reply(err)["error"]["code"] == -32601
    async with asgi_client(
        misbehaving_server.build_app(Toggles(sse=True, x_accel_buffering=False)), lifespan=False
    ) as c:
        r = await post(c, "tools/list")
        assert media_type(r) == "text/event-stream"
        assert "x-accel-buffering" not in r.headers


async def test_request_log_and_client_port() -> None:
    app = misbehaving_server.build_app()
    async with asgi_client(app, lifespan=False, client_port=41234) as c:
        r = await post(c, "tools/list")
        assert r.headers["x-client-port"] == "41234"
        await post(c, "server/discover", headers=modern_headers("server/discover", origin=EVIL))
        await c.get("/mcp")
        log = (await c.get("/__log")).json()
        assert [(e["verb"], e["method"]) for e in log] == [
            ("POST", "tools/list"),
            ("POST", "server/discover"),
            ("GET", None),
        ]
        assert log[0]["headers"]["mcp-method"] == "tools/list"
        assert (
            log[0]["body"]["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"]
            == "2026-07-28"
        )
        assert log[1]["headers"]["origin"] == EVIL
        assert all(e["client_port"] == 41234 for e in log)
        assert app.state.fixture.log is not None
        assert len(app.state.fixture.log) == 3
        assert (await c.delete("/__log")).status_code == 204
        assert (await c.get("/__log")).json() == []


def test_toggles_from_cli() -> None:
    toggles = toggles_from_cli(
        ["omit_ttl", "sse"],
        [
            "cache_scope=user",
            "unknown_method_status=200",
            "float_ttl=60000.0",
            "status=401",
            "body=hi",
            "pages=[]",
            "discover_variant=no-hints",
            "x_accel_buffering=false",
            "slow_seconds=1.5",
        ],
    )
    assert toggles == Toggles(
        omit_ttl=True,
        sse=True,
        cache_scope="user",
        unknown_method_status=200,
        float_ttl=60000.0,
        status=401,
        body=b"hi",
        pages=(),
        discover_variant="no-hints",
        x_accel_buffering=False,
        slow_seconds=1.5,
    )
    assert toggles_from_cli(
        [], ["status=none", 'tools=[{"name":"t","inputSchema":{}}]']
    ) == Toggles(status=None, tools=({"name": "t", "inputSchema": {}},))
    with pytest.raises(ValueError, match="unknown toggle"):
        toggles_from_cli(["nope"], [])
    with pytest.raises(ValueError, match="not boolean"):
        toggles_from_cli(["cache_scope"], [])
    with pytest.raises(ValueError, match="FIELD=VALUE"):
        toggles_from_cli([], ["nope=1"])
    with pytest.raises(ValueError, match="boolean"):
        toggles_from_cli([], ["sse=maybe"])


def test_default_toggles_equal_and_match_reference() -> None:
    assert Toggles() == Toggles()
    assert Toggles().tools == misbehaving_server.DEFAULT_TOOLS
    assert [t["name"] for t in misbehaving_server.DEFAULT_TOOLS] == list(
        reference_server.TOOL_NAMES
    )


# ----------------------------------------------------------------- TCP-only behaviour


@pytest.mark.integration
async def test_tcp_fresh_connections_have_distinct_client_ports(
    misbehaving_url: Callable[[Toggles], str],
) -> None:
    url = misbehaving_url(Toggles())
    ports = set()
    for _ in range(3):
        async with httpx2.AsyncClient(timeout=5) as c:
            r = await c.post(
                url, headers=modern_headers("tools/list"), json=modern_body("tools/list")
            )
            ports.add(r.headers["x-client-port"])
    assert len(ports) == 3
    log = fetch_request_log(url)
    assert len({e["client_port"] for e in log}) == 3
    reset_request_log(url)
    assert fetch_request_log(url) == []


@pytest.mark.integration
async def test_tcp_reset_evil_closes_connection(misbehaving_url: Callable[[Toggles], str]) -> None:
    url = misbehaving_url(Toggles(origin_mode="reset_evil"))
    async with httpx2.AsyncClient(timeout=5) as c:
        with pytest.raises(httpx2.RemoteProtocolError):
            await c.post(
                url,
                headers=modern_headers("server/discover", origin=EVIL),
                json=modern_body("server/discover"),
            )
        ok = await c.post(
            url, headers=modern_headers("server/discover"), json=modern_body("server/discover")
        )
        assert ok.status_code == 200


@pytest.mark.integration
@pytest.mark.slow
async def test_tcp_hang_and_slow_time_out(misbehaving_url: Callable[[Toggles], str]) -> None:
    hang = misbehaving_url(Toggles(origin_mode="hang_evil"))
    started = time.monotonic()
    async with httpx2.AsyncClient(timeout=httpx2.Timeout(0.5)) as c:
        with pytest.raises(httpx2.ReadTimeout):
            await c.post(
                hang,
                headers=modern_headers("server/discover", origin=EVIL),
                json=modern_body("server/discover"),
            )
        assert (
            await c.post(
                hang, headers=modern_headers("server/discover"), json=modern_body("server/discover")
            )
        ).status_code == 200
    assert time.monotonic() - started < 3
    slow = misbehaving_url(Toggles(slow_seconds=1.0))
    async with httpx2.AsyncClient(timeout=httpx2.Timeout(0.3)) as c:
        with pytest.raises(httpx2.ReadTimeout):
            await c.post(slow, headers=modern_headers("tools/list"), json=modern_body("tools/list"))
    async with httpx2.AsyncClient(timeout=httpx2.Timeout(5)) as c:
        assert (
            await c.post(slow, headers=modern_headers("tools/list"), json=modern_body("tools/list"))
        ).status_code == 200


@pytest.mark.integration
@pytest.mark.slow
async def test_tcp_endless_and_lingering_sse(misbehaving_url: Callable[[Toggles], str]) -> None:
    # Keepalive pings defeat per-chunk read timeouts (PLAN.md section 6.4), so a total
    # budget is the only thing that bounds these streams - exactly what mcpcheck must do.
    endless = misbehaving_url(Toggles(endless_sse=True))
    started = time.monotonic()
    async with httpx2.AsyncClient(timeout=httpx2.Timeout(5)) as c:
        with pytest.raises(TimeoutError), anyio.fail_after(1.0):
            await c.post(
                endless, headers=modern_headers("tools/list"), json=modern_body("tools/list")
            )
    assert time.monotonic() - started < 3
    lingering = misbehaving_url(Toggles(discover_variant="sse-not-terminated"))
    async with (
        httpx2.AsyncClient(timeout=httpx2.Timeout(5)) as c,
        c.stream(
            "POST",
            lingering,
            headers=modern_headers("server/discover"),
            json=modern_body("server/discover"),
        ) as response,
    ):
        assert response.status_code == 200
        assert media_type(response) == "text/event-stream"
        chunks = response.aiter_bytes()
        first = await chunks.__anext__()
        assert b'"resultType":"complete"' in first
        received: list[bytes] = []

        async def drain() -> None:
            async for chunk in chunks:
                received.append(chunk)  # noqa: PERF401 - partial results must survive the timeout

        with pytest.raises(TimeoutError), anyio.fail_after(1.0):
            await drain()
        assert len(received) >= 2
        assert all(chunk.startswith(b": ping") for chunk in received)


@pytest.mark.integration
async def test_tcp_session_header_wire_casing(misbehaving_url: Callable[[Toggles], str]) -> None:
    url = misbehaving_url(Toggles(mint_session_id=True))
    async with httpx2.AsyncClient(timeout=5) as c:
        r = await c.post(url, headers=modern_headers("tools/list"), json=modern_body("tools/list"))
    assert (b"Mcp-Session-Id", SESSION_ID.encode()) in r.headers.raw


@pytest.mark.integration
async def test_tcp_reference_json_mode(reference_url_json: str) -> None:
    async with httpx2.AsyncClient(timeout=5) as c:
        r = await c.post(
            reference_url_json, headers=modern_headers("tools/list"), json=modern_body("tools/list")
        )
        legacy = await c.post(
            reference_url_json,
            headers={
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
    assert media_type(r) == "application/json"
    assert media_type(legacy) == "application/json"


@pytest.mark.integration
async def test_tcp_reference_factory_require_token(
    reference_url_factory: Callable[..., str],
) -> None:
    url = reference_url_factory(require_token="tok")
    assert url == reference_url_factory(require_token="tok")
    async with httpx2.AsyncClient(timeout=5) as c:
        denied = await c.post(
            url, headers=modern_headers("tools/list"), json=modern_body("tools/list")
        )
        ok = await c.post(
            url,
            headers=modern_headers("tools/list", authorization="Bearer tok"),
            json=modern_body("tools/list"),
        )
    assert denied.status_code == 401
    assert ok.status_code == 200


# ----------------------------------------------------------------- hygiene


@pytest.mark.integration
def test_thread_server_starts_and_stops_without_leaks() -> None:
    before = {t.name for t in threading.enumerate() if t.name == "uvicorn-fixture"}
    server = start_misbehaving_server(Toggles())
    assert bool(server.running)  # bool(): keep mypy from narrowing the property
    with httpx2.Client(timeout=15) as c:
        assert (
            c.post(
                server.url, headers=modern_headers("tools/list"), json=modern_body("tools/list")
            ).status_code
            == 200
        )
    port = server.port
    server.stop()
    assert not server.running
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", port), timeout=0.5)
    after = {t.name for t in threading.enumerate() if t.name == "uvicorn-fixture"}
    assert after == before
    with (
        ThreadServer(reference_server.build_app(), lifespan="on") as ref,
        httpx2.Client(timeout=15) as c,
    ):
        assert (
            c.post(
                ref.url, headers=modern_headers("tools/list"), json=modern_body("tools/list")
            ).status_code
            == 200
        )
    assert not ref.running


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize(
    ("module", "args", "banner"),
    [
        ("tests.fixtures.reference_server", [], "mcpcheck reference server 0.1.0 listening on "),
        (
            "tests.fixtures.misbehaving_server",
            ["--toggle", "omit_ttl", "--set", "cache_scope=user"],
            "mcpcheck misbehaving server 0.1.0 listening on ",
        ),
    ],
    ids=["reference", "misbehaving"],
)
def test_standalone_entry_point_prints_url_and_serves(
    module: str, args: list[str], banner: str
) -> None:
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, our own module
        [sys.executable, "-m", module, "--port", "0", *args],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stderr is not None
        line = proc.stderr.readline()
        assert line.startswith(banner), line
        match = re.search(r"(http://127\.0\.0\.1:(\d+)/mcp)", line)
        assert match is not None
        url, port = match.group(1), int(match.group(2))
        assert port != 0
        with httpx2.Client(timeout=15) as c:
            r = c.post(url, headers=modern_headers("tools/list"), json=modern_body("tools/list"))
        assert r.status_code == 200
        result = parse_reply(r)["result"]
        if "misbehaving" in module:
            assert "ttlMs" not in result
            assert result["cacheScope"] == "user"
        else:
            assert result["ttlMs"] == 300000
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if proc.stderr is not None:
            proc.stderr.close()
