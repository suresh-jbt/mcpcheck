"""Pure wire-level helpers: envelopes, headers, body parsing, classification, redaction.

Nothing in this module performs I/O or imports the SDK at module import time
(so :func:`mcpcheck.sdk_compat.require_sdk` can still produce an actionable
error). The literal protocol constants below are asserted equal to the SDK's own
constants in ``tests/test_sdk_contract.py``. See PLAN.md §6.4-§6.6 and §6.13.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, quote_plus, urlsplit

from mcpcheck import __version__

if TYPE_CHECKING:
    from collections.abc import Iterable

    from mcpcheck.client import RawReply

# --------------------------------------------------------------------------- constants (§6.4)

PROTOCOL_VERSION = "2026-07-28"
JSONRPC_VERSION = "2.0"
MODERN_ERROR_CODES: frozenset[int] = frozenset({-32020, -32021, -32022})
HEADER_MISMATCH = -32020
MISSING_REQUIRED_CLIENT_CAPABILITY = -32021
UNSUPPORTED_PROTOCOL_VERSION = -32022
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

REQUEST_TIMEOUT = 10.0
CHECK_BUDGET_FACTOR = 10
MAX_BODY_BYTES = 16 * 1024 * 1024
SSE_TERMINATION_GRACE_S = 2.0
HTTP_PROBE_BUDGET_S = 5.0
MAX_PAGES = 100
WALK_BYTE_BUDGET = 64 * 1024 * 1024
"""Cumulative body bytes one ``nextCursor`` walk may fetch before it stops (inconclusive, never a defect)."""
SESSION_CONNECTIONS = 3
MAX_TOOLS = 20
TTL_SANITY_MS = 2_592_000_000
MAX_SCHEMA_DEPTH = 64
MAX_SCHEMA_NODES = 5000
MAX_ITEMS_SHOWN = 5
TRANSCRIPT_BODY_LIMIT = 64 * 1024
UNKNOWN_TOOL_NAME = "mcpcheck__nonexistent_tool_7f3a"
EVIL_ORIGIN = "https://evil.example.com"
USER_AGENT = f"mcpcheck/{__version__} (+https://github.com/suresh-jbt/mcpcheck)"

# header names (lowercase; httpx2 headers are case-insensitive)
H_ACCEPT = "accept"
H_CONTENT_TYPE = "content-type"
H_PROTOCOL_VERSION = "mcp-protocol-version"
H_METHOD = "mcp-method"
H_NAME = "mcp-name"
H_SESSION_ID = "mcp-session-id"
H_AUTHORIZATION = "authorization"
H_ORIGIN = "origin"
H_USER_AGENT = "user-agent"
ACCEPT_VALUE = "application/json, text/event-stream"
MEDIA_JSON = "application/json"
MEDIA_SSE = "text/event-stream"

# _meta keys (asserted against mcp_types.*_META_KEY)
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"
META_LOG_LEVEL = "io.modelcontextprotocol/logLevel"

# methods whose Mcp-Name header mirrors a body field (T:287-294)
NAMED_METHODS: dict[str, str] = {
    "tools/call": "name",
    "prompts/get": "name",
    "resources/read": "uri",
}

# _meta key grammar (B:332-346)
_LABEL = r"[A-Za-z](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
META_KEY_RE = re.compile(
    rf"^(?:(?:{_LABEL})(?:\.{_LABEL})*/)?(?:[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)?$"
)
RESERVED_PREFIX_RE = re.compile(
    r"^[A-Za-z0-9-]+\.(?:modelcontextprotocol|mcp)(?:\.[A-Za-z0-9-]+)*/"
)

REDACTED = "****"
_SENSITIVE_HEADERS = frozenset(
    {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"}
)
_SENSITIVE_NAME_RE = re.compile(r"token|secret|password", re.IGNORECASE)
MIN_DERIVED_NEEDLE = 8
"""Encoded forms of a secret shorter than this are not scrubbed (they would match noise)."""
JSON_TOO_DEEP = "JSON nests too deeply"
MAX_JSON_DEPTH = 512
"""Deepest nesting mcpcheck parses from a server.

CPython's own limit is not a contract. First depth at which ``json`` raises
``RecursionError``, measured on 2026-10-17::

    3.10.20  996        3.13.5   9999
    3.11.15  996        3.14.7   43389
    3.12.13  9998       3.14 (GitHub runners)  > 100000

