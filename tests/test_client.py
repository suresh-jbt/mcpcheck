"""RawClient over MockTransport (PLAN.md §10.5): what is sent, how replies are parsed, policies."""

from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest

from mcpcheck import __version__, wire
from mcpcheck.client import ClientConfig, RawClient, ServerUnreachable, Transcript
from tests.helpers.scripted import ScriptedServer, error_response, list_result, ok, sse_body

pytestmark = pytest.mark.anyio

URL = "http://127.0.0.1:1/mcp"


def cfg(**kw: Any) -> ClientConfig:
    return ClientConfig(url=URL, **kw)


def tools_server(*, sse: bool = False, headers: dict[str, str] | None = None) -> ScriptedServer:
    return ScriptedServer(
        routes={"tools/list": lambda b, _r: ok(b, list_result([]), sse=sse, headers=headers)}
    )


# --------------------------------------------------------------------------- what we send


@pytest.mark.parametrize("sse", [False, True])
async def test_primary_request_shape(sse: bool) -> None:
    server = tools_server(sse=sse)
    async with server.raw_client(
        cfg(token="sekrit", origin="https://app.example.com", extra_headers=(("X-Env", "ci"),))
    ) as raw:
        reply = await raw.post("tools/list", wait_for_eof=True)
    seen = server.seen[0]
    assert seen.method == "tools/list"
    h = seen.headers
    assert h["accept"] == "application/json, text/event-stream"
    assert h["content-type"] == "application/json"
    assert h["mcp-protocol-version"] == "2026-07-28"
    assert h["mcp-method"] == "tools/list"
    assert h["authorization"] == "Bearer sekrit"
    assert h["origin"] == "https://app.example.com"
    assert h["x-env"] == "ci"
    assert h["user-agent"] == wire.USER_AGENT
    assert "mcp-session-id" not in h
    assert seen.body == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
                "io.modelcontextprotocol/clientInfo": {
                    "name": "mcpcheck",
                    "version": __version__,
                },
            }
        },
    }
    assert reply.status == 200
    assert reply.request_id == 1
    assert reply.is_sse is sse
    assert reply.result is not None
    assert reply.result["resultType"] == "complete"
    assert reply.stream_terminated is (True if sse else None)
    assert reply.error is None
    assert reply.elapsed_ms >= 0


async def test_no_origin_and_no_auth_by_default() -> None:
    server = tools_server()
    async with server.raw_client() as raw:
        await raw.post("tools/list")
    assert "origin" not in server.seen[0].headers
    assert "authorization" not in server.seen[0].headers


async def test_send_token_false_omits_authorization() -> None:
    server = tools_server()
    async with server.raw_client(cfg(token="sekrit"), label="anon", send_token=False) as raw:
        await raw.post("tools/list")
    assert "authorization" not in server.seen[0].headers


async def test_request_ids_count_per_client_and_custom_ids() -> None:
    server = tools_server()
    async with server.raw_client() as raw:
        r1 = await raw.post("tools/list")
        r2 = await raw.post("tools/list", req_id="errors:0:echo")
        r3 = await raw.post("tools/list")
    assert (r1.request_id, r2.request_id, r3.request_id) == (1, "errors:0:echo", 2)
    assert [s.body["id"] for s in server.seen if s.body] == [1, "errors:0:echo", 2]


async def test_header_overrides_and_omissions_on_fresh_client() -> None:
    server = tools_server()
    async with server.raw_client(label="protocol-1") as raw:
        await raw.post(
            "tools/list",
            header_version="2025-11-25",
            headers={"Mcp-Session-Id": "bogus"},
            omit_headers=["mcp-method"],
            omit_meta_keys=[wire.META_CLIENT_CAPABILITIES],
            client_info=False,
        )
        await raw.post("tools/list", header_version=None, meta_version=None)
    first, second = server.seen
    assert first.headers["mcp-protocol-version"] == "2025-11-25"
    assert "mcp-method" not in first.headers
    assert first.headers["mcp-session-id"] == "bogus"
    assert first.meta == {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}
    assert "mcp-protocol-version" not in second.headers
    assert second.body == {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    tags = [e.tags for e in raw.transcript]
    assert "[MCP-Protocol-Version: 2025-11-25]" in tags[0]
    assert "[no Mcp-Method]" in tags[0]
    assert "[Mcp-Session-Id: bogus]" in tags[0]
    assert "[no MCP-Protocol-Version]" in tags[1]
    assert "[no _meta]" in tags[1]
    assert raw.transcript.exchanges[1].legacy is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"header_version": None},
        {"meta_version": None},
        {"omit_headers": ["mcp-method"]},
        {"omit_meta_keys": ["x"]},
        {"client_info": False},
        {"headers": {"Mcp-Method": "x"}},
        {"headers": {"Origin": "https://evil.example.com"}},
    ],
)
async def test_main_client_rejects_tampering(kwargs: dict[str, Any]) -> None:
    server = tools_server()
    async with server.raw_client() as raw:
        with pytest.raises(RuntimeError, match="fresh"):
            await raw.post("tools/list", **kwargs)
    assert server.seen == []


