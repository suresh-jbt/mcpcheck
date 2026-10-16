"""Tests for the ``discover`` check (PLAN.md §7.3 part 9, §10.4 and §10.5 rows).

L1: ``ScriptedServer`` over ``httpx2.MockTransport`` (verdict logic, what mcpcheck sent).
L2: the ASGI fixture apps (reference server + every misbehaving ``discover_variant``).
L3: uvicorn over TCP for hazards only (non-terminating SSE, slow replies, endless pings,
and the request-log safety assertion).
"""

from __future__ import annotations

import contextlib
import json
import time
from typing import TYPE_CHECKING, Any

import anyio
import httpx2
import pytest

from mcpcheck import wire
from mcpcheck.checks.base import CheckContext, Findings
from mcpcheck.checks.discover import (
    DiscoverCheck,
    Item,
    error_code_range_item,
    fields_summary,
    supported_from_error_data,
    validate_discover_result,
)
from mcpcheck.client import ClientConfig, RawClient, Transcript
from mcpcheck.models import CheckResult, Level, Status
from tests.conftest import ASGI_BASE_URL, ASGI_CLIENT_PORT, fetch_request_log
from tests.fixtures import misbehaving_server, reference_server
from tests.fixtures.misbehaving_server import (
    DISCOVER_PAYLOAD_VARIANTS,
    WIRE_ENVELOPE_VARIANTS,
    WIRE_ERROR_VARIANTS,
    Toggles,
)
from tests.helpers.scripted import (
    Route,
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
SSE_HEADERS = {"x-accel-buffering": "no"}
SERVER_INFO = {"name": "scripted", "version": "0.0.1"}
PV = wire.PROTOCOL_VERSION
SSE_PARAMS = pytest.mark.parametrize("sse", [False, True], ids=["json", "sse"])


# ------------------------------------------------------------------ helpers


def codes_of(result: CheckResult) -> set[str]:
    return {f.code for f in (*result.failures, *result.warnings, *result.info)}


def failure_codes(result: CheckResult) -> set[str]:
    return {f.code for f in result.failures}


def warning_codes(result: CheckResult) -> set[str]:
    return {f.code for f in result.warnings}


def info_codes(result: CheckResult) -> set[str]:
    return {f.code for f in result.info}


def level_of(result: CheckResult, code: str) -> Level:
    for finding in (*result.failures, *result.warnings, *result.info):
        if finding.code == code:
            return finding.level
    raise AssertionError(f"{code} not found in {codes_of(result)}")


def base_result() -> dict[str, Any]:
    """A fully conformant ``server/discover`` result (mirrors the misbehaving baseline)."""
    return {
        "resultType": "complete",
        "supportedVersions": [PV],
        "capabilities": {
            "tools": {"listChanged": False},
            "prompts": {"listChanged": False},
            "resources": {"listChanged": False, "subscribe": False},
        },
        "instructions": "Use the tools.",
        "ttlMs": 3_600_000,
        "cacheScope": "public",
        "_meta": {wire.META_SERVER_INFO: dict(SERVER_INFO)},
    }


def payload(variant: str) -> dict[str, Any]:
    """Apply a ``DISCOVER_PAYLOAD_VARIANTS`` entry to :func:`base_result`."""
    drop, overrides = DISCOVER_PAYLOAD_VARIANTS[variant]
    result = base_result()
    for key in drop:
        result.pop(key, None)
    result.update(json.loads(json.dumps(overrides)))
    return result


def status_of_items(items: list[Item]) -> Status:
    severities = {i.severity for i in items}
    if "must" in severities:
        return Status.FAIL
    if severities & {"should", "best-practice"}:
        return Status.WARN
    return Status.PASS


async def run_check(
    raw: RawClient,
    cfg: ClientConfig,
    transcript: Transcript,
    *,
    preset: Callable[[CheckContext], None] | None = None,
) -> tuple[CheckResult, CheckContext]:
    """Run the discover check the way the runner does (fresh context, tagged transcript)."""
    ctx = CheckContext(cfg, transcript, raw)
    if preset is not None:
        preset(ctx)
    check = DiscoverCheck()
    f = Findings(check, strict=cfg.strict)
    ctx.current_check = check.name
    transcript.current_check = check.name
    before = len(transcript)
    await check.run(ctx, f)
    transcript.current_check = ""
    return f.to_result(requests=len(transcript) - before), ctx


async def run_scripted(
    server: ScriptedServer,
    *,
    cfg: ClientConfig | None = None,
    preset: Callable[[CheckContext], None] | None = None,
) -> tuple[CheckResult, CheckContext]:
    cfg = cfg or ClientConfig(url=URL)
    transcript = Transcript()
    async with server.raw_client(cfg, transcript) as raw:
        return await run_check(raw, cfg, transcript, preset=preset)


@contextlib.asynccontextmanager
async def asgi_raw(
    app: Any,
    *,
    lifespan: bool,
    cfg: ClientConfig | None = None,
    client_port: int = ASGI_CLIENT_PORT,
) -> AsyncIterator[tuple[RawClient, ClientConfig, Transcript]]:
    """A ``RawClient`` driving ``app`` in-process through ``httpx2.ASGITransport``."""
    cfg = cfg or ClientConfig(url=ASGI_BASE_URL + "/mcp")
    transcript = Transcript()
    transport = httpx2.ASGITransport(app, client=("127.0.0.1", client_port))
    async with contextlib.AsyncExitStack() as stack:
        if lifespan:
            await stack.enter_async_context(app.router.lifespan_context(app))
        raw = await stack.enter_async_context(RawClient(cfg, transcript, transport=transport))
        yield raw, cfg, transcript


async def run_asgi(app: Any, *, lifespan: bool, cfg: ClientConfig | None = None) -> CheckResult:
    async with asgi_raw(app, lifespan=lifespan, cfg=cfg) as (raw, cfg_, transcript):
        result, _ctx = await run_check(raw, cfg_, transcript)
        return result


async def run_misbehaving(toggles: Toggles) -> CheckResult:
    return await run_asgi(misbehaving_server.build_app(toggles), lifespan=False)


async def run_reference(**build_kwargs: Any) -> CheckResult:
    cfg = None
    if "require_token" in build_kwargs:
        cfg = ClientConfig(url=ASGI_BASE_URL + "/mcp", token=build_kwargs.get("_token"))
    build_kwargs.pop("_token", None)
    return await run_asgi(reference_server.build_app(**build_kwargs), lifespan=True, cfg=cfg)


async def run_tcp(url: str, *, request_timeout: float = 10.0) -> tuple[CheckResult, float]:
    cfg = ClientConfig(url=url, timeout=request_timeout)
    transcript = Transcript()
    t0 = time.perf_counter()
    async with RawClient(cfg, transcript) as raw:
        result, _ctx = await run_check(raw, cfg, transcript)
    return result, time.perf_counter() - t0


def discover_route(
    result: dict[str, Any],
    *,
    sse: bool,
    headers: dict[str, str] | None = None,
    status: int = 200,
    extra_messages: tuple[dict[str, Any], ...] = (),
) -> Route:
    """A ``server/discover`` route answering ``result`` (SSE replies carry X-Accel-Buffering)."""
    hdrs = {**(SSE_HEADERS if sse else {}), **(headers or {})}
    return lambda body, _req: ok(
        body, result, sse=sse, headers=hdrs, status=status, extra_messages=extra_messages
    )


def scripted(route: Route, *, sse: bool = False) -> ScriptedServer:
    """A conformant scripted server whose ``server/discover`` is ``route``."""
    server = conformant_server(sse=sse)
    server.routes["server/discover"] = route
    return server


def good_server(*, sse: bool = False, **overrides: Any) -> ScriptedServer:
    result = base_result()
    result.update(overrides)
    return scripted(discover_route(result, sse=sse), sse=sse)


def http_server(
    status: int, *, body: str = "", headers: dict[str, str] | None = None
) -> ScriptedServer:
    """Every POST answers a raw HTTP ``status`` (no JSON-RPC body unless ``body``)."""

    def route(_body: dict[str, Any], _req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, content=body.encode(), headers=headers or {})

    return ScriptedServer(catch_all=route)


# ------------------------------------------------------------------ expected tables (§10.4 / §7.3 (9))

PAYLOAD_EXPECTED: dict[str, tuple[Status, tuple[str, ...]]] = {
    "no-hints": (Status.FAIL, ("discover.ttl_missing", "discover.scope_missing")),
    "ttl-missing": (Status.FAIL, ("discover.ttl_missing",)),
    "scope-missing": (Status.FAIL, ("discover.scope_missing",)),
    "neg-ttl": (Status.FAIL, ("discover.ttl_negative",)),
    "ttl-str": (Status.FAIL, ("discover.ttl_not_integer",)),
    "ttl-bool": (Status.FAIL, ("discover.ttl_not_integer",)),
    "ttl-fraction": (Status.WARN, ("discover.ttl_not_integral",)),
    "ttl-float": (Status.WARN, ("discover.ttl_integral_float",)),
    "ttl-huge": (Status.WARN, ("discover.ttl_implausible",)),
    "scope-user": (Status.FAIL, ("discover.scope_invalid",)),
    "scope-case": (Status.FAIL, ("discover.scope_invalid",)),
    "no-resulttype": (Status.FAIL, ("discover.result_type_missing",)),
    "resulttype-unknown": (Status.FAIL, ("discover.unrecognized_result_type",)),
    "resulttype-input-required": (Status.FAIL, ("discover.unexpected_result_type",)),
    "resulttype-not-string": (Status.FAIL, ("discover.result_type_not_string",)),
    "versions-omit-2026": (Status.WARN, ("discover.supported_versions_omit_served",)),
    "versions-empty": (Status.WARN, ("discover.supported_versions_omit_served",)),
    "versions-not-list": (Status.FAIL, ("discover.supported_versions_invalid",)),
    "versions-nonstring": (Status.FAIL, ("discover.supported_versions_invalid",)),
    "versions-nondate": (Status.PASS, ("discover.supported_versions_nondate",)),
    "versions-duplicate": (Status.WARN, ("discover.supported_versions_duplicate",)),
    "versions-missing": (Status.FAIL, ("discover.supported_versions_invalid",)),
    "serverinfo-absent": (Status.WARN, ("discover.server_info_missing",)),
    "serverinfo-malformed": (Status.FAIL, ("discover.server_info_malformed",)),
    "serverinfo-top-level": (Status.WARN, ("discover.legacy_initialize_fields",)),
    "serverinfo-bad-urls": (Status.WARN, ("discover.server_info_url_scheme",)),
    "meta-badkey": (Status.FAIL, ("discover.meta_key_invalid",)),
    "meta-not-object": (Status.FAIL, ("discover.meta_not_object",)),
    "meta-reserved-unknown": (Status.PASS, ("discover.meta_reserved_key_unknown",)),
    "caps-missing": (Status.FAIL, ("discover.capabilities_invalid",)),
    "caps-not-object": (Status.FAIL, ("discover.capabilities_invalid",)),
    "caps-null": (Status.FAIL, ("discover.capability_null",)),
    "caps-tools-true": (Status.FAIL, ("discover.capabilities_invalid",)),
    "caps-unknown-key": (Status.PASS, ("discover.capability_unknown",)),
    "caps-logging": (Status.WARN, ("discover.logging_deprecated",)),
    "caps-no-tools": (Status.PASS, ()),
    "extensions-bad-id": (Status.FAIL, ("discover.extension_id_invalid",)),
    "extensions-value-not-object": (Status.FAIL, ("discover.extension_value_not_object",)),
    "listchanged-not-boolean": (Status.FAIL, ("discover.capability_flag_not_boolean",)),
    "instructions-not-string": (Status.FAIL, ("discover.instructions_not_string",)),
    "instructions-huge": (Status.PASS, ()),
    "nextcursor-present": (Status.WARN, ("discover.next_cursor_present",)),
    "extra-unknown-key": (Status.PASS, ("discover.extra_key",)),
}

# the full check also cross-checks: caps-no-tools fails because tools/list serves two tools
WIRE_PAYLOAD_OVERRIDES: dict[str, tuple[Status, tuple[str, ...]]] = {
    "caps-no-tools": (Status.FAIL, ("discover.capability_undeclared",)),
}

ERROR_EXPECTED: dict[str, tuple[Status, tuple[str, ...]]] = {
    "error-32020": (Status.FAIL, ("discover.header_mismatch_on_valid_request",)),
    "error-32021": (Status.INCONCLUSIVE, ("discover.client_capability_demanded",)),
    "error-32022": (Status.FAIL, ("discover.version_unsupported",)),
    "error-32022-contradiction": (
        Status.FAIL,
        ("discover.version_unsupported", "discover.contradictory_unsupported"),
    ),
    "error-32022-bad-data": (
        Status.FAIL,
        ("discover.version_unsupported", "discover.unsupported_data_malformed"),
    ),
    "error-32022-wrong-status": (
        Status.FAIL,
        ("discover.version_unsupported", "discover.wrong_status_for_32022"),
    ),
    "error-32602": (Status.FAIL, ("discover.invalid_params_on_valid_request",)),
    "error-32042": (Status.FAIL, ("discover.unexpected_error", "discover.forbidden_error_code")),
    "error-32000": (Status.FAIL, ("discover.unexpected_error", "discover.legacy_error_code")),
    "legacy-version-reject": (Status.FAIL, ("discover.unexpected_error",)),
}

ENVELOPE_EXPECTED: dict[str, tuple[Status, tuple[str, ...]]] = {
    "wrong-id": (Status.FAIL, ("discover.id_mismatch",)),
    "null-id": (Status.FAIL, ("discover.id_mismatch",)),
    "jsonrpc-version-wrong": (Status.FAIL, ("discover.envelope_invalid",)),
    "result-and-error": (Status.FAIL, ("discover.envelope_invalid",)),
    "html-200": (Status.FAIL, ("discover.not_jsonrpc",)),
    "json-no-content-type": (Status.FAIL, ("discover.not_jsonrpc",)),
    "json-array": (Status.FAIL, ("discover.not_jsonrpc",)),
    "status-202": (Status.FAIL, ("discover.not_jsonrpc",)),
    "sse-notification-message": (Status.FAIL, ("discover.log_without_loglevel",)),
    "sse-server-request": (Status.FAIL, ("discover.server_request_on_stream",)),
    "sse-progress-then-result": (Status.PASS, ("discover.progress_notification",)),
    "sse-two-responses": (Status.FAIL, ("discover.multiple_responses",)),
    "sse-empty": (Status.FAIL, ("discover.no_response",)),
    # sse-not-terminated is TCP-only (ASGITransport never times out): see test_tcp_sse_not_terminated
}


def test_tables_cover_every_fixture_variant() -> None:
    assert set(PAYLOAD_EXPECTED) == set(DISCOVER_PAYLOAD_VARIANTS)
    assert set(ERROR_EXPECTED) == set(WIRE_ERROR_VARIANTS)
    assert set(ENVELOPE_EXPECTED) | {"sse-not-terminated"} == set(WIRE_ENVELOPE_VARIANTS)


# ------------------------------------------------------------------ pure validator (step 4 + 5)


@pytest.mark.parametrize("variant", sorted(DISCOVER_PAYLOAD_VARIANTS))
def test_validate_discover_result_table(variant: str) -> None:
    expected_status, expected_codes = PAYLOAD_EXPECTED[variant]
    items = validate_discover_result(payload(variant))
    codes = {i.code for i in items}
    assert set(expected_codes) <= codes, (variant, codes)
    assert status_of_items(items) is expected_status, (variant, codes)
    # raw rules cover everything the SDK validator flags on these payloads
    assert "discover.schema_validation" not in codes, variant
    for item in items:
        assert item.subject.startswith("result")
        assert item.message


def test_validate_conformant_result_only_infos() -> None:
    items = validate_discover_result(base_result())
    assert [i.severity for i in items] == []
    result = base_result()
    del result["instructions"]
    items = validate_discover_result(result)
    assert {i.code for i in items} == {"discover.instructions_absent"}


def test_sdk_safety_net_catches_what_raw_rules_do_not() -> None:
    result = base_result()
    result["capabilities"]["experimental"] = {
        "a": "b"
    }  # ServerCapabilities.experimental: dict[str, dict]
    items = validate_discover_result(result)
    schema = [i for i in items if i.code == "discover.schema_validation"]
    assert len(schema) == 1
    assert schema[0].subject == "result.capabilities.experimental.a"
    assert schema[0].severity == "must"


def test_sdk_safety_net_does_not_duplicate_raw_findings() -> None:
    result = base_result()
    result["capabilities"] = "tools"
    codes = [i.code for i in validate_discover_result(result)]
    assert codes.count("discover.capabilities_invalid") == 1
    assert "discover.schema_validation" not in codes


def test_input_required_skips_hint_checks() -> None:
    result = payload("resulttype-input-required")
    del result["ttlMs"]
    codes = {i.code for i in validate_discover_result(result)}
    assert "discover.unexpected_result_type" in codes
    assert "discover.ttl_missing" not in codes


def test_supported_versions_future_version_is_info_only() -> None:
    result = base_result()
    result["supportedVersions"] = [PV, "2026-10-13"]
    items = validate_discover_result(result)
    assert status_of_items(items) is Status.PASS
    assert all(i.code != "discover.supported_versions_nondate" for i in items)


def test_extension_ids_follow_meta_key_rules() -> None:
    result = base_result()
    result["capabilities"]["extensions"] = {
        "io.example/ext": {},
        "bad key!/x": {},
        "noslash": {},
    }
    items = validate_discover_result(result)
    bad = sorted(i.subject for i in items if i.code == "discover.extension_id_invalid")
    assert bad == [
        'result.capabilities.extensions["bad key!/x"]',
        'result.capabilities.extensions["noslash"]',
    ]


def test_server_info_not_object_is_malformed() -> None:
    result = base_result()
    result["_meta"][wire.META_SERVER_INFO] = "scripted 0.1"
    codes = {i.code for i in validate_discover_result(result)}
    assert "discover.server_info_malformed" in codes


def test_fields_summary_shapes() -> None:
    assert fields_summary([]) is None
    assert fields_summary(validate_discover_result(payload("no-hints"))) == (
        "`server/discover` result missing ttlMs, cacheScope"
    )
    result = payload("scope-user")
    result["ttlMs"] = -1
    assert fields_summary(validate_discover_result(result)) == (
        "`server/discover` result invalid: ttlMs -1 (+1 more)"
    )
    assert fields_summary(validate_discover_result(payload("ttl-float"))) is None


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (-32002, "discover.forbidden_error_code"),
        (-32042, "discover.forbidden_error_code"),
        (-32050, "discover.forbidden_error_code"),
        (-32099, "discover.forbidden_error_code"),
        (-32000, "discover.legacy_error_code"),
        (-32019, "discover.legacy_error_code"),
        (-32020, None),
        (-32021, None),
        (-32022, None),
        (-32600, None),
        (-32603, None),
        (1, None),
    ],
)
def test_error_code_range_item(code: int, expected: str | None) -> None:
    item = error_code_range_item(code)
    assert (item.code if item else None) == expected


