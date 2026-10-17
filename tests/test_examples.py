"""The ``examples/`` demo and the ``Makefile`` are kept honest here (PLAN.md section 12).

The demo is the first thing a visitor runs, so CI has to prove it still works:

* ``examples/demo_server.py`` boots and passes all eight checks with no ``--break``;
* every ``--break`` feature changes exactly one check, to the status and the finding code
  this file tabulates (``BREAK_EXPECTATIONS``) — the table ``examples/README.md`` shows;
* ``--list-breaks`` lists exactly the implemented features, and ``examples/README.md``
  documents exactly those;
* ``examples/demo.sh`` exists, is committed executable and LF, parses, only breaks
  features that exist, explains each of them, and hands over commands that really run;
* every target ``make help`` advertises really exists;
* the README's copies of this output — its ``make help`` block, its ``--list-breaks``
  block, its "Watch a real failure first" report and every flag it passes to
  ``demo_server.py`` — still match the real thing.

Servers run in-process on ``127.0.0.1:<free port>`` via ``tests.conftest.ThreadServer``;
the subprocess tests call the demo server's own CLI with ``sys.executable``. The README's
hero block has its own regeneration test in ``tests/test_readme_hero.py``.
"""

from __future__ import annotations

import importlib.util
import io
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import pytest
from rich.console import Console

from mcpcheck import output
from mcpcheck.checks import NAMES, select
from mcpcheck.checks.codes import lookup
from mcpcheck.client import ClientConfig
from mcpcheck.models import Level, Status
from mcpcheck.runner import run_checks
from tests.conftest import ThreadServer

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from types import ModuleType

    from mcpcheck.models import Report

REPO = Path(__file__).resolve().parent.parent
README = REPO / "README.md"
EXAMPLES = REPO / "examples"
DEMO_SERVER = EXAMPLES / "demo_server.py"
DEMO_SH = EXAMPLES / "demo.sh"
EXAMPLES_README = EXAMPLES / "README.md"
MAKEFILE = REPO / "Makefile"
CLI_TIMEOUT_S = 30


