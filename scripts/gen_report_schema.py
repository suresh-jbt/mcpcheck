"""Regenerate the report JSON Schema (package data + repo copy); both must stay byte-identical.

Usage: ``uv run python scripts/gen_report_schema.py [--check]``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_COPY = ROOT / "src" / "mcpcheck" / "schema" / "report.v1.json"
REPO_COPY = ROOT / "schema" / "mcpcheck-report.v1.schema.json"


def render() -> str:
    """The schema text exactly as shipped."""
    sys.path.insert(0, str(ROOT / "src"))
    from mcpcheck.models import report_json_schema

    return json.dumps(report_json_schema(), indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    text = render()
    if "--check" in argv:
        stale = [
            p
            for p in (PACKAGE_COPY, REPO_COPY)
            if not p.exists() or p.read_text(encoding="utf-8") != text
        ]
        for p in stale:
            print(f"stale: {p.relative_to(ROOT)}")
        return 1 if stale else 0
    for p in (PACKAGE_COPY, REPO_COPY):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        print(f"wrote {p.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
