#!/usr/bin/env python3
"""Check that Markdown files render safely on PyPI (PLAN.md §12.5, items 1-6).

Usage: ``python scripts/check_readme.py README.md [CHANGELOG.md ...] [--release]``

Rules (each violation is printed as ``file:line: message``; exit 1 if any):
  1. no relative links or images — PyPI serves the description from another origin;
  2. no raw HTML outside a small allow-list, no ``target=``, no ``data:`` images,
     no ``mermaid`` fences (GitHub-only);
  3. every ``#anchor`` link targets a heading in the same file;
  4. ``readme_renderer`` must render the file (``variant="GFM"``) — what PyPI runs;
  5. with ``--release``: zero ``TO-BE-REPLACED`` / ``TO-FILL`` markers remain;
  6. inline code and fenced blocks are ignored by rules 1-3.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ALLOWED_TAGS = {
    "br",
    "sub",
    "sup",
    "kbd",
    "details",
    "summary",
    "p",
    "img",
    "a",
    "picture",
    "source",
}
MARKER_RE = re.compile(r"TO-BE-REPLACED|TO-FILL")
LINK_RE = re.compile(r"!?\[[^\]]*\]\(\s*(<[^>]*>|[^)\s]+)")
HTML_TAG_RE = re.compile(r"<\s*/?\s*([a-zA-Z][a-zA-Z0-9-]*)[^>]*>")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
HTML_HEADING_ID_RE = re.compile(r'id="([^"]+)"')


def slugify(heading: str) -> str:
    """Approximate GitHub/PyPI heading anchors."""
    text = re.sub(r"`([^`]*)`", r"\1", heading)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = text.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def strip_code(lines: list[str]) -> list[str]:
    """Blank out fenced blocks and inline code so rules 1-3 ignore them (rule 6)."""
    out: list[str] = []
    fence: str | None = None
    for line in lines:
        stripped = line.strip()
        if fence is None and (stripped.startswith(("```", "~~~"))):
            fence = stripped[:3]
            out.append("")
            continue
        if fence is not None:
            if stripped.startswith(fence):
                fence = None
            out.append("")
            continue
        out.append(re.sub(r"`[^`]*`", "", line))
    return out


def check_file(path: Path, *, release: bool) -> list[str]:
    """Return the list of violations for one Markdown file."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    problems: list[str] = []
    anchors: set[str] = set()
    for line in lines:
        m = HEADING_RE.match(line)
        if m:
            anchors.add(slugify(m.group(2)))
        anchors.update(HTML_HEADING_ID_RE.findall(line))

    fence: str | None = None
    for no, raw in enumerate(lines, 1):
        stripped = raw.strip()
        if fence is None and (stripped.startswith(("```", "~~~"))):
            fence = stripped[:3]
            if "mermaid" in stripped:
                problems.append(f"{path}:{no}: mermaid fence does not render on PyPI")
            continue
        if fence is not None:
            if stripped.startswith(fence):
                fence = None
            continue
        if release and MARKER_RE.search(raw):
            problems.append(f"{path}:{no}: placeholder marker left in a release document")

    for no, line in enumerate(strip_code(lines), 1):
        for m in LINK_RE.finditer(line):
            target = m.group(1).strip("<>")
            if target.startswith("#"):
                if target[1:] not in anchors:
                    problems.append(f"{path}:{no}: anchor {target!r} has no matching heading")
            elif not re.match(r"^(https?:)?//|^mailto:", target):
                problems.append(f"{path}:{no}: relative link/image {target!r} breaks on PyPI")
        for m in HTML_TAG_RE.finditer(line):
            tag = m.group(1).lower()
            if tag not in ALLOWED_TAGS:
                problems.append(f"{path}:{no}: HTML tag <{tag}> is stripped by PyPI")
            if "target=" in m.group(0):
                problems.append(f"{path}:{no}: target= attribute is stripped by PyPI")
            if 'src="data:' in m.group(0):
                problems.append(f"{path}:{no}: data: image is stripped by PyPI")

    try:
        from readme_renderer.markdown import render  # type: ignore[import-untyped,unused-ignore]
    except ImportError:
        problems.append(f"{path}: readme_renderer is not installed (uv sync --group lint)")
    else:
        if render(text, variant="GFM") is None:
            problems.append(f"{path}: readme_renderer could not render this file")
    return problems


def main(argv: list[str]) -> int:
    """Entry point; returns the process exit code."""
    release = "--release" in argv
    files = [Path(a) for a in argv if not a.startswith("--")]
    if not files:
        print(__doc__)
        return 2
    problems = [p for f in files for p in check_file(f, release=release)]
    for p in problems:
        print(p)
    if not problems:
        mode = "release" if release else "dev"
        print(f"ok: {', '.join(str(f) for f in files)} are PyPI-safe ({mode} mode)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
