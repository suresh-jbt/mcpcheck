"""Fully conformant MCP 2026-07-28 reference server built on the official ``mcp`` SDK.

This is the fixture every mcpcheck check must *pass* against (PLAN.md section 10.3).
It is tests-only (not shipped in the wheel) and deterministic: fixed ``serverInfo``,
fixed tool order, fixed strings, so later phases can embed real output in the README
and in golden files.

Run it standalone from the repository root::

    uv run python -m tests.fixtures.reference_server --port 0

It prints ``mcpcheck reference server 0.1.0 listening on http://127.0.0.1:<port>/mcp``
to stderr once the socket is bound and serves until interrupted.

What the default app exposes
    * two read-only tools (``lookup_weather``, ``echo_required``), both with an
      ``outputSchema`` and ``readOnlyHint: true`` (``--no-output-schema`` drops the
      output schemas so the ``schemas`` WARN path can be exercised);
    * one resource (``demo://greeting``) and one prompt (``summarize``);
    * result-level cache hints on all six cacheable methods;
    * Origin / Host validation (the SDK's DNS-rebinding guard) for loopback hosts;
    * the SDK's dual-era routing: a POST without ``MCP-Protocol-Version`` (or naming a
      2024/2025 version) is served by the legacy transport, everything else by the
      modern per-request entry which never mints ``Mcp-Session-Id``.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, Any, TypedDict

import uvicorn
from mcp.server.caching import CacheHint
from mcp.server.lowlevel.server import Server
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from starlette.responses import Response

from tests.fixtures.misbehaving_server import serve_forever

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mcp_types.methods import CacheableMethod
    from starlette.applications import Starlette
    from starlette.types import ASGIApp, Receive, Scope, Send

SERVER_NAME = "mcpcheck-reference-server"
SERVER_VERSION = "0.1.0"
MCP_PATH = "/mcp"
INSTRUCTIONS = "Reference fixture for mcpcheck. All tools are read-only and side-effect free."
TOOL_NAMES: tuple[str, ...] = ("lookup_weather", "echo_required")
"""Tool names in the order ``tools/list`` returns them."""

CACHE_HINTS: dict[CacheableMethod, CacheHint] = {
    "server/discover": CacheHint(ttl_ms=3_600_000, scope="public"),
    "tools/list": CacheHint(ttl_ms=300_000, scope="public"),
    "prompts/list": CacheHint(ttl_ms=300_000, scope="public"),
    "resources/list": CacheHint(ttl_ms=60_000, scope="public"),
    "resources/templates/list": CacheHint(ttl_ms=60_000, scope="public"),
    "resources/read": CacheHint(ttl_ms=0, scope="private"),
}

READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)


class Weather(TypedDict):
    """Structured result of ``lookup_weather``."""

    city: str
    temperature_c: float
    conditions: str


class Echo(TypedDict):
    """Structured result of ``echo_required``."""

    text: str
    repeat: int


def make_server(*, output_schema: bool = True, empty: bool = False) -> MCPServer:
    """Create the ``MCPServer`` instance behind the reference app.

    Args:
        output_schema: When False, both tools are registered with
            ``structured_output=False`` so their listings carry no ``outputSchema``.
        empty: When True, register no tools, resources or prompts; the SDK still
            declares all three capabilities and serves empty lists.

    Returns:
        A configured ``MCPServer`` (not yet wrapped in an ASGI app).
    """
    server = MCPServer(
        name=SERVER_NAME,
        version=SERVER_VERSION,
        instructions=INSTRUCTIONS,
        cache_hints=CACHE_HINTS,
        log_level="WARNING",
    )
    if empty:
        return server
    structured: bool | None = None if output_schema else False

    @server.tool(
        name="lookup_weather",
        description="Return a canned weather report for a city (read-only, deterministic).",
        annotations=READ_ONLY,
        structured_output=structured,
    )
    def lookup_weather(city: str, units: str = "celsius") -> Weather:
        """Return a canned weather report for ``city``."""
        temperature = 21.5 if units == "celsius" else 70.7
        return Weather(city=city, temperature_c=temperature, conditions="clear")

    @server.tool(
        name="echo_required",
        description="Echo a message a number of times (read-only). Both arguments are required.",
        annotations=READ_ONLY,
        structured_output=structured,
    )
    async def echo_required(message: str, repeat: int, ctx: Context[Any]) -> Echo:
        """Echo ``message`` ``repeat`` times, reporting progress when a token was sent."""
        # A no-op unless the request carried _meta.progressToken; with one the SDK
        # answers text/event-stream instead of application/json.
        await ctx.report_progress(1, 1, "echoing")
        return Echo(text=" ".join([message] * repeat), repeat=repeat)

    @server.resource(
        "demo://greeting",
        name="greeting",
        description="A static greeting.",
        mime_type="text/plain",
    )
    def greeting() -> str:
        """Return the static greeting resource."""
        return "hello from the mcpcheck reference server"

    @server.prompt(name="summarize", description="Ask the model to summarize some text.")
    def summarize(text: str) -> str:
        """Build the summarize prompt."""
        return f"Summarize the following text in one sentence:\n\n{text}"

    return server


class BearerTokenMiddleware:
    """Pure-ASGI middleware that demands ``Authorization: Bearer <token>`` on every request.

    Anything else receives ``401`` with a ``WWW-Authenticate`` challenge and an empty
    body, which is what mcpcheck's checks must classify as ``auth_required``.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Dispatch the request or answer 401."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        presented = next(
            (v.decode("latin-1") for k, v in scope["headers"] if k == b"authorization"), None
        )
        if presented != f"Bearer {self.token}":
            response = Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="mcpcheck-reference-server"'},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def build_app(
    *,
    json_response: bool = False,
    stateless_http: bool = True,
    origin_check: bool = True,
    host: str = "127.0.0.1",
    output_schema: bool = True,
    empty: bool = False,
    bare: bool = False,
    require_token: str | None = None,
) -> Starlette:
    """Build the reference Starlette app.

    Args:
        json_response: Always answer ``application/json`` (never SSE).
        stateless_http: ``True`` (default) keeps the legacy path session-less; ``False``
            makes the legacy ``initialize`` path mint ``Mcp-Session-Id`` (dual-era server).
            The modern path never mints ids either way.
        origin_check: ``True`` leaves the SDK's automatic DNS-rebinding protection in
            place (enabled for loopback ``host`` values only, exactly as a real
            deployment would see it); ``False`` disables Origin/Host validation.
        host: The ``host`` passed to ``streamable_http_app``. Passing ``"0.0.0.0"``
            with ``origin_check=True`` reproduces the SDK footgun: auto-protection is
            not enabled for non-loopback hosts, so a foreign Origin is accepted.
        output_schema: ``False`` drops ``outputSchema`` from both tools.
        empty: Declare all capabilities but register no primitives.
        bare: Use a low-level ``Server`` with no handlers: ``capabilities: {}`` and every
            list method answers ``-32601``.
        require_token: When set, wrap the app in :class:`BearerTokenMiddleware`.

    Returns:
        The Starlette app. Under ``httpx2.ASGITransport`` its lifespan must be entered
        manually: ``async with app.router.lifespan_context(app): ...``.
    """
    security: TransportSecuritySettings | None = None
    if not origin_check:
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    if bare:
        low = Server(name=SERVER_NAME, version=SERVER_VERSION)
        app = low.streamable_http_app(
            streamable_http_path=MCP_PATH,
            json_response=json_response,
            stateless_http=stateless_http,
            transport_security=security,
            host=host,
        )
    else:
        app = make_server(output_schema=output_schema, empty=empty).streamable_http_app(
            streamable_http_path=MCP_PATH,
            json_response=json_response,
            stateless_http=stateless_http,
            transport_security=security,
            host=host,
        )
    if require_token is not None:
        app.add_middleware(BearerTokenMiddleware, token=require_token)
    return app


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tests.fixtures.reference_server",
        description="Conformant MCP 2026-07-28 reference server for mcpcheck (tests only).",
    )
    parser.add_argument("--port", type=int, default=0, help="TCP port on 127.0.0.1 (0 = free port)")
    parser.add_argument(
        "--json-response", action="store_true", help="always answer application/json"
    )
    parser.add_argument(
        "--stateful",
        action="store_true",
        help="legacy initialize path keeps sessions (dual-era server, mints Mcp-Session-Id)",
    )
    parser.add_argument(
        "--no-origin-check", action="store_true", help="disable Origin/Host validation"
    )
    parser.add_argument(
        "--require-token", metavar="TOKEN", default=None, help="demand Authorization: Bearer TOKEN"
    )
    parser.add_argument(
        "--app-host",
        metavar="HOST",
        default="127.0.0.1",
        help="host passed to streamable_http_app (0.0.0.0 disables auto Origin protection)",
    )
    parser.add_argument(
        "--no-output-schema", action="store_true", help="drop outputSchema from both tools"
    )
    parser.add_argument(
        "--empty", action="store_true", help="declare capabilities but expose no primitives"
    )
    parser.add_argument(
        "--bare", action="store_true", help="low-level server: capabilities {} and lists -32601"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Run the reference server on 127.0.0.1 until interrupted.

    Args:
        argv: Command-line arguments (defaults to ``sys.argv[1:]``).
    """
    args = _parser().parse_args(argv)
    app = build_app(
        json_response=args.json_response,
        stateless_http=not args.stateful,
        origin_check=not args.no_origin_check,
        host=args.app_host,
        output_schema=not args.no_output_schema,
        empty=args.empty,
        bare=args.bare,
        require_token=args.require_token,
    )
    config = uvicorn.Config(
        app, host="127.0.0.1", port=args.port, log_level="warning", access_log=False, lifespan="on"
    )
    serve_forever(config, f"mcpcheck reference server {SERVER_VERSION} listening on ")


if __name__ == "__main__":  # pragma: no cover
    main()
