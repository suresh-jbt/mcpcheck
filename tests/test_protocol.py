"""The ``protocol`` check (PLAN.md §7.8, §10.4, §10.5).

L1: every verdict path over a scripted validation ladder (MockTransport), asserting
what mcpcheck *sent*. L2: the SDK reference app (both response modes) and the
misbehaving app's toggles over ASGI. L3: the runner over TCP for safety
(request log allow-list, fresh connections) and the slow-server hazard.
"""

from __future__ import annotations

import contextlib
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import anyio
import httpx2
import pytest
from mcp.shared.inbound import decode_header_value

from mcpcheck import runner, wire
from mcpcheck.checks.base import CheckContext, Findings
from mcpcheck.checks.protocol import (
    INVALID_CURSOR,
    MISMATCH_HEADER_VERSION,
    OTHER_NAME,
    UNKNOWN_METHOD,
    UNSUPPORTED_VERSION,
    ProtocolCheck,
    base64_sentinel,
    is_forbidden_error_code,
    is_legacy_session_rejection,
    is_legacy_shaped,
    is_probe_eligible,
    select_param_probe,
)
from mcpcheck.client import ClientConfig, RawClient, Transcript
from mcpcheck.models import CheckResult, Level, Status
from tests.conftest import ASGI_BASE_URL, ASGI_CLIENT_PORT, fetch_request_log
from tests.fixtures import misbehaving_server, reference_server
from tests.fixtures.misbehaving_server import HDR_TOOL_SCHEMA, Toggles
from tests.helpers.scripted import ScriptedServer, error_response, list_result, ok

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable


pytestmark = pytest.mark.anyio

URL = "http://127.0.0.1:1/mcp"
PV = wire.PROTOCOL_VERSION
META_PV = wire.META_PROTOCOL_VERSION
META_CAPS = wire.META_CLIENT_CAPABILITIES

PLAIN_TOOL: dict[str, Any] = {
    "name": "echo",
    "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
    "annotations": {"readOnlyHint": True},
}
HDR_TOOL: dict[str, Any] = {
    "name": "hdr_tool",
    "inputSchema": HDR_TOOL_SCHEMA,
    "annotations": {"readOnlyHint": True, "destructiveHint": False},
}
HDR_TOOL_UNANNOTATED: dict[str, Any] = {"name": "hdr_plain", "inputSchema": HDR_TOOL_SCHEMA}


# --------------------------------------------------------------------------- harness


def cfg(**kw: Any) -> ClientConfig:
    return ClientConfig(url=URL, **kw)


class _Ctx(CheckContext):
    """A CheckContext whose fresh connections go through a test transport."""

    def __init__(
        self,
        cfg: ClientConfig,
        transcript: Transcript,
        raw: RawClient,
        factory: Callable[[], httpx2.AsyncBaseTransport],
    ) -> None:
        super().__init__(cfg, transcript, raw)
        self._factory = factory

    @contextlib.asynccontextmanager
    async def fresh(self, label: str, *, send_token: bool = True) -> AsyncIterator[RawClient]:
        assert label != "main"
        client = RawClient(
            self.cfg, self.transcript, label=label, send_token=send_token, transport=self._factory()
        )
        try:
            yield client
        finally:
            await client.aclose()


async def run_protocol(
    factory: Callable[[], httpx2.AsyncBaseTransport], config: ClientConfig
) -> tuple[CheckResult, _Ctx]:
    """Run the check the way the runner does with handshake deselected (internal gate)."""
    transcript = Transcript()
    async with RawClient(config, transcript, label="main", transport=factory()) as raw:
        ctx = _Ctx(config, transcript, raw, factory)
        await runner._internal_gate(ctx)
        check = ProtocolCheck()
        f = Findings(check, strict=config.strict)
        ctx.current_check = check.name
        transcript.current_check = check.name
        before = len(transcript)
        with anyio.fail_after(config.check_budget):
            await check.run(ctx, f)
        return f.to_result(requests=len(transcript) - before), ctx


def codes_of(result: CheckResult) -> dict[str, Level]:
    return {x.code: x.level for x in (*result.failures, *result.warnings, *result.info)}


def finding(result: CheckResult, code: str) -> Any:
    for x in (*result.failures, *result.warnings, *result.info):
        if x.code == code:
            return x
    raise AssertionError(f"{code} not in {sorted(codes_of(result))}")


# --------------------------------------------------------------------------- scripted ladder


