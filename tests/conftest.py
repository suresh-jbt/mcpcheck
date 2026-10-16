"""Shared pytest configuration and fixture-server plumbing.

The anyio pytest plugin parametrises async tests over every installed backend;
pin it to asyncio (the only backend mcpcheck supports).

Fixture servers (PLAN.md section 10.3 / 10.6)
    * :class:`ThreadServer` runs any ASGI app under uvicorn on ``127.0.0.1:<free port>`` in
      a daemon thread (readiness by polling ``server.started``, clean shutdown via
      ``should_exit``). Never ``localhost``, never port 8765.
    * ``reference_url`` / ``reference_url_json`` (session) and ``reference_url_factory``
      give TCP URLs of the SDK-based reference server; ``misbehaving_url(toggles)``
      (module) starts one misbehaving server per call.
    * :func:`asgi_client` wraps ``httpx2.ASGITransport`` and enters the SDK app's
      lifespan manually; ``reference_client`` / ``misbehaving_client`` are ready-made
      default clients. ASGI requests must use host ``127.0.0.1:<port>`` (the SDK's
      DNS-rebinding guard answers 421 for ``testserver`` and for a port-less Host).
    * ``request_log(url)`` / ``clear_request_log(url)`` read and reset the misbehaving
      server's ``/__log``.
    * :func:`modern_headers` / :func:`modern_body` build a complete 2026-07-28 request.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from typing import TYPE_CHECKING, Any, Literal

import httpx2
import pytest
import uvicorn

from tests.fixtures import misbehaving_server, reference_server
from tests.fixtures.misbehaving_server import Toggles

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator

    from starlette.types import ASGIApp

LifespanMode = Literal["auto", "on", "off"]
PROTOCOL_VERSION = "2026-07-28"
ASGI_CLIENT_PORT = 40001
"""Peer port reported for ASGI requests (``X-Client-Port`` on the misbehaving server)."""
ASGI_BASE_URL = "http://127.0.0.1:8000"
"""Base URL of ASGI clients; the Host header must carry a port for the SDK's guard (``127.0.0.1:*``)."""
CLIENT_INFO: dict[str, str] = {"name": "mcpcheck-tests", "version": "0.0.0"}
READINESS_TIMEOUT_S = 10.0
SHUTDOWN_TIMEOUT_S = 5.0


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Run every ``@pytest.mark.anyio`` test on asyncio only."""
    return "asyncio"


# ------------------------------------------------------------------ wire helpers


def modern_meta(protocol_version: str = PROTOCOL_VERSION) -> dict[str, Any]:
    """Return a complete ``_meta`` envelope for a modern request."""
    return {
        "io.modelcontextprotocol/protocolVersion": protocol_version,
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": dict(CLIENT_INFO),
    }


def modern_headers(
    method: str, *, protocol_version: str = PROTOCOL_VERSION, **extra: str
) -> dict[str, str]:
    """Return the request headers of a conformant 2026-07-28 POST.

    Args:
        method: The JSON-RPC method (mirrored into ``Mcp-Method``).
        protocol_version: Value of ``MCP-Protocol-Version``.
        **extra: Additional headers (e.g. ``origin=...``); header names use underscores
            for dashes (``mcp_name`` -> ``mcp-name``).
    """
    headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "mcp-protocol-version": protocol_version,
        "mcp-method": method,
    }
    headers.update({k.replace("_", "-"): v for k, v in extra.items()})
    return headers


def modern_body(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    rid: Any = 1,
    protocol_version: str = PROTOCOL_VERSION,
) -> dict[str, Any]:
    """Return the JSON body of a conformant 2026-07-28 request (``_meta`` first)."""
    full: dict[str, Any] = {"_meta": modern_meta(protocol_version)}
    if params:
        full.update(params)
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": full}


# ------------------------------------------------------------------ TCP servers


def free_port() -> int:
    """Return a currently free TCP port on 127.0.0.1 (read back from the socket)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ThreadServer:
    """uvicorn in a daemon thread on ``127.0.0.1:<free port>``.

    The socket is bound before the thread starts (no port race); ``start()`` returns
    once uvicorn reports ``started``; ``stop()`` sets ``should_exit`` and joins.
    """

    def __init__(self, app: ASGIApp, *, lifespan: LifespanMode = "on") -> None:
        self.app = app
        self.lifespan = lifespan
        self.port = 0
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def origin(self) -> str:
        """``http://127.0.0.1:<port>``."""
        return f"http://127.0.0.1:{self.port}"

    @property
    def url(self) -> str:
        """The MCP endpoint URL."""
        return f"{self.origin}/mcp"

    @property
    def running(self) -> bool:
        """Whether the server thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> ThreadServer:
        """Bind, start the thread and wait for readiness."""
        config = uvicorn.Config(
            self.app,
            host="127.0.0.1",
            port=0,
            log_level="warning",
            access_log=False,
            lifespan=self.lifespan,
            timeout_graceful_shutdown=2,
        )
        server = uvicorn.Server(config)
        sock = config.bind_socket()
        self.port = int(sock.getsockname()[1])
        thread = threading.Thread(
            target=server.run, kwargs={"sockets": [sock]}, name="uvicorn-fixture", daemon=True
        )
        self._server, self._thread = server, thread
        thread.start()
        deadline = time.monotonic() + READINESS_TIMEOUT_S
        while not server.started:
            if not thread.is_alive():
                raise RuntimeError("uvicorn fixture thread died during startup")
            if time.monotonic() > deadline:
                self.stop()
                raise RuntimeError("uvicorn fixture did not start within 10 s")
            time.sleep(0.01)
        return self

    def stop(self) -> None:
        """Ask uvicorn to exit and join the thread."""
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(SHUTDOWN_TIMEOUT_S)
            if self._thread.is_alive():
                raise RuntimeError("uvicorn fixture thread did not stop within 5 s")
        self._server, self._thread = None, None

    def __enter__(self) -> ThreadServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


def start_reference_server(**build_kwargs: Any) -> ThreadServer:
    """Start the SDK reference server over TCP (``build_app(**build_kwargs)``)."""
    return ThreadServer(reference_server.build_app(**build_kwargs), lifespan="on").start()


def start_misbehaving_server(toggles: Toggles = Toggles()) -> ThreadServer:  # noqa: B008 - frozen default
    """Start the misbehaving server over TCP for ``toggles``."""
    return ThreadServer(misbehaving_server.build_app(toggles), lifespan="off").start()


@pytest.fixture(scope="session")
def reference_server_tcp() -> Iterator[ThreadServer]:
    """Session-wide default reference server over TCP."""
    server = start_reference_server()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture(scope="session")
def reference_url(reference_server_tcp: ThreadServer) -> str:
    """URL of the default (SSE-capable, stateless) reference server."""
    return reference_server_tcp.url


@pytest.fixture(scope="session")
def reference_url_json() -> Iterator[str]:
    """URL of a reference server that always answers ``application/json``."""
    server = start_reference_server(json_response=True)
    try:
        yield server.url
    finally:
        server.stop()


@pytest.fixture(scope="session")
def reference_url_factory() -> Iterator[Callable[..., str]]:
    """``factory(**build_app kwargs) -> url``; one server per distinct kwargs, torn down at session end."""
    servers: dict[tuple[tuple[str, Any], ...], ThreadServer] = {}

    def factory(**build_kwargs: Any) -> str:
        key = tuple(sorted(build_kwargs.items()))
        if key not in servers:
            servers[key] = start_reference_server(**build_kwargs)
        return servers[key].url

    try:
        yield factory
    finally:
        for server in servers.values():
            server.stop()


@pytest.fixture(scope="module")
def misbehaving_url() -> Iterator[Callable[[Toggles], str]]:
    """``misbehaving_url(toggles) -> url``; a fresh server per call, all stopped at module end."""
    servers: list[ThreadServer] = []

    def factory(toggles: Toggles = Toggles()) -> str:  # noqa: B008 - frozen default
        server = start_misbehaving_server(toggles)
        servers.append(server)
        return server.url

    try:
        yield factory
    finally:
        for server in servers:
            server.stop()


def _log_url(url: str) -> str:
    return str(httpx2.URL(url).copy_with(path=misbehaving_server.LOG_PATH, query=None))


def fetch_request_log(url: str) -> list[dict[str, Any]]:
    """Return the misbehaving server's request log for the server behind ``url``."""
    with httpx2.Client(timeout=5) as client:
        response = client.get(_log_url(url))
        response.raise_for_status()
        entries: list[dict[str, Any]] = response.json()
        return entries