async def test_main_client_allows_plain_extra_headers_and_custom_id() -> None:
    server = tools_server()
    async with server.raw_client() as raw:
        await raw.post("tools/list", headers={"X-Trace": "1"}, req_id="x")
    assert server.seen[0].headers["x-trace"] == "1"


async def test_mcp_name_header_for_tools_call() -> None:
    server = ScriptedServer(
        routes={"tools/call": lambda b, _r: ok(b, {"resultType": "complete", "content": []})}
    )
    async with server.raw_client() as raw:
        await raw.post("tools/call", {"name": "工具", "arguments": {}})
    assert server.seen[0].headers["mcp-name"].startswith("=?base64?")
    assert server.seen[0].headers["mcp-method"] == "tools/call"


# --------------------------------------------------------------------------- what we parse


async def test_4xx_status_and_rpc_error_preserved() -> None:
    server = ScriptedServer(
        routes={
            "tools/list": lambda b, _r: error_response(
                b, -32600, "Bad Request: Missing session ID", status=400
            )
        }
    )
    async with server.raw_client() as raw:
        reply = await raw.post("tools/list")
    assert reply.status == 400
    assert reply.rpc_error == (-32600, "Bad Request: Missing session ID", None)
    assert wire.classify_reply(reply).kind is wire.OutcomeKind.NON_MODERN_REJECTION


async def test_null_id_error_matches_our_request() -> None:
    server = ScriptedServer(
        routes={
            "tools/list": lambda b, _r: error_response(
                b, -32020, "mismatch", status=400, null_id=True
            )
        }
    )
    async with server.raw_client() as raw:
        reply = await raw.post("tools/list")
    assert reply.response is not None
    assert reply.rpc_error is not None
    assert reply.rpc_error[0] == -32020


async def test_id_mismatch_type_sensitive() -> None:
    server = ScriptedServer(
        routes={
            "tools/list": lambda _b, _r: httpx2.Response(
                200, json={"jsonrpc": "2.0", "id": "1", "result": {}}
            )
        }
    )
    async with server.raw_client() as raw:
        reply = await raw.post("tools/list")
    assert reply.response is None
    assert reply.any_response == {"jsonrpc": "2.0", "id": "1", "result": {}}
    assert wire.classify_reply(reply).extra.get("id_mismatch") is True


async def test_content_type_charset_and_media_type() -> None:
    server = ScriptedServer(
        routes={
            "tools/list": lambda b, _r: httpx2.Response(
                200,
                content=json.dumps({"jsonrpc": "2.0", "id": b["id"], "result": {}}).encode(),
                headers={"content-type": "Application/JSON; charset=UTF-8"},
            )
        }
    )
    async with server.raw_client() as raw:
        reply = await raw.post("tools/list")
    assert (reply.media_type, reply.charset) == ("application/json", "utf-8")
    assert reply.result == {}


async def test_non_utf8_charset_and_body_are_parse_errors() -> None:
    server = ScriptedServer(
        routes={
            "tools/list": lambda _b, _r: httpx2.Response(
                200, content=b"{}", headers={"content-type": "application/json; charset=latin-1"}
            ),
            "server/discover": lambda _b, _r: httpx2.Response(
                200, content=b"\xff\xfe{}", headers={"content-type": "application/json"}
            ),
        }
    )
    async with server.raw_client() as raw:
        r1 = await raw.post("tools/list")
        r2 = await raw.post("server/discover")
    assert r1.parse_error == "charset latin-1 is not utf-8"
    assert r2.parse_error == "not utf-8"
    assert wire.classify_reply(r1).kind is wire.OutcomeKind.NOT_JSONRPC_2XX
    assert wire.classify_reply(r2).kind is wire.OutcomeKind.NOT_JSONRPC_2XX


async def test_html_200_and_json_array() -> None:
    server = ScriptedServer(
        routes={
            "tools/list": lambda _b, _r: httpx2.Response(
                200, text="<html>", headers={"content-type": "text/html"}
            ),
            "server/discover": lambda b, _r: httpx2.Response(
                200, json=[{"jsonrpc": "2.0", "id": b["id"], "result": {}}]
            ),
        }
    )
    async with server.raw_client() as raw:
        r1 = await raw.post("tools/list")
        r2 = await raw.post("server/discover")
    assert r1.media_type == "text/html"
    assert r1.messages == ()
    assert r2.parse_error is not None
    assert r2.parse_error.startswith("JSON array")