@dataclass
class Ladder:
    """A scripted 2026-07-28 validation ladder; each field breaks one rule on demand."""

    sse: bool = False
    accel: bool = True
    mismatch: str = "reject"  # reject | accept | status_200 | bare_400 | as_unsupported | http_500
    headerless: str = "reject"  # reject | modern | legacy | wrong_code | not_found
    unsupported: str = "reject"  # reject | accept | no_data | bad_requested | status_200 | wrong_code | omit_served
    method_header: str = "reject"  # reject | accept | wrong_code | bare_404
    meta: str = "reject"  # reject | accept | status_200 | wrong_code
    unknown: str = "404"  # 404 | 200 | 400 | bare_404 | legacy_code | reserved_code
    cursor: str = "reject"  # reject | ignore | wrong_code | status_200 | bare_400
    param_header: str = "reject"  # reject | accept | invalid_args | status_200
    name_header: str = "reject"  # reject | accept | wrong_code | not_decoded | bare_400
    tools_list: str = "ok"  # ok | not_implemented
    tools: list[dict[str, Any]] = field(default_factory=lambda: [dict(PLAIN_TOOL)])

    def _hdrs(self) -> dict[str, str]:
        return {"x-accel-buffering": "no"} if (self.sse and self.accel) else {}

    def err(
        self, body: dict[str, Any], code: int, status: int = 400, data: Any = None
    ) -> httpx2.Response:
        return error_response(
            body,
            code,
            f"error {code}",
            status=status,
            data=data,
            sse=self.sse,
            headers=self._hdrs(),
        )

    def good(self, body: dict[str, Any], result: dict[str, Any]) -> httpx2.Response:
        return ok(body, result, sse=self.sse, headers=self._hdrs())

    def unsupported_reply(self, body: dict[str, Any], hv: str) -> httpx2.Response | None:
        data: dict[str, Any] | None = {"supported": [PV], "requested": hv}
        mode = self.unsupported
        if mode == "accept":
            return None
        if mode == "no_data":
            data = None
        elif mode == "bad_requested":
            data = {"supported": [PV], "requested": "oops"}
        elif mode == "omit_served":
            data = {"supported": ["2025-11-25"], "requested": hv}
        elif mode == "wrong_code":
            return self.err(body, -32600)
        status = 200 if mode == "status_200" else 400
        return self.err(body, -32022, status, data)

    def __call__(self, body: dict[str, Any], req: httpx2.Request) -> httpx2.Response:
        h = req.headers
        method = body.get("method")
        params: dict[str, Any] = body["params"] if isinstance(body.get("params"), dict) else {}
        meta: dict[str, Any] | None = (
            params["_meta"] if isinstance(params.get("_meta"), dict) else None
        )
        meta_pv = meta.get(META_PV) if meta else None
        hv = h.get("mcp-protocol-version")
        if hv is None:
            if self.headerless == "reject":
                return self.err(body, -32020)
            if self.headerless == "wrong_code":
                return self.err(body, -32600)
            if self.headerless == "not_found":
                return self.err(body, -32601, 404, method)
            if self.headerless == "legacy":
                return self.good(body, {"tools": self.tools})
            hv = meta_pv  # "modern": served as 2026-07-28 without the header
        if meta_pv is not None and hv != meta_pv:
            if self.mismatch == "http_500":
                return httpx2.Response(500, text="boom")
            if self.mismatch == "reject":
                return self.err(body, -32020)
            if self.mismatch == "status_200":
                return self.err(body, -32020, 200)
            if self.mismatch == "bare_400":
                return httpx2.Response(400)
            if self.mismatch == "as_unsupported":
                return self.err(body, -32022, 400, {"supported": [PV], "requested": hv})
            hv = meta_pv  # accept
        if hv != PV:
            reply = self.unsupported_reply(body, str(hv))
            if reply is not None:
                return reply
        if meta is None or META_CAPS not in meta:
            if self.meta == "reject":
                return self.err(body, -32602)
            if self.meta == "status_200":
                return self.err(body, -32602, 200)
            if self.meta == "wrong_code":
                return self.err(body, -32600)
        if h.get("mcp-method") != method:
            if self.method_header == "reject":
                return self.err(body, -32020)
            if self.method_header == "wrong_code":
                return self.err(body, -32600)
            if self.method_header == "bare_404":
                return httpx2.Response(404, text="not found")
        if method == "tools/list":
            if self.tools_list == "not_implemented":
                return self.err(body, -32601, 404, method)
            if "cursor" in params:
                if self.cursor == "reject":
                    return self.err(body, -32602)
                if self.cursor == "wrong_code":
                    return self.err(body, -32603)
                if self.cursor == "status_200":
                    return self.err(body, -32602, 200)
                if self.cursor == "bare_400":
                    return httpx2.Response(400)
            return self.good(body, list_result(self.tools))
        if method == "server/discover":
            return self.good(
                body,
                {
                    "resultType": "complete",
                    "supportedVersions": [PV],
                    "capabilities": {"tools": {}},
                    "ttlMs": 60000,
                    "cacheScope": "public",
                },
            )
        if method == "tools/call":
            raw_name = h.get("mcp-name")
            if self.name_header == "not_decoded" and (raw_name or "").startswith("=?base64?"):
                return self.err(body, -32020)  # compared without decoding: a mismatch
            if decode_header_value(raw_name) != params.get("name"):
                if self.name_header == "wrong_code":
                    return self.err(body, -32600)
                if self.name_header == "bare_400":
                    return httpx2.Response(400)
                if self.name_header != "accept":  # reject | not_decoded
                    return self.err(body, -32020)
            args: dict[str, Any] = (
                params["arguments"] if isinstance(params.get("arguments"), dict) else {}
            )
            if "region" in args and h.get("mcp-param-region") is None:
                if self.param_header == "reject":
                    return self.err(body, -32020)
                if self.param_header == "status_200":
                    return self.err(body, -32020, 200)
                if self.param_header == "invalid_args":
                    return self.err(body, -32602)
            return self.good(
                body, {"content": [{"type": "text", "text": "missing q"}], "isError": True}
            )
        # unknown method
        if self.unknown == "bare_404":
            return httpx2.Response(404, text="not found")
        if self.unknown == "legacy_code":
            return self.err(body, -32002, 404, method)
        if self.unknown == "reserved_code":
            return self.err(body, -32050, 404, method)
        return self.err(body, -32601, int(self.unknown), method)


def scripted(ladder: Ladder, **server_kw: Any) -> ScriptedServer:
    return ScriptedServer(catch_all=ladder, **server_kw)


async def run_scripted(
    ladder: Ladder, config: ClientConfig | None = None, **server_kw: Any
) -> tuple[CheckResult, ScriptedServer, _Ctx]:
    server = scripted(ladder, **server_kw)
    result, ctx = await run_protocol(server.transport, config or cfg())
    return result, server, ctx


# =========================================================================== L1: pass path