def _load_demo_server() -> ModuleType:
    """Import ``examples/demo_server.py`` by path (``examples`` is not a package)."""
    spec = importlib.util.spec_from_file_location("mcpcheck_examples_demo_server", DEMO_SERVER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


demo_server = _load_demo_server()


class Expected(NamedTuple):
    """What one ``--break`` feature must do to one check."""

    check: str
    status: Status
    code: str


BREAK_EXPECTATIONS: dict[str, Expected] = {
    "cache": Expected("cache", Status.FAIL, "cache.ttl_missing"),
    "origin": Expected("origin", Status.FAIL, "origin.evil_accepted"),
    "output-schema": Expected("schemas", Status.WARN, "schemas.output_schema_missing"),
    "errors": Expected("errors", Status.WARN, "errors.non_actionable_messages"),
    "discover": Expected("discover", Status.WARN, "discover.server_info_missing"),
    "session": Expected("session", Status.WARN, "session.session_id_minted"),
    "initialize": Expected("handshake", Status.FAIL, "handshake.initialize_required"),
}
"""One row per ``--break`` feature: the check it moves, that check's status, its code."""

GATING_BREAK = "initialize"
"""The only break that stops the other checks from running (the handshake era gate)."""


@pytest.fixture
def demo_url() -> Iterator[Callable[[str], str]]:
    """``demo_url(breaks) -> url``; one demo server per call, all stopped at test end."""
    servers: list[ThreadServer] = []

    def factory(breaks: str = "") -> str:
        app = demo_server.build_app(demo_server.parse_breaks(breaks))
        server = ThreadServer(app, lifespan="on").start()
        servers.append(server)
        return server.url

    try:
        yield factory
    finally:
        for server in servers:
            server.stop()


async def run_all(url: str) -> Report:
    """Run all eight checks against ``url`` with default options."""
    return await run_checks(ClientConfig(url=url), select())


def status_of(report: Report, name: str) -> Status:
    """Status of check ``name`` in ``report``."""
    return next(r.status for r in report.checks if r.name.value == name)


def codes_of(report: Report, name: str) -> set[str]:
    """Failure and warning codes of check ``name``."""
    result = next(r for r in report.checks if r.name.value == name)
    return {f.code for f in (*result.failures, *result.warnings)}


# --------------------------------------------------------------- the demo server runs


@pytest.mark.integration
@pytest.mark.anyio
async def test_demo_server_passes_every_check(demo_url: Callable[[str], str]) -> None:
    """With no ``--break`` the demo server is fully conformant: eight passes, no findings."""
    report = await run_all(demo_url(""))
    assert [r.status for r in report.checks] == [Status.PASS] * len(NAMES)
    assert report.summary.passed == report.summary.total == len(NAMES)
    assert report.summary.exit_code == 0
    assert report.server_info == {
        "name": demo_server.SERVER_NAME,
        "version": demo_server.SERVER_VERSION,
    }


@pytest.mark.integration
@pytest.mark.anyio
@pytest.mark.parametrize("feature", list(BREAK_EXPECTATIONS))
async def test_break_moves_exactly_one_check(feature: str, demo_url: Callable[[str], str]) -> None:
    """Each ``--break`` feature trips its own check, with the tabulated code and status."""
    expected = BREAK_EXPECTATIONS[feature]
    report = await run_all(demo_url(feature))

    assert status_of(report, expected.check) is expected.status
    assert expected.code in codes_of(report, expected.check)
    # The registry's severity is what makes a check fail rather than warn.
    level = lookup(expected.code).level
    assert (level is Level.MUST) == (expected.status is Status.FAIL)

    others = [r for r in report.checks if r.name.value != expected.check]
    if feature == GATING_BREAK:
        # A server that demands `initialize` cannot be judged any further.
        assert all(r.status is Status.INCONCLUSIVE for r in others)
    else:
        assert [r.name.value for r in others if r.status is not Status.PASS] == []


# --------------------------------------------------------------- the demo server CLI


def demo_cli(*args: str) -> subprocess.CompletedProcess[str]:
    """Run ``python examples/demo_server.py <args>`` and capture its output."""
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, str(DEMO_SERVER), *args],
        capture_output=True,
        text=True,
        timeout=CLI_TIMEOUT_S,
        check=False,
    )


def listed_breaks() -> list[str]:
    """The feature names ``--list-breaks`` prints, in order."""
    done = demo_cli("--list-breaks")
    assert done.returncode == 0, done.stderr
    return [line.split()[0] for line in done.stdout.splitlines() if line.strip()]


def test_list_breaks_matches_the_implemented_features() -> None:
    """``--list-breaks`` advertises every implemented feature, plus ``all``, and nothing else."""
    assert listed_breaks() == [*demo_server.BREAKS, demo_server.ALL_BREAKS]
    assert set(demo_server.BREAKS) == set(BREAK_EXPECTATIONS)


def test_unknown_break_feature_is_a_clear_error() -> None:
    """An unknown ``--break`` feature exits 2 and names the valid choices."""
    done = demo_cli("--break", "does-not-exist")
    assert done.returncode == 2
    assert "unknown --break feature 'does-not-exist'" in done.stderr
    assert "cache" in done.stderr


# --------------------------------------------------------------- the docs and scripts


def test_examples_readme_documents_every_break() -> None:
    """Every feature is a row of the ``examples/README.md`` table, with its finding code."""
    text = EXAMPLES_README.read_text(encoding="utf-8")
    documented = set(re.findall(r"^\| `([a-z][a-z-]*)` \|", text, re.MULTILINE))
    assert documented == set(demo_server.BREAKS) | {demo_server.ALL_BREAKS}
    for feature, expected in BREAK_EXPECTATIONS.items():
        row = next(line for line in text.splitlines() if line.startswith(f"| `{feature}` |"))
        assert expected.code in row, f"{feature} row does not name {expected.code}"
        assert expected.check in row, f"{feature} row does not name the {expected.check} check"