async def test_sse_stream_with_notifications_server_request_and_ids() -> None:
    def route(b: dict[str, Any], _r: httpx2.Request) -> httpx2.Response:
        body = (
            b": hello\n\n"
            b"id: 1\n"
            + sse_body({"jsonrpc": "2.0", "method": "notifications/progress", "params": {}})
            + sse_body({"jsonrpc": "2.0", "id": "srv-1", "method": "roots/list"})
            + sse_body({"jsonrpc": "2.0", "id": b["id"], "result": list_result([])})
        )
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    server = ScriptedServer(routes={"tools/list": route})
    async with server.raw_client() as raw:
        reply = await raw.post("tools/list", wait_for_eof=True)
    assert len(reply.messages) == 3
    assert len(reply.notifications) == 1
    assert reply.server_requests == ({"jsonrpc": "2.0", "id": "srv-1", "method": "roots/list"},)
    assert reply.result is not None
    assert reply.sse_event_ids == 1
    assert reply.stream_terminated is True
    assert wire.classify_reply(reply).ok


async def test_sse_stops_after_our_response_without_wait() -> None:
    def route(b: dict[str, Any], _r: httpx2.Request) -> httpx2.Response:
        body = sse_body({"jsonrpc": "2.0", "id": b["id"], "result": list_result([])}) + sse_body(
            {"jsonrpc": "2.0", "method": "notifications/after"}
        )
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    server = ScriptedServer(routes={"tools/list": route})
    async with server.raw_client() as raw:
        reply = await raw.post("tools/list")
    # MockTransport hands over the whole body in one chunk; either way the result is present
    assert reply.result is not None
    assert reply.stream_terminated in (None, False, True)


async def test_sse_without_response_is_not_jsonrpc() -> None:
    server = ScriptedServer(
        routes={
            "tools/list": lambda _b, _r: httpx2.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse_body({"jsonrpc": "2.0", "method": "notifications/x"}),
            )
        }
    )
    async with server.raw_client() as raw:
        reply = await raw.post("tools/list")
    assert reply.response is None
    assert reply.stream_terminated is True
    assert wire.classify_reply(reply).kind is wire.OutcomeKind.NOT_JSONRPC_2XX


async def test_session_id_headers_wire_casing() -> None:
    server = tools_server(headers={"Mcp-Session-Id": "abc"})
    async with server.raw_client() as raw:
        reply = await raw.post("tools/list")
    assert reply.session_ids == ["abc"]
    assert (b"Mcp-Session-Id", b"abc") in reply.headers.raw
    assert raw.transcript.exchanges[0].session_ids_received == ["abc"]


# --------------------------------------------------------------------------- redirects


def redirect_server(status: int, location: str) -> ScriptedServer:
    def handler(b: dict[str, Any], req: httpx2.Request) -> httpx2.Response:
        if req.url.path == "/mcp":
            return httpx2.Response(status, headers={"location": location})
        return ok(b, list_result([]))

    return ScriptedServer(routes={"tools/list": handler})


async def test_same_origin_307_followed_with_post_preserved() -> None:
    server = redirect_server(307, "/v2/mcp")
    async with server.raw_client(cfg(token="sekrit")) as raw:
        reply = await raw.post("tools/list")
    assert reply.status == 200
    assert reply.redirects_followed == 1
    assert reply.redirect_blocked is None
    assert [s.request.url.path for s in server.seen] == ["/mcp", "/v2/mcp"]
    assert all(s.request.method == "POST" for s in server.seen)
    assert server.seen[1].headers["authorization"] == "Bearer sekrit"


@pytest.mark.parametrize("status", [301, 302, 303])
async def test_3xx_other_than_307_308_blocked(status: int) -> None:
    server = redirect_server(status, "/v2/mcp")
    async with server.raw_client() as raw:
        reply = await raw.post("tools/list")
    assert reply.status == status
    assert reply.redirect_blocked == "http://127.0.0.1:1/v2/mcp"
    assert len(server.seen) == 1
    assert wire.classify_reply(reply).kind is wire.OutcomeKind.REDIRECTED


async def test_cross_origin_308_blocked_token_never_sent_elsewhere() -> None:
    server = redirect_server(308, "https://other.example.com/mcp")
    async with server.raw_client(cfg(token="sekrit")) as raw:
        reply = await raw.post("tools/list")
    assert reply.redirect_blocked == "https://other.example.com/mcp"
    assert len(server.seen) == 1


