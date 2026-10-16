"""Deliberately misbehaving MCP server fixture (hand-rolled Starlette JSON-RPC, no lifespan).

The default :class:`Toggles` instance is **fully conformant** to MCP 2026-07-28 over
Streamable HTTP: this is the regression guard proving that toggles, not the baseline,
cause findings. Every toggle flips exactly one wire behaviour (PLAN.md section 10.3; the
per-check needs are in section 7, parts (8)/(9)).

Run it standalone from the repository root::

    uv run python -m tests.fixtures.misbehaving_server --port 0 --toggle omit_ttl --set cache_scope=user

Extras for assertions
    * ``X-Client-Port`` on every response (peer port, for fresh-connection tests);
    * ``GET /__log`` returns the request log as JSON, ``DELETE /__log`` clears it; the same
      list is reachable in-process as ``app.state.fixture.log``;
    * ``/mcp-redirected`` is a second, identical MCP endpoint used as the target of
      same-origin redirects (``status=307``); a 3xx ``status`` with a ``body`` uses the
      body text as the ``Location`` instead (cross-origin redirect tests).

Wire-level conventions of the conformant baseline
    * modern-only: a POST without ``MCP-Protocol-Version`` is rejected with ``400``/``-32020``
      (dual-era behaviour is opt-in via ``serve_headerless_as_legacy`` and the session toggles);
    * ``GET``/``DELETE``/``OPTIONS`` -> ``405`` with ``Allow: POST``;
    * foreign ``Origin`` -> ``403`` text ``Invalid Origin header`` without content-type (SDK shape);
    * unknown method -> ``404``/``-32601``; unknown cursor -> ``400``/``-32602``;
    * invalid tool arguments -> ``200`` ``CallToolResult`` with ``isError: true`` and an
      actionable message naming the tool and the offending property.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import anyio
import uvicorn
from jsonschema import Draft202012Validator
from mcp.shared.inbound import decode_header_value, mcp_param_headers, x_mcp_header_map
from starlette.applications import Starlette
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping, Sequence

    from starlette.requests import Request
    from starlette.types import Receive, Scope, Send

SERVER_VERSION = "0.1.0"
SERVER_INFO: dict[str, str] = {"name": "mcpcheck-misbehaving-server", "version": SERVER_VERSION}
PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSION = "2025-11-25"
HANDSHAKE_VERSIONS: tuple[str, ...] = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
MCP_PATH = "/mcp"
REDIRECT_PATH = "/mcp-redirected"
LOG_PATH = "/__log"
SESSION_ID = "6d63706368656b2d666978747572652d7331"
"""The one session id this fixture ever mints (deterministic hex)."""
EVIL_ORIGIN = "https://evil.example.com"
APP_ORIGIN = "https://app.example.com"
LOOPBACK_ORIGIN_PREFIXES: tuple[str, ...] = (
    "http://127.0.0.1:",
    "http://localhost:",
    "http://[::1]:",
)
LOOPBACK_ORIGINS: tuple[str, ...] = tuple(p.rstrip(":") for p in LOOPBACK_ORIGIN_PREFIXES)
INSTRUCTIONS = "Misbehaving fixture for mcpcheck. Behaviour is selected by Toggles."

META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"
SERVER_INFO_META: dict[str, Any] = {META_SERVER_INFO: SERVER_INFO}

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
HEADER_MISMATCH = -32020
MISSING_CLIENT_CAPABILITY = -32021
UNSUPPORTED_PROTOCOL_VERSION = -32022

DEFAULT_TTL_MS = 300_000
DISCOVER_TTL_MS = 3_600_000
HANG_CAP_SECONDS = 60.0
SSE_PING_SECONDS = 0.25

ErrorStyle = Literal["actionable", "traceback", "bare", "jsonrpc", "success"]

# --------------------------------------------------------------------------- tools

READ_ONLY_ANNOTATIONS: dict[str, bool] = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}


def _tool(
    name: str,
    description: str,
    input_schema: dict[str, Any],
    *,
    output_schema: dict[str, Any] | None = None,
    annotations: dict[str, bool] | None = READ_ONLY_ANNOTATIONS,
) -> dict[str, Any]:
    tool: dict[str, Any] = {"name": name, "description": description, "inputSchema": input_schema}
    if output_schema is not None:
        tool["outputSchema"] = output_schema
    if annotations is not None:
        tool["annotations"] = dict(annotations)
    return tool


Q_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"q": {"type": "string", "description": "Query text."}},
    "required": ["q"],
}
Q_OPTIONAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"q": {"type": "string", "description": "Query text."}},
}
TEXT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
}

DEFAULT_TOOLS: tuple[dict[str, Any], ...] = (
    _tool(
        "lookup_weather",
        "Return a canned weather report for a city (read-only, deterministic).",
        {
            "type": "object",
            "properties": {
                "city": {"type": "string", "title": "City"},
                "units": {"type": "string", "title": "Units", "default": "celsius"},
            },
            "required": ["city"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "city": {"type": "string"},
                "temperature_c": {"type": "number"},
                "conditions": {"type": "string"},
            },
            "required": ["city", "temperature_c", "conditions"],
        },
    ),
    _tool(
        "echo_required",
        "Echo a message a number of times (read-only). Both arguments are required.",
        {
            "type": "object",
            "properties": {
                "message": {"type": "string", "title": "Message"},
                "repeat": {"type": "integer", "title": "Repeat"},
            },
            "required": ["message", "repeat"],
        },
        output_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}, "repeat": {"type": "integer"}},
            "required": ["text", "repeat"],
        },
    ),
)
"""The conformant baseline listing (same two tools as the reference server)."""

USER_SPECIFIC_TOOL: dict[str, Any] = _tool(
    "list_my_orders",
    "List the orders of the calling user (read-only).",
    Q_OPTIONAL_SCHEMA,
    output_schema=TEXT_OUTPUT_SCHEMA,
)


def _catalogue_tool(name: str, schema: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
    return _tool(
        name,
        f"Error-shape catalogue tool {name}: the name selects the reply (read-only, never acts).",
        schema if schema is not None else Q_SCHEMA,
        output_schema=TEXT_OUTPUT_SCHEMA,
        **kw,
    )


HDR_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "q": {"type": "string", "description": "Query text."},
        "region": {"type": "string", "x-mcp-header": "Region", "description": "Routing region."},
    },
    "required": ["q"],
}

CATALOGUE_TOOLS: tuple[dict[str, Any], ...] = (
    _catalogue_tool("err_good"),
    _catalogue_tool("err_rpc_32602_200"),
    _catalogue_tool("err_rpc_32602_400"),
    _catalogue_tool("err_rpc_nullid_400"),
    _catalogue_tool("err_rpc_32000"),
    _catalogue_tool("err_rpc_32042"),
    _catalogue_tool("err_rpc_32050"),
    _catalogue_tool("err_rpc_32603"),
    _catalogue_tool("err_generic"),
    _catalogue_tool("err_traceback"),
    _catalogue_tool("err_empty_content"),
    _catalogue_tool("err_no_resulttype"),
    _catalogue_tool("err_structured"),
    _catalogue_tool("ok_garbage"),
    _catalogue_tool("input_required_state"),
    _catalogue_tool("input_required_requests"),
    _catalogue_tool("input_required_empty"),
    _catalogue_tool("http_500_html"),
    _catalogue_tool("http_403_text"),
    _catalogue_tool("ctype_text"),
    _catalogue_tool("slow"),
    _catalogue_tool("sse_iserror"),
    _catalogue_tool("sse_notification_message"),
    _catalogue_tool("hdr_tool", HDR_TOOL_SCHEMA),
    _catalogue_tool("destructive_norequired", Q_OPTIONAL_SCHEMA, annotations=None),
    _catalogue_tool("readonly_permissive", {"type": "object"}),
    _catalogue_tool("工具_unicode"),
    _catalogue_tool("wrongtype_ok", Q_OPTIONAL_SCHEMA),
)
"""The named tools of PLAN.md section 7.5 (9); enabled by ``error_shapes_catalogue``."""

CATALOGUE_TOOL_NAMES: tuple[str, ...] = tuple(t["name"] for t in CATALOGUE_TOOLS)

DEFAULT_PROMPTS: tuple[dict[str, Any], ...] = (
    {
        "name": "summarize",
        "description": "Ask the model to summarize some text.",
        "arguments": [{"name": "text", "required": True}],
    },
)
DEFAULT_RESOURCES: tuple[dict[str, Any], ...] = (
    {
        "uri": "demo://greeting",
        "name": "greeting",
        "description": "A static greeting.",
        "mimeType": "text/plain",
    },
)

TRACEBACK_TEXT = (
    "Traceback (most recent call last):\n"
    '  File "/srv/tools/handler.py", line 42, in run\n'
    "    raise ValueError(arguments)\n"
    "ValueError: {}"
)

# ------------------------------------------------------------------ discover variants

BASE_CAPABILITIES: dict[str, Any] = {
    "tools": {"listChanged": False},
    "prompts": {"listChanged": False},
    "resources": {"listChanged": False, "subscribe": False},
}


def _base_discover() -> dict[str, Any]:
    return {
        "resultType": "complete",
        "supportedVersions": [PROTOCOL_VERSION],
        "capabilities": json.loads(json.dumps(BASE_CAPABILITIES)),
        "instructions": INSTRUCTIONS,
        "ttlMs": DISCOVER_TTL_MS,
        "cacheScope": "public",
        "_meta": {META_SERVER_INFO: dict(SERVER_INFO)},
    }


def _caps(**overrides: Any) -> dict[str, Any]:
    caps: dict[str, Any] = json.loads(json.dumps(BASE_CAPABILITIES))
    caps.update(overrides)
    return caps


# name -> (keys to drop, keys to set)
DISCOVER_PAYLOAD_VARIANTS: dict[str, tuple[tuple[str, ...], dict[str, Any]]] = {
    # cache hints
    "no-hints": (("ttlMs", "cacheScope"), {}),
    "ttl-missing": (("ttlMs",), {}),
    "scope-missing": (("cacheScope",), {}),
    "neg-ttl": ((), {"ttlMs": -1}),
    "ttl-str": ((), {"ttlMs": "5"}),
    "ttl-bool": ((), {"ttlMs": True}),
    "ttl-fraction": ((), {"ttlMs": 5.5}),
    "ttl-float": ((), {"ttlMs": 60000.0}),
    "ttl-huge": ((), {"ttlMs": 10**12}),
    "scope-user": ((), {"cacheScope": "user"}),
    "scope-case": ((), {"cacheScope": "PUBLIC"}),
    # resultType
    "no-resulttype": (("resultType",), {}),
    "resulttype-unknown": ((), {"resultType": "partial"}),
    "resulttype-input-required": ((), {"resultType": "input_required"}),
    "resulttype-not-string": ((), {"resultType": 1}),
    # supportedVersions
    "versions-omit-2026": ((), {"supportedVersions": [LEGACY_PROTOCOL_VERSION]}),
    "versions-empty": ((), {"supportedVersions": []}),
    "versions-not-list": ((), {"supportedVersions": PROTOCOL_VERSION}),
    "versions-nonstring": ((), {"supportedVersions": [20260728]}),
    "versions-nondate": ((), {"supportedVersions": [PROTOCOL_VERSION, "next"]}),
    "versions-duplicate": ((), {"supportedVersions": [PROTOCOL_VERSION, PROTOCOL_VERSION]}),
    "versions-missing": (("supportedVersions",), {}),
    # serverInfo / _meta
    "serverinfo-absent": (("_meta",), {}),
    "serverinfo-malformed": (
        (),
        {"_meta": {META_SERVER_INFO: {"name": "mcpcheck-misbehaving-server"}}},
    ),
    "serverinfo-top-level": (
        (),
        {"serverInfo": dict(SERVER_INFO), "protocolVersion": PROTOCOL_VERSION},
    ),
    "serverinfo-bad-urls": (
        (),
        {
            "_meta": {
                META_SERVER_INFO: {
                    **SERVER_INFO,
                    "websiteUrl": "ftp://example.invalid/",
                    "icons": [{"src": "http://example.invalid/icon.png"}],
                }
            }
        },
    ),
    "meta-badkey": ((), {"_meta": {"bad key!": 1, **SERVER_INFO_META}}),
    "meta-not-object": ((), {"_meta": []}),
    "meta-reserved-unknown": (
        (),
        {"_meta": {"io.modelcontextprotocol/unknownThing": {}, **SERVER_INFO_META}},
    ),
    # capabilities
    "caps-missing": (("capabilities",), {}),
    "caps-not-object": ((), {"capabilities": "tools"}),
    "caps-null": ((), {"capabilities": _caps(tools=None)}),
    "caps-tools-true": ((), {"capabilities": _caps(tools=True)}),
    "caps-unknown-key": ((), {"capabilities": _caps(fancy={})}),
    "caps-logging": ((), {"capabilities": _caps(logging={})}),
    "caps-no-tools": (
        (),
        {"capabilities": {k: v for k, v in BASE_CAPABILITIES.items() if k != "tools"}},
    ),
    "extensions-bad-id": ((), {"capabilities": _caps(extensions={"bad": {}})}),
    "extensions-value-not-object": (
        (),
        {"capabilities": _caps(extensions={"io.example/ext": True})},
    ),
    "listchanged-not-boolean": ((), {"capabilities": _caps(tools={"listChanged": "yes"})}),
    # instructions / extras
    "instructions-not-string": ((), {"instructions": 42}),
    "instructions-huge": ((), {"instructions": "x" * (2 * 1024 * 1024)}),
    "nextcursor-present": ((), {"nextCursor": "page-2"}),
    "extra-unknown-key": ((), {"vendorExtra": 1}),
}
"""43 ``server/discover`` result payload variants (``discover_variant``): the plan's 42 plus ``caps-no-tools``."""

