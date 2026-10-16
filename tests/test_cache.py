"""Tests for the ``cache`` check (PLAN.md §7.6 part 9, §10.4 rows, §10.5).

L1: verdict logic over ``ScriptedServer`` (JSON and SSE); L2: real wire shapes from the
ASGI fixture apps (reference server, misbehaving toggles, an SDK server without cache
hints); L3: hazards over TCP (timeouts, request allow-list).
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from mcp.server import MCPServer

from mcpcheck import spec_urls, wire
from mcpcheck.checks import select
from mcpcheck.checks.base import CheckContext, Findings
from mcpcheck.checks.cache import (
    CacheCheck,
    apply_findings,
    is_legacy_shaped,
    looks_user_specific,
    tokenise,
    validate_cache_hints,
)
from mcpcheck.client import ClientConfig, RawClient, Transcript
from mcpcheck.models import CheckResult, Level, Status
from mcpcheck.runner import run_checks
from tests.conftest import ASGI_BASE_URL, ASGI_CLIENT_PORT, fetch_request_log
from tests.fixtures import misbehaving_server, reference_server
from tests.fixtures.misbehaving_server import Toggles
from tests.helpers.scripted import (
    ScriptedServer,
    conformant_server,
    error_response,
    list_result,
    ok,
    sse_body,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

pytestmark = pytest.mark.anyio

URL = "http://127.0.0.1:1/mcp"
LIST_METHODS = ("tools/list", "prompts/list", "resources/list", "resources/templates/list")
ITEM_KEYS = ("tools", "prompts", "resources", "resourceTemplates")
ALLOWED_REQUESTS = {("POST", m) for m in (*LIST_METHODS, "server/discover")}


# --------------------------------------------------------------------------- helpers


@dataclass
class Run:
    """What one check run produced."""

    result: CheckResult
    transcript: Transcript
    ctx: CheckContext

    @property
    def codes(self) -> list[str]:
        return [x.code for x in (*self.result.failures, *self.result.warnings)]

    @property
    def info_codes(self) -> list[str]:
        return [x.code for x in self.result.info]

    def finding(self, code: str) -> Any:
        return next(x for x in (*self.result.failures, *self.result.warnings) if x.code == code)


def cfg(url: str = URL, **kw: Any) -> ClientConfig:
    return ClientConfig(url=url, **kw)


async def run_cache(
    raw: RawClient,
    *,
    gate: bool = False,
    prefetch: bool = False,
) -> Run:
    """Run the check the way the runner does (context, Findings, request counting)."""
    ctx = CheckContext(raw.cfg, raw.transcript, raw)
    if gate:  # what the runner's internal gate does when handshake is deselected
        ctx.seed_tools_page0(await raw.post("tools/list", purpose="internal gate", check="runner"))
    if prefetch:  # another check already fetched everything
        for method in LIST_METHODS:
            await ctx.list_pages(method)
        await ctx.discover()
    check = CacheCheck()
    f = Findings(check, strict=raw.cfg.strict)
    ctx.current_check = check.name
    raw.transcript.current_check = check.name
    before = len(raw.transcript)
    await check.run(ctx, f)
    return Run(f.to_result(requests=len(raw.transcript) - before), raw.transcript, ctx)


async def run_scripted(
    server: ScriptedServer, config: ClientConfig | None = None, **kw: Any
) -> Run:
    async with server.raw_client(config or cfg()) as raw:
        return await run_cache(raw, **kw)


@contextlib.asynccontextmanager
async def asgi_raw(
    app: Any, *, lifespan: bool, config: ClientConfig | None = None
) -> AsyncIterator[RawClient]:
    transport = httpx2.ASGITransport(app, client=("127.0.0.1", ASGI_CLIENT_PORT))
    async with contextlib.AsyncExitStack() as stack:
        if lifespan:
            await stack.enter_async_context(app.router.lifespan_context(app))
        raw = RawClient(
            config or cfg(f"{ASGI_BASE_URL}/mcp"), Transcript(), label="main", transport=transport
        )
        await stack.enter_async_context(raw)
        yield raw


async def run_reference(**kw: Any) -> Run:
    async with asgi_raw(reference_server.build_app(**kw), lifespan=True) as raw:
        return await run_cache(raw)


async def run_misbehaving(toggles: Toggles, *, token: str | None = None) -> Run:
    async with asgi_raw(
        misbehaving_server.build_app(toggles),
        lifespan=False,
        config=cfg(f"{ASGI_BASE_URL}/mcp", token=token),
    ) as raw:
        return await run_cache(raw)


def paged(
    key: str, pages: list[dict[str, Any]], *, sse: bool
) -> Callable[[dict[str, Any], httpx2.Request], httpx2.Response]:
    """A route serving ``pages`` by cursor chain (page k for cursor == pages[k-1].nextCursor)."""

    def route(body: dict[str, Any], _req: httpx2.Request) -> httpx2.Response:
        params = body.get("params") or {}
        cursor = params.get("cursor")
        index = 0
        if cursor is not None:
            index = next(i + 1 for i, p in enumerate(pages[:-1]) if p.get("nextCursor") == cursor)
        return ok(body, pages[index], sse=sse)

    return route


def fixed_route(
    result: dict[str, Any], *, sse: bool = False
) -> Callable[[dict[str, Any], httpx2.Request], httpx2.Response]:
    """A route that always answers ``result``."""

    def route(body: dict[str, Any], _req: httpx2.Request) -> httpx2.Response:
        return ok(body, result, sse=sse)

    return route


def sent_cursors(server: ScriptedServer) -> list[Any]:
    """The ``cursor`` param of every ``tools/list`` request, in order."""
    return [
        (s.body or {}).get("params", {}).get("cursor")
        for s in server.seen
        if s.method == "tools/list"
    ]


def tools_server(result: dict[str, Any], *, sse: bool) -> ScriptedServer:
    """A conformant server whose ``tools/list`` answers ``result``."""
    server = conformant_server(sse=sse)
    server.routes["tools/list"] = lambda b, _r: ok(b, result, sse=sse)
    return server


# --------------------------------------------------------------------------- pure validator


@pytest.mark.parametrize(
    ("hints", "expected"),
    [
        ({"ttlMs": 300000, "cacheScope": "public"}, []),
        ({"ttlMs": 0, "cacheScope": "private"}, []),
        ({"cacheScope": "public"}, ["cache.ttl_missing"]),
        ({"ttlMs": None, "cacheScope": "public"}, ["cache.ttl_not_integer"]),
        ({"ttlMs": -1, "cacheScope": "public"}, ["cache.ttl_negative"]),
        ({"ttlMs": 5.5, "cacheScope": "public"}, ["cache.ttl_not_integral"]),
        ({"ttlMs": -0.5, "cacheScope": "public"}, ["cache.ttl_not_integral", "cache.ttl_negative"]),
        ({"ttlMs": float("nan"), "cacheScope": "public"}, ["cache.ttl_not_integer"]),
        ({"ttlMs": float("inf"), "cacheScope": "public"}, ["cache.ttl_not_integer"]),
        ({"ttlMs": 5.0, "cacheScope": "public"}, ["cache.ttl_integral_float"]),
        ({"ttlMs": 1e3, "cacheScope": "public"}, ["cache.ttl_integral_float"]),
        (
            {"ttlMs": -1.0, "cacheScope": "public"},
            ["cache.ttl_integral_float", "cache.ttl_negative"],
        ),
        ({"ttlMs": "5", "cacheScope": "public"}, ["cache.ttl_not_integer"]),
        ({"ttlMs": True, "cacheScope": "public"}, ["cache.ttl_not_integer"]),
        ({"ttlMs": 10**12, "cacheScope": "public"}, ["cache.ttl_implausible"]),
        ({"ttlMs": 10**20, "cacheScope": "public"}, ["cache.ttl_implausible"]),
        ({"ttlMs": wire.TTL_SANITY_MS, "cacheScope": "public"}, []),
        ({"ttlMs": 1, "cacheScope": "user"}, ["cache.scope_invalid"]),
        ({"ttlMs": 1, "cacheScope": "PUBLIC"}, ["cache.scope_invalid"]),
        ({"ttlMs": 1, "cacheScope": "Private"}, ["cache.scope_invalid"]),
        ({"ttlMs": 1, "cacheScope": None}, ["cache.scope_invalid"]),
        ({"ttlMs": 1, "cacheScope": 1}, ["cache.scope_invalid"]),
        ({"ttlMs": 1}, ["cache.scope_missing"]),
        ({}, ["cache.ttl_missing", "cache.scope_missing"]),
    ],
)
def test_pure_validate_matrix(hints: dict[str, Any], expected: list[str]) -> None:
    result = {"resultType": "complete", "tools": [], **hints}
    result_type, findings = validate_cache_hints("tools/list", 0, result, token_supplied=False)
    assert result_type == "complete"
    assert [x.code for x in findings] == expected
    for x in findings:
        assert x.subject == "tools/list"
        assert x.evidence is not None
        assert x.evidence["method"] == "tools/list"
        assert x.evidence["page"] == 0
        assert "expected" in x.evidence
        assert x.spec_url is not None
        assert x.spec_url.startswith(spec_urls.SPEC_CACHING)


def test_pure_validate_levels_and_pages() -> None:
    _, findings = validate_cache_hints(
        "prompts/list", 2, {"prompts": [], "ttlMs": 5.0, "cacheScope": "nope"}, token_supplied=False
    )
    by_code = {x.code: x for x in findings}
    assert by_code["cache.ttl_integral_float"].level is Level.BEST_PRACTICE
    assert by_code["cache.scope_invalid"].level is Level.MUST
    assert all(x.subject == "prompts/list page 2" for x in findings)
    assert by_code["cache.scope_invalid"].evidence == {
        "method": "prompts/list",
        "page": 2,
        "got": "nope",
        "expected": '"public" or "private"',
    }


def test_pure_validate_absent_result_type_means_complete() -> None:
    result_type, findings = validate_cache_hints(
        "tools/list", 0, {"tools": [], "_meta": {}}, token_supplied=False
    )
    assert result_type is None
    assert [x.code for x in findings] == ["cache.ttl_missing", "cache.scope_missing"]
    assert "pre-2026" not in findings[0].message


def test_pure_validate_legacy_shape_message() -> None:
    assert is_legacy_shaped({"tools": []})
    assert not is_legacy_shaped({"tools": [], "_meta": {}})
    _, findings = validate_cache_hints("tools/list", 0, {"tools": []}, token_supplied=False)
    assert [x.code for x in findings] == ["cache.ttl_missing", "cache.scope_missing"]
    assert all("pre-2026 result shape; see handshake" in x.message for x in findings)


def test_pure_validate_input_required_skips_hints() -> None:
    result_type, findings = validate_cache_hints(
        "tools/list", 0, {"resultType": "input_required", "tools": []}, token_supplied=False
    )
    assert result_type == "input_required"
    assert findings == []


def test_pure_validate_discover_prefix() -> None:
    _, findings = validate_cache_hints(
        "server/discover",
        0,
        {"resultType": "complete", "supportedVersions": ["2026-07-28"], "capabilities": {}},
        token_supplied=True,
        code_prefix="discover",
    )
    assert [x.code for x in findings] == ["discover.ttl_missing", "discover.scope_missing"]
    assert all(x.level is Level.MUST for x in findings)


def test_pure_validate_public_heuristic_needs_token_and_public() -> None:
    result = {
        "resultType": "complete",
        "tools": [{"name": "list_my_orders"}, {"name": "lookup_weather"}],
        "ttlMs": 1,
        "cacheScope": "public",
    }
    _, with_token = validate_cache_hints("tools/list", 0, result, token_supplied=True)
    assert [x.code for x in with_token] == ["cache.public_user_specific_names"]
    assert with_token[0].level is Level.BEST_PRACTICE
    assert with_token[0].evidence is not None
    assert with_token[0].evidence["names"] == ["list_my_orders"]
    _, without = validate_cache_hints("tools/list", 0, result, token_supplied=False)
    assert without == []
    _, private = validate_cache_hints(
        "tools/list", 0, {**result, "cacheScope": "private"}, token_supplied=True
    )
    assert private == []
    # uri / uriTemplate are inspected too
    _, res = validate_cache_hints(
        "resources/templates/list",
        0,
        {
            "resultType": "complete",
            "resourceTemplates": [{"uriTemplate": "file:///home/{user}/notes", "name": "notes"}],
            "ttlMs": 1,
            "cacheScope": "public",
        },
        token_supplied=True,
    )
    assert [x.code for x in res] == ["cache.public_user_specific_names"]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("list_my_orders", True),
        ("listMyOrders", True),
        ("user_profile", True),
        ("getAccountBalance", True),
        ("me", True),
        ("self-service", True),
        ("HTTPUserAgent", True),
        ("resumeUpload", False),
        ("users", False),
        ("summary", False),
        ("demo://greeting", False),
        ("lookup_weather", False),
        ("", False),
    ],
)
def test_pure_user_specific_tokeniser(name: str, expected: bool) -> None:
    assert looks_user_specific(name) is expected


def test_tokenise_splits_camel_case_and_separators() -> None:
    assert tokenise("listMyOrders") == ["list", "my", "orders"]
    assert tokenise("HTTPUserAgent") == ["http", "user", "agent"]
    assert tokenise("file:///home/{user}/x") == ["file", "home", "user", "x"]


def test_apply_findings_dispatches_by_registered_severity() -> None:
    f = Findings(CacheCheck())
    _, findings = validate_cache_hints(
        "tools/list",
        0,
        {"tools": [{"name": "my_data"}], "ttlMs": 5.0, "cacheScope": "public", "_meta": {}},
        token_supplied=True,
    )
    apply_findings(f, findings)
    assert [x.code for x in f.warnings] == [
        "cache.ttl_integral_float",
        "cache.public_user_specific_names",
    ]
    assert f.failures == []
    assert f.status is Status.WARN


# --------------------------------------------------------------------------- L1: scripted


@pytest.mark.parametrize("sse", [False, True])
async def test_conformant_passes_and_sends_allowed_requests_only(sse: bool) -> None:
    server = conformant_server(sse=sse)
    run = await run_scripted(server, cfg(token="sekrit"))
    assert run.result.status is Status.PASS
    assert run.result.summary == "Cache hints valid on 4 list methods (4 pages)"
    assert run.result.failures == []
    assert run.result.warnings == []
    assert run.info_codes == ["cache.resources_read_not_probed"]
    assert run.result.requests == 5
    assert server.methods() == [*LIST_METHODS, "server/discover"]
    for seen in server.seen:
        assert seen.method is not None
        assert seen.headers["mcp-method"] == seen.method
        assert seen.headers["mcp-protocol-version"] == "2026-07-28"
        assert seen.headers["authorization"] == "Bearer sekrit"
        assert "mcp-name" not in seen.headers
        assert seen.meta is not None
        assert seen.meta[wire.META_PROTOCOL_VERSION] == "2026-07-28"
        assert seen.body is not None
        assert "cursor" not in seen.body["params"]
    assert "Capabilities declared: tools, prompts, resources." in run.result.details
    assert "resources/read" in run.result.details


async def test_uses_shared_listing_zero_requests() -> None:
    server = conformant_server()
    run = await run_scripted(server, prefetch=True)
    assert run.result.status is Status.PASS
    assert run.result.requests == 0
    assert len(server.seen) == 5


async def test_gate_seeded_page0_is_reused() -> None:
    server = conformant_server()
    run = await run_scripted(server, gate=True)
    assert run.result.status is Status.PASS
    assert server.methods().count("tools/list") == 1
    assert run.result.requests == 4  # prompts, resources, templates, discover


@pytest.mark.parametrize("sse", [False, True])
async def test_missing_ttl_fails(sse: bool) -> None:
    run = await run_scripted(_without(list_result([]), "ttlMs", sse=sse))
    assert run.result.status is Status.FAIL
    assert run.codes == ["cache.ttl_missing"]
    finding = run.finding("cache.ttl_missing")
    assert finding.level is Level.MUST
    assert finding.subject == "tools/list"
    assert finding.spec_url == spec_urls.SPEC_CACHEABLE_RESULTS
    assert run.result.summary == "1 cache-hint violation: tools/list ttlMs missing"


def _without(result: dict[str, Any], key: str, *, sse: bool) -> ScriptedServer:
    result = dict(result)
    result.pop(key, None)
    return tools_server(result, sse=sse)


@pytest.mark.parametrize("sse", [False, True])
async def test_missing_scope_fails(sse: bool) -> None:
    run = await run_scripted(_without(list_result([]), "cacheScope", sse=sse))
    assert run.result.status is Status.FAIL
    assert run.codes == ["cache.scope_missing"]
    assert run.finding("cache.scope_missing").spec_url == spec_urls.SPEC_CACHE_SCOPE


@pytest.mark.parametrize("sse", [False, True])
async def test_negative_ttl_fails(sse: bool) -> None:
    run = await run_scripted(tools_server(list_result([], ttlMs=-1), sse=sse))
    assert run.result.status is Status.FAIL
    assert run.codes == ["cache.ttl_negative"]
    assert run.finding("cache.ttl_negative").spec_url == spec_urls.SPEC_TTL
    assert run.finding("cache.ttl_negative").evidence["got"] == -1


@pytest.mark.parametrize("sse", [False, True])
async def test_fractional_ttl_warns(sse: bool) -> None:
    """schema.ts types ttlMs as `number`; CA:45's "integer" is prose, so 5.5 is a SHOULD, not a MUST."""
    run = await run_scripted(tools_server(list_result([], ttlMs=5.5), sse=sse))
    assert run.result.status is Status.WARN
    assert run.codes == ["cache.ttl_not_integral"]
    assert run.finding("cache.ttl_not_integral").level is Level.SHOULD
    assert run.result.summary == "Cache hints valid; 1 warning: tools/list ttlMs not a whole number"


