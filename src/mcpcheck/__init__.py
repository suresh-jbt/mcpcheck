"""mcpcheck — validate an MCP server's conformance to the MCP 2026-07-28 revision."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("mcpcheck")
except PackageNotFoundError:  # pragma: no cover - only in a bare checkout
    __version__ = "0+unknown"

__all__ = ["__version__"]