WIRE_ERROR_VARIANTS: frozenset[str] = frozenset(
    {
        "error-32020",
        "error-32021",
        "error-32022",
        "error-32022-contradiction",
        "error-32022-bad-data",
        "error-32022-wrong-status",
        "error-32602",
        "error-32042",
        "error-32000",
        "legacy-version-reject",
    }
)
"""Reply every modern request with one JSON-RPC error (also ``discover_variant``)."""

WIRE_ENVELOPE_VARIANTS: frozenset[str] = frozenset(
    {
        "wrong-id",
        "null-id",
        "jsonrpc-version-wrong",
        "result-and-error",
        "html-200",
        "json-no-content-type",
        "json-array",
        "status-202",
        "sse-notification-message",
        "sse-server-request",
        "sse-progress-then-result",
        "sse-two-responses",
        "sse-empty",
        "sse-not-terminated",
    }
)
"""Reshape the envelope/transport of every modern JSON-RPC result (also ``discover_variant``)."""

DISCOVER_VARIANTS: frozenset[str] = (
    frozenset(DISCOVER_PAYLOAD_VARIANTS) | WIRE_ERROR_VARIANTS | WIRE_ENVELOPE_VARIANTS
)

ORIGIN_MODES: tuple[str, ...] = (
    "sdk_like",
    "reject_400_jsonrpc",
    "reject_200_jsonrpc_error",
    "allowlist_app_origin",
    "require_origin",
    "accept_null",
    "accept_all",
    "accept_all_sse",
    "legacy_only",
    "redirect_evil",
    "reset_evil",
    "hang_evil",
    "cors_reflect",
)