def reset_request_log(url: str) -> None:
    """Clear the misbehaving server's request log for the server behind ``url``."""
    with httpx2.Client(timeout=5) as client:
        client.delete(_log_url(url)).raise_for_status()


@pytest.fixture
def request_log() -> Callable[[str], list[dict[str, Any]]]:
    """Accessor ``request_log(url) -> entries`` for the misbehaving server's ``/__log``."""
    return fetch_request_log


@pytest.fixture
def clear_request_log() -> Callable[[str], None]:
    """``clear_request_log(url)`` empties the misbehaving server's request log."""
    return reset_request_log


# ------------------------------------------------------------------ ASGI clients


@contextlib.asynccontextmanager
async def asgi_client(
    app: Any,
    *,
    client_port: int = ASGI_CLIENT_PORT,
    lifespan: bool = True,
    **client_kwargs: Any,
) -> AsyncIterator[httpx2.AsyncClient]:
    """Yield an ``httpx2.AsyncClient`` driving ``app`` in-process.

    Args:
        app: A Starlette app from either fixture's ``build_app``.
        client_port: Peer port presented to the app (distinct values simulate distinct
            connections for the misbehaving server's ``X-Client-Port``).
        lifespan: Enter ``app.router.lifespan_context`` (required for the SDK app, harmless
            for the misbehaving one).
        **client_kwargs: Forwarded to ``httpx2.AsyncClient`` (e.g. ``headers=``).
    """
    transport = httpx2.ASGITransport(app, client=("127.0.0.1", client_port))
    async with contextlib.AsyncExitStack() as stack:
        if lifespan:
            await stack.enter_async_context(app.router.lifespan_context(app))
        client = await stack.enter_async_context(
            httpx2.AsyncClient(
                transport=transport,
                base_url=ASGI_BASE_URL,
                timeout=httpx2.Timeout(10),
                **client_kwargs,
            )
        )
        yield client


@pytest.fixture
async def reference_client(anyio_backend: str) -> AsyncIterator[httpx2.AsyncClient]:
    """In-process client for the default reference app (lifespan entered)."""
    async with asgi_client(reference_server.build_app()) as client:
        yield client


@pytest.fixture
async def misbehaving_client(anyio_backend: str) -> AsyncIterator[httpx2.AsyncClient]:
    """In-process client for the default (conformant) misbehaving app."""
    async with asgi_client(misbehaving_server.build_app(), lifespan=False) as client:
        yield client