@pytest.mark.parametrize("sse", [False, True])
async def test_integral_float_ttl_warns(sse: bool) -> None:
    run = await run_scripted(tools_server(list_result([], ttlMs=60000.0), sse=sse))
    assert run.result.status is Status.WARN
    assert run.codes == ["cache.ttl_integral_float"]
    assert run.finding("cache.ttl_integral_float").level is Level.BEST_PRACTICE
    assert (
        run.result.summary == "Cache hints valid; 1 warning: tools/list ttlMs is an integral float"
    )


@pytest.mark.parametrize("value", ["5", True, None])
async def test_string_bool_null_ttl_fail(value: Any) -> None:
    run = await run_scripted(tools_server(list_result([], ttlMs=value), sse=False))
    assert run.result.status is Status.FAIL
    assert run.codes == ["cache.ttl_not_integer"]
    assert run.finding("cache.ttl_not_integer").evidence["got"] == value


async def test_huge_ttl_warns() -> None:
    run = await run_scripted(tools_server(list_result([], ttlMs=10**20), sse=False))
    assert run.result.status is Status.WARN
    assert run.codes == ["cache.ttl_implausible"]


async def test_zero_ttl_legal_and_noted() -> None:
    server = conformant_server()
    for method, key in zip(LIST_METHODS, ITEM_KEYS, strict=True):
        server.routes[method] = fixed_route(list_result([], key, ttlMs=0))
    run = await run_scripted(server)
    assert run.result.status is Status.PASS
    assert "All ttlMs are 0" in run.result.details


