"""Tests for the ``schemas`` check (PLAN.md §7.4 part 9, §10.4 rows, §10.5).

L1: ``ScriptedServer`` over ``httpx2.MockTransport`` (verdict logic, what mcpcheck sent).
L2: the ASGI fixture apps (reference server + misbehaving ``Toggles``).
L3: uvicorn over TCP for hazards only (slow server → inconclusive within budget).
"""

from __future__ import annotations

import contextlib
import json
import socket
import time
from collections.abc import Callable
from typing import Any

import httpx2
import pytest

from mcpcheck import wire
from mcpcheck.checks.base import CheckContext, Findings
from mcpcheck.checks.schemas import (
    SchemasCheck,
    ToolIssue,
    _witness,
    evaluate_listing,
    external_refs,
    is_trivially_permissive,
    schema_bounds,
)
from mcpcheck.client import ClientConfig, RawClient, Transcript
from mcpcheck.models import CheckResult, Level, Status
from tests.conftest import ASGI_BASE_URL, ASGI_CLIENT_PORT
from tests.fixtures import misbehaving_server, reference_server
from tests.fixtures.misbehaving_server import CATALOGUE_TOOL_NAMES, Toggles
from tests.helpers.scripted import ScriptedServer, error_response, list_result, ok

pytestmark = pytest.mark.anyio

SSE = pytest.mark.parametrize("sse", [False, True], ids=["json", "sse"])
MISSING: Any = object()
OBJ_IN: dict[str, Any] = {
    "type": "object",
    "properties": {"message": {"type": "string"}},
    "required": ["message"],
}
OBJ_OUT: dict[str, Any] = {"type": "object", "properties": {"text": {"type": "string"}}}
URL = "http://127.0.0.1:1/mcp"


# ------------------------------------------------------------------ helpers


def tool(
    name: str = "echo",
    input_schema: Any = MISSING,
    output_schema: Any = MISSING,
    **extra: Any,
) -> dict[str, Any]:
    """A raw tool dict; ``MISSING`` omits the key, any other value is sent verbatim."""
    t: dict[str, Any] = {"name": name}
    if input_schema is MISSING:
        t["inputSchema"] = json.loads(json.dumps(OBJ_IN))
    else:
        t["inputSchema"] = input_schema
    if output_schema is MISSING:
        t["outputSchema"] = json.loads(json.dumps(OBJ_OUT))
    else:
        t["outputSchema"] = output_schema
    t.update(extra)
    return t


def tool_without_output(name: str = "echo", input_schema: Any = MISSING) -> dict[str, Any]:
    t = tool(name, input_schema)
    del t["outputSchema"]
    return t


Route = Callable[[dict[str, Any], httpx2.Request], httpx2.Response]


def discover_route(caps: dict[str, Any] | None, *, sse: bool = False) -> Route:
    def route(body: dict[str, Any], _req: httpx2.Request) -> httpx2.Response:
        result: dict[str, Any] = {
            "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "scripted", "version": "0"}},
            "resultType": "complete",
            "supportedVersions": ["2026-07-28"],
            "ttlMs": 60000,
            "cacheScope": "public",
        }
        if caps is not None:
            result["capabilities"] = caps
        return ok(body, result, sse=sse)

    return route


def listing_server(
    tools: list[Any],
    *,
    sse: bool = False,
    result_override: dict[str, Any] | None = None,
) -> ScriptedServer:
    """A server answering ``tools/list`` with one page of ``tools``."""

    def route(body: dict[str, Any], _req: httpx2.Request) -> httpx2.Response:
        result = result_override if result_override is not None else list_result(tools, "tools")
        return ok(body, result, sse=sse)

    return ScriptedServer(routes={"tools/list": route})


def paged_server(
    pages: list[tuple[list[Any], Any]],
    *,
    sse: bool = False,
    on_page: dict[int, Route] | None = None,
) -> ScriptedServer:
    """Pages as ``(tools, nextCursor)``; ``MISSING`` omits ``nextCursor``.

    Cursors chain like the misbehaving fixture: page ``k`` answers the cursor
    issued by page ``k-1``; an unknown cursor gets ``400 -32602``; ``on_page``
    overrides the reply for a given page index (hazards).
    """

    def route(body: dict[str, Any], req: httpx2.Request) -> httpx2.Response:
        params = body.get("params") or {}
        cursor = params.get("cursor")
        if cursor is None:
            index = 0
        else:
            matches = [i + 1 for i, (_, nxt) in enumerate(pages[:-1]) if nxt == cursor]
            if not matches:
                return error_response(body, -32602, "Invalid cursor", status=400, sse=sse)
            index = matches[0]
        if on_page and index in on_page:
            return on_page[index](body, req)
        tools, nxt = pages[index]
        extra = {} if nxt is MISSING else {"nextCursor": nxt}
        return ok(body, list_result(tools, "tools", **extra), sse=sse)

    return ScriptedServer(routes={"tools/list": route})


async def run_with(ctx: CheckContext, *, strict: bool = False) -> CheckResult:
    ctx.current_check = "schemas"
    ctx.transcript.current_check = "schemas"
    f = Findings(SchemasCheck(), strict=strict)
    await SchemasCheck().run(ctx, f)
    return f.to_result(requests=len(ctx.transcript))


async def run_scripted(
    server: ScriptedServer, *, strict: bool = False, **cfg_kwargs: Any
) -> CheckResult:
    """Build a ``CheckContext`` the way the runner does and run the check once."""
    cfg = ClientConfig(url=URL, strict=strict, **cfg_kwargs)
    transcript = Transcript()
    async with server.raw_client(cfg, transcript) as raw:
        ctx = CheckContext(cfg, transcript, raw)
        return await run_with(ctx, strict=strict)


async def run_asgi(
    app: Any, *, lifespan: bool, strict: bool = False, **cfg_kwargs: Any
) -> CheckResult:
    transport = httpx2.ASGITransport(app, client=("127.0.0.1", ASGI_CLIENT_PORT))
    cfg = ClientConfig(url=ASGI_BASE_URL + "/mcp", strict=strict, **cfg_kwargs)
    transcript = Transcript()
    async with contextlib.AsyncExitStack() as stack:
        if lifespan:
            await stack.enter_async_context(app.router.lifespan_context(app))
        raw = await stack.enter_async_context(
            RawClient(cfg, transcript, label="main", transport=transport)
        )
        ctx = CheckContext(cfg, transcript, raw)
        return await run_with(ctx, strict=strict)


async def run_tcp(url: str, *, strict: bool = False, **cfg_kwargs: Any) -> CheckResult:
    cfg = ClientConfig(url=url, strict=strict, **cfg_kwargs)
    transcript = Transcript()
    async with RawClient(cfg, transcript, label="main") as raw:
        ctx = CheckContext(cfg, transcript, raw)
        return await run_with(ctx, strict=strict)


def ev(finding: Any) -> dict[str, Any]:
    """The finding's evidence dict (asserted present)."""
    assert finding.evidence is not None
    evidence: dict[str, Any] = finding.evidence
    return evidence


