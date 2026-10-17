"""CLI tests (PLAN.md §8.1-§8.4, §10.8): exit-code matrix via a fake runner, flags, usage errors, no leaks."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import jsonschema  # type: ignore[import-untyped,unused-ignore]
import pytest
from click.testing import CliRunner, Result

import mcpcheck
from mcpcheck import __main__ as cli
from mcpcheck import runner as runner_mod
from mcpcheck.checks import NAMES
from tests.helpers.reports import fake_report

if TYPE_CHECKING:
    from mcpcheck.client import ClientConfig
    from mcpcheck.models import Report

ENV = {"COLUMNS": "100", "NO_COLOR": "1", "TERM": "dumb", "TZ": "UTC", "MCPCHECK_TOKEN": None}
URL = "http://127.0.0.1:9/mcp"


def invoke(*args: str, env: dict[str, str | None] | None = None, **kw: Any) -> Result:
    merged: dict[str, str | None] = dict(ENV)
    merged.update(env or {})
    return CliRunner().invoke(
        cli.main,
        list(args),
        env=merged,
        catch_exceptions=False,
        terminal_width=80,
        prog_name="mcpcheck",
        **kw,
    )


@pytest.fixture
def fake_runner(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace ``runner.run_checks`` with a fake that records its inputs and returns a chosen report."""
    state: dict[str, Any] = {"variant": "all_pass", "calls": []}

    async def run_checks(
        cfg: ClientConfig, checks: Any, *, progress: Any = None, transcript: Any = None
    ) -> Report:
        state["calls"].append({"cfg": cfg, "names": [c.name for c in checks]})
        if progress is not None:
            progress("running fake…")
        report = fake_report(state["variant"], strict=cfg.strict, token=bool(cfg.token))
        report.server = cfg.url
        report.request.extra_headers = [name for name, _ in cfg.extra_headers]
        return report

    monkeypatch.setattr(runner_mod, "run_checks", run_checks)
    return state


# --------------------------------------------------------------------------- exit-code matrix


@pytest.mark.parametrize(
    ("variant", "strict", "exit_code"),
    [
        ("all_pass", False, 0),
        ("all_pass", True, 0),
        ("warn_only", False, 0),
        ("warn_only", True, 1),
        ("mixed", False, 1),
        ("mixed", True, 1),
        ("unreachable", False, 2),
        ("unreachable", True, 2),
    ],
)
def test_exit_code_matrix(
    fake_runner: dict[str, Any], variant: str, strict: bool, exit_code: int
) -> None:
    fake_runner["variant"] = variant
    args = [URL, *(["--strict"] if strict else [])]
    result = invoke(*args)
    assert result.exit_code == exit_code, result.output
    assert result.stderr == ""


def test_all_pass_human_output(fake_runner: dict[str, Any]) -> None:
    result = invoke(URL)
    lines = result.stdout.splitlines()
    assert lines[0] == f"mcpcheck {mcpcheck.__version__} · MCP 2026-07-28 · {URL}"
    assert sum(ln.startswith("✓ ") for ln in lines) == 8
    assert lines[-1] == "All 8 checks passed in 1.3s."


def test_failures_human_output(fake_runner: dict[str, Any]) -> None:
    fake_runner["variant"] = "mixed"
    result = invoke(URL)
    assert result.exit_code == 1
    assert "✗ schemas       " in result.stdout
    assert (
        result.stdout.splitlines()[-1]
        == "2 of 8 checks failed, 1 warning, 1 inconclusive. See --json for details."
    )


# --------------------------------------------------------------------------- --json / --output


def test_json_validates_against_shipped_schema(fake_runner: dict[str, Any]) -> None:
    fake_runner["variant"] = "mixed"
    result = invoke(URL, "--json")
    assert result.exit_code == 1
    data = json.loads(result.stdout)  # nothing else on stdout
    schema = json.loads(cli.shipped_schema_text())
    jsonschema.validate(data, schema, cls=jsonschema.Draft202012Validator)
    assert data["summary"]["total"] == len(data["checks"]) == 8
    assert data["timestamp"].endswith("Z")
    assert data["server"] == URL
    assert result.stderr == ""