# ----------------------------------------------------------------------- toggles


@dataclass(frozen=True)
class Toggles:
    """Wire-behaviour switches; the default instance is fully conformant.

    Field list exactly as PLAN.md section 10.3. ``tools``/``pages`` hold raw JSON
    dictionaries so tests can serve arbitrary listings; ``pages=()`` makes ``tools/list``
    answer ``404``/``-32601`` (not implemented).
    """

    # handshake / session
    require_initialize: bool = False
    legacy_shape: bool = False
    mint_session_id: bool = False
    require_session_id: bool = False
    echo_session_id: bool = False
    vary_tools_per_connection: bool = False
    vary_tools_per_request: bool = False
    nondeterministic_order: bool = False
    vary_description: bool = False
    # discover
    no_discover: bool = False
    discover_no_server_info: bool = False
    discover_wrong_versions: bool = False
    capability_mismatch: bool = False
    discover_variant: str | None = None
    # schemas
    tools: tuple[dict[str, Any], ...] = DEFAULT_TOOLS
    pages: tuple[dict[str, Any], ...] | None = None
    loop_cursor: bool = False
    prompts: tuple[dict[str, Any], ...] = DEFAULT_PROMPTS
    resources: tuple[dict[str, Any], ...] = DEFAULT_RESOURCES
    # errors
    error_style: ErrorStyle = "actionable"
    error_shapes_catalogue: bool = False
    # cache
    omit_ttl: bool = False
    negative_ttl: bool = False
    float_ttl: float | None = None
    omit_cache_scope: bool = False
    cache_scope: str = "public"
    scope_varies_across_pages: bool = False
    user_specific_public: bool = False
    # origin
    origin_mode: str = "sdk_like"
    # protocol
    ignore_version_mismatch: bool = False
    serve_headerless_as_legacy: bool = False
    unknown_method_status: int = 404
    legacy_error_codes: bool = False
    accept_missing_meta: bool = False
    ignore_cursor: bool = False
    ignore_mcp_name: bool = False
    # transport shape
    sse: bool = False
    slow_seconds: float = 0.0
    endless_sse: bool = False
    x_accel_buffering: bool = True
    status: int | None = None
    content_type: str | None = None
    body: bytes | None = None

    def __post_init__(self) -> None:
        if self.discover_variant is not None and self.discover_variant not in DISCOVER_VARIANTS:
            raise ValueError(f"unknown discover_variant {self.discover_variant!r}")
        if self.origin_mode not in ORIGIN_MODES:
            raise ValueError(f"unknown origin_mode {self.origin_mode!r}")

    @property
    def legacy_enabled(self) -> bool:
        """Whether header-less (legacy) requests are served instead of rejected."""
        return self.require_initialize or self.require_session_id or self.serve_headerless_as_legacy

    @property
    def force_sse(self) -> bool:
        """Whether every JSON-RPC reply is framed as ``text/event-stream``."""
        return self.sse or self.origin_mode == "accept_all_sse"


TOGGLE_NAMES: tuple[str, ...] = tuple(f.name for f in dataclasses.fields(Toggles))


def _json_tuple(raw: str) -> tuple[dict[str, Any], ...]:
    value = json.loads(raw)
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        raise ValueError("expected a JSON array of objects")
    return tuple(value)


def _optional(convert: Callable[[str], Any]) -> Callable[[str], Any]:
    def inner(raw: str) -> Any:
        return None if raw.lower() in {"none", "null"} else convert(raw)

    return inner


def _bool(raw: str) -> bool:
    lowered = raw.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"not a boolean: {raw!r}")


_COERCERS: dict[str, Callable[[str], Any]] = {
    "discover_variant": _optional(str),
    "tools": _json_tuple,
    "pages": _optional(_json_tuple),
    "prompts": _json_tuple,
    "resources": _json_tuple,
    "error_style": str,
    "float_ttl": _optional(float),
    "cache_scope": str,
    "origin_mode": str,
    "unknown_method_status": int,
    "slow_seconds": float,
    "status": _optional(int),
    "content_type": _optional(str),
    "body": _optional(lambda raw: raw.encode("utf-8")),
}


def toggles_from_cli(toggle_names: Sequence[str], assignments: Sequence[str]) -> Toggles:
    """Build :class:`Toggles` from ``--toggle NAME`` and ``--set key=value`` arguments.

    Args:
        toggle_names: Boolean fields to switch on.
        assignments: ``key=value`` pairs; non-boolean fields are coerced by type
            (``tools``/``pages`` take JSON arrays, ``none`` clears an optional field).

    Returns:
        The resulting toggles.

    Raises:
        ValueError: On an unknown field or an unparsable value.
    """
    values: dict[str, Any] = {}
    for name in toggle_names:
        if name not in TOGGLE_NAMES:
            raise ValueError(f"unknown toggle {name!r}")
        if name in _COERCERS:
            raise ValueError(f"{name!r} is not boolean; use --set {name}=VALUE")
        values[name] = True
    for assignment in assignments:
        key, sep, raw = assignment.partition("=")
        if not sep or key not in TOGGLE_NAMES:
            raise ValueError(f"expected FIELD=VALUE with a known field, got {assignment!r}")
        values[key] = _COERCERS.get(key, _bool)(raw)
    return Toggles(**values)


# ----------------------------------------------------------------------- helpers


