"""Verified MCP 2026-07-28 specification URLs (PLAN.md Appendix B).

Every constant below answered HTTP 200 *without following a redirect* on
2026-10-16 (``tests/test_spec_urls.py::test_anchors_resolve_online`` re-checks
that so a moved page is caught). The security best practices moved out of the
specification tree: ``/specification/2026-07-28/basic/security_best_practices``
answers 308 to the ``/docs/`` page below, which is the canonical one. The four
dead handoff paths (``/transports/streamable-http``, ``/errors``, ``/cache``,
``/security#origin-validation``) are deliberately absent.
"""

from __future__ import annotations

SPEC_BASE = "https://modelcontextprotocol.io/specification/2026-07-28"
DOCS_BASE = "https://modelcontextprotocol.io/docs/2026-07-28"

# changelog
SPEC_CHANGELOG = f"{SPEC_BASE}/changelog"
SPEC_CHANGELOG_MAJOR = f"{SPEC_CHANGELOG}#major-changes"
SPEC_CHANGELOG_MINOR = f"{SPEC_CHANGELOG}#minor-changes"

# basic
SPEC_BASIC = f"{SPEC_BASE}/basic"
SPEC_STATELESSNESS = f"{SPEC_BASIC}#statelessness"
SPEC_RESULT_RESPONSES = f"{SPEC_BASIC}#result-responses"
SPEC_ERROR_RESPONSES = f"{SPEC_BASIC}#error-responses"
SPEC_ERROR_CODES = f"{SPEC_BASIC}#error-codes"
SPEC_META = f"{SPEC_BASIC}#_meta"
SPEC_SCHEMA_SECTION = f"{SPEC_BASIC}#schema"
SPEC_SCHEMA_DIALECT = f"{SPEC_BASIC}#schema-dialect"
SPEC_IMPL_REQUIREMENTS = f"{SPEC_BASIC}#implementation-requirements"
SPEC_SCHEMA_VALIDATION = f"{SPEC_BASIC}#schema-validation"
SPEC_REF_RESOLUTION = f"{SPEC_BASIC}#$ref-resolution"
SPEC_COMPOSITION_BOUNDS = f"{SPEC_BASIC}#composition-keyword-resource-use"
SPEC_ICONS = f"{SPEC_BASIC}#icons"

# versioning
SPEC_VERSIONING = f"{SPEC_BASE}/basic/versioning"
SPEC_VERSION_NEGOTIATION = f"{SPEC_VERSIONING}#protocol-version-negotiation"
SPEC_EXTENSION_NEGOTIATION = f"{SPEC_VERSIONING}#extension-negotiation"
SPEC_BACKCOMPAT_INIT = (
    f"{SPEC_VERSIONING}#backward-compatibility-with-initialization-based-versions"
)
SPEC_COMPAT_MATRIX = f"{SPEC_VERSIONING}#compatibility-matrix"
SPEC_VERSIONING_TERMINOLOGY = f"{SPEC_VERSIONING}#terminology"

# transports
SPEC_TRANSPORTS_UTF8 = f"{SPEC_BASE}/basic/transports"
SPEC_TRANSPORT = f"{SPEC_BASE}/basic/transports/streamable-http"
SPEC_ORIGIN_FRAGMENT = f"{SPEC_TRANSPORT}#security-&-endpoint"
SPEC_SENDING_MESSAGES = f"{SPEC_TRANSPORT}#sending-messages"
SPEC_RECEIVING_MESSAGES = f"{SPEC_TRANSPORT}#receiving-messages"
SPEC_CANCELLATION = f"{SPEC_TRANSPORT}#cancellation"
SPEC_PROTOCOL_VERSION_HEADER = f"{SPEC_TRANSPORT}#protocol-version-header"
SPEC_STANDARD_HEADERS = f"{SPEC_TRANSPORT}#standard-request-headers"
SPEC_VALUE_ENCODING = f"{SPEC_TRANSPORT}#value-encoding"
SPEC_CUSTOM_HEADERS = f"{SPEC_TRANSPORT}#custom-headers-from-tool-parameters"
SPEC_SCHEMA_EXTENSION = f"{SPEC_TRANSPORT}#schema-extension"
SPEC_PARAM_HEADERS = f"{SPEC_TRANSPORT}#server-behavior-for-custom-headers"
SPEC_SERVER_VALIDATION = f"{SPEC_TRANSPORT}#server-validation"
SPEC_BACKCOMPAT_HTTP = f"{SPEC_TRANSPORT}#backward-compatibility"
SPEC_EARLIER_REVISIONS = f"{SPEC_TRANSPORT}#earlier-streamable-http-revisions"

