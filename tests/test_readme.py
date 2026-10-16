"""README / companion-doc consistency (PLAN.md §10.7, §12.3).

Everything here is pure text processing over the repository files; no network, no
``readme_renderer`` (that runs in ``scripts/check_readme.py`` from the lint job).
"""

from __future__ import annotations

import importlib.util
import re
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
