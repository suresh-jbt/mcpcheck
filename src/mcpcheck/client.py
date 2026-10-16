"""Raw Streamable HTTP JSON-RPC client over ``httpx2`` (PLAN.md §6.5).

Every verdict mcpcheck emits comes from a :class:`RawReply` produced here: HTTP
status and headers are preserved, JSON and SSE bodies are parsed into JSON-RPC
objects, header overrides/omissions are possible on fresh clients, every request
runs under a *total* ``anyio.fail_after`` budget, bodies are capped, redirects
follow the same-origin 307/308 policy only, and every exchange is recorded
(redacted) in a :class:`Transcript`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urljoin

import anyio
import httpx2

from mcpcheck import wire
from mcpcheck.wire import (
    H_AUTHORIZATION,
    H_METHOD,
    H_ORIGIN,
    H_PROTOCOL_VERSION,
    H_SESSION_ID,
    HTTP_PROBE_BUDGET_S,
    MAX_BODY_BYTES,
    MEDIA_JSON,
    MEDIA_SSE,
    PROTOCOL_VERSION,
    SSE_TERMINATION_GRACE_S,
    TRANSCRIPT_BODY_LIMIT,
    USER_AGENT,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from types import TracebackType


class _Unset:
    """Sentinel type for "argument not given" (distinct from ``None``)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"


Unset = _Unset
UNSET = _Unset()

ProbePolicy = Literal["safe", "all", "none"]


@dataclass(frozen=True)
class ClientConfig:
    """Everything the CLI decided; shared by every client of a run."""

    url: str
    token: str | None = None
    extra_headers: tuple[tuple[str, str], ...] = ()
    origin: str | None = None
    timeout: float = wire.REQUEST_TIMEOUT
    verify_tls: bool = True
    max_redirects: int = 5
    probe: ProbePolicy = "safe"
    max_tools: int = wire.MAX_TOOLS
    legacy_probes: bool = False
    strict: bool = False
    user_agent: str = USER_AGENT

    @property
    def check_budget(self) -> float:
        """Per-check wall clock: ``CHECK_BUDGET_FACTOR x timeout``."""
        return wire.CHECK_BUDGET_FACTOR * self.timeout

    @property
    def secrets(self) -> tuple[str, ...]:
        """Values that must never reach a console, transcript or report, longest first.

        The ``--token`` and every ``-H`` value, plus their percent-encoded and
        Base64 forms (see :func:`mcpcheck.wire.secret_needles`), so a server that
        reflects our headers into a redirect URL or an error message still leaks
        nothing.
        """
        values = [self.token] if self.token else []
        values.extend(v for _, v in self.extra_headers if v)
        return wire.secret_needles(values)


class ServerUnreachable(Exception):  # noqa: N818 — name fixed by PLAN.md §6.5
    """Connect refused / DNS / TLS / unsupported scheme: the run exits 2."""

    def __init__(self, url: str, cause: BaseException) -> None:
        self.url = url
        self.cause = cause
        detail = str(cause).strip() or type(cause).__name__
        super().__init__(f"cannot connect to {url}: {type(cause).__name__}: {detail}")


@dataclass(frozen=True)
class RawReply:
    """One HTTP reply, parsed but unjudged."""

    status: int
    headers: httpx2.Headers
    media_type: str
    charset: str | None
    messages: tuple[dict[str, Any], ...]
    raw: bytes
    truncated: bool
    parse_error: str | None
    elapsed_ms: int
    request_id: int | str | None
    sse_event_ids: int = 0
    stream_terminated: bool | None = None
    redirects_followed: int = 0
    redirect_blocked: str | None = None
    error: str | None = None
    method: str = ""
    url: str = ""

    def response_for(self, req_id: int | str | None) -> dict[str, Any] | None:
        """First message carrying ``result``/``error`` whose id equals ``req_id`` or is null."""
        for msg in self.messages:
            if not wire.is_response(msg):
                continue
            mid = msg.get("id")
            if mid is None or (mid == req_id and type(mid) is type(req_id)):
                return msg
        return None

    @property
    def response(self) -> dict[str, Any] | None:
        """The response to *our* request (id-matched, or a null-id error)."""
        return self.response_for(self.request_id)

    @property
    def any_response(self) -> dict[str, Any] | None:
        """First response message regardless of id (for id-mismatch findings)."""
        return next((m for m in self.messages if wire.is_response(m)), None)

    @property
    def result(self) -> dict[str, Any] | None:
        """``response["result"]`` when it is an object, else ``None``."""
        resp = self.response
        if resp is None:
            return None
        res = resp.get("result")
        return res if isinstance(res, dict) else None

    @property
    def rpc_error(self) -> tuple[int, str, Any] | None:
        """``(code, message, data)`` of the response error; ``None`` for a non-int code."""
        resp = self.response
        if resp is None:
            return None
        return wire.rpc_error_of(resp)

    @property
    def notifications(self) -> tuple[dict[str, Any], ...]:
        """Messages with ``method`` and no id."""
        return tuple(m for m in self.messages if wire.is_notification(m))

    @property
    def server_requests(self) -> tuple[dict[str, Any], ...]:
        """Messages with ``method`` **and** id (T:114 MUST NOT appear on the stream)."""
        return tuple(m for m in self.messages if wire.is_server_request(m))

    @property
    def session_ids(self) -> list[str]:
        """Every ``Mcp-Session-Id`` response header value."""
        return self.headers.get_list(H_SESSION_ID)

    @property
    def is_sse(self) -> bool:
        """Whether the body was a ``text/event-stream``."""
        return self.media_type == MEDIA_SSE


