"""Pins the SDK facts mcpcheck relies on (PLAN.md Appendix A, §10.9); runs on every matrix cell."""

from __future__ import annotations

import importlib

import pytest

from mcpcheck import sdk_compat, wire

pytestmark = pytest.mark.sdk_matrix


def test_require_sdk_passes_and_reports_version() -> None:
    info = sdk_compat.require_sdk()
    assert info.version == sdk_compat.installed_sdk_version()
    assert info.warnings == ()


def test_wire_constants_equal_sdk_constants() -> None:
    import mcp_types
    from mcp.client.streamable_http import MCP_SESSION_ID
    from mcp.shared.inbound import (  # defining module; mcp.client.session re-exports them
        MCP_METHOD_HEADER,
        MCP_NAME_HEADER,
        MCP_PROTOCOL_VERSION_HEADER,
    )
    from mcp_types.version import MODERN_PROTOCOL_VERSIONS

    session = importlib.import_module("mcp.client.session")  # Appendix A import path
    for name in (
        "MCP_METHOD_HEADER",
        "MCP_NAME_HEADER",
        "MCP_PROTOCOL_VERSION_HEADER",
        "MODERN_PROTOCOL_VERSIONS",
    ):
        assert hasattr(session, name), name
    assert wire.PROTOCOL_VERSION == mcp_types.LATEST_PROTOCOL_VERSION
    assert MODERN_PROTOCOL_VERSIONS == ("2026-07-28",)
    assert wire.H_METHOD == MCP_METHOD_HEADER
    assert wire.H_NAME == MCP_NAME_HEADER
    assert wire.H_PROTOCOL_VERSION == MCP_PROTOCOL_VERSION_HEADER
    assert wire.H_SESSION_ID == MCP_SESSION_ID
    assert wire.META_PROTOCOL_VERSION == mcp_types.PROTOCOL_VERSION_META_KEY
    assert wire.META_CLIENT_INFO == mcp_types.CLIENT_INFO_META_KEY
    assert wire.META_CLIENT_CAPABILITIES == mcp_types.CLIENT_CAPABILITIES_META_KEY
    assert wire.META_SERVER_INFO == mcp_types.SERVER_INFO_META_KEY
    assert wire.META_LOG_LEVEL == mcp_types.LOG_LEVEL_META_KEY
    assert wire.HEADER_MISMATCH == mcp_types.HEADER_MISMATCH == -32020
    assert (
        wire.MISSING_REQUIRED_CLIENT_CAPABILITY
        == mcp_types.MISSING_REQUIRED_CLIENT_CAPABILITY
        == -32021
    )
    assert wire.UNSUPPORTED_PROTOCOL_VERSION == mcp_types.UNSUPPORTED_PROTOCOL_VERSION == -32022
    assert wire.METHOD_NOT_FOUND == mcp_types.METHOD_NOT_FOUND == -32601
    assert wire.INVALID_PARAMS == mcp_types.INVALID_PARAMS == -32602
    assert wire.INTERNAL_ERROR == mcp_types.INTERNAL_ERROR
    assert wire.PARSE_ERROR == mcp_types.PARSE_ERROR


def test_error_code_http_status_and_cacheable_methods() -> None:
    from mcp.shared.inbound import ERROR_CODE_HTTP_STATUS
    from mcp_types.methods import CACHEABLE_METHODS, SERVER_RESULTS

    assert ERROR_CODE_HTTP_STATUS == {
        -32700: 400,
        -32602: 400,
        -32601: 404,
        -32600: 400,
        -32022: 400,
        -32021: 400,
        -32020: 400,
    }
    assert set(CACHEABLE_METHODS) == {
        "prompts/list",
        "resources/list",
        "resources/read",
        "resources/templates/list",
        "server/discover",
        "tools/list",
    }
    assert ("tools/call", "2026-07-28") in SERVER_RESULTS
    assert ("server/discover", "2025-11-25") not in SERVER_RESULTS


def test_encode_header_value_sentinel() -> None:
    from mcp.shared.inbound import encode_header_value

    assert encode_header_value("héllo") == "=?base64?aMOpbGxv?="
    assert encode_header_value("=?base64?literal?=") == "=?base64?PT9iYXNlNjQ/bGl0ZXJhbD89?="
    assert encode_header_value("plain") == "plain"


def test_validate_server_result_is_type_lax() -> None:
    from mcp_types.methods import validate_server_result
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc:
        validate_server_result("tools/list", "2026-07-28", {"tools": []})
    locs = {e["loc"][0] for e in exc.value.errors()}
    assert locs == {"cacheScope", "resultType", "ttlMs"}
    base = {"tools": [], "resultType": "complete", "cacheScope": "public"}
    with pytest.raises(ValidationError):
        validate_server_result("tools/list", "2026-07-28", {**base, "ttlMs": -1})
    with pytest.raises(ValidationError):
        validate_server_result(
            "tools/list", "2026-07-28", {**base, "ttlMs": 1, "cacheScope": "user"}
        )
    for lax in ("5", 5.0, True):
        validate_server_result("tools/list", "2026-07-28", {**base, "ttlMs": lax})


def test_find_invalid_x_mcp_header_reason_string() -> None:
    from mcp.shared.inbound import find_invalid_x_mcp_header, mcp_param_headers, x_mcp_header_map

    schema = {
        "type": "object",
        "properties": {"amount": {"type": "number", "x-mcp-header": "Amount"}},
    }
    assert find_invalid_x_mcp_header(schema) == (
        "property 'amount': x-mcp-header is only permitted on integer/string/boolean properties (got 'number')"
    )
    ok_schema = {
        "type": "object",
        "properties": {"region": {"type": "string", "x-mcp-header": "Region"}},
    }
    assert find_invalid_x_mcp_header(ok_schema) is None
    assert mcp_param_headers(x_mcp_header_map(ok_schema), {"region": 12345}) == {
        "Mcp-Param-Region": "12345"
    }


def test_tool_annotations_default_none_and_implementation_requires_version() -> None:
    import mcp_types
    from pydantic import ValidationError

    ann = mcp_types.ToolAnnotations()
    assert ann.read_only_hint is None
    assert ann.destructive_hint is None
    with pytest.raises(ValidationError):
        mcp_types.Implementation.model_validate({"name": "x"})
    assert mcp_types.ClientCapabilities().model_dump(by_alias=True, exclude_none=True) == {}


def test_deprecation_warning_importable() -> None:
    from mcp.shared.exceptions import MCPDeprecationWarning, MCPError

    assert issubclass(MCPDeprecationWarning, Warning)
    assert issubclass(MCPError, Exception)


def test_require_sdk_error_message_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sdk_compat, "REQUIRED_SYMBOLS", (("mcp_types", "DOES_NOT_EXIST_7f3a"),))
    with pytest.raises(sdk_compat.SdkIncompatibleError) as exc:
        sdk_compat.require_sdk()
    text = str(exc.value)
    assert text.startswith(
        "mcpcheck needs mcp>=2.2,<3 exposing mcp_types.DOES_NOT_EXIST_7f3a; installed mcp "
    )
    assert text.endswith("Fix: pip install -U 'mcp>=2.2,<3'")
