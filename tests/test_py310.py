"""Python 3.10 compatibility guard (PLAN.md §6.12): AST feature version + forbidden constructs."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "mcpcheck"
FORBIDDEN = re.compile(
    r"\bStrEnum\b|asyncio\.timeout\b|asyncio\.TaskGroup\b|except\*|\btomllib\b|datetime\.UTC\b|"
    r"\.add_note\(|from typing import [^\n]*\bSelf\b|typing\.Self\b"
)
SOURCES = sorted(SRC.rglob("*.py"))


@pytest.mark.parametrize("path", SOURCES, ids=[str(p.relative_to(SRC)) for p in SOURCES])
def test_parses_as_python_310(path: Path) -> None:
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 10))


@pytest.mark.parametrize("path", SOURCES, ids=[str(p.relative_to(SRC)) for p in SOURCES])
def test_no_forbidden_constructs(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    hits = [m.group(0) for m in FORBIDDEN.finditer(text)]
    assert not hits, hits


def test_builtin_exception_group_not_used_directly() -> None:
    for path in SOURCES:
        if path.name == "compat.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert "ExceptionGroup" not in names, path
        assert "BaseExceptionGroup" not in names, path


def test_compat_unwraps_single_member_groups() -> None:
    from mcpcheck.compat import ExceptionGroup, unwrap_exception_group

    inner = ValueError("x")
    group = ExceptionGroup("g", [ExceptionGroup("h", [inner])])
    assert unwrap_exception_group(group) is inner
    multi = ExceptionGroup("g", [ValueError("a"), ValueError("b")])
    assert unwrap_exception_group(multi) is multi
    assert unwrap_exception_group(inner) is inner