@pytest.mark.parametrize("scope", ["user", "PUBLIC", "Private", "public "])
async def test_scope_invalid_fails(scope: str) -> None:
    run = await run_scripted(tools_server(list_result([], cacheScope=scope), sse=False))
    assert run.result.status is Status.FAIL
    assert run.codes == ["cache.scope_invalid"]
    assert run.finding("cache.scope_invalid").evidence["got"] == scope


@pytest.mark.parametrize("sse", [False, True])
async def test_absent_resulttype_still_requires_hints(sse: bool) -> None:
    result = list_result([])
    del result["resultType"]
    del result["ttlMs"]
    run = await run_scripted(tools_server(result, sse=sse))
    assert run.result.status is Status.FAIL
    assert run.codes == ["cache.ttl_missing"]
    assert "pre-2026" not in run.finding("cache.ttl_missing").message


@pytest.mark.parametrize("sse", [False, True])
async def test_legacy_shape_fails_with_pre2026_message(sse: bool) -> None:
    run = await run_scripted(tools_server({"tools": []}, sse=sse))
    assert run.result.status is Status.FAIL
    assert run.codes == ["cache.ttl_missing", "cache.scope_missing"]
    for code in run.codes:
        assert "pre-2026 result shape; see handshake" in run.finding(code).message
    assert run.result.summary == (
        "2 cache-hint violations: tools/list ttlMs missing; tools/list cacheScope missing"
    )


