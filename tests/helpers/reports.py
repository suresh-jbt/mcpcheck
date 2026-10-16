"""Deterministic fake ``Report`` objects for output/schema/CLI tests (no network)."""

from __future__ import annotations

from mcpcheck import spec_urls as su
from mcpcheck.checks import REGISTRY
from mcpcheck.models import (
    CheckError,
    CheckName,
    CheckResult,
    Era,
    Finding,
    Level,
    Options,
    Report,
    Request,
    Status,
    Summary,
    aggregate_exit_code,
)

SERVER = "https://api.example.com/mcp"
TIMESTAMP = "2026-10-16T14:32:11Z"
TITLES = {cls.name: cls.title for cls in REGISTRY}
SPEC = {cls.name: cls.spec_url for cls in REGISTRY}

PASS_SUMMARIES = {
    "handshake": "Server does not require `initialize`",
    "session": "No `Mcp-Session-Id` issued or expected; lists identical across 3 connections",
    "discover": "`server/discover` responds correctly",
    "protocol": "Header validation and version errors conform",
    "schemas": "All 12 tools publish valid schemas",
    "errors": "Tool errors carry actionable messages",
    "cache": "Cache hints valid on 4 list methods (6 pages)",
    "origin": "Origin header validated",
}


def _check(
    name: str,
    status: Status,
    summary: str,
    *,
    failures: list[Finding] | None = None,
    warnings: list[Finding] | None = None,
    info: list[Finding] | None = None,
    error: CheckError | None = None,
    details: str = "",
    requests: int = 1,
    strict: bool = False,
) -> CheckResult:
    return CheckResult(
        name=CheckName(name),
        title=TITLES[name],
        status=status,
        summary=summary,
        details=details,
        spec_url=SPEC[name],
        failures=failures or [],
        warnings=warnings or [],
        info=info or [],
        duration_ms=42,
        requests=requests,
        error=error,
    ).with_strict(strict)


def _all_pass(strict: bool) -> list[CheckResult]:
    return [_check(n, Status.PASS, PASS_SUMMARIES[n], strict=strict) for n in TITLES]


def _mixed(strict: bool) -> list[CheckResult]:
    serverinfo = Finding(
        code="discover.server_info_missing",
        level=Level.SHOULD,
        subject='result._meta["io.modelcontextprotocol/serverInfo"]',
        message="server does not identify itself",
        spec_url=su.SPEC_DISCOVER_RESULT,
        evidence={"got": None, "expected": '{"name": string, "version": string}'},
    )
    schema_fail = Finding(
        code="schemas.input_schema_invalid",
        level=Level.MUST,
        subject="tool:search_orders",
        message="inputSchema is not a valid JSON Schema 2020-12: 'nonsense' is not valid under any of the given schemas",
        spec_url=su.SPEC_SCHEMA_VALIDATION,
        tool="search_orders",
        evidence={"path": "$.tools[4].inputSchema.type", "page": 0},
    )
    out_missing = [
        Finding(
            code="schemas.output_schema_missing",
            level=Level.BEST_PRACTICE,
            subject=f"tool:{t}",
            message="outputSchema absent; optional per spec",
            spec_url=su.SPEC_OUTPUT_SCHEMA,
            tool=t,
        )
        for t in ("get_invoice", "list_orders", "ship_order", "refund")
    ]
    cache_fails = [
        Finding(
            code="cache.ttl_missing",
            level=Level.MUST,
            subject="prompts/list",
            message="result has no ttlMs",
            spec_url=su.SPEC_CACHEABLE_RESULTS,
            evidence={
                "method": "prompts/list",
                "page": 0,
                "got": None,
                "expected": "JSON integer >= 0",
            },
        ),
        Finding(
            code="cache.scope_missing",
            level=Level.MUST,
            subject="prompts/list",
            message="result has no cacheScope",
            spec_url=su.SPEC_CACHE_SCOPE,
            evidence={
                "method": "prompts/list",
                "page": 0,
                "got": None,
                "expected": '"public" | "private"',
            },
        ),
    ]
    return [
        _check("handshake", Status.PASS, PASS_SUMMARIES["handshake"], strict=strict),
        _check("session", Status.PASS, PASS_SUMMARIES["session"], requests=14, strict=strict),
        _check(
            "discover",
            Status.WARN,
            "`server/discover` responds correctly (1 warning)",
            warnings=[serverinfo],
            strict=strict,
        ),
        _check("protocol", Status.PASS, PASS_SUMMARIES["protocol"], requests=8, strict=strict),
        _check(
            "schemas",
            Status.FAIL,
            "2 of 12 tools have invalid inputSchema (+3 more issues)",
            failures=[
                schema_fail,
                schema_fail.model_copy(update={"subject": "tool:get_quote", "tool": "get_quote"}),
            ],
            warnings=out_missing,
            requests=0,
            strict=strict,
        ),
        _check(
            "errors",
            Status.INCONCLUSIVE,
            "No probe-safe tools (12 listed; annotate readOnlyHint/destructiveHint or use --probe all)",
            requests=1,
            strict=strict,
        ),
        _check(
            "cache",
            Status.FAIL,
            "2 cache-hint violations: prompts/list ttlMs missing; prompts/list cacheScope missing",
            failures=cache_fails,
            info=[
                Finding(
                    code="cache.resources_read_not_probed",
                    level=Level.BEST_PRACTICE,
                    subject="resources/read",
                    message="resources/read hints not verified (read-only policy)",
                )
            ],
            requests=0,
            strict=strict,
        ),
        _check("origin", Status.PASS, PASS_SUMMARIES["origin"], requests=5, strict=strict),
    ]