def codes(result: CheckResult) -> list[str]:
    return [f.code for f in (*result.failures, *result.warnings)]


def by_code(result: CheckResult, code: str) -> list[Any]:
    return [f for f in (*result.failures, *result.warnings) if f.code == code]


def assert_modern_tools_list_sent(server: ScriptedServer) -> None:
    """Every request was a modern ``tools/list`` POST (headers, ``_meta``, no ``Mcp-Name``)."""
    assert server.seen, "nothing was sent"
    for seen in server.seen:
        assert seen.request.method == "POST"
        assert seen.method == "tools/list"
        assert seen.headers[wire.H_METHOD] == "tools/list"
        assert seen.headers[wire.H_PROTOCOL_VERSION] == wire.PROTOCOL_VERSION
        assert seen.headers["accept"] == wire.ACCEPT_VALUE
        assert seen.headers["content-type"] == wire.MEDIA_JSON
        assert wire.H_NAME not in seen.headers
        assert seen.meta is not None
        assert seen.meta[wire.META_PROTOCOL_VERSION] == wire.PROTOCOL_VERSION
        assert wire.META_CLIENT_CAPABILITIES in seen.meta
        assert seen.body is not None
        assert "id" in seen.body


# ------------------------------------------------------------------ L1: pass / warn / strict


@SSE
async def test_pass_scripted(sse: bool) -> None:
    server = listing_server([tool("a"), tool("b")], sse=sse)
    result = await run_scripted(server)
    assert result.status is Status.PASS
    assert result.summary == "All 2 tools publish valid schemas"
    assert result.failures == []
    assert result.warnings == []
    assert result.requests == 1
    assert server.methods() == ["tools/list"]
    assert_modern_tools_list_sent(server)
    assert "cursor" not in (server.seen[0].body or {}).get("params", {})


@SSE
async def test_empty_list(sse: bool) -> None:
    result = await run_scripted(listing_server([], sse=sse))
    assert result.status is Status.PASS
    assert result.summary == "No tools advertised (nothing to check)"


@SSE
async def test_warn_missing_output_schema(sse: bool) -> None:
    result = await run_scripted(listing_server([tool("a"), tool_without_output("b")], sse=sse))
    assert result.status is Status.WARN
    assert codes(result) == ["schemas.output_schema_missing"]
    finding = result.warnings[0]
    assert finding.level is Level.BEST_PRACTICE
    assert finding.subject == "tool:b"
    assert finding.tool == "b"
    assert finding.evidence == {
        "path": "$.tools[1].outputSchema",
        "page": 0,
        "expected": "a JSON Schema describing structuredContent",
    }
    assert result.summary == "1 of 2 tools missing outputSchema"
    assert result.passed is True


async def test_output_schema_explicit_null_is_absent() -> None:
    result = await run_scripted(listing_server([tool("a", output_schema=None)]))
    assert codes(result) == ["schemas.output_schema_missing"]
    assert "explicitly null" in result.warnings[0].message


async def test_strict_promotes() -> None:
    server = listing_server([tool_without_output("a")])
    result = await run_scripted(server, strict=True)
    assert result.status is Status.WARN
    assert result.passed is False
    assert result.warnings[0].evidence is not None
    assert ev(result.warnings[0])["promoted_by_strict"] is True
    relaxed = await run_scripted(listing_server([tool_without_output("a")]))
    assert relaxed.warnings[0].evidence is not None
    assert "promoted_by_strict" not in relaxed.warnings[0].evidence


# ------------------------------------------------------------------ L1: inputSchema rules


@pytest.mark.parametrize(
    ("input_schema", "code", "path"),
    [
        pytest.param(None, "schemas.input_schema_not_object", "$.tools[0].inputSchema", id="null"),
        pytest.param(True, "schemas.input_schema_not_object", "$.tools[0].inputSchema", id="true"),
        pytest.param([], "schemas.input_schema_not_object", "$.tools[0].inputSchema", id="list"),
        pytest.param("x", "schemas.input_schema_not_object", "$.tools[0].inputSchema", id="str"),
        pytest.param(5, "schemas.input_schema_not_object", "$.tools[0].inputSchema", id="number"),
        pytest.param(
            MISSING, "schemas.input_schema_missing", "$.tools[0].inputSchema", id="missing"
        ),
        pytest.param({}, "schemas.input_schema_type", "$.tools[0].inputSchema.type", id="empty"),
        pytest.param(
            {"type": ["object"]},
            "schemas.input_schema_type",
            "$.tools[0].inputSchema.type",
            id="type-array-literal",
        ),
        pytest.param(
            {"type": "array"},
            "schemas.input_schema_type",
            "$.tools[0].inputSchema.type",
            id="array",
        ),
        pytest.param(
            {"type": "object", "required": "a"},
            "schemas.input_schema_invalid",
            "$.tools[0].inputSchema.required",
            id="invalid-metaschema",
        ),
        pytest.param(
            {"type": "object", "properties": {"a": {"type": "nonsense"}}},
            "schemas.input_schema_invalid",
            "$.tools[0].inputSchema.properties.a.type",
            id="nested-invalid",
        ),
    ],
)
@SSE
async def test_input_schema_rules(input_schema: Any, code: str, path: str, sse: bool) -> None:
    t = tool("t", input_schema)
    if input_schema is MISSING:
        del t["inputSchema"]
    result = await run_scripted(listing_server([t], sse=sse))
    assert result.status is Status.FAIL
    found = by_code(result, code)
    assert found, codes(result)
    assert found[0].level is Level.MUST
    assert found[0].subject == "tool:t"
    assert found[0].tool == "t"
    assert ev(found[0])["path"] == path
    assert result.summary.startswith("1 of 1 tool have invalid inputSchema")
    assert "schemas.sdk_rejects_listing" not in codes(result)


async def test_input_schema_invalid_message_and_dialect() -> None:
    result = await run_scripted(listing_server([tool("t", {"type": "object", "required": "a"})]))
    finding = by_code(result, "schemas.input_schema_invalid")[0]
    assert "'a' is not of type 'array'" in finding.message
    assert ev(finding)["dialect"] == "https://json-schema.org/draft/2020-12/schema"
    assert ev(finding)["got"] == "a"


async def test_draft07_declared() -> None:
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {"xs": {"type": "array", "items": [{"type": "string"}]}},
    }
    result = await run_scripted(listing_server([tool("t", schema)]))
    assert result.status is Status.WARN
    assert codes(result) == ["schemas.dialect_not_2020_12"]
    assert result.warnings[0].level is Level.SHOULD
    assert ev(result.warnings[0])["dialect"] == "http://json-schema.org/draft-07/schema#"
    assert result.summary == "1 of 1 tool use a non-default schema dialect"
    # the same array-form `items` is invalid under the default dialect
    strict_2020 = await run_scripted(
        listing_server([tool("t", {k: v for k, v in schema.items() if k != "$schema"})])
    )
    assert "schemas.input_schema_invalid" in codes(strict_2020)