@dataclass
class Exchange:
    """One recorded request/response pair (redacted) for the ``--verbose`` transcript."""

    seq: int
    connection: str
    check: str
    purpose: str
    verb: str
    url: str
    rpc_method: str
    request_headers: list[tuple[str, str]]
    request_body: str
    status: int
    response_headers: list[tuple[str, str]]
    response_body: str
    elapsed_ms: int
    error: str | None = None
    tags: list[str] = field(default_factory=list)
    legacy: bool = False
    """Legacy-era shape: ``_meta`` omitted *or* ``MCP-Protocol-Version`` omitted (T:277-281)."""
    session_ids_sent: list[str] = field(default_factory=list)
    session_ids_received: list[str] = field(default_factory=list)


class Transcript:
    """Ordered list of every exchange of a run; ``current_check`` tags new entries."""

    def __init__(self) -> None:
        self.exchanges: list[Exchange] = []
        self.current_check: str = ""

    def __len__(self) -> int:
        return len(self.exchanges)

    def __iter__(self) -> Any:
        return iter(self.exchanges)

    def next_seq(self) -> int:
        """Sequence number for the next exchange (1-based)."""
        return len(self.exchanges) + 1

    def append(self, exchange: Exchange) -> None:
        """Record an exchange."""
        self.exchanges.append(exchange)

    def for_check(self, name: str) -> list[Exchange]:
        """Exchanges tagged with ``check == name``."""
        return [e for e in self.exchanges if e.check == name]


@dataclass
class _Accum:
    """Mutable state of one request so a timed-out request still yields a partial reply."""

    status: int = 0
    headers: httpx2.Headers = field(default_factory=httpx2.Headers)
    media_type: str = ""
    charset: str | None = None
    raw: bytearray = field(default_factory=bytearray)
    truncated: bool = False
    messages: list[dict[str, Any]] = field(default_factory=list)
    parse_error: str | None = None
    sse_event_ids: int = 0
    stream_terminated: bool | None = None
    redirects_followed: int = 0
    redirect_blocked: str | None = None
    error: str | None = None
    response_seen: bool = False


_TAMPER_HEADERS = frozenset(
    {
        "accept",
        "content-type",
        "mcp-protocol-version",
        "mcp-method",
        "mcp-name",
        "origin",
        "authorization",
        "mcp-session-id",
    }
)