@pytest.mark.parametrize("sse", [False, True])
async def test_pass_conformant_ladder_and_what_was_sent(sse: bool) -> None:
    result, server, _ = await run_scripted(Ladder(sse=sse))
    assert result.status is Status.PASS
    assert result.summary == "Header validation and version errors conform"
    assert result.failures == []
    assert result.warnings == []
    assert result.info == []
    assert result.requests == 11
    assert "Probes:" in result.details

    gate, p1, p2, p3, p4, p5, p6, p7, p8, p11, p12, p13 = server.seen
    assert gate.method == "tools/list"
    assert gate.headers["mcp-protocol-version"] == PV
    # 1. header ≠ _meta with a non-handshake header value
    assert p1.headers["mcp-protocol-version"] == MISMATCH_HEADER_VERSION == "2026-01-01"
    assert p1.meta is not None
    assert p1.meta[META_PV] == PV
    assert p1.headers["mcp-method"] == "tools/list"
    # 2. no header, _meta intact
    assert "mcp-protocol-version" not in p2.headers
    assert p2.meta is not None
    assert p2.meta[META_PV] == PV
    assert META_CAPS in p2.meta
    # 3. unsupported version in both places (2099, not the plan's 1900: no legacy mapping possible)
    assert p3.headers["mcp-protocol-version"] == UNSUPPORTED_VERSION == "2099-01-01"
    assert p3.meta is not None
    assert p3.meta[META_PV] == UNSUPPORTED_VERSION
    # 4. no Mcp-Method
    assert "mcp-method" not in p4.headers
    assert p4.headers["mcp-protocol-version"] == PV
    # 5. mis-cased Mcp-Method value
    assert p5.headers["mcp-method"] == "Tools/List"
    assert p5.body is not None
    assert p5.body["method"] == "tools/list"
    # 6. _meta without clientCapabilities (protocolVersion kept)
    assert p6.meta == {META_PV: PV, wire.META_CLIENT_INFO: wire.client_info()}
    # 7. unknown method with a matching Mcp-Method
    assert p7.method == UNKNOWN_METHOD == p7.headers["mcp-method"]
    # 8. bogus cursor on an otherwise valid request
    assert p8.body is not None
    assert p8.body["params"]["cursor"] == INVALID_CURSOR
    assert p8.headers["mcp-protocol-version"] == PV
    assert p8.headers["mcp-method"] == "tools/list"
    # 11-13. Mcp-Name validation on a tools/call that can never execute (unknown tool)
    for probe in (p11, p12, p13):
        assert probe.method == "tools/call"
        assert probe.body is not None
        assert probe.body["params"]["name"] == wire.UNKNOWN_TOOL_NAME
        assert probe.body["params"]["arguments"] == {}
        assert probe.headers["mcp-method"] == "tools/call"
        assert probe.headers["mcp-protocol-version"] == PV
    assert "mcp-name" not in p11.headers
    assert p12.headers["mcp-name"] == OTHER_NAME
    assert p13.headers["mcp-name"] == base64_sentinel(wire.UNKNOWN_TOOL_NAME)
    assert decode_header_value(p13.headers["mcp-name"]) == wire.UNKNOWN_TOOL_NAME
    # nothing executable, no GET, every probe on its own connection
    assert all(s.request.method == "POST" for s in server.seen)
    assert server.methods().count("tools/call") == 3
    for seen in (gate, p1, p2, p3, p4, p5, p6, p7, p8):
        assert "mcp-name" not in seen.headers
    # every finding/evidence must survive the JSON report
    assert json.loads(result.model_dump_json())["status"] == "pass"


async def test_fresh_connection_per_probe_in_transcript() -> None:
    server = scripted(Ladder())
    transcript = Transcript()
    config = cfg()
    async with RawClient(config, transcript, label="main", transport=server.transport()) as raw:
        ctx = _Ctx(config, transcript, raw, server.transport)
        await runner._internal_gate(ctx)
        await ProtocolCheck().run(ctx, Findings(ProtocolCheck()))
    connections = [e.connection for e in transcript]
    assert connections == ["main", *(f"protocol-{n}" for n in (*range(1, 9), 11, 12, 13))]
    assert all(e.purpose.startswith("probe ") for e in list(transcript)[1:])
    tags = [e.tags for e in list(transcript)[1:]]
    assert "[MCP-Protocol-Version: 2026-01-01]" in tags[0]
    assert "[no MCP-Protocol-Version]" in tags[1]
    assert "[no Mcp-Method]" in tags[3]
    assert "[Mcp-Method: Tools/List]" in tags[4]


# =========================================================================== L1: probe 1 (mismatch)


@pytest.mark.parametrize("sse", [False, True])
async def test_header_mismatch_accepted(sse: bool) -> None:
    result, _, _ = await run_scripted(Ladder(sse=sse, mismatch="accept"))
    assert result.status is Status.FAIL
    assert codes_of(result) == {"protocol.header_mismatch_accepted": Level.MUST}
    assert result.summary.startswith(
        "Version/header mismatch accepted (MCP-Protocol-Version 2026-01-01"
    )
    ev = result.failures[0].evidence
    assert ev is not None
    assert ev["http_status"] == 200
    assert ev["rpc_code"] is None
    assert ev["sent_headers_diff"] == {"mcp-protocol-version": "2026-01-01"}
    assert result.failures[0].subject.startswith("probe 1: ")


async def test_header_mismatch_wrong_status_and_wrong_code() -> None:
    result, _, _ = await run_scripted(Ladder(mismatch="status_200"))
    assert codes_of(result) == {"protocol.header_mismatch_wrong_status": Level.MUST}
    assert "HTTP 200 with JSON-RPC -32020" in result.failures[0].message
    # version-support check running before the header/body comparison is tolerated
    result, _, _ = await run_scripted(Ladder(mismatch="as_unsupported"))
    assert result.status is Status.PASS
    assert codes_of(result) == {"protocol.mismatch_rejected_as_unsupported": Level.BEST_PRACTICE}