3.14 derives it from the available C stack, so it varies by machine: the same
hostile body produced ``JSON_TOO_DEEP`` locally and "JSON array (batching...)"
on CI. A conformance tool whose verdict depends on the interpreter cannot be
used to diff two environments, so the bound is ours.

512 sits below the lowest limit above (996), which makes the ``RecursionError``
branch unreachable and every supported Python agree, and far above anything a
real result reaches -- a pathological 200-level tool schema nests to 405, and
the schemas check caps schemas at ``MAX_SCHEMA_DEPTH`` (64) and reports them
per tool rather than rejecting the whole body.
"""

_JSON_STRING_RE = re.compile(rb'"(?:[^"\\]|\\.)*"', re.DOTALL)
_NON_BRACKET_RE = re.compile(rb"[^\[\]{}]+")


def json_nests_too_deeply(raw: bytes, limit: int = MAX_JSON_DEPTH) -> bool:
    """Whether ``raw`` nests deeper than ``limit``, ignoring string contents.

    Both substitutions run in C, and the surviving string holds only brackets,
    so a 16 MiB body costs two linear scans rather than a Python loop per byte.
    """
    depth = 0
    for char in _NON_BRACKET_RE.sub(b"", _JSON_STRING_RE.sub(b"", raw)):
        if char in (0x5B, 0x7B):  # [ {
            depth += 1
            if depth > limit:
                return True
        else:
            depth -= 1
    return False


# --------------------------------------------------------------------------- envelope / headers


def client_info() -> dict[str, str]:
    """``io.modelcontextprotocol/clientInfo`` value mcpcheck sends."""
    return {"name": "mcpcheck", "version": __version__}


def build_meta(
    meta_version: str,
    *,
    client_info_: bool = True,
    client_capabilities: Mapping[str, Any] | None = None,
    omit_meta_keys: Iterable[str] = (),
) -> dict[str, Any]:
    """Build the per-request ``_meta`` object (B:375-387)."""
    meta: dict[str, Any] = {
        META_PROTOCOL_VERSION: meta_version,
        META_CLIENT_CAPABILITIES: dict(client_capabilities or {}),
    }
    if client_info_:
        meta[META_CLIENT_INFO] = client_info()
    for key in omit_meta_keys:
        meta.pop(key, None)
    return meta


def envelope(
    method: str,
    params: Mapping[str, Any] | None = None,
    *,
    req_id: int | str | None,
    meta_version: str | None = PROTOCOL_VERSION,
    client_info_: bool = True,
    client_capabilities: Mapping[str, Any] | None = None,
    omit_meta_keys: Iterable[str] = (),
) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 request object.

    ``meta_version=None`` omits the ``_meta`` envelope entirely (legacy-shaped
    request); ``req_id=None`` produces a notification (no ``id`` member).
    """
    body: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION}
    if req_id is not None:
        body["id"] = req_id
    body["method"] = method
    merged: dict[str, Any] = dict(params or {})
    if meta_version is not None:
        merged["_meta"] = build_meta(
            meta_version,
            client_info_=client_info_,
            client_capabilities=client_capabilities,
            omit_meta_keys=omit_meta_keys,
        )
    if merged or meta_version is not None:
        body["params"] = merged
    return body


def encode_body(body: Mapping[str, Any]) -> bytes:
    """Serialize a JSON-RPC object compactly as UTF-8 (TI:31)."""
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def mcp_name_value(name: str) -> str:
    """Encode a ``Mcp-Name`` header value via the SDK's sentinel rule (T:491-509)."""
    from mcp.shared.inbound import encode_header_value

    encoded: str = encode_header_value(name)
    return encoded


def default_headers(
    body: Mapping[str, Any],
    *,
    header_version: str | None = PROTOCOL_VERSION,
    user_agent: str = USER_AGENT,
) -> dict[str, str]:
    """Headers every modern POST carries (T:75-76, T:252-253, T:287-294).

    Returned keys are lowercase. ``header_version=None`` omits
    ``MCP-Protocol-Version``.
    """
    headers: dict[str, str] = {
        H_ACCEPT: ACCEPT_VALUE,
        H_CONTENT_TYPE: MEDIA_JSON,
        H_USER_AGENT: user_agent,
    }
    if header_version is not None:
        headers[H_PROTOCOL_VERSION] = header_version
    method = body.get("method")
    if isinstance(method, str):
        headers[H_METHOD] = method
        field_name = NAMED_METHODS.get(method)
        params = body.get("params")
        if field_name and isinstance(params, Mapping):
            value = params.get(field_name)
            if isinstance(value, str):
                headers[H_NAME] = mcp_name_value(value)
    return headers