def test_supported_from_error_data() -> None:
    assert supported_from_error_data({"supported": ["2027-01-01"], "requested": PV}) == [
        "2027-01-01"
    ]
    assert supported_from_error_data({"supported": "2027-01-01"}) is None
    assert supported_from_error_data("nope") is None
    assert supported_from_error_data(None) is None


# ------------------------------------------------------------------ L1: what mcpcheck sends


@SSE_PARAMS
async def test_pass_scripted_and_request_shape(sse: bool) -> None:
    server = good_server(sse=sse)
    result, ctx = await run_scripted(server)
    assert result.status is Status.PASS, (result.summary, codes_of(result))
    assert result.summary == "`server/discover` responds correctly"
    assert result.warnings == []
    assert result.requests == 5  # discover + four list methods
    assert server.methods() == [
        "server/discover",
        "tools/list",
        "prompts/list",
        "resources/list",
        "resources/templates/list",
    ]
    seen = server.seen[0]
    assert seen.headers["mcp-method"] == "server/discover"
    assert seen.headers["mcp-protocol-version"] == PV
    assert "mcp-name" not in seen.headers
    assert seen.headers["accept"] == "application/json, text/event-stream"
    assert seen.body is not None
    assert set(seen.body["params"]) == {"_meta"}  # DI:15: no params beyond _meta
    assert seen.meta is not None
    assert set(seen.meta) == {
        wire.META_PROTOCOL_VERSION,
        wire.META_CLIENT_INFO,
        wire.META_CLIENT_CAPABILITIES,
    }
    assert seen.meta[wire.META_PROTOCOL_VERSION] == PV
    assert ctx.protocol_version == PV
    assert ctx.server_info == SERVER_INFO
    assert ctx.server_supported_versions == [PV]
    assert {"discover.capability_consistent"} <= info_codes(result)