@pytest.mark.parametrize("sse", [False, True])
async def test_input_required_skips_hints(sse: bool) -> None:
    run = await run_scripted(
        tools_server({"resultType": "input_required", "inputRequests": []}, sse=sse)
    )
    assert run.result.status is Status.PASS
    assert run.codes == []
    assert "input_required" in run.result.details


async def test_empty_lists_still_need_hints() -> None:
    server = conformant_server()
    server.routes["prompts/list"] = lambda b, _r: ok(b, {"resultType": "complete", "prompts": []})
    run = await run_scripted(server)
    assert run.result.status is Status.FAIL
    assert {x.subject for x in run.result.failures} == {"prompts/list"}


async def test_failure_summary_counts_and_clips() -> None:
    server = conformant_server()
    for method, key in zip(LIST_METHODS, ITEM_KEYS, strict=True):
        server.routes[method] = fixed_route({"resultType": "complete", key: []})
    run = await run_scripted(server)
    assert run.result.status is Status.FAIL
    assert len(run.result.failures) == 8
    assert run.result.summary == (
        "8 cache-hint violations: tools/list ttlMs missing; tools/list cacheScope missing (+6 more)"
    )


# ---- pagination


@pytest.mark.parametrize("sse", [False, True])
async def test_paginated_all_pages_checked(sse: bool) -> None:
    server = conformant_server(sse=sse)
    server.routes["tools/list"] = paged(
        "tools",
        [
            list_result([{"name": "a"}], nextCursor="c1"),
            list_result([{"name": "b"}], nextCursor="c2", ttlMs=1000),
            list_result([{"name": "c"}]),
        ],
        sse=sse,
    )
    run = await run_scripted(server)
    assert run.result.status is Status.PASS
    assert run.result.summary == "Cache hints valid on 4 list methods (6 pages)"
    cursors = sent_cursors(server)
    assert cursors == [None, "c1", "c2"]
    assert "tools/list: 300000, 1000, 300000" in run.result.details


@pytest.mark.parametrize("sse", [False, True])
async def test_page2_missing_hints_fails(sse: bool) -> None:
    server = conformant_server(sse=sse)
    server.routes["tools/list"] = paged(
        "tools",
        [list_result([], nextCursor="c1"), {"resultType": "complete", "tools": []}],
        sse=sse,
    )
    run = await run_scripted(server)
    assert run.result.status is Status.FAIL
    assert run.codes == ["cache.ttl_missing", "cache.scope_missing"]
    assert {x.subject for x in run.result.failures} == {"tools/list page 1"}
    assert run.result.summary.startswith("2 cache-hint violations: tools/list page 1 ttlMs missing")


@pytest.mark.parametrize("sse", [False, True])
async def test_page2_scope_differs_fails_once(sse: bool) -> None:
    server = conformant_server(sse=sse)
    server.routes["tools/list"] = paged(
        "tools",
        [
            list_result([], nextCursor="c1"),
            list_result([], nextCursor="c2", cacheScope="private"),
            list_result([], cacheScope="private"),
        ],
        sse=sse,
    )
    run = await run_scripted(server)
    assert run.result.status is Status.FAIL
    assert run.codes == ["cache.scope_inconsistent_pages"]
    assert run.finding("cache.scope_inconsistent_pages").subject == "tools/list page 1"