async def test_dialect_unsupported() -> None:
    schema = {"$schema": "https://example.com/my-dialect", "type": "object", "required": "oops"}
    result = await run_scripted(listing_server([tool("t", schema)]))
    assert result.status is Status.WARN
    assert codes(result) == ["schemas.dialect_unsupported"]
    assert "validity not asserted" in result.warnings[0].message


async def test_dollar_schema_not_string() -> None:
    result = await run_scripted(listing_server([tool("t", {"$schema": 42, "type": "object"})]))
    assert result.status is Status.FAIL
    assert codes(result) == ["schemas.input_schema_invalid"]
    assert ev(result.failures[0])["path"] == '$.tools[0].inputSchema["$schema"]'
    assert "schemas.sdk_rejects_listing" not in codes(result)


# ------------------------------------------------------------------ L1: outputSchema rules


@pytest.mark.parametrize(
    ("output_schema", "code", "level", "status"),
    [
        pytest.param(True, "schemas.output_schema_not_object", Level.MUST, Status.FAIL, id="true"),
        pytest.param(
            False, "schemas.output_schema_not_object", Level.MUST, Status.FAIL, id="false"
        ),
        pytest.param([], "schemas.output_schema_not_object", Level.MUST, Status.FAIL, id="list"),
        pytest.param("s", "schemas.output_schema_not_object", Level.MUST, Status.FAIL, id="str"),
        pytest.param(
            {"type": "object", "properties": "nope"},
            "schemas.output_schema_invalid",
            Level.MUST,
            Status.FAIL,
            id="invalid",
        ),
        pytest.param(
            {},
            "schemas.output_schema_permissive",
            Level.BEST_PRACTICE,
            Status.WARN,
            id="permissive",
        ),
        pytest.param(
            {
                "title": "Anything",
                "description": "goes",
                "$schema": "https://json-schema.org/draft/2020-12/schema",
            },
            "schemas.output_schema_permissive",
            Level.BEST_PRACTICE,
            Status.WARN,
            id="annotation-only",
        ),
    ],
)
@SSE
async def test_output_schema_rules(
    output_schema: Any, code: str, level: Level, status: Status, sse: bool
) -> None:
    result = await run_scripted(listing_server([tool("t", output_schema=output_schema)], sse=sse))
    assert result.status is status
    assert codes(result) == [code]
    finding = (*result.failures, *result.warnings)[0]
    assert finding.level is level
    assert ev(finding)["path"].startswith("$.tools[0].outputSchema")
    if level is Level.MUST:
        assert result.summary.startswith("1 of 1 tool have invalid outputSchema")
    else:
        assert result.summary == "1 of 1 tool have a permissive outputSchema"


async def test_output_schema_array_ok() -> None:
    # S-7: outputSchema may be any JSON Schema; no type:"object" requirement
    result = await run_scripted(
        listing_server([tool("t", output_schema={"type": "array", "items": {"type": "string"}})])
    )
    assert result.status is Status.PASS


# ------------------------------------------------------------------ L1: x-mcp-header


XMCP_CASES = [
    pytest.param(
        {"type": "object", "properties": {"amount": {"type": "number", "x-mcp-header": "Amount"}}},
        "property 'amount': x-mcp-header is only permitted on integer/string/boolean properties (got 'number')",
        "$.tools[0].inputSchema.properties.amount",
        id="number",
    ),
    pytest.param(
        {"type": "object", "properties": {"a": {"type": "string", "x-mcp-header": "bad header"}}},
        "property 'a': x-mcp-header 'bad header' is not an RFC 9110 token",
        "$.tools[0].inputSchema.properties.a",
        id="not-token",
    ),
    pytest.param(
        {
            "type": "object",
            "properties": {
                "a": {"type": "string", "x-mcp-header": "X-A"},
                "b": {"type": "string", "x-mcp-header": "x-a"},
            },
        },
        "x-mcp-header 'X-A' on property 'a' duplicates property 'b'",
        "$.tools[0].inputSchema.properties.a",
        id="duplicate-ci",
    ),
    pytest.param(
        {
            "type": "object",
            "anyOf": [{"properties": {"a": {"type": "string", "x-mcp-header": "A"}}}],
        },
        "x-mcp-header found at a schema position not reachable via a pure `properties` chain",
        "$.tools[0].inputSchema",
        id="unreachable",
    ),
]


@pytest.mark.parametrize(("schema", "reason", "path"), XMCP_CASES)
@SSE
async def test_xmcp_header_invalid_reason_strings(
    schema: dict[str, Any], reason: str, path: str, sse: bool
) -> None:
    """Guards SDK drift: the exact reason strings verified on mcp 2.2.0 and 2.3.0."""
    result = await run_scripted(listing_server([tool("t", schema)], sse=sse))
    assert result.status is Status.FAIL
    assert codes(result) == ["schemas.x_mcp_header_invalid"]
    finding = result.failures[0]
    assert finding.level is Level.MUST
    assert finding.message.startswith(reason)
    assert (
        "conforming Streamable HTTP clients MUST exclude this tool from tools/list"
        in finding.message
    )
    assert ev(finding)["path"] == path
    assert ev(finding)["page"] == 0
    assert finding.spec_url is not None
    assert finding.spec_url.endswith("#x-mcp-header")
    assert result.summary == "1 of 1 tool dropped by conforming clients (invalid x-mcp-header)"


async def test_xmcp_header_evidence_got_is_property() -> None:
    schema = {
        "type": "object",
        "properties": {"amount": {"type": "number", "x-mcp-header": "Amount"}},
    }
    result = await run_scripted(listing_server([tool("t", schema)]))
    got = ev(result.failures[0])["got"]
    assert json.loads(got) == {"type": "number", "x-mcp-header": "Amount"}


async def test_xmcp_header_valid_passes() -> None:
    schema = {
        "type": "object",
        "properties": {
            "q": {"type": "string"},
            "region": {"type": "string", "x-mcp-header": "Region"},
            "count": {"type": "integer", "x-mcp-header": "X-Count"},
        },
        "required": ["q"],
    }
    result = await run_scripted(listing_server([tool("t", schema)]))
    assert result.status is Status.PASS


async def test_xmcp_header_sensitive() -> None:
    schema = {
        "type": "object",
        "properties": {
            "password": {"type": "string", "x-mcp-header": "X-Pass"},
            "nested": {
                "type": "object",
                "properties": {"api_key": {"type": "string", "x-mcp-header": "Key"}},
            },
            "region": {"type": "string", "x-mcp-header": "Region"},
            "fine": {"type": "string", "x-mcp-header": "Authorization-Scope"},
        },
    }
    result = await run_scripted(listing_server([tool("t", schema)]))
    assert result.status is Status.WARN
    found = by_code(result, "schemas.x_mcp_header_sensitive")
    assert len(found) == 3
    assert {ev(f)["path"] for f in found} == {
        '$.tools[0].inputSchema.properties.password["x-mcp-header"]',
        '$.tools[0].inputSchema.properties.nested.properties.api_key["x-mcp-header"]',
        '$.tools[0].inputSchema.properties.fine["x-mcp-header"]',
    }
    assert result.summary == "1 of 1 tool mirror sensitive parameters into headers"