async def test_compose_does_not_overwrite_handshake_values() -> None:
    def preset(ctx: CheckContext) -> None:
        ctx.protocol_version = PV
        ctx.server_info = {"name": "from-handshake", "version": "9"}
        ctx.server_supported_versions = ["2026-07-28", "2099-01-01"]

    result, ctx = await run_scripted(good_server(), preset=preset)
    assert result.status is Status.PASS
    assert ctx.server_info == {"name": "from-handshake", "version": "9"}
    assert ctx.server_supported_versions == ["2026-07-28", "2099-01-01"]


async def test_discover_fetched_once_and_lists_once() -> None:
    server = good_server()
    cfg = ClientConfig(url=URL)
    transcript = Transcript()
    async with server.raw_client(cfg, transcript) as raw:
        result, ctx = await run_check(raw, cfg, transcript)
        assert result.status is Status.PASS
        await ctx.discover()
        await ctx.capabilities()
        await ctx.tools_list_pages()
    assert server.methods().count("server/discover") == 1
    assert len(server.methods()) == 5


@SSE_PARAMS
async def test_pass_with_warnings_summary(sse: bool) -> None:
    result, _ctx = await run_scripted(
        good_server(sse=sse, supportedVersions=[PV, PV], nextCursor="p2")
    )
    assert result.status is Status.WARN
    assert warning_codes(result) == {
        "discover.supported_versions_duplicate",
        "discover.next_cursor_present",
    }
    assert result.summary == "`server/discover` responds correctly (2 warnings)"


