"""Registry of every finding code mcpcheck can emit (PLAN.md §7, §8.7).

Grammar: ``<check>.<snake_slug>`` (see ``CODE_PATTERN`` in ``models.py``) (plus the reserved
``runner.*`` codes). Each entry fixes the normative *severity* of the code
(``must`` → ``failures[]``; ``should``/``best-practice`` → ``warnings[]``;
``info`` → ``info[]``), a one-line description and the spec clause it cites.

To add a code: add one ``_c(...)`` line to the right block below, or — from a
check module, at import time — call :func:`register`. The
:class:`~mcpcheck.checks.base.Findings` builder refuses unregistered codes and
codes used at a different severity than registered, and ``tests/test_codes.py``
re-checks the grammar.
"""

from __future__ import annotations

import re
from typing import Literal, NamedTuple

from mcpcheck import spec_urls as su
from mcpcheck.models import CODE_PATTERN, Level

CODE_RE = re.compile(CODE_PATTERN)

Severity = Literal["must", "should", "best-practice", "info"]


class CodeInfo(NamedTuple):
    """Registry entry for one finding code."""

    severity: Severity
    description: str
    spec_url: str | None

    @property
    def level(self) -> Level:
        """The :class:`Level` recorded on findings with this code (``info`` → best-practice)."""
        return Level.BEST_PRACTICE if self.severity == "info" else Level(self.severity)


def _c(
    code: str, severity: Severity, description: str, spec_url: str | None
) -> tuple[str, CodeInfo]:
    return code, CodeInfo(severity, description, spec_url)


