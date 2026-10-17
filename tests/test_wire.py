"""Pure tests for ``mcpcheck.wire`` (PLAN.md §10.5): parsing, headers, redaction, classification."""

from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import quote, quote_plus

import httpx2
import pytest

from mcpcheck import __version__, wire
from mcpcheck.client import RawReply
from mcpcheck.wire import OutcomeKind

# --------------------------------------------------------------------------- envelope / headers


def test_envelope_modern_shape() -> None:
    body = wire.envelope("tools/list", req_id=1)
    assert body == {
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
    # compact, UTF-8, no spaces
    assert wire.encode_body(body).startswith(b'{"jsonrpc":"2.0","id":1,"method":"tools/list"')


def test_envelope_legacy_and_notification() -> None:
    legacy = wire.envelope(
        "initialize", {"protocolVersion": "2025-11-25"}, req_id=2, meta_version=None
    )
    assert "_meta" not in legacy["params"]
    assert legacy["id"] == 2
    bare = wire.envelope("tools/list", req_id=3, meta_version=None)
    assert "params" not in bare
    notif = wire.envelope("notifications/x", req_id=None)
    assert "id" not in notif


def test_envelope_omit_meta_keys_and_client_info() -> None:
    body = wire.envelope(
        "tools/list", req_id=1, client_info_=False, omit_meta_keys=[wire.META_CLIENT_CAPABILITIES]
    )
    meta = body["params"]["_meta"]
    assert wire.META_CLIENT_INFO not in meta
    assert wire.META_CLIENT_CAPABILITIES not in meta
    assert meta[wire.META_PROTOCOL_VERSION] == "2026-07-28"


def test_default_headers_tools_list() -> None:
    h = wire.default_headers(wire.envelope("tools/list", req_id=1))
    assert h["accept"] == "application/json, text/event-stream"
    assert h["content-type"] == "application/json"
    assert h["mcp-protocol-version"] == "2026-07-28"
    assert h["mcp-method"] == "tools/list"
    assert "mcp-name" not in h
    assert h["user-agent"].startswith("mcpcheck/")


def test_default_headers_mcp_name_base64_sentinel() -> None:
    h = wire.default_headers(
        wire.envelope("tools/call", {"name": "héllo", "arguments": {}}, req_id=1)
    )
    assert h["mcp-name"] == "=?base64?aMOpbGxv?="
    h2 = wire.default_headers(
        wire.envelope("tools/call", {"name": "plain", "arguments": {}}, req_id=1)
    )
    assert h2["mcp-name"] == "plain"
    h3 = wire.default_headers(wire.envelope("resources/read", {"uri": "file:///x"}, req_id=1))
    assert h3["mcp-name"] == "file:///x"


def test_default_headers_omit_version() -> None:
    h = wire.default_headers(wire.envelope("tools/list", req_id=1), header_version=None)
    assert "mcp-protocol-version" not in h


def test_merge_headers_case_insensitive() -> None:
    merged = wire.merge_headers(
        {"Mcp-Method": "tools/list", "Accept": "x"}, {"MCP-METHOD": "Tools/List"}, ["accept"]
    )
    assert merged == {"mcp-method": "Tools/List"}


# --------------------------------------------------------------------------- content-type / JSON


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ("", None)),
        ("application/json", ("application/json", None)),
        ("Application/JSON; charset=UTF-8", ("application/json", "utf-8")),
        ('text/event-stream; charset="utf-8"', ("text/event-stream", "utf-8")),
        ("text/html;charset=latin-1", ("text/html", "latin-1")),
    ],
)
def test_classify_content_type(value: str | None, expected: tuple[str, str | None]) -> None:
    assert wire.classify_content_type(value) == expected


def test_parse_json_body_object() -> None:
    msgs, err = wire.parse_json_body(b'{"jsonrpc":"2.0","id":1,"result":{}}')
    assert err is None
    assert msgs == ({"jsonrpc": "2.0", "id": 1, "result": {}},)