@SSE_PARAMS
async def test_fail_missing_hints_summary(sse: bool) -> None:
    result = payload("no-hints")
    out, _ctx = await run_scripted(scripted(discover_route(result, sse=sse), sse=sse))
    assert out.status is Status.FAIL
    assert failure_codes(out) == {"discover.ttl_missing", "discover.scope_missing"}
    assert out.summary == "`server/discover` result missing ttlMs, cacheScope"
    assert all(f.level is Level.MUST for f in out.failures)


async def test_fail_invalid_fields_summary() -> None:
    result = payload("scope-user")
    result["resultType"] = "partial"
    out, _ctx = await run_scripted(scripted(discover_route(result, sse=False)))
    assert out.status is Status.FAIL
    assert out.summary == '`server/discover` result invalid: resultType "partial" (+1 more)'


async def test_warn_ttl_integral_float_is_best_practice() -> None:
    out, _ctx = await run_scripted(good_server(ttlMs=60000.0))
    assert out.status is Status.WARN
    assert level_of(out, "discover.ttl_integral_float") is Level.BEST_PRACTICE
    assert out.warnings[0].evidence == {"got": 60000.0, "expected": 60000}


async def test_pass_huge_instructions() -> None:
    out, _ctx = await run_scripted(good_server(instructions="x" * (2 * 1024 * 1024)))
    assert out.status is Status.PASS
    assert "instructions: 2097152 characters." in out.details


async def test_info_cache_headers() -> None:
    server = scripted(
        discover_route(
            base_result(), sse=False, headers={"cache-control": "max-age=60", "etag": '"abc"'}
        )
    )
    out, _ctx = await run_scripted(server)
    assert out.status is Status.PASS
    info = next(f for f in out.info if f.code == "discover.cache_headers")
    assert info.evidence == {"cache_headers": {"cache-control": "max-age=60", "etag": '"abc"'}}


# ------------------------------------------------------------------ L1: JSON-RPC error rows


@SSE_PARAMS
async def test_fail_not_implemented_404(sse: bool) -> None:
    server = scripted(
        lambda b, _r: error_response(b, -32601, "Method not found", status=404, sse=sse), sse=sse
    )
    out, _ctx = await run_scripted(server)
    assert out.status is Status.FAIL
    assert failure_codes(out) == {"discover.not_implemented"}
    assert out.summary == "`server/discover` not implemented (-32601)"
    assert server.methods() == ["server/discover"]  # no cross-check without capabilities


@SSE_PARAMS
async def test_fail_not_implemented_200_status(sse: bool) -> None:
    server = scripted(
        lambda b, _r: error_response(b, -32601, "Method not found", status=200, sse=sse), sse=sse
    )
    out, _ctx = await run_scripted(server)
    assert failure_codes(out) == {"discover.not_implemented", "discover.wrong_status_for_32601"}


@SSE_PARAMS
async def test_fail_unsupported_version_fills_supported(sse: bool) -> None:
    data = {"supported": ["2025-11-25"], "requested": PV}
    server = scripted(
        lambda b, _r: error_response(
            b, -32022, "Unsupported protocol version", status=400, data=data, sse=sse
        ),
        sse=sse,
    )
    out, ctx = await run_scripted(server)
    assert out.status is Status.FAIL
    assert failure_codes(out) == {"discover.version_unsupported"}
    assert out.summary == "Server does not support 2026-07-28 (supports: 2025-11-25)"
    assert ctx.server_supported_versions == ["2025-11-25"]


async def test_fail_header_mismatch_summary() -> None:
    server = scripted(
        lambda b, _r: error_response(b, -32020, "Header mismatch: mcp-method 'x'", status=400)
    )
    out, _ctx = await run_scripted(server)
    assert failure_codes(out) == {"discover.header_mismatch_on_valid_request"}
    assert out.summary == (
        "`server/discover` rejected a valid request (-32020: Header mismatch: mcp-method 'x')"
    )


@SSE_PARAMS
async def test_inconclusive_32021(sse: bool) -> None:
    data = {"requiredCapabilities": ["sampling"]}
    server = scripted(
        lambda b, _r: error_response(
            b, -32021, "Missing required client capability", status=400, data=data, sse=sse
        ),
        sse=sse,
    )
    out, _ctx = await run_scripted(server)
    assert out.status is Status.INCONCLUSIVE
    assert warning_codes(out) == {"discover.client_capability_demanded"}
    assert out.summary == 'Not evaluated: server demands a client capability (-32021: ["sampling"])'


async def test_fail_32602() -> None:
    server = scripted(lambda b, _r: error_response(b, -32602, "Invalid params", status=400))
    out, _ctx = await run_scripted(server)
    assert failure_codes(out) == {"discover.invalid_params_on_valid_request"}