def test_output_file_plus_human(fake_runner: dict[str, Any], tmp_path: Path) -> None:
    target = tmp_path / "r.json"
    result = invoke(URL, "-o", str(target))
    assert result.exit_code == 0
    assert "✓ handshake" in result.stdout
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1


def test_output_dash_implies_json(fake_runner: dict[str, Any]) -> None:
    result = invoke(URL, "--output", "-")
    assert result.exit_code == 0
    assert json.loads(result.stdout)["schema_version"] == 1


def test_json_written_even_on_exit_2(fake_runner: dict[str, Any], tmp_path: Path) -> None:
    fake_runner["variant"] = "unreachable"
    target = tmp_path / "r.json"
    result = invoke(URL, "--json", "-o", str(target))
    assert result.exit_code == 2
    assert json.loads(target.read_text())["summary"]["exit_code"] == 2
    assert json.loads(result.stdout)["summary"]["errored"] == 8


# --------------------------------------------------------------------------- selection and config plumbing


def test_check_subset_canonical_order(fake_runner: dict[str, Any]) -> None:
    result = invoke(URL, "-c", "origin,cache", "--check", "HANDSHAKE")
    assert result.exit_code == 0
    assert fake_runner["calls"][0]["names"] == ["handshake", "cache", "origin"]


def test_skip(fake_runner: dict[str, Any]) -> None:
    invoke(URL, "--skip", "errors", "--skip", "origin,protocol")
    assert fake_runner["calls"][0]["names"] == [
        "handshake",
        "session",
        "discover",
        "schemas",
        "cache",
    ]


def test_config_plumbing(fake_runner: dict[str, Any]) -> None:
    result = invoke(
        URL,
        "--token",
        "sekrit",
        "-H",
        "X-A: 1",
        "-H",
        "X-B:2",
        "--origin",
        "https://app.example.com",
        "--timeout",
        "2.5",
        "--probe",
        "all",
        "--max-tools",
        "3",
        "--legacy-probes",
        "--strict",
        "--insecure",
    )
    assert result.exit_code == 0
    cfg: ClientConfig = fake_runner["calls"][0]["cfg"]
    assert cfg.token == "sekrit"
    assert cfg.extra_headers == (("X-A", "1"), ("X-B", "2"))
    assert cfg.origin == "https://app.example.com"
    assert cfg.timeout == 2.5
    assert cfg.probe == "all"
    assert cfg.max_tools == 3
    assert cfg.legacy_probes is True
    assert cfg.strict is True
    assert cfg.verify_tls is False
    assert "warning: --insecure disables TLS certificate verification" in result.stderr


def test_token_from_env(fake_runner: dict[str, Any]) -> None:
    invoke(URL, env={"MCPCHECK_TOKEN": "from-env"})
    assert fake_runner["calls"][0]["cfg"].token == "from-env"


def test_token_never_leaks(fake_runner: dict[str, Any], tmp_path: Path) -> None:
    fake_runner["variant"] = "mixed"
    target = tmp_path / "r.json"
    result = invoke(
        URL, "--token", "sekrit-7f3a", "-H", "X-Api-Key: k3y-7f3a", "-vv", "-o", str(target)
    )
    for text in (result.stdout, result.stderr, target.read_text()):
        assert "sekrit-7f3a" not in text
        assert "k3y-7f3a" not in text
    data = json.loads(target.read_text())
    assert data["request"] == {
        "origin": None,
        "auth": "bearer",
        "extra_headers": ["X-Api-Key"],
        "tls_verify": True,
    }