async def test_bare_400_counts_as_rejection_with_info() -> None:
    result, _, _ = await run_scripted(Ladder(mismatch="bare_400", cursor="bare_400"))
    assert result.status is Status.PASS
    infos = [x for x in result.info if x.code == "protocol.bare_400_accepted"]
    assert [x.subject[:8] for x in infos] == ["probe 1:", "probe 8:"]


# =========================================================================== L1: probe 2 (header-less)


@pytest.mark.parametrize("sse", [False, True])
async def test_headerless_served_modern_is_should(sse: bool) -> None:
    result, _, _ = await run_scripted(Ladder(sse=sse, headerless="modern"))
    assert result.status is Status.WARN
    assert codes_of(result) == {"protocol.headerless_accepted": Level.SHOULD}
    assert result.warnings[0].spec_url == ProtocolCheck.spec_url


@pytest.mark.parametrize("sse", [False, True])
async def test_headerless_served_legacy_is_dual_era_info(sse: bool) -> None:
    result, _, _ = await run_scripted(Ladder(sse=sse, headerless="legacy"))
    assert result.status is Status.PASS
    assert codes_of(result) == {"protocol.dual_era_server": Level.BEST_PRACTICE}
    assert "dual-era" in result.info[0].message


async def test_headerless_rejection_malformed_and_legacy_not_found() -> None:
    result, _, _ = await run_scripted(Ladder(headerless="wrong_code"))
    assert codes_of(result) == {"protocol.headerless_rejection_malformed": Level.SHOULD}
    result, _, _ = await run_scripted(Ladder(headerless="not_found"))
    assert result.status is Status.PASS
    assert codes_of(result) == {"protocol.dual_era_server": Level.BEST_PRACTICE}


# =========================================================================== L1: probe 3 (unsupported)


@pytest.mark.parametrize(
    ("mode", "code"),
    [
        ("accept", "protocol.unsupported_version_not_rejected"),
        ("no_data", "protocol.unsupported_data_missing"),
        ("bad_requested", "protocol.unsupported_data_missing"),
        ("status_200", "protocol.unsupported_wrong_status"),
        ("wrong_code", "protocol.unsupported_wrong_code"),
    ],
)
async def test_unsupported_version_failures(mode: str, code: str) -> None:
    result, _, _ = await run_scripted(Ladder(unsupported=mode))
    assert result.status is Status.FAIL
    assert codes_of(result) == {code: Level.MUST}
    assert result.failures[0].subject == f"probe 3: protocol version {UNSUPPORTED_VERSION}"


async def test_unsupported_supported_omits_served_is_should_and_versions_recorded() -> None:
    result, _, ctx = await run_scripted(Ladder(unsupported="omit_served"))
    assert codes_of(result) == {"protocol.unsupported_supported_omits_served": Level.SHOULD}
    assert ctx.server_supported_versions == ["2025-11-25"]
    result, _, ctx = await run_scripted(Ladder())
    assert ctx.server_supported_versions == [PV]


# =========================================================================== L1: probes 4-5 (Mcp-Method)


@pytest.mark.parametrize("sse", [False, True])
async def test_method_header_defects_accepted(sse: bool) -> None:
    result, _, _ = await run_scripted(Ladder(sse=sse, method_header="accept"))
    assert result.status is Status.FAIL
    assert codes_of(result) == {
        "protocol.missing_mcp_method_accepted": Level.MUST,
        "protocol.method_header_case_accepted": Level.MUST,
    }
    assert result.failures[0].subject == "probe 4: no Mcp-Method header"
    assert result.failures[1].subject == "probe 5: Mcp-Method Tools/List"


@pytest.mark.parametrize("mode", ["wrong_code", "bare_404"])
async def test_method_header_wrong_rejection(mode: str) -> None:
    result, _, _ = await run_scripted(Ladder(method_header=mode))
    assert set(codes_of(result)) == {"protocol.method_header_wrong_rejection"}
    assert len(result.failures) == 2


# =========================================================================== L1: probe 6 (_meta)


@pytest.mark.parametrize(
    ("mode", "code"),
    [
        ("accept", "protocol.missing_meta_accepted"),
        ("status_200", "protocol.missing_meta_wrong_status"),
        ("wrong_code", "protocol.missing_meta_wrong_code"),
    ],
)
async def test_missing_meta_failures(mode: str, code: str) -> None:
    result, server, _ = await run_scripted(Ladder(meta=mode))
    assert codes_of(result) == {code: Level.MUST}
    assert result.failures[0].subject == "probe 6: _meta without clientCapabilities"
    sent = server.seen[6].meta
    assert sent is not None
    assert META_CAPS not in sent
    assert sent[META_PV] == PV


# =========================================================================== L1: probe 7 (unknown method)


@pytest.mark.parametrize("status", ["200", "400"])
async def test_unknown_method_wrong_status(status: str) -> None:
    result, _, _ = await run_scripted(Ladder(unknown=status))
    assert codes_of(result) == {"protocol.unknown_method_wrong_status": Level.MUST}
    assert f"HTTP {status} instead of 404" in result.failures[0].message


async def test_unknown_method_not_jsonrpc() -> None:
    result, _, _ = await run_scripted(Ladder(unknown="bare_404"))
    assert codes_of(result) == {"protocol.unknown_method_not_jsonrpc": Level.MUST}
    assert "text/plain" in result.failures[0].message


@pytest.mark.parametrize("mode", ["legacy_code", "reserved_code"])
async def test_forbidden_error_code_on_any_reply(mode: str) -> None:
    result, _, _ = await run_scripted(Ladder(unknown=mode))
    assert codes_of(result) == {
        "protocol.unknown_method_not_jsonrpc": Level.MUST,
        "protocol.forbidden_error_code": Level.MUST,
    }
    forbidden = finding(result, "protocol.forbidden_error_code")
    assert forbidden.evidence["forbidden_code"] == (-32002 if mode == "legacy_code" else -32050)
    assert forbidden.spec_url.endswith("#error-codes")