@pytest.mark.skipif(os.name == "nt", reason="Windows filesystems have no POSIX mode bits")
def test_demo_sh_is_executable_on_posix() -> None:
    """``examples/demo.sh`` carries the execute bit in the working tree."""
    assert DEMO_SH.is_file()
    assert DEMO_SH.stat().st_mode & 0o111, "examples/demo.sh is not executable (chmod +x)"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_demo_sh_parses_as_bash() -> None:
    """``examples/demo.sh`` parses; a CRLF checkout would fail here (SC1017)."""
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["bash", "-n", str(DEMO_SH)],  # noqa: S607 - bash is resolved from PATH on purpose
        check=False,
        capture_output=True,
        text=True,
        timeout=CLI_TIMEOUT_S,
    )
    # Show what bash objected to: on Windows this is the difference between a CRLF
    # checkout (SC1017) and a real syntax error, and CI job logs are admin-only.
    crlf = b"\r\n" in DEMO_SH.read_bytes()
    assert proc.returncode == 0, f"bash -n failed (CRLF in working tree: {crlf})\n{proc.stderr}"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_demo_sh_is_committed_executable() -> None:
    """git records mode ``100755``, which is what a clone on any OS gets.

    The working-tree mode bits above do not exist on Windows (CPython only sets
    ``0o111`` for ``.exe``/``.bat``/``.cmd``/``.com``), so the committed mode is the
    portable statement of "this script is runnable".
    """
    done = subprocess.run(
        ["git", "ls-files", "-s", "--", "examples/demo.sh"],  # noqa: S607 - from PATH on purpose
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=CLI_TIMEOUT_S,
        check=False,
    )
    if done.returncode != 0 or not done.stdout.strip():
        pytest.skip("examples/demo.sh is not tracked by git in this checkout")
    assert done.stdout.split()[0] == "100755", done.stdout.strip()


def test_shell_scripts_are_checked_out_with_lf() -> None:
    """``.gitattributes`` pins LF for shell scripts and the Makefile, and the file is LF.

    Without it, a Windows clone with Git's default ``core.autocrlf=true`` gets
    ``examples/demo.sh`` with CRLF endings, which breaks ``bash -n``, shellcheck
    (SC1017) and the shebang itself.
    """
    attributes = (REPO / ".gitattributes").read_text(encoding="utf-8")
    assert "*.sh text eol=lf" in attributes
    assert "Makefile text eol=lf" in attributes
    assert b"\r\n" not in DEMO_SH.read_bytes(), "examples/demo.sh has CRLF line endings"
    assert b"\r\n" not in MAKEFILE.read_bytes(), "Makefile has CRLF line endings"


def test_demo_sh_next_block_hands_over_runnable_commands() -> None:
    """The closing "Next" block must name the commands that work in *this* environment.

    A bare ``python examples/demo_server.py`` or ``mcpcheck <url>`` does not work after a
    plain ``make demo`` (nothing is installed and ``uv`` owns the environment), so the
    heredoc interpolates the command arrays the script itself resolved.
    """
    text = DEMO_SH.read_text(encoding="utf-8")
    assert "cat <<NEXT" in text, "the Next heredoc is quoted; it must interpolate"
    assert "cat <<'NEXT'" not in text
    block = text.split("cat <<NEXT\n", 1)[1].split("\nNEXT", 1)[0]
    assert "${python[*]} examples/demo_server.py --list-breaks" in block
    assert "${mcpcheck[*]} http://127.0.0.1:<your-port>/mcp" in block
    bare = [
        line
        for line in block.splitlines()
        if re.search(r"(?<![\w\]}])(python|mcpcheck) (examples/|http)", line)
    ]
    assert bare == [], f"the Next block suggests commands that are not on PATH: {bare}"