def dumps(obj: Any) -> str:
    """Serialise compactly and deterministically (insertion order, non-ASCII kept)."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def sse_frame(message: Any, *, event: str = "message") -> str:
    """Render one SSE event carrying a JSON message."""
    return f"event: {event}\ndata: {dumps(message)}\n\n"


def rpc_error(rid: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    """Build a JSON-RPC error envelope."""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": error}


def rpc_result(rid: Any, result: Any) -> dict[str, Any]:
    """Build a JSON-RPC result envelope."""
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _first_schema_error(schema: Mapping[str, Any], arguments: Any) -> str | None:
    errors = sorted(
        Draft202012Validator(schema).iter_errors(arguments), key=lambda e: [str(p) for p in e.path]
    )
    if not errors:
        return None
    first = errors[0]
    where = ".".join(str(p) for p in first.path)
    return f"{where}: {first.message}" if where else first.message


class _AbortResponse(Response):
    """Announce a body and then close the connection without sending it (TCP only)."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json"), (b"content-length", b"100")],
            }
        )


@dataclass
class FixtureState:
    """Mutable per-app state: request log and the counters toggles depend on."""

    toggles: Toggles
    log: list[dict[str, Any]] = field(default_factory=list)
    request_count: int = 0
    list_count: int = 0
    connections: dict[int, int] = field(default_factory=dict)
    sessions: set[str] = field(default_factory=set)

    def clear(self) -> None:
        """Forget the request log (counters are kept)."""
        self.log.clear()

    def connection_index(self, port: int) -> int:
        """Return a stable 0-based index for a client port (0 = first connection seen)."""
        return self.connections.setdefault(port, len(self.connections))


# ------------------------------------------------------------------------ server