@pytest.mark.parametrize(
    ("raw", "err"),
    [
        (b"", "empty body"),
        (b"   ", "empty body"),
        (b"{not json", "invalid JSON"),
        (b"[{}]", "JSON array (batching is not part of 2026-07-28)"),
        (b'"str"', "not a JSON object"),
        (b"\xff\xfe", "not utf-8"),
    ],
)
def test_parse_json_body_errors(raw: bytes, err: str) -> None:
    msgs, got = wire.parse_json_body(raw)
    assert msgs == ()
    assert got == err


def test_parse_json_body_bom_ok() -> None:
    msgs, err = wire.parse_json_body(b"\xef\xbb\xbf{}")
    assert err is None
    assert msgs == ({},)


# --------------------------------------------------------------------------- SSE


def test_sse_multi_line_crlf_comments_and_ids() -> None:
    body = (
        b"event: message\r\n"
        b'data: {"jsonrpc":"2.0",\r\n'
        b'data: "id":1,"result":{}}\r\n'
        b"\r\n"
        b": keepalive\n"
        b"\n"
        b"id: 7\n"
        b'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n'
        b"\n"
        b"retry: 1000\n"
        b"data: not json\n\n"
    )
    events = wire.parse_sse_events(body)
    assert [e.event for e in events] == ["message", "message", "message"]
    assert json.loads(events[0].data) == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert events[1].id == "7"
    msgs, err = wire.sse_messages(events)
    assert len(msgs) == 2
    assert err == "invalid JSON in SSE data"


def test_sse_decoder_incremental_split_crlf_and_trailing_partial() -> None:
    dec = wire.SseDecoder()
    assert dec.feed(b'data: {"a":1}\r') == []  # lone CR may be half of CRLF
    assert dec.feed(b"\n\r\n") == [wire.SseEvent(data='{"a":1}')]
    assert dec.feed(b'data: {"b":') == []
    assert dec.has_partial_event
    # a trailing unterminated line is processed on flush, but the event never completed
    assert dec.flush() == []
    assert dec.id_fields == 0


def test_sse_cr_only_line_endings() -> None:
    events = wire.parse_sse_events(b"data: x\rdata: y\r\r")
    assert events == [wire.SseEvent(data="x\ny")]


def test_sse_event_without_data_is_dropped_by_sse_messages() -> None:
    events = wire.parse_sse_events(b"event: ping\n\n")
    assert events == [wire.SseEvent(data="", event="ping")]
    assert wire.sse_messages(events) == ((), None)


def test_sse_non_object_data() -> None:
    msgs, err = wire.sse_messages(wire.parse_sse_events(b"data: [1]\n\n"))
    assert msgs == ()
    assert err == "SSE data is not a JSON object"


# --------------------------------------------------------------------------- message helpers


def test_message_helpers() -> None:
    assert wire.is_response({"id": 1, "result": {}})
    assert wire.is_response({"id": None, "error": {"code": 1}})
    assert wire.is_server_request({"id": 5, "method": "roots/list"})
    assert not wire.is_server_request({"method": "notifications/x"})
    assert wire.is_notification({"method": "notifications/x"})
    assert wire.rpc_error_of({"error": {"code": -32601, "message": "m", "data": 1}}) == (
        -32601,
        "m",
        1,
    )
    assert wire.rpc_error_of({"error": {"code": "x", "message": "m"}}) is None
    assert wire.rpc_error_of({"error": {"code": True, "message": "m"}}) is None
    assert wire.rpc_error_of({"error": {"code": -1}}) == (-1, "", None)
    assert wire.rpc_error_of({"result": {}}) is None


# --------------------------------------------------------------------------- redaction


def test_redact_headers_and_scrub() -> None:
    pairs = [
        ("Authorization", "Bearer sekrit"),
        ("X-Api-Key", "k"),
        ("Cookie", "a=b"),
        ("X-My-Token", "t"),
        ("Content-Type", "application/json"),
        ("X-Echo", "contains sekrit here"),
    ]
    out = dict(wire.redact(pairs, secrets=["sekrit"]))
    assert out["Authorization"] == "Bearer ****"
    assert out["X-Api-Key"] == "****"
    assert out["Cookie"] == "****"
    assert out["X-My-Token"] == "****"
    assert out["Content-Type"] == "application/json"
    assert out["X-Echo"] == "contains **** here"
    assert wire.scrub("url?t=sekrit", ["sekrit", ""]) == "url?t=****"
    assert wire.redact_header_value("authorization", "token-only") == "****"