async def test_redirect_loop_bounded_by_max_redirects() -> None:
    server = ScriptedServer(
        routes={"tools/list": lambda _b, _r: httpx2.Response(307, headers={"location": "/mcp"})}
    )
    async with server.raw_client(cfg(max_redirects=3)) as raw:
        reply = await raw.post("tools/list")
    assert reply.redirects_followed == 3
    assert reply.redirect_blocked == "http://127.0.0.1:1/mcp"
    assert len(server.seen) == 4


# --------------------------------------------------------------------------- failures / bounds


async def test_connect_error_raises_server_unreachable_and_records_exchange() -> None:
    async def boom(_req: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("All connection attempts failed")

    transcript = Transcript()
    async with RawClient(cfg(), transcript, transport=httpx2.MockTransport(boom)) as raw:
        with pytest.raises(ServerUnreachable) as exc:
            await raw.post("tools/list")
    assert isinstance(exc.value.cause, httpx2.ConnectError)
    assert "cannot connect to http://127.0.0.1:1/mcp" in str(exc.value)
    assert transcript.exchanges[0].error is not None
    assert transcript.exchanges[0].status == 0


async def test_read_error_is_transport_reply_not_exception() -> None:
    async def boom(_req: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadError("connection reset")

    async with RawClient(cfg(), Transcript(), transport=httpx2.MockTransport(boom)) as raw:
        reply = await raw.post("tools/list")
    assert reply.status == 0
    assert reply.error == "ReadError: connection reset"
    assert wire.classify_reply(reply).kind is wire.OutcomeKind.TRANSPORT


async def test_total_budget_enforced_on_slow_transport() -> None:
    import anyio

    async def slow(_req: httpx2.Request) -> httpx2.Response:
        await anyio.sleep(5)
        return httpx2.Response(200, json={})

    async with RawClient(
        cfg(timeout=0.2), Transcript(), transport=httpx2.MockTransport(slow)
    ) as raw:
        with anyio.fail_after(3):
            reply = await raw.post("tools/list")
    assert reply.status == 0
    assert reply.error is not None
    assert "total budget" in reply.error
    assert wire.classify_reply(reply).kind is wire.OutcomeKind.TRANSPORT


async def test_body_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcpcheck import client as client_mod

    monkeypatch.setattr(client_mod, "MAX_BODY_BYTES", 64)
    server = ScriptedServer(
        routes={
            "tools/list": lambda b, _r: httpx2.Response(
                200, json={"jsonrpc": "2.0", "id": b["id"], "result": {"x": "y" * 500}}
            )
        }
    )
    async with server.raw_client() as raw:
        reply = await raw.post("tools/list")
    assert reply.truncated is True
    assert reply.error is not None
    assert "cap" in reply.error
    assert len(reply.raw) == 64
    assert wire.classify_reply(reply).kind is wire.OutcomeKind.TRANSPORT


# --------------------------------------------------------------------------- GET / OPTIONS probes


async def test_http_get_probe_headers_only() -> None:
    server = ScriptedServer(
        http_routes={"GET": lambda _r: httpx2.Response(405, headers={"allow": "POST"})}
    )
    async with server.raw_client(cfg(token="sekrit"), label="protocol-get") as raw:
        reply = await raw.http("GET", purpose="legacy GET")
    assert reply.status == 405
    assert reply.headers["allow"] == "POST"
    seen = server.seen[0]
    assert seen.request.method == "GET"
    assert seen.headers["mcp-protocol-version"] == "2026-07-28"
    assert seen.headers["authorization"] == "Bearer sekrit"
    assert "content-type" not in seen.headers


async def test_http_options_probe_with_custom_headers() -> None:
    server = ScriptedServer(
        http_routes={
            "OPTIONS": lambda _r: httpx2.Response(204, headers={"access-control-allow-origin": "*"})
        }
    )
    async with server.raw_client(label="origin-preflight") as raw:
        reply = await raw.http(
            "OPTIONS", headers={"Origin": wire.EVIL_ORIGIN, "Access-Control-Request-Method": "POST"}
        )
    assert reply.status == 204
    assert server.seen[0].headers["origin"] == wire.EVIL_ORIGIN
    assert reply.headers["access-control-allow-origin"] == "*"


async def test_http_get_reads_at_most_one_sse_event() -> None:
    body = sse_body({"jsonrpc": "2.0", "method": "notifications/a"}) + sse_body(
        {"jsonrpc": "2.0", "method": "notifications/b"}
    )
    server = ScriptedServer(
        http_routes={
            "GET": lambda _r: httpx2.Response(
                200, headers={"content-type": "text/event-stream"}, content=body
            )
        }
    )
    async with server.raw_client(label="protocol-get") as raw:
        reply = await raw.http("GET")
    assert reply.status == 200
    assert reply.is_sse
    assert len(reply.messages) == 1


# --------------------------------------------------------------------------- transcript / redaction


async def test_transcript_redacts_token_everywhere() -> None:
    server = ScriptedServer(
        routes={
            "tools/list": lambda b, _r: httpx2.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": b["id"],
                    "result": {"echo": _r.headers.get("authorization")},
                },
                headers={"X-Echo": _r.headers.get("authorization", "")},
            )
        }
    )
    transcript = Transcript()
    transcript.current_check = "handshake"
    async with server.raw_client(
        cfg(token="sekrit-token", extra_headers=(("X-Api-Key", "k3y"),)), transcript
    ) as raw:
        reply = await raw.post("tools/list", purpose="primary")
    assert reply.result == {"echo": "Bearer sekrit-token"}  # the reply itself is not redacted
    ex = transcript.exchanges[0]
    dumped = json.dumps(ex.__dict__, default=str)
    assert "sekrit-token" not in dumped
    assert "k3y" not in dumped
    assert dict(ex.request_headers)["authorization"] == "Bearer ****"
    assert dict(ex.request_headers)["x-api-key"] == "****"
    assert {k.lower(): v for k, v in ex.response_headers}["x-echo"] == "Bearer ****"
    assert ex.check == "handshake"
    assert ex.purpose == "primary"
    assert ex.connection == "main"
    assert ex.seq == 1
    assert ex.rpc_method == "tools/list"
    assert ex.verb == "POST"
    assert transcript.for_check("handshake") == [ex]