def merge_headers(
    base: Mapping[str, str],
    overrides: Mapping[str, str] | None = None,
    omit: Iterable[str] = (),
) -> dict[str, str]:
    """Case-insensitively apply overrides then omissions; keys are lowercased."""
    merged = {k.lower(): v for k, v in base.items()}
    for k, v in (overrides or {}).items():
        merged[k.lower()] = v
    for k in omit:
        merged.pop(k.lower(), None)
    return merged


# --------------------------------------------------------------------------- content-type / bodies


def classify_content_type(value: str | None) -> tuple[str, str | None]:
    """Split a Content-Type into ``(media_type, charset)``; both lowercased."""
    if not value:
        return "", None
    parts = [p.strip() for p in value.split(";")]
    media = parts[0].lower()
    charset: str | None = None
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            if k.strip().lower() == "charset":
                charset = v.strip().strip('"').lower() or None
    return media, charset


def decode_utf8(raw: bytes) -> tuple[str | None, str | None]:
    """Strictly decode UTF-8 (with optional BOM); returns ``(text, error)``."""
    try:
        return raw.decode("utf-8-sig"), None
    except UnicodeDecodeError:
        return None, "not utf-8"


def parse_json_body(raw: bytes) -> tuple[tuple[dict[str, Any], ...], str | None]:
    """Parse an ``application/json`` body into JSON-RPC objects.

    Returns ``(messages, parse_error)``. A JSON array is rejected (batching was
    removed; B:48-49), as is any non-object value.
    """
    text, err = decode_utf8(raw)
    if text is None:
        return (), err
    if not text.strip():
        return (), "empty body"
    if json_nests_too_deeply(raw):
        return (), JSON_TOO_DEEP
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return (), "invalid JSON"
    except RecursionError:  # server-controlled nesting depth must never crash the run
        return (), JSON_TOO_DEEP
    if isinstance(value, list):
        return (), "JSON array (batching is not part of 2026-07-28)"
    if not isinstance(value, dict):
        return (), "not a JSON object"
    return (value,), None


@dataclass(frozen=True)
class SseEvent:
    """One server-sent event (WHATWG EventSource framing)."""

    data: str
    event: str = "message"
    id: str | None = None


class SseDecoder:
    r"""Incremental WHATWG-compliant SSE decoder (LF, CRLF and CR line endings).

    Comment lines (``:``) are skipped, multi-line ``data:`` fields are joined
    with a newline, ``id:`` fields are counted (legacy resumability noise).
    """

    def __init__(self) -> None:
        self._buffer = b""
        self._data: list[str] = []
        self._event = ""
        self._id: str | None = None
        self._pending = False
        self.id_fields = 0
        self.decode_error: str | None = None

    def feed(self, chunk: bytes) -> list[SseEvent]:
        """Feed raw bytes; return every event completed by this chunk."""
        self._buffer += chunk
        events: list[SseEvent] = []
        while True:
            m = re.search(rb"\r\n|\r|\n", self._buffer)
            if m is None:
                break
            # a lone CR at the very end may be the first half of CRLF: wait for more
            if m.group() == b"\r" and m.end() == len(self._buffer):
                break
            line = self._buffer[: m.start()]
            self._buffer = self._buffer[m.end() :]
            ev = self._line(line)
            if ev is not None:
                events.append(ev)
        return events

    def flush(self) -> list[SseEvent]:
        """Finish the stream; a trailing unterminated line is processed, a partial event dropped."""
        events: list[SseEvent] = []
        if self._buffer:
            ev = self._line(self._buffer.rstrip(b"\r"))
            self._buffer = b""
            if ev is not None:
                events.append(ev)
        return events

    @property
    def has_partial_event(self) -> bool:
        """True when fields were received without the terminating blank line."""
        return self._pending or bool(self._buffer)

    def _line(self, raw_line: bytes) -> SseEvent | None:
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError:
            self.decode_error = "not utf-8"
            line = raw_line.decode("utf-8", errors="replace")
        if line == "":
            if not self._pending:
                return None
            ev = SseEvent(data="\n".join(self._data), event=self._event or "message", id=self._id)
            self._data, self._event, self._pending = [], "", False
            return ev
        if line.startswith(":"):
            return None
        name, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if name == "data":
            self._data.append(value)
            self._pending = True
        elif name == "event":
            self._event = value
            self._pending = True
        elif name == "id":
            if "\0" not in value:
                self._id = value
                self.id_fields += 1
                self._pending = True
        elif name == "retry":
            self._pending = True
        return None


