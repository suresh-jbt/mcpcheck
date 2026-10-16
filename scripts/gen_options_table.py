#!/usr/bin/env python3
"""Render the README "Options" table from the live ``mcpcheck --help`` (PLAN.md §12.3 item 3).

Usage::

    uv run python scripts/gen_options_table.py                 # print the table
    uv run python scripts/gen_options_table.py README.md       # rewrite the block in place
    uv run python scripts/gen_options_table.py --check README.md   # exit 1 when the block is stale

The block in the README is delimited by the ``<!-- options:start -->`` and
``<!-- options:end -->`` comments. Each row reuses click's own help record, so the
table is what ``--help`` prints, reflowed: single-quoted fragments become inline
code (which also keeps ``<TEXT>`` from being read as an HTML tag on PyPI) and pipes
are escaped for GFM tables. ``tests/test_readme.py`` runs the ``--check`` mode.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import click

START = "<!-- options:start -->"
END = "<!-- options:end -->"
_QUOTED_RE = re.compile(r"'([^']+)'")


def _cell(text: str) -> str:
    """Escape one table cell: single quotes → inline code, pipes → ``\\|``."""
    text = _QUOTED_RE.sub(lambda m: f"`{m.group(1)}`", text)
    return text.replace("|", "\\|")


def option_rows() -> list[tuple[str, str]]:
    """``(option, description)`` for every option of the CLI, in declaration order."""
    from mcpcheck.__main__ import main as cli

    ctx = cli.make_context("mcpcheck", [], resilient_parsing=True)
    rows: list[tuple[str, str]] = []
    help_option = cli.get_help_option(ctx)
    params = [*cli.params, *([help_option] if help_option is not None else [])]
    for param in params:
        if not isinstance(param, click.Option):
            continue
        record = param.get_help_record(ctx)
        if record is None:
            continue
        opts, help_text = record
        rows.append((opts, " ".join(help_text.split())))
    return rows


def render_table() -> str:
    """The Markdown table (header + one row per option)."""
    lines = ["| Option | Description |", "|---|---|"]
    lines.extend(f"| `{_cell(opts)}` | {_cell(desc)} |" for opts, desc in option_rows())
    return "\n".join(lines) + "\n"


def render_block() -> str:
    """The delimited block as it appears in the README."""
    return f"{START}\n{render_table()}{END}\n"


def splice(text: str, block: str) -> str:
    """Return ``text`` with the delimited block replaced by ``block``."""
    start = text.index(START)
    end = text.index(END, start) + len(END)
    tail = text[end:].removeprefix("\n")
    return text[:start] + block + tail


def main(argv: list[str]) -> int:
    """Entry point; returns the process exit code."""
    check = "--check" in argv
    paths = [Path(a) for a in argv if not a.startswith("--")]
    block = render_block()
    if not paths:
        sys.stdout.write(render_table())
        return 0
    stale = 0
    for path in paths:
        text = path.read_text(encoding="utf-8")
        if START not in text or END not in text:
            print(f"{path}: missing {START} / {END} markers")
            return 2
        new = splice(text, block)
        if new == text:
            continue
        if check:
            print(f"{path}: options table is out of date (run scripts/gen_options_table.py {path})")
            stale = 1
        else:
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(new, encoding="utf-8")
            tmp.replace(path)
            print(f"wrote {path}")
    return stale


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
