"""Finding-code registry tests (§7 intro, §10.7)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mcpcheck import spec_urls
from mcpcheck.checks import NAMES
from mcpcheck.checks.base import Check, Findings
from mcpcheck.checks.codes import CODE_RE, FINDING_CODES, lookup
from mcpcheck.models import Level

SRC = Path(__file__).resolve().parent.parent / "src" / "mcpcheck"
TESTS = Path(__file__).resolve().parent
CODE_LITERAL_RE = re.compile(r"[\"'](" + "|".join(NAMES) + r"|runner)\.([a-z0-9_]+)[\"']")


def test_every_code_matches_grammar_and_prefix() -> None:
    valid_prefixes = {*NAMES, "runner"}
    for code, info in FINDING_CODES.items():
        assert CODE_RE.match(code), code
        assert code.split(".", 1)[0] in valid_prefixes, code
        assert info.description
        assert info.severity in ("must", "should", "best-practice", "info")
        if info.severity in ("must", "should"):
            assert info.spec_url is not None, code
            assert info.spec_url.startswith(spec_urls.SPEC_BASE), code


def test_every_check_has_codes_and_runner_codes_exist() -> None:
    prefixes = {c.split(".", 1)[0] for c in FINDING_CODES}
    assert set(NAMES) <= prefixes
    assert "runner.not_run" in FINDING_CODES
    assert "runner.internal_error" in FINDING_CODES


def test_plan_examples_registered_with_expected_level() -> None:
    expected = {
        "handshake.initialize_required": "must",
        "handshake.server_info_missing": "should",
        "session.session_id_minted": "should",
        "session.list_changed_during_run": "best-practice",
        "discover.supported_versions_omit_served": "best-practice",
        "discover.capability_undeclared": "must",
        "schemas.output_schema_missing": "best-practice",
        "schemas.x_mcp_header_invalid": "must",
        "errors.accepted_invalid_input": "must",
        "errors.accepted_wrong_type": "should",
        "cache.ttl_missing": "must",
        "cache.scope_inconsistent_pages": "must",
        "cache.public_user_specific_names": "best-practice",
        "origin.evil_accepted": "must",
        "origin.evil_rejected_wrong_status": "must",
        "origin.same_origin_rejected": "best-practice",
        "protocol.header_mismatch_accepted": "must",
        "protocol.headerless_accepted": "should",
    }
    for code, severity in expected.items():
        assert FINDING_CODES[code].severity == severity, code
    assert (
        FINDING_CODES["runner.not_run"].level is Level.BEST_PRACTICE
    )  # info records the lowest level


def test_lookup_unknown_raises_with_hint() -> None:
    with pytest.raises(KeyError, match=r"codes\.py"):
        lookup("cache.nope")


def test_every_code_literal_in_sources_is_registered() -> None:
    files = [*SRC.rglob("*.py"), *TESTS.rglob("*.py")]
    unregistered: set[str] = set()
    for path in files:
        if path.name == "codes.py":
            continue
        for m in CODE_LITERAL_RE.finditer(path.read_text(encoding="utf-8")):
            code = f"{m.group(1)}.{m.group(2)}"
            if code not in FINDING_CODES and code not in {"cache.nope", "discover.x"}:
                unregistered.add(code)
    assert not unregistered, sorted(unregistered)


class _Dummy(Check):
    name = "cache"
    title = "t"
    spec_url = spec_urls.SPEC_CACHEABLE_RESULTS
    order = 1

    async def run(self, ctx: object, f: Findings) -> None:  # pragma: no cover
        raise NotImplementedError


def test_findings_enforce_registry() -> None:
    f = Findings(_Dummy())
    with pytest.raises(KeyError):
        f.must("cache.nope", "m", subject="s")
    with pytest.raises(ValueError, match=r"registered as 'should'"):
        f.must("cache.cursor_loop", "m", subject="s")
    f.must("cache.ttl_missing", "m", subject="tools/list")
    assert f.failures[0].spec_url == spec_urls.SPEC_CACHEABLE_RESULTS
    f.best_practice("origin.same_origin_rejected", "m", subject="origin:same")
    assert f.warnings[0].spec_url is None
    f.info("runner.not_run", "m", subject="x")
    assert f.infos[0].level is Level.BEST_PRACTICE