# ------------------------------------------------------------------ L1: names, $ref, icons, bounds


async def test_name_rules() -> None:
    tools = [
        tool("has space"),
        tool(""),
        tool("a" * 129),
        tool("comma,name"),
        tool("dup"),
        tool("dup"),
        tool("dup"),
        tool("getUser"),
        tool("DATA_EXPORT_v2"),
        tool("admin.tools.list"),
        tool("with-dash"),
    ]
    result = await run_scripted(listing_server(tools))
    assert result.status is Status.WARN
    invalid = by_code(result, "schemas.name_invalid")
    assert [ev(f)["path"] for f in invalid] == [f"$.tools[{i}].name" for i in range(4)]
    assert all(f.level is Level.SHOULD for f in invalid)
    duplicates = by_code(result, "schemas.name_duplicate")
    assert [ev(f)["path"] for f in duplicates] == ["$.tools[5].name", "$.tools[6].name"]
    assert all(f.subject == "tool:dup" for f in duplicates)
    assert all(f.tool == "dup" for f in duplicates)
    assert "first on page 0, index 4" in duplicates[0].message
    assert len(codes(result)) == 6
    assert result.summary == "5 of 11 tools have non-conforming names"


async def test_name_duplicate_across_pages() -> None:
    server = paged_server([([tool("same")], "c1"), ([tool("same")], MISSING)])
    result = await run_scripted(server)
    dup = by_code(result, "schemas.name_duplicate")
    assert len(dup) == 1
    assert dup[0].evidence == {
        "path": "$.tools[0].name",
        "page": 1,
        "got": "same",
        "expected": "unique tool names within a server",
        "first_seen": "page 0 index 0",
    }