# =========================================================================== L1: probe 8 (cursor)


async def test_invalid_cursor_ignored_is_info_only() -> None:
    result, _, _ = await run_scripted(Ladder(cursor="ignore"))
    assert result.status is Status.PASS
    assert codes_of(result) == {"protocol.invalid_cursor_ignored": Level.BEST_PRACTICE}


async def test_invalid_cursor_other_error_is_should() -> None:
    result, _, _ = await run_scripted(Ladder(cursor="wrong_code"))
    assert result.status is Status.WARN
    assert codes_of(result) == {"protocol.invalid_cursor_not_rejected": Level.SHOULD}
    result, _, _ = await run_scripted(Ladder(cursor="status_200"))
    assert result.status is Status.PASS
    assert codes_of(result) == {}


async def test_tool_less_server_probes_discover_and_skips_cursor() -> None:
    result, server, _ = await run_scripted(Ladder(tools_list="not_implemented"))
    assert result.status is Status.PASS
    methods = server.methods()
    assert methods == ["tools/list", *["server/discover"] * 6, UNKNOWN_METHOD, *["tools/call"] * 3]
    assert server.seen[5].headers["mcp-method"] == "Server/Discover"
    assert result.requests == 10  # 7 list/discover probes + 3 Mcp-Name probes


# =========================================================================== L1: probe 9 (Mcp-Param)


@pytest.mark.parametrize("sse", [False, True])
async def test_param_header_omission_rejected_passes_and_payload_is_invalid(sse: bool) -> None:
    result, server, _ = await run_scripted(
        Ladder(sse=sse, tools=[dict(PLAIN_TOOL), dict(HDR_TOOL)])
    )
    assert result.status is Status.PASS
    assert codes_of(result) == {}
    call = next(s for s in server.seen if s.body and s.body["params"].get("name") == "hdr_tool")
    assert call.method == "tools/call"
    assert call.headers["mcp-name"] == "hdr_tool"
    assert call.headers["mcp-method"] == "tools/call"
    assert "mcp-param-region" not in call.headers
    assert call.body is not None
    assert call.body["params"]["name"] == "hdr_tool"
    assert call.body["params"]["arguments"] == {"region": "mcpcheck"}  # q (required) missing
    assert "probe 9: tools/call hdr_tool without Mcp-Param-Region" in result.details


async def test_param_header_omission_accepted_is_must() -> None:
    result, _, _ = await run_scripted(Ladder(param_header="accept", tools=[dict(HDR_TOOL)]))
    assert codes_of(result) == {"protocol.param_header_omission_accepted": Level.MUST}
    assert result.failures[0].tool == "hdr_tool"
    result, _, _ = await run_scripted(Ladder(param_header="status_200", tools=[dict(HDR_TOOL)]))
    assert codes_of(result) == {"protocol.header_mismatch_wrong_status": Level.MUST}


async def test_param_header_not_observable_is_info() -> None:
    result, _, _ = await run_scripted(Ladder(param_header="invalid_args", tools=[dict(HDR_TOOL)]))
    assert result.status is Status.PASS
    assert codes_of(result) == {"protocol.param_header_not_observable": Level.BEST_PRACTICE}


@pytest.mark.parametrize(
    ("policy", "tools", "called"),
    [
        ("none", [HDR_TOOL], False),
        ("safe", [HDR_TOOL_UNANNOTATED], False),
        ("all", [HDR_TOOL_UNANNOTATED], True),
        ("safe", [PLAIN_TOOL], False),
    ],
)
async def test_param_probe_policy_gating(
    policy: str, tools: list[dict[str, Any]], called: bool
) -> None:
    result, server, _ = await run_scripted(
        Ladder(tools=[dict(t) for t in tools]),
        cfg(probe=policy),
    )
    assert result.status is Status.PASS
    named = [
        s for s in server.seen if s.body and s.body["params"].get("name") != wire.UNKNOWN_TOOL_NAME
    ]
    assert any(s.method == "tools/call" for s in named) is called
    # --probe none sends no tools/call at all, not even to the non-existent tool
    assert ("tools/call" in server.methods()) is (policy != "none")
    if policy == "none":
        assert "probe 9 (Mcp-Param-* omission) skipped: --probe none" in result.details
        assert "probes 11-13 (Mcp-Name) skipped: --probe none" in result.details


# =========================================================================== L1: probes 11-13 (Mcp-Name)


@pytest.mark.parametrize("sse", [False, True])
async def test_mcp_name_ignored_is_two_musts(sse: bool) -> None:
    """A server that never looks at Mcp-Name accepts both the missing and the mismatching header."""
    result, _, _ = await run_scripted(Ladder(sse=sse, name_header="accept"))
    assert result.status is Status.FAIL
    assert codes_of(result) == {
        "protocol.missing_mcp_name_accepted": Level.MUST,
        "protocol.name_header_mismatch_accepted": Level.MUST,
    }
    missing = finding(result, "protocol.missing_mcp_name_accepted")
    assert missing.evidence is not None
    assert missing.evidence["probe"] == 11
    assert missing.evidence["sent_headers_diff"] == {"mcp-name": None}
    assert missing.evidence["expected"] == "HTTP 400 + -32020"
    mismatch = finding(result, "protocol.name_header_mismatch_accepted")
    assert mismatch.evidence is not None
    assert mismatch.evidence["probe"] == 12
    assert mismatch.evidence["sent_headers_diff"] == {"mcp-name": OTHER_NAME}
    assert all(f.spec_url == wire_spec_server_validation() for f in result.failures)


def wire_spec_server_validation() -> str:
    from mcpcheck import spec_urls

    return spec_urls.SPEC_SERVER_VALIDATION