FINDING_CODES: dict[str, CodeInfo] = dict(
    [
        # ------------------------------------------------------------- runner (§6.9, §8.7)
        _c(
            "runner.not_run",
            "info",
            "Check not run: the handshake gate blocked it or the run was aborted",
            None,
        ),
        _c("runner.internal_error", "info", "mcpcheck crashed inside a check (bug)", None),
        # ------------------------------------------------------------- handshake (§7.1)
        _c(
            "handshake.legacy_shaped_result",
            "must",
            "Result lacks resultType (pre-2026 shape)",
            su.SPEC_RESULT_RESPONSES,
        ),
        _c(
            "handshake.unexpected_result_type",
            "must",
            'resultType is not "complete" on tools/list',
            su.SPEC_RESULT_RESPONSES,
        ),
        _c(
            "handshake.id_mismatch",
            "must",
            "Response id differs from the request id",
            su.SPEC_RESULT_RESPONSES,
        ),
        _c(
            "handshake.tools_not_list",
            "must",
            "result.tools is not an array",
            su.SPEC_TOOLS_CAPABILITIES,
        ),
        _c(
            "handshake.server_info_missing",
            "should",
            "_meta serverInfo absent or lacking name/version",
            su.SPEC_META,
        ),
        _c(
            "handshake.server_request_on_stream",
            "must",
            "Server sent a JSON-RPC request on the response stream",
            su.SPEC_RECEIVING_MESSAGES,
        ),
        _c(
            "handshake.stream_not_terminated",
            "should",
            "SSE stream not closed after the response",
            su.SPEC_RECEIVING_MESSAGES,
        ),
        _c(
            "handshake.version_unsupported",
            "must",
            "Server rejects 2026-07-28 with -32022",
            su.SPEC_PROTOCOL_VERSION_HEADER,
        ),
        _c(
            "handshake.contradictory_unsupported",
            "must",
            "-32022 although data.supported lists 2026-07-28",
            su.SPEC_PROTOCOL_VERSION_HEADER,
        ),
        _c(
            "handshake.unsupported_data_malformed",
            "must",
            "-32022 data is not UnsupportedProtocolVersionErrorData",
            su.SPEC_SCHEMA_UNSUPPORTED_VERSION,
        ),
        _c(
            "handshake.header_mismatch_on_valid_request",
            "must",
            "-32020 on a well-formed request (intermediary stripping headers?)",
            su.SPEC_SERVER_VALIDATION,
        ),
        _c(
            "handshake.client_capability_demanded",
            "should",
            "Server demands a client capability (-32021)",
            su.SPEC_META,
        ),
        _c(
            "handshake.capability_error_malformed",
            "must",
            "-32021 data.requiredCapabilities is not a non-empty string array",
            su.SPEC_META,
        ),
        _c(
            "handshake.wrong_status_for_32021",
            "must",
            "-32021 without HTTP 400",
            su.SPEC_META,
        ),
        _c(
            "handshake.initialize_required",
            "must",
            "Server requires the legacy initialize handshake",
            su.SPEC_STATELESSNESS,
        ),
        _c(
            "handshake.not_jsonrpc",
            "must",
            "2xx response is not a JSON-RPC body",
            su.SPEC_SENDING_MESSAGES,
        ),
        _c(
            "handshake.discover_missing",
            "must",
            "server/discover not implemented",
            su.SPEC_DISCOVER,
        ),
        _c(
            "handshake.tools_declared_but_unimplemented",
            "must",
            "capabilities.tools declared but tools/list is -32601",
            su.SPEC_TOOLS_CAPABILITIES,
        ),
        _c(
            "handshake.initialize_error_names_no_version",
            "should",
            "Legacy initialize error names no supported version",
            su.SPEC_BACKCOMPAT_INIT,
        ),
        _c(
            "handshake.dual_era",
            "info",
            "Server also answers the legacy initialize handshake",
            su.SPEC_COMPAT_MATRIX,
        ),
        _c(
            "handshake.session_id_seen",
            "info",
            "Mcp-Session-Id response header observed (judged by session)",
            su.SPEC_EARLIER_REVISIONS,
        ),
        # ------------------------------------------------------------- session (§7.2)
        _c(
            "session.required",
            "must",
            "Modern request rejected for lacking a session",
            su.SPEC_STATELESSNESS,
        ),
        _c(
            "session.bogus_id_rejected",
            "must",
            "Bogus Mcp-Session-Id rejected while plain request succeeded",
            su.SPEC_STATELESSNESS,
        ),
        _c(
            "session.list_varies_per_connection",
            "must",
            "List results differ between fresh connections",
            su.SPEC_TOOLS_CAPABILITIES,
        ),
        _c(
            "session.list_varies_within_connection",
            "must",
            "List results differ within one connection",
            su.SPEC_TOOLS_CAPABILITIES,
        ),
        _c(
            "session.list_varies_with_session_header",
            "must",
            "List results differ when a bogus session id is sent",
            su.SPEC_TOOLS_CAPABILITIES,
        ),
        _c(
            "session.session_id_minted",
            "should",
            "Server mints Mcp-Session-Id on modern responses",
            su.SPEC_EARLIER_REVISIONS,
        ),
        _c(
            "session.session_id_echoed",
            "should",
            "Server echoes a client-supplied Mcp-Session-Id",
            su.SPEC_EARLIER_REVISIONS,
        ),
        _c(
            "session.definition_unstable",
            "should",
            "Same items, different definition bytes across connections",
            su.SPEC_TOOLS_CAPABILITIES,
        ),
        _c(
            "session.order_not_deterministic",
            "should",
            "Tool order differs across connections",
            su.SPEC_TOOLS_CAPABILITIES,
        ),
        _c(
            "session.list_changed_during_run",
            "best-practice",
            "List changed between the run cache and the first sample",
            su.SPEC_TOOLS_CAPABILITIES,
        ),
        _c(
            "session.cursor_loop",
            "should",
            "Repeated nextCursor (pagination loop)",
            su.SPEC_PAGINATION_GUIDELINES,
        ),
        _c(
            "session.walk_truncated",
            "info",
            "List walk stopped by mcpcheck's page or byte budget (compared on pages fetched)",
            su.SPEC_PAGINATION_GUIDELINES,
        ),
        _c(
            "session.partial_samples",
            "should",
            "Some fresh connections failed",
            su.SPEC_STATELESSNESS,
        ),
        # ------------------------------------------------------------- discover (§7.3)
        _c("discover.not_implemented", "must", "server/discover answered -32601", su.SPEC_DISCOVER),
        _c(
            "discover.wrong_status_for_32601",
            "must",
            "-32601 without HTTP 404",
            su.SPEC_PROTOCOL_VERSION_HEADER,
        ),
        _c(
            "discover.version_unsupported",
            "must",
            "Server rejects 2026-07-28 with -32022",
            su.SPEC_PROTOCOL_VERSION_HEADER,
        ),
        _c(
            "discover.contradictory_unsupported",
            "must",
            "-32022 although data.supported lists 2026-07-28",
            su.SPEC_PROTOCOL_VERSION_HEADER,
        ),
        _c(
            "discover.unsupported_data_malformed",
            "must",
            "-32022 data is malformed",
            su.SPEC_SCHEMA_UNSUPPORTED_VERSION,
        ),
        _c(
            "discover.wrong_status_for_32022",
            "must",
            "-32022 without HTTP 400",
            su.SPEC_PROTOCOL_VERSION_HEADER,
        ),
        _c(
            "discover.header_mismatch_on_valid_request",
            "must",
            "-32020 on a well-formed request",
            su.SPEC_SERVER_VALIDATION,
        ),
        _c(
            "discover.client_capability_demanded",
            "should",
            "Server demands a client capability (-32021)",
            su.SPEC_META,
        ),
        _c(
            "discover.capability_error_malformed",
            "must",
            "-32021 data.requiredCapabilities is not a non-empty string array",
            su.SPEC_META,
        ),
        _c(
            "discover.wrong_status_for_32021",
            "must",
            "-32021 without HTTP 400",
            su.SPEC_META,
        ),
        _c(
            "discover.invalid_params_on_valid_request",
            "must",
            "-32602 on a valid server/discover request",
            su.SPEC_DISCOVER,
        ),
        _c(
            "discover.unexpected_error",
            "must",
            "Unexpected JSON-RPC error on server/discover",
            su.SPEC_DISCOVER,
        ),
        _c(
            "discover.forbidden_error_code",
            "must",
            "Error code from a reserved/forbidden range",
            su.SPEC_ERROR_CODES,
        ),
        _c(
            "discover.legacy_error_code",
            "should",
            "Error code from the legacy -32000..-32019 range",
            su.SPEC_ERROR_CODES,
        ),
        _c(
            "discover.post_rejected",
            "must",
            "POST to the MCP endpoint rejected with HTTP 405",
            su.SPEC_SENDING_MESSAGES,
        ),
        _c(
            "discover.not_jsonrpc",
            "must",
            "2xx response is not a JSON-RPC body",
            su.SPEC_SENDING_MESSAGES,
        ),
        _c(
            "discover.no_response",
            "must",
            "No JSON-RPC response in the body/stream",
            su.SPEC_RESULT_RESPONSES,
        ),
        _c(
            "discover.multiple_responses",
            "must",
            "More than one response to a single request",
            su.SPEC_RESULT_RESPONSES,
        ),
        _c(
            "discover.server_request_on_stream",
            "must",
            "Server sent a JSON-RPC request on the stream",
            su.SPEC_RECEIVING_MESSAGES,
        ),
        _c(
            "discover.log_without_loglevel",
            "must",
            "notifications/message without a logLevel in the request",
            su.SPEC_LOGGING,
        ),
        _c(
            "discover.progress_notification",
            "info",
            "notifications/progress received without a progress token",
            su.SPEC_RECEIVING_MESSAGES,
        ),
        _c(
            "discover.stream_not_terminated",
            "should",
            "SSE stream not closed after the response",
            su.SPEC_RECEIVING_MESSAGES,
        ),
        _c(
            "discover.no_accel_buffering_header",
            "should",
            "SSE response lacks X-Accel-Buffering: no",
            su.SPEC_RECEIVING_MESSAGES,
        ),
        _c(
            "discover.envelope_invalid",
            "must",
            "JSON-RPC envelope malformed (jsonrpc/result/error)",
            su.SPEC_RESULT_RESPONSES,
        ),
        _c(
            "discover.id_mismatch",
            "must",
            "Response id differs from the request id",
            su.SPEC_RESULT_RESPONSES,
        ),
        _c("discover.result_type_missing", "must", "resultType missing", su.SPEC_RESULT_RESPONSES),
        _c(
            "discover.result_type_not_string",
            "must",
            "resultType is not a string",
            su.SPEC_RESULT_RESPONSES,
        ),
        _c(
            "discover.unexpected_result_type",
            "must",
            "resultType input_required on server/discover",
            su.SPEC_RESULT_RESPONSES,
        ),
        _c(
            "discover.unrecognized_result_type",
            "must",
            "Unrecognized resultType",
            su.SPEC_RESULT_RESPONSES,
        ),
        _c(
            "discover.supported_versions_invalid",
            "must",
            "supportedVersions missing or not a string array",
            su.SPEC_SCHEMA_DISCOVER_RESULT,
        ),
        _c(
            "discover.supported_versions_omit_served",
            "best-practice",
            "supportedVersions omits the version that was just served",
            su.SPEC_DISCOVER_RESULT,
        ),
        _c(
            "discover.supported_versions_duplicate",
            "should",
            "Duplicate entries in supportedVersions",
            su.SPEC_DISCOVER_RESULT,
        ),
        _c(
            "discover.supported_versions_nondate",
            "info",
            "Non date-shaped entry in supportedVersions",
            su.SPEC_VERSIONING_TERMINOLOGY,
        ),
        _c(
            "discover.capabilities_invalid",
            "must",
            "capabilities missing or not an object",
            su.SPEC_SCHEMA_DISCOVER_RESULT,
        ),
        _c(
            "discover.capability_null",
            "must",
            "Known capability key present with null",
            su.SPEC_SCHEMA_SERVER_CAPABILITIES,
        ),
        _c(
            "discover.capability_flag_not_boolean",
            "must",
            "listChanged/subscribe is not a boolean",
            su.SPEC_SCHEMA_SERVER_CAPABILITIES,
        ),
        _c(
            "discover.logging_deprecated",
            "should",
            "Deprecated logging capability declared",
            su.SPEC_DEPRECATED,
        ),
        _c(
            "discover.extension_id_invalid",
            "must",
            "Extension id violates the _meta key rules",
            su.SPEC_EXTENSION_NEGOTIATION,
        ),
        _c(
            "discover.extension_value_not_object",
            "must",
            "Extension value is not an object",
            su.SPEC_EXTENSION_NEGOTIATION,
        ),
        _c(
            "discover.capability_unknown",
            "info",
            "Unknown capability key (open set)",
            su.SPEC_SCHEMA_SERVER_CAPABILITIES,
        ),
        _c("discover.meta_not_object", "must", "_meta is not an object", su.SPEC_META),
        _c("discover.meta_key_invalid", "must", "_meta key violates the key format", su.SPEC_META),
        _c(
            "discover.meta_reserved_key_unknown",
            "info",
            "Key under a reserved _meta prefix that the core spec does not define (extensions may)",
            su.SPEC_META,
        ),
        _c(
            "discover.server_info_missing",
            "should",
            "Server does not identify itself",
            su.SPEC_DISCOVER_RESULT,
        ),
        _c(
            "discover.server_info_malformed",
            "must",
            "serverInfo is not a valid Implementation",
            su.SPEC_SCHEMA_IMPLEMENTATION,
        ),
        _c(
            "discover.server_info_url_scheme",
            "best-practice",
            "serverInfo websiteUrl/icon uses a non-https scheme",
            su.SPEC_ICONS,
        ),
        _c(
            "discover.instructions_absent",
            "info",
            "No instructions provided",
            su.SPEC_DISCOVER_RESULT,
        ),
        _c(
            "discover.instructions_not_string",
            "must",
            "instructions is not a string",
            su.SPEC_SCHEMA_DISCOVER_RESULT,
        ),
        _c("discover.ttl_missing", "must", "Result has no ttlMs", su.SPEC_CACHEABLE_RESULTS),
        _c("discover.ttl_not_integer", "must", "ttlMs is not a JSON number", su.SPEC_TTL),
        _c(
            "discover.ttl_not_integral",
            "should",
            "ttlMs is a finite non-integral number (the spec defines it as an integer)",
            su.SPEC_TTL,
        ),
        _c(
            "discover.ttl_integral_float",
            "best-practice",
            "ttlMs is an integral float",
            su.SPEC_TTL,
        ),
        _c("discover.ttl_negative", "must", "ttlMs is negative", su.SPEC_TTL),
        _c("discover.ttl_implausible", "best-practice", "ttlMs exceeds 30 days", su.SPEC_TTL),
        _c("discover.scope_missing", "must", "Result has no cacheScope", su.SPEC_CACHE_SCOPE),
        _c(
            "discover.scope_invalid",
            "must",
            'cacheScope is not "public" or "private"',
            su.SPEC_CACHE_SCOPE,
        ),
        _c(
            "discover.legacy_initialize_fields",
            "best-practice",
            "Top-level serverInfo/protocolVersion (half-ported legacy server)",
            su.SPEC_DISCOVER_RESULT,
        ),
        _c(
            "discover.next_cursor_present",
            "best-practice",
            "nextCursor on a non-paginated result",
            su.SPEC_DISCOVER_RESULT,
        ),
        _c("discover.extra_key", "info", "Unknown top-level result key", su.SPEC_DISCOVER_RESULT),
        _c(
            "discover.schema_validation",
            "must",
            "SDK validator rejects the result",
            su.SPEC_SCHEMA_DISCOVER_RESULT,
        ),
        _c(
            "discover.capability_declared_but_unimplemented",
            "must",
            "Declared capability's list method answers -32601",
            su.SPEC_TOOLS_CAPABILITIES,
        ),
        _c(
            "discover.capability_unverified",
            "should",
            "Declared capability could not be verified",
            su.SPEC_TOOLS_CAPABILITIES,
        ),
        _c(
            "discover.capability_undeclared",
            "must",
            "List method serves items without the capability declared",
            su.SPEC_TOOLS_CAPABILITIES,
        ),
        _c(
            "discover.capability_undeclared_empty",
            "best-practice",
            "Undeclared list method serves an empty list",
            su.SPEC_TOOLS_CAPABILITIES,
        ),
        _c(
            "discover.capability_consistent",
            "info",
            "Declared capability and list method agree",
            su.SPEC_TOOLS_CAPABILITIES,
        ),
        _c(
            "discover.cache_headers",
            "info",
            "HTTP cache headers observed (not evaluated)",
            su.SPEC_CACHEABLE_RESULTS,
        ),
        # ------------------------------------------------------------- protocol (§7.8)
        _c(
            "protocol.header_mismatch_accepted",
            "must",
            "Header/_meta version mismatch accepted",
            su.SPEC_SERVER_VALIDATION,
        ),
        _c(
            "protocol.header_mismatch_wrong_status",
            "must",
            "Header mismatch rejected without 400 + -32020",
            su.SPEC_SERVER_VALIDATION,
        ),
        _c(
            "protocol.headerless_accepted",
            "should",
            "Request without MCP-Protocol-Version accepted",
            su.SPEC_PROTOCOL_VERSION_HEADER,
        ),
        _c(
            "protocol.unsupported_version_not_rejected",
            "must",
            "Unsupported version accepted",
            su.SPEC_PROTOCOL_VERSION_HEADER,
        ),
        _c(
            "protocol.unsupported_data_missing",
            "must",
            "-32022 without data.supported/requested",
            su.SPEC_SCHEMA_UNSUPPORTED_VERSION,
        ),
        _c(
            "protocol.unsupported_wrong_status",
            "must",
            "-32022 without HTTP 400",
            su.SPEC_PROTOCOL_VERSION_HEADER,
        ),
        _c(
            "protocol.missing_mcp_method_accepted",
            "must",
            "Request without Mcp-Method accepted",
            su.SPEC_STANDARD_HEADERS,
        ),
        _c(
            "protocol.method_header_case_accepted",
            "must",
            "Mcp-Method value with wrong case accepted",
            su.SPEC_SERVER_VALIDATION,
        ),
        _c(
            "protocol.missing_meta_accepted",
            "must",
            "Request lacking a required _meta field accepted",
            su.SPEC_META,
        ),
        _c(
            "protocol.missing_meta_wrong_code",
            "must",
            "Missing _meta field rejected without -32602",
            su.SPEC_META,
        ),
        _c(
            "protocol.missing_meta_wrong_status",
            "must",
            "Missing _meta field rejected without HTTP 400",
            su.SPEC_META,
        ),
        _c(
            "protocol.unknown_method_wrong_status",
            "must",
            "Unknown method not answered with HTTP 404",
            su.SPEC_PROTOCOL_VERSION_HEADER,
        ),
        _c(
            "protocol.unknown_method_not_jsonrpc",
            "must",
            "Unknown method not answered with -32601",
            su.SPEC_PROTOCOL_VERSION_HEADER,
        ),
        _c(
            "protocol.invalid_cursor_not_rejected",
            "should",
            "Invalid cursor not answered with -32602",
            su.SPEC_PAGINATION_ERRORS,
        ),
        _c(
            "protocol.param_header_omission_accepted",
            "must",
            "Body value without its Mcp-Param-* header accepted",
            su.SPEC_PARAM_HEADERS,
        ),
        _c(
            "protocol.legacy_get_hosted",
            "info",
            "GET opens a legacy SSE stream (dual-era MAY)",
            su.SPEC_EARLIER_REVISIONS,
        ),
        _c(
            "protocol.forbidden_error_code",
            "must",
            "Error code from a reserved/forbidden range",
            su.SPEC_ERROR_CODES,
        ),
        _c(
            "protocol.no_accel_buffering_header",
            "should",
            "SSE response lacks X-Accel-Buffering: no",
            su.SPEC_RECEIVING_MESSAGES,
        ),
        _c(
            "protocol.bare_400_accepted",
            "info",
            "Bare HTTP 400 without JSON-RPC body accepted as rejection",
            su.SPEC_SERVER_VALIDATION,
        ),
        # ------------------------------------------------------------- schemas (§7.4)
        _c(
            "schemas.pagination_rejected_own_cursor",
            "should",
            "Server rejected the cursor it issued",
            su.SPEC_PAGINATION_ERRORS,
        ),
        _c(
            "schemas.tools_list_unimplemented",
            "must",
            "capabilities.tools declared but tools/list is -32601",
            su.SPEC_TOOLS_CAPABILITIES,
        ),
        _c(
            "schemas.not_jsonrpc",
            "must",
            "2xx response is not a JSON-RPC body",
            su.SPEC_SENDING_MESSAGES,
        ),
        _c(
            "schemas.listing_malformed",
            "must",
            "result.tools is not an array",
            su.SPEC_TOOLS_CAPABILITIES,
        ),
        _c(
            "schemas.tool_malformed",
            "must",
            "Tool object does not match the Tool schema",
            su.SPEC_TOOL,
        ),
        _c(
            "schemas.pagination_malformed",
            "must",
            "nextCursor is not a string",
            su.SPEC_PAGINATION_GUIDELINES,
        ),
        _c(
            "schemas.pagination_incomplete",
            "should",
            "Cursor loop or MAX_PAGES reached",
            su.SPEC_PAGINATION_GUIDELINES,
        ),
        _c("schemas.input_schema_missing", "must", "inputSchema missing", su.SPEC_TOOL),
        _c("schemas.input_schema_not_object", "must", "inputSchema is not an object", su.SPEC_TOOL),
        _c(
            "schemas.input_schema_type",
            "must",
            'inputSchema root type is not literally "object"',
            su.SPEC_TOOL,
        ),
        _c(
            "schemas.input_schema_invalid",
            "must",
            "inputSchema is not a valid JSON Schema under its dialect",
            su.SPEC_SCHEMA_VALIDATION,
        ),
        _c(
            "schemas.dialect_unsupported",
            "should",
            "Unknown $schema dialect (validity unasserted)",
            su.SPEC_SCHEMA_DIALECT,
        ),
        _c(
            "schemas.dialect_not_2020_12",
            "should",
            "Schema declares a dialect other than 2020-12",
            su.SPEC_SCHEMA_DIALECT,
        ),
        _c(
            "schemas.schema_unverifiable",
            "should",
            "Schema exceeds depth/node bounds (not validated)",
            su.SPEC_COMPOSITION_BOUNDS,
        ),
        _c(
            "schemas.output_schema_missing",
            "best-practice",
            "outputSchema absent (optional per spec)",
            su.SPEC_OUTPUT_SCHEMA,
        ),
        _c(
            "schemas.output_schema_not_object",
            "must",
            "outputSchema is not an object",
            su.SPEC_OUTPUT_SCHEMA,
        ),
        _c(
            "schemas.output_schema_invalid",
            "must",
            "outputSchema is not a valid JSON Schema",
            su.SPEC_SCHEMA_VALIDATION,
        ),
        _c(
            "schemas.output_schema_permissive",
            "best-practice",
            "outputSchema accepts anything",
            su.SPEC_OUTPUT_SCHEMA,
        ),
        _c(
            "schemas.x_mcp_header_invalid",
            "must",
            "x-mcp-header violates a MUST; conforming clients drop the tool",
            su.SPEC_X_MCP_HEADER,
        ),
        _c(
            "schemas.x_mcp_header_sensitive",
            "should",
            "Sensitive parameter mirrored into a header",
            su.SPEC_X_MCP_HEADER,
        ),
        _c(
            "schemas.name_invalid",
            "should",
            "Tool name violates the naming SHOULDs",
            su.SPEC_TOOL_NAMES,
        ),
        _c("schemas.name_duplicate", "should", "Duplicate tool name", su.SPEC_TOOL_NAMES),
        _c(
            "schemas.ref_external",
            "should",
            "External $ref (never fetched)",
            su.SPEC_REF_RESOLUTION,
        ),
        _c(
            "schemas.icon_src_scheme",
            "best-practice",
            "Icon src is not https or data:",
            su.SPEC_ICONS,
        ),
        _c(
            "schemas.sdk_rejects_listing",
            "must",
            "SDK validator rejects the listing for an unmodelled reason",
            su.SPEC_TOOL,
        ),
        # ------------------------------------------------------------- errors (§7.5)
        _c(
            "errors.bad_content_type",
            "must",
            "tools/call answered with a non-JSON-RPC media type",
            su.SPEC_SENDING_MESSAGES,
        ),
        _c(
            "errors.server_request_on_stream",
            "must",
            "Server sent a JSON-RPC request on the stream",
            su.SPEC_RECEIVING_MESSAGES,
        ),
        _c(
            "errors.log_without_loglevel",
            "must",
            "notifications/message without a logLevel in the request",
            su.SPEC_LOGGING,
        ),
        _c(
            "errors.progress_notification",
            "info",
            "notifications/progress received without a progress token",
            su.SPEC_RECEIVING_MESSAGES,
        ),
        _c(
            "errors.missing_response",
            "should",
            "No JSON-RPC response to the probe",
            su.SPEC_RESULT_RESPONSES,
        ),
        _c(
            "errors.id_mismatch",
            "must",
            "Response id differs from the request id",
            su.SPEC_ERROR_RESPONSES,
        ),
        _c(
            "errors.id_null_on_4xx",
            "should",
            "Null id on a 4xx error to a well-formed request",
            su.SPEC_ERROR_RESPONSES,
        ),
        _c(
            "errors.malformed_error",
            "must",
            "error.code not an integer or message not a string",
            su.SPEC_ERROR_RESPONSES,
        ),
        _c(
            "errors.header_mismatch",
            "info",
            "-32020 on a probe (headers recorded)",
            su.SPEC_SERVER_VALIDATION,
        ),
        _c(
            "errors.protocol_rejection",
            "info",
            "-32021/-32022 on a probe",
            su.SPEC_PROTOCOL_VERSION_HEADER,
        ),
        _c(
            "errors.tools_call_not_implemented",
            "best-practice",
            "tools/call answers -32601 although tools are listed",
            su.SPEC_TOOL_ERRORS,
        ),
        _c(
            "errors.forbidden_error_code",
            "must",
            "Error code -32002/-32042 (MUST NOT be emitted)",
            su.SPEC_ERROR_CODES,
        ),
        _c(
            "errors.reserved_error_code",
            "must",
            "Undefined code in the reserved -32020..-32099 range",
            su.SPEC_ERROR_CODES,
        ),
        _c(
            "errors.legacy_error_code",
            "should",
            "Error code from the legacy -32000..-32019 range",
            su.SPEC_ERROR_CODES,
        ),
        _c(
            "errors.missing_result_type",
            "must",
            "Result lacks a string resultType",
            su.SPEC_RESULT_RESPONSES,
        ),
        _c(
            "errors.undeclared_input_requests",
            "must",
            "inputRequests sent although the client declared no support",
            su.SPEC_MRTR_SERVER_REQS,
        ),
        _c(
            "errors.empty_input_required",
            "must",
            "InputRequiredResult with neither inputRequests nor requestState",
            su.SPEC_MRTR_SERVER_REQS,
        ),
        _c(
            "errors.input_required_state_only",
            "info",
            "input_required with requestState only (load shedding)",
            su.SPEC_MRTR_SERVER_REQS,
        ),
        _c(
            "errors.unknown_result_type",
            "must",
            "Unrecognized resultType",
            su.SPEC_RESULT_RESPONSES,
        ),
        _c(
            "errors.structured_content_present",
            "info",
            "Error result carries structuredContent",
            su.SPEC_TOOL_ERRORS,
        ),
        _c(
            "errors.accepted_invalid_input",
            "must",
            "Tool executed on schema-invalid input",
            su.SPEC_TOOLS_SECURITY,
        ),
        _c(
            "errors.accepted_wrong_type",
            "should",
            "Tool accepted wrong-typed input (lenient coercion)",
            su.SPEC_TOOLS_SECURITY,
        ),
        _c(
            "errors.malformed_result",
            "should",
            "SDK validator rejects the CallToolResult",
            su.SPEC_TOOL_ERRORS,
        ),
        _c(
            "errors.verbose_error_message",
            "should",
            "JSON-RPC error message is not a concise single sentence",
            su.SPEC_ERROR_RESPONSES,
        ),
        _c(
            "errors.unknown_tool_executed",
            "best-practice",
            "Call to a non-existent tool returned a non-error result",
            su.SPEC_TOOL_ERRORS,
        ),
        _c(
            "errors.non_actionable_messages",
            "best-practice",
            "Fewer than 80 % of error messages are actionable",
            su.SPEC_TOOL_ERRORS,
        ),
        _c(
            "errors.non_actionable_message",
            "best-practice",
            "Error message fails the actionability heuristics",
            su.SPEC_TOOL_ERRORS,
        ),
        _c(
            "errors.tool_skipped",
            "info",
            "Tool not probed (policy or untestable schema)",
            su.SPEC_TOOLS_SECURITY,
        ),
        # ------------------------------------------------------------- cache (§7.6)
        _c(
            "cache.unevaluated",
            "should",
            "List method could not be evaluated",
            su.SPEC_CACHEABLE_RESULTS,
        ),
        _c("cache.ttl_missing", "must", "Result has no ttlMs", su.SPEC_CACHEABLE_RESULTS),
        _c("cache.ttl_not_integer", "must", "ttlMs is not a JSON number", su.SPEC_TTL),
        _c(
            "cache.ttl_not_integral",
            "should",
            "ttlMs is a finite non-integral number (the spec defines it as an integer)",
            su.SPEC_TTL,
        ),
        _c("cache.ttl_integral_float", "best-practice", "ttlMs is an integral float", su.SPEC_TTL),
        _c("cache.ttl_negative", "must", "ttlMs is negative", su.SPEC_TTL),
        _c("cache.ttl_implausible", "best-practice", "ttlMs exceeds 30 days", su.SPEC_TTL),
        _c("cache.scope_missing", "must", "Result has no cacheScope", su.SPEC_CACHE_SCOPE),
        _c(
            "cache.scope_invalid",
            "must",
            'cacheScope is not "public" or "private"',
            su.SPEC_CACHE_SCOPE,
        ),
        _c(
            "cache.public_user_specific_names",
            "best-practice",
            "cacheScope public but item names look user-specific",
            su.SPEC_CACHE_SECURITY,
        ),
        _c(
            "cache.scope_inconsistent_pages",
            "must",
            "cacheScope differs across pages",
            su.SPEC_CACHE_PAGINATION,
        ),
        _c(
            "cache.cursor_not_string",
            "must",
            "nextCursor is not a string",
            su.SPEC_PAGINATION_GUIDELINES,
        ),
        _c("cache.cursor_loop", "should", "Repeated nextCursor", su.SPEC_PAGINATION_GUIDELINES),
        _c(
            "cache.item_malformed",
            "must",
            "A prompt/resource/template item does not match its schema (SDK validator)",
            su.SPEC_PROMPTS_CAPABILITIES,
        ),
        _c(
            "cache.method_not_implemented",
            "info",
            "List method answers -32601 (skipped)",
            su.SPEC_CACHEABLE_RESULTS,
        ),
        _c(
            "cache.resources_read_not_probed",
            "info",
            "resources/read hints not verified (read-only policy)",
            su.SPEC_CACHEABLE_RESULTS,
        ),
        # ------------------------------------------------------------- origin (§7.7)
        _c(
            "origin.evil_403_body_has_id",
            "info",
            "403 body is a JSON-RPC error carrying an id",
            su.SPEC_ORIGIN_FRAGMENT,
        ),
        _c(
            "origin.evil_accepted",
            "must",
            "Request with a foreign Origin was processed",
            su.SPEC_ORIGIN_FRAGMENT,
        ),
        _c(
            "origin.not_validated",
            "must",
            "Identical reply with and without the evil Origin",
            su.SPEC_ORIGIN_FRAGMENT,
        ),
        _c(
            "origin.evil_rejected_wrong_status",
            "must",
            "Foreign Origin rejected with a status other than 403",
            su.SPEC_ORIGIN_FRAGMENT,
        ),
        _c(
            "origin.evil_rejection_unconfirmed",
            "should",
            "2xx without a result for the evil Origin",
            su.SPEC_ORIGIN_FRAGMENT,
        ),
        _c(
            "origin.evil_redirected",
            "should",
            "Evil-Origin request redirected",
            su.SPEC_ORIGIN_FRAGMENT,
        ),
        _c(
            "origin.evil_connection_closed",
            "should",
            "Evil-Origin connection closed without a response",
            su.SPEC_ORIGIN_FRAGMENT,
        ),
        _c(
            "origin.evil_timeout",
            "should",
            "Evil-Origin request timed out",
            su.SPEC_ORIGIN_FRAGMENT,
        ),
        _c(
            "origin.same_origin_rejected",
            "best-practice",
            "Same-origin Origin rejected (pass --origin)",
            None,
        ),
        _c("origin.no_origin_rejected", "best-practice", "Requests without Origin rejected", None),
        _c("origin.null_accepted", "best-practice", '"Origin: null" accepted', None),
        _c(
            "origin.cors_preflight_permissive",
            "info",
            "CORS preflight allows the evil origin or *",
            None,
        ),
    ]
)

