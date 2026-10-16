"""README / companion-doc consistency (PLAN.md §10.7, §12.3).

Everything here is pure text processing over the repository files; no network, no
``readme_renderer`` (that runs in ``scripts/check_readme.py`` from the lint job).
"""

from __future__ import annotations

import importlib.util
import re
import shlex
from pathlib import Path
from typing import TYPE_CHECKING

import click
import pytest

from mcpcheck import __version__, spec_urls
from mcpcheck.__main__ import main as cli
from mcpcheck.checks import NAMES, codes

if TYPE_CHECKING:
    from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
RELEASE_DOCS = (README, ROOT / "CHANGELOG.md", ROOT / "CONTRIBUTING.md")

MARKER_RE = re.compile(r"TO-BE-REPLACED|TO-FILL")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
ANCHOR_LINK_RE = re.compile(r"\]\(#([^)\s]+)\)")
CHECK_ROW_RE = re.compile(r"^\| `([a-z]+)` \|")
OPTION_ROW_RE = re.compile(r"^\| `([^`]+)` \|")
FINDING_CODE_RE = re.compile(r"\b((?:" + "|".join(NAMES) + r"|runner)\.[a-z0-9_]+)\b")
SPEC_URL_RE = re.compile(r"https://modelcontextprotocol\.io/specification/[^\s)\]\"'`>]+")


def _load_script(name: str) -> ModuleType:
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def _strip_code(text: str) -> str:
    """Blank fenced blocks and inline code (prose-only view of the document)."""
    out: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if fence is None and stripped.startswith(("```", "~~~")):
            fence = stripped[:3]
            out.append("")
            continue
        if fence is not None:
            if stripped.startswith(fence):
                fence = None
            out.append("")
            continue
        out.append(re.sub(r"`[^`]*`", "", line))
    return "\n".join(out)


