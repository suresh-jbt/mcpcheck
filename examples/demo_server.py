"""A small, conformant MCP **2026-07-28** server — and a switch to break it on purpose.

This file exists to be read. It is the server the one-command demo
(``bash examples/demo.sh``) points ``mcpcheck`` at, and it doubles as a worked
example of *how to write a conformant 2026-07-28 Streamable HTTP server* with the
official `mcp <https://pypi.org/project/mcp/>`_ Python SDK: every block that is
here **because the specification requires it** carries a one-line comment naming
the requirement.

It models a plausible little "support desk" server: two read-only tools, one
resource, one prompt, no side effects, deterministic answers. Everything above
:data:`BREAKS` is the conformant server; the ``--break`` machinery is a clearly
marked appendix at the end.

Run it::

    uv run python examples/demo_server.py --port 0          # conformant
    uv run python examples/demo_server.py --break cache     # one deliberate flaw
    uv run python examples/demo_server.py --list-breaks     # what can be broken

It prints ``demo server listening on http://127.0.0.1:<port>/mcp`` to stderr once
the socket is accepting connections, then serves until interrupted. It binds
``127.0.0.1`` only and never writes anything outside memory.

``--break FEATURE[,FEATURE...]`` introduces one realistic non-conformance per
feature; each one makes a *different* ``mcpcheck`` check fail or warn. See
``examples/README.md`` for the verified table of outcomes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import TYPE_CHECKING, TypedDict

import uvicorn
from mcp.server.caching import CacheHint
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mcp_types.methods import CacheableMethod
    from starlette.applications import Starlette
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

# --------------------------------------------------------------------------- identity

SERVER_NAME = "mcpcheck-demo-support-desk"
SERVER_VERSION = "1.0.0"
MCP_PATH = "/mcp"
INSTRUCTIONS = "Demo support desk. Every tool is read-only, deterministic and side-effect free."

#: What ``--break`` accepts, and what each feature does to the server.
BREAKS: dict[str, str] = {
    "cache": "strip ttlMs/cacheScope from every */list result",
    "origin": "accept any Origin header (no DNS-rebinding protection)",
    "output-schema": "drop outputSchema from every tool",
    "errors": "answer a failing tools/call with a stack trace",
    "discover": "omit serverInfo from the server/discover _meta",
    "session": "mint an Mcp-Session-Id response header",
    "initialize": "demand the legacy initialize handshake (2025-era server)",
}
ALL_BREAKS = "all"

# MUST: every cacheable result carries result-level cache hints (``ttlMs`` + ``cacheScope``).
# The SDK renders one entry of this mapping onto each matching result, so the whole
# requirement is satisfied by declaring it once, here.
# https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/caching
CACHE_HINTS: dict[CacheableMethod, CacheHint] = {
    "server/discover": CacheHint(ttl_ms=3_600_000, scope="public"),
    "tools/list": CacheHint(ttl_ms=300_000, scope="public"),
    "prompts/list": CacheHint(ttl_ms=300_000, scope="public"),
    "resources/list": CacheHint(ttl_ms=60_000, scope="public"),
    "resources/templates/list": CacheHint(ttl_ms=60_000, scope="public"),
    # ``private`` because a read can be user-specific; ``ttl_ms=0`` means "do not store".
    "resources/read": CacheHint(ttl_ms=0, scope="private"),
}
#: The four list methods whose results the ``cache`` break strips the hints from.
CACHEABLE_LISTS: frozenset[str] = frozenset(
    {"tools/list", "prompts/list", "resources/list", "resources/templates/list"}
)

# SHOULD: annotate read-only tools so clients (and mcpcheck) know they are safe to call.
# https://modelcontextprotocol.io/specification/2026-07-28/server/tools#tool
READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)

SERVER_INFO_KEY = "io.modelcontextprotocol/serverInfo"
FAKE_SESSION_ID = "demo-session-0000"
STACK_TRACE = (
    'Traceback (most recent call last):\n  File "demo_server.py", line 1, in call_tool\n'
    "    raise ValueError(args)\nValueError: error"
)

# --------------------------------------------------------------------------- the data

ORDERS: dict[str, tuple[str, int]] = {
    "A-1001": ("shipped", 2),
    "A-1002": ("packing", 4),
    "A-1003": ("delivered", 0),
}
FAQ: tuple[tuple[str, str], ...] = (
    ("refund", "Refunds are issued to the original payment method within 5 business days."),
    ("shipping", "Standard shipping takes 2-4 business days; express ships next day."),
    ("warranty", "Every order carries a 24 month warranty against manufacturing defects."),
)


class Order(TypedDict):
    """Structured result of ``lookup_order`` — described by its ``outputSchema``."""

    order_id: str
    status: str
    eta_days: int


class FaqAnswers(TypedDict):
    """Structured result of ``search_faq``."""

    query: str
    answers: list[str]


# --------------------------------------------------------------------------- the server


def make_server(*, output_schema: bool = True) -> MCPServer:
    """Build the support-desk ``MCPServer``.

    Args:
        output_schema: ``False`` registers the tools without an ``outputSchema``
            (the ``output-schema`` break).

    Returns:
        The configured server, not yet wrapped in an ASGI app.
    """
    server = MCPServer(
        name=SERVER_NAME,  # MUST: identify the server; the SDK publishes this as
        version=SERVER_VERSION,  # result._meta["io.modelcontextprotocol/serverInfo"].
        instructions=INSTRUCTIONS,
        cache_hints=CACHE_HINTS,
        log_level="WARNING",
    )
    # ``structured_output=None`` lets the SDK derive an ``outputSchema`` from the return
    # annotation; ``False`` suppresses it. An ``outputSchema`` is optional per spec, but
    # publishing one is what lets a client validate ``structuredContent``.
    structured: bool | None = None if output_schema else False

    @server.tool(
        name="lookup_order",
        description="Look up the status and ETA of an order by its id (read-only).",
        annotations=READ_ONLY,
        structured_output=structured,
    )
    def lookup_order(order_id: str) -> Order:
        """Return the status of ``order_id``.

        The SDK validates ``order_id`` against the generated ``inputSchema`` before this
        runs, so a MUST ("servers MUST validate all tool inputs") is met for free. The
        error raised here is a complete sentence naming the argument: error messages are
        read by a language model, so they have to be actionable.
        """
        try:
            status, eta = ORDERS[order_id]
        except KeyError:
            known = ", ".join(sorted(ORDERS))
            msg = f"Unknown order_id {order_id!r}. Known demo orders are: {known}."
            raise ValueError(msg) from None
        return Order(order_id=order_id, status=status, eta_days=eta)

    @server.tool(
        name="search_faq",
        description="Search the support FAQ and return the matching answers (read-only).",
        annotations=READ_ONLY,
        structured_output=structured,
    )
    def search_faq(query: str, limit: int = 3) -> FaqAnswers:
        """Return up to ``limit`` FAQ answers matching ``query``."""
        if limit < 1:
            msg = f"limit must be a positive integer, got {limit}."
            raise ValueError(msg)
        hits = [answer for topic, answer in FAQ if query.lower() in topic or topic in query.lower()]
        return FaqAnswers(query=query, answers=hits[:limit])

    @server.resource(
        "support://desk/hours",
        name="opening_hours",
        description="When the support desk is staffed.",
        mime_type="text/plain",
    )
    def opening_hours() -> str:
        """Return the static opening-hours resource."""
        return "The support desk is staffed Monday to Friday, 09:00-17:00 UTC."

    @server.prompt(name="escalate", description="Draft an escalation note for an order.")
    def escalate(order_id: str) -> str:
        """Build the escalation prompt."""
        return f"Draft a short, apologetic escalation note for order {order_id}."

    return server


def build_app(breaks: frozenset[str] = frozenset()) -> Starlette:
    """Build the demo ASGI app, with ``breaks`` applied.

    Args:
        breaks: Feature names from :data:`BREAKS`.

    Returns:
        A Starlette app serving MCP at :data:`MCP_PATH`.
    """
    # MUST: validate the ``Origin`` header and answer a forged one with HTTP 403
    # (DNS-rebinding protection). The SDK enables it automatically for loopback hosts,
    # so a conformant server only has to *not* switch it off.
    # https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http
    security: TransportSecuritySettings | None = None
    if "origin" in breaks:
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

    app = make_server(output_schema="output-schema" not in breaks).streamable_http_app(
        streamable_http_path=MCP_PATH,
        # Answer ``application/json`` rather than an SSE stream: simpler to curl, and
        # equally conformant — a server MAY choose either for a request/response pair.
        json_response=True,
        # MUST NOT require a session: 2026-07-28 removed `initialize` and
        # `Mcp-Session-Id`. Every request carries its own protocol version and client
        # capabilities in ``_meta``, so this server is stateless and scales horizontally.
        # https://modelcontextprotocol.io/specification/2026-07-28/basic#statelessness
        stateless_http=True,
        transport_security=security,
        host="127.0.0.1",
    )
    if "initialize" in breaks:
        app.add_middleware(RequireInitialize)
    surgery = breaks & {"cache", "discover", "errors", "session"}
    if surgery:
        app.add_middleware(BreakResponses, features=surgery)
    return app


# --------------------------------------------------------------------------- the breaks
#
# Everything below exists only for the ``--break`` switch. Two pure-ASGI middlewares are
# enough to reproduce the three mistakes that cannot be made by configuration alone:
# a 2025-era server that demands ``initialize``, and small edits to outgoing responses.


class RequireInitialize:
    """Reject every POST the way a 2025-era, session-based server would.

    A real server with this bug looks healthy to old clients and refuses every
    2026-07-28 client. ``mcpcheck``'s ``handshake`` check is the era gate, so this
    break also stops the remaining checks from running.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Answer HTTP 400 + ``-32600`` to POSTs, pass everything else through."""
        if scope["type"] == "http" and scope["method"] == "POST":
            body = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Server not initialized"},
            }
            await JSONResponse(body, status_code=400)(scope, receive, send)
            return
        await self.app(scope, receive, send)


class BreakResponses:
    """Edit outgoing responses: strip cache hints, drop ``serverInfo``, mint a session id.

    Buffers only ``application/json`` bodies (an SSE stream is streamed through
    untouched) and keys the body edits off the request's ``Mcp-Method`` header, so each
    break stays confined to the one check it is meant to trip.
    """

    def __init__(self, app: ASGIApp, features: frozenset[str]) -> None:
        self.app = app
        self.features = features

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Wrap ``send`` with the response surgery for this request."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = _header(scope, b"mcp-method")
        start: Message | None = None
        chunks: list[bytes] = []
        buffering = False

        async def intercept(message: Message) -> None:
            nonlocal start, buffering
            if message["type"] == "http.response.start":
                headers = MutableHeaders(raw=message["headers"])
                if "session" in self.features:
                    # SHOULD NOT: a 2026-07-28 server has no sessions, so minting an id
                    # makes it stateful again and tells clients to come back to one node.
                    headers["mcp-session-id"] = FAKE_SESSION_ID
                start, buffering = (
                    message,
                    headers.get("content-type", "").startswith("application/json"),
                )
                if not buffering:
                    await send(message)
                return
            if message["type"] != "http.response.body" or not buffering or start is None:
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return
            body = self._patch(b"".join(chunks), method)
            MutableHeaders(raw=start["headers"])["content-length"] = str(len(body))
            await send(start)
            await send({"type": "http.response.body", "body": body, "more_body": False})

        await self.app(scope, receive, intercept)

    def _patch(self, raw: bytes, method: str | None) -> bytes:
        """Return ``raw`` with this instance's body edits applied."""
        try:
            message = json.loads(raw)
        except ValueError:
            return raw
        if not isinstance(message, dict):
            return raw
        result = message.get("result")
        error = message.get("error")
        if "cache" in self.features and method in CACHEABLE_LISTS and isinstance(result, dict):
            # MUST: ``ttlMs`` and ``cacheScope`` on every cacheable result. Without them
            # a client cannot cache a listing at all and has to re-fetch it every time.
            result.pop("ttlMs", None)
            result.pop("cacheScope", None)
        if "discover" in self.features and method == "server/discover":
            # SHOULD: identify the server in every result's ``_meta``. Without it a
            # client cannot tell what it is talking to, or report it to a human.
            meta = result.get("_meta") if isinstance(result, dict) else None
            if isinstance(meta, dict):
                meta.pop(SERVER_INFO_KEY, None)
        if "errors" in self.features and method == "tools/call":
            # SHOULD: a tool error is a concise sentence a model can act on. A stack
            # trace tells the caller nothing and leaks server internals. The SDK reports
            # input-validation failures as a ``CallToolResult`` with ``isError: true``
            # (both that and a JSON-RPC error are conformant), so patch whichever came.
            if isinstance(error, dict):
                error["message"] = STACK_TRACE
            elif isinstance(result, dict) and result.get("isError") is True:
                result["content"] = [{"type": "text", "text": STACK_TRACE}]
        return json.dumps(message).encode()