class RawClient:
    """One ``httpx2.AsyncClient`` per instance; a new instance == a new TCP connection pool."""

    def __init__(
        self,
        cfg: ClientConfig,
        transcript: Transcript,
        *,
        label: str = "main",
        send_token: bool = True,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.cfg = cfg
        self.transcript = transcript
        self.label = label
        self.send_token = send_token
        self._next_id = 1
        self._http = httpx2.AsyncClient(
            timeout=httpx2.Timeout(cfg.timeout),
            verify=cfg.verify_tls,
            follow_redirects=False,
            transport=transport,
            headers={"user-agent": cfg.user_agent},
        )

    # ------------------------------------------------------------------ lifecycle
    async def __aenter__(self) -> RawClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the connection pool."""
        await self._http.aclose()

    # ------------------------------------------------------------------ public API
    def next_request_id(self) -> int:
        """Allocate the next integer request id (1-based per client)."""
        rid = self._next_id
        self._next_id += 1
        return rid

    def base_headers(self) -> dict[str, str]:
        """Authorization / Origin / ``-H`` headers of this client (lowercase keys)."""
        headers: dict[str, str] = {}
        if self.cfg.token and self.send_token:
            headers[H_AUTHORIZATION] = f"Bearer {self.cfg.token}"
        if self.cfg.origin is not None:
            headers[H_ORIGIN] = self.cfg.origin
        for name, value in self.cfg.extra_headers:
            headers[name.lower()] = value
        return headers

    async def post(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        meta_version: str | None = PROTOCOL_VERSION,
        header_version: str | Unset | None = UNSET,
        headers: Mapping[str, str] | None = None,
        omit_headers: Iterable[str] = (),
        omit_meta_keys: Iterable[str] = (),
        req_id: int | str | Unset | None = UNSET,
        client_info: bool = True,
        wait_for_eof: bool = False,
        purpose: str = "",
        check: str = "",
    ) -> RawReply:
        """Send one JSON-RPC request and return the parsed reply.

        ``meta_version=None`` omits ``_meta`` (legacy-shaped request);
        ``header_version`` defaults to ``meta_version`` and ``None`` omits the
        ``MCP-Protocol-Version`` header; ``req_id=None`` sends a notification.
        Tampering keyword arguments are rejected on the ``main`` client.

        Raises:
            ServerUnreachable: connect refused / DNS / TLS / unsupported scheme.
        """
        omit_headers = tuple(omit_headers)
        omit_meta_keys = tuple(omit_meta_keys)
        overrides = {k.lower(): v for k, v in (headers or {}).items()}
        if self.label == "main":
            tampering = (
                meta_version != PROTOCOL_VERSION
                or not isinstance(header_version, _Unset)
                or omit_headers
                or omit_meta_keys
                or not client_info
                or any(k in _TAMPER_HEADERS for k in overrides)
            )
            if tampering:
                msg = (
                    "tampering kwargs are not allowed on the shared 'main' client; use ctx.fresh()"
                )
                raise RuntimeError(msg)

        rid: int | str | None = self.next_request_id() if isinstance(req_id, _Unset) else req_id
        body = wire.envelope(
            method,
            params,
            req_id=rid,
            meta_version=meta_version,
            client_info_=client_info,
            omit_meta_keys=omit_meta_keys,
        )
        hv: str | None = meta_version if isinstance(header_version, _Unset) else header_version
        sent_headers = wire.merge_headers(
            {
                **wire.default_headers(body, header_version=hv, user_agent=self.cfg.user_agent),
                **self.base_headers(),
            },
            overrides,
            omit_headers,
        )
        content = wire.encode_body(body)
        tags = self._tags(sent_headers, method, meta_version)
        return await self._request(
            "POST",
            self.cfg.url,
            sent_headers,
            content,
            rid,
            method,
            wait_for_eof=wait_for_eof,
            purpose=purpose,
            check=check,
            tags=tags,
            # a header-less request MAY be routed to a 2025-03-26 path on dual-era servers
            legacy=meta_version is None or hv is None,
            body_text=content.decode("utf-8"),
        )

    async def http(
        self,
        verb: Literal["GET", "OPTIONS"],
        *,
        headers: Mapping[str, str] | None = None,
        omit_headers: Iterable[str] = (),
        purpose: str = "",
        check: str = "",
    ) -> RawReply:
        """Headers-only probe: read status + headers (+ at most one SSE event) and close.

        Bounded by ``anyio.fail_after(min(timeout, 5))`` because a header-less
        GET on an SDK server opens an endless legacy SSE stream.
        """
        base = {
            wire.H_ACCEPT: wire.ACCEPT_VALUE,
            H_PROTOCOL_VERSION: PROTOCOL_VERSION,
            wire.H_USER_AGENT: self.cfg.user_agent,
            **self.base_headers(),
        }
        sent_headers = wire.merge_headers(base, headers, omit_headers)
        tags = self._tags(sent_headers, None, PROTOCOL_VERSION)
        return await self._request(
            verb,
            self.cfg.url,
            sent_headers,
            b"",
            None,
            "",
            wait_for_eof=False,
            purpose=purpose,
            check=check,
            tags=tags,
            legacy=False,
            body_text="",
            budget=min(self.cfg.timeout, HTTP_PROBE_BUDGET_S),
            headers_only=True,
        )

    # ------------------------------------------------------------------ internals
    def _tags(
        self, headers: Mapping[str, str], method: str | None, meta_version: str | None
    ) -> list[str]:
        tags: list[str] = []
        if H_ORIGIN in headers:
            tags.append(f"[Origin: {headers[H_ORIGIN]}]")
        if H_PROTOCOL_VERSION not in headers:
            tags.append("[no MCP-Protocol-Version]")
        elif headers[H_PROTOCOL_VERSION] != PROTOCOL_VERSION:
            tags.append(f"[MCP-Protocol-Version: {headers[H_PROTOCOL_VERSION]}]")
        if method is not None:
            if H_METHOD not in headers:
                tags.append("[no Mcp-Method]")
            elif headers[H_METHOD] != method:
                tags.append(f"[Mcp-Method: {headers[H_METHOD]}]")
            if meta_version is None:
                tags.append("[no _meta]")
            elif meta_version != PROTOCOL_VERSION:
                tags.append(f"[_meta: {meta_version}]")
        if H_SESSION_ID in headers:
            tags.append(f"[Mcp-Session-Id: {headers[H_SESSION_ID]}]")
        return tags

    async def _request(
        self,
        verb: str,
        url: str,
        headers: dict[str, str],
        content: bytes,
        rid: int | str | None,
        method: str,
        *,
        wait_for_eof: bool,
        purpose: str,
        check: str,
        tags: list[str],
        legacy: bool,
        body_text: str,
        budget: float | None = None,
        headers_only: bool = False,
    ) -> RawReply:
        acc = _Accum()
        t0 = time.perf_counter()
        unreachable: BaseException | None = None
        try:
            with anyio.fail_after(budget if budget is not None else self.cfg.timeout):
                await self._send(verb, url, headers, content, rid, acc, wait_for_eof, headers_only)
        except TimeoutError:
            acc.error = f"request exceeded the {budget or self.cfg.timeout:g}s total budget"
        except (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.UnsupportedProtocol) as exc:
            unreachable = exc
            acc.error = f"{type(exc).__name__}: {exc}"
        except (
            httpx2.TransportError,
            httpx2.DecodingError,  # bad Content-Encoding body: a RequestError, not a TransportError
            httpx2.StreamError,
            httpx2.InvalidURL,
        ) as exc:
            acc.error = f"{type(exc).__name__}: {str(exc).strip() or 'transport failure'}"
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        reply = RawReply(
            status=acc.status,
            headers=acc.headers,
            media_type=acc.media_type,
            charset=acc.charset,
            messages=tuple(acc.messages),
            raw=bytes(acc.raw),
            truncated=acc.truncated,
            parse_error=acc.parse_error,
            elapsed_ms=elapsed_ms,
            request_id=rid,
            sse_event_ids=acc.sse_event_ids,
            stream_terminated=acc.stream_terminated,
            redirects_followed=acc.redirects_followed,
            redirect_blocked=acc.redirect_blocked,
            error=acc.error,
            method=method,
            url=url,
        )
        self._record(verb, url, headers, body_text, reply, purpose, check, tags, legacy)
        if unreachable is not None:
            raise ServerUnreachable(url, unreachable)
        return reply

    async def _send(
        self,
        verb: str,
        url: str,
        headers: dict[str, str],
        content: bytes,
        rid: int | str | None,
        acc: _Accum,
        wait_for_eof: bool,
        headers_only: bool,
    ) -> None:
        current = url
        while True:
            async with self._http.stream(
                verb, current, content=content or None, headers=headers
            ) as resp:
                acc.status = resp.status_code
                acc.headers = resp.headers
                acc.media_type, acc.charset = wire.classify_content_type(
                    resp.headers.get("content-type")
                )
                if resp.status_code in (307, 308) and resp.headers.get("location"):
                    target = urljoin(current, resp.headers["location"])
                    if (
                        wire.same_origin(target, self.cfg.url)
                        and acc.redirects_followed < self.cfg.max_redirects
                    ):
                        acc.redirects_followed += 1
                        current = target
                        continue
                    acc.redirect_blocked = target
                    return
                if 300 <= resp.status_code <= 399 and resp.headers.get("location"):
                    acc.redirect_blocked = urljoin(current, resp.headers["location"])
                    return
                if headers_only:
                    if acc.media_type == MEDIA_SSE:
                        await self._read_sse(resp, acc, rid, wait_for_eof=False, max_events=1)
                    return
                if acc.media_type == MEDIA_SSE:
                    await self._read_sse(resp, acc, rid, wait_for_eof=wait_for_eof)
                else:
                    await self._read_body(resp, acc)
                return

    async def _read_body(self, resp: httpx2.Response, acc: _Accum) -> None:
        async for chunk in resp.aiter_bytes():
            if len(acc.raw) + len(chunk) > MAX_BODY_BYTES:
                acc.raw += chunk[: MAX_BODY_BYTES - len(acc.raw)]
                acc.truncated = True
                acc.error = f"body exceeds the {MAX_BODY_BYTES // (1024 * 1024)} MiB cap"
                return
            acc.raw += chunk
        if acc.media_type == MEDIA_JSON:
            if acc.charset not in (None, "utf-8", "utf8"):
                acc.parse_error = f"charset {acc.charset} is not utf-8"
                return
            messages, err = wire.parse_json_body(bytes(acc.raw))
            acc.messages.extend(messages)
            acc.parse_error = err

    async def _read_sse(
        self,
        resp: httpx2.Response,
        acc: _Accum,
        rid: int | str | None,
        *,
        wait_for_eof: bool,
        max_events: int | None = None,
    ) -> None:
        decoder = wire.SseDecoder()
        events_seen = 0
        stop = False

        def consume(events: list[wire.SseEvent]) -> None:
            nonlocal events_seen, stop
            for ev in events:
                if stop:
                    return
                events_seen += 1
                msgs, err = wire.sse_messages([ev])
                if err and acc.parse_error is None:
                    acc.parse_error = err
                for msg in msgs:
                    acc.messages.append(msg)
                    if wire.is_response(msg) and _id_matches(msg.get("id"), rid):
                        acc.response_seen = True
                        stop = True
                if max_events is not None and events_seen >= max_events:
                    stop = True

        def take(chunk: bytes) -> bool:
            """Buffer one chunk (respecting the body cap); return False when the cap was hit."""
            if len(acc.raw) + len(chunk) > MAX_BODY_BYTES:
                acc.truncated = True
                acc.error = f"body exceeds the {MAX_BODY_BYTES // (1024 * 1024)} MiB cap"
                return False
            acc.raw += chunk
            consume(decoder.feed(chunk))
            return True

        chunks = resp.aiter_bytes()  # one generator: the grace phase continues where we stopped
        exhausted = False
        async for chunk in chunks:
            if not take(chunk) or stop:
                break
        else:
            exhausted = True
        if exhausted:
            consume(decoder.flush())
            acc.stream_terminated = True
        acc.sse_event_ids = decoder.id_fields
        if decoder.decode_error and acc.parse_error is None:
            acc.parse_error = decoder.decode_error
        if acc.stream_terminated or acc.error is not None:
            return
        if wait_for_eof and acc.response_seen:
            acc.stream_terminated = False
            with anyio.move_on_after(SSE_TERMINATION_GRACE_S):
                async for chunk in chunks:
                    if len(acc.raw) + len(chunk) <= MAX_BODY_BYTES:
                        acc.raw += chunk
                        decoder.feed(chunk)
                acc.stream_terminated = True

    def _record(
        self,
        verb: str,
        url: str,
        headers: dict[str, str],
        body_text: str,
        reply: RawReply,
        purpose: str,
        check: str,
        tags: list[str],
        legacy: bool,
    ) -> None:
        secrets = self.cfg.secrets
        resp_body: str
        if reply.raw:
            text, err = wire.decode_utf8(reply.raw[:TRANSCRIPT_BODY_LIMIT])
            resp_body = text if text is not None else f"<{len(reply.raw)} bytes, {err}>"
        else:
            resp_body = ""
        self.transcript.append(
            Exchange(
                seq=self.transcript.next_seq(),
                connection=self.label,
                check=check or self.transcript.current_check,
                purpose=purpose,
                verb=verb,
                url=wire.scrub(url, secrets),
                rpc_method=reply.method,
                request_headers=wire.redact(headers, secrets),
                request_body=wire.scrub(body_text, secrets),
                status=reply.status,
                response_headers=wire.redact(
                    [(k.decode("latin-1"), v.decode("latin-1")) for k, v in reply.headers.raw],
                    secrets,
                ),
                response_body=wire.scrub(resp_body, secrets),
                elapsed_ms=reply.elapsed_ms,
                error=wire.scrub(reply.error, secrets) if reply.error else None,
                tags=tags,
                legacy=legacy,
                session_ids_sent=[v for k, v in headers.items() if k == H_SESSION_ID],
                session_ids_received=list(reply.session_ids),
            )
        )


def _id_matches(got: Any, want: int | str | None) -> bool:
    if got is None:
        return True
    return bool(got == want and type(got) is type(want))


__all__ = [
    "UNSET",
    "ClientConfig",
    "Exchange",
    "ProbePolicy",
    "RawClient",
    "RawReply",
    "ServerUnreachable",
    "Transcript",
    "Unset",
]
