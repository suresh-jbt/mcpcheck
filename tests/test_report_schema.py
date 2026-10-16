"""The shipped JSON Schema: in sync, metaschema-valid, example-valid, repo copy identical (§8.7, §10.7)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema  # type: ignore[import-untyped,unused-ignore]
import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped,unused-ignore]

from mcpcheck.models import report_json_schema
from mcpcheck.output import report_to_json
from tests.helpers.reports import fake_report

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_COPY = ROOT / "src" / "mcpcheck" / "schema" / "report.v1.json"
REPO_COPY = ROOT / "schema" / "mcpcheck-report.v1.schema.json"


def shipped() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(PACKAGE_COPY.read_text(encoding="utf-8"))
    return data


def test_schema_files_in_sync_with_model() -> None:
    expected = json.dumps(report_json_schema(), indent=2, ensure_ascii=False) + "\n"
    assert PACKAGE_COPY.read_text(encoding="utf-8") == expected, "run scripts/gen_report_schema.py"


def test_repo_copy_byte_identical() -> None:
    assert REPO_COPY.read_bytes() == PACKAGE_COPY.read_bytes()


def test_schema_is_valid_2020_12() -> None:
    schema = shipped()
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"].endswith("/schema/mcpcheck-report.v1.schema.json")
    assert schema["title"] == "mcpcheck report"
    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema_version"]["const"] == 1
    assert set(schema["required"]) >= {
        "schema_version",
        "mcpcheck_version",
        "sdk_version",
        "python_version",
        "spec_revision",
        "server",
        "request",
        "options",
        "protocol_version",
        "server_supported_versions",
        "server_info",
        "era",
        "timestamp",
        "duration_ms",
        "checks",
        "summary",
    }
    assert schema["$defs"]["CheckName"]["enum"] == [
        "handshake",
        "session",
        "discover",
        "protocol",
        "schemas",
        "errors",
        "cache",
        "origin",
    ]
    assert schema["$defs"]["Status"]["enum"] == ["pass", "warn", "fail", "inconclusive", "error"]
    assert schema["$defs"]["Level"]["enum"] == ["must", "should", "best-practice"]
    assert len(schema["$defs"]["CheckResult"]["allOf"]) == 3


@pytest.mark.parametrize("variant", ["all_pass", "mixed", "unreachable"])
def test_examples_validate(variant: str) -> None:
    data = json.loads(report_to_json(fake_report(variant)))
    jsonschema.validate(data, shipped(), cls=Draft202012Validator)


def _validator() -> Draft202012Validator:
    return Draft202012Validator(shipped())


def test_negative_fail_with_empty_failures() -> None:
    data = json.loads(report_to_json(fake_report("mixed")))
    bad = next(c for c in data["checks"] if c["status"] == "fail")
    bad["failures"] = []
    assert not _validator().is_valid(data)


def test_negative_exit_code_3() -> None:
    data = json.loads(report_to_json(fake_report()))
    data["summary"]["exit_code"] = 3
    assert not _validator().is_valid(data)


def test_negative_bad_code_case() -> None:
    data = json.loads(report_to_json(fake_report("mixed")))
    data["checks"][4]["failures"][0]["code"] = "Discover.X"
    assert not _validator().is_valid(data)


def test_negative_timestamp_offset() -> None:
    data = json.loads(report_to_json(fake_report()))
    data["timestamp"] = "2026-10-16T14:32:11+00:00"
    assert not _validator().is_valid(data)


def test_negative_extra_top_level_key() -> None:
    data = json.loads(report_to_json(fake_report()))
    data["transcript"] = []
    assert not _validator().is_valid(data)


def test_negative_pass_with_warning() -> None:
    data = json.loads(report_to_json(fake_report()))
    data["checks"][0]["warnings"].append(
        {
            "code": "cache.cursor_loop",
            "level": "should",
            "subject": "s",
            "message": "m",
            "issue": "m",
        }
    )
    assert not _validator().is_valid(data)
