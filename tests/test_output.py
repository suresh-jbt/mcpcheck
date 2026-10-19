"""Human/JSON/transcript rendering against golden files (PLAN.md §8.5-§8.7, §10.8).

Set ``MCPCHECK_UPDATE_GOLDENS=1`` to rewrite ``tests/golden/*``; CI never does.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import IO, cast

import httpx2
import pytest
from rich.console import Console

from mcpcheck import __version__, output
from mcpcheck.client import Exchange
from mcpcheck.models import CheckName, Status
from tests.helpers.reports import fake_report

GOLDEN = Path(__file__).parent / "golden"


def console(file: IO[str], *, ascii_only: bool = False, width: int = 100) -> Console:
    if ascii_only:

        class _Ascii(io.TextIOBase):
            encoding = "ascii"

            def write(self, s: str) -> int:
                file.write(s)
                return len(s)

        return Console(
            file=cast("IO[str]", _Ascii()),
            color_system=None,
            highlight=False,
            markup=False,
            soft_wrap=True,
            width=width,
        )
    return Console(
        file=file, color_system=None, highlight=False, markup=False, soft_wrap=True, width=width
    )


def render_human(
    variant: str, *, verbose: int = 0, ascii_only: bool = False, strict: bool = False
) -> str:
    buf = io.StringIO()
    output.HumanRenderer(console(buf, ascii_only=ascii_only), verbose=verbose).render(
        fake_report(variant, strict=strict)
    )
    return buf.getvalue()


def check_golden(name: str, text: str) -> None:
    path = GOLDEN / name
    if os.environ.get("MCPCHECK_UPDATE_GOLDENS"):
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert path.exists(), f"missing golden {path}; run with MCPCHECK_UPDATE_GOLDENS=1"
    assert text == path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("variant", "golden"),
    [("all_pass", "all_pass.txt"), ("mixed", "mixed.txt"), ("unreachable", "unreachable.txt")],
)
def test_human_goldens(variant: str, golden: str) -> None:
    check_golden(golden, render_human(variant))


def test_human_verbose_golden() -> None:
    check_golden("mixed_verbose.txt", render_human("mixed", verbose=1))


def test_json_golden() -> None:
    text = output.report_to_json(fake_report("mixed"))
    check_golden("json_pretty.json", text)
    data = json.loads(text)
    assert list(data)[:3] == ["$schema", "schema_version", "mcpcheck_version"]
    assert data["summary"]["exit_code"] == 1
    assert data["checks"][4]["failures"][0]["issue"] == data["checks"][4]["failures"][0]["message"]


def test_layout_rows_and_header() -> None:
    lines = render_human("mixed").splitlines()
    assert lines[0] == f"mcpcheck {__version__} · MCP 2026-07-28 · https://api.example.com/mcp"
    assert lines[1] == "server: (no serverInfo) · speaks 2026-07-28 · 8 checks"
    assert lines[2] == ""
    assert lines[3] == "✓ handshake     Server does not require `initialize`"
    assert "✗ schemas       2 of 12 tools have invalid inputSchema (+3 more issues)" in lines
    assert (
        "? errors        No probe-safe tools (12 listed; annotate readOnlyHint/destructiveHint or use --probe all)"
        in lines
    )
    assert "    … and 1 more (use --verbose or --json)" in lines
    assert lines[-1] == "2 of 8 checks failed, 1 warning, 1 inconclusive. See --json for details."
    spec_lines = [ln for ln in lines if ln.startswith("    spec: ")]
    assert len(spec_lines) == 3  # discover (warn), schemas (fail), cache (fail)
    rows = [ln for ln in lines[3:] if ln and not ln.startswith(" ")]
    assert [ln[2:16].rstrip() for ln in rows[:8]] == [c.value for c in CheckName]
    assert all(ln[16] != " " for ln in rows[:8])  # summary starts exactly at column 16


def test_server_header_variants() -> None:
    lines = render_human("all_pass").splitlines()
    assert lines[1] == "server: example-server 2.4.0 · speaks 2026-07-28 · 8 checks"
    assert lines[-1] == "All 8 checks passed in 1.3s."
    lines = render_human("unreachable").splitlines()
    assert lines[1].startswith(
        "server: unreachable — cannot connect to https://api.example.com/mcp: ConnectError"
    )
    assert lines[3].startswith("E handshake     Error: cannot connect")
    assert lines[4].startswith("    error: ServerUnreachable: ")
    assert lines[-1].startswith("Run incomplete: 8 of 8 checks could not run (cannot connect to")


def test_ascii_fallback_glyphs() -> None:
    text = render_human("mixed", ascii_only=True)
    assert "✓" not in text
    assert "✗" not in text
    assert "·" not in text
    assert "+ handshake     " in text
    assert "x schemas       " in text
    assert f"mcpcheck {__version__} | MCP 2026-07-28 | https://api.example.com/mcp" in text


def test_final_line_templates() -> None:
    assert output.final_line(fake_report("all_pass")) == "All 8 checks passed in 1.3s."
    assert output.final_line(fake_report("warn_only")) == "All 8 checks passed, 1 warning in 1.3s."
    assert (
        output.final_line(fake_report("warn_only", strict=True))
        == "1 of 8 checks failed, 1 promoted by --strict. See --json for details."
    )
    assert (
        output.final_line(fake_report("mixed"))
        == "2 of 8 checks failed, 1 warning, 1 inconclusive. See --json for details."
    )
    assert output.final_line(fake_report("unreachable")).startswith(
        "Run incomplete: 8 of 8 checks could not run ("
    )
    report = fake_report("all_pass")
    for c in report.checks:
        c.status = Status.INCONCLUSIVE
    report.summary.passed, report.summary.inconclusive = 0, 8
    assert output.final_line(report) == "No conclusive checks: 8 inconclusive in 1.3s."


def test_sanitize_neutralizes_control_chars_and_newlines() -> None:
    assert output.sanitize("a\x1b[31mred\x1b[0m\nb\tc") == "a?[31mred?[0m b c"
    # the replacement is ASCII so a non-UTF-8 console can print the sanitized text
    assert output.sanitize("a\x1bb").isascii()
    assert output.sanitize("x" * 600).endswith("…")
    assert len(output.sanitize("x" * 600)) == 500
    assert "" not in output.sanitize("ab")


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("rtl_\u202eevil", "rtl_?evil"),  # bidi override (Trojan-Source style)
        ("zw\u200bj", "zw?j"),  # zero-width space
        ("a\u200fb\u2066c\u2069d", "a?b?c?d"),  # RLM, isolates
        ("line\u2028break\u2029para", "line?break?para"),
        ("\ufeffbom", "?bom"),
        ("plain ünïcödé 工具 ✓", "plain ünïcödé 工具 ✓"),  # visible non-ASCII is kept
    ],
)
def test_sanitize_neutralizes_bidi_and_zero_width_characters(raw: str, clean: str) -> None:
    assert output.sanitize(raw) == clean


def test_report_to_json_ascii_mode_is_equivalent() -> None:
    report = fake_report()
    report.server_info = {"name": "wéather ✓ →", "version": "1"}
    text = output.report_to_json(report, ensure_ascii=True)
    assert text.isascii()
    assert json.loads(text) == json.loads(output.report_to_json(report))


def test_write_json_escapes_for_non_utf8_streams() -> None:
    report = fake_report()
    report.server_info = {"name": "wéather →", "version": "1"}
    buf = io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict")
    output.write_json(report, stream=buf)
    buf.seek(0)
    data = json.loads(buf.read())
    assert data["server_info"]["name"] == "wéather →"
    assert output.is_utf8_stream(io.TextIOWrapper(io.BytesIO(), encoding="utf-8"))
    assert output.is_utf8_stream(io.TextIOWrapper(io.BytesIO(), encoding="UTF_8"))
    assert not output.is_utf8_stream(buf)


def test_renderer_never_interprets_markup() -> None:
    report = fake_report("mixed")
    report.checks[0].summary = "[bold red]evil[/] and \x1b[2Jcleared"
    buf = io.StringIO()
    output.HumanRenderer(console(buf)).render(report)
    text = buf.getvalue()
    assert "[bold red]evil[/]" in text
    assert "\x1b" not in text


def test_transcript_renderer_levels() -> None:
    ex = Exchange(
        seq=1,
        connection="handshake",
        check="handshake",
        purpose="primary",
        verb="POST",
        url="https://api.example.com/mcp",
        rpc_method="tools/list",
        request_headers=[
            ("accept", "application/json, text/event-stream"),
            ("authorization", "Bearer ****"),
        ],
        request_body='{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
        status=200,
        response_headers=[("content-type", "application/json"), ("content-length", "612")],
        response_body='{"jsonrpc":"2.0","id":1,"result":{}}',
        elapsed_ms=48,
        tags=["[Origin: https://a.example]"],
    )
    err = Exchange(
        seq=2,
        connection="main",
        check="session",
        purpose="",
        verb="POST",
        url="https://api.example.com/mcp",
        rpc_method="tools/list",
        request_headers=[],
        request_body="{}",
        status=0,
        response_headers=[],
        response_body="",
        elapsed_ms=10001,
        error="request exceeded the 10s total budget",
    )
    buf = io.StringIO()
    output.TranscriptRenderer(console(buf), verbose=1).render([ex, err])
    v1 = buf.getvalue().splitlines()
    assert v1 == [
        "# check: handshake  conn=handshake",
        "> 1 POST https://api.example.com/mcp  tools/list [Origin: https://a.example]",
        "< 1 200 application/json 48ms",
        "# check: session  conn=main",
        "> 2 POST https://api.example.com/mcp  tools/list",
        "< 2 ERROR request exceeded the 10s total budget",
    ]
    buf = io.StringIO()
    output.TranscriptRenderer(console(buf), verbose=2).render([ex])
    v2 = buf.getvalue()
    assert ">   authorization: Bearer ****" in v2
    assert '>   {"jsonrpc":"2.0","id":1,"method":"tools/list"}' in v2
    assert "<   content-length: 612" in v2
    assert '<   {"jsonrpc":"2.0","id":1,"result":{}}' in v2


def test_write_json_atomic_file_and_stream(tmp_path: Path) -> None:
    report = fake_report("all_pass")
    target = tmp_path / "sub" / "report.json"
    stream = io.StringIO()
    output.write_json(report, path=str(target), stream=stream)
    assert json.loads(target.read_text(encoding="utf-8")) == json.loads(stream.getvalue())
    assert not [p for p in (tmp_path / "sub").iterdir() if p.name.startswith(".mcpcheck-")]


def test_glyph_tables_match_plan() -> None:
    assert output.GLYPHS_UNICODE == {
        Status.PASS: "✓",
        Status.FAIL: "✗",
        Status.WARN: "!",
        Status.INCONCLUSIVE: "?",
        Status.ERROR: "E",
    }
    assert output.GLYPHS_ASCII == {
        Status.PASS: "+",
        Status.FAIL: "x",
        Status.WARN: "!",
        Status.INCONCLUSIVE: "?",
        Status.ERROR: "E",
    }
    for forbidden in ("⚠", "▲", "×", "–"):  # noqa: RUF001 — the plan forbids exactly these glyphs
        assert forbidden not in output.GLYPHS_UNICODE.values()


def test_make_console_respects_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert output.make_console(no_color=True, file=io.StringIO()).color_system is None
    monkeypatch.setenv("NO_COLOR", "1")
    assert output.make_console(file=io.StringIO()).color_system is None
    assert isinstance(httpx2.Headers(), httpx2.Headers)  # keep httpx2 import meaningful for mypy
