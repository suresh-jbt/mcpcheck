"""Python 3.10 compatibility shims (PLAN.md §6.12).

``ExceptionGroup`` is a builtin from 3.11; on 3.10 anyio pulls in the
``exceptiongroup`` backport. Import it from here, never from ``builtins``.
"""

from __future__ import annotations

import sys

if sys.version_info >= (3, 11):
    import builtins

    ExceptionGroup = builtins.ExceptionGroup
    BaseExceptionGroup = builtins.BaseExceptionGroup
else:  # pragma: no cover - exercised only on CPython 3.10
    from exceptiongroup import (  # type: ignore[import-not-found,unused-ignore]
        BaseExceptionGroup,
        ExceptionGroup,
    )


def unwrap_exception_group(exc: BaseException) -> BaseException:
    """Return the innermost exception of nested single-member exception groups.

    anyio task groups wrap failures in ``ExceptionGroup``; when such a group has
    exactly one member the member is the interesting exception. Groups with
    several members are returned unchanged.
    """
    seen = 0
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1 and seen < 32:
        exc = exc.exceptions[0]
        seen += 1
    return exc


__all__ = ["BaseExceptionGroup", "ExceptionGroup", "unwrap_exception_group"]