# --------------------------------------------------------------------------- usage errors (§8.2)


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ([], "Missing argument 'SERVER_URL'."),
        (
            ["localhost:8000/mcp"],
            "Invalid value for 'SERVER_URL': must be an absolute http:// or https:// URL, e.g. https://api.example.com/mcp",
        ),
        (
            ["ftp://h/mcp"],
            "Invalid value for 'SERVER_URL': must be an absolute http:// or https:// URL, e.g. https://api.example.com/mcp",
        ),
        (
            ["http://"],
            "Invalid value for 'SERVER_URL': must be an absolute http:// or https:// URL, e.g. https://api.example.com/mcp",
        ),
        (
            ["http://u:p@h/mcp"],
            "Invalid value for 'SERVER_URL': must not contain credentials; use --token or --header instead",
        ),
        (["http://h/mcp#x"], "Invalid value for 'SERVER_URL': must not contain a #fragment"),
        (
            ["-c", "origin,sesion", URL],
            "Invalid value for '-c' / '--check': 'sesion' is not one of 'handshake', 'session', 'discover', 'protocol', 'schemas', 'errors', 'cache', 'origin'.",
        ),
        (["-c", "origin", "--skip", "origin", URL], "--check and --skip both name 'origin'."),
        (["--skip", ",".join(NAMES), URL], "No checks left to run after applying --skip."),
        (
            ["--origin", "https://a.example/path", URL],
            "Invalid value for '--origin': must be a serialized origin such as https://app.example.com (no path)",
        ),
        (
            ["-H", "X-Api-Key", URL],
            "Invalid value for '-H' / '--header': 'X-Api-Key' is not in KEY:VALUE form",
        ),
        (["--timeout", "0", URL], "Invalid value for '--timeout': 0.0 is not in the range x>0."),
        (["--max-tools", "0", URL], "Invalid value for '--max-tools': 0 is not in the range x>=1."),
        (["--probe", "maybe", URL], "Invalid value for '--probe'"),
    ],
)
def test_usage_errors_exit_2_before_io(
    fake_runner: dict[str, Any], args: list[str], message: str
) -> None:
    result = invoke(*args)
    assert result.exit_code == 2
    assert f"Error: {message}" in result.stderr or message in result.stderr
    assert fake_runner["calls"] == []
    assert result.stdout == ""


# --------------------------------------------------------------------------- eager options


def test_version() -> None:
    result = invoke("--version")
    assert result.exit_code == 0
    assert result.stdout.strip() == f"mcpcheck {mcpcheck.__version__}"


def test_list_checks_without_url() -> None:
    result = invoke("--list-checks")
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert [ln.split()[0] for ln in lines] == list(NAMES)
    assert lines[0] == "handshake  Server does not require initialize"
    assert lines[-1] == "origin     Forged Origin header rejected with HTTP 403"


def test_json_schema_without_url() -> None:
    result = invoke("--json-schema")
    assert result.exit_code == 0
    schema = json.loads(result.stdout)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    jsonschema.Draft202012Validator.check_schema(schema)
    repo = Path(__file__).resolve().parent.parent / "schema" / "mcpcheck-report.v1.schema.json"
    assert result.stdout == repo.read_text(encoding="utf-8")


def test_help_lists_frozen_option_set() -> None:
    result = invoke("--help")
    assert result.exit_code == 0
    text = result.stdout
    assert text.startswith("Usage: mcpcheck [OPTIONS] SERVER_URL")
    for opt in (
        "--token TEXT",
        "-H, --header KEY:VALUE",
        "--origin TEXT",
        "--timeout SECONDS",
        "-c, --check NAME",
        "--skip NAME",
        "--probe [safe|all|none]",
        "--max-tools N",
        "--legacy-probes",
        "--strict",
        "--json",
        "-o, --output FILE",
        "--json-schema",
        "--list-checks",
        "-v, --verbose",
        "--no-color",
        "--insecure",
        "-V, --version",
        "-h, --help",
    ):
        assert opt in text, opt
    assert "[env var: MCPCHECK_TOKEN]" in text
    assert "[default: 10.0; x>0]" in text
    assert "mcpcheck is read-only" in text


