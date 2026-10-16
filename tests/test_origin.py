"""Tests for the ``origin`` check (PLAN.md §7.7 part 9, §10.4 origin rows, §10.5).

L1: verdict logic over ``ScriptedServer`` (MockTransport), parametrised JSON/SSE.
L2: real wire shapes through the ASGI fixtures (reference app + misbehaving Toggles).
L3: hazards over TCP (connection reset, hang, fresh connections, redirects) and the SDK matrix.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx2
import pytest

from mcpcheck import wire
from mcpcheck.checks.base import CheckContext, Findings
from mcpcheck.checks.origin import (
    PROBE_METHOD,
    Baseline,
    OriginCheck,
    Probe,
    ProbeOutcome,
    choose_baseline,
    classify,
)
from mcpcheck.client import ClientConfig, RawClient, RawReply, Transcript
from mcpcheck.models import Level, Status
from tests.conftest import ASGI_BASE_URL
from tests.fixtures import misbehaving_server, reference_server
from tests.fixtures.misbehaving_server import Toggles
from tests.helpers.scripted import Route, ScriptedServer, error_response, jsonrpc_error, ok

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator

    from mcpcheck.models import CheckResult

pytestmark = pytest.mark.anyio

URL = "http://127.0.0.1:1/mcp"
SAME = "http://127.0.0.1:1"
EVIL = wire.EVIL_ORIGIN
APP = "https://app.example.com"
REPO_ROOT = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------- harness


class _Ctx(CheckContext):
    """A CheckContext whose fresh connections go through a per-label transport."""

    def __init__(
        self,
        cfg: ClientConfig,
        transcript: Transcript,
        raw: RawClient,
        factory: Callable[[str], httpx2.AsyncBaseTransport | None],
    ) -> None:
        super().__init__(cfg, transcript, raw)
        self._factory = factory
        self.fresh_labels: list[str] = []

    @asynccontextmanager
    async def fresh(self, label: str, *, send_token: bool = True) -> AsyncIterator[RawClient]:
        if label == "main":
            raise ValueError("fresh clients must not be labelled 'main'")
        self.fresh_labels.append(label)
        client = RawClient(
            self.cfg,
            self.transcript,
            label=label,
            send_token=send_token,
            transport=self._factory(label),
        )
        try:
            yield client
        finally:
            await client.aclose()


async def run_origin(
    cfg: ClientConfig,
    factory: Callable[[str], httpx2.AsyncBaseTransport | None] = lambda _label: None,
) -> tuple[CheckResult, Transcript, _Ctx]:
    """Run the origin check the way the runner does and return its result."""
    transcript = Transcript()
    async with RawClient(cfg, transcript, label="main", transport=factory("main")) as raw:
        ctx = _Ctx(cfg, transcript, raw, factory)
        check = OriginCheck()
        f = Findings(check, strict=cfg.strict)
        ctx.current_check = check.name
        transcript.current_check = check.name
        before = len(transcript)
        await check.run(ctx, f)
        return f.to_result(requests=len(transcript) - before), transcript, ctx


def scripted_factory(server: ScriptedServer) -> Callable[[str], httpx2.AsyncBaseTransport]:
    return lambda _label: server.transport()


def asgi_factory(app: Any) -> Callable[[str], httpx2.AsyncBaseTransport]:
    """Distinct ``client_port`` per fresh label (simulates distinct TCP connections)."""
    counter = {"port": 41000}

    def factory(_label: str) -> httpx2.AsyncBaseTransport:
        counter["port"] += 1
        return httpx2.ASGITransport(app, client=("127.0.0.1", counter["port"]))

    return factory


def codes_of(result: CheckResult) -> dict[str, Level]:
    return {f.code: f.level for f in (*result.failures, *result.warnings, *result.info)}


def discover_result() -> dict[str, Any]:
    return {
        "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "scripted", "version": "0.0.1"}},
        "resultType": "complete",
        "supportedVersions": ["2026-07-28"],
        "capabilities": {"tools": {}},
        "ttlMs": 60000,
        "cacheScope": "public",
    }


def forbidden() -> httpx2.Response:
    """The SDK shape: text body, no content-type."""
    return httpx2.Response(403, content=b"Invalid Origin header")


def origin_server(mode: str, *, sse: bool = False) -> ScriptedServer:
    """A scripted server whose ``server/discover`` reacts to the Origin header per ``mode``."""

    def foreign(origin: str | None) -> bool:
        if origin is None:
            return False
        if mode == "accept_null" and origin == "null":
            return False
        return origin not in (SAME, APP)

    def discover(body: dict[str, Any], req: httpx2.Request) -> httpx2.Response:
        origin = req.headers.get("origin")
        if mode == "all_401":
            return httpx2.Response(401, headers={"www-authenticate": "Bearer"})
        if mode == "all_403":
            return forbidden()
        if mode == "insufficient_scope":
            return httpx2.Response(
                403, headers={"www-authenticate": 'Bearer error="insufficient_scope"'}
            )
        if mode == "all_500":
            return httpx2.Response(500, content=b"boom")
        if mode == "all_429":
            return httpx2.Response(429, headers={"retry-after": "1"})
        if mode == "all_redirect":
            return httpx2.Response(307, headers={"location": "https://elsewhere.example/mcp"})
        if mode == "all_202":
            return httpx2.Response(202)
        if mode == "allowlist_app_origin" and origin is not None and origin != APP:
            return forbidden()
        if mode == "require_origin" and origin is None:
            return forbidden()
        if mode == "same_origin_400" and origin == SAME:
            return error_response(body, wire.INVALID_REQUEST, "Origin not allowed", status=400)
        if foreign(origin):
            if mode in ("sdk_like", "allowlist_app_origin", "require_origin", "accept_null"):
                return forbidden()
            if mode == "reject_400_jsonrpc":
                return error_response(body, wire.INVALID_REQUEST, "Origin not allowed", status=400)
            if mode == "reject_200_jsonrpc_error":
                return error_response(
                    body, wire.INVALID_REQUEST, "Origin not allowed", status=200, sse=sse
                )
            if mode == "reject_403_jsonrpc_null_id":
                return error_response(
                    body, wire.INVALID_REQUEST, "Origin not allowed", status=403, null_id=True
                )
            if mode == "reject_403_jsonrpc_with_id":
                return error_response(body, wire.INVALID_REQUEST, "Origin not allowed", status=403)
            if mode == "redirect_evil":
                return httpx2.Response(302, headers={"location": "/mcp-redirected"})
            if mode == "reset_evil":
                raise httpx2.RemoteProtocolError("peer closed connection without sending body")
            if mode == "hang_evil":
                raise httpx2.ReadTimeout("timed out")
            if mode == "evil_202":
                return httpx2.Response(202)
            if mode == "evil_html":
                return httpx2.Response(
                    200, headers={"content-type": "text/html"}, content=b"<h1>no</h1>"
                )
            if mode == "evil_500":
                return httpx2.Response(500, content=b"boom")
            if mode == "evil_401":
                return httpx2.Response(401, headers={"www-authenticate": "Bearer"})
            if mode == "evil_404_html":
                return httpx2.Response(
                    404, headers={"content-type": "text/html"}, content=b"<h1>404</h1>"
                )
            if mode == "evil_id_mismatch":
                return httpx2.Response(
                    200, json={"jsonrpc": "2.0", "id": "other", "result": discover_result()}
                )
            if mode != "accept_all":
                return forbidden()  # every other mode validates like the SDK
        return ok(body, discover_result(), sse=sse)

    http_routes: dict[str, Callable[[httpx2.Request], httpx2.Response]] = {}
    if mode in ("cors_reflect", "cors_star"):
        value = "*" if mode == "cors_star" else EVIL

        def preflight(req: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(
                204,
                headers={
                    "access-control-allow-origin": req.headers.get("origin", "") and value,
                    "access-control-allow-credentials": "true",
                    "access-control-allow-methods": "POST",
                },
            )

        http_routes["OPTIONS"] = preflight
    routes: dict[str, Route] = {} if mode == "legacy_only" else {PROBE_METHOD: discover}
    return ScriptedServer(routes=routes, http_routes=http_routes)


async def run_scripted(
    mode: str, *, sse: bool = False, **cfg_kw: Any
) -> tuple[CheckResult, ScriptedServer]:
    server = origin_server(mode, sse=sse)
    result, _, _ = await run_origin(ClientConfig(url=URL, **cfg_kw), scripted_factory(server))
    return result, server


# --------------------------------------------------------------------------- L1: what is sent


@pytest.mark.parametrize("sse", [False, True])
async def test_probe_request_shape(sse: bool) -> None:
    result, server = await run_scripted("sdk_like", sse=sse)
    assert result.status is Status.PASS
    assert result.summary == "Origin header validated"
    assert result.requests == 5
    assert server.methods() == [PROBE_METHOD] * 4 + [None]
    evil, same, none, null, pre = server.seen
    for seen in (evil, same, none, null):
        assert seen.request.method == "POST"
        assert seen.headers["mcp-method"] == PROBE_METHOD
        assert seen.headers["mcp-protocol-version"] == wire.PROTOCOL_VERSION
        assert seen.headers["accept"] == wire.ACCEPT_VALUE
        assert seen.headers["connection"] == "close"
        assert "authorization" not in seen.headers
        assert seen.meta is not None
        assert seen.meta[wire.META_PROTOCOL_VERSION] == wire.PROTOCOL_VERSION
        assert wire.META_CLIENT_CAPABILITIES in seen.meta
        assert seen.body is not None
        assert seen.body["id"] == 1  # fresh client per probe → ids restart at 1
    assert evil.headers["origin"] == EVIL
    assert same.headers["origin"] == SAME
    assert "origin" not in none.headers
    assert null.headers["origin"] == "null"
    assert pre.request.method == "OPTIONS"
    assert pre.headers["origin"] == EVIL
    assert pre.headers["access-control-request-method"] == "POST"
    assert pre.headers["access-control-request-headers"] == (
        "content-type,mcp-method,mcp-protocol-version"
    )
    assert pre.raw == b""


async def test_probe_headers_with_token_and_origin_option() -> None:
    server = origin_server("allowlist_app_origin")
    cfg = ClientConfig(url=URL, token="sekrit", origin=APP, extra_headers=(("X-Env", "ci"),))
    result, transcript, ctx = await run_origin(cfg, scripted_factory(server))
    assert result.status is Status.PASS, result.summary
    evil, same, none, null, pre = server.seen
    assert evil.headers["origin"] == EVIL  # --origin does not leak into P1
    assert same.headers["origin"] == APP  # --origin replaces P2's value verbatim
    assert "origin" not in none.headers  # ... and is removed from P3
    assert null.headers["origin"] == "null"
    for seen in server.seen:
        assert seen.headers["authorization"] == "Bearer sekrit"
        assert seen.headers["x-env"] == "ci"
    assert pre.headers["access-control-request-headers"].endswith(",authorization")
    assert ctx.fresh_labels == [
        "origin-evil",
        "origin-same",
        "origin-none",
        "origin-null",
        "origin-preflight",
    ]
    for ex in transcript.for_check("origin"):
        assert "sekrit" not in str(ex.request_headers)
        assert ex.connection != "main"


# --------------------------------------------------------------------------- L1: verdicts


@pytest.mark.parametrize("sse", [False, True])
async def test_pass_sdk_like(sse: bool) -> None:
    result, _ = await run_scripted("sdk_like", sse=sse)
    assert result.status is Status.PASS
    assert not result.failures
    assert not result.warnings
    assert not result.info
    assert "security_best_practices" in result.details
    assert "Origin https://evil.example.com → HTTP 403" in result.details


@pytest.mark.parametrize("sse", [False, True])
async def test_fail_evil_accepted(sse: bool) -> None:
    result, _ = await run_scripted("accept_all", sse=sse)
    assert result.status is Status.FAIL
    assert codes_of(result) == {"origin.evil_accepted": Level.MUST}
    assert result.summary == (
        "Evil Origin accepted: https://evil.example.com got HTTP 200 + result (expected 403)"
    )
    finding = result.failures[0]
    assert finding.subject == "origin:evil"
    assert finding.spec_url is not None
    assert finding.spec_url.endswith("streamable-http#security-&-endpoint")
    assert finding.evidence is not None
    assert finding.evidence["request"] == {
        "method": PROBE_METHOD,
        "origin": EVIL,
        "fresh_connection": True,
    }
    assert finding.evidence["http_status"] == 200
    assert finding.evidence["media_type"] == (wire.MEDIA_SSE if sse else wire.MEDIA_JSON)
    assert finding.evidence["expected"].startswith("HTTP 403 Forbidden")
    assert "disables the automatic Origin validation" in result.details  # SDK footgun hint


@pytest.mark.parametrize("sse", [False, True])
async def test_fail_wrong_status_400_jsonrpc(sse: bool) -> None:
    result, _ = await run_scripted("reject_400_jsonrpc", sse=sse)
    assert result.status is Status.FAIL
    assert codes_of(result) == {"origin.evil_rejected_wrong_status": Level.MUST}
    assert result.summary == "Evil Origin rejected with HTTP 400 (spec requires 403)"
    message = result.failures[0].message
    assert "security property held" in message
    assert "403" in message
    assert result.failures[0].evidence is not None
    assert result.failures[0].evidence["rpc_code"] == wire.INVALID_REQUEST


@pytest.mark.parametrize("sse", [False, True])
async def test_fail_200_jsonrpc_error(sse: bool) -> None:
    result, _ = await run_scripted("reject_200_jsonrpc_error", sse=sse)
    assert result.status is Status.FAIL
    assert codes_of(result) == {"origin.evil_rejected_wrong_status": Level.MUST}
    assert result.summary == (
        "Evil Origin rejected with a JSON-RPC error on HTTP 200 (spec requires 403)"
    )


@pytest.mark.parametrize(
    ("mode", "status"), [("evil_500", 500), ("evil_401", 401), ("evil_404_html", 404)]
)
async def test_fail_wrong_status_http(mode: str, status: int) -> None:
    result, _ = await run_scripted(mode)
    assert result.status is Status.FAIL
    assert codes_of(result) == {"origin.evil_rejected_wrong_status": Level.MUST}
    assert result.summary == f"Evil Origin rejected with HTTP {status} (spec requires 403)"


async def test_pass_403_jsonrpc_error_without_id() -> None:
    result, _ = await run_scripted("reject_403_jsonrpc_null_id")
    assert result.status is Status.PASS
    assert not result.info


async def test_pass_403_jsonrpc_error_with_id_is_info() -> None:
    result, _ = await run_scripted("reject_403_jsonrpc_with_id")
    assert result.status is Status.PASS
    assert codes_of(result) == {"origin.evil_403_body_has_id": Level.BEST_PRACTICE}
    assert result.info[0].subject == "origin:evil"


@pytest.mark.parametrize("sse", [False, True])
async def test_warn_same_origin_rejected(sse: bool) -> None:
    result, _ = await run_scripted("allowlist_app_origin", sse=sse)
    assert result.status is Status.WARN
    assert codes_of(result) == {"origin.same_origin_rejected": Level.BEST_PRACTICE}
    assert result.summary == (
        "Evil Origin rejected (403); same-origin http://127.0.0.1:1 also rejected (403)"
    )
    assert "--origin" in result.warnings[0].message
    assert result.warnings[0].subject == "origin:same"
    # the documented remedy makes it pass
    result2, _ = await run_scripted("allowlist_app_origin", sse=sse, origin=APP)
    assert result2.status is Status.PASS


async def test_warn_same_origin_rejected_with_jsonrpc_error() -> None:
    result, _ = await run_scripted("same_origin_400")
    assert result.status is Status.WARN
    assert codes_of(result) == {"origin.same_origin_rejected": Level.BEST_PRACTICE}
    assert "HTTP 400, JSON-RPC -32600" in result.summary


@pytest.mark.parametrize("sse", [False, True])
async def test_warn_no_origin_rejected(sse: bool) -> None:
    result, _ = await run_scripted("require_origin", sse=sse)
    assert result.status is Status.WARN
    assert codes_of(result) == {"origin.no_origin_rejected": Level.BEST_PRACTICE}
    assert result.summary == "Evil Origin rejected (403); requests without Origin rejected (403)"
    assert result.warnings[0].subject == "origin:none"


@pytest.mark.parametrize("sse", [False, True])
async def test_warn_null_accepted(sse: bool) -> None:
    result, _ = await run_scripted("accept_null", sse=sse)
    assert result.status is Status.WARN
    assert codes_of(result) == {"origin.null_accepted": Level.BEST_PRACTICE}
    assert result.summary == 'Evil Origin rejected (403); "Origin: null" accepted'
    assert result.warnings[0].subject == "origin:null"


@pytest.mark.parametrize("sse", [False, True])
async def test_fail_legacy_only_processed(sse: bool) -> None:
    result, server = await run_scripted("legacy_only", sse=sse)
    assert result.status is Status.FAIL
    assert codes_of(result) == {"origin.not_validated": Level.MUST}
    assert result.summary == (
        "Origin not validated: identical reply with and without Origin https://evil.example.com"
    )
    assert server.methods() == [PROBE_METHOD] * 4 + [None]


async def test_warn_redirect() -> None:
    result, server = await run_scripted("redirect_evil")
    assert result.status is Status.WARN
    assert codes_of(result) == {"origin.evil_redirected": Level.SHOULD}
    assert all(s.request.method in ("POST", "OPTIONS") for s in server.seen)  # never followed
    assert result.warnings[0].evidence is not None
    assert result.warnings[0].evidence["location"].endswith("/mcp-redirected")


async def test_warn_connection_closed() -> None:
    result, _ = await run_scripted("reset_evil")
    assert result.status is Status.WARN
    assert codes_of(result) == {"origin.evil_connection_closed": Level.SHOULD}
    assert "connection closed" in result.summary.lower()


async def test_warn_timeout() -> None:
    result, _ = await run_scripted("hang_evil")
    assert result.status is Status.WARN
    assert codes_of(result) == {"origin.evil_timeout": Level.SHOULD}


@pytest.mark.parametrize("mode", ["evil_202", "evil_html", "evil_id_mismatch"])
async def test_warn_rejection_unconfirmed(mode: str) -> None:
    result, _ = await run_scripted(mode)
    assert result.status is Status.WARN
    assert codes_of(result) == {"origin.evil_rejection_unconfirmed": Level.SHOULD}
    assert "rejection unconfirmed" in result.summary


async def test_inconclusive_401() -> None:
    result, _ = await run_scripted("all_401")
    assert result.status is Status.INCONCLUSIVE
    assert result.summary == "Not evaluated: HTTP 401 on every probe (pass --token)"
    assert not result.failures
    assert not result.warnings
    result2, _ = await run_scripted("all_401", token="t")
    assert result2.summary == "Not evaluated: HTTP 401 on every probe (token rejected)"


async def test_inconclusive_403_everywhere_hints_origin() -> None:
    result, _ = await run_scripted("all_403")
    assert result.status is Status.INCONCLUSIVE
    assert result.summary.startswith("Not evaluated: HTTP 403 on every probe")
    assert "--origin" in result.summary


async def test_inconclusive_insufficient_scope() -> None:
    result, _ = await run_scripted("insufficient_scope")
    assert result.status is Status.INCONCLUSIVE
    assert "HTTP 403" in result.summary


@pytest.mark.parametrize(
    ("mode", "fragment"),
    [
        ("all_500", "HTTP 500"),
        ("all_429", "HTTP 429"),
        ("all_redirect", "redirect"),
        ("all_202", "HTTP 202"),
    ],
)
async def test_inconclusive_control_down(mode: str, fragment: str) -> None:
    result, _ = await run_scripted(mode)
    assert result.status is Status.INCONCLUSIVE
    assert result.summary.startswith("Not evaluated: no baseline response (")
    assert fragment in result.summary
    assert not result.failures
    assert not result.warnings


@pytest.mark.parametrize(("mode", "value"), [("cors_reflect", EVIL), ("cors_star", "*")])
async def test_info_cors_preflight(mode: str, value: str) -> None:
    result, _ = await run_scripted(mode)
    assert result.status is Status.PASS  # info never affects the status
    assert codes_of(result) == {"origin.cors_preflight_permissive": Level.BEST_PRACTICE}
    info = result.info[0]
    assert info.subject == "origin:preflight"
    assert value in info.message
    assert "with credentials" in info.message
    assert info.evidence is not None
    assert info.evidence["request"]["method"] == "OPTIONS"


# --------------------------------------------------------------------------- pure helpers


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://api.example.com:443/mcp", "https://api.example.com"),
        ("https://api.example.com/mcp", "https://api.example.com"),
        ("http://api.example.com:80/mcp", "http://api.example.com"),
        ("http://[::1]:9000/mcp", "http://[::1]:9000"),
        ("HTTP://Example.COM:8080/mcp?x=1#f", "http://example.com:8080"),
        ("http://127.0.0.1:8080/mcp", "http://127.0.0.1:8080"),
        ("https://h.example/", "https://h.example"),
    ],
)
def test_derive_origin(url: str, expected: str) -> None:
    assert wire.derive_origin(url) == expected


def _reply(
    status: int,
    *,
    messages: tuple[dict[str, Any], ...] = (),
    media: str = "",
    raw: bytes = b"",
    error: str | None = None,
    redirect: str | None = None,
    headers: dict[str, str] | None = None,
    request_id: int = 1,
) -> RawReply:
    return RawReply(
        status=status,
        headers=httpx2.Headers(headers or {}),
        media_type=media,
        charset=None,
        messages=messages,
        raw=raw,
        truncated=False,
        parse_error=None,
        elapsed_ms=1,
        request_id=request_id,
        redirect_blocked=redirect,
        error=error,
        method=PROBE_METHOD,
    )


RESULT_MSG: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "result": {"resultType": "complete"}}


@pytest.mark.parametrize(
    ("reply", "outcome", "rpc_code", "has_id"),
    [
        (_reply(403, raw=b"Invalid Origin header"), ProbeOutcome.HTTP_REJECT, None, False),
        (
            _reply(200, messages=(RESULT_MSG,), media="application/json"),
            ProbeOutcome.RESULT,
            None,
            False,
        ),
        (
            _reply(200, messages=(RESULT_MSG,), media="text/event-stream"),
            ProbeOutcome.RESULT,
            None,
            False,
        ),
        (
            _reply(400, messages=(jsonrpc_error(1, -32600, "no"),), media="application/json"),
            ProbeOutcome.RPC_ERROR,
            -32600,
            True,
        ),
        (
            _reply(403, messages=(jsonrpc_error(None, -32600, "no"),), media="application/json"),
            ProbeOutcome.RPC_ERROR,
            -32600,
            False,
        ),
        (
            _reply(200, messages=(jsonrpc_error(1, -32600, "no"),), media="application/json"),
            ProbeOutcome.RPC_ERROR,
            -32600,
            True,
        ),
        (_reply(202), ProbeOutcome.NO_RESULT_2XX, None, False),
        (
            _reply(200, media="text/html", raw=b"<h1>x</h1>"),
            ProbeOutcome.NO_RESULT_2XX,
            None,
            False,
        ),
        (
            _reply(200, messages=({**RESULT_MSG, "id": "other"},), media="application/json"),
            ProbeOutcome.NO_RESULT_2XX,
            None,
            False,
        ),
        (
            _reply(302, redirect="http://127.0.0.1:1/mcp-redirected"),
            ProbeOutcome.REDIRECT,
            None,
            False,
        ),
        (_reply(307, headers={"location": "/x"}), ProbeOutcome.REDIRECT, None, False),
        (
            _reply(0, error="request exceeded the 1s total budget"),
            ProbeOutcome.TRANSPORT,
            None,
            False,
        ),
        (
            _reply(200, media="application/json", error="RemoteProtocolError: peer closed"),
            ProbeOutcome.TRANSPORT,
            None,
            False,
        ),
        (
            _reply(401, headers={"www-authenticate": "Bearer"}),
            ProbeOutcome.HTTP_REJECT,
            None,
            False,
        ),
        (_reply(500, raw=b"boom"), ProbeOutcome.HTTP_REJECT, None, False),
    ],
)
def test_classify_matrix(
    reply: RawReply, outcome: ProbeOutcome, rpc_code: int | None, has_id: bool
) -> None:
    probe = classify(reply, "origin:evil", EVIL)
    assert probe.outcome is outcome
    assert probe.rpc_code == rpc_code
    assert probe.rpc_has_id is has_id
    assert probe.status == reply.status
    assert probe.id == "origin:evil"
    assert probe.origin == EVIL
    assert probe.rejected is (outcome is not ProbeOutcome.RESULT)
    assert probe.describe()


def test_classify_details() -> None:
    timeout = classify(_reply(0, error="request exceeded the 1s total budget"), "origin:evil", EVIL)
    assert timeout.timed_out
    closed = classify(_reply(200, error="RemoteProtocolError: peer closed"), "origin:evil", EVIL)
    assert not closed.timed_out
    big = classify(_reply(403, raw=b"x" * 1000), "origin:evil", EVIL)
    assert len(big.body_excerpt) <= 200
    assert big.evidence(got="g", expected="e")["body_excerpt"].endswith("…")
    bad = classify(_reply(403, raw=b"\xff\xfe"), "origin:evil", EVIL)
    assert "not utf-8" in bad.body_excerpt
    cors = classify(
        _reply(
            204,
            headers={
                "access-control-allow-origin": "*",
                "access-control-allow-credentials": "true",
            },
        ),
        "origin:preflight",
        EVIL,
    )
    assert cors.allow_origin == "*"
    assert cors.allow_credentials == "true"
    none = classify(
        _reply(200, messages=(RESULT_MSG,), media="application/json"), "origin:none", None
    )
    assert none.evidence(got="g", expected="e")["request"]["origin"] is None
    assert "rpc_code" not in none.evidence(got="g", expected="e")


def _probe(
    outcome: ProbeOutcome, status: int, rpc_code: int | None = None, www: str | None = None
) -> Probe:
    return Probe(
        id="origin:x",
        origin=None,
        outcome=outcome,
        status=status,
        rpc_code=rpc_code,
        rpc_has_id=rpc_code is not None,
        media_type="",
        www_authenticate=www,
        location=None,
        body_excerpt="",
        error=None,
        elapsed_ms=1,
    )


def test_choose_baseline() -> None:
    result = _probe(ProbeOutcome.RESULT, 200)
    reject = _probe(ProbeOutcome.HTTP_REJECT, 403)
    unauth = _probe(ProbeOutcome.HTTP_REJECT, 401)
    legacy = _probe(ProbeOutcome.RPC_ERROR, 404, -32601)
    down = _probe(ProbeOutcome.HTTP_REJECT, 500)
    assert choose_baseline(result, result) == (Baseline.RESULT, result)
    assert choose_baseline(reject, result)[0] is Baseline.RESULT  # P3 preferred
    assert choose_baseline(result, reject) == (Baseline.RESULT, result)  # P2 as fallback
    assert choose_baseline(unauth, unauth) == (Baseline.AUTH, None)
    assert choose_baseline(unauth, down) == (Baseline.AUTH, None)  # 401 on P2 alone suffices
    assert choose_baseline(reject, reject) == (Baseline.AUTH, None)
    scope = _probe(ProbeOutcome.HTTP_REJECT, 403, www='Bearer error="insufficient_scope"')
    assert choose_baseline(down, scope) == (Baseline.AUTH, None)
    assert choose_baseline(down, reject) == (Baseline.DOWN, None)  # 403 only on P3: not AUTH
    assert choose_baseline(legacy, legacy) == (Baseline.RPC_LAYER, legacy)
    assert choose_baseline(down, down) == (Baseline.DOWN, None)
    assert legacy.processed_like(_probe(ProbeOutcome.RPC_ERROR, 404, -32601))
    assert not legacy.processed_like(_probe(ProbeOutcome.RPC_ERROR, 400, -32601))


# --------------------------------------------------------------------------- L2: ASGI


@asynccontextmanager
async def reference_app(**kw: Any) -> AsyncIterator[Any]:
    app = reference_server.build_app(**kw)
    async with app.router.lifespan_context(app):
        yield app


ASGI_URL = ASGI_BASE_URL + "/mcp"


async def run_asgi(app: Any, **cfg_kw: Any) -> CheckResult:
    result, _, _ = await run_origin(ClientConfig(url=ASGI_URL, **cfg_kw), asgi_factory(app))
    return result


@pytest.mark.parametrize(
    "kw", [{}, {"json_response": True}, {"stateless_http": False}, {"bare": True}]
)
async def test_pass_sdk_default(kw: dict[str, Any]) -> None:
    async with reference_app(**kw) as app:
        result = await run_asgi(app)
    assert result.status is Status.PASS, (result.summary, result.details)
    assert result.summary == "Origin header validated"
    assert not result.info
    assert result.requests == 5
    assert "Origin https://evil.example.com → HTTP 403" in result.details
    assert "Origin null → HTTP 403" in result.details
    assert "no Origin → HTTP 200 application/json JSON-RPC result" in result.details


async def test_pass_with_token() -> None:
    async with reference_app(require_token="T0K") as app:
        result = await run_asgi(app, token="T0K")
    assert result.status is Status.PASS, result.summary


async def test_inconclusive_401_sdk() -> None:
    async with reference_app(require_token="T0K") as app:
        result = await run_asgi(app)
        rejected = await run_asgi(app, token="wrong")
    assert result.status is Status.INCONCLUSIVE
    assert result.summary == "Not evaluated: HTTP 401 on every probe (pass --token)"
    assert rejected.summary == "Not evaluated: HTTP 401 on every probe (token rejected)"


async def test_fail_sdk_disabled() -> None:
    async with reference_app(origin_check=False) as app:
        result = await run_asgi(app)
    assert result.status is Status.FAIL
    assert codes_of(result) == {"origin.evil_accepted": Level.MUST}


async def test_fail_sdk_host_0000() -> None:
    async with reference_app(host="0.0.0.0") as app:  # noqa: S104 — the SDK footgun under test
        result = await run_asgi(app)
    assert result.status is Status.FAIL
    assert codes_of(result) == {"origin.evil_accepted": Level.MUST}


def misbehaving_app(**toggles: Any) -> Any:
    return misbehaving_server.build_app(Toggles(**toggles))


@pytest.mark.parametrize("sse", [False, True])
async def test_pass_misbehaving_default(sse: bool) -> None:
    app = misbehaving_app(sse=sse)
    result = await run_asgi(app)
    assert result.status is Status.PASS, result.summary
    log = app.state.fixture.log
    assert [(e["verb"], e["method"]) for e in log] == [("POST", PROBE_METHOD)] * 4 + [
        ("OPTIONS", None)
    ]
    assert len({e["client_port"] for e in log}) == 5  # one fresh connection per probe


@pytest.mark.parametrize(
    ("origin_mode", "status", "code", "level"),
    [
        ("accept_all", Status.FAIL, "origin.evil_accepted", Level.MUST),
        ("accept_all_sse", Status.FAIL, "origin.evil_accepted", Level.MUST),
        ("reject_400_jsonrpc", Status.FAIL, "origin.evil_rejected_wrong_status", Level.MUST),
        ("reject_200_jsonrpc_error", Status.FAIL, "origin.evil_rejected_wrong_status", Level.MUST),
        ("legacy_only", Status.FAIL, "origin.not_validated", Level.MUST),
        ("allowlist_app_origin", Status.WARN, "origin.same_origin_rejected", Level.BEST_PRACTICE),
        ("require_origin", Status.WARN, "origin.no_origin_rejected", Level.BEST_PRACTICE),
        ("accept_null", Status.WARN, "origin.null_accepted", Level.BEST_PRACTICE),
        ("redirect_evil", Status.WARN, "origin.evil_redirected", Level.SHOULD),
        ("cors_reflect", Status.PASS, "origin.cors_preflight_permissive", Level.BEST_PRACTICE),
    ],
)
async def test_misbehaving_origin_modes(
    origin_mode: str, status: Status, code: str, level: Level
) -> None:
    app = misbehaving_app(origin_mode=origin_mode)
    result = await run_asgi(app)
    assert result.status is status, (result.summary, result.details)
    assert codes_of(result) == {code: level}
    verbs = {e["verb"] for e in app.state.fixture.log}
    assert verbs <= {"POST", "OPTIONS"}  # a 302 is never followed (would become a GET)


async def test_allowlist_passes_with_origin_option() -> None:
    result = await run_asgi(misbehaving_app(origin_mode="allowlist_app_origin"), origin=APP)
    assert result.status is Status.PASS, result.summary


async def test_misbehaving_wrong_status_summaries() -> None:
    r400 = await run_asgi(misbehaving_app(origin_mode="reject_400_jsonrpc"))
    assert r400.summary == "Evil Origin rejected with HTTP 400 (spec requires 403)"
    r200 = await run_asgi(misbehaving_app(origin_mode="reject_200_jsonrpc_error"))
    assert r200.summary == (
        "Evil Origin rejected with a JSON-RPC error on HTTP 200 (spec requires 403)"
    )


@pytest.mark.parametrize(
    ("toggles", "fragment"),
    [
        ({"status": 401}, "--token"),
        ({"status": 403, "body": b"Forbidden"}, "--origin"),
        ({"status": 500}, "no baseline response"),
        ({"status": 404, "content_type": "text/html", "body": b"<h1>404</h1>"}, "no baseline"),
    ],
)
async def test_inconclusive_environment_rows(toggles: dict[str, Any], fragment: str) -> None:
    result = await run_asgi(misbehaving_app(**toggles))
    assert result.status is Status.INCONCLUSIVE
    assert fragment in result.summary
    assert not result.failures
    assert not result.warnings


# --------------------------------------------------------------------------- L3: TCP hazards


async def test_fresh_connections_tcp(misbehaving_url: Callable[[Toggles], str]) -> None:
    url = misbehaving_url(Toggles())
    result, transcript, _ = await run_origin(ClientConfig(url=url))
    assert result.status is Status.PASS, result.summary
    with httpx2.Client(timeout=5) as client:
        log = client.get(url.replace("/mcp", "/__log")).json()
    assert [(e["verb"], e["method"]) for e in log] == [("POST", PROBE_METHOD)] * 4 + [
        ("OPTIONS", None)
    ]
    assert len({e["client_port"] for e in log}) == 5
    assert [e.connection for e in transcript.for_check("origin")] == [
        "origin-evil",
        "origin-same",
        "origin-none",
        "origin-null",
        "origin-preflight",
    ]


async def test_warn_connection_closed_tcp(misbehaving_url: Callable[[Toggles], str]) -> None:
    url = misbehaving_url(Toggles(origin_mode="reset_evil"))
    result, _, _ = await run_origin(ClientConfig(url=url, timeout=5))
    assert result.status is Status.WARN, result.summary
    assert codes_of(result) == {"origin.evil_connection_closed": Level.SHOULD}


async def test_warn_timeout_tcp(misbehaving_url: Callable[[Toggles], str]) -> None:
    url = misbehaving_url(Toggles(origin_mode="hang_evil"))
    t0 = time.perf_counter()
    result, _, _ = await run_origin(ClientConfig(url=url, timeout=0.5))
    wall = time.perf_counter() - t0
    assert result.status is Status.WARN, result.summary
    assert codes_of(result) == {"origin.evil_timeout": Level.SHOULD}
    assert wall < 2.0, wall  # P1 and P4 each hit the 0.5 s budget; nothing else waits


async def test_warn_redirect_tcp(misbehaving_url: Callable[[Toggles], str]) -> None:
    url = misbehaving_url(Toggles(origin_mode="redirect_evil"))
    result, _, _ = await run_origin(ClientConfig(url=url))
    assert codes_of(result) == {"origin.evil_redirected": Level.SHOULD}
    with httpx2.Client(timeout=5) as client:
        log = client.get(url.replace("/mcp", "/__log")).json()
    assert all(e["verb"] != "GET" for e in log)
    assert all(e["path"] == "/mcp" for e in log)  # the redirect target was never requested


async def test_pass_reference_tcp(reference_url: str) -> None:
    result, _, _ = await run_origin(ClientConfig(url=reference_url))
    assert result.status is Status.PASS, result.summary


async def test_fail_reference_tcp_no_origin_check(
    reference_url_factory: Callable[..., str],
) -> None:
    url = reference_url_factory(origin_check=False)
    result, _, _ = await run_origin(ClientConfig(url=url))
    assert result.status is Status.FAIL
    assert codes_of(result) == {"origin.evil_accepted": Level.MUST}


# --------------------------------------------------------------------------- SDK matrix


def _interpreters() -> list[tuple[str, str]]:
    rows = [("current", sys.executable)]
    extra = os.environ.get("MCPCHECK_MATRIX_PYTHONS", "")
    rows.extend((Path(p).parent.parent.name, p) for p in extra.split(os.pathsep) if p)
    return rows


@pytest.fixture
def spawned_reference_url(request: pytest.FixtureRequest) -> Iterator[str]:
    """Start ``tests.fixtures.reference_server`` under another interpreter; yield its URL."""
    python: str = request.param
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    proc = subprocess.Popen(  # noqa: S603 — fixed module path, interpreter from the matrix env var
        [python, "-m", "tests.fixtures.reference_server", "--port", "0"],
        cwd=REPO_ROOT,
        env=env,
        stderr=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        text=True,
    )
    assert proc.stderr is not None
    url = ""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        line = proc.stderr.readline()
        if not line:
            break
        if "listening on " in line:
            url = line.split("listening on ", 1)[1].strip()
            break
    try:
        if not url:
            pytest.fail(f"reference server under {python} did not start")
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(5)
        proc.stderr.close()


@pytest.mark.parametrize(
    "spawned_reference_url",
    [p for _, p in _interpreters()],
    ids=[n for n, _ in _interpreters()],
    indirect=True,
)
async def test_sdk_matrix(spawned_reference_url: str) -> None:
    result, _, _ = await run_origin(ClientConfig(url=spawned_reference_url))
    assert result.status is Status.PASS, (result.summary, result.details)
    assert result.requests == 5