# --------------------------------------------------------------------------- origins


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://api.example.com:443/mcp", "https://api.example.com"),
        ("https://API.Example.com/mcp?x=1", "https://api.example.com"),
        ("http://[::1]:9000/mcp", "http://[::1]:9000"),
        ("http://127.0.0.1:8080/mcp", "http://127.0.0.1:8080"),
        ("http://h:80/", "http://h"),
    ],
)
def test_derive_origin(url: str, origin: str) -> None:
    assert wire.derive_origin(url) == origin


def test_same_origin() -> None:
    assert wire.same_origin("http://h:80/a", "http://h/b")
    assert not wire.same_origin("http://h/a", "https://h/a")
    assert not wire.same_origin("http://h/a", "http://other/a")


# --------------------------------------------------------------------------- classify_reply


def reply(
    status: int = 200,
    *,
    media: str = "application/json",
    messages: tuple[dict[str, Any], ...] = (),
    parse_error: str | None = None,
    error: str | None = None,
    redirect_blocked: str | None = None,
    request_id: int | str | None = 1,
) -> RawReply:
    return RawReply(
        status=status,
        headers=httpx2.Headers(),
        media_type=media,
        charset=None,
        messages=messages,
        raw=b"",
        truncated=False,
        parse_error=parse_error,
        elapsed_ms=1,
        request_id=request_id,
        redirect_blocked=redirect_blocked,
        error=error,
    )


def err(code: int, req_id: Any = 1, message: str = "m") -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


RESULT = {"jsonrpc": "2.0", "id": 1, "result": {"resultType": "complete"}}

MATRIX: list[tuple[str, RawReply, OutcomeKind, str]] = [
    ("ok json", reply(messages=(RESULT,)), OutcomeKind.OK, "evaluate"),
    ("ok sse", reply(media="text/event-stream", messages=(RESULT,)), OutcomeKind.OK, "evaluate"),
    ("rpc_error 200 -32020", reply(messages=(err(-32020),)), OutcomeKind.RPC_ERROR, "evaluate"),
    (
        "modern -32020",
        reply(400, messages=(err(-32020),)),
        OutcomeKind.MODERN_REJECTION,
        "inconclusive",
    ),
    (
        "modern -32021",
        reply(400, messages=(err(-32021),)),
        OutcomeKind.MODERN_REJECTION,
        "inconclusive",
    ),
    (
        "modern -32022",
        reply(400, messages=(err(-32022),)),
        OutcomeKind.MODERN_REJECTION,
        "inconclusive",
    ),
    (
        "method_not_found",
        reply(404, messages=(err(-32601),)),
        OutcomeKind.METHOD_NOT_FOUND,
        "evaluate",
    ),
    (
        "non_modern 400 -32600",
        reply(400, messages=(err(-32600, "server-error"),)),
        OutcomeKind.NON_MODERN_REJECTION,
        "inconclusive",
    ),
    (
        "non_modern 200 -32602",
        reply(200, messages=(err(-32602),)),
        OutcomeKind.NON_MODERN_REJECTION,
        "inconclusive",
    ),
    (
        "non_modern 404 -32600",
        reply(404, messages=(err(-32600),)),
        OutcomeKind.NON_MODERN_REJECTION,
        "inconclusive",
    ),
    (
        "non_modern empty 400",
        reply(400, media="", parse_error="empty body"),
        OutcomeKind.NON_MODERN_REJECTION,
        "inconclusive",
    ),
    (
        "not_mcp 404 html",
        reply(404, media="text/html"),
        OutcomeKind.NOT_MCP_ENDPOINT,
        "inconclusive",
    ),
    ("not_mcp 405", reply(405, media=""), OutcomeKind.NOT_MCP_ENDPOINT, "inconclusive"),
    ("not_mcp 406", reply(406, media=""), OutcomeKind.NOT_MCP_ENDPOINT, "inconclusive"),
    ("not_mcp 421", reply(421, media=""), OutcomeKind.NOT_MCP_ENDPOINT, "inconclusive"),
    ("auth 401", reply(401, media="text/plain"), OutcomeKind.AUTH_REQUIRED, "inconclusive"),
    (
        "auth 401 jsonrpc",
        reply(401, messages=(err(-32600),)),
        OutcomeKind.AUTH_REQUIRED,
        "inconclusive",
    ),
    ("auth 407", reply(407, media=""), OutcomeKind.AUTH_REQUIRED, "inconclusive"),
    ("forbidden 403 text", reply(403, media="text/plain"), OutcomeKind.FORBIDDEN, "inconclusive"),
    ("server_error 503", reply(503, media="text/html"), OutcomeKind.SERVER_ERROR, "inconclusive"),
    (
        "server_error 500 jsonrpc",
        reply(500, messages=(err(-32603),)),
        OutcomeKind.SERVER_ERROR,
        "inconclusive",
    ),
    (
        "redirected 302",
        reply(302, media="", redirect_blocked="http://else/"),
        OutcomeKind.REDIRECTED,
        "inconclusive",
    ),
    (
        "redirected 307 cross",
        reply(307, media="", redirect_blocked="https://other/mcp"),
        OutcomeKind.REDIRECTED,
        "inconclusive",
    ),
    ("not_jsonrpc html 200", reply(200, media="text/html"), OutcomeKind.NOT_JSONRPC_2XX, "fail"),
    (
        "not_jsonrpc bad json",
        reply(200, parse_error="invalid JSON"),
        OutcomeKind.NOT_JSONRPC_2XX,
        "fail",
    ),
    (
        "not_jsonrpc array",
        reply(200, parse_error="JSON array (batching is not part of 2026-07-28)"),
        OutcomeKind.NOT_JSONRPC_2XX,
        "fail",
    ),
    ("not_jsonrpc 202", reply(202, media=""), OutcomeKind.NOT_JSONRPC_2XX, "fail"),
    (
        "not_jsonrpc no response",
        reply(200, messages=({"jsonrpc": "2.0", "method": "notifications/x"},)),
        OutcomeKind.NOT_JSONRPC_2XX,
        "fail",
    ),
    (
        "not_jsonrpc id mismatch",
        reply(200, messages=({"jsonrpc": "2.0", "id": 2, "result": {}},)),
        OutcomeKind.NOT_JSONRPC_2XX,
        "fail",
    ),
    (
        "transport timeout",
        reply(0, media="", error="request exceeded the 10s total budget"),
        OutcomeKind.TRANSPORT,
        "inconclusive",
    ),
    (
        "transport body cap",
        reply(200, error="body exceeds the 16 MiB cap"),
        OutcomeKind.TRANSPORT,
        "inconclusive",
    ),
]