def _unreachable(strict: bool) -> list[CheckResult]:
    first = "cannot connect to https://api.example.com/mcp: ConnectError: All connection attempts failed"
    out = [
        _check(
            "handshake",
            Status.ERROR,
            f"Error: {first}",
            error=CheckError(type="ServerUnreachable", message=first),
            requests=1,
            strict=strict,
        )
    ]
    out.extend(
        _check(
            n,
            Status.ERROR,
            "Error: not run: server unreachable (ConnectError)",
            error=CheckError(
                type="RunAborted", message="not run: server unreachable (ConnectError)"
            ),
            requests=0,
            strict=strict,
        )
        for n in list(TITLES)[1:]
    )
    return out


def _warn_only(strict: bool) -> list[CheckResult]:
    checks = _all_pass(strict)
    checks[2] = _check(
        "discover",
        Status.WARN,
        "`server/discover` responds correctly (1 warning)",
        warnings=[
            Finding(
                code="discover.server_info_missing",
                level=Level.SHOULD,
                subject='result._meta["io.modelcontextprotocol/serverInfo"]',
                message="server does not identify itself",
                spec_url=su.SPEC_DISCOVER_RESULT,
            )
        ],
        strict=strict,
    )
    return checks


VARIANTS = {
    "all_pass": _all_pass,
    "mixed": _mixed,
    "unreachable": _unreachable,
    "warn_only": _warn_only,
}


def fake_report(variant: str = "all_pass", *, strict: bool = False, token: bool = True) -> Report:
    """A complete, schema-valid report for ``variant`` ∈ VARIANTS."""
    checks = VARIANTS[variant](strict)
    statuses = [c.status for c in checks]
    unreachable = variant == "unreachable"
    return Report(
        mcpcheck_version="0.1.0",
        sdk_version="2.3.0",
        python_version="3.13.5",
        server=SERVER,
        request=Request(
            origin=None, auth="bearer" if token else "none", extra_headers=[], tls_verify=True
        ),
        options=Options(
            strict=strict, probe="safe", max_tools=20, timeout=10.0, legacy_probes=False
        ),
        protocol_version=None if unreachable else "2026-07-28",
        server_supported_versions=None if unreachable else ["2026-07-28"],
        server_info=None
        if unreachable or variant == "mixed"
        else {"name": "example-server", "version": "2.4.0"},
        era=Era.UNKNOWN,
        timestamp=TIMESTAMP,
        duration_ms=1342,
        checks=checks,
        summary=Summary(
            total=len(checks),
            passed=statuses.count(Status.PASS),
            warned=statuses.count(Status.WARN),
            failed=statuses.count(Status.FAIL),
            inconclusive=statuses.count(Status.INCONCLUSIVE),
            errored=statuses.count(Status.ERROR),
            skipped=[],
            exit_code=aggregate_exit_code(statuses, strict=strict),
        ),
    )


__all__ = ["SERVER", "TIMESTAMP", "VARIANTS", "fake_report"]