def test_demo_sh_explains_every_feature_it_breaks() -> None:
    """Each feature in ``BREAKS=`` has an explanation arm, and every arm is a real break."""
    text = DEMO_SH.read_text(encoding="utf-8")
    value = re.search(r'^BREAKS="([^"]*)"', text, re.MULTILINE)
    assert value is not None
    features = set(value.group(1).split(","))
    arms = set(re.findall(r"^\s+([a-z][a-z-]*)\)\s+effect=", text, re.MULTILINE))
    assert features <= arms, f"no explanation for {sorted(features - arms)}"
    assert arms <= set(demo_server.BREAKS), (
        f"not --break features: {sorted(arms - set(demo_server.BREAKS))}"
    )


def test_demo_sh_only_breaks_features_that_exist() -> None:
    """The ``BREAKS=`` line in ``demo.sh`` names implemented features."""
    text = DEMO_SH.read_text(encoding="utf-8")
    value = re.search(r'^BREAKS="([^"]*)"', text, re.MULTILINE)
    assert value is not None, "demo.sh no longer defines BREAKS=..."
    features = value.group(1).split(",")
    assert features
    assert set(features) <= set(demo_server.BREAKS)


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed")
def test_demo_sh_passes_shellcheck() -> None:
    """``examples/demo.sh`` is clean under shellcheck when it is available."""
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["shellcheck", "--severity=style", str(DEMO_SH)],  # noqa: S607 - from PATH on purpose
        capture_output=True,
        text=True,
        timeout=CLI_TIMEOUT_S,
        check=False,
    )
    assert done.returncode == 0, done.stdout or done.stderr


def documented_make_targets() -> list[str]:
    """Target names carrying a ``##`` help comment in the Makefile."""
    text = MAKEFILE.read_text(encoding="utf-8")
    return re.findall(r"^([a-z][a-z-]*):[^=\n]*##", text, re.MULTILINE)


def test_makefile_help_lists_only_real_targets() -> None:
    """Every documented target is declared ``.PHONY`` and has a recipe."""
    text = MAKEFILE.read_text(encoding="utf-8")
    targets = documented_make_targets()
    assert "help" in targets
    assert "demo" in targets
    phony = next(line for line in text.splitlines() if line.startswith(".PHONY:"))
    assert sorted(phony.split()[1:]) == sorted(targets)
    for target in targets:
        body = text.split(f"\n{target}:", 1)[1].splitlines()[1]
        assert body.startswith("\t"), f"make target {target} has no recipe"


# ------------------------------------------------- the README's copies of this output

SHELL_LANGS = frozenset({"shell", "sh", "bash", "console"})
CHAIN_TOKENS = frozenset({"|", "||", "&&", ";", "&", "|&", ">", ">>"})
DOC_FILES = (README, EXAMPLES_README)


def fenced_shell_lines(text: str) -> list[str]:
    """Every command line inside a shell/console fence (``$ `` prompts stripped)."""
    lines: list[str] = []
    lang: str | None = None
    for raw in text.splitlines():
        fence = re.match(r"^(?:```|~~~)\s*([A-Za-z0-9_+-]*)\s*$", raw.rstrip())
        if lang is None:
            if fence is not None:
                lang = fence.group(1)
            continue
        if fence is not None:
            lang = None
            continue
        if lang not in SHELL_LANGS:
            continue
        line = raw.strip()
        if lang == "console":
            if not line.startswith("$ "):
                continue
            line = line[2:]
        if line:
            lines.append(line)
    return lines