@pytest.mark.parametrize(
    ("code", "status", "extra"),
    [
        (-32002, 404, "discover.forbidden_error_code"),
        (-32042, 400, "discover.forbidden_error_code"),
        (-32050, 400, "discover.forbidden_error_code"),
        (-32000, 500, None),  # 5xx is infrastructure: never judged
    ],
)
async def test_fail_forbidden_error_code(code: int, status: int, extra: str | None) -> None:
    server = scripted(lambda b, _r: error_response(b, code, "oops", status=status))
    out, _ctx = await run_scripted(server)
    if status >= 500:
        assert out.status is Status.INCONCLUSIVE
        return
    assert out.status is Status.FAIL
    assert failure_codes(out) == {"discover.unexpected_error", extra}
    assert out.summary == f"`server/discover` rejected a valid request ({code}: oops)"


async def test_warn_legacy_error_code_range() -> None:
    server = scripted(lambda b, _r: error_response(b, -32000, "Server error", status=400))
    out, _ctx = await run_scripted(server)
    assert out.status is Status.FAIL
    assert failure_codes(out) == {"discover.unexpected_error"}
    assert warning_codes(out) == {"discover.legacy_error_code"}


async def test_result_and_error_is_envelope_not_rpc_error() -> None:
    def route(body: dict[str, Any], _req: httpx2.Request) -> httpx2.Response:
        msg = {"jsonrpc": "2.0", "id": body["id"], "result": base_result()}
        msg["error"] = {"code": -32603, "message": "also failed"}
        return httpx2.Response(200, json=msg)

    out, _ctx = await run_scripted(scripted(route))
    assert failure_codes(out) == {"discover.envelope_invalid"}
    assert out.summary == "`server/discover` response carries both result and error"


# ------------------------------------------------------------------ L1: HTTP-level and infra rows


@pytest.mark.parametrize(
    ("status", "token", "fragment"),
    [
        (401, None, "HTTP 401 (pass --token)"),
        (401, "s3cret", "HTTP 401 (token rejected)"),
        (403, None, "HTTP 403 (try --origin)"),
        (407, None, "HTTP 407"),
        (500, None, "HTTP 500"),
        (503, None, "HTTP 503"),
        (429, None, "HTTP 429"),
    ],
)
async def test_inconclusive_infra_statuses(status: int, token: str | None, fragment: str) -> None:
    cfg = ClientConfig(url=URL, token=token)
    out, _ctx = await run_scripted(http_server(status), cfg=cfg)
    assert out.status is Status.INCONCLUSIVE, out.summary
    assert out.failures == []
    assert fragment in out.summary
    assert out.summary.startswith("Not evaluated: ")


async def test_inconclusive_401_with_www_authenticate_body() -> None:
    server = http_server(401, body="unauthorized", headers={"www-authenticate": "Bearer"})
    out, _ctx = await run_scripted(server)
    assert out.status is Status.INCONCLUSIVE
    assert "pass --token" in out.summary


async def test_inconclusive_403_with_jsonrpc_body() -> None:
    server = scripted(lambda b, _r: error_response(b, -32000, "forbidden", status=403))
    out, _ctx = await run_scripted(server)
    assert out.status is Status.INCONCLUSIVE
    assert out.failures == []


@pytest.mark.parametrize("status", [404, 406, 410, 421])
async def test_inconclusive_not_mcp_endpoint(status: int) -> None:
    server = http_server(status, body="<html>nope</html>", headers={"content-type": "text/html"})
    out, _ctx = await run_scripted(server)
    assert out.status is Status.INCONCLUSIVE
    assert "no MCP endpoint at URL" in out.summary


async def test_fail_405_post_rejected() -> None:
    server = http_server(405, headers={"allow": "GET"})
    out, _ctx = await run_scripted(server)
    assert out.status is Status.FAIL
    assert failure_codes(out) == {"discover.post_rejected"}
    assert out.summary == "`server/discover` returned HTTP 405"


async def test_fail_202() -> None:
    out, _ctx = await run_scripted(http_server(202))
    assert out.status is Status.FAIL
    assert failure_codes(out) == {"discover.not_jsonrpc"}
    assert out.summary == "`server/discover` returned HTTP 202"


async def test_fail_html_200() -> None:
    server = http_server(200, body="<html>MCP</html>", headers={"content-type": "text/html"})
    out, _ctx = await run_scripted(server)
    assert failure_codes(out) == {"discover.not_jsonrpc"}
    assert out.summary == "`server/discover` response is not JSON-RPC (Content-Type text/html)"


async def test_fail_json_array() -> None:
    def route(body: dict[str, Any], _req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, json=[{"jsonrpc": "2.0", "id": body["id"], "result": base_result()}]
        )

    out, _ctx = await run_scripted(scripted(route))
    assert failure_codes(out) == {"discover.not_jsonrpc"}
    assert "JSON array" in out.summary


async def test_fail_json_without_content_type_still_validates_body() -> None:
    def route(body: dict[str, Any], _req: httpx2.Request) -> httpx2.Response:
        result = payload("ttl-missing")
        content = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result}).encode()
        return httpx2.Response(200, content=content)  # httpx2 adds no content-type here

    server = scripted(route)
    out, ctx = await run_scripted(server)
    assert failure_codes(out) == {"discover.not_jsonrpc", "discover.ttl_missing"}
    assert (
        out.summary == "`server/discover` response is not JSON-RPC (Content-Type no Content-Type)"
    )
    assert ctx.server_info == SERVER_INFO  # body was still read
    assert len(server.methods()) == 5  # cross-check still ran


@SSE_PARAMS
async def test_fail_wrong_id_type_sensitive(sse: bool) -> None:
    def route(body: dict[str, Any], _req: httpx2.Request) -> httpx2.Response:
        msg = {"jsonrpc": "2.0", "id": str(body["id"]), "result": base_result()}
        if sse:
            return httpx2.Response(
                200,
                headers={"content-type": "text/event-stream", **SSE_HEADERS},
                content=sse_body(msg),
            )
        return httpx2.Response(200, json=msg)

    t0 = time.perf_counter()
    out, _ctx = await run_scripted(scripted(route, sse=sse))
    assert time.perf_counter() - t0 < 1.0  # the SDK would hang 10 s here
    assert out.status is Status.FAIL
    assert failure_codes(out) == {"discover.id_mismatch"}
    assert out.summary == '`server/discover` response id "1" does not match request id 1'


async def test_fail_null_id() -> None:
    def route(_body: dict[str, Any], _req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": None, "result": base_result()})

    out, _ctx = await run_scripted(scripted(route))
    assert failure_codes(out) == {"discover.id_mismatch"}
    assert out.summary == "`server/discover` response id null does not match request id 1"


async def test_fail_jsonrpc_version_and_result_not_object() -> None:
    def route(body: dict[str, Any], _req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"jsonrpc": "1.0", "id": body["id"], "result": []})

    out, _ctx = await run_scripted(scripted(route))
    assert out.status is Status.FAIL
    assert [f.code for f in out.failures] == [
        "discover.envelope_invalid",
        "discover.envelope_invalid",
    ]
    assert out.summary == '`server/discover` response "jsonrpc" is not "2.0"'