async def test_ref_external_not_fetched(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    schema = {
        "type": "object",
        "$id": "https://example.com/schemas/root.json",
        "$defs": {"local": {"type": "string"}},
        "properties": {
            "a": {"$ref": "#/$defs/local"},
            "b": {"$ref": "https://example.com/schemas/root.json#/$defs/local"},
            "c": {"$ref": "https://example.com/other.json"},
            "d": {"$ref": "relative.json#/x"},
            "$ref": {"type": "string"},
            "e": {"type": "object", "properties": {"$ref": {"type": "integer"}}},
        },
    }
    result = await run_scripted(listing_server([tool("t", schema)]))
    assert result.status is Status.WARN
    found = by_code(result, "schemas.ref_external")
    assert sorted(ev(f)["path"] for f in found) == [
        '$.tools[0].inputSchema.properties.c["$ref"]',
        '$.tools[0].inputSchema.properties.d["$ref"]',
    ]
    assert all(f.level is Level.SHOULD for f in found)
    assert result.summary == "1 of 1 tool reference external $ref"


def test_external_refs_pure() -> None:
    assert external_refs({"$ref": "#/x"}) == []
    assert external_refs({"$id": "https://a/b.json", "$ref": "https://a/b.json#/x"}) == []
    assert external_refs({"properties": {"$ref": {"type": "string"}}}) == []
    assert external_refs({"items": {"$ref": "https://a/c.json"}}) == [
        (("items",), "https://a/c.json")
    ]
    assert external_refs(None) == []


async def test_depth_cap_no_recursionerror() -> None:
    deep: dict[str, Any] = {"type": "object"}
    node = deep
    for _ in range(200):
        child: dict[str, Any] = {"type": "object"}
        node["properties"] = {"a": child}
        node = child
    wide: dict[str, Any] = {
        "type": "object",
        "properties": {f"p{i}": {"type": "string"} for i in range(wire.MAX_SCHEMA_NODES + 1)},
    }
    result = await run_scripted(
        listing_server(
            [tool("deep", deep), tool("wide", wide), tool("deep_out", output_schema=deep)]
        )
    )
    assert result.status is Status.WARN
    found = by_code(result, "schemas.schema_unverifiable")
    assert [f.subject for f in found] == ["tool:deep", "tool:wide", "tool:deep_out"]
    assert all(f.level is Level.SHOULD for f in found)
    assert "depth" in found[0].message
    assert "nodes" in found[1].message
    assert result.summary == "3 of 3 tools exceed schema bounds (not validated)"


def test_schema_bounds_pure() -> None:
    assert schema_bounds({"type": "object"}) == (1, 1, True)
    assert schema_bounds(5) == (0, 0, True)
    _, _, within = schema_bounds([[[[[1]]]]] * 1)
    assert within is True
    depth, _, within = schema_bounds(json.loads("[" * 70 + "]" * 70))
    assert within is False
    assert depth == wire.MAX_SCHEMA_DEPTH + 1


def test_is_trivially_permissive() -> None:
    assert is_trivially_permissive(True)
    assert is_trivially_permissive({})
    assert is_trivially_permissive({"title": "x", "$comment": "y", "examples": [1]})
    assert not is_trivially_permissive({"type": "object"})
    assert not is_trivially_permissive(False)
    assert not is_trivially_permissive(None)


async def test_icon_scheme() -> None:
    icons = [
        {"src": "https://example.com/i.png", "mimeType": "image/png"},
        {"src": "data:image/png;base64,AAAA"},
        {"src": "http://example.com/i.png"},
        {"src": "javascript:alert(1)"},
        {"src": "relative/icon.png"},
    ]
    result = await run_scripted(listing_server([tool("t", icons=icons)]))
    assert result.status is Status.WARN
    found = by_code(result, "schemas.icon_src_scheme")
    assert [ev(f)["path"] for f in found] == [
        "$.tools[0].icons[2].src",
        "$.tools[0].icons[3].src",
        "$.tools[0].icons[4].src",
    ]
    assert all(f.level is Level.BEST_PRACTICE for f in found)
    assert result.summary == "1 of 1 tool have non-https icon URIs"


async def test_tool_malformed() -> None:
    tools: list[Any] = [
        5,
        "not a tool",
        tool("t", title=5, annotations="x"),
        {"inputSchema": {"type": "object"}, "outputSchema": {"type": "object"}},
        {"name": 7, "inputSchema": {"type": "object"}, "outputSchema": {"type": "object"}},
        tool("meta", _meta=5),
    ]
    result = await run_scripted(listing_server(tools))
    assert result.status is Status.FAIL
    found = by_code(result, "schemas.tool_malformed")
    assert [f.subject for f in found] == [
        "tool:#0.0",
        "tool:#0.1",
        "tool:t",
        "tool:#0.3",
        "tool:#0.4",
        "tool:meta",
    ]
    assert found[2].tool == "t"
    assert found[3].tool is None
    assert "title" in found[2].message
    assert "annotations" in found[2].message
    assert "name" in found[3].message
    assert result.summary.startswith("6 of 6 tools have schema errors")
    assert "schemas.sdk_rejects_listing" not in codes(result)


# ------------------------------------------------------------------ L1: pagination


@SSE
async def test_pagination_three_pages_empty_cursor(sse: bool) -> None:
    server = paged_server([([tool("a")], "c1"), ([tool("b")], ""), ([tool("c")], MISSING)], sse=sse)
    result = await run_scripted(server)
    assert result.status is Status.PASS
    assert result.summary == "All 3 tools publish valid schemas"
    assert result.requests == 3
    assert server.methods() == ["tools/list"] * 3
    assert_modern_tools_list_sent(server)
    params = [(s.body or {}).get("params", {}) for s in server.seen]
    assert "cursor" not in params[0]
    assert params[1]["cursor"] == "c1"
    assert params[2]["cursor"] == ""


async def test_pagination_null_cursor_ends() -> None:
    server = paged_server([([tool("a")], None)])
    result = await run_scripted(server)
    assert result.status is Status.PASS
    assert result.requests == 1


@SSE
async def test_pagination_loop(sse: bool) -> None:
    server = paged_server([([tool("a"), tool("b")], "c1"), ([tool("a"), tool("b")], "c1")], sse=sse)
    result = await run_scripted(server)
    assert result.status is Status.INCONCLUSIVE
    assert result.summary == "Listing incomplete (cursor loop after 2 pages)"
    assert codes(result) == ["schemas.pagination_incomplete"]
    assert result.warnings[0].level is Level.SHOULD
    assert result.warnings[0].subject == "tools/list page 1"
    assert result.requests == 2  # the walker stopped at the repeated cursor
    # the repeated page is not double-counted as duplicate names
    assert "schemas.name_duplicate" not in codes(result)


async def test_pagination_loop_with_failures_still_fails() -> None:
    server = paged_server([([tool("a", None)], "c1"), ([tool("a", None)], "c1")])
    result = await run_scripted(server)
    assert result.status is Status.FAIL
    assert "schemas.input_schema_not_object" in codes(result)
    assert "schemas.pagination_incomplete" in codes(result)
    assert result.summary.startswith("1 of 1 tool have invalid inputSchema")


async def test_pagination_max_pages() -> None:
    def route(body: dict[str, Any], _req: httpx2.Request) -> httpx2.Response:
        cursor = (body.get("params") or {}).get("cursor")
        n = 0 if cursor is None else int(cursor[1:]) + 1
        return ok(body, list_result([tool(f"t{n}")], "tools", nextCursor=f"c{n}"))

    server = ScriptedServer(routes={"tools/list": route})
    result = await run_scripted(server)
    assert result.status is Status.INCONCLUSIVE
    assert result.requests == wire.MAX_PAGES
    assert result.summary == f"Listing incomplete (MAX_PAGES={wire.MAX_PAGES} reached)"
    assert codes(result) == ["schemas.pagination_incomplete"]


@SSE
async def test_pagination_next_cursor_not_string(sse: bool) -> None:
    server = paged_server([([tool("a")], 5)], sse=sse)
    result = await run_scripted(server)
    assert result.status is Status.FAIL
    assert codes(result) == ["schemas.pagination_malformed"]
    assert result.failures[0].level is Level.MUST
    assert ev(result.failures[0])["got"] == 5
    assert result.requests == 1
    assert result.summary.startswith("nextCursor on page 0 is number, not a string")


@SSE
async def test_pagination_error_on_page_2(sse: bool) -> None:
    # the server issues "zz" but rejects it: only one page exists for that cursor chain
    server = paged_server([([tool("a")], "zz")], sse=sse)
    result = await run_scripted(server)
    assert result.status is Status.WARN
    assert codes(result) == ["schemas.pagination_rejected_own_cursor"]
    finding = result.warnings[0]
    assert finding.level is Level.SHOULD
    assert finding.subject == "tools/list page 1"
    assert ev(finding)["cursor"] == "zz"
    assert ev(finding)["rpc_code"] == -32602
    assert (server.seen[1].body or {})["params"]["cursor"] == "zz"
    assert "Invalid cursor" in finding.message
    assert finding.message.lower().startswith("server rejected the nextcursor it issued")


async def test_pagination_infra_failure_on_page_2_is_inconclusive() -> None:
    server = paged_server(
        [([tool("a")], "c1"), ([tool("b")], MISSING)],
        on_page={1: lambda _b, _r: httpx2.Response(503, text="later")},
    )
    result = await run_scripted(server)
    assert result.status is Status.INCONCLUSIVE
    assert codes(result) == ["schemas.pagination_incomplete"]
    assert result.summary.startswith("Listing incomplete (HTTP 503 on page 1)")
    assert "1 page(s) evaluated" in result.warnings[0].message


async def test_pagination_not_jsonrpc_on_page_2_fails() -> None:
    server = paged_server(
        [([tool("a")], "c1"), ([tool("b")], MISSING)],
        on_page={
            1: lambda _b, _r: httpx2.Response(
                200, text="<html>", headers={"content-type": "text/html"}
            )
        },
    )
    result = await run_scripted(server)
    assert result.status is Status.FAIL
    assert codes(result) == ["schemas.not_jsonrpc"]
    assert result.failures[0].subject == "tools/list page 1"


# ------------------------------------------------------------------ L1: listing shape / witness


@pytest.mark.parametrize(
    ("result_override", "message_part"),
    [
        pytest.param(
            list_result([], "tools", tools=5), "result.tools is number", id="tools-number"
        ),
        pytest.param(
            {k: v for k, v in list_result([], "tools").items() if k != "tools"},
            "result.tools is missing",
            id="tools-missing",
        ),
    ],
)
async def test_listing_malformed(result_override: dict[str, Any], message_part: str) -> None:
    result = await run_scripted(listing_server([], result_override=result_override))
    assert result.status is Status.FAIL
    assert codes(result) == ["schemas.listing_malformed"]
    assert result.failures[0].subject == "tools/list page 0"
    assert message_part in result.failures[0].message
    assert "schemas.sdk_rejects_listing" not in codes(result)


async def test_listing_result_not_object() -> None:
    def route(body: dict[str, Any], _req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"), "result": []})

    result = await run_scripted(ScriptedServer(routes={"tools/list": route}))
    assert result.status is Status.FAIL
    assert codes(result) == ["schemas.listing_malformed"]
    assert "not an object" in result.failures[0].message


async def test_legacy_shaped_listing_still_evaluated() -> None:
    # hint-less result (no ttlMs/cacheScope/resultType): other checks own those; tools are judged
    override = {"tools": [tool("a"), tool_without_output("b")]}
    result = await run_scripted(listing_server([], result_override=override))
    assert result.status is Status.WARN
    assert codes(result) == ["schemas.output_schema_missing"]


def test_witness_reports_unmodelled_locs_only() -> None:
    page = list_result([{"inputSchema": {"type": "object"}}], "tools")
    # nothing covered → the missing name is an unmodelled SDK rejection
    out = _witness(page, 0, [])
    assert [i.code for i in out] == ["schemas.sdk_rejects_listing"]
    assert out[0].subject == "tool:#0.0"
    assert out[0].severity == "must"
    assert ev(out[0])["path"] == "$.tools[0].name"
    covered = [ToolIssue("must", "schemas.tool_malformed", "m", "tool:#0.0", None, {}, None, 0, 0)]
    assert _witness(page, 0, covered) == []
    # hint locs are ignored (owned by cache/handshake)
    assert _witness({"tools": [tool("a")]}, 0, []) == []


async def test_witness_never_fires_on_modelled_defects() -> None:
    tools: list[Any] = [
        tool("a", None),
        tool("b", {}),
        tool("c", {"$schema": 1, "type": "object"}),
        tool("d", output_schema=True),
        tool("e", output_schema={"$schema": 1}),
        5,
        {"inputSchema": {"type": "object"}},
        tool("f", title=3),
    ]
    result = await run_scripted(listing_server(tools))
    assert result.status is Status.FAIL
    assert "schemas.sdk_rejects_listing" not in codes(result)


def test_evaluate_listing_is_deterministic() -> None:
    pages = [list_result([tool("a", None), tool("a"), tool("b", output_schema={})], "tools")]
    first, n1 = evaluate_listing(pages)
    second, n2 = evaluate_listing(pages)
    assert n1 == 3
    assert n2 == 3
    assert first == second
    assert [i.code for i in first] == [
        "schemas.input_schema_not_object",
        "schemas.name_duplicate",
        "schemas.output_schema_permissive",
    ]


# ------------------------------------------------------------------ L1: page 0 outcomes


def raising_route(exc: Exception) -> Route:
    def route(_body: dict[str, Any], _req: httpx2.Request) -> httpx2.Response:
        raise exc

    return route


@pytest.mark.parametrize(
    ("route", "summary_prefix"),
    [
        pytest.param(
            lambda _b, _r: httpx2.Response(401, headers={"www-authenticate": "Bearer"}),
            "Not evaluated: HTTP 401 (pass --token)",
            id="401",
        ),
        pytest.param(
            lambda _b, _r: httpx2.Response(403, text="Invalid Origin header"),
            "Not evaluated: HTTP 403",
            id="403",
        ),
        pytest.param(
            lambda b, _r: error_response(b, -32020, "header mismatch", status=400),
            "Not evaluated: well-formed request rejected with -32020",
            id="400-32020",
        ),
        pytest.param(
            lambda b, _r: error_response(b, -32022, "unsupported", status=400),
            "Not evaluated: server does not support 2026-07-28",
            id="400-32022",
        ),
        pytest.param(
            lambda b, _r: error_response(b, -32021, "need capability", status=400),
            "Not evaluated: server demands a client capability",
            id="400-32021",
        ),
        pytest.param(
            lambda _b, _r: httpx2.Response(500, text="boom"), "Not evaluated: HTTP 500", id="5xx"
        ),
        pytest.param(
            lambda _b, _r: httpx2.Response(
                302, headers={"location": "https://elsewhere.example/mcp"}
            ),
            "Not evaluated: redirects (302)",
            id="302",
        ),
        pytest.param(
            raising_route(httpx2.ReadTimeout("read timed out")),
            "Not evaluated:",
            id="timeout",
        ),
        pytest.param(
            lambda b, _r: error_response(b, -32000, "nope", status=400),
            "Not evaluated: HTTP 400, JSON-RPC error -32000",
            id="non-modern-400",
        ),
        pytest.param(
            lambda b, _r: error_response(b, -32020, "odd", status=200),
            "Not evaluated: tools/list answered JSON-RPC error -32020 on HTTP 200",
            id="rpc-error-200",
        ),
    ],
)
async def test_page0_infra_inconclusive(route: Route, summary_prefix: str) -> None:
    server = ScriptedServer(routes={"tools/list": route})
    result = await run_scripted(server)
    assert result.status is Status.INCONCLUSIVE
    assert result.summary.startswith(summary_prefix), result.summary
    assert result.failures == []
    assert result.warnings == []
    assert server.methods() == ["tools/list"]


async def test_401_with_token_says_rejected() -> None:
    server = ScriptedServer(
        routes={
            "tools/list": lambda _b, _r: httpx2.Response(
                401, headers={"www-authenticate": "Bearer"}
            )
        }
    )
    result = await run_scripted(server, token="secret-token")
    assert result.summary == "Not evaluated: HTTP 401 (token rejected)"
    assert server.seen[0].headers["authorization"] == "Bearer secret-token"


@SSE
async def test_404_skipped_without_capability(sse: bool) -> None:
    server = ScriptedServer(routes={"server/discover": discover_route({"prompts": {}}, sse=sse)})
    result = await run_scripted(server)
    assert result.status is Status.INCONCLUSIVE
    assert result.summary == "Not evaluated: server does not advertise tools (tools/list → -32601)"
    assert server.methods() == ["tools/list", "server/discover"]
    assert result.requests == 2


@SSE
async def test_404_fail_with_capability(sse: bool) -> None:
    server = ScriptedServer(routes={"server/discover": discover_route({"tools": {}}, sse=sse)})
    result = await run_scripted(server)
    assert result.status is Status.FAIL
    assert codes(result) == ["schemas.tools_list_unimplemented"]
    assert result.failures[0].level is Level.MUST
    assert result.failures[0].subject == "tools/list"
    assert (
        result.summary == "capabilities.tools declared but tools/list is not implemented (-32601)"
    )
    assert server.methods() == ["tools/list", "server/discover"]


async def test_404_discover_unavailable_is_skipped() -> None:
    server = ScriptedServer()  # everything 404 -32601
    result = await run_scripted(server)
    assert result.status is Status.INCONCLUSIVE
    assert server.methods() == ["tools/list", "server/discover"]


@pytest.mark.parametrize(
    "route",
    [
        pytest.param(
            lambda _b, _r: httpx2.Response(
                200, text="<html>", headers={"content-type": "text/html"}
            ),
            id="html",
        ),
        pytest.param(
            lambda _b, _r: httpx2.Response(
                200, content=b"{not json", headers={"content-type": "application/json"}
            ),
            id="bad-json",
        ),
        pytest.param(lambda _b, _r: httpx2.Response(202), id="202"),
        pytest.param(
            lambda _b, _r: httpx2.Response(
                200, json={"jsonrpc": "2.0", "id": "other", "result": {"tools": []}}
            ),
            id="id-mismatch",
        ),
    ],
)
async def test_unexpected_content_type_fails(route: Route) -> None:
    result = await run_scripted(ScriptedServer(routes={"tools/list": route}))
    assert result.status is Status.FAIL
    assert codes(result) == ["schemas.not_jsonrpc"]
    assert result.failures[0].level is Level.MUST
    assert result.summary.startswith("tools/list answered without a JSON-RPC body")


async def test_reuses_shared_listing() -> None:
    """With page 0 seeded by the handshake and no cursor, the check sends nothing."""
    server = listing_server([tool("a"), tool_without_output("b")])
    cfg = ClientConfig(url=URL)
    transcript = Transcript()
    async with server.raw_client(cfg, transcript, label="handshake") as fresh:
        seed = await fresh.post("tools/list", purpose="primary request", check="handshake")
    assert server.methods() == ["tools/list"]
    async with server.raw_client(cfg, transcript) as raw:
        ctx = CheckContext(cfg, transcript, raw)
        ctx.seed_tools_page0(seed)
        before = len(transcript)
        result = await run_with(ctx)
    assert server.methods() == ["tools/list"]  # zero additional HTTP
    assert len(transcript) == before
    assert result.status is Status.WARN
    assert codes(result) == ["schemas.output_schema_missing"]


async def test_seeded_page0_inconclusive_reused() -> None:
    server = ScriptedServer(
        routes={
            "tools/list": lambda _b, _r: httpx2.Response(
                401, headers={"www-authenticate": "Bearer"}
            )
        }
    )
    cfg = ClientConfig(url=URL)
    transcript = Transcript()
    async with server.raw_client(cfg, transcript, label="handshake") as fresh:
        seed = await fresh.post("tools/list")
    async with server.raw_client(cfg, transcript) as raw:
        ctx = CheckContext(cfg, transcript, raw)
        ctx.seed_tools_page0(seed)
        result = await run_with(ctx)
    assert result.status is Status.INCONCLUSIVE
    assert len(server.seen) == 1


# ------------------------------------------------------------------ L1: summaries


async def test_summary_headline_selection_warnings() -> None:
    tools = [tool(f"ok{i}") for i in range(7)]
    tools += [tool_without_output(f"noout{i}") for i in range(3)]
    tools += [tool("bad name"), tool("other,bad")]
    result = await run_scripted(listing_server(tools))
    assert result.status is Status.WARN
    assert result.summary == "3 of 12 tools missing outputSchema (+2 more warnings)"


async def test_summary_headline_selection_failures() -> None:
    tools: list[Any] = [tool(f"ok{i}") for i in range(6)]
    tools += [tool("in1", None), tool("in2", {})]
    tools += [
        tool(
            "hdr",
            {"type": "object", "properties": {"n": {"type": "number", "x-mcp-header": "N"}}},
        )
    ]
    tools += [tool_without_output(f"noout{i}") for i in range(3)]
    result = await run_scripted(listing_server(tools))
    assert result.status is Status.FAIL
    assert result.summary == "2 of 12 tools have invalid inputSchema (+4 more issues)"
    assert len(result.failures) == 3
    assert len(result.warnings) == 3


async def test_summary_ties_follow_table_order() -> None:
    # one tool with an x-mcp-header MUST, one with an outputSchema MUST: input/output codes
    # come before x-mcp-header in the §7.4 table
    tools = [
        tool(
            "hdr", {"type": "object", "properties": {"n": {"type": "number", "x-mcp-header": "N"}}}
        ),
        tool("out", output_schema=True),
    ]
    result = await run_scripted(listing_server(tools))
    assert result.summary == "1 of 2 tools have invalid outputSchema (+1 more issue)"


async def test_summary_singular_tool() -> None:
    result = await run_scripted(listing_server([tool("only")]))
    assert result.summary == "All 1 tool publish valid schemas"


# ------------------------------------------------------------------ L2: ASGI reference server


@pytest.mark.parametrize("json_response", [False, True], ids=["sse", "json"])
async def test_asgi_reference_pass(json_response: bool) -> None:
    result = await run_asgi(reference_server.build_app(json_response=json_response), lifespan=True)
    assert result.status is Status.PASS
    assert result.summary == "All 2 tools publish valid schemas"
    assert result.requests == 1
    assert result.failures == []
    assert result.warnings == []
    assert result.info == []


async def test_asgi_reference_no_output_schema_warns_not_fails() -> None:
    app = reference_server.build_app(output_schema=False)
    result = await run_asgi(app, lifespan=True)
    assert result.status is Status.WARN
    assert codes(result) == ["schemas.output_schema_missing"] * 2
    assert [f.tool for f in result.warnings] == list(reference_server.TOOL_NAMES)
    assert result.summary == "2 of 2 tools missing outputSchema"
    assert result.passed is True
    strict = await run_asgi(
        reference_server.build_app(output_schema=False), lifespan=True, strict=True
    )
    assert strict.status is Status.WARN
    assert strict.passed is False


async def test_asgi_reference_empty_and_bare() -> None:
    empty = await run_asgi(reference_server.build_app(empty=True), lifespan=True)
    assert empty.status is Status.PASS
    assert empty.summary == "No tools advertised (nothing to check)"
    bare = await run_asgi(reference_server.build_app(bare=True), lifespan=True)
    assert bare.status is Status.INCONCLUSIVE
    assert bare.summary == "Not evaluated: server does not advertise tools (tools/list → -32601)"


async def test_asgi_reference_requires_token() -> None:
    result = await run_asgi(reference_server.build_app(require_token="t0k"), lifespan=True)
    assert result.status is Status.INCONCLUSIVE
    assert result.summary == "Not evaluated: HTTP 401 (pass --token)"
    with_token = await run_asgi(
        reference_server.build_app(require_token="t0k"), lifespan=True, token="t0k"
    )
    assert with_token.status is Status.PASS


# ------------------------------------------------------------------ L2: ASGI misbehaving server (§10.4 rows)


def _page(tools: list[Any], **extra: Any) -> dict[str, Any]:
    return {
        "tools": tools,
        "ttlMs": 300000,
        "cacheScope": "public",
        "resultType": "complete",
        **extra,
    }


@pytest.mark.parametrize(
    ("toggles", "status", "code", "level"),
    [
        pytest.param(Toggles(), Status.PASS, None, None, id="baseline"),
        pytest.param(Toggles(sse=True), Status.PASS, None, None, id="sse"),
        pytest.param(
            Toggles(legacy_shape=True), Status.PASS, None, None, id="legacy-shape-tools-ok"
        ),
        pytest.param(
            Toggles(tools=(tool("a", None),)),
            Status.FAIL,
            "schemas.input_schema_not_object",
            Level.MUST,
            id="inputSchema-null",
        ),
        pytest.param(
            Toggles(tools=(tool("a", {}),)),
            Status.FAIL,
            "schemas.input_schema_type",
            Level.MUST,
            id="inputSchema-empty",
        ),
        pytest.param(
            Toggles(tools=(tool("a", {"type": "object", "required": "a"}),)),
            Status.FAIL,
            "schemas.input_schema_invalid",
            Level.MUST,
            id="inputSchema-required-str",
        ),
        pytest.param(
            Toggles(tools=(tool_without_output("a"), tool("b"))),
            Status.WARN,
            "schemas.output_schema_missing",
            Level.BEST_PRACTICE,
            id="no-outputSchema",
        ),
        pytest.param(
            Toggles(tools=(tool("a", output_schema={}),)),
            Status.WARN,
            "schemas.output_schema_permissive",
            Level.BEST_PRACTICE,
            id="outputSchema-empty",
        ),
        pytest.param(
            Toggles(
                tools=(
                    tool(
                        "a",
                        {
                            "type": "object",
                            "properties": {"amount": {"type": "number", "x-mcp-header": "Amount"}},
                        },
                    ),
                )
            ),
            Status.FAIL,
            "schemas.x_mcp_header_invalid",
            Level.MUST,
            id="bad-x-mcp-header",
        ),
        pytest.param(
            Toggles(loop_cursor=True),
            Status.INCONCLUSIVE,
            "schemas.pagination_incomplete",
            Level.SHOULD,
            id="loop_cursor",
        ),
        pytest.param(
            Toggles(pages=()),
            Status.FAIL,
            "schemas.tools_list_unimplemented",
            Level.MUST,
            id="pages-empty-tools-declared",
        ),
        pytest.param(
            Toggles(pages=(), capability_mismatch=True),
            Status.INCONCLUSIVE,
            None,
            None,
            id="pages-empty-tools-undeclared",
        ),
        pytest.param(
            Toggles(pages=(), no_discover=True),
            Status.INCONCLUSIVE,
            None,
            None,
            id="pages-empty-no-discover",
        ),
        pytest.param(
            Toggles(
                pages=(
                    _page([tool("a")], nextCursor="c1"),
                    _page([tool("b")], nextCursor=""),
                    _page([]),
                )
            ),
            Status.PASS,
            None,
            None,
            id="three-pages",
        ),
        pytest.param(
            Toggles(pages=(_page([tool("a")], nextCursor=5),)),
            Status.FAIL,
            "schemas.pagination_malformed",
            Level.MUST,
            id="nextCursor-int",
        ),
        pytest.param(
            Toggles(pages=(_page([tool("a")], nextCursor="zz"),)),
            Status.WARN,
            "schemas.pagination_rejected_own_cursor",
            Level.SHOULD,
            id="own-cursor-rejected",
        ),
        pytest.param(
            Toggles(scope_varies_across_pages=True),
            Status.PASS,
            None,
            None,
            id="two-pages-scope-flip",
        ),
        pytest.param(Toggles(status=401), Status.INCONCLUSIVE, None, None, id="401"),
        pytest.param(Toggles(status=403, body=b"nope"), Status.INCONCLUSIVE, None, None, id="403"),
        pytest.param(Toggles(status=500), Status.INCONCLUSIVE, None, None, id="500"),
        pytest.param(
            Toggles(status=404, content_type="text/html"),
            Status.INCONCLUSIVE,
            None,
            None,
            id="404-html",
        ),
        pytest.param(Toggles(status=400), Status.INCONCLUSIVE, None, None, id="400-empty"),
        pytest.param(Toggles(status=202), Status.FAIL, "schemas.not_jsonrpc", Level.MUST, id="202"),
        pytest.param(
            Toggles(discover_variant="html-200"),
            Status.FAIL,
            "schemas.not_jsonrpc",
            Level.MUST,
            id="html-200",
        ),
        pytest.param(
            Toggles(discover_variant="error-32022"),
            Status.INCONCLUSIVE,
            None,
            None,
            id="error-32022",
        ),
        pytest.param(
            Toggles(discover_variant="sse-progress-then-result"),
            Status.PASS,
            None,
            None,
            id="sse-progress",
        ),
        pytest.param(
            Toggles(require_session_id=True), Status.INCONCLUSIVE, None, None, id="session-gate"
        ),
    ],
)
async def test_asgi_misbehaving_rows(
    toggles: Toggles, status: Status, code: str | None, level: Level | None
) -> None:
    app = misbehaving_server.build_app(toggles)
    result = await run_asgi(app, lifespan=False)
    assert result.status is status, (result.summary, codes(result))
    if code is None:
        assert result.failures == []
        if status is Status.PASS:
            assert result.warnings == []
    else:
        found = by_code(result, code)
        assert found, codes(result)
        assert found[0].level is level
    # safety: only tools/list (and server/discover after -32601) ever hit the wire
    methods = {entry["method"] for entry in app.state.fixture.log if entry["verb"] == "POST"}
    assert methods <= {"tools/list", "server/discover"}
    assert all(entry["verb"] == "POST" for entry in app.state.fixture.log)


async def test_asgi_misbehaving_catalogue_only_unicode_name_warns() -> None:
    app = misbehaving_server.build_app(Toggles(error_shapes_catalogue=True))
    result = await run_asgi(app, lifespan=False)
    assert result.status is Status.WARN
    assert codes(result) == ["schemas.name_invalid"]
    assert result.warnings[0].tool == "工具_unicode"
    assert result.summary == f"1 of {2 + len(CATALOGUE_TOOL_NAMES)} tools have non-conforming names"


async def test_asgi_misbehaving_sent_exactly_one_tools_list() -> None:
    app = misbehaving_server.build_app()
    await run_asgi(app, lifespan=False)
    log = app.state.fixture.log
    assert [(e["verb"], e["method"]) for e in log] == [("POST", "tools/list")]
    assert log[0]["headers"]["mcp-method"] == "tools/list"
    assert log[0]["headers"]["mcp-protocol-version"] == wire.PROTOCOL_VERSION
    assert "mcp-name" not in log[0]["headers"]


# ------------------------------------------------------------------ L3: TCP hazards


@pytest.mark.integration
async def test_tcp_reference_pass(reference_url: str) -> None:
    result = await run_tcp(reference_url)
    assert result.status is Status.PASS
    assert result.summary == "All 2 tools publish valid schemas"


@pytest.mark.integration
async def test_tcp_slow_server_inconclusive_within_budget(
    misbehaving_url: Callable[[Toggles], str],
) -> None:
    url = misbehaving_url(Toggles(slow_seconds=2.0))
    t0 = time.perf_counter()
    result = await run_tcp(url, timeout=0.5)
    elapsed = time.perf_counter() - t0
    assert result.status is Status.INCONCLUSIVE
    assert result.summary.startswith("Not evaluated:")
    assert result.failures == []
    assert result.warnings == []
    assert elapsed < 4.0


@pytest.mark.integration
async def test_tcp_request_log_only_tools_list(
    misbehaving_url: Callable[[Toggles], str],
    request_log: Callable[[str], list[dict[str, Any]]],
) -> None:
    url = misbehaving_url(Toggles(tools=(tool("a", None), tool("b"))))
    result = await run_tcp(url)
    assert result.status is Status.FAIL
    entries = request_log(url)
    assert [(e["verb"], e["method"]) for e in entries] == [("POST", "tools/list")]


async def test_pagination_byte_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pages under the per-request cap still stop the walk once their bytes add up (never a defect)."""
    monkeypatch.setattr(wire, "WALK_BYTE_BUDGET", 2000)
    filler = "x" * 600

    def route(body: dict[str, Any], _req: httpx2.Request) -> httpx2.Response:
        cursor = (body.get("params") or {}).get("cursor")
        n = 0 if cursor is None else int(cursor[1:]) + 1
        page = list_result([tool(f"t{n}", description=filler)], "tools", nextCursor=f"c{n}")
        return ok(body, page)

    server = ScriptedServer(routes={"tools/list": route})
    result = await run_scripted(server)
    assert result.status is Status.INCONCLUSIVE
    assert result.summary == "Listing incomplete (walk byte budget reached)"
    assert codes(result) == ["schemas.pagination_incomplete"]
    assert 2 <= result.requests < 10
    warning = result.warnings[0]
    assert "walk budget" in warning.message
    assert ev(warning)["stopped"].endswith("exceeded mcpcheck's 0 MiB walk budget")
    assert result.failures == []