def parse_sse_events(raw: bytes) -> list[SseEvent]:
    """Decode a complete SSE body into events (pure helper around :class:`SseDecoder`)."""
    dec = SseDecoder()
    events = dec.feed(raw)
    events.extend(dec.flush())
    return events


def sse_messages(events: Iterable[SseEvent]) -> tuple[tuple[dict[str, Any], ...], str | None]:
    """Parse the ``data`` of each event as a JSON-RPC object.

    Events whose data is not a JSON object produce a parse error string (the
    first one is returned) and are skipped.
    """
    messages: list[dict[str, Any]] = []
    error: str | None = None
    for ev in events:
        if not ev.data.strip():
            continue
        if json_nests_too_deeply(ev.data.encode("utf-8", "surrogatepass")):
            error = error or f"{JSON_TOO_DEEP} in SSE data"
            continue
        try:
            value = json.loads(ev.data)
        except json.JSONDecodeError:
            error = error or "invalid JSON in SSE data"
            continue
        except RecursionError:  # server-controlled nesting depth must never crash the run
            error = error or f"{JSON_TOO_DEEP} in SSE data"
            continue
        if isinstance(value, dict):
            messages.append(value)
        else:
            error = error or "SSE data is not a JSON object"
    return tuple(messages), error


# --------------------------------------------------------------------------- message helpers


def is_response(msg: Mapping[str, Any]) -> bool:
    """A JSON-RPC response carries ``result`` or ``error``."""
    return "result" in msg or "error" in msg


def is_server_request(msg: Mapping[str, Any]) -> bool:
    """``method`` and ``id`` together make a request (T:114 MUST NOT appear on the stream)."""
    return "method" in msg and "id" in msg and msg.get("id") is not None


def is_notification(msg: Mapping[str, Any]) -> bool:
    """``method`` without ``id``."""
    return "method" in msg and ("id" not in msg or msg.get("id") is None)


def rpc_error_of(msg: Mapping[str, Any]) -> tuple[int, str, Any] | None:
    """Return ``(code, message, data)`` when ``msg`` is a well-formed error response."""
    err = msg.get("error")
    if not isinstance(err, Mapping):
        return None
    code = err.get("code")
    if isinstance(code, bool) or not isinstance(code, int):
        return None
    message = err.get("message")
    return code, message if isinstance(message, str) else "", err.get("data")


# --------------------------------------------------------------------------- redaction (§6.13)


def is_sensitive_header(name: str) -> bool:
    """Whether a header's value must be masked everywhere."""
    lower = name.lower()
    return lower in _SENSITIVE_HEADERS or bool(_SENSITIVE_NAME_RE.search(lower))


def redact_header_value(name: str, value: str) -> str:
    """Mask a sensitive header value, keeping the auth scheme word for Authorization."""
    if not is_sensitive_header(name):
        return value
    if name.lower() == "authorization" and " " in value.strip():
        scheme = value.strip().split(" ", 1)[0]
        return f"{scheme} {REDACTED}"
    return REDACTED


def secret_needles(values: Iterable[str]) -> tuple[str, ...]:
    """Every form of each secret that a server might echo back, longest first.

    Besides the literal value: its percent-encoded forms (``quote`` and
    ``quote_plus``, as reflected into redirect ``Location`` query strings), its
    Base64 form and the Base64 form of ``Bearer <value>`` (with and without
    ``=`` padding). Derived forms shorter than :data:`MIN_DERIVED_NEEDLE` are
    dropped so that a short header value does not scrub unrelated text.
    """
    needles: set[str] = set()
    for value in values:
        if not value:
            continue
        needles.add(value)
        derived = {quote(value, safe=""), quote_plus(value)}
        for plain in (value, f"Bearer {value}"):
            b64 = base64.b64encode(plain.encode("utf-8")).decode("ascii")
            derived.update({b64, b64.rstrip("=")})
        needles.update(d for d in derived if d != value and len(d) >= MIN_DERIVED_NEEDLE)
    return tuple(sorted(needles, key=lambda n: (-len(n), n)))