async def test_inconclusive_timeout() -> None:
    async def boom(_req: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow")

    cfg = ClientConfig(url=URL)
    transcript = Transcript()
    async with RawClient(cfg, transcript, transport=httpx2.MockTransport(boom)) as raw:
        out, _ctx = await run_check(raw, cfg, transcript)
    assert out.status is Status.INCONCLUSIVE
    assert out.summary.startswith("Not evaluated: ReadTimeout")


async def test_inconclusive_body_too_large() -> None:
    def route(body: dict[str, Any], _req: httpx2.Request) -> httpx2.Response:
        result = base_result()
        result["instructions"] = "x" * (wire.MAX_BODY_BYTES + 1)
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    out, _ctx = await run_scripted(scripted(route))
    assert out.status is Status.INCONCLUSIVE
    assert "16 MiB cap" in out.summary


async def _redirect_run(location: str, status: int) -> tuple[CheckResult, list[str]]:
    paths: list[str] = []
    server = good_server()

    async def handler(req: httpx2.Request) -> httpx2.Response:
        paths.append(req.url.path)
        if req.url.path == "/mcp":
            return httpx2.Response(status, headers={"location": location})
        return await server.handler(req)

    cfg = ClientConfig(url=URL)
    transcript = Transcript()
    async with RawClient(cfg, transcript, transport=httpx2.MockTransport(handler)) as raw:
        out, _ctx = await run_check(raw, cfg, transcript)
    return out, paths


async def test_pass_redirect_307_same_origin_followed() -> None:
    out, paths = await _redirect_run("http://127.0.0.1:1/mcp-v2", 307)
    assert out.status is Status.PASS, out.summary
    assert paths[:2] == ["/mcp", "/mcp-v2"]


async def test_inconclusive_redirect_302() -> None:
    out, paths = await _redirect_run("http://127.0.0.1:1/mcp-v2", 302)
    assert out.status is Status.INCONCLUSIVE
    assert "redirects (302)" in out.summary
    assert paths == ["/mcp"]


async def test_inconclusive_redirect_cross_origin_307() -> None:
    out, paths = await _redirect_run("http://other.example/mcp", 307)
    assert out.status is Status.INCONCLUSIVE
    assert "other.example" in out.summary
    assert paths == ["/mcp"]


# ------------------------------------------------------------------ L1: SSE rows (scripted)


async def test_warn_sse_no_accel_buffering() -> None:
    server = scripted(lambda b, _r: ok(b, base_result(), sse=True), sse=True)
    out, _ctx = await run_scripted(server)
    assert out.status is Status.WARN
    assert warning_codes(out) == {"discover.no_accel_buffering_header"}
    assert level_of(out, "discover.no_accel_buffering_header") is Level.SHOULD


async def test_fail_sse_notifications_message_and_server_request() -> None:
    note = {
        "jsonrpc": "2.0",
        "method": "notifications/message",
        "params": {"level": "info", "data": "hi"},
    }
    request = {"jsonrpc": "2.0", "id": "srv-1", "method": "roots/list"}
    progress = {
        "jsonrpc": "2.0",
        "method": "notifications/progress",
        "params": {"progressToken": "p", "progress": 1},
    }
    other = {"jsonrpc": "2.0", "method": "notifications/resources/updated", "params": {"uri": "x"}}
    server = scripted(
        discover_route(base_result(), sse=True, extra_messages=(note, request, progress, other)),
        sse=True,
    )
    out, _ctx = await run_scripted(server)
    assert out.status is Status.FAIL
    assert failure_codes(out) == {
        "discover.log_without_loglevel",
        "discover.server_request_on_stream",
    }
    assert "discover.progress_notification" in info_codes(out)
    assert "notifications/resources/updated" in out.details


async def test_fail_sse_two_responses() -> None:
    def route(body: dict[str, Any], _req: httpx2.Request) -> httpx2.Response:
        msg = {"jsonrpc": "2.0", "id": body["id"], "result": base_result()}
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream", **SSE_HEADERS},
            content=sse_body(msg, msg),
        )

    out, _ctx = await run_scripted(scripted(route, sse=True))
    assert failure_codes(out) == {"discover.multiple_responses"}


async def test_fail_sse_empty_stream() -> None:
    def route(_body: dict[str, Any], _req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream", **SSE_HEADERS},
            content=b": nothing\n\n",
        )

    out, _ctx = await run_scripted(scripted(route, sse=True))
    assert failure_codes(out) == {"discover.no_response"}
    assert out.summary == "`server/discover` stream carried no JSON-RPC response"


# ------------------------------------------------------------------ L1: capability cross-check


def caps_server(
    caps: dict[str, Any],
    *,
    tools: Route | None = None,
    prompts: Route | None = None,
    resources: Route | None = None,
    templates: Route | None = None,
) -> ScriptedServer:
    server = good_server(capabilities=caps)
    for method, route in (
        ("tools/list", tools),
        ("prompts/list", prompts),
        ("resources/list", resources),
        ("resources/templates/list", templates),
    ):
        if route is not None:
            server.routes[method] = route
    return server


def not_found(body: dict[str, Any], _req: httpx2.Request) -> httpx2.Response:
    return error_response(body, -32601, "Method not found", status=404)