@pytest.mark.parametrize(("label", "r", "kind", "hint"), MATRIX, ids=[m[0] for m in MATRIX])
def test_classify_reply_matrix(label: str, r: RawReply, kind: OutcomeKind, hint: str) -> None:
    outcome = wire.classify_reply(r)
    assert outcome.kind is kind, label
    assert outcome.status_hint == hint, label


def test_classify_reply_forbidden_depends_on_origin() -> None:
    r = reply(403, messages=(err(-32600),))
    assert wire.classify_reply(r, sent_origin=False).kind is OutcomeKind.NON_MODERN_REJECTION
    assert wire.classify_reply(r, sent_origin=True).kind is OutcomeKind.FORBIDDEN
    assert "try --origin" in wire.classify_reply(reply(403, media="text/html")).detail


def test_classify_reply_details_and_codes() -> None:
    assert (
        wire.classify_reply(reply(401, media="")).detail == "Not evaluated: HTTP 401 (pass --token)"
    )
    assert (
        wire.classify_reply(reply(401, media=""), sent_token=True).detail
        == "Not evaluated: HTTP 401 (token rejected)"
    )
    assert (
        wire.classify_reply(reply(404, media="text/html")).detail
        == "Not evaluated: no MCP endpoint at URL (HTTP 404 text/html)"
    )
    o = wire.classify_reply(reply(302, media="", redirect_blocked="http://else/mcp"))
    assert o.detail == "Not evaluated: redirects (302) to http://else/mcp"
    o = wire.classify_reply(reply(400, messages=(err(-32022),)))
    assert o.rpc_code == -32022
    assert o.detail == "Not evaluated: server does not support 2026-07-28"
    o = wire.classify_reply(
        reply(400, messages=(err(-32600, 1, "Bad Request: Missing session ID"),))
    )
    assert o.rpc_code == -32600
    assert o.rpc_message == "Bad Request: Missing session ID"
    assert wire.classify_reply(reply(200, messages=(RESULT,))).ok