async def test_json_record_shape() -> None:
    server = conformant_server()
    server.routes["tools/list"] = paged(
        "tools",
        [list_result([], nextCursor="c1"), list_result([], cacheScope="private")],
        sse=False,
    )
    run = await run_scripted(server)
    record = run.result.failures[0].model_dump(mode="json")
    assert record == {
        "code": "cache.scope_inconsistent_pages",
        "level": "must",
        "subject": "tools/list page 1",
        "message": "cacheScope differs across pages of one list request",
        "spec_url": spec_urls.SPEC_CACHE_PAGINATION,
        "evidence": {
            "method": "tools/list",
            "page": 1,
            "got": {"page0": "public", "page1": "private"},
            "expected": "same cacheScope on every page",
        },
        "tool": None,
        "issue": "cacheScope differs across pages of one list request",
    }
    assert (
        record["spec_url"]
        == "https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/caching#interaction-with-pagination"
    )
    full = run.result.model_dump(mode="json")
    assert full["name"] == "cache"
    assert full["status"] == "fail"
    assert full["severity"] == "must"
    assert full["passed"] is False


@pytest.mark.parametrize("sse", [False, True])
async def test_cursor_loop_warns(sse: bool) -> None:
    server = conformant_server(sse=sse)
    server.routes["tools/list"] = lambda b, _r: ok(b, list_result([], nextCursor="c1"), sse=sse)
    run = await run_scripted(server)
    assert run.result.status is Status.WARN
    assert run.codes == ["cache.cursor_loop"]
    assert run.finding("cache.cursor_loop").subject == "tools/list page 1"
    assert server.methods().count("tools/list") == 2


@pytest.mark.parametrize("sse", [False, True])
async def test_empty_cursor_continues(sse: bool) -> None:
    server = conformant_server(sse=sse)
    server.routes["tools/list"] = paged(
        "tools", [list_result([], nextCursor=""), list_result([])], sse=sse
    )
    run = await run_scripted(server)
    assert run.result.status is Status.PASS
    assert "(5 pages)" in run.result.summary
    cursors = sent_cursors(server)
    assert cursors == [None, ""]


@pytest.mark.parametrize("value", [5, True, {"a": 1}])
async def test_cursor_not_string_fails(value: Any) -> None:
    run = await run_scripted(tools_server(list_result([], nextCursor=value), sse=False))
    assert run.result.status is Status.FAIL
    assert run.codes == ["cache.cursor_not_string"]
    assert run.finding("cache.cursor_not_string").spec_url == spec_urls.SPEC_PAGINATION_GUIDELINES


async def test_null_cursor_ends_pagination() -> None:
    run = await run_scripted(tools_server(list_result([], nextCursor=None), sse=False))
    assert run.result.status is Status.PASS


async def test_page2_error_unevaluated() -> None:
    server = conformant_server()

    def route(body: dict[str, Any], _req: httpx2.Request) -> httpx2.Response:
        if (body.get("params") or {}).get("cursor") is None:
            return ok(body, list_result([], nextCursor="c1"))
        return error_response(body, -32602, "Invalid cursor", status=400)

    server.routes["tools/list"] = route
    run = await run_scripted(server)
    assert run.result.status is Status.WARN
    assert run.codes == ["cache.unevaluated"]
    assert run.finding("cache.unevaluated").subject == "tools/list page 1"


# ---- heuristic


@pytest.mark.parametrize("sse", [False, True])
async def test_public_user_names_warn_only_with_token(sse: bool) -> None:
    tools = [{"name": "list_my_orders", "inputSchema": {"type": "object"}}]
    server = conformant_server(sse=sse, tools=tools)
    run = await run_scripted(server, cfg(token="sekrit"))
    assert run.result.status is Status.WARN
    assert run.codes == ["cache.public_user_specific_names"]
    assert run.finding("cache.public_user_specific_names").spec_url == spec_urls.SPEC_CACHE_SECURITY
    assert run.result.summary == (
        "Cache hints valid; 1 warning: tools/list cacheScope public but item names look user-specific"
    )
    anonymous = await run_scripted(conformant_server(sse=sse, tools=tools))
    assert anonymous.result.status is Status.PASS


async def test_private_scope_no_heuristic() -> None:
    server = tools_server(
        list_result([{"name": "list_my_orders"}], cacheScope="private"), sse=False
    )
    run = await run_scripted(server, cfg(token="sekrit"))
    assert run.result.status is Status.PASS


# ---- outcomes


@pytest.mark.parametrize("sse", [False, True])
async def test_method_not_implemented_skipped(sse: bool) -> None:
    server = conformant_server(sse=sse)
    del server.routes["prompts/list"]
    run = await run_scripted(server)
    assert run.result.status is Status.PASS
    assert (
        run.result.summary
        == "Cache hints valid on 3 list methods (3 pages); 1 method not implemented"
    )
    assert run.info_codes == ["cache.method_not_implemented", "cache.resources_read_not_probed"]
    assert "Declared but answering -32601: prompts/list" in run.result.details


async def test_all_unimplemented_inconclusive() -> None:
    server = ScriptedServer()
    run = await run_scripted(server)
    assert run.result.status is Status.INCONCLUSIVE
    assert run.result.summary == "Skipped: no list method implemented (all four answered -32601)"
    assert run.info_codes.count("cache.method_not_implemented") == 4
    assert server.methods() == list(LIST_METHODS)  # no discover needed


@pytest.mark.parametrize("sse", [False, True])
async def test_other_jsonrpc_error_unevaluated(sse: bool) -> None:
    server = conformant_server(sse=sse)
    server.routes["resources/list"] = lambda b, _r: error_response(
        b, -32603, "Internal error", status=200, sse=sse
    )
    run = await run_scripted(server)
    assert run.result.status is Status.WARN
    assert run.codes == ["cache.unevaluated"]
    finding = run.finding("cache.unevaluated")
    assert finding.level is Level.SHOULD
    assert finding.subject == "resources/list"
    assert finding.evidence["rpc_code"] == -32603
    assert run.result.summary == "Cache hints valid; 1 warning: resources/list not evaluated"


async def test_modern_rejection_32020_unevaluated() -> None:
    server = conformant_server()
    server.routes["tools/list"] = lambda b, _r: error_response(
        b, -32020, "header mismatch", status=400
    )
    run = await run_scripted(server)
    assert run.result.status is Status.WARN
    assert run.codes == ["cache.unevaluated"]