# --------------------------------------------------------------------------- real runner (stub checks)


def test_unreachable_server_exits_2_quickly() -> None:
    import time

    t0 = time.perf_counter()
    result = invoke("http://127.0.0.1:1/mcp", "--timeout", "2")
    assert result.exit_code == 2
    assert time.perf_counter() - t0 < 5
    assert "E handshake" in result.stdout
    assert (
        "Run incomplete: 8 of 8 checks could not run (cannot connect to http://127.0.0.1:1/mcp"
        in result.stdout
    )


def test_verbose_transcript_on_stderr_only() -> None:
    result = invoke("http://127.0.0.1:1/mcp", "--timeout", "2", "-v")
    assert result.exit_code == 2
    assert "# check: handshake  conn=handshake" in result.stderr
    assert "> 1 POST http://127.0.0.1:1/mcp  tools/list" in result.stderr
    # Windows reports a dead port as a connect timeout rather than a refusal.
    assert any(f"< 1 ERROR {k}" in result.stderr for k in ("ConnectError", "ConnectTimeout"))
    assert "# check" not in result.stdout


def test_python_m_mcpcheck_smoke() -> None:
    env = {**os.environ, "NO_COLOR": "1"}
    proc = subprocess.run(
        [sys.executable, "-m", "mcpcheck", "--version"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == f"mcpcheck {mcpcheck.__version__}"


# --------------------------------------------------------------------------- -o validation, Ctrl-C


def test_output_parent_must_be_a_writable_directory(
    fake_runner: dict[str, Any], tmp_path: Path
) -> None:
    """A bad -o target is a usage error before any I/O, not a traceback after the run."""
    blocker = tmp_path / "file.txt"
    blocker.write_text("x", encoding="utf-8")
    result = invoke(URL, "-o", str(blocker / "report.json"))
    assert result.exit_code == 2
    assert "is not a directory" in result.output
    assert fake_runner["calls"] == []  # validation happened before the run
    result = invoke(URL, "-o", str(tmp_path))
    assert result.exit_code == 2
    assert fake_runner["calls"] == []
    # a not-yet-existing nested path under a writable directory is fine
    result = invoke(URL, "-o", str(tmp_path / "new" / "dir" / "report.json"))
    assert result.exit_code == 0, result.output
    assert (tmp_path / "new" / "dir" / "report.json").exists()


@pytest.mark.skipif(
    os.name == "nt" or os.geteuid() == 0, reason="needs POSIX permissions, non-root"
)
def test_output_into_read_only_directory_is_a_usage_error(
    fake_runner: dict[str, Any], tmp_path: Path
) -> None:
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o555)
    try:
        result = invoke(URL, "-o", str(ro / "report.json"))
    finally:
        ro.chmod(0o755)
    assert result.exit_code == 2
    assert "not writable" in result.output
    assert fake_runner["calls"] == []


def test_write_failure_after_the_run_exits_2_with_a_message(
    fake_runner: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcpcheck.output import write_json as real

    def boom(report: Any, *, path: str | None = None, stream: Any = None) -> None:
        if path is not None:  # the file write fails (disk full, permissions changed mid-run)
            raise PermissionError(13, "Permission denied")
        real(report, stream=stream)

    monkeypatch.setattr(cli, "write_json", boom)
    result = invoke(URL, "--json", "-o", str(tmp_path / "report.json"))
    assert result.exit_code == 2
    assert "cannot write report to" in result.output
    assert "Traceback" not in result.output


def test_keyboard_interrupt_anywhere_exits_130(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted() -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "require_sdk", interrupted)
    result = invoke(URL)
    assert result.exit_code == cli.EXIT_INTERRUPTED == 130
    assert "interrupted" in result.output
    assert "Aborted" not in result.output