def test_classify_reply_null_id_error_is_matched() -> None:
    r = reply(400, messages=(err(-32020, None),))
    assert wire.classify_reply(r).kind is OutcomeKind.MODERN_REJECTION
    assert r.response is not None


def test_meta_key_regexes() -> None:
    assert wire.META_KEY_RE.match("io.modelcontextprotocol/serverInfo")
    assert wire.META_KEY_RE.match("com.example/x-y_z.1")
    assert wire.META_KEY_RE.match("plainname")
    assert not wire.META_KEY_RE.match("bad key")
    assert not wire.META_KEY_RE.match("-leading/x")
    assert not wire.META_KEY_RE.match("a//b")
    assert wire.RESERVED_PREFIX_RE.match("dev.mcp/x")
    assert wire.RESERVED_PREFIX_RE.match("org.modelcontextprotocol.api/x")
    assert not wire.RESERVED_PREFIX_RE.match("com.example.mcp/x")


def test_constants() -> None:
    assert wire.PROTOCOL_VERSION == "2026-07-28"
    assert {-32020, -32021, -32022} == wire.MODERN_ERROR_CODES
    assert wire.MAX_BODY_BYTES == 16 * 1024 * 1024
    assert wire.SSE_TERMINATION_GRACE_S == 2.0
    assert wire.MAX_PAGES == 100
    assert wire.SESSION_CONNECTIONS == 3
    assert wire.TTL_SANITY_MS == 2_592_000_000
    assert (wire.MAX_SCHEMA_DEPTH, wire.MAX_SCHEMA_NODES) == (64, 5000)
    assert wire.MAX_ITEMS_SHOWN == 5
    assert wire.UNKNOWN_TOOL_NAME == "mcpcheck__nonexistent_tool_7f3a"
    assert wire.EVIL_ORIGIN == "https://evil.example.com"
    assert f"mcpcheck/{__version__} (+https://github.com/suresh-jbt/mcpcheck)" == wire.USER_AGENT


# --------------------------------------------------------------------------- hostile bodies / redaction


def test_json_depth_bound_is_mcpchecks_own_not_cpythons() -> None:
    """The verdict must not depend on where CPython's parser gives up.

    Regression: ``json`` raised ``RecursionError`` on a 100k-deep array under 3.13 but
    parsed it under some 3.14 builds, so the same hostile body produced JSON_TOO_DEEP on
    one interpreter and "JSON array (batching...)" on another -- which CI caught across
    the matrix. These depths are well inside every CPython's limit, so only mcpcheck's
    own bound can reject them.
    """
    too_deep = ("[" * (wire.MAX_JSON_DEPTH + 1) + "]" * (wire.MAX_JSON_DEPTH + 1)).encode()
    allowed = ("[" * (wire.MAX_JSON_DEPTH - 1) + "]" * (wire.MAX_JSON_DEPTH - 1)).encode()
    json.loads(too_deep)  # the parser itself is perfectly happy at this depth
    assert wire.parse_json_body(too_deep) == ((), wire.JSON_TOO_DEEP)
    assert wire.parse_json_body(allowed) == ((), "JSON array (batching is not part of 2026-07-28)")
    assert wire.sse_messages([wire.SseEvent(data=too_deep.decode())]) == (
        (),
        f"{wire.JSON_TOO_DEEP} in SSE data",
    )


def test_brackets_inside_strings_do_not_count_toward_depth() -> None:
    body = b'{"jsonrpc":"2.0","id":1,"result":{"text":"' + b"[" * 10_000 + b'"}}'
    messages, error = wire.parse_json_body(body)
    assert error is None
    assert messages[0]["result"]["text"].startswith("[[[")