def _header(scope: Scope, name: bytes) -> str | None:
    """Return request header ``name`` (lowercase, bytes) from an ASGI scope."""
    headers: list[tuple[bytes, bytes]] = scope["headers"]
    for key, value in headers:
        if key == name:
            return value.decode("latin-1")
    return None


# --------------------------------------------------------------------------- entry point


def parse_breaks(value: str) -> frozenset[str]:
    """Parse a ``--break`` value into feature names.

    Args:
        value: Comma-separated feature names, or ``all``.

    Raises:
        ValueError: An unknown feature name was given.
    """
    names = [part.strip() for part in value.split(",") if part.strip()]
    if ALL_BREAKS in names:
        return frozenset(BREAKS)
    unknown = [name for name in names if name not in BREAKS]
    if unknown:
        choices = ", ".join([*BREAKS, ALL_BREAKS])
        msg = f"unknown --break feature {unknown[0]!r}; choose from {choices}"
        raise ValueError(msg)
    return frozenset(names)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python examples/demo_server.py",
        description="Conformant MCP 2026-07-28 demo server, with optional deliberate flaws.",
    )
    parser.add_argument("--port", type=int, default=0, help="TCP port on 127.0.0.1 (0 = free port)")
    parser.add_argument(
        "--break",
        dest="breaks",
        metavar="FEATURE[,FEATURE...]",
        default="",
        help="introduce deliberate non-conformances (see --list-breaks, or 'all')",
    )
    parser.add_argument(
        "--list-breaks", action="store_true", help="print the --break features and exit"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Serve the demo on ``127.0.0.1`` until interrupted.

    Args:
        argv: Command-line arguments (defaults to ``sys.argv[1:]``).

    Returns:
        The process exit code (``2`` for a bad ``--break`` value).
    """
    args = _parser().parse_args(argv)
    if args.list_breaks:
        for name, what in BREAKS.items():
            print(f"{name:14s} {what}")  # noqa: T201 - this subcommand's whole output
        print(f"{ALL_BREAKS:14s} every break above at once")  # noqa: T201
        return 0
    try:
        breaks = parse_breaks(args.breaks)
    except ValueError as exc:
        print(f"demo_server.py: error: {exc}", file=sys.stderr)  # noqa: T201 - CLI diagnostics
        return 2
    config = uvicorn.Config(
        build_app(breaks),
        host="127.0.0.1",
        port=args.port,
        log_level="warning",
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(config)
    sock = config.bind_socket()  # bind first so the printed port is the real one
    url = f"http://127.0.0.1:{sock.getsockname()[1]}{MCP_PATH}"

    async def serve() -> None:
        task = asyncio.ensure_future(server.serve(sockets=[sock]))
        while not server.started and not task.done():  # noqa: ASYNC110 - uvicorn exposes a flag
            await asyncio.sleep(0.01)
        if not task.done():
            broken = ", ".join(sorted(breaks)) or "none"
            print(  # noqa: T201 - the readiness contract demo.sh waits for
                f"demo server listening on {url} (breaks: {broken})", file=sys.stderr, flush=True
            )
        await task

    asyncio.run(serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