def console_output_after(text: str, command: str) -> str:
    """The output lines printed under ``$ <command>`` in a ``console`` fence."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"$ {command}")
    out: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith(("$ ", "```", "~~~")):
            break
        out.append(line)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out) + "\n"


def demo_server_option_tokens(text: str) -> list[str]:
    """Every option token handed to ``demo_server.py`` in a shell/console fence."""
    options: list[str] = []
    for line in fenced_shell_lines(text):
        body = re.sub(r"\s+#.*$", "", line).strip()
        try:
            tokens = shlex.split(body, comments=False)
        except ValueError:  # pragma: no cover - unbalanced quotes would be a typo
            continue
        start = next(
            (i for i, tok in enumerate(tokens) if tok.endswith("examples/demo_server.py")), None
        )
        if start is None:
            continue
        for tok in tokens[start + 1 :]:
            if tok in CHAIN_TOKENS or re.match(r"^\d*>", tok):
                break
            if tok.startswith("-") and tok != "-":
                options.append(tok)
    return options


def demo_server_known_options() -> set[str]:
    """The option strings ``demo_server.py --help`` advertises."""
    done = demo_cli("--help")
    assert done.returncode == 0, done.stderr
    return set(re.findall(r"--?[A-Za-z][\w-]*", done.stdout))


@pytest.mark.parametrize("path", DOC_FILES, ids=lambda p: p.name)
def test_documented_demo_server_flags_exist(path: Path) -> None:
    """Every flag the docs pass to ``demo_server.py`` is one its CLI really accepts."""
    known = demo_server_known_options()
    used = demo_server_option_tokens(path.read_text(encoding="utf-8"))
    assert used, f"{path.name} shows no demo_server.py invocation"
    unknown = sorted({tok for tok in used if tok.split("=", 1)[0] not in known})
    assert unknown == [], f"{path.name}: not demo_server.py options: {unknown}"


def test_readme_list_breaks_block_matches_the_cli() -> None:
    """The README's ``--list-breaks`` console block is the CLI's real output."""
    done = demo_cli("--list-breaks")
    assert done.returncode == 0, done.stderr
    printed = console_output_after(
        README.read_text(encoding="utf-8"), "uv run python examples/demo_server.py --list-breaks"
    )
    assert printed == done.stdout, (
        "README.md's --list-breaks block is stale: paste "
        "`uv run python examples/demo_server.py --list-breaks` again"
    )


@pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")
def test_readme_make_help_block_matches_make_help() -> None:
    """The README's ``make help`` console block is what ``make help`` really prints."""
    env = {**os.environ, "MAKEFLAGS": "", "MFLAGS": ""}
    done = subprocess.run(
        ["make", "help"],  # noqa: S607 - make is resolved from PATH on purpose
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=CLI_TIMEOUT_S,
        check=True,
        env=env,
    )
    printed = console_output_after(README.read_text(encoding="utf-8"), "make help")
    assert printed == done.stdout, "README.md's `make help` block no longer matches the Makefile"


# ------------------------------------------------- the README's "real failure" report


def render_report(report: Report, *, width: int = 100) -> str:
    """Render ``report`` exactly as the CLI does at ``COLUMNS=width`` with ``NO_COLOR=1``."""
    buf = io.StringIO()
    console = Console(
        file=buf,
        color_system=None,
        highlight=False,
        markup=False,
        soft_wrap=True,
        width=width,
    )
    output.HumanRenderer(console).render(report)
    return buf.getvalue()


@pytest.mark.integration
@pytest.mark.anyio
async def test_readme_broken_demo_report_is_a_real_run(demo_url: Callable[[str], str]) -> None:
    """The README's "Watch a real failure first" report is what that recipe really prints.

    The recipe starts the demo server with ``--break cache,origin,output-schema`` on port
    8000 and runs ``mcpcheck`` against it; only the URL differs here, because the test
    server takes a free port.
    """
    recipe_url = "http://127.0.0.1:8000/mcp"
    url = demo_url("cache,origin,output-schema")
    report = await run_all(url)
    rendered = render_report(report).replace(url, recipe_url)

    text = README.read_text(encoding="utf-8")
    section = text.split("### Watch a real failure first", 1)[1]
    assert f"uv run mcpcheck {recipe_url}" in section.split("```\n", 1)[0], (
        "the recipe no longer runs mcpcheck against " + recipe_url
    )
    printed = section.split("```console\n", 1)[1].split("```", 1)[0]
    assert printed == rendered, (
        'README.md\'s "Watch a real failure first" report is stale: re-capture it with\n'
        "  uv run python examples/demo_server.py --port 8000 --break cache,origin,output-schema &\n"
        f"  NO_COLOR=1 COLUMNS=100 uv run mcpcheck {recipe_url}"
    )