@pytest.mark.parametrize("sse", [False, True])
async def test_id_mismatch_unevaluated(sse: bool) -> None:
    server = conformant_server(sse=sse)

    def route(body: dict[str, Any], _req: httpx2.Request) -> httpx2.Response:
        msg = {"jsonrpc": "2.0", "id": "other", "result": list_result([])}
        if sse:
            return httpx2.Response(
                200, headers={"content-type": "text/event-stream"}, content=sse_body(msg)
            )
        return httpx2.Response(200, json=msg)

    server.routes["tools/list"] = route
    run = await run_scripted(server)
    assert run.result.status is Status.WARN
    assert run.codes == ["cache.unevaluated"]
    assert "does not match request id" in run.finding("cache.unevaluated").message


async def test_not_json_2xx_unevaluated() -> None:
    server = conformant_server()
    server.routes["tools/list"] = lambda _b, _r: httpx2.Response(
        200, headers={"content-type": "text/html"}, content=b"<html/>"
    )
    run = await run_scripted(server)
    assert run.result.status is Status.WARN
    assert run.codes == ["cache.unevaluated"]


async def test_401_without_token_inconclusive() -> None:
    server = conformant_server()
    server.routes["tools/list"] = lambda _b, _r: httpx2.Response(
        401, headers={"www-authenticate": "Bearer"}
    )
    run = await run_scripted(server)
    assert run.result.status is Status.INCONCLUSIVE
    assert run.result.summary == "Not evaluated: HTTP 401 on tools/list (pass --token)"
    assert server.methods() == ["tools/list"]  # stops immediately


async def test_401_with_token_inconclusive_token_rejected() -> None:
    server = conformant_server()
    server.routes["prompts/list"] = lambda _b, _r: httpx2.Response(401)
    run = await run_scripted(server, cfg(token="bad"))
    assert run.result.status is Status.INCONCLUSIVE
    assert run.result.summary == "Not evaluated: HTTP 401 on prompts/list (token rejected)"


async def test_403_without_body_inconclusive() -> None:
    server = conformant_server()
    server.routes["tools/list"] = lambda _b, _r: httpx2.Response(403, text="Invalid Origin header")
    run = await run_scripted(server)
    assert run.result.status is Status.INCONCLUSIVE
    assert run.result.summary == "Not evaluated: HTTP 403 on tools/list (try --origin)"


async def test_403_with_jsonrpc_body_unevaluated() -> None:
    server = conformant_server()
    server.routes["tools/list"] = lambda b, _r: error_response(b, -32000, "forbidden", status=403)
    run = await run_scripted(server)
    assert run.result.status is Status.WARN
    assert run.codes == ["cache.unevaluated"]


async def test_html_500_inconclusive() -> None:
    server = conformant_server()
    server.routes["tools/list"] = lambda _b, _r: httpx2.Response(
        500, headers={"content-type": "text/html"}, content=b"<h1>oops</h1>"
    )
    run = await run_scripted(server)
    assert run.result.status is Status.INCONCLUSIVE
    assert run.result.summary == "Not evaluated: HTTP 500 on tools/list"


async def test_redirect_inconclusive() -> None:
    server = conformant_server()
    server.routes["tools/list"] = lambda _b, _r: httpx2.Response(
        302, headers={"location": "https://elsewhere.example.com/mcp"}
    )
    run = await run_scripted(server)
    assert run.result.status is Status.INCONCLUSIVE
    assert run.result.summary == (
        "Not evaluated: redirects on tools/list (302) to https://elsewhere.example.com/mcp"
    )


async def test_not_mcp_endpoint_inconclusive() -> None:
    server = conformant_server()
    server.routes["tools/list"] = lambda _b, _r: httpx2.Response(404, text="nope")
    run = await run_scripted(server)
    assert run.result.status is Status.INCONCLUSIVE
    assert "no MCP endpoint at URL" in run.result.summary


@pytest.mark.parametrize("sse", [False, True])
async def test_unsupported_version_inconclusive(sse: bool) -> None:
    server = conformant_server(sse=sse)
    server.routes["tools/list"] = lambda b, _r: error_response(
        b,
        -32022,
        "Unsupported protocol version",
        status=400,
        data={"supported": ["2025-11-25"], "requested": "2026-07-28"},
        sse=sse,
    )
    run = await run_scripted(server)
    assert run.result.status is Status.INCONCLUSIVE
    assert run.result.summary == (
        "Not evaluated: server does not support 2026-07-28 (supports: 2025-11-25)"
    )


async def test_environment_outcome_keeps_earlier_failures() -> None:
    server = conformant_server()
    server.routes["tools/list"] = lambda b, _r: ok(b, {"resultType": "complete", "tools": []})
    server.routes["resources/list"] = lambda _b, _r: httpx2.Response(503)
    run = await run_scripted(server)
    assert run.result.status is Status.FAIL  # fail beats inconclusive
    assert {x.code for x in run.result.failures} == {"cache.ttl_missing", "cache.scope_missing"}


@pytest.mark.parametrize("sse", [False, True])
async def test_sse_response_parsed(sse: bool) -> None:
    server = conformant_server(sse=sse)
    server.routes["tools/list"] = lambda b, _r: ok(
        b,
        list_result([]),
        sse=sse,
        extra_messages=({"jsonrpc": "2.0", "method": "notifications/message", "params": {}},),
    )
    run = await run_scripted(server)
    assert run.result.status is Status.PASS


# --------------------------------------------------------------------------- L2: ASGI fixtures


@pytest.mark.parametrize("kwargs", [{}, {"json_response": True}, {"empty": True}])
async def test_reference_server_passes(kwargs: dict[str, Any]) -> None:
    run = await run_reference(**kwargs)
    assert run.result.status is Status.PASS, run.result.model_dump()
    assert run.result.summary == "Cache hints valid on 4 list methods (4 pages)"
    assert run.result.failures == []
    assert run.result.warnings == []
    assert run.info_codes == ["cache.resources_read_not_probed"]
    assert "tools/list: 300000" in run.result.details
    assert "resources/list: 60000" in run.result.details


async def test_reference_bare_server_all_unimplemented_inconclusive() -> None:
    run = await run_reference(bare=True)
    assert run.result.status is Status.INCONCLUSIVE
    assert run.result.summary == "Skipped: no list method implemented (all four answered -32601)"