def server_error(body: dict[str, Any], _req: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(500, content=b"boom")


def empty_list(key: str) -> Route:
    return lambda b, _r: ok(b, list_result([], key))


async def test_fail_xcheck_undeclared_tools() -> None:
    server = caps_server({"prompts": {}, "resources": {}})
    out, _ctx = await run_scripted(server)
    assert out.status is Status.FAIL
    assert failure_codes(out) == {"discover.capability_undeclared"}
    assert (
        out.summary
        == "Capabilities inconsistent: tools/list works but capabilities.tools undeclared"
    )
    assert out.failures[0].subject == "capabilities.tools ↔ tools/list"


async def test_fail_xcheck_declared_404() -> None:
    server = caps_server({"tools": {}, "prompts": {}, "resources": {}}, prompts=not_found)
    out, _ctx = await run_scripted(server)
    assert failure_codes(out) == {"discover.capability_declared_but_unimplemented"}
    assert out.summary == (
        "Capabilities inconsistent: capabilities.prompts declared but prompts/list answers -32601"
    )


async def test_warn_xcheck_undeclared_empty() -> None:
    server = caps_server({"tools": {}, "resources": {}})  # prompts/list serves [] undeclared
    out, _ctx = await run_scripted(server)
    assert out.status is Status.WARN
    assert warning_codes(out) == {"discover.capability_undeclared_empty"}


async def test_warn_xcheck_declared_unverifiable() -> None:
    server = caps_server({"tools": {}, "prompts": {}, "resources": {}}, tools=server_error)
    out, _ctx = await run_scripted(server)
    assert out.status is Status.WARN
    assert warning_codes(out) == {"discover.capability_unverified"}


async def test_info_xcheck_undeclared_and_not_implemented_is_consistent() -> None:
    server = caps_server({"tools": {}}, prompts=not_found, resources=not_found, templates=not_found)
    out, _ctx = await run_scripted(server)
    assert out.status is Status.PASS
    consistent = [f for f in out.info if f.code == "discover.capability_consistent"]
    assert len(consistent) == 3


async def test_xcheck_resources_counts_templates() -> None:
    template = {"uriTemplate": "demo://{x}", "name": "t"}
    server = caps_server(
        {"tools": {}, "prompts": {}},
        resources=not_found,
        templates=lambda b, _r: ok(b, list_result([template], "resourceTemplates")),
    )
    out, _ctx = await run_scripted(server)
    assert failure_codes(out) == {"discover.capability_undeclared"}
    assert out.failures[0].subject == "capabilities.resources ↔ resources/templates/list"


async def test_xcheck_null_capability_counts_as_declared() -> None:
    server = caps_server({"tools": None, "prompts": {}, "resources": {}})
    out, _ctx = await run_scripted(server)
    assert failure_codes(out) == {"discover.capability_null"}  # not doubled with undeclared


async def test_xcheck_uses_seeded_tools_page0() -> None:
    server = good_server()
    cfg = ClientConfig(url=URL)
    transcript = Transcript()
    async with server.raw_client(cfg, transcript) as raw:
        gate = await raw.post("tools/list", purpose="internal gate", check="runner")

        def preset(ctx: CheckContext) -> None:
            ctx.seed_tools_page0(gate)

        out, _ctx = await run_check(raw, cfg, transcript, preset=preset)
    assert out.status is Status.PASS
    assert server.methods().count("tools/list") == 1
    assert out.requests == 4


# ------------------------------------------------------------------ L2: misbehaving fixture (ASGI)


@pytest.mark.parametrize("sse", [False, True], ids=["json", "sse"])
@pytest.mark.parametrize("variant", sorted(DISCOVER_PAYLOAD_VARIANTS))
async def test_misbehaving_payload_variants(variant: str, sse: bool) -> None:
    expected_status, expected_codes = WIRE_PAYLOAD_OVERRIDES.get(variant, PAYLOAD_EXPECTED[variant])
    out = await run_misbehaving(Toggles(discover_variant=variant, sse=sse))
    assert out.status is expected_status, (variant, out.summary, codes_of(out))
    assert set(expected_codes) <= codes_of(out), (variant, codes_of(out))
    assert "discover.no_accel_buffering_header" not in codes_of(out)


@pytest.mark.parametrize("sse", [False, True], ids=["json", "sse"])
@pytest.mark.parametrize("variant", sorted(WIRE_ERROR_VARIANTS))
async def test_misbehaving_error_variants(variant: str, sse: bool) -> None:
    expected_status, expected_codes = ERROR_EXPECTED[variant]
    out = await run_misbehaving(Toggles(discover_variant=variant, sse=sse))
    assert out.status is expected_status, (variant, out.summary, codes_of(out))
    assert set(expected_codes) <= codes_of(out), (variant, codes_of(out))
    assert failure_codes(out) <= set(expected_codes), (variant, failure_codes(out))
    assert out.requests == 1  # no cross-check after an error


@pytest.mark.parametrize("variant", sorted(ENVELOPE_EXPECTED))
async def test_misbehaving_envelope_variants(variant: str) -> None:
    expected_status, expected_codes = ENVELOPE_EXPECTED[variant]
    out = await run_misbehaving(Toggles(discover_variant=variant))
    assert out.status is expected_status, (variant, out.summary, codes_of(out))
    assert set(expected_codes) <= codes_of(out), (variant, codes_of(out))
    if expected_status is Status.FAIL:
        assert failure_codes(out) == set(expected_codes), (variant, failure_codes(out))


async def test_misbehaving_baseline_passes() -> None:
    out = await run_misbehaving(Toggles())
    assert out.status is Status.PASS, (out.summary, codes_of(out))
    assert out.warnings == []


async def test_misbehaving_sse_alone_passes() -> None:
    out = await run_misbehaving(Toggles(sse=True))
    assert out.status is Status.PASS, (out.summary, codes_of(out))
    assert out.warnings == []


async def test_row_sse_without_accel_buffering_warns() -> None:
    out = await run_misbehaving(Toggles(sse=True, x_accel_buffering=False))
    assert out.status is Status.WARN
    assert warning_codes(out) == {"discover.no_accel_buffering_header"}
    assert level_of(out, "discover.no_accel_buffering_header") is Level.SHOULD


async def test_row_no_discover() -> None:
    out = await run_misbehaving(Toggles(no_discover=True))
    assert out.status is Status.FAIL
    assert failure_codes(out) == {"discover.not_implemented"}
    assert level_of(out, "discover.not_implemented") is Level.MUST


async def test_row_no_discover_wrong_status() -> None:
    out = await run_misbehaving(Toggles(no_discover=True, unknown_method_status=200))
    assert failure_codes(out) == {"discover.not_implemented", "discover.wrong_status_for_32601"}


async def test_row_no_discover_legacy_error_code() -> None:
    out = await run_misbehaving(Toggles(no_discover=True, legacy_error_codes=True))
    assert failure_codes(out) == {"discover.unexpected_error", "discover.forbidden_error_code"}


async def test_row_discover_no_server_info() -> None:
    out = await run_misbehaving(Toggles(discover_no_server_info=True))
    assert out.status is Status.WARN
    assert warning_codes(out) == {"discover.server_info_missing"}
    assert level_of(out, "discover.server_info_missing") is Level.SHOULD
    finding = out.warnings[0]
    assert finding.subject == 'result._meta["io.modelcontextprotocol/serverInfo"]'
    assert finding.message == "server does not identify itself"
    assert finding.evidence is not None
    assert finding.evidence["got"] is None


async def test_row_discover_wrong_versions() -> None:
    out = await run_misbehaving(Toggles(discover_wrong_versions=True))
    assert out.status is Status.WARN
    assert warning_codes(out) == {"discover.supported_versions_omit_served"}
    assert level_of(out, "discover.supported_versions_omit_served") is Level.BEST_PRACTICE


async def test_row_capability_mismatch() -> None:
    out = await run_misbehaving(Toggles(capability_mismatch=True))
    assert out.status is Status.FAIL
    assert failure_codes(out) == {"discover.capability_undeclared"}
    assert level_of(out, "discover.capability_undeclared") is Level.MUST


async def test_row_discover_variant_no_hints() -> None:
    out = await run_misbehaving(Toggles(discover_variant="no-hints"))
    assert failure_codes(out) == {"discover.ttl_missing", "discover.scope_missing"}
    assert out.summary == "`server/discover` result missing ttlMs, cacheScope"


async def test_row_status_401_inconclusive() -> None:
    out = await run_misbehaving(Toggles(status=401))
    assert out.status is Status.INCONCLUSIVE
    assert "--token" in out.summary


async def test_misbehaving_pages_empty_declared_tools_404() -> None:
    out = await run_misbehaving(Toggles(pages=()))
    assert failure_codes(out) == {"discover.capability_declared_but_unimplemented"}


async def test_misbehaving_requests_are_only_allowed_methods() -> None:
    app = misbehaving_server.build_app(Toggles())
    async with asgi_raw(app, lifespan=False) as (raw, cfg, transcript):
        out, _ctx = await run_check(raw, cfg, transcript)
    assert out.status is Status.PASS
    entries = app.state.fixture.log
    assert {(e["verb"], e["method"]) for e in entries} == {
        ("POST", "server/discover"),
        ("POST", "tools/list"),
        ("POST", "prompts/list"),
        ("POST", "resources/list"),
        ("POST", "resources/templates/list"),
    }
    assert len(entries) == 5


# ------------------------------------------------------------------ L2: reference fixture (ASGI)


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"json_response": True}, {"stateless_http": False}, {"empty": True}, {"bare": True}],
    ids=["default", "json", "stateful", "no_primitives", "bare_lowlevel"],
)
async def test_pass_reference(kwargs: dict[str, Any]) -> None:
    out = await run_reference(**kwargs)
    assert out.status is Status.PASS, (out.summary, codes_of(out))
    assert out.failures == []
    assert out.warnings == []
    assert out.summary == "`server/discover` responds correctly"


