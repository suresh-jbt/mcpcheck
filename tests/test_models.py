"""Report model tests (PLAN.md §6.11, §10.7)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mcpcheck import models
from mcpcheck.models import (
    CheckError,
    CheckName,
    CheckResult,
    Finding,
    Level,
    Status,
    aggregate_exit_code,
    exit_code_for,
)


def finding(level: Level = Level.MUST, code: str = "cache.ttl_missing") -> Finding:
    return Finding(
        code=code,
        level=level,
        subject="tools/list",
        message="result has no ttlMs",
        spec_url="https://x",
    )


def test_finding_issue_mirrors_message_and_code_pattern() -> None:
    f = finding()
    dumped = f.model_dump(mode="json")
    assert dumped["issue"] == "result has no ttlMs"
    assert dumped["level"] == "must"
    with pytest.raises(ValidationError):
        Finding(code="Discover.X", level=Level.MUST, subject="s", message="m")
    with pytest.raises(ValidationError):
        Finding(code="cache.ttl_missing", level=Level.MUST, subject="s", message="m" * 501)
    with pytest.raises(ValidationError):
        Finding(code="cache.ttl_missing", level=Level.MUST, subject="s", message="m", extra=1)  # type: ignore[call-arg]


def test_check_result_severity_and_passed() -> None:
    r = CheckResult(
        name=CheckName.CACHE,
        title="t",
        status=Status.FAIL,
        summary="s",
        spec_url="u",
        duration_ms=1,
        failures=[finding()],
        warnings=[finding(Level.BEST_PRACTICE, "cache.ttl_integral_float")],
    )
    assert r.severity is Level.MUST
    assert r.passed is False
    w = CheckResult(
        name=CheckName.CACHE,
        title="t",
        status=Status.WARN,
        summary="s",
        spec_url="u",
        duration_ms=1,
        warnings=[finding(Level.SHOULD, "cache.cursor_loop")],
    )
    assert w.severity is Level.SHOULD
    assert w.passed is True
    assert w.with_strict(True).passed is False
    p = CheckResult(
        name=CheckName.CACHE,
        title="t",
        status=Status.PASS,
        summary="s",
        spec_url="u",
        duration_ms=1,
    )
    assert p.severity is None
    assert p.passed is True
    e = CheckResult(
        name=CheckName.CACHE,
        title="t",
        status=Status.ERROR,
        summary="s",
        spec_url="u",
        duration_ms=1,
        error=CheckError(type="X", message="m"),
    )
    assert e.passed is False
    dumped = e.model_dump(mode="json")
    assert dumped["severity"] is None
    assert dumped["passed"] is False
    assert dumped["error"] == {"type": "X", "message": "m"}


def test_check_result_constraints() -> None:
    with pytest.raises(ValidationError):
        CheckResult(
            name=CheckName.CACHE,
            title="t",
            status=Status.PASS,
            summary="s" * 101,
            spec_url="u",
            duration_ms=1,
        )
    with pytest.raises(ValidationError):
        CheckResult(
            name=CheckName.CACHE,
            title="t",
            status=Status.PASS,
            summary="s",
            spec_url="u",
            duration_ms=-1,
        )


@pytest.mark.parametrize(
    ("status", "default", "strict"),
    [
        (Status.PASS, 0, 0),
        (Status.WARN, 0, 1),
        (Status.INCONCLUSIVE, 0, 1),
        (Status.FAIL, 1, 1),
        (Status.ERROR, 2, 2),
    ],
)
def test_exit_code_table(status: Status, default: int, strict: int) -> None:
    assert exit_code_for(status, strict=False) == default
    assert exit_code_for(status, strict=True) == strict


def test_aggregate_exit_code_precedence() -> None:
    assert aggregate_exit_code([Status.PASS, Status.WARN], strict=False) == 0
    assert aggregate_exit_code([Status.PASS, Status.WARN], strict=True) == 1
    assert aggregate_exit_code([Status.FAIL, Status.ERROR, Status.PASS], strict=False) == 2
    assert aggregate_exit_code([Status.FAIL, Status.INCONCLUSIVE], strict=False) == 1
    assert aggregate_exit_code([], strict=True) == 0


def test_a_run_that_judged_nothing_exits_2() -> None:
    """Every check inconclusive means nothing was verified: "could not run".

    Regression: this exited 0, so `mcpcheck "$URL" && deploy` went green against a URL
    that was not an MCP server at all (a plain web server answering 501, a gateway
    answering 403 — both seen in the wild).
    """
    for strict in (False, True):
        assert aggregate_exit_code([Status.INCONCLUSIVE] * 8, strict=strict) == 2
    # a single inconclusive check alongside real verdicts is still not a failure
    assert aggregate_exit_code([Status.PASS, Status.INCONCLUSIVE], strict=False) == 0
    assert aggregate_exit_code([Status.FAIL, Status.INCONCLUSIVE], strict=False) == 1


def test_enums_are_str_enums_not_strenum() -> None:
    assert isinstance(Status.PASS, str)
    assert Status("pass") is Status.PASS
    assert [c.value for c in CheckName] == [
        "handshake",
        "session",
        "discover",
        "protocol",
        "schemas",
        "errors",
        "cache",
        "origin",
    ]
    assert [e.value for e in models.Era] == ["modern-only", "dual-era", "legacy-only", "unknown"]


def test_report_rejects_bad_timestamp_and_extra_key(
    example_report_kwargs: dict[str, object],
) -> None:
    models.Report(**example_report_kwargs)  # type: ignore[arg-type]
    bad = dict(example_report_kwargs, timestamp="2026-10-16T14:32:11+00:00")
    with pytest.raises(ValidationError):
        models.Report(**bad)  # type: ignore[arg-type]
    bad = dict(example_report_kwargs, unexpected=1)
    with pytest.raises(ValidationError):
        models.Report(**bad)  # type: ignore[arg-type]
    bad = dict(example_report_kwargs, schema_version=2)
    with pytest.raises(ValidationError):
        models.Report(**bad)  # type: ignore[arg-type]


@pytest.fixture
def example_report_kwargs() -> dict[str, object]:
    from tests.helpers.reports import fake_report

    return fake_report().model_dump(
        mode="python", exclude={"checks": {"__all__": {"severity", "passed", "issue"}}}
    )
