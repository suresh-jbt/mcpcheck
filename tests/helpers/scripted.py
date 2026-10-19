"""MockTransport-based scripted MCP server for L1 tests (PLAN.md §10.2).

``ScriptedServer`` records every request it sees (``Seen(method, headers, body)``)
so tests can assert what mcpcheck *sent*, and answers from ``routes`` keyed by
JSON-RPC method. Unknown methods get ``404 -32601`` by default.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx2

from mcpcheck.client import ClientConfig, RawClient, Transcript

Route = Callable[[dict[str, Any], httpx2.Request], httpx2.Response]
HttpRoute = Callable[[httpx2.Request], httpx2.Response]


@dataclass
class Seen:
    """One recorded request."""

    method: str | None
    headers: httpx2.Headers
    body: dict[str, Any] | None
    request: httpx2.Request
    raw: bytes

    @property
    def meta(self) -> dict[str, Any] | None:
        """The ``params._meta`` object of the request body, if any."""
        if not self.body:
            return None
        params = self.body.get("params")
        if isinstance(params, dict) and isinstance(params.get("_meta"), dict):
            meta: dict[str, Any] = params["_meta"]
            return meta
        return None


def sse_body(*msgs: dict[str, Any]) -> bytes:
    """Encode JSON-RPC objects as ``event: message`` SSE frames."""
    return "".join(f"event: message\ndata: {json.dumps(m)}\n\n" for m in msgs).encode()


def ok(
    body: dict[str, Any],
    result: dict[str, Any],
    *,
    sse: bool = False,
    headers: dict[str, str] | None = None,
    status: int = 200,
    extra_messages: tuple[dict[str, Any], ...] = (),
) -> httpx2.Response:
    """A result response to ``body`` (same id), as JSON or SSE."""
    msg = {"jsonrpc": "2.0", "id": body.get("id"), "result": result}
    if sse:
        return httpx2.Response(
            status,
            headers={"content-type": "text/event-stream", **(headers or {})},
            content=sse_body(*extra_messages, msg),
        )
    return httpx2.Response(status, json=msg, headers=headers or {})


def jsonrpc_error(
    req_id: int | str | None, code: int, message: str, data: Any = None
) -> dict[str, Any]:
    """A JSON-RPC error object."""
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


_ECHO = object()
"""Sentinel: echo the request's own id (what a conformant server does)."""


def error_response(
    body: dict[str, Any],
    code: int,
    message: str,
    *,
    status: int,
    data: Any = None,
    sse: bool = False,
    null_id: bool = False,
    request_id: Any = _ECHO,
    headers: dict[str, str] | None = None,
) -> httpx2.Response:
    """An HTTP ``status`` carrying a JSON-RPC error for ``body``.

    ``request_id`` overrides the echoed id, which real servers do: Python mcp 1.x
    answers a rejected request with the sentinel ``"server-error"``.
    """
    echoed = body.get("id") if request_id is _ECHO else request_id
    msg = jsonrpc_error(None if null_id else echoed, code, message, data)
    if sse:
        return httpx2.Response(
            status,
            headers={"content-type": "text/event-stream", **(headers or {})},
            content=sse_body(msg),
        )
    return httpx2.Response(status, json=msg, headers=headers or {})


def list_result(items: list[dict[str, Any]], key: str = "tools", **extra: Any) -> dict[str, Any]:
    """A conformant 2026-07-28 list result with cache hints and serverInfo."""
    result: dict[str, Any] = {
        "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "scripted", "version": "0.0.1"}},
        "resultType": "complete",
        key: items,
        "ttlMs": 300000,
        "cacheScope": "public",
    }
    result.update(extra)
    return result