def test_deeply_nested_json_is_a_parse_error_not_a_crash() -> None:
    deep = "[" * 100_000 + "]" * 100_000
    assert wire.parse_json_body(deep.encode()) == ((), wire.JSON_TOO_DEEP)
    wrapped = ('{"jsonrpc":"2.0","id":1,"result":{"deep":' + deep + "}}").encode()
    assert wire.parse_json_body(wrapped) == ((), wire.JSON_TOO_DEEP)
    msgs, err = wire.sse_messages([wire.SseEvent(data=deep)])
    assert msgs == ()
    assert err == f"{wire.JSON_TOO_DEEP} in SSE data"
    # a later well-formed event on the same stream is still parsed
    msgs, err = wire.sse_messages([wire.SseEvent(data=deep), wire.SseEvent(data='{"id": 1}')])
    assert msgs == ({"id": 1},)
    assert err is not None


def test_secret_needles_cover_encoded_forms_longest_first() -> None:
    token = "sek+ret/XYZ=="
    needles = wire.secret_needles([token, "k+1/23", ""])
    assert token in needles
    assert quote(token, safe="") in needles  # sek%2Bret%2FXYZ%3D%3D (redirect Location)
    assert quote_plus(token) in needles
    b64 = base64.b64encode(token.encode()).decode()
    bearer = base64.b64encode(f"Bearer {token}".encode()).decode()
    assert {b64, b64.rstrip("="), bearer, bearer.rstrip("=")} <= set(needles)
    assert list(needles) == sorted(set(needles), key=lambda n: (-len(n), n))
    # short derived forms would match unrelated text and are dropped; the literal stays
    short = wire.secret_needles(["ab"])
    assert "ab" in short
    assert all(len(n) >= wire.MIN_DERIVED_NEEDLE for n in short if n != "ab")
    text = (
        f"302 https://evil.example.net/leak?s=auth%3DBearer%20{quote(token, safe='')}"
        f"&b={b64}&c={bearer} raw={token} plus={quote_plus(token)}"
    )
    scrubbed = wire.scrub(text, needles)
    for form in (token, quote(token, safe=""), quote_plus(token), b64, bearer):
        assert form not in scrubbed
    assert scrubbed.count(wire.REDACTED) >= 5


def test_scrub_value_walks_everything_and_redacts_sensitive_keys() -> None:
    secrets = wire.secret_needles(["sekret-XYZ", "k-123"])
    value: dict[str, Any] = {
        "server_info": {"name": "reflect auth=Bearer sekret-XYZ apikey=k-123", "v": 1},
        "checks": [
            {
                "summary": "redirects (302) to https://evil.example.net/leak?auth=Bearer%20sekret-XYZ",
                "evidence": {
                    "data": {
                        "echo": {
                            "authorization": "Bearer sekret-XYZ",
                            "x-api-key": "k-123",
                            "x-custom": "cust-777",
                            "progressToken": "p-1",
                        }
                    },
                    "rpc_message": "got sekret-XYZ",
                    "count": 3,
                },
            }
        ],
        "sekret-XYZ": True,
    }
    out = wire.scrub_value(value, secrets)
    text = json.dumps(out)
    assert "sekret-XYZ" not in text
    assert "k-123" not in text
    echo = out["checks"][0]["evidence"]["data"]["echo"]
    assert echo["authorization"] == wire.REDACTED  # sensitive key: whole value replaced
    assert echo["x-api-key"] == wire.REDACTED
    assert echo["progressToken"] == wire.REDACTED  # *token* keys are sensitive by name
    assert echo["x-custom"] == "cust-777"  # not a secret of ours, not a sensitive key
    assert out["checks"][0]["evidence"]["count"] == 3
    assert out["server_info"]["v"] == 1
    assert out[wire.REDACTED] is True  # keys are scrubbed too
    # the input is left intact
    assert value["server_info"]["name"].endswith("k-123")
    assert value["checks"][0]["evidence"]["data"]["echo"]["authorization"] == "Bearer sekret-XYZ"
    # no secrets: nothing changes, nothing is copied needlessly
    assert wire.scrub_value(value, ()) == value


def test_scrub_value_survives_hostile_nesting() -> None:
    deep: Any = "x"
    for _ in range(5000):
        deep = [deep]
    node = wire.scrub_value({"d": deep}, ("x",))["d"]
    for _ in range(5000):
        node = node[0]
    assert node == wire.REDACTED