async def test_reference_bare_cross_check_consistent() -> None:
    out = await run_reference(bare=True)
    consistent = [f for f in out.info if f.code == "discover.capability_consistent"]
    assert len(consistent) == 3
    assert all("answers -32601" in f.message for f in consistent)
    assert (
        consistent[2].subject == "capabilities.resources ↔ resources/list, resources/templates/list"
    )


async def test_reference_require_token() -> None:
    out = await run_reference(require_token="s3cret")
    assert out.status is Status.INCONCLUSIVE
    assert "pass --token" in out.summary
    out = await run_reference(require_token="s3cret", _token="s3cret")
    assert out.status is Status.PASS, out.summary


async def test_reference_sets_context() -> None:
    async with asgi_raw(reference_server.build_app(), lifespan=True) as (raw, cfg, transcript):
        out, ctx = await run_check(raw, cfg, transcript)
    assert out.status is Status.PASS
    assert ctx.server_info == {
        "name": reference_server.SERVER_NAME,
        "version": reference_server.SERVER_VERSION,
    }
    assert ctx.server_supported_versions == [PV]
    assert ctx.protocol_version == PV


# ------------------------------------------------------------------ L3: TCP hazards


@pytest.mark.integration
async def test_pass_reference_tcp(reference_url: str) -> None:
    out, _elapsed = await run_tcp(reference_url)
    assert out.status is Status.PASS, (out.summary, codes_of(out))
    assert out.warnings == []


@pytest.mark.integration
async def test_tcp_request_log_only_allowed_requests(
    misbehaving_url: Callable[[Toggles], str],
) -> None:
    url = misbehaving_url(Toggles())
    out, _elapsed = await run_tcp(url)
    assert out.status is Status.PASS
    pairs = {(e["verb"], e["method"]) for e in fetch_request_log(url)}
    assert pairs == {
        ("POST", "server/discover"),
        ("POST", "tools/list"),
        ("POST", "prompts/list"),
        ("POST", "resources/list"),
        ("POST", "resources/templates/list"),
    }


@pytest.mark.integration
@pytest.mark.slow
async def test_tcp_sse_not_terminated(misbehaving_url: Callable[[Toggles], str]) -> None:
    url = misbehaving_url(Toggles(discover_variant="sse-not-terminated"))
    out, elapsed = await run_tcp(url, request_timeout=5.0)
    assert out.status is Status.WARN, (out.summary, codes_of(out))
    assert warning_codes(out) == {"discover.stream_not_terminated"}
    assert wire.SSE_TERMINATION_GRACE_S <= elapsed < wire.SSE_TERMINATION_GRACE_S + 4


@pytest.mark.integration
@pytest.mark.slow
async def test_tcp_slow_server_inconclusive_within_budget(
    misbehaving_url: Callable[[Toggles], str],
) -> None:
    url = misbehaving_url(Toggles(slow_seconds=2.0))
    out, elapsed = await run_tcp(url, request_timeout=0.5)
    assert out.status is Status.INCONCLUSIVE
    assert "0.5s total budget" in out.summary
    assert elapsed < 1.5


@pytest.mark.integration
@pytest.mark.slow
async def test_tcp_endless_sse_inconclusive_never_hangs(
    misbehaving_url: Callable[[Toggles], str],
) -> None:
    url = misbehaving_url(Toggles(endless_sse=True))
    with anyio.fail_after(10):
        out, elapsed = await run_tcp(url, request_timeout=1.0)
    assert out.status is Status.INCONCLUSIVE
    assert out.failures == []
    assert elapsed < 3.0


# ------------------------------------------------------------------ registry sanity


def test_every_emitted_code_is_registered_at_the_right_severity() -> None:
    """``Findings`` raises on misuse; drive every pure item through it."""
    check = DiscoverCheck()
    for variant in DISCOVER_PAYLOAD_VARIANTS:
        f = Findings(check)
        for item in validate_discover_result(payload(variant)):
            getattr(f, item.severity.replace("-", "_"))(
                item.code, item.message, subject=item.subject, spec_url=item.spec_url
            )


# --------------------------------------------------------------------------- -32021 shape MUSTs (B:389-394)


async def test_capability_demand_shape_musts() -> None:
    from tests.helpers.scripted import error_response

    def route(body: dict[str, Any], _req: httpx2.Request) -> httpx2.Response:
        return error_response(
            body,
            -32021,
            "Missing capability",
            status=200,
            data={"requiredCapabilities": "sampling"},
        )

    out, _ctx = await run_scripted(scripted(route))
    assert out.status is Status.FAIL
    assert warning_codes(out) == {"discover.client_capability_demanded"}
    assert failure_codes(out) == {
        "discover.capability_error_malformed",
        "discover.wrong_status_for_32021",
    }
    malformed = next(f for f in out.failures if f.code == "discover.capability_error_malformed")
    assert malformed.subject == "rpc.error.data.requiredCapabilities"
    assert malformed.evidence == {"got": "sampling", "expected": '["<capability>", ...]'}
    status = next(f for f in out.failures if f.code == "discover.wrong_status_for_32021")
    assert status.evidence is not None
    assert status.evidence["http_status"] == 200
    assert status.evidence["expected"] == 400


async def test_capability_demand_conformant_stays_inconclusive_only() -> None:
    from tests.helpers.scripted import error_response

    def route(body: dict[str, Any], _req: httpx2.Request) -> httpx2.Response:
        return error_response(
            body,
            -32021,
            "Missing capability",
            status=400,
            data={"requiredCapabilities": ["sampling"]},
        )

    out, _ctx = await run_scripted(scripted(route))
    assert out.status is Status.INCONCLUSIVE
    assert failure_codes(out) == set()
    assert warning_codes(out) == {"discover.client_capability_demanded"}