def scrub(text: str, secrets: Iterable[str]) -> str:
    """Replace every literal occurrence of each secret with ``****`` (longest secret first)."""
    for secret in sorted(set(secrets), key=lambda n: (-len(n), n)):
        if secret:
            text = text.replace(secret, REDACTED)
    return text


def scrub_value(value: Any, secrets: Iterable[str]) -> Any:
    """Scrub every string inside a JSON-like value (dict keys included).

    While any secret is given, a string held under a mapping key that
    :func:`is_sensitive_header` recognizes (``authorization``, ``x-api-key``,
    ``*token*`` …) is replaced by ``****`` wholesale, because a server that echoes
    our request headers leaks exactly there. The walk is iterative, so
    server-controlled nesting depth cannot overflow the stack; new containers are
    built and the input is left intact. Without secrets the value is returned as is.
    """
    needles = tuple(sorted({n for n in secrets if n}, key=lambda n: (-len(n), n)))
    if not needles:
        return value  # nothing of ours to hide: the server's own header echoes are its business
    root: list[Any] = [value]
    stack: list[tuple[Any, Any]] = [(root, 0)]
    while stack:
        container, key = stack.pop()
        item = container[key]
        if isinstance(item, str):
            container[key] = scrub(item, needles)
        elif isinstance(item, list):
            copy = list(item)
            container[key] = copy
            stack.extend((copy, i) for i in range(len(copy)))
        elif isinstance(item, dict):
            rebuilt: dict[Any, Any] = {}
            for k, v in item.items():
                new_key = scrub(k, needles) if isinstance(k, str) else k
                if isinstance(k, str) and isinstance(v, str) and is_sensitive_header(k):
                    rebuilt[new_key] = REDACTED
                else:
                    rebuilt[new_key] = v
            container[key] = rebuilt
            stack.extend((rebuilt, k) for k in rebuilt)
    return root[0]


def redact(
    headers: Iterable[tuple[str, str]] | Mapping[str, str],
    secrets: Iterable[str] = (),
) -> list[tuple[str, str]]:
    """Redact a header list for display/transcript (names kept, values masked/scrubbed)."""
    pairs = headers.items() if isinstance(headers, Mapping) else headers
    secrets = tuple(secrets)
    out: list[tuple[str, str]] = []
    for name, value in pairs:
        out.append((name, scrub(redact_header_value(name, value), secrets)))
    return out


# --------------------------------------------------------------------------- URLs