class MisbehavingServer:
    """Request handler holding the toggles and state of one fixture app."""

    def __init__(self, toggles: Toggles) -> None:
        self.toggles = toggles
        self.state = FixtureState(toggles)

    # -- public entry points -------------------------------------------------

    async def handle(self, request: Request) -> Response:
        """Serve one request on the MCP endpoint (any verb)."""
        self.state.request_count += 1
        port = request.client.port if request.client else 0
        body = await request.body()
        entry = self._log(request, body, port)
        response = await self._dispatch(request, body, port, entry)
        if not isinstance(response, _AbortResponse):
            response.raw_headers.append((b"X-Client-Port", str(port).encode("ascii")))
        return response

    async def handle_log(self, request: Request) -> Response:
        """``GET /__log`` returns the request log; ``DELETE /__log`` clears it."""
        if request.method == "DELETE":
            self.state.clear()
            return Response(status_code=204)
        return Response(dumps(self.state.log), media_type="application/json")

    # -- dispatch --------------------------------------------------------------

    def _log(self, request: Request, body: bytes, port: int) -> dict[str, Any]:
        parsed: Any = None
        try:
            parsed = json.loads(body) if body else None
        except ValueError:
            parsed = None
        entry: dict[str, Any] = {
            "n": self.state.request_count,
            "verb": request.method,
            "path": request.url.path,
            "headers": dict(request.headers.items()),
            "client_port": port,
            "body": parsed,
            "method": parsed.get("method") if isinstance(parsed, dict) else None,
            "time": time.time(),
        }
        self.state.log.append(entry)
        return entry

    async def _dispatch(
        self, request: Request, body: bytes, port: int, entry: dict[str, Any]
    ) -> Response:
        t = self.toggles
        if t.status is not None or t.content_type is not None or t.body is not None:
            return self._raw_override()
        if request.method == "OPTIONS":
            return self._preflight(request)
        if request.method != "POST":
            return Response(status_code=405, headers={"Allow": "POST"})

        parsed: Any = entry["body"]
        rid = parsed.get("id") if isinstance(parsed, dict) else None
        origin_reply = await self._origin_gate(request, rid)
        if origin_reply is not None:
            return origin_reply
        if t.origin_mode == "legacy_only":
            return await self._legacy_only(request, parsed)

        if body and parsed is None:
            return self._reply(rpc_error(None, PARSE_ERROR, "Parse error"), status=400)
        if not isinstance(parsed, dict):
            return self._reply(rpc_error(None, INVALID_REQUEST, "Invalid Request"), status=400)

        pv_header = request.headers.get("mcp-protocol-version")
        if pv_header is None:
            return await self._legacy(request, parsed)
        return await self._modern(request, parsed, pv_header, port)

    # -- raw overrides / preflight / origin --------------------------------------

    def _raw_override(self) -> Response:
        t = self.toggles
        status = t.status if t.status is not None else 200
        headers: dict[str, str] = {}
        if 300 <= status < 400:
            headers["Location"] = (
                t.body.decode("utf-8", "replace") if t.body is not None else REDIRECT_PATH
            )
            return Response(status_code=status, headers=headers)
        return Response(
            content=t.body if t.body is not None else b"",
            status_code=status,
            media_type=t.content_type,
        )

    def _preflight(self, request: Request) -> Response:
        origin = request.headers.get("origin")
        if self.toggles.origin_mode == "cors_reflect" and origin is not None:
            return Response(
                status_code=204,
                headers={
                    "Access-Control-Allow-Origin": origin,
                    "Access-Control-Allow-Credentials": "true",
                    "Access-Control-Allow-Methods": "POST",
                    "Access-Control-Allow-Headers": request.headers.get(
                        "access-control-request-headers", "content-type"
                    ),
                },
            )
        return Response(status_code=405, headers={"Allow": "POST"})

    @staticmethod
    def _origin_allowed(origin: str) -> bool:
        # Browsers omit the default port, so accept both ``http://127.0.0.1`` and ``…:port``.
        return origin.startswith(LOOPBACK_ORIGIN_PREFIXES) or origin in LOOPBACK_ORIGINS

    async def _origin_gate(self, request: Request, rid: Any) -> Response | None:
        """Return a rejection for the request's Origin, or None when it may proceed."""
        mode = self.toggles.origin_mode
        origin = request.headers.get("origin")
        if mode in {"accept_all", "accept_all_sse", "legacy_only"}:
            return None
        if origin is None:
            if mode == "require_origin":
                return self._forbidden()
            return None
        if mode == "allowlist_app_origin":
            return None if origin == APP_ORIGIN else self._forbidden()
        if mode == "accept_null" and origin == "null":
            return None
        if self._origin_allowed(origin):
            return None
        # Foreign (or "null") Origin from here on.
        if mode == "reject_400_jsonrpc":
            return self._reply(
                rpc_error(rid, INVALID_REQUEST, f"Origin not allowed: {origin}"), status=400
            )
        if mode == "reject_200_jsonrpc_error":
            return self._reply(rpc_error(rid, INVALID_REQUEST, f"Origin not allowed: {origin}"))
        if mode == "redirect_evil":
            return Response(status_code=302, headers={"Location": REDIRECT_PATH})
        if mode == "reset_evil":
            return _AbortResponse()
        if mode == "hang_evil":
            await self._sleep_or_disconnect(request, HANG_CAP_SECONDS)
            return Response(status_code=204)
        return self._forbidden()

    @staticmethod
    def _forbidden() -> Response:
        # SDK shape: text body, no content-type.
        response = Response(content=b"Invalid Origin header", status_code=403)
        response.raw_headers = [(b"content-length", b"21")]
        return response

    # -- legacy era --------------------------------------------------------------

    def _legacy_initialize_result(self) -> dict[str, Any]:
        return {
            "protocolVersion": LEGACY_PROTOCOL_VERSION,
            "capabilities": {"tools": {}, "prompts": {}, "resources": {}},
            "serverInfo": dict(SERVER_INFO),
        }

    async def _legacy_only(self, request: Request, parsed: Any) -> Response:
        rid = parsed.get("id") if isinstance(parsed, dict) else None
        method = parsed.get("method") if isinstance(parsed, dict) else None
        if method == "initialize" and request.headers.get("mcp-protocol-version") is None:
            return self._reply(rpc_result(rid, self._legacy_initialize_result()), legacy=True)
        return self._reply(
            rpc_error(rid, METHOD_NOT_FOUND, "Method not found", method), status=404, legacy=True
        )

    async def _legacy(self, request: Request, parsed: dict[str, Any]) -> Response:
        """Serve a header-less request: rejected unless legacy serving is enabled."""
        t = self.toggles
        rid = parsed.get("id")
        method = parsed.get("method")
        if not t.legacy_enabled:
            return self._reply(
                rpc_error(rid, HEADER_MISMATCH, "mcp-protocol-version header is missing"),
                status=400,
            )
        session = request.headers.get("mcp-session-id")
        if method == "initialize":
            if t.require_initialize or t.require_session_id or t.mint_session_id:
                self.state.sessions.add(SESSION_ID)
                return self._reply(
                    rpc_result(rid, self._legacy_initialize_result()),
                    legacy=True,
                    session_id=SESSION_ID,
                )
            return self._reply(rpc_result(rid, self._legacy_initialize_result()), legacy=True)
        if method == "notifications/initialized":
            return Response(status_code=202)
        if (t.require_initialize or t.require_session_id) and session not in self.state.sessions:
            return self._session_rejection(rid, session)
        result = self._list_result(method, parsed.get("params"), legacy=True)
        if result is None:
            return self._reply(
                rpc_error(rid, METHOD_NOT_FOUND, "Method not found", method),
                status=404,
                legacy=True,
            )
        return self._reply(rpc_result(rid, result), legacy=True, session_id=session)

    def _session_rejection(self, rid: Any, session: str | None) -> Response:
        # mcp 1.x shape: id "server-error" when the server never dispatched the request.
        error_id: Any = "server-error" if self.toggles.require_initialize else rid
        if session is None:
            return self._reply(
                rpc_error(error_id, INVALID_REQUEST, "Bad Request: Missing session ID"), status=400
            )
        return self._reply(rpc_error(error_id, INVALID_REQUEST, "Session not found"), status=404)

    # -- modern era --------------------------------------------------------------

    async def _modern(
        self, request: Request, parsed: dict[str, Any], pv_header: str, port: int
    ) -> Response:
        t = self.toggles
        rid = parsed.get("id")
        method = parsed.get("method")
        raw_params = parsed.get("params")
        params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
        raw_meta = params.get("_meta")
        meta: dict[str, Any] | None = raw_meta if isinstance(raw_meta, dict) else None

        # Session gating (statefulness bugs).
        session = request.headers.get("mcp-session-id")
        if (t.require_initialize or t.require_session_id) and session not in self.state.sessions:
            return self._session_rejection(rid, session)

        # Header/body validation.
        meta_pv = meta.get(META_PROTOCOL_VERSION) if meta is not None else None
        if meta_pv is not None and meta_pv != pv_header and not t.ignore_version_mismatch:
            return self._reply(
                rpc_error(
                    rid,
                    HEADER_MISMATCH,
                    "mcp-protocol-version header does not match the request envelope's "
                    "protocol version",
                ),
                status=400,
            )
        requested = meta_pv if (t.ignore_version_mismatch and meta_pv is not None) else pv_header
        if requested != PROTOCOL_VERSION:
            return self._reply(
                rpc_error(
                    rid,
                    UNSUPPORTED_PROTOCOL_VERSION,
                    "Unsupported protocol version",
                    {"supported": [PROTOCOL_VERSION], "requested": requested},
                ),
                status=400,
            )
        if not t.accept_missing_meta:
            missing = [
                k
                for k in (META_PROTOCOL_VERSION, META_CLIENT_CAPABILITIES)
                if meta is None or k not in meta
            ]
            if missing:
                return self._reply(
                    rpc_error(
                        rid,
                        INVALID_PARAMS,
                        "params._meta is missing the required envelope key(s): "
                        + ", ".join(missing),
                    ),
                    status=400,
                )
        if request.headers.get("mcp-method") != method:
            return self._reply(
                rpc_error(
                    rid,
                    HEADER_MISMATCH,
                    "mcp-method header does not match the request body's method",
                ),
                status=400,
            )
        if (
            method == "tools/call"
            and not t.ignore_mcp_name
            and decode_header_value(request.headers.get("mcp-name")) != params.get("name")
        ):
            return self._reply(
                rpc_error(
                    rid,
                    HEADER_MISMATCH,
                    "mcp-name header does not match the request body's params.name",
                ),
                status=400,
            )

        wire_error = self._wire_error_variant(rid)
        if wire_error is not None:
            return wire_error

        if t.slow_seconds > 0 and await self._sleep_or_disconnect(request, t.slow_seconds):
            return Response(status_code=204)
        if t.endless_sse:
            return self._endless_sse()

        if method == "server/discover":
            return self._discover(rid, session)
        if method == "tools/call":
            return await self._tools_call(request, rid, params, session)
        # require_initialize models an mcp 1.x server: behind its session every reply is 2025-shaped.
        legacy_shaped = t.legacy_shape or t.require_initialize
        result = self._list_result(method, params, legacy=legacy_shaped, port=port)
        if result is None:
            return self._not_found(rid, method, session)
        return self._reply(rpc_result(rid, result), session_id=session)

    def _not_found(self, rid: Any, method: Any, session: str | None = None) -> Response:
        code = -32002 if self.toggles.legacy_error_codes else METHOD_NOT_FOUND
        return self._reply(
            rpc_error(rid, code, "Method not found", method),
            status=self.toggles.unknown_method_status,
            session_id=session,
        )

    def _invalid_params(self, rid: Any, message: str, *, status: int = 400) -> Response:
        code = -32042 if self.toggles.legacy_error_codes else INVALID_PARAMS
        return self._reply(rpc_error(rid, code, message), status=status)

    # -- discover ----------------------------------------------------------------

    def _discover(self, rid: Any, session: str | None) -> Response:
        t = self.toggles
        if t.no_discover:
            return self._not_found(rid, "server/discover", session)
        result = _base_discover()
        if t.discover_variant in DISCOVER_PAYLOAD_VARIANTS:
            drop, overrides = DISCOVER_PAYLOAD_VARIANTS[t.discover_variant]
            for key in drop:
                result.pop(key, None)
            result.update(json.loads(dumps(overrides)))
        if t.discover_no_server_info:
            result.pop("_meta", None)
        if t.discover_wrong_versions:
            result["supportedVersions"] = [LEGACY_PROTOCOL_VERSION]
        if t.capability_mismatch and isinstance(result.get("capabilities"), dict):
            result["capabilities"].pop("tools", None)
        return self._reply(rpc_result(rid, result), session_id=session)

    def _wire_error_variant(self, rid: Any) -> Response | None:
        variant = self.toggles.discover_variant
        if variant is None or variant not in WIRE_ERROR_VARIANTS:
            return None
        if variant == "error-32020":
            return self._reply(
                rpc_error(rid, HEADER_MISMATCH, "Header mismatch: mcp-method 'tools/list'"),
                status=400,
            )
        if variant == "error-32021":
            return self._reply(
                rpc_error(
                    rid,
                    MISSING_CLIENT_CAPABILITY,
                    "Missing required client capability",
                    {"requiredCapabilities": ["sampling"]},
                ),
                status=400,
            )
        if variant.startswith("error-32022"):
            data: Any = {"supported": ["2027-01-01"], "requested": PROTOCOL_VERSION}
            status = 400
            if variant == "error-32022-contradiction":
                data = {
                    "supported": ["2027-01-01", PROTOCOL_VERSION],
                    "requested": PROTOCOL_VERSION,
                }
            elif variant == "error-32022-bad-data":
                data = "nope"
            elif variant == "error-32022-wrong-status":
                status = 200
            return self._reply(
                rpc_error(rid, UNSUPPORTED_PROTOCOL_VERSION, "Unsupported protocol version", data),
                status=status,
            )
        if variant == "error-32602":
            return self._reply(rpc_error(rid, INVALID_PARAMS, "Invalid params"), status=400)
        if variant == "error-32042":
            return self._reply(rpc_error(rid, -32042, "Resource not found"), status=400)
        if variant == "error-32000":
            return self._reply(rpc_error(rid, -32000, "Server error"), status=400)
        # legacy-version-reject: an mcp 1.x stateless server rejecting the version
        return self._reply(
            rpc_error(
                rid,
                INVALID_REQUEST,
                "Bad Request: Unsupported protocol version (supported versions: "
                "2024-11-05, 2025-03-26, 2025-06-18, 2025-11-25)",
            ),
            status=400,
        )

    # -- lists -------------------------------------------------------------------

    def _active_tools(self) -> list[dict[str, Any]]:
        tools = list(self.toggles.tools)
        if self.toggles.error_shapes_catalogue:
            tools.extend(CATALOGUE_TOOLS)
        if self.toggles.user_specific_public:
            tools.append(USER_SPECIFIC_TOOL)
        return [json.loads(dumps(t)) for t in tools]

    def _varied_tools(self, port: int) -> list[dict[str, Any]]:
        t = self.toggles
        n = self.state.list_count
        tools = self._active_tools()
        if t.vary_tools_per_connection:
            idx = self.state.connection_index(port)
            if idx > 0:
                tools.append(
                    _tool(f"tool_conn_{idx}", "Appears on later connections only.", Q_SCHEMA)
                )
        if t.vary_tools_per_request:
            tools.append(_tool(f"tool_req_{n}", "Appears on this request only.", Q_SCHEMA))
        if t.nondeterministic_order and n % 2 == 1:
            tools.reverse()
        if t.vary_description and tools:
            revision = "A" if n % 2 == 0 else "B"
            tools[0]["description"] = f"{tools[0]['description']} (revision {revision})"
        return tools

    def _cache_hints(self, page: int) -> dict[str, Any]:
        t = self.toggles
        hints: dict[str, Any] = {}
        if not t.omit_ttl:
            ttl: Any = DEFAULT_TTL_MS
            if t.negative_ttl:
                ttl = -1
            elif t.float_ttl is not None:
                ttl = t.float_ttl
            hints["ttlMs"] = ttl
        if not t.omit_cache_scope:
            scope = t.cache_scope
            if t.scope_varies_across_pages and page > 0:
                scope = "private" if scope == "public" else "public"
            hints["cacheScope"] = scope
        return hints

    def _shape(
        self,
        key: str,
        items: list[dict[str, Any]],
        *,
        page: int,
        legacy: bool,
        next_cursor: str | None,
    ) -> dict[str, Any]:
        if legacy:
            result: dict[str, Any] = {key: items}
        else:
            result = {"resultType": "complete", key: items, **self._cache_hints(page)}
            result["_meta"] = {META_SERVER_INFO: dict(SERVER_INFO)}
        if next_cursor is not None:
            result["nextCursor"] = next_cursor
        return result

    def _list_result(
        self, method: Any, params: Any, *, legacy: bool, port: int = 0
    ) -> dict[str, Any] | None:
        """Return the result of a list method, or None when the method is unknown."""
        if method == "tools/list":
            result = self._tools_list(
                params if isinstance(params, dict) else {}, legacy=legacy, port=port
            )
            self.state.list_count += 1
            return result
        if method == "prompts/list":
            return self._shape(
                "prompts",
                [json.loads(dumps(p)) for p in self.toggles.prompts],
                page=0,
                legacy=legacy,
                next_cursor=None,
            )
        if method == "resources/list":
            return self._shape(
                "resources",
                [json.loads(dumps(r)) for r in self.toggles.resources],
                page=0,
                legacy=legacy,
                next_cursor=None,
            )
        if method == "resources/templates/list":
            return self._shape("resourceTemplates", [], page=0, legacy=legacy, next_cursor=None)
        return None

    def _tools_list(self, params: dict[str, Any], *, legacy: bool, port: int) -> dict[str, Any]:
        t = self.toggles
        cursor = params.get("cursor")
        if t.pages is not None:
            if not t.pages:
                return {"__not_implemented__": True}
            index = 0
            if cursor is not None and not t.ignore_cursor:
                matches = [
                    i + 1 for i, p in enumerate(t.pages[:-1]) if p.get("nextCursor") == cursor
                ]
                if not matches:
                    return {"__invalid_cursor__": True}
                index = matches[0]
            page: dict[str, Any] = json.loads(dumps(t.pages[index]))
            return page
        tools = self._varied_tools(port)
        if t.loop_cursor:
            return self._shape("tools", tools, page=0, legacy=legacy, next_cursor="c1")
        if t.scope_varies_across_pages:
            pages = [[tool] for tool in tools] or [[]]
            index = 0
            if cursor is not None and not t.ignore_cursor:
                if (
                    not cursor.startswith("c")
                    or not cursor[1:].isdigit()
                    or int(cursor[1:]) >= len(pages)
                ):
                    return {"__invalid_cursor__": True}
                index = int(cursor[1:])
            nxt = f"c{index + 1}" if index + 1 < len(pages) else None
            return self._shape("tools", pages[index], page=index, legacy=legacy, next_cursor=nxt)
        if cursor is not None and not t.ignore_cursor:
            return {"__invalid_cursor__": True}
        return self._shape("tools", tools, page=0, legacy=legacy, next_cursor=None)

    # -- tools/call --------------------------------------------------------------

    async def _tools_call(
        self, request: Request, rid: Any, params: dict[str, Any], session: str | None
    ) -> Response:
        t = self.toggles
        name = params.get("name")
        raw_arguments = params.get("arguments")
        arguments: dict[str, Any] = raw_arguments if isinstance(raw_arguments, dict) else {}
        tool = next((x for x in self._active_tools() if x["name"] == name), None)
        if tool is None:
            if t.error_style == "success":
                return self._reply(rpc_result(rid, self._success_result("ok")), session_id=session)
            return self._reply(
                rpc_result(rid, self._is_error_result(f"Unknown tool: {name}")), session_id=session
            )
        schema = tool["inputSchema"]
        expected_headers = mcp_param_headers(x_mcp_header_map(schema), arguments)
        for header, value in expected_headers.items():
            if decode_header_value(request.headers.get(header)) != value:
                return self._reply(
                    rpc_error(
                        rid,
                        HEADER_MISMATCH,
                        f"{header} header does not match the request body's arguments",
                    ),
                    status=400,
                )
        problem = _first_schema_error(schema, arguments)
        if t.error_shapes_catalogue and name in CATALOGUE_TOOL_NAMES:
            return await self._catalogue_reply(request, rid, str(name), arguments, problem)
        if problem is None:
            return self._reply(
                rpc_result(rid, self._success_result(f"{name} executed")), session_id=session
            )
        return self._invalid_arguments(rid, str(name), problem, session)

    def _invalid_arguments(
        self, rid: Any, name: str, problem: str, session: str | None = None
    ) -> Response:
        style = self.toggles.error_style
        if style == "jsonrpc":
            return self._invalid_params(rid, f"Invalid params for tool '{name}': {problem}")
        if style == "success":
            return self._reply(rpc_result(rid, self._success_result("ok")), session_id=session)
        if style == "traceback":
            text = TRACEBACK_TEXT
        elif style == "bare":
            text = "error"
        else:
            text = self._actionable(name, problem)
        return self._reply(rpc_result(rid, self._is_error_result(text)), session_id=session)

    @staticmethod
    def _actionable(name: str, problem: str) -> str:
        return f"Invalid arguments for tool '{name}': {problem}"

    @staticmethod
    def _is_error_result(text: str, **extra: Any) -> dict[str, Any]:
        result: dict[str, Any] = {
            "resultType": "complete",
            "content": [{"type": "text", "text": text}],
            "isError": True,
        }
        result.update(extra)
        return result

    @staticmethod
    def _success_result(text: str) -> dict[str, Any]:
        return {
            "resultType": "complete",
            "content": [{"type": "text", "text": text}],
            "structuredContent": {"text": text},
            "isError": False,
        }

    async def _catalogue_reply(
        self, request: Request, rid: Any, name: str, arguments: dict[str, Any], problem: str | None
    ) -> Response:
        """Reply for a catalogue tool; the tool name selects the shape."""
        actionable = self._actionable(name, problem or "'q' is a required property")
        if name in {
            "err_good",
            "hdr_tool",
            "destructive_norequired",
            "readonly_permissive",
            "工具_unicode",
        }:
            if problem is None:
                return self._reply(rpc_result(rid, self._success_result(f"{name} executed")))
            return self._reply(rpc_result(rid, self._is_error_result(actionable)))
        if name == "wrongtype_ok":
            # Lenient coercion: any argument shape is "executed".
            return self._reply(rpc_result(rid, self._success_result("coerced")))
        if name == "err_rpc_32602_200":
            return self._reply(rpc_error(rid, INVALID_PARAMS, actionable))
        if name == "err_rpc_32602_400":
            return self._reply(rpc_error(rid, INVALID_PARAMS, actionable), status=400)
        if name == "err_rpc_nullid_400":
            return self._reply(rpc_error(None, INVALID_PARAMS, actionable), status=400)
        if name == "err_rpc_32000":
            return self._reply(rpc_error(rid, -32000, actionable))
        if name == "err_rpc_32042":
            return self._reply(rpc_error(rid, -32042, actionable))
        if name == "err_rpc_32050":
            return self._reply(rpc_error(rid, -32050, actionable))
        if name == "err_rpc_32603":
            return self._reply(rpc_error(rid, INTERNAL_ERROR, "Internal error"))
        if name == "err_generic":
            return self._reply(rpc_result(rid, self._is_error_result("error")))
        if name == "err_traceback":
            return self._reply(rpc_result(rid, self._is_error_result(TRACEBACK_TEXT)))
        if name == "err_empty_content":
            return self._reply(
                rpc_result(rid, {"resultType": "complete", "content": [], "isError": True})
            )
        if name == "err_no_resulttype":
            return self._reply(
                rpc_result(
                    rid, {"content": [{"type": "text", "text": actionable}], "isError": True}
                )
            )
        if name == "err_structured":
            return self._reply(
                rpc_result(
                    rid,
                    self._is_error_result(
                        actionable,
                        structuredContent={"error": "invalid_arguments", "property": "q"},
                    ),
                )
            )
        if name == "ok_garbage":
            return self._reply(rpc_result(rid, self._success_result("Report queued")))
        if name == "input_required_state":
            return self._reply(
                rpc_result(rid, {"resultType": "input_required", "requestState": {"token": "abc"}})
            )
        if name == "input_required_requests":
            return self._reply(
                rpc_result(
                    rid,
                    {
                        "resultType": "input_required",
                        "inputRequests": [
                            {
                                "id": "elicit-1",
                                "method": "elicitation/create",
                                "params": {"message": "Confirm?"},
                            }
                        ],
                    },
                )
            )
        if name == "input_required_empty":
            return self._reply(rpc_result(rid, {"resultType": "input_required"}))
        if name == "http_500_html":
            return Response(
                "<html><body>Internal Server Error</body></html>",
                status_code=500,
                media_type="text/html",
            )
        if name == "http_403_text":
            return Response("forbidden", status_code=403, media_type="text/plain")
        if name == "ctype_text":
            return Response(
                dumps(rpc_result(rid, self._is_error_result(actionable))), media_type="text/plain"
            )
        if name == "slow":
            if await self._sleep_or_disconnect(request, 5.0):
                return Response(status_code=204)
            return self._reply(rpc_result(rid, self._is_error_result(actionable)))
        if name == "sse_iserror":
            return self._sse_response([rpc_result(rid, self._is_error_result(actionable))])
        # sse_notification_message
        notification = {
            "jsonrpc": "2.0",
            "method": "notifications/message",
            "params": {"level": "info", "data": "validating arguments"},
        }
        return self._sse_response(
            [notification, rpc_result(rid, self._is_error_result(actionable))]
        )

    # -- rendering ---------------------------------------------------------------

    def _reply(
        self,
        message: dict[str, Any],
        *,
        status: int = 200,
        legacy: bool = False,
        session_id: str | None = None,
    ) -> Response:
        """Render one JSON-RPC message as JSON or SSE, applying envelope variants."""
        t = self.toggles
        if isinstance(message.get("result"), dict):
            marker = message["result"]
            if marker.get("__not_implemented__"):
                return self._not_found(message["id"], "tools/list", session_id)
            if marker.get("__invalid_cursor__"):
                return self._invalid_params(message["id"], "Invalid cursor")
        variant = t.discover_variant if (not legacy and "result" in message) else None
        if variant in WIRE_ENVELOPE_VARIANTS:
            response = self._envelope_variant(str(variant), message)
        elif t.force_sse:
            response = self._sse_response([message], status=status)
        else:
            response = Response(dumps(message), status_code=status, media_type="application/json")
        self._session_headers(response, session_id)
        return response

    def _session_headers(self, response: Response, session_id: str | None) -> None:
        t = self.toggles
        value: str | None = None
        if t.mint_session_id:
            value = SESSION_ID
        elif session_id is not None and (
            t.echo_session_id or t.require_initialize or t.require_session_id
        ):
            value = session_id
        if value is not None:
            response.raw_headers.append((b"Mcp-Session-Id", value.encode("ascii")))

    def _envelope_variant(self, variant: str, message: dict[str, Any]) -> Response:
        rid = message.get("id")
        if variant == "wrong-id":
            return self._json_or_sse({**message, "id": "other"})
        if variant == "null-id":
            return self._json_or_sse({**message, "id": None})
        if variant == "jsonrpc-version-wrong":
            return self._json_or_sse({**message, "jsonrpc": "1.0"})
        if variant == "result-and-error":
            return self._json_or_sse(
                {**message, "error": {"code": -32603, "message": "also failed"}}
            )
        if variant == "html-200":
            return Response("<html><body>MCP endpoint</body></html>", media_type="text/html")
        if variant == "json-no-content-type":
            response = Response(dumps(message))
            response.raw_headers = [h for h in response.raw_headers if h[0] != b"content-type"]
            return response
        if variant == "json-array":
            return Response(dumps([message]), media_type="application/json")
        if variant == "status-202":
            return Response(status_code=202)
        if variant == "sse-notification-message":
            note = {
                "jsonrpc": "2.0",
                "method": "notifications/message",
                "params": {"level": "info", "data": "hello"},
            }
            return self._sse_response([note, message])
        if variant == "sse-server-request":
            server_request = {"jsonrpc": "2.0", "id": "srv-1", "method": "roots/list"}
            return self._sse_response([server_request, message])
        if variant == "sse-progress-then-result":
            progress = {
                "jsonrpc": "2.0",
                "method": "notifications/progress",
                "params": {"progressToken": "p1", "progress": 1, "total": 1},
            }
            return self._sse_response([progress, message])
        if variant == "sse-two-responses":
            return self._sse_response([message, {**message, "id": rid}])
        if variant == "sse-empty":
            return self._sse_response([], prefix=": no response follows\n\n")
        # sse-not-terminated
        return self._sse_stream(self._lingering(message))

    def _json_or_sse(self, message: dict[str, Any]) -> Response:
        if self.toggles.force_sse:
            return self._sse_response([message])
        return Response(dumps(message), media_type="application/json")

    def _sse_headers(self) -> dict[str, str]:
        headers = {"Cache-Control": "no-cache"}
        if self.toggles.x_accel_buffering:
            headers["X-Accel-Buffering"] = "no"
        return headers

    def _sse_response(
        self, messages: list[dict[str, Any]], *, status: int = 200, prefix: str = ""
    ) -> Response:
        body = prefix + "".join(sse_frame(m) for m in messages)
        return Response(
            body, status_code=status, media_type="text/event-stream", headers=self._sse_headers()
        )

    def _sse_stream(self, chunks: AsyncIterator[str]) -> StreamingResponse:
        return StreamingResponse(
            chunks, media_type="text/event-stream", headers=self._sse_headers()
        )

    async def _lingering(self, message: dict[str, Any]) -> AsyncIterator[str]:
        yield sse_frame(message)
        deadline = time.monotonic() + HANG_CAP_SECONDS
        while time.monotonic() < deadline:
            await anyio.sleep(SSE_PING_SECONDS)
            yield ": ping\n\n"

    async def _pings(self) -> AsyncIterator[str]:
        deadline = time.monotonic() + HANG_CAP_SECONDS
        while time.monotonic() < deadline:
            yield ": ping\n\n"
            await anyio.sleep(SSE_PING_SECONDS)

    def _endless_sse(self) -> Response:
        return self._sse_stream(self._pings())

    @staticmethod
    async def _sleep_or_disconnect(request: Request, seconds: float) -> bool:
        """Sleep up to ``seconds``; return True early if the client went away."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if await request.is_disconnected():
                return True
            await anyio.sleep(min(0.05, max(deadline - time.monotonic(), 0)))
        return False


ALL_VERBS: list[str] = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]


def build_app(toggles: Toggles = Toggles()) -> Starlette:  # noqa: B008 - frozen, immutable default
    """Build the misbehaving Starlette app for ``toggles`` (no lifespan needed).

    Args:
        toggles: The wire behaviours to enable; the default is fully conformant.

    Returns:
        A Starlette app serving ``/mcp`` (and ``/mcp-redirected``) plus ``/__log``. The
        handler state is exposed as ``app.state.fixture`` (:class:`FixtureState`).
    """
    server = MisbehavingServer(toggles)
    app = Starlette(
        routes=[
            Route(MCP_PATH, server.handle, methods=ALL_VERBS),
            Route(REDIRECT_PATH, server.handle, methods=ALL_VERBS),
            Route(LOG_PATH, server.handle_log, methods=["GET", "DELETE"]),
        ]
    )
    app.state.fixture = server.state
    return app


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tests.fixtures.misbehaving_server",
        description="Deliberately misbehaving MCP server for mcpcheck tests (default: conformant).",
    )
    parser.add_argument("--port", type=int, default=0, help="TCP port on 127.0.0.1 (0 = free port)")
    parser.add_argument(
        "--toggle", action="append", default=[], metavar="NAME", help="switch a boolean toggle on"
    )
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="FIELD=VALUE",
        help="set any toggle field (JSON for tools/pages, 'none' clears optionals)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Run the misbehaving server on 127.0.0.1 until interrupted.

    Args:
        argv: Command-line arguments (defaults to ``sys.argv[1:]``).
    """
    args = _parser().parse_args(argv)
    try:
        toggles = toggles_from_cli(args.toggle, args.set)
    except ValueError as exc:
        _parser().error(str(exc))
    app = build_app(toggles)
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=args.port,
        log_level="warning",
        access_log=False,
        lifespan="off",
        timeout_graceful_shutdown=2,
    )
    serve_forever(config, f"mcpcheck misbehaving server {SERVER_VERSION} listening on ")


def serve_forever(config: uvicorn.Config, banner: str) -> None:
    """Bind, serve and print ``banner + url`` to stderr once the socket is listening."""
    server = uvicorn.Server(config)
    sock = config.bind_socket()
    url = f"http://127.0.0.1:{sock.getsockname()[1]}{MCP_PATH}"

    async def run() -> None:
        task = asyncio.ensure_future(server.serve(sockets=[sock]))
        while not server.started and not task.done():  # noqa: ASYNC110 - uvicorn exposes a flag, not an event
            await asyncio.sleep(0.01)
        if not task.done():
            print(banner + url, file=sys.stderr, flush=True)  # noqa: T201 - the entry point's contract
        await task

    asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover
    main()