async def test_reference_server_with_token_no_heuristic_hit() -> None:
    app = reference_server.build_app(require_token="s3cret")
    async with asgi_raw(
        app, lifespan=True, config=cfg(f"{ASGI_BASE_URL}/mcp", token="s3cret")
    ) as raw:
        run = await run_cache(raw)
    assert run.result.status is Status.PASS


async def test_reference_server_401_inconclusive() -> None:
    app = reference_server.build_app(require_token="s3cret")
    async with asgi_raw(app, lifespan=True) as raw:
        run = await run_cache(raw)
    assert run.result.status is Status.INCONCLUSIVE
    assert run.result.summary == "Not evaluated: HTTP 401 on tools/list (pass --token)"


async def test_sdk_server_without_cache_hints_passes() -> None:
    server = MCPServer(name="nohints", version="0.0.1", log_level="WARNING")

    @server.tool(name="echo")
    def echo(q: str) -> str:
        return q

    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=None,
    )
    async with asgi_raw(app, lifespan=True) as raw:
        run = await run_cache(raw)
    assert run.result.status is Status.PASS, run.result.model_dump()
    assert "All ttlMs are 0" in run.result.details
    pages = await run.ctx.tools_list_pages()
    assert pages[0].result is not None
    assert pages[0].result["cacheScope"] == "private"


async def test_misbehaving_baseline_passes() -> None:
    run = await run_misbehaving(Toggles())
    assert run.result.status is Status.PASS
    assert run.result.summary == "Cache hints valid on 4 list methods (4 pages)"


async def test_misbehaving_sse_passes() -> None:
    run = await run_misbehaving(Toggles(sse=True))
    assert run.result.status is Status.PASS
    pages = await run.ctx.tools_list_pages()
    assert pages[0].is_sse


@pytest.mark.parametrize(
    ("toggles", "code", "status", "level", "subjects"),
    [
        (Toggles(omit_ttl=True), "cache.ttl_missing", Status.FAIL, Level.MUST, 4),
        (Toggles(omit_cache_scope=True), "cache.scope_missing", Status.FAIL, Level.MUST, 4),
        (Toggles(negative_ttl=True), "cache.ttl_negative", Status.FAIL, Level.MUST, 4),
        (
            Toggles(float_ttl=60000.0),
            "cache.ttl_integral_float",
            Status.WARN,
            Level.BEST_PRACTICE,
            4,
        ),
        (Toggles(cache_scope="user"), "cache.scope_invalid", Status.FAIL, Level.MUST, 4),
        (Toggles(cache_scope="Public"), "cache.scope_invalid", Status.FAIL, Level.MUST, 4),
        (
            Toggles(scope_varies_across_pages=True),
            "cache.scope_inconsistent_pages",
            Status.FAIL,
            Level.MUST,
            1,
        ),
        (Toggles(loop_cursor=True), "cache.cursor_loop", Status.WARN, Level.SHOULD, 1),
    ],
    ids=[
        "omit_ttl",
        "omit_cache_scope",
        "negative_ttl",
        "float_ttl",
        "cache_scope_user",
        "cache_scope_case",
        "scope_varies_across_pages",
        "loop_cursor",
    ],
)
async def test_misbehaving_toggle_rows(
    toggles: Toggles, code: str, status: Status, level: Level, subjects: int
) -> None:
    run = await run_misbehaving(toggles)
    assert run.result.status is status, run.result.model_dump()
    hits = [x for x in (*run.result.failures, *run.result.warnings) if x.code == code]
    assert len(hits) == subjects
    assert {x.level for x in hits} == {level}
    assert set(run.codes) == {code}


async def test_misbehaving_scope_varies_evidence() -> None:
    run = await run_misbehaving(Toggles(scope_varies_across_pages=True))
    finding = run.finding("cache.scope_inconsistent_pages")
    assert finding.subject == "tools/list page 1"
    assert finding.evidence["got"] == {"page0": "public", "page1": "private"}
    assert run.result.summary == (
        "1 cache-hint violation: tools/list page 1 cacheScope differs across pages"
    )


async def test_misbehaving_user_specific_public() -> None:
    with_token = await run_misbehaving(Toggles(user_specific_public=True), token="sekrit")
    assert with_token.result.status is Status.WARN
    assert with_token.codes == ["cache.public_user_specific_names"]
    assert with_token.finding("cache.public_user_specific_names").evidence["names"] == [
        "list_my_orders"
    ]
    anonymous = await run_misbehaving(Toggles(user_specific_public=True))
    assert anonymous.result.status is Status.PASS


async def test_misbehaving_legacy_shape_fails() -> None:
    run = await run_misbehaving(Toggles(legacy_shape=True))
    assert run.result.status is Status.FAIL
    assert sorted(set(run.codes)) == ["cache.scope_missing", "cache.ttl_missing"]
    assert len(run.result.failures) == 8
    assert all("pre-2026 result shape" in x.message for x in run.result.failures)


async def test_misbehaving_pages_not_implemented() -> None:
    run = await run_misbehaving(Toggles(pages=()))
    assert run.result.status is Status.PASS
    assert run.result.summary == (
        "Cache hints valid on 3 list methods (3 pages); 1 method not implemented"
    )
    assert "Declared but answering -32601: tools/list" in run.result.details


async def test_misbehaving_raw_pages_page1_fails() -> None:
    pages = (
        {
            "resultType": "complete",
            "tools": [],
            "ttlMs": 1,
            "cacheScope": "public",
            "nextCursor": "x",
        },
        {"resultType": "complete", "tools": [], "ttlMs": "1", "cacheScope": "public"},
    )
    run = await run_misbehaving(Toggles(pages=pages))
    assert run.result.status is Status.FAIL
    assert run.codes == ["cache.ttl_not_integer"]
    assert run.finding("cache.ttl_not_integer").subject == "tools/list page 1"


async def test_misbehaving_raw_pages_cursor_not_string() -> None:
    pages = (
        {
            "resultType": "complete",
            "tools": [],
            "ttlMs": 1,
            "cacheScope": "public",
            "nextCursor": 7,
        },
    )
    run = await run_misbehaving(Toggles(pages=pages))
    assert run.result.status is Status.FAIL
    assert run.codes == ["cache.cursor_not_string"]


