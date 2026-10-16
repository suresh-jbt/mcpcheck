"""Runner-level rules (PLAN.md §6.9-§6.10, §6.13): the gate and the central report scrub."""

from __future__ import annotations

import json
from urllib.parse import quote

from mcpcheck import runner, spec_urls, wire
from mcpcheck.models import CheckName, CheckResult, Finding, Level, Status
from tests.helpers.reports import fake_report

TOKEN = "sekret-XYZ"
API_KEY = "k-123"


def _handshake(status: Status, *codes: str) -> CheckResult:
    findings = [
        Finding(
            code=code,
            level=Level.MUST,
            subject="tools/list",
            message=f"{code} happened",
            spec_url=spec_urls.SPEC_SENDING_MESSAGES,
        )
        for code in codes
    ]
    return CheckResult(
        name=CheckName.HANDSHAKE,
        title="Server does not require initialize",
        status=status,
        summary="Non-MCP response to `tools/list` (HTTP 200 text/html)",
        spec_url=spec_urls.SPEC_STATELESSNESS,
        failures=findings,
        duration_ms=1,
    )


def test_gate_blocks_on_a_non_jsonrpc_handshake_only() -> None:
    not_jsonrpc = _handshake(Status.FAIL, "handshake.not_jsonrpc")
    assert runner.gate_reason(not_jsonrpc) == not_jsonrpc.summary
    # a shape failure next to it means the server did answer JSON-RPC: dependents run
    mixed = _handshake(Status.FAIL, "handshake.not_jsonrpc", "handshake.id_mismatch")
    assert runner.gate_reason(mixed) is None
    assert runner.gate_reason(_handshake(Status.FAIL, "handshake.id_mismatch")) is None
    assert runner.gate_reason(_handshake(Status.PASS)) is None
    era = _handshake(Status.FAIL, "handshake.initialize_required")
    assert runner.gate_reason(era) == era.summary


def test_scrub_report_removes_every_form_of_every_secret() -> None:
    report = fake_report("mixed", strict=True)
    blob = f"auth=Bearer {TOKEN} apikey={API_KEY}"
    report.server_info = {
        "name": f"reflect {blob}",
        "version": "1",
        "websiteUrl": f"https://x/?{blob}",
    }
    check = report.checks[0]
    check.summary = f"Not evaluated: redirects (302) to https://evil.example.net/leak?{blob}"
    check.details = f"probe → {blob}"
    finding = Finding(
        code="protocol.header_mismatch_wrong_status",
        level=Level.MUST,
        subject=f"probe 1: {blob}",
        message=f"server said {blob}",
        spec_url=spec_urls.SPEC_SERVER_VALIDATION,
        evidence={
            "rpc_message": blob,
            "data": {
                "echo": {"authorization": f"Bearer {TOKEN}", "x-api-key": API_KEY, "x-custom": "c"}
            },
            "location": f"https://evil.example.net/?t={quote(TOKEN, safe='')}",
        },
    )
    check.failures.append(finding)
    before = json.dumps(report.model_dump(mode="json"))
    assert TOKEN in before

    secrets = wire.secret_needles([TOKEN, API_KEY])
    scrubbed = runner.scrub_report(report, secrets)
    text = json.dumps(scrubbed.model_dump(mode="json"))
    assert TOKEN not in text
    assert API_KEY not in text
    assert "sekret" not in text
    assert scrubbed.server_info is not None
    assert (
        scrubbed.server_info["name"]
        == f"reflect auth=Bearer {wire.REDACTED} apikey={wire.REDACTED}"
    )
    out = scrubbed.checks[0]
    assert out.summary.endswith(f"leak?auth=Bearer {wire.REDACTED} apikey={wire.REDACTED}")
    ev = out.failures[-1].evidence
    assert ev is not None
    assert ev["data"]["echo"]["authorization"] == wire.REDACTED
    assert ev["data"]["echo"]["x-api-key"] == wire.REDACTED
    assert ev["data"]["echo"]["x-custom"] == "c"
    # the computed fields and the --strict flag survive the round trip
    assert [c.passed for c in scrubbed.checks] == [c.passed for c in report.checks]
    assert [c.severity for c in scrubbed.checks] == [c.severity for c in report.checks]
    assert scrubbed.summary == report.summary
    # the original report object was not modified
    assert TOKEN in report.server_info["name"]


def test_scrub_report_without_secrets_is_identity() -> None:
    report = fake_report()
    assert runner.scrub_report(report, ()) is report