def _slugify(heading: str) -> str:
    text = re.sub(r"`([^`]*)`", r"\1", heading)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = text.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _section(text: str, title: str) -> str:
    """The body of the ``## title`` section (up to the next ``## `` heading)."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"## {title}")
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        body.append(line)
    return "\n".join(body)


# --------------------------------------------------------------------------- options table


def _help_option_names() -> set[str]:
    ctx = cli.make_context("mcpcheck", [], resilient_parsing=True)
    names: set[str] = set()
    help_option = cli.get_help_option(ctx)
    for param in (*cli.params, *([help_option] if help_option is not None else [])):
        if isinstance(param, click.Option):
            names.update(param.opts)
            names.update(param.secondary_opts)
    return names


def _readme_option_names() -> set[str]:
    section = _section(_readme(), "Options")
    names: set[str] = set()
    for line in section.splitlines():
        m = OPTION_ROW_RE.match(line)
        if m is None or m.group(1) == "Option":
            continue
        for flag in re.findall(r"(?<![\w-])(--?[A-Za-z][\w-]*)", m.group(1)):
            names.add(flag)
    return names


def test_options_table_lists_exactly_the_help_options() -> None:
    assert _readme_option_names() == _help_option_names()


def test_options_table_is_generated_from_help() -> None:
    gen = _load_script("gen_options_table")
    text = _readme()
    assert gen.START in text
    assert gen.END in text
    assert gen.splice(text, gen.render_block()) == text, (
        "README options table is stale: run `uv run python scripts/gen_options_table.py README.md`"
    )


# --------------------------------------------------------------------------- checks table


def test_checks_table_rows_match_registry_order() -> None:
    section = _section(_readme(), "What it checks")
    rows = [m.group(1) for m in map(CHECK_ROW_RE.match, section.splitlines()) if m]
    assert rows == list(NAMES)


def test_checks_table_cites_each_checks_canonical_spec_page() -> None:
    from mcpcheck.checks import REGISTRY

    section = _section(_readme(), "What it checks")
    for cls in REGISTRY:
        row = next(line for line in section.splitlines() if line.startswith(f"| `{cls.name}` |"))
        page = cls.spec_url.split("#", 1)[0]
        assert page in row, f"{cls.name}: row does not link to {page}"


# --------------------------------------------------------------------------- markers and anchors


@pytest.mark.parametrize("path", RELEASE_DOCS, ids=lambda p: p.name)
def test_no_placeholder_markers(path: Path) -> None:
    offenders = [
        f"{path.name}:{no}"
        for no, line in enumerate(path.read_text("utf-8").splitlines(), 1)
        if MARKER_RE.search(line)
    ]
    assert offenders == []


@pytest.mark.parametrize("path", RELEASE_DOCS, ids=lambda p: p.name)
def test_every_internal_anchor_resolves(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    anchors = {_slugify(m.group(2)) for m in map(HEADING_RE.match, text.splitlines()) if m}
    missing = [a for a in ANCHOR_LINK_RE.findall(_strip_code(text)) if a not in anchors]
    assert missing == []


@pytest.mark.parametrize("path", RELEASE_DOCS, ids=lambda p: p.name)
def test_links_are_absolute(path: Path) -> None:
    prose = _strip_code(path.read_text(encoding="utf-8"))
    targets = [m.group(1) for m in re.finditer(r"!?\[[^\]]*\]\(\s*([^)\s]+)", prose)]
    relative = [
        t for t in targets if not t.startswith("#") and not re.match(r"^https?://|^mailto:", t)
    ]
    assert relative == []


# --------------------------------------------------------------------------- header banner


BANNER_PATH = "assets/banner.webp"
BANNER_URL = f"https://raw.githubusercontent.com/suresh-jbt/mcpcheck/main/{BANNER_PATH}"


def test_banner_is_the_first_thing_in_the_readme() -> None:
    """The header image leads the file, so GitHub and PyPI both show it first."""
    first = README.read_text(encoding="utf-8").lstrip().splitlines()[0]
    assert BANNER_URL in first, first


def test_banner_asset_is_committed_and_referenced_absolutely() -> None:
    """A relative image silently breaks on PyPI, which serves the README elsewhere."""
    asset = ROOT / BANNER_PATH
    assert asset.is_file(), f"{BANNER_PATH} is missing"
    assert asset.stat().st_size > 1024
    assert asset.read_bytes()[:4] == b"RIFF", "banner is not the expected WebP file"
    assert BANNER_PATH in README.read_text(encoding="utf-8")


def test_banner_has_alt_text() -> None:
    """Alt text carries the pitch when images are blocked or read aloud."""
    first = README.read_text(encoding="utf-8").lstrip().splitlines()[0]
    alt = re.match(r"\[!\[([^\]]+)\]", first)
    assert alt is not None, first
    assert len(alt.group(1)) > 20, alt.group(1)


# --------------------------------------------------------------------------- codes, URLs, version


def test_every_finding_code_mentioned_is_registered() -> None:
    mentioned = set(FINDING_CODE_RE.findall(_readme()))
    assert mentioned, "README should mention at least one finding code"
    unknown = sorted(code for code in mentioned if code not in codes.FINDING_CODES)
    assert unknown == []


def test_every_spec_url_is_a_verified_constant() -> None:
    known = set(spec_urls.all_urls().values())
    used = set(SPEC_URL_RE.findall(_readme()))
    assert used, "README should link to the specification"
    unknown = sorted(u for u in used if u not in known)
    assert unknown == [], f"not in spec_urls.py: {unknown}"


def test_readme_pins_the_current_version() -> None:
    text = _readme()
    assert f"uvx mcpcheck@{__version__}" in text
    assert f"mcpcheck {__version__} · MCP 2026-07-28 ·" in text


def test_hero_block_names_every_check() -> None:
    hero = _readme().split("```console", 1)[1].split("```", 1)[0]
    for name in NAMES:
        assert re.search(rf"^[✓✗!?E] {name}\s", hero, re.MULTILINE), name


# --------------------------------------------------------------------------- the new prose


FENCE_RE = re.compile(r"^(?:```|~~~)\s*([A-Za-z0-9_+-]*)\s*$")
SHELL_LANGS = frozenset({"shell", "sh", "bash", "console"})
RECIPE_SECTIONS = ("Examples", "Fixing what it finds")
MCPCHECK_WORD_RE = re.compile(r"^mcpcheck(?:[@=][\w.]+)?$")
CHAIN_TOKENS = frozenset({"|", "||", "&&", ";", "&", "|&", ">", ">>"})
REDIRECT_RE = re.compile(r"^\d*>")


def _fences(text: str) -> list[tuple[str, str]]:
    """``(language, body)`` for every fenced block of ``text`` (language may be empty)."""
    blocks: list[tuple[str, str]] = []
    lang: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        m = FENCE_RE.match(line.rstrip())
        if lang is None:
            if m is not None:
                lang, body = m.group(1), []
            continue
        if m is not None:
            blocks.append((lang, "\n".join(body)))
            lang = None
            continue
        body.append(line)
    return blocks


def _mcpcheck_option_tokens(text: str) -> list[str]:
    """Every option token passed to ``mcpcheck`` in the shell/console blocks of ``text``."""
    options: list[str] = []
    for lang, body in _fences(text):
        if lang not in SHELL_LANGS:
            continue
        for raw in body.splitlines():
            line = raw.strip()
            if lang == "console":
                if not line.startswith("$ "):
                    continue
                line = line[2:]
            line = re.sub(r"\s+#.*$", "", line).strip()
            if not line:
                continue
            try:
                tokens = shlex.split(line, comments=False)
            except ValueError:  # pragma: no cover - unbalanced quotes would be a typo
                continue
            start = next((i for i, tok in enumerate(tokens) if MCPCHECK_WORD_RE.match(tok)), None)
            if start is None:
                continue
            for tok in tokens[start + 1 :]:
                if tok in CHAIN_TOKENS or REDIRECT_RE.match(tok):
                    break
                if tok.startswith("-") and tok != "-":
                    options.append(tok)
    return options


def test_try_it_section_runs_the_demo() -> None:
    """The first thing a visitor sees is the one-command demo and where its source lives."""
    section = _section(_readme(), "Try it")
    assert "make demo" in section
    assert "bash examples/demo.sh" in section
    assert "examples/demo_server.py" in section
    assert "--break cache,origin,output-schema" in section
    assert "https://github.com/suresh-jbt/mcpcheck/blob/main/examples/README.md" in section


def test_demo_section_shows_a_real_all_pass_run() -> None:
    """The Try-it block is the demo server's real report, not a sketch."""
    section = _section(_readme(), "Try it")
    assert "server: mcpcheck-demo-support-desk 1.0.0 · speaks 2026-07-28 · 8 checks" in section
    assert "All 8 checks passed" in section
    for name in NAMES:
        assert re.search(rf"^✓ {name}\s", section, re.MULTILINE), name