async def test_misbehaving_discover_variant_does_not_affect_cache() -> None:
    # discover's own hints are the discover check's business
    run = await run_misbehaving(Toggles(discover_variant="no-hints"))
    assert run.result.status is Status.PASS


async def test_misbehaving_no_discover_capabilities_unknown() -> None:
    run = await run_misbehaving(Toggles(no_discover=True))
    assert run.result.status is Status.PASS
    assert "Capabilities unknown" in run.result.details


async def test_misbehaving_unsupported_version_inconclusive() -> None:
    run = await run_misbehaving(Toggles(discover_variant="error-32022"))
    assert run.result.status is Status.INCONCLUSIVE
    assert run.result.summary == (
        "Not evaluated: server does not support 2026-07-28 (supports: 2027-01-01)"
    )


async def test_misbehaving_status_401_inconclusive() -> None:
    run = await run_misbehaving(Toggles(status=401))
    assert run.result.status is Status.INCONCLUSIVE
    assert run.result.summary == "Not evaluated: HTTP 401 on tools/list (pass --token)"


# --------------------------------------------------------------------------- L3: TCP hazards


@pytest.mark.integration
async def test_tcp_reference_full_runner_passes(reference_url: str) -> None:
    report = await run_checks(ClientConfig(url=reference_url), select(["cache"]))
    (result,) = report.checks
    assert result.status is Status.PASS
    assert result.summary == "Cache hints valid on 4 list methods (4 pages)"
    assert report.summary.exit_code == 0


@pytest.mark.integration
async def test_tcp_request_log_contains_only_allowed_requests(
    misbehaving_url: Callable[[Toggles], str],
) -> None:
    url = misbehaving_url(Toggles())
    report = await run_checks(ClientConfig(url=url), select(["cache"]))
    assert report.checks[0].status is Status.PASS
    entries = fetch_request_log(url)
    assert entries
    assert {(e["verb"], e["method"]) for e in entries} <= ALLOWED_REQUESTS
    assert [e["method"] for e in entries] == [*LIST_METHODS, "server/discover"]


@pytest.mark.integration
@pytest.mark.slow
async def test_tcp_timeout_inconclusive(misbehaving_url: Callable[[Toggles], str]) -> None:
    url = misbehaving_url(Toggles(slow_seconds=2))
    t0 = time.perf_counter()
    async with RawClient(cfg(url, timeout=0.5), Transcript(), label="main") as raw:
        run = await run_cache(raw)
    elapsed = time.perf_counter() - t0
    assert run.result.status is Status.INCONCLUSIVE
    assert run.result.summary.startswith("Not evaluated: request exceeded the 0.5s total budget")
    assert "tools/list" in run.result.summary
    assert elapsed < 2.0, elapsed


@pytest.mark.integration
@pytest.mark.slow
async def test_tcp_endless_sse_inconclusive(misbehaving_url: Callable[[Toggles], str]) -> None:
    url = misbehaving_url(Toggles(endless_sse=True))
    async with RawClient(cfg(url, timeout=0.5), Transcript(), label="main") as raw:
        run = await run_cache(raw)
    assert run.result.status is Status.INCONCLUSIVE
    assert "exceeded the 0.5s total budget" in run.result.summary


# --------------------------------------------------------------------------- item shape (prompts/resources)


@pytest.mark.parametrize("sse", [False, True])
async def test_prompt_without_name_and_resource_without_uri_fail(sse: bool) -> None:
    """Prompt.name and Resource.uri are schema-typed required fields (SCH:2617-2624, SCH:2848)."""
    server = conformant_server(sse=sse)
    server.routes["prompts/list"] = fixed_route(
        list_result([{"nme": "summarize", "arguments": "not-a-list"}], "prompts"), sse=sse
    )
    server.routes["resources/list"] = fixed_route(
        list_result([{"name": "greeting", "mimeType": 7}], "resources"), sse=sse
    )
    run = await run_scripted(server)
    assert run.result.status is Status.FAIL
    assert set(run.codes) == {"cache.item_malformed"}
    locs = {f.evidence["loc"] for f in run.result.failures if f.evidence}
    assert locs == {
        "prompts.0.name",
        "prompts.0.arguments",
        "resources.0.uri",
        "resources.0.mimeType",
    }
    prompt = next(
        f for f in run.result.failures if f.evidence and f.evidence["loc"] == "prompts.0.name"
    )
    assert prompt.level is Level.MUST
    assert prompt.subject == "prompts/list"
    assert prompt.spec_url == spec_urls.SPEC_PROMPTS_CAPABILITIES
    assert prompt.message.startswith(
        "prompt #0 does not match the prompts item schema at prompts.0.name"
    )
    assert prompt.evidence is not None
    assert prompt.evidence["method"] == "prompts/list"
    resource = next(
        f for f in run.result.failures if f.evidence and f.evidence["loc"] == "resources.0.uri"
    )
    assert resource.spec_url == spec_urls.SPEC_RESOURCES_CAPABILITIES
    assert "resource #0" in resource.message
    # the SDK witness details no longer repeat what became a verdict
    assert "prompts.0.name" not in run.result.details


async def test_item_shape_findings_skip_other_locations() -> None:
    from mcpcheck.checks.cache import item_shape_findings, sdk_errors

    result = list_result([{"name": "ok", "uri": "demo://x"}], "resources", ttlMs="bad")
    errors = sdk_errors("resources/list", result)
    assert any(e["loc"][0] == "ttlMs" for e in errors)
    assert item_shape_findings("resources/list", 0, result, errors) == []
    # tools/list items belong to the schemas check
    assert (
        item_shape_findings("tools/list", 0, {"tools": [{}]}, [{"loc": ("tools", 0, "name")}]) == []
    )


async def test_misbehaving_malformed_prompts_and_resources() -> None:
    toggles = Toggles(
        prompts=({"nme": "summarize", "arguments": "not-a-list"},),
        resources=({"name": "greeting", "mimeType": 7},),
    )
    run = await run_misbehaving(toggles)
    assert run.result.status is Status.FAIL
    assert set(run.codes) == {"cache.item_malformed"}
    assert len(run.codes) == 4
    assert run.result.summary.startswith("4 list violations: prompts/list item does not match")