# authorization / security
SPEC_AUTH_ERRORS = f"{SPEC_BASE}/basic/authorization#error-handling"
SPEC_AUTH_TOKEN_HANDLING = f"{SPEC_BASE}/basic/authorization#token-handling"
SPEC_SECURITY_BEST_PRACTICES = f"{DOCS_BASE}/tutorials/security/security_best_practices"
SPEC_SECURITY_LOCAL = f"{SPEC_SECURITY_BEST_PRACTICES}#local-mcp-server-compromise"
SPEC_MRTR_SERVER_REQS = f"{SPEC_BASE}/basic/patterns/mrtr#server-requirements-basic-workflow"

# server features
SPEC_DISCOVER = f"{SPEC_BASE}/server/discover"
SPEC_DISCOVER_RESULT = f"{SPEC_DISCOVER}#discoverresult"
SPEC_DISCOVER_WHEN_TO_CALL = f"{SPEC_DISCOVER}#when-to-call"
SPEC_TOOLS = f"{SPEC_BASE}/server/tools"
SPEC_TOOLS_CAPABILITIES = f"{SPEC_TOOLS}#capabilities"
SPEC_TOOL = f"{SPEC_TOOLS}#tool"
SPEC_TOOL_NAMES = f"{SPEC_TOOLS}#tool-names"
SPEC_X_MCP_HEADER = f"{SPEC_TOOLS}#x-mcp-header"
SPEC_OUTPUT_SCHEMA = f"{SPEC_TOOLS}#output-schema"
SPEC_TOOL_ERRORS = f"{SPEC_TOOLS}#error-handling"
SPEC_TOOLS_SECURITY = f"{SPEC_TOOLS}#security-considerations"
SPEC_RESOURCES = f"{SPEC_BASE}/server/resources"
SPEC_RESOURCES_CAPABILITIES = f"{SPEC_RESOURCES}#capabilities"
SPEC_PROMPTS = f"{SPEC_BASE}/server/prompts"
SPEC_PROMPTS_CAPABILITIES = f"{SPEC_PROMPTS}#capabilities"

# utilities
SPEC_CACHING = f"{SPEC_BASE}/server/utilities/caching"
SPEC_CACHEABLE_RESULTS = f"{SPEC_CACHING}#cacheable-results"
SPEC_TTL = f"{SPEC_CACHING}#time-to-live-ttl-field"
SPEC_CACHE_SCOPE = f"{SPEC_CACHING}#cache-scope-field"
SPEC_CACHE_PAGINATION = f"{SPEC_CACHING}#interaction-with-pagination"
SPEC_CACHE_SECURITY = f"{SPEC_CACHING}#security-considerations"
SPEC_PAGINATION = f"{SPEC_BASE}/server/utilities/pagination"
SPEC_PAGINATION_GUIDELINES = f"{SPEC_PAGINATION}#implementation-guidelines"
SPEC_PAGINATION_ERRORS = f"{SPEC_PAGINATION}#error-handling"
SPEC_LOGGING = f"{SPEC_BASE}/server/utilities/logging"
SPEC_DEPRECATED = f"{SPEC_BASE}/deprecated#deprecated"

# schema reference
SPEC_SCHEMA_DISCOVER_RESULT = f"{SPEC_BASE}/schema#discoverresult"
SPEC_SCHEMA_UNSUPPORTED_VERSION = f"{SPEC_BASE}/schema#unsupportedprotocolversionerror"
SPEC_SCHEMA_IMPLEMENTATION = f"{SPEC_BASE}/schema#implementation"
SPEC_SCHEMA_SERVER_CAPABILITIES = f"{SPEC_BASE}/schema#servercapabilities"
SPEC_SCHEMA_RESULT_META = f"{SPEC_BASE}/schema#resultmetaobject"


def all_urls() -> dict[str, str]:
    """Return every ``SPEC_*`` constant of this module keyed by name."""
    return {name: value for name, value in globals().items() if name.startswith("SPEC_")}