async def test_mcp_name_wrong_rejection_and_bare_400() -> None:
    result, _, _ = await run_scripted(Ladder(name_header="wrong_code"))
    assert codes_of(result) == {"protocol.name_header_wrong_rejection": Level.MUST}
    assert len(result.failures) == 2  # one per defective probe (11 and 12)
    result, _, _ = await run_scripted(Ladder(name_header="bare_400"))
    assert result.status is Status.PASS
    assert codes_of(result) == {"protocol.bare_400_accepted": Level.BEST_PRACTICE}


async def test_mcp_name_not_decoded_is_must() -> None:
    result, _, _ = await run_scripted(Ladder(name_header="not_decoded"))
    assert result.status is Status.FAIL
    assert codes_of(result) == {"protocol.name_header_not_decoded": Level.MUST}
    f = result.failures[0]
    assert f.evidence is not None
    assert f.evidence["probe"] == 13
    assert f.evidence["rpc_code"] == -32020
    assert f.spec_url is not None
    assert f.spec_url.endswith("#value-encoding")


def test_is_legacy_session_rejection() -> None:
    def reply(status: int, msg: dict[str, Any]) -> Any:
        from mcpcheck.client import RawReply

        return RawReply(
            status=status,
            headers=httpx2.Headers(),
            media_type="application/json",
            charset=None,
            messages=(msg,),
            raw=b"",
            truncated=False,
            parse_error=None,
            elapsed_ms=1,
            request_id=1,
        )

    legacy_id = {
        "jsonrpc": "2.0",
        "id": "server-error",
        "error": {"code": -32600, "message": "Bad Request: Missing session ID"},
    }
    session_word = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32600, "message": "Session not found"},
    }
    other = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32600, "message": "Bad Request"}}
    mismatch = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32020, "message": "session header mismatch"},
    }
    assert is_legacy_session_rejection(reply(400, legacy_id))
    assert is_legacy_session_rejection(reply(404, session_word))
    assert not is_legacy_session_rejection(reply(400, other))
    assert not is_legacy_session_rejection(reply(400, mismatch))  # -32020 is the modern code


async def test_headerless_legacy_session_rejection_is_dual_era_info() -> None:
    """T:277-281: a dual-era server MAY treat the header-less request as 2025-03-26 and demand a session."""

    def ladder_with_session_gate(body: dict[str, Any], req: httpx2.Request) -> httpx2.Response:
        if req.headers.get("mcp-protocol-version") is None:
            return httpx2.Response(
                400,
                json={
                    "jsonrpc": "2.0",
                    "id": "server-error",
                    "error": {"code": -32600, "message": "Bad Request: Missing session ID"},
                },
            )
        return Ladder()(body, req)

    server = ScriptedServer(catch_all=ladder_with_session_gate)
    result, _ = await run_protocol(server.transport, cfg())
    assert result.status is Status.PASS
    assert codes_of(result) == {"protocol.dual_era_server": Level.BEST_PRACTICE}
    assert "legacy session path" in result.info[0].message


# =========================================================================== L1: probe 10 (GET)


async def test_legacy_get_only_under_legacy_probes() -> None:
    result, server, _ = await run_scripted(Ladder())
    assert all(s.request.method == "POST" for s in server.seen)
    result, server, _ = await run_scripted(Ladder(), cfg(legacy_probes=True))
    assert result.status is Status.PASS
    assert codes_of(result) == {}
    get = server.seen[-1]
    assert get.request.method == "GET"
    assert get.headers["mcp-protocol-version"] == PV
    assert "probe 10: headers-only GET → HTTP 405 Allow: POST" in result.details


async def test_legacy_get_hosted_is_info() -> None:
    def sse_stream(_req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, content=b": keepalive\n\n"
        )

    result, _, _ = await run_scripted(
        Ladder(), cfg(legacy_probes=True), http_routes={"GET": sse_stream}
    )
    assert result.status is Status.PASS
    assert codes_of(result) == {"protocol.legacy_get_hosted": Level.BEST_PRACTICE}


# =========================================================================== L1: cross-cutting


async def test_sse_without_accel_buffering_is_should_once() -> None:
    result, _, _ = await run_scripted(Ladder(sse=True, accel=False))
    assert result.status is Status.WARN
    assert codes_of(result) == {"protocol.no_accel_buffering_header": Level.SHOULD}
    assert len(result.warnings) == 1


async def test_infra_outcome_makes_probe_inconclusive() -> None:
    result, _, _ = await run_scripted(Ladder(mismatch="http_500"))
    assert result.status is Status.INCONCLUSIVE
    assert result.summary == "Not evaluated: HTTP 500"
    assert codes_of(result) == {}
    # a MUST violation elsewhere still dominates
    result, _, _ = await run_scripted(Ladder(mismatch="http_500", meta="accept"))
    assert result.status is Status.FAIL