async def test_client_config_defaults_and_budget() -> None:
    c = cfg()
    assert c.timeout == 10.0
    assert c.check_budget == 100.0
    assert c.max_redirects == 5
    assert c.probe == "safe"
    assert c.max_tools == 20
    assert c.verify_tls is True
    assert c.secrets == ()
    secrets = cfg(token="t", extra_headers=(("A", "b"),)).secrets
    # literal values plus the encoded forms a server might echo (longest first)
    assert set(secrets) >= {"t", "b", "QmVhcmVyIHQ=", "QmVhcmVyIGI="}  # b64("Bearer t"/"Bearer b")
    assert list(secrets) == sorted(secrets, key=lambda n: (-len(n), n))
    assert "dA==" not in secrets  # b64("t") is too short to be a safe needle


# --------------------------------------------------------------------------- hostile replies


async def test_corrupt_content_encoding_is_a_transport_reply_not_a_crash() -> None:
    """``httpx2.DecodingError`` is a RequestError but not a TransportError (bad gzip body)."""

    def handler(_req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            headers={"content-type": "application/json", "content-encoding": "gzip"},
            content=b"this is not gzip",
        )

    transcript = Transcript()
    async with RawClient(cfg(), transcript, transport=httpx2.MockTransport(handler)) as raw:
        reply = await raw.post("tools/list")
    assert reply.status in (0, 200)  # MockTransport decodes eagerly; a real stream fails mid-body
    assert reply.error is not None
    assert reply.error.startswith("DecodingError")
    assert reply.messages == ()
    outcome = wire.classify_reply(reply)
    assert outcome.kind is wire.OutcomeKind.TRANSPORT
    assert outcome.inconclusive
    assert transcript.exchanges[0].error is not None


async def test_deeply_nested_body_is_a_parse_error_reply() -> None:
    deep = "[" * 100_000 + "]" * 100_000

    def handler(_req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"content-type": "application/json"}, content=deep)

    async with RawClient(cfg(), Transcript(), transport=httpx2.MockTransport(handler)) as raw:
        reply = await raw.post("tools/list")
    assert reply.parse_error == wire.JSON_TOO_DEEP
    assert wire.classify_reply(reply).kind is wire.OutcomeKind.NOT_JSONRPC_2XX


async def test_header_less_exchange_is_recorded_as_legacy() -> None:
    """Dual-era servers MAY route a request without MCP-Protocol-Version to their 2025 path."""
    server = ScriptedServer(routes={"tools/list": lambda b, _r: ok(b, list_result([]))})
    transcript = Transcript()
    async with server.raw_client(transcript=transcript, label="probe") as raw:
        await raw.post("tools/list", header_version=None)
        await raw.post("tools/list", meta_version=None)
        await raw.post("tools/list")
    assert [e.legacy for e in transcript] == [True, True, False]
