"""SDK compatibility gate (PLAN.md §6.12).

``require_sdk()`` imports the handful of SDK symbols mcpcheck relies on
(Appendix A) and turns a missing one into an actionable error that the CLI maps
to exit code 2. Version drift (an SDK targeting a newer revision) is reported as
a warning string, never an error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version

SDK_REQUIREMENT = "mcp>=2.2,<3"
TARGET_REVISION = "2026-07-28"

# (module, attribute) pairs that must import; see PLAN.md Appendix A.
REQUIRED_SYMBOLS: tuple[tuple[str, str], ...] = (
    ("mcp_types", "LATEST_PROTOCOL_VERSION"),
    ("mcp_types", "PROTOCOL_VERSION_META_KEY"),
    ("mcp_types", "CLIENT_INFO_META_KEY"),
    ("mcp_types", "CLIENT_CAPABILITIES_META_KEY"),
    ("mcp_types", "SERVER_INFO_META_KEY"),
    ("mcp.client.session", "MCP_METHOD_HEADER"),
    ("mcp.client.session", "MCP_NAME_HEADER"),
    ("mcp.client.session", "MCP_PROTOCOL_VERSION_HEADER"),
    ("mcp.client.session", "MODERN_PROTOCOL_VERSIONS"),
    ("mcp.client.streamable_http", "MCP_SESSION_ID"),
    ("mcp.shared.inbound", "encode_header_value"),
    ("mcp.shared.inbound", "find_invalid_x_mcp_header"),
    ("mcp.shared.inbound", "x_mcp_header_map"),
    ("mcp.shared.inbound", "mcp_param_headers"),
    ("mcp.shared.inbound", "ERROR_CODE_HTTP_STATUS"),
    ("mcp_types.methods", "validate_server_result"),
    ("mcp_types.methods", "SERVER_RESULTS"),
    ("mcp_types.methods", "CACHEABLE_METHODS"),
)


class SdkIncompatibleError(Exception):
    """The installed ``mcp`` package does not expose what mcpcheck needs (exit 2)."""


@dataclass(frozen=True)
class SdkInfo:
    """Result of :func:`require_sdk`."""

    version: str
    warnings: tuple[str, ...] = field(default=())


def installed_sdk_version() -> str:
    """Return ``importlib.metadata.version("mcp")`` or ``"unknown"``."""
    try:
        return version("mcp")
    except PackageNotFoundError:  # pragma: no cover - mcp is a hard dependency
        return "unknown"


def _import_symbol(module_name: str, attr: str, installed: str) -> None:
    try:
        module = import_module(module_name)
        getattr(module, attr)
    except (ImportError, AttributeError) as exc:
        msg = (
            f"mcpcheck needs {SDK_REQUIREMENT} exposing {module_name}.{attr}; "
            f"installed mcp {installed}. Fix: pip install -U '{SDK_REQUIREMENT}'"
        )
        raise SdkIncompatibleError(msg) from exc


def require_sdk() -> SdkInfo:
    """Import every required SDK symbol and check the targeted revision.

    Raises:
        SdkIncompatibleError: when a symbol cannot be imported.
    """
    installed = installed_sdk_version()
    for module_name, attr in REQUIRED_SYMBOLS:
        _import_symbol(module_name, attr, installed)

    mcp_types = import_module("mcp_types")
    session = import_module("mcp.client.session")
    warnings: list[str] = []
    latest = getattr(mcp_types, "LATEST_PROTOCOL_VERSION", None)
    modern = tuple(getattr(session, "MODERN_PROTOCOL_VERSIONS", ()))
    if latest != TARGET_REVISION or TARGET_REVISION not in modern:
        warnings.append(
            f"installed SDK (mcp {installed}) targets revision {latest}; "
            f"results apply to {TARGET_REVISION}"
        )
    return SdkInfo(version=installed, warnings=tuple(warnings))


__all__ = [
    "REQUIRED_SYMBOLS",
    "SDK_REQUIREMENT",
    "TARGET_REVISION",
    "SdkIncompatibleError",
    "SdkInfo",
    "installed_sdk_version",
    "require_sdk",
]