NAMES_WITH_CODES: frozenset[str] = frozenset(code.split(".", 1)[0] for code in FINDING_CODES)


def lookup(code: str) -> CodeInfo:
    """Return the registry entry for ``code`` or raise ``KeyError`` with a hint."""
    try:
        return FINDING_CODES[code]
    except KeyError:
        msg = f"finding code {code!r} is not registered in mcpcheck/checks/codes.py"
        raise KeyError(msg) from None


def register(code: str, severity: Severity, description: str, spec_url: str | None) -> str:
    """Register ``code`` from a check module at import time and return it.

    Use this for codes that are not pre-listed above. Re-registering an identical
    entry is a no-op (module reloads); a conflicting entry raises ``ValueError``.

    Raises:
        ValueError: the code violates the grammar or conflicts with an existing entry.
    """
    if not CODE_RE.match(code):
        msg = f"finding code {code!r} violates {CODE_PATTERN}"
        raise ValueError(msg)
    info = CodeInfo(severity, description, spec_url)
    existing = FINDING_CODES.get(code)
    if existing is not None and existing != info:
        msg = f"finding code {code!r} is already registered as {existing.severity!r}"
        raise ValueError(msg)
    FINDING_CODES[code] = info
    return code


def _validate_registry() -> None:
    for code in FINDING_CODES:
        if not CODE_RE.match(code):
            msg = f"finding code {code!r} violates {CODE_PATTERN}"
            raise ValueError(msg)


_validate_registry()

__all__ = [
    "CODE_RE",
    "FINDING_CODES",
    "NAMES_WITH_CODES",
    "CodeInfo",
    "Severity",
    "lookup",
    "register",
]