@dataclass
class ScriptedServer:
    """Answer JSON-RPC methods from ``routes``; record everything in ``seen``."""

    routes: dict[str, Route] = field(default_factory=dict)
    default_status: int = 404
    seen: list[Seen] = field(default_factory=list)
    http_routes: dict[str, HttpRoute] = field(default_factory=dict)
    catch_all: Route | None = None

    async def handler(self, req: httpx2.Request) -> httpx2.Response:
        """The ``httpx2.MockTransport`` handler."""
        raw = req.content
        body: dict[str, Any] | None = None
        method: str | None = None
        if req.method == "POST":
            try:
                parsed = json.loads(raw.decode("utf-8")) if raw else None
            except (ValueError, UnicodeDecodeError):
                parsed = None
            if isinstance(parsed, dict):
                body = parsed
                m = parsed.get("method")
                method = m if isinstance(m, str) else None
        self.seen.append(Seen(method, req.headers, body, req, raw))
        if req.method != "POST":
            route = self.http_routes.get(req.method)
            if route is None:
                return httpx2.Response(405, headers={"allow": "POST"})
            return route(req)
        if body is None:
            return httpx2.Response(400, json=jsonrpc_error(None, -32700, "Parse error"))
        if method is not None and method in self.routes:
            return self.routes[method](body, req)
        if self.catch_all is not None:
            return self.catch_all(body, req)
        return httpx2.Response(
            self.default_status,
            json=jsonrpc_error(body.get("id"), -32601, f"Method not found: {method}"),
        )

    def transport(self) -> httpx2.MockTransport:
        """A transport wired to :meth:`handler`."""
        return httpx2.MockTransport(self.handler)

    def client(self, **kw: Any) -> httpx2.AsyncClient:
        """A plain ``httpx2.AsyncClient`` talking to this server."""
        return httpx2.AsyncClient(
            transport=self.transport(),
            base_url="http://127.0.0.1:1",
            timeout=httpx2.Timeout(5),
            **kw,
        )

    def raw_client(
        self,
        cfg: ClientConfig | None = None,
        transcript: Transcript | None = None,
        *,
        label: str = "main",
        send_token: bool = True,
    ) -> RawClient:
        """A :class:`RawClient` talking to this server through ``MockTransport``."""
        return RawClient(
            cfg or ClientConfig(url="http://127.0.0.1:1/mcp"),
            transcript if transcript is not None else Transcript(),
            label=label,
            send_token=send_token,
            transport=self.transport(),
        )

    def methods(self) -> list[str | None]:
        """Methods in order of arrival."""
        return [s.method for s in self.seen]


def conformant_server(
    *, sse: bool = False, tools: list[dict[str, Any]] | None = None
) -> ScriptedServer:
    """A server whose list/discover methods answer conformantly."""
    tools = tools if tools is not None else [{"name": "echo", "inputSchema": {"type": "object"}}]

    def discover(body: dict[str, Any], _req: httpx2.Request) -> httpx2.Response:
        return ok(
            body,
            {
                "_meta": {
                    "io.modelcontextprotocol/serverInfo": {"name": "scripted", "version": "0.0.1"}
                },
                "resultType": "complete",
                "supportedVersions": ["2026-07-28"],
                "capabilities": {"tools": {}, "prompts": {}, "resources": {}},
                "ttlMs": 60000,
                "cacheScope": "public",
            },
            sse=sse,
        )

    return ScriptedServer(
        routes={
            "server/discover": discover,
            "tools/list": lambda b, _r: ok(b, list_result(tools, "tools"), sse=sse),
            "prompts/list": lambda b, _r: ok(b, list_result([], "prompts"), sse=sse),
            "resources/list": lambda b, _r: ok(b, list_result([], "resources"), sse=sse),
            "resources/templates/list": lambda b, _r: ok(
                b, list_result([], "resourceTemplates"), sse=sse
            ),
        }
    )


__all__ = [
    "HttpRoute",
    "Route",
    "ScriptedServer",
    "Seen",
    "conformant_server",
    "error_response",
    "jsonrpc_error",
    "list_result",
    "ok",
    "sse_body",
]