async def test_auth_required_on_probes_is_inconclusive_with_token_hint() -> None:
    calls: list[int] = []

    def gate_then_401(body: dict[str, Any], _req: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        if len(calls) == 1:  # the internal gate on main succeeds; every probe hits auth
            return ok(body, list_result([dict(PLAIN_TOOL)]))
        return httpx2.Response(401, headers={"www-authenticate": "Bearer"})

    server = ScriptedServer(catch_all=gate_then_401)
    result, _ = await run_protocol(server.transport, cfg())
    assert result.status is Status.INCONCLUSIVE
    assert result.summary == "Not evaluated: HTTP 401 (pass --token)"


# =========================================================================== pure helpers


def test_helpers() -> None:
    assert is_legacy_shaped({"tools": []})
    assert not is_legacy_shaped({"tools": [], "ttlMs": 1})
    assert is_forbidden_error_code(-32002)
    assert is_forbidden_error_code(-32042)
    assert is_forbidden_error_code(-32050)
    assert is_forbidden_error_code(-32099)
    assert not is_forbidden_error_code(-32020)
    assert not is_forbidden_error_code(-32022)
    assert not is_forbidden_error_code(-32601)
    assert not is_forbidden_error_code(-32000)
    assert is_probe_eligible(HDR_TOOL, "safe")
    assert not is_probe_eligible(HDR_TOOL, "none")
    assert not is_probe_eligible(HDR_TOOL_UNANNOTATED, "safe")
    assert is_probe_eligible(HDR_TOOL_UNANNOTATED, "all")
    assert is_probe_eligible({"annotations": {"destructiveHint": False}}, "safe")
    assert not is_probe_eligible({"annotations": {"destructiveHint": True}}, "safe")


def test_select_param_probe_only_with_proven_invalid_payload() -> None:
    assert select_param_probe([HDR_TOOL], "safe") == ("hdr_tool", {"region": "mcpcheck"}, "Region")
    # header property is the only required one and extra properties are allowed: nothing provable
    permissive: dict[str, Any] = {
        "name": "p",
        "annotations": {"readOnlyHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {"region": {"type": "string", "x-mcp-header": "Region"}},
        },
    }
    assert select_param_probe([permissive], "safe") is None
    strict_extra = {
        **permissive,
        "inputSchema": {**permissive["inputSchema"], "additionalProperties": False},
    }
    picked = select_param_probe([strict_extra], "safe")
    assert picked is not None
    assert "mcpcheck_nonexistent_argument_7f3a" in picked[1]
    # x-mcp-header on a non-permitted type, bad schemas, number types: skipped
    bad = {
        **HDR_TOOL,
        "inputSchema": {
            "type": "object",
            "properties": {"n": {"type": "number", "x-mcp-header": "N"}},
        },
    }
    assert select_param_probe([bad], "safe") is None
    assert select_param_probe([{**HDR_TOOL, "inputSchema": {"type": 42}}], "safe") is None
    assert (
        select_param_probe(
            [{**HDR_TOOL, "inputSchema": {"$schema": 42, **HDR_TOOL_SCHEMA}}], "safe"
        )
        is None
    )
    enum_tool = {
        **HDR_TOOL,
        "inputSchema": {
            "type": "object",
            "properties": {
                "q": {"type": "string"},
                "n": {"type": "integer", "enum": [7, 9], "x-mcp-header": "N"},
            },
            "required": ["q"],
        },
    }
    assert select_param_probe([enum_tool], "all") == ("hdr_tool", {"n": 7}, "N")


# =========================================================================== L2: ASGI fixtures


def asgi_factory(app: Any) -> Callable[[], httpx2.AsyncBaseTransport]:
    return lambda: httpx2.ASGITransport(app, client=("127.0.0.1", ASGI_CLIENT_PORT))


ASGI_URL = ASGI_BASE_URL + "/mcp"


async def run_reference(**build_kwargs: Any) -> CheckResult:
    app = reference_server.build_app(**build_kwargs)
    async with app.router.lifespan_context(app):
        result, _ = await run_protocol(asgi_factory(app), ClientConfig(url=ASGI_URL))
    return result


async def run_misbehaving(toggles: Toggles, **cfg_kw: Any) -> CheckResult:
    app = misbehaving_server.build_app(toggles)
    result, _ = await run_protocol(asgi_factory(app), ClientConfig(url=ASGI_URL, **cfg_kw))
    return result


@pytest.mark.parametrize("json_response", [False, True])
async def test_reference_server_passes(json_response: bool) -> None:
    result = await run_reference(json_response=json_response)
    assert result.status is Status.PASS, result.model_dump()
    assert result.failures == []
    assert result.warnings == []
    assert result.summary == "Header validation and version errors conform"
    # the SDK serves header-less requests on its legacy path and ignores unknown cursors
    assert codes_of(result) == {
        "protocol.dual_era_server": Level.BEST_PRACTICE,
        "protocol.invalid_cursor_ignored": Level.BEST_PRACTICE,
    }


async def test_reference_server_legacy_get_is_405() -> None:
    app = reference_server.build_app()
    async with app.router.lifespan_context(app):
        result, _ = await run_protocol(
            asgi_factory(app), ClientConfig(url=ASGI_URL, legacy_probes=True)
        )
    assert result.status is Status.PASS
    assert "probe 10: headers-only GET → HTTP 405 Allow: POST" in result.details


async def test_misbehaving_default_passes_without_infos() -> None:
    result = await run_misbehaving(Toggles())
    assert result.status is Status.PASS
    assert codes_of(result) == {}


@pytest.mark.parametrize(
    ("toggles", "code", "status", "level"),
    [
        (
            Toggles(ignore_version_mismatch=True),
            "protocol.header_mismatch_accepted",
            Status.FAIL,
            Level.MUST,
        ),
        (
            Toggles(unknown_method_status=200),
            "protocol.unknown_method_wrong_status",
            Status.FAIL,
            Level.MUST,
        ),
        (
            Toggles(legacy_error_codes=True),
            "protocol.forbidden_error_code",
            Status.FAIL,
            Level.MUST,
        ),
        (
            Toggles(accept_missing_meta=True),
            "protocol.missing_meta_accepted",
            Status.FAIL,
            Level.MUST,
        ),
        (
            Toggles(sse=True, x_accel_buffering=False),
            "protocol.no_accel_buffering_header",
            Status.WARN,
            Level.SHOULD,
        ),
        (
            Toggles(ignore_cursor=True),
            "protocol.invalid_cursor_ignored",
            Status.PASS,
            Level.BEST_PRACTICE,
        ),
        (
            Toggles(ignore_mcp_name=True),
            "protocol.missing_mcp_name_accepted",
            Status.FAIL,
            Level.MUST,
        ),
        # §10.4 lists headerless_accepted/warn here; the 2025-shaped answer is the dual-era MAY the
        # SDK reference server also exhibits, so it is an info (see deviations).
        (
            Toggles(serve_headerless_as_legacy=True),
            "protocol.dual_era_server",
            Status.PASS,
            Level.BEST_PRACTICE,
        ),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
async def test_misbehaving_toggle_rows(
    toggles: Toggles, code: str, status: Status, level: Level
) -> None:
    result = await run_misbehaving(toggles)
    assert result.status is status, result.model_dump()
    assert codes_of(result)[code] is level


async def test_misbehaving_legacy_error_codes_details() -> None:
    result = await run_misbehaving(Toggles(legacy_error_codes=True))
    # the fixture maps -32601 → -32002 and the cursor's -32602 → -32042 (its envelope
    # -32602 for a missing _meta key is emitted directly and stays conformant)
    assert set(codes_of(result)) == {
        "protocol.forbidden_error_code",
        "protocol.invalid_cursor_not_rejected",
        "protocol.unknown_method_not_jsonrpc",
    }
    assert {
        (x.evidence or {})["forbidden_code"]
        for x in result.failures
        if x.code == "protocol.forbidden_error_code"
    } == {-32042, -32002}


async def test_misbehaving_sse_alone_passes() -> None:
    result = await run_misbehaving(Toggles(sse=True))
    assert result.status is Status.PASS
    assert codes_of(result) == {}


async def test_misbehaving_param_header_probe_against_hdr_tool() -> None:
    result = await run_misbehaving(Toggles(error_shapes_catalogue=True))
    assert result.status is Status.PASS, result.model_dump()
    assert codes_of(result) == {}
    assert (
        "probe 9: tools/call hdr_tool without Mcp-Param-Region → HTTP 400 with JSON-RPC -32020"
        in result.details
    )


async def test_misbehaving_401_everywhere_is_gate_blocked() -> None:
    app = misbehaving_server.build_app(Toggles(status=401))
    transcript = Transcript()
    config = ClientConfig(url=ASGI_URL)
    factory = asgi_factory(app)
    async with RawClient(config, transcript, label="main", transport=factory()) as raw:
        ctx = _Ctx(config, transcript, raw, factory)
        await runner._internal_gate(ctx)
    assert ctx.gate_blocked == "Not evaluated: HTTP 401 (pass --token)"


# =========================================================================== L3: TCP (runner, safety, hazards)

ALLOWED_METHODS = {"tools/list", "server/discover", UNKNOWN_METHOD}


@pytest.mark.integration
async def test_tcp_reference_runner_pass(reference_url: str, reference_url_json: str) -> None:
    for url in (reference_url, reference_url_json):
        report = await runner.run_checks(ClientConfig(url=url), [ProtocolCheck()])
        (result,) = report.checks
        assert result.status is Status.PASS, result.model_dump()
        assert report.summary.exit_code == 0
        assert report.summary.warned == 0


@pytest.mark.integration
async def test_tcp_request_log_only_allowed_requests_and_fresh_connections(
    misbehaving_url: Callable[[Toggles], str],
) -> None:
    url = misbehaving_url(Toggles(error_shapes_catalogue=True))
    report = await runner.run_checks(ClientConfig(url=url), [ProtocolCheck()])
    (result,) = report.checks
    assert result.status is Status.PASS, result.model_dump()
    entries = fetch_request_log(url)
    assert {e["verb"] for e in entries} == {"POST"}
    calls = [e for e in entries if e["method"] == "tools/call"]
    assert len(calls) == 4
    assert calls[0]["body"]["params"] == {
        "name": "hdr_tool",
        "arguments": {"region": "mcpcheck"},
        "_meta": calls[0]["body"]["params"]["_meta"],
    }
    assert "mcp-param-region" not in calls[0]["headers"]
    # the Mcp-Name probes name a tool that does not exist: nothing can execute
    assert [c["body"]["params"]["name"] for c in calls[1:]] == [wire.UNKNOWN_TOOL_NAME] * 3
    assert {e["method"] for e in entries} <= ALLOWED_METHODS | {"tools/call"}
    probe_entries = entries[1:]  # entry 0 is the internal gate on main
    assert len(probe_entries) == 12
    assert len({e["client_port"] for e in probe_entries}) == 12  # one fresh connection per probe
    assert result.requests == 12


@pytest.mark.integration
@pytest.mark.slow
async def test_tcp_slow_server_is_inconclusive_within_budget(
    misbehaving_url: Callable[[Toggles], str],
) -> None:
    url = misbehaving_url(Toggles(slow_seconds=2))
    t0 = time.perf_counter()
    report = await runner.run_checks(ClientConfig(url=url, timeout=0.5), [ProtocolCheck()])
    elapsed = time.perf_counter() - t0
    (result,) = report.checks
    assert result.status is Status.INCONCLUSIVE
    assert result.summary.startswith("Not evaluated: request exceeded the 0.5s total budget")
    assert elapsed < 10  # gate + 11 probes x 0.5 s, far below the 10x check budget


@pytest.mark.integration
async def test_tcp_legacy_probes_get_405_on_misbehaving(
    misbehaving_url: Callable[[Toggles], str],
) -> None:
    url = misbehaving_url(Toggles())
    report = await runner.run_checks(ClientConfig(url=url, legacy_probes=True), [ProtocolCheck()])
    (result,) = report.checks
    assert result.status is Status.PASS
    verbs = [e["verb"] for e in fetch_request_log(url)]
    assert verbs.count("GET") == 1
    assert set(verbs) == {"POST", "GET"}


async def test_failure_evidence_is_json_serialisable() -> None:
    result, _, _ = await run_scripted(Ladder(mismatch="accept", unsupported="no_data"))
    dumped = json.loads(result.model_dump_json())
    assert dumped["status"] == "fail"
    assert [x["code"] for x in dumped["failures"]] == [
        "protocol.header_mismatch_accepted",
        "protocol.unsupported_data_missing",
    ]
    assert dumped["failures"][1]["evidence"]["rpc_code"] == -32022
