"""Check registry and selection (PLAN.md §6.8).

``REGISTRY`` is the canonical order; ``select()`` applies ``--check``/``--skip``
and always returns checks in canonical order regardless of CLI order.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mcpcheck.checks.base import Check
from mcpcheck.checks.cache import CacheCheck
from mcpcheck.checks.discover import DiscoverCheck
from mcpcheck.checks.errors import ErrorsCheck
from mcpcheck.checks.handshake import HandshakeCheck
from mcpcheck.checks.origin import OriginCheck
from mcpcheck.checks.protocol import ProtocolCheck
from mcpcheck.checks.schemas import SchemasCheck
from mcpcheck.checks.session import SessionCheck

if TYPE_CHECKING:
    from collections.abc import Iterable

REGISTRY: tuple[type[Check], ...] = (
    HandshakeCheck,
    SessionCheck,
    DiscoverCheck,
    ProtocolCheck,
    SchemasCheck,
    ErrorsCheck,
    CacheCheck,
    OriginCheck,
)
NAMES: tuple[str, ...] = tuple(c.name for c in REGISTRY)
BY_NAME: dict[str, type[Check]] = {c.name: c for c in REGISTRY}


class SelectionError(ValueError):
    """``--check``/``--skip`` combination leaves nothing sensible to run."""


def select(checks: Iterable[str] = (), skips: Iterable[str] = ()) -> list[Check]:
    """Instantiate the selected checks in canonical order.

    Args:
        checks: names from ``--check`` (empty → every check).
        skips: names from ``--skip``.

    Raises:
        SelectionError: unknown name, a name in both lists, or nothing left.
    """
    wanted = set(checks)
    skipped = set(skips)
    for name in wanted | skipped:
        if name not in BY_NAME:
            msg = f"unknown check {name!r}; choose from {', '.join(NAMES)}"
            raise SelectionError(msg)
    both = sorted(wanted & skipped, key=NAMES.index)
    if both:
        msg = f"--check and --skip both name '{both[0]}'."
        raise SelectionError(msg)
    selected = [
        cls() for cls in REGISTRY if (not wanted or cls.name in wanted) and cls.name not in skipped
    ]
    if not selected:
        msg = "No checks left to run after applying --skip."
        raise SelectionError(msg)
    return selected


__all__ = [
    "BY_NAME",
    "NAMES",
    "REGISTRY",
    "CacheCheck",
    "Check",
    "DiscoverCheck",
    "ErrorsCheck",
    "HandshakeCheck",
    "OriginCheck",
    "ProtocolCheck",
    "SchemasCheck",
    "SelectionError",
    "SessionCheck",
    "select",
]