def test_every_mcpcheck_command_uses_real_options() -> None:
    """Every flag handed to ``mcpcheck`` in a shell/console block is a real CLI option."""
    known = _help_option_names()
    used = _mcpcheck_option_tokens(_readme())
    assert used, "no mcpcheck invocations found in the README's shell blocks"
    unknown: list[str] = []
    for tok in used:
        if tok.startswith("--"):
            if tok.split("=", 1)[0] not in known:
                unknown.append(tok)
        elif not all(f"-{char}" in known for char in tok[1:]):  # clustered short flags (-vv)
            unknown.append(tok)
    assert unknown == [], f"not mcpcheck options: {unknown}"


def test_examples_section_is_a_set_of_recipes() -> None:
    """The Examples section is several titled recipes, each with a command to copy."""
    section = _section(_readme(), "Examples")
    recipes = [line for line in section.splitlines() if line.startswith("### ")]
    assert len(recipes) >= 8, recipes
    blocks = _fences(section)
    assert sum(1 for lang, _ in blocks if lang in SHELL_LANGS) >= len(recipes)


@pytest.mark.parametrize("title", RECIPE_SECTIONS)
def test_recipe_code_fences_are_tagged(title: str) -> None:
    """Every fence in the new sections declares a language (GitHub and PyPI both use it)."""
    untagged = [
        body.splitlines()[:1] for lang, body in _fences(_section(_readme(), title)) if not lang
    ]
    assert untagged == []


def test_fixing_section_explains_the_common_findings() -> None:
    """Each documented fix names a registered finding code and cites a verified spec URL."""
    section = _section(_readme(), "Fixing what it finds")
    expected = {
        "cache.ttl_missing",
        "cache.scope_missing",
        "origin.evil_accepted",
        "handshake.initialize_required",
        "schemas.output_schema_missing",
        "discover.server_info_missing",
        "errors.non_actionable_messages",
    }
    mentioned = set(FINDING_CODE_RE.findall(section))
    assert expected <= mentioned, sorted(expected - mentioned)
    known = set(spec_urls.all_urls().values())
    cited = set(SPEC_URL_RE.findall(section))
    assert len(cited) >= 5, cited
    assert cited <= known, sorted(cited - known)
    assert len([lang for lang, _ in _fences(section) if lang == "python"]) >= len(expected) - 2