def derive_origin(url: str) -> str:
    """``scheme://host[:port]`` of a URL with the host lowercased (RFC 6454 serialization)."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = parts.port
    default = {"http": 80, "https": 443}.get(parts.scheme.lower())
    netloc = host if port is None or port == default else f"{host}:{port}"
    return f"{parts.scheme.lower()}://{netloc}"


def same_origin(a: str, b: str) -> bool:
    """Whether two URLs share scheme, host and effective port."""
    return derive_origin(a) == derive_origin(b)


# --------------------------------------------------------------------------- classification (§6.6)


class OutcomeKind(str, Enum):
    """The twelve reply classes of PLAN.md §6.6."""

    OK = "ok"
    RPC_ERROR = "rpc_error"
    MODERN_REJECTION = "modern_rejection"
    METHOD_NOT_FOUND = "method_not_found"
    NON_MODERN_REJECTION = "non_modern_rejection"
    NOT_MCP_ENDPOINT = "not_mcp_endpoint"
    AUTH_REQUIRED = "auth_required"
    FORBIDDEN = "forbidden"
    SERVER_ERROR = "server_error"
    REDIRECTED = "redirected"
    NOT_JSONRPC_2XX = "not_jsonrpc_2xx"
    TRANSPORT = "transport"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class Outcome:
    """Classification of one :class:`~mcpcheck.client.RawReply`.

    ``status_hint`` is the default status in a non-gate check: ``"evaluate"``
    (look at the content), ``"inconclusive"`` or ``"fail"``. ``detail`` is a
    ready-made ``Not evaluated: …`` summary for inconclusive outcomes.
    """

    kind: OutcomeKind
    status_hint: str
    detail: str
    rpc_code: int | None = None
    rpc_message: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """Shortcut for ``kind is OutcomeKind.OK``."""
        return self.kind is OutcomeKind.OK

    @property
    def inconclusive(self) -> bool:
        """Shortcut for ``status_hint == "inconclusive"``."""
        return self.status_hint == "inconclusive"


_NOT_MCP_STATUSES = frozenset({404, 405, 406, 410, 421})


def _first_error(messages: Iterable[Mapping[str, Any]]) -> tuple[int, str, Any] | None:
    for msg in messages:
        err = rpc_error_of(msg)
        if err is not None:
            return err
    return None


def classify_reply(
    reply: RawReply, *, sent_origin: bool = False, sent_token: bool = False
) -> Outcome:
    """Implement the §6.6 outcome table. Infrastructure is never a failure."""
    status = reply.status
    media = reply.media_type or "(no content-type)"
    rpc = _first_error(reply.messages)
    rpc_code = rpc[0] if rpc else None
    rpc_message = rpc[1] if rpc else ""

    def out(kind: OutcomeKind, hint: str, detail: str, **extra: Any) -> Outcome:
        return Outcome(kind, hint, detail, rpc_code, rpc_message, dict(extra))

    if reply.error is not None and status == 0:
        return out(OutcomeKind.TRANSPORT, "inconclusive", f"Not evaluated: {reply.error}")
    if reply.redirect_blocked is not None:
        return out(
            OutcomeKind.REDIRECTED,
            "inconclusive",
            f"Not evaluated: redirects ({status}) to {reply.redirect_blocked}",
            location=reply.redirect_blocked,
        )
    if reply.error is not None:
        return out(OutcomeKind.TRANSPORT, "inconclusive", f"Not evaluated: {reply.error}")
    if status in (401, 407):
        hint = "token rejected" if sent_token else "pass --token"
        return out(
            OutcomeKind.AUTH_REQUIRED, "inconclusive", f"Not evaluated: HTTP {status} ({hint})"
        )
    if status == 403 and (rpc is None or sent_origin):
        hint = "Origin rejected or insufficient scope" if sent_origin else "try --origin"
        return out(OutcomeKind.FORBIDDEN, "inconclusive", f"Not evaluated: HTTP 403 ({hint})")
    if 500 <= status <= 599:
        return out(OutcomeKind.SERVER_ERROR, "inconclusive", f"Not evaluated: HTTP {status}")
    if 300 <= status <= 399:
        return out(OutcomeKind.REDIRECTED, "inconclusive", f"Not evaluated: redirects ({status})")

    if rpc is not None:
        if status == 400 and rpc_code in MODERN_ERROR_CODES:
            detail = {
                HEADER_MISMATCH: "Not evaluated: well-formed request rejected with -32020 (intermediary?)",
                MISSING_REQUIRED_CLIENT_CAPABILITY: "Not evaluated: server demands a client capability (-32021)",
                UNSUPPORTED_PROTOCOL_VERSION: "Not evaluated: server does not support 2026-07-28",
            }[rpc_code]
            return out(OutcomeKind.MODERN_REJECTION, "inconclusive", detail)
        if status == 404 and rpc_code == METHOD_NOT_FOUND:
            return out(OutcomeKind.METHOD_NOT_FOUND, "evaluate", "method not implemented (-32601)")
        if 200 <= status <= 299 and rpc_code in MODERN_ERROR_CODES:
            return out(
                OutcomeKind.RPC_ERROR, "evaluate", f"JSON-RPC error {rpc_code} on HTTP {status}"
            )
        if rpc_code in MODERN_ERROR_CODES:
            return out(
                OutcomeKind.RPC_ERROR, "evaluate", f"JSON-RPC error {rpc_code} on HTTP {status}"
            )
        return out(
            OutcomeKind.NON_MODERN_REJECTION,
            "inconclusive",
            f"Not evaluated: HTTP {status}, JSON-RPC error {rpc_code} {rpc_message!r}"[:200],
        )

    if 200 <= status <= 299:
        if status == 202 and reply.request_id is not None:
            return out(OutcomeKind.NOT_JSONRPC_2XX, "fail", "HTTP 202 to a JSON-RPC request")
        if reply.media_type not in (MEDIA_JSON, MEDIA_SSE):
            return out(
                OutcomeKind.NOT_JSONRPC_2XX, "fail", f"HTTP {status} {media} is not JSON-RPC"
            )
        if reply.parse_error is not None:
            return out(
                OutcomeKind.NOT_JSONRPC_2XX, "fail", f"HTTP {status} {media}: {reply.parse_error}"
            )
        response = reply.response
        if response is None:
            others = [m for m in reply.messages if is_response(m)]
            if others:
                return out(
                    OutcomeKind.NOT_JSONRPC_2XX,
                    "fail",
                    f"response id {others[0].get('id')!r} does not match request id {reply.request_id!r}",
                    id_mismatch=True,
                )
            return out(OutcomeKind.NOT_JSONRPC_2XX, "fail", "no JSON-RPC response in the body")
        return out(OutcomeKind.OK, "evaluate", f"HTTP {status} {media}")

    if status in _NOT_MCP_STATUSES:
        return out(
            OutcomeKind.NOT_MCP_ENDPOINT,
            "inconclusive",
            f"Not evaluated: no MCP endpoint at URL (HTTP {status} {media})",
        )
    if status == 403:
        return out(OutcomeKind.FORBIDDEN, "inconclusive", "Not evaluated: HTTP 403 (try --origin)")
    body_desc = reply.parse_error or "no JSON-RPC body"
    return out(
        OutcomeKind.NON_MODERN_REJECTION,
        "inconclusive",
        f"Not evaluated: HTTP {status} {media} ({body_desc})",
    )


__all__ = [
    "ACCEPT_VALUE",
    "CHECK_BUDGET_FACTOR",
    "EVIL_ORIGIN",
    "HEADER_MISMATCH",
    "HTTP_PROBE_BUDGET_S",
    "H_ACCEPT",
    "H_AUTHORIZATION",
    "H_CONTENT_TYPE",
    "H_METHOD",
    "H_NAME",
    "H_ORIGIN",
    "H_PROTOCOL_VERSION",
    "H_SESSION_ID",
    "H_USER_AGENT",
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "JSONRPC_VERSION",
    "JSON_TOO_DEEP",
    "MAX_BODY_BYTES",
    "MAX_ITEMS_SHOWN",
    "MAX_PAGES",
    "MAX_SCHEMA_DEPTH",
    "MAX_SCHEMA_NODES",
    "MAX_TOOLS",
    "MEDIA_JSON",
    "MEDIA_SSE",
    "META_CLIENT_CAPABILITIES",
    "META_CLIENT_INFO",
    "META_KEY_RE",
    "META_LOG_LEVEL",
    "META_PROTOCOL_VERSION",
    "META_SERVER_INFO",
    "METHOD_NOT_FOUND",
    "MIN_DERIVED_NEEDLE",
    "MISSING_REQUIRED_CLIENT_CAPABILITY",
    "MODERN_ERROR_CODES",
    "NAMED_METHODS",
    "PARSE_ERROR",
    "PROTOCOL_VERSION",
    "REDACTED",
    "REQUEST_TIMEOUT",
    "RESERVED_PREFIX_RE",
    "SESSION_CONNECTIONS",
    "SSE_TERMINATION_GRACE_S",
    "TRANSCRIPT_BODY_LIMIT",
    "TTL_SANITY_MS",
    "UNKNOWN_TOOL_NAME",
    "UNSUPPORTED_PROTOCOL_VERSION",
    "USER_AGENT",
    "WALK_BYTE_BUDGET",
    "Outcome",
    "OutcomeKind",
    "SseDecoder",
    "SseEvent",
    "build_meta",
    "classify_content_type",
    "classify_reply",
    "client_info",
    "decode_utf8",
    "default_headers",
    "derive_origin",
    "encode_body",
    "envelope",
    "is_notification",
    "is_response",
    "is_sensitive_header",
    "is_server_request",
    "mcp_name_value",
    "merge_headers",
    "parse_json_body",
    "parse_sse_events",
    "redact",
    "redact_header_value",
    "rpc_error_of",
    "same_origin",
    "scrub",
    "scrub_value",
    "secret_needles",
    "sse_messages",
]
