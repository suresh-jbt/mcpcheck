"""Tests for the ``errors`` check (PLAN.md §7.5 part 9, §10.4/§10.5 rows).

L1: ``ScriptedServer`` over ``httpx2.MockTransport`` (verdict logic, what mcpcheck sends).
L2: the ASGI fixture apps (reference server, misbehaving ``error_shapes_catalogue``).
L3: uvicorn over TCP for hazards (timeouts) and the request-log safety assertion.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from mcp.shared.inbound import encode_header_value

from mcpcheck import wire
from mcpcheck.checks.base import CheckContext, Findings
from mcpcheck.checks.errors import ErrorsCheck, _retry_after_seconds, is_actionable, judge_message
from mcpcheck.client import ClientConfig, RawClient, Transcript
from mcpcheck.models import CheckResult, Level, Status
from mcpcheck.runner import run_checks
from tests.conftest import ASGI_BASE_URL, ASGI_CLIENT_PORT, fetch_request_log
from tests.fixtures import misbehaving_server, reference_server
from tests.fixtures.misbehaving_server import CATALOGUE_TOOLS, Toggles
from tests.helpers.scripted import ScriptedServer, error_response, list_result, ok, sse_body

pytestmark = pytest.mark.anyio

URL = "http://127.0.0.1:1/mcp"
READ_ONLY: dict[str, bool] = {"readOnlyHint": True, "destructiveHint": False}
Q_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"q": {"type": "string"}},
    "required": ["q"],
}
ACTIONABLE = "Invalid arguments for tool '{name}': 'q' is a required property"
CATALOGUE_BY_NAME: dict[str, dict[str, Any]] = {t["name"]: t for t in CATALOGUE_TOOLS}

Responder = Callable[[dict[str, Any]], httpx2.Response]


# --------------------------------------------------------------------------- helpers


def tool(
    name: str, schema: dict[str, Any] | None = None, annotations: dict[str, Any] | None = READ_ONLY
) -> dict[str, Any]:
    t: dict[str, Any] = {"name": name, "inputSchema": schema if schema is not None else Q_SCHEMA}
    if annotations is not None:
        t["annotations"] = dict(annotations)
    return t


def is_error(text: str, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "resultType": "complete",
        "content": [{"type": "text", "text": text}],
        "isError": True,
    }
    result.update(extra)
    return result


def success(text: str = "done") -> dict[str, Any]:
    return {"resultType": "complete", "content": [{"type": "text", "text": text}], "isError": False}


def actionable_for(body: dict[str, Any]) -> str:
    return ACTIONABLE.format(name=body["params"]["name"])


def scripted(
    tools: list[dict[str, Any]],
    responders: dict[str, Responder],
    *,
    sse: bool = False,
    default: Responder | None = None,
) -> ScriptedServer:
    """A server listing ``tools`` whose ``tools/call`` reply is chosen by tool name."""

    def default_call(body: dict[str, Any]) -> httpx2.Response:
        return ok(body, is_error(actionable_for(body)), sse=sse)

    def call(body: dict[str, Any], _req: httpx2.Request) -> httpx2.Response:
        name = body["params"]["name"]
        responder = responders.get(name, default or default_call)
        return responder(body)

    return ScriptedServer(
        routes={
            "tools/list": lambda b, _r: ok(b, list_result(tools), sse=sse),
            "tools/call": call,
        }
    )


async def run_with(raw: RawClient, cfg: ClientConfig, transcript: Transcript) -> CheckResult:
    """Run the check the way the runner does and freeze the result."""
    ctx = CheckContext(cfg, transcript, raw)
    ctx.current_check = "errors"
    transcript.current_check = "errors"
    check = ErrorsCheck()
    f = Findings(check, strict=cfg.strict)
    try:
        await check.run(ctx, f)
    finally:
        await raw.aclose()
    return f.to_result(requests=len(transcript))


async def run_scripted(server: ScriptedServer, **cfg_kw: Any) -> CheckResult:
    cfg = ClientConfig(url=URL, **cfg_kw)
    transcript = Transcript()
    return await run_with(server.raw_client(cfg, transcript), cfg, transcript)


async def run_asgi(
    app: Any, *, lifespan: bool = False, client_port: int = ASGI_CLIENT_PORT, **cfg_kw: Any
) -> CheckResult:
    cfg = ClientConfig(url=f"{ASGI_BASE_URL}/mcp", **cfg_kw)
    transcript = Transcript()
    transport = httpx2.ASGITransport(app, client=("127.0.0.1", client_port))
    raw = RawClient(cfg, transcript, transport=transport)
    if lifespan:
        async with app.router.lifespan_context(app):
            return await run_with(raw, cfg, transcript)
    return await run_with(raw, cfg, transcript)


def catalogue_toggles(*names: str, catalogue: bool = True, **toggle_kw: Any) -> Toggles:
    """Misbehaving toggles listing the named catalogue tools first.

    With ``catalogue=True`` the remaining 28 catalogue tools follow (probe with
    ``max_tools`` = number of eligible named tools); ``catalogue=False`` lists only
    the named tools, answered by the regular (actionable) ``tools/call`` path.
    """
    return Toggles(
        tools=tuple(CATALOGUE_BY_NAME[n] for n in names),
        error_shapes_catalogue=catalogue,
        **toggle_kw,
    )


async def run_catalogue(*names: str, probe: str = "safe", **toggle_kw: Any) -> CheckResult:
    app = misbehaving_server.build_app(catalogue_toggles(*names, **toggle_kw))
    return await run_asgi(app, probe=probe, max_tools=len(names))


def codes(result: CheckResult) -> dict[str, list[str]]:
    """Finding codes by severity (``errors.tool_skipped`` info entries left out)."""
    return {
        "must": [x.code for x in result.failures],
        "warn": [x.code for x in result.warnings],
        "info": [x.code for x in result.info if x.code != "errors.tool_skipped"],
    }


def skipped(result: CheckResult) -> list[Any]:
    return [x for x in result.info if x.code == "errors.tool_skipped"]


def finding(result: CheckResult, code: str) -> Any:
    for x in (*result.failures, *result.warnings, *result.info):
        if x.code == code:
            return x
    msg = f"{code} not found in {codes(result)}"
    raise AssertionError(msg)


def calls(server: ScriptedServer) -> list[Any]:
    return [s for s in server.seen if s.method == "tools/call"]


def called_names(log: list[dict[str, Any]]) -> list[str]:
    return [
        str(e["body"]["params"]["name"])
        for e in log
        if e.get("method") == "tools/call" and isinstance(e.get("body"), dict)
    ]


# =========================================================================== pure


@pytest.mark.parametrize(
    ("text", "failed"),
    [
        ("Invalid arguments for tool 'echo_required': 'message' is a required property", []),
        (
            (
                "Error executing tool echo_required: 1 validation error for echo_requiredArguments\n"
                "repeat\n  Field required [type=missing, input_value={}, input_type=dict]"
            ),
            [],
        ),
        ("Unknown tool: mcpcheck__nonexistent_tool_7f3a", []),
        ("error", ["A1", "A2", "A4"]),
        ("Internal error", ["A1", "A2", "A4"]),
        ("500 Internal Server Error", ["A2", "A4"]),
        ("HTTP 400", ["A1", "A2", "A4"]),
        ("", ["A1", "A4"]),
        (
            (
                'Traceback (most recent call last):\n  File "/srv/tools/handler.py", line 42, in run\n'
                "    raise ValueError(arguments)\nValueError: {}"
            ),
            ["A3", "A4"],
        ),
        ("ValueError: the q property must be a string", ["A3"]),
        ("    at run (/srv/handler.js:42:13) while validating the message property", ["A3"]),
        ("The weather service is temporarily unavailable, please retry later", ["A4"]),
        ("The city property must be a non-empty string naming a known city", []),
    ],
)
def test_judge_message_is_error_result(text: str, failed: list[str]) -> None:
    got = judge_message(
        text, tool_name="echo_required", properties=("message", "repeat"), jsonrpc=False
    )
    assert got == failed
    assert is_actionable(got) is (failed == [])


def test_judge_message_a4_matches_normalised_property_and_tool_name() -> None:
    assert (
        judge_message(
            "Please provide a value for max-results.",
            tool_name="t",
            properties=("max_results",),
            jsonrpc=False,
        )
        == []
    )
    assert (
        judge_message(
            "lookupWeather cannot run without a city argument",
            tool_name="lookup_weather",
            properties=(),
            jsonrpc=False,
        )
        == []
    )


@pytest.mark.parametrize(
    ("text", "a5"),
    [
        ("Invalid params for tool 'x': 'q' is a required property", False),
        ("Invalid params for tool 'x'. Please provide q. Then retry.", True),
        ("Invalid params for tool 'x':\n'q' is a required property", True),
        ("Invalid params for tool 'x': " + "q " * 120, True),
    ],
)
def test_judge_message_a5_only_for_jsonrpc(text: str, a5: bool) -> None:
    assert ("A5" in judge_message(text, tool_name="x", properties=("q",), jsonrpc=True)) is a5
    assert "A5" not in judge_message(text, tool_name="x", properties=("q",), jsonrpc=False)


def test_retry_after_parsing() -> None:
    assert _retry_after_seconds(None) is None
    assert _retry_after_seconds("") is None
    assert _retry_after_seconds("2") == 2.0
    assert _retry_after_seconds("nonsense") is None
    seconds = _retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT")
    assert seconds == 0.0  # in the past → immediate


# =========================================================================== L1: what we send


@pytest.mark.parametrize("sse", [False, True])
async def test_probe_request_shape(sse: bool) -> None:
    server = scripted([tool("echo")], {}, sse=sse)
    result = await run_scripted(server)
    assert result.status is Status.PASS
    assert result.summary == "Tool errors carry actionable messages"
    assert server.methods() == ["tools/list", "tools/call", "tools/call"]
    probe, unknown = calls(server)
    h = probe.headers
    assert h["mcp-method"] == "tools/call"
    assert h["mcp-name"] == "echo"
    assert h["mcp-protocol-version"] == wire.PROTOCOL_VERSION
    assert h["accept"] == "application/json, text/event-stream"
    assert h["content-type"] == "application/json"
    assert not [k for k in h if k.lower().startswith("mcp-param-")]
    assert probe.body is not None
    assert probe.body["id"] == "errors:0:echo"
    assert probe.body["params"]["name"] == "echo"
    assert probe.body["params"]["arguments"] == {}
    meta = probe.meta
    assert meta is not None
    assert meta[wire.META_CLIENT_CAPABILITIES] == {}
    assert meta[wire.META_PROTOCOL_VERSION] == wire.PROTOCOL_VERSION
    assert "progressToken" not in probe.body["params"]["_meta"]
    assert wire.META_LOG_LEVEL not in meta
    assert unknown.body is not None
    assert unknown.body["params"] == {
        "_meta": meta,
        "name": wire.UNKNOWN_TOOL_NAME,
        "arguments": {},
    }
    assert unknown.headers["mcp-name"] == wire.UNKNOWN_TOOL_NAME
    assert unknown.body["id"] == f"errors:1:{wire.UNKNOWN_TOOL_NAME}"
    assert result.requests == 3


async def test_probe_none_sends_nothing() -> None:
    server = scripted([tool("echo")], {})
    result = await run_scripted(server, probe="none")
    assert result.status is Status.INCONCLUSIVE
    assert result.summary == "Skipped: --probe none"
    assert server.seen == []
    assert result.requests == 0


async def test_no_tools_inconclusive() -> None:
    server = scripted([], {})
    result = await run_scripted(server)
    assert result.status is Status.INCONCLUSIVE
    assert result.summary == "Server exposes no tools"
    assert server.methods() == ["tools/list"]


async def test_tools_list_not_ok_inconclusive() -> None:
    server = ScriptedServer()  # every method → 404 -32601
    result = await run_scripted(server)
    assert result.status is Status.INCONCLUSIVE
    assert "tool listing unavailable" in result.summary
    assert server.methods() == ["tools/list"]


async def test_no_probe_safe_tools_inconclusive_but_unknown_probe_runs() -> None:
    server = scripted(
        [tool("a", annotations=None), tool("b", annotations={"destructiveHint": True})], {}
    )
    result = await run_scripted(server)
    assert result.status is Status.INCONCLUSIVE
    assert result.summary == (
        "No probe-safe tools (2 listed; annotate readOnlyHint/destructiveHint or use --probe all)"
    )
    assert [c.body["params"]["name"] for c in calls(server)] == [wire.UNKNOWN_TOOL_NAME]
    assert [x.subject for x in skipped(result)] == ["a", "b"]


async def test_no_testable_tools_under_all_inconclusive() -> None:
    server = scripted([tool("a", {"type": "object"}, None)], {})
    result = await run_scripted(server, probe="all")
    assert result.status is Status.INCONCLUSIVE
    assert result.summary.startswith("No testable tools (1 listed")


async def test_probe_all_logs_warning_and_calls_unannotated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    opt = {"type": "object", "properties": {"q": {"type": "string"}}}
    server = scripted([tool("danger", opt, None)], {})
    with caplog.at_level(logging.WARNING, logger="mcpcheck.errors"):
        result = await run_scripted(server, probe="all")
    assert any("--probe all" in r.getMessage() for r in caplog.records)
    assert result.status is Status.PASS
    probe = calls(server)[0]
    assert probe.body["params"]["arguments"] == {"q": 12345}


async def test_safe_never_logs_or_calls_unannotated(caplog: pytest.LogCaptureFixture) -> None:
    server = scripted([tool("danger", annotations=None), tool("safe_one")], {})
    with caplog.at_level(logging.WARNING, logger="mcpcheck.errors"):
        result = await run_scripted(server)
    assert not caplog.records
    assert [c.body["params"]["name"] for c in calls(server)] == ["safe_one", wire.UNKNOWN_TOOL_NAME]
    assert result.status is Status.PASS


async def test_max_tools_truncates_to_n_plus_one_posts() -> None:
    server = scripted([tool(f"t{i}") for i in range(6)], {})
    result = await run_scripted(server, max_tools=3)
    assert [c.body["params"]["name"] for c in calls(server)] == [
        "t0",
        "t1",
        "t2",
        wire.UNKNOWN_TOOL_NAME,
    ]
    assert len(calls(server)) == 3 + 1
    assert [x.subject for x in skipped(result)] == ["t3", "t4", "t5"]
    assert all("--max-tools 3" in x.message for x in skipped(result))


async def test_duplicate_tool_names_probe_first_occurrence_only() -> None:
    server = scripted([tool("dup"), tool("dup")], {})
    await run_scripted(server)
    assert [c.body["params"]["name"] for c in calls(server)] == ["dup", wire.UNKNOWN_TOOL_NAME]


# --------------------------------------------------------------------------- x-mcp-header


HDR_OPTIONAL: dict[str, Any] = {
    "type": "object",
    "properties": {"region": {"type": "string", "x-mcp-header": "Region"}},
}


async def test_x_mcp_header_safe_skips_tool_whose_payloads_carry_annotated_values() -> None:
    server = scripted([tool("hdr", HDR_OPTIONAL)], {})
    result = await run_scripted(server)
    assert [c.body["params"]["name"] for c in calls(server)] == [wire.UNKNOWN_TOOL_NAME]
    assert "x-mcp-header" in skipped(result)[0].message


async def test_x_mcp_header_all_mode_emits_param_header() -> None:
    server = scripted([tool("hdr", HDR_OPTIONAL)], {})
    result = await run_scripted(server, probe="all")
    probe = calls(server)[0]
    assert probe.body["params"]["arguments"] == {"region": 12345}
    assert probe.headers["mcp-param-region"] == "12345"
    assert result.status is Status.PASS


async def test_unicode_tool_name_sent_base64_in_mcp_name() -> None:
    server = scripted([tool("工具_unicode")], {})
    result = await run_scripted(server)
    probe = calls(server)[0]
    assert probe.headers["mcp-name"] == encode_header_value("工具_unicode")
    assert probe.headers["mcp-name"].startswith("=?base64?")
    assert probe.body["params"]["name"] == "工具_unicode"
    assert result.status is Status.PASS


# =========================================================================== L1: classification table


@pytest.mark.parametrize("sse", [False, True])
async def test_both_error_shapes_accepted(sse: bool) -> None:
    server = scripted(
        [tool("iserr"), tool("rpc200"), tool("rpc400")],
        {
            "rpc200": lambda b: error_response(b, -32602, actionable_for(b), status=200, sse=sse),
            "rpc400": lambda b: error_response(b, -32602, actionable_for(b), status=400, sse=sse),
        },
        sse=sse,
    )
    result = await run_scripted(server)
    assert result.status is Status.PASS, codes(result)
    assert result.failures == result.warnings == []


@pytest.mark.parametrize("sse", [False, True])
async def test_null_id_on_400_is_should(sse: bool) -> None:
    server = scripted(
        [tool("nullid")],
        {
            "nullid": lambda b: error_response(
                b, -32602, actionable_for(b), status=400, null_id=True, sse=sse
            )
        },
        sse=sse,
    )
    result = await run_scripted(server)
    assert result.status is Status.WARN
    assert codes(result)["warn"] == ["errors.id_null_on_4xx"]
    assert finding(result, "errors.id_null_on_4xx").level is Level.SHOULD
    assert result.summary == "Tool errors actionable; 1 warning (id_null_on_4xx)"


@pytest.mark.parametrize("sse", [False, True])
async def test_id_mismatch_fails(sse: bool) -> None:
    def other(b: dict[str, Any]) -> httpx2.Response:
        return ok({**b, "id": "other"}, is_error(actionable_for(b)), sse=sse)

    result = await run_scripted(scripted([tool("mismatch")], {"mismatch": other}, sse=sse))
    assert result.status is Status.FAIL
    assert codes(result)["must"] == ["errors.id_mismatch"]
    assert finding(result, "errors.id_mismatch").evidence["probe"]["shape"] == "id_mismatch"


async def test_null_id_result_on_200_is_mismatch() -> None:
    result = await run_scripted(
        scripted([tool("t")], {"t": lambda b: ok({**b, "id": None}, is_error(actionable_for(b)))})
    )
    assert codes(result)["must"] == ["errors.id_mismatch"]


@pytest.mark.parametrize("sse", [False, True])
async def test_unexpected_success_missing_required_fails(sse: bool) -> None:
    server = scripted(
        [tool("garbage")], {"garbage": lambda b: ok(b, success("Report queued"), sse=sse)}, sse=sse
    )
    result = await run_scripted(server)
    assert result.status is Status.FAIL
    assert result.summary == "1 tool(s) executed on schema-invalid input"
    x = finding(result, "errors.accepted_invalid_input")
    assert x.level is Level.MUST
    assert x.tool == "garbage"
    assert x.evidence["probe"]["strategy"] == "missing-required"
    assert x.evidence["probe"]["arguments"] == {}
    assert x.evidence["probe"]["request_id"] == "errors:0:garbage"
    assert x.evidence["probe"]["http_status"] == 200
    assert x.evidence["probe"]["media_type"] == ("text/event-stream" if sse else "application/json")
    assert x.evidence["probe"]["shape"] == "success"
    assert x.evidence["got"]["content"][0]["text"] == "Report queued"
    assert "MUST validate" in x.evidence["expected"]


@pytest.mark.parametrize("sse", [False, True])
async def test_unexpected_success_wrong_type_warns(sse: bool) -> None:
    opt = {"type": "object", "properties": {"q": {"type": "string"}}}
    server = scripted(
        [tool("lenient", opt)], {"lenient": lambda b: ok(b, success("coerced"), sse=sse)}, sse=sse
    )
    result = await run_scripted(server)
    assert result.status is Status.WARN
    x = finding(result, "errors.accepted_wrong_type")
    assert x.level is Level.SHOULD
    assert x.evidence["probe"]["strategy"] == "wrong-type-optional"
    assert x.evidence["probe"]["arguments"] == {"q": 12345}


async def test_enum_sentinel_success_is_must() -> None:
    schema = {"type": "object", "properties": {"mode": {"enum": ["a", "b"]}}}
    server = scripted([tool("enum_tool", schema)], {"enum_tool": lambda b: ok(b, success())})
    result = await run_scripted(server)
    assert codes(result)["must"] == ["errors.accepted_invalid_input"]


@pytest.mark.parametrize("sse", [False, True])
async def test_generic_message_ratio_warns(sse: bool) -> None:
    traceback_text = 'Traceback (most recent call last):\n  File "h.py", line 1\nValueError: x'
    server = scripted(
        [tool("good"), tool("generic"), tool("crash")],
        {
            "generic": lambda b: ok(b, is_error("error"), sse=sse),
            "crash": lambda b: ok(b, is_error(traceback_text), sse=sse),
        },
        sse=sse,
    )
    result = await run_scripted(server)
    assert result.status is Status.WARN
    assert result.summary == "2 of 4 probed tools return non-actionable errors"
    assert codes(result)["warn"] == [
        "errors.non_actionable_messages",
        "errors.non_actionable_message",
        "errors.non_actionable_message",
    ]
    per_tool = {
        x.subject: (x.evidence or {})["probe"]["heuristics_failed"] for x in result.warnings[1:]
    }
    assert per_tool == {"generic": ["A1", "A2", "A4"], "crash": ["A3", "A4"]}
    assert all(x.level is Level.BEST_PRACTICE for x in result.warnings)


async def test_ratio_at_or_above_threshold_passes_with_details() -> None:
    server = scripted(
        [tool(f"g{i}") for i in range(4)],
        {"g0": lambda b: ok(b, is_error("error"))},
    )
    result = await run_scripted(server)
    assert result.status is Status.PASS  # 4 of 5 actionable = 0.8
    assert "g0" in result.details


@pytest.mark.parametrize("sse", [False, True])
async def test_empty_content_non_actionable(sse: bool) -> None:
    server = scripted(
        [tool("empty")],
        {
            "empty": lambda b: ok(
                b, {"resultType": "complete", "content": [], "isError": True}, sse=sse
            )
        },
        sse=sse,
    )
    result = await run_scripted(server)
    assert result.status is Status.WARN
    assert finding(result, "errors.non_actionable_message").evidence["probe"][
        "heuristics_failed"
    ] == ["A1", "A4"]


@pytest.mark.parametrize(
    ("code", "expected_code", "level", "status"),
    [
        (-32042, "errors.forbidden_error_code", Level.MUST, Status.FAIL),
        (-32002, "errors.forbidden_error_code", Level.MUST, Status.FAIL),
        (-32050, "errors.reserved_error_code", Level.MUST, Status.FAIL),
        (-32099, "errors.reserved_error_code", Level.MUST, Status.FAIL),
        (-32000, "errors.legacy_error_code", Level.SHOULD, Status.WARN),
        (-32019, "errors.legacy_error_code", Level.SHOULD, Status.WARN),
    ],
)
@pytest.mark.parametrize("sse", [False, True])
async def test_error_code_ranges(
    code: int, expected_code: str, level: Level, status: Status, sse: bool
) -> None:
    server = scripted(
        [tool("coded")],
        {"coded": lambda b: error_response(b, code, actionable_for(b), status=200, sse=sse)},
        sse=sse,
    )
    result = await run_scripted(server)
    assert result.status is status
    x = finding(result, expected_code)
    assert x.level is level
    assert x.evidence["probe"]["error_code"] == code
    if expected_code == "errors.forbidden_error_code":
        assert result.summary == f"Forbidden error code {code} from `coded`"
    if expected_code == "errors.reserved_error_code":
        assert result.summary == f"Reserved error code {code} from `coded`"


async def test_application_defined_code_outside_reserved_range_is_fine() -> None:
    server = scripted(
        [tool("app")], {"app": lambda b: error_response(b, -31000, actionable_for(b), status=200)}
    )
    result = await run_scripted(server)
    assert result.status is Status.PASS


@pytest.mark.parametrize("sse", [False, True])
async def test_internal_error_not_actionable(sse: bool) -> None:
    server = scripted(
        [tool("boom")],
        {"boom": lambda b: error_response(b, -32603, "Internal error", status=200, sse=sse)},
        sse=sse,
    )
    result = await run_scripted(server)
    assert result.status is Status.WARN
    assert finding(result, "errors.non_actionable_message").evidence["probe"][
        "heuristics_failed"
    ] == ["A1", "A2", "A4"]
    assert result.summary == "1 of 2 probed tools return non-actionable errors"


async def test_verbose_jsonrpc_message_is_should() -> None:
    text = "Invalid params for tool 'wordy': 'q' is required. Please add it. Then retry the call."
    server = scripted(
        [tool("wordy")], {"wordy": lambda b: error_response(b, -32602, text, status=400)}
    )
    result = await run_scripted(server)
    assert result.status is Status.WARN
    assert codes(result)["warn"] == ["errors.verbose_error_message"]
    assert (
        "A5"
        in finding(result, "errors.verbose_error_message").evidence["probe"]["heuristics_failed"]
    )


async def test_malformed_error_fails() -> None:
    def bad(b: dict[str, Any]) -> httpx2.Response:
        return httpx2.Response(
            400, json={"jsonrpc": "2.0", "id": b["id"], "error": {"code": "E1", "message": "x"}}
        )

    result = await run_scripted(scripted([tool("bad")], {"bad": bad}))
    assert codes(result)["must"] == ["errors.malformed_error"]


async def test_empty_error_message_is_malformed() -> None:
    result = await run_scripted(
        scripted([tool("bad")], {"bad": lambda b: error_response(b, -32602, "", status=400)})
    )
    assert codes(result)["must"] == ["errors.malformed_error"]


@pytest.mark.parametrize("sse", [False, True])
async def test_missing_resulttype_fails(sse: bool) -> None:
    server = scripted(
        [tool("legacy")],
        {
            "legacy": lambda b: ok(
                b,
                {"content": [{"type": "text", "text": actionable_for(b)}], "isError": True},
                sse=sse,
            )
        },
        sse=sse,
    )
    result = await run_scripted(server)
    assert result.status is Status.FAIL
    assert codes(result)["must"] == ["errors.missing_result_type"]
    assert result.summary == "`legacy` result lacks resultType"
    assert (
        finding(result, "errors.missing_result_type").evidence["probe"]["shape"] == "legacy_result"
    )


async def test_result_not_object_is_missing_result_type() -> None:
    def weird(b: dict[str, Any]) -> httpx2.Response:
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": b["id"], "result": "nope"})

    result = await run_scripted(scripted([tool("weird")], {"weird": weird}))
    assert codes(result)["must"] == ["errors.missing_result_type"]


async def test_unknown_result_type_fails() -> None:
    server = scripted(
        [tool("odd")], {"odd": lambda b: ok(b, {"resultType": "partial", "content": []})}
    )
    result = await run_scripted(server)
    assert codes(result)["must"] == ["errors.unknown_result_type"]
    assert result.summary == "`odd` returned an unrecognized resultType"


@pytest.mark.parametrize("sse", [False, True])
async def test_structured_content_is_info_only(sse: bool) -> None:
    server = scripted(
        [tool("structured")],
        {
            "structured": lambda b: ok(
                b,
                is_error(actionable_for(b), structuredContent={"error": "invalid_arguments"}),
                sse=sse,
            )
        },
        sse=sse,
    )
    result = await run_scripted(server)
    assert result.status is Status.PASS
    assert codes(result)["info"] == ["errors.structured_content_present"]


@pytest.mark.parametrize("sse", [False, True])
async def test_input_required_state_inconclusive(sse: bool) -> None:
    server = scripted(
        [tool("state")],
        {
            "state": lambda b: ok(
                b, {"resultType": "input_required", "requestState": "eyJ0b2tlbiI6ImFiYyJ9"}, sse=sse
            )
        },
        sse=sse,
    )
    result = await run_scripted(server)
    assert result.status is Status.INCONCLUSIVE
    assert "requestState only" in result.summary
    assert codes(result) == {"must": [], "warn": [], "info": ["errors.input_required_state_only"]}


@pytest.mark.parametrize("sse", [False, True])
async def test_input_required_with_requests_fails(sse: bool) -> None:
    payload = {
        "resultType": "input_required",
        "inputRequests": {
            "confirm": {"method": "elicitation/create", "params": {"message": "Confirm?"}}
        },
    }
    server = scripted([tool("asks")], {"asks": lambda b: ok(b, payload, sse=sse)}, sse=sse)
    result = await run_scripted(server)
    assert result.status is Status.FAIL
    assert codes(result)["must"] == ["errors.undeclared_input_requests"]
    assert result.summary == "`asks` requested undeclared client input"


async def test_input_required_empty_fails() -> None:
    server = scripted([tool("empty")], {"empty": lambda b: ok(b, {"resultType": "input_required"})})
    result = await run_scripted(server)
    assert codes(result)["must"] == ["errors.empty_input_required"]
    assert result.summary == "`empty` returned an empty input_required result"


async def test_malformed_result_is_should() -> None:
    server = scripted(
        [tool("bad_content")], {"bad_content": lambda b: ok(b, is_error("x", content="not a list"))}
    )
    result = await run_scripted(server)
    assert "errors.malformed_result" in codes(result)["warn"]


# --------------------------------------------------------------------------- infrastructure outcomes


@pytest.mark.parametrize("sse", [False, True])
async def test_401_inconclusive(sse: bool) -> None:
    def denied(_b: dict[str, Any]) -> httpx2.Response:
        return httpx2.Response(401, headers={"www-authenticate": 'Bearer realm="x"'})

    result = await run_scripted(scripted([tool("a"), tool("b")], {}, sse=sse, default=denied))
    assert result.status is Status.INCONCLUSIVE
    assert result.summary == "Inconclusive: HTTP 401 on tools/call (check --token)"
    assert result.failures == result.warnings == []


async def test_401_with_token_sent_is_still_inconclusive() -> None:
    result = await run_scripted(
        scripted([tool("a")], {}, default=lambda _b: httpx2.Response(401)), token="sekrit"
    )
    assert result.status is Status.INCONCLUSIVE
    assert "401" in result.summary


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(
            500,
            content=b"<html>Internal Server Error</html>",
            headers={"content-type": "text/html"},
        ),
        httpx2.Response(403, content=b"forbidden", headers={"content-type": "text/plain"}),
        httpx2.Response(404, content=b"not here"),
        httpx2.Response(405, headers={"allow": "GET"}),
        httpx2.Response(400, content=b""),
        httpx2.Response(302, headers={"location": "https://elsewhere.example.com/mcp"}),
    ],
    ids=["500-html", "403-text", "404", "405", "400-empty", "302"],
)
async def test_non_jsonrpc_4xx_5xx_3xx_inconclusive(response: httpx2.Response) -> None:
    result = await run_scripted(scripted([tool("a")], {}, default=lambda _b: response))
    assert result.status is Status.INCONCLUSIVE, codes(result)
    assert result.summary.startswith("Not evaluated")
    assert result.failures == result.warnings == []


async def test_mixed_inconclusive_and_judged_is_evaluated() -> None:
    server = scripted(
        [tool("ok_tool"), tool("broken")], {"broken": lambda _b: httpx2.Response(500)}
    )
    result = await run_scripted(server)
    assert result.status is Status.PASS
    assert "`broken`: Not evaluated: HTTP 500" in result.details


async def test_4xx_with_jsonrpc_body_is_judged_as_jsonrpc_error() -> None:
    server = scripted(
        [tool("t")], {"t": lambda b: error_response(b, -32602, actionable_for(b), status=422)}
    )
    result = await run_scripted(server)
    assert result.status is Status.PASS


@pytest.mark.parametrize(
    ("response", "media"),
    [
        (
            httpx2.Response(200, content=b"plain text", headers={"content-type": "text/plain"}),
            "text/plain",
        ),
        (
            httpx2.Response(200, content=b"<p>hi</p>", headers={"content-type": "text/html"}),
            "text/html",
        ),
        (httpx2.Response(202), "no content-type"),
    ],
    ids=["text-plain", "html", "202"],
)
async def test_bad_content_type_fails(response: httpx2.Response, media: str) -> None:
    result = await run_scripted(scripted([tool("ctype")], {"ctype": lambda _b: response}))
    assert result.status is Status.FAIL
    assert codes(result)["must"] == ["errors.bad_content_type"]
    assert finding(result, "errors.bad_content_type").tool == "ctype"
    assert result.summary == f"`tools/call` returned {media} (not JSON-RPC)"


async def test_missing_response_is_should_and_inconclusive() -> None:
    empty = httpx2.Response(200, content=b"", headers={"content-type": "application/json"})
    result = await run_scripted(scripted([tool("silent")], {"silent": lambda _b: empty}))
    assert result.status is Status.INCONCLUSIVE
    assert codes(result)["warn"] == ["errors.missing_response"]


@pytest.mark.parametrize("sse", [False, True])
async def test_header_mismatch_inconclusive_with_headers_recorded(sse: bool) -> None:
    server = scripted(
        [tool("picky")],
        {
            "picky": lambda b: error_response(
                b, -32020, "Mcp-Name header mismatch", status=400, sse=sse
            )
        },
        sse=sse,
    )
    result = await run_scripted(server)
    assert result.status is Status.INCONCLUSIVE
    x = finding(result, "errors.header_mismatch")
    sent = dict(tuple(h) for h in x.evidence["headers_sent"])
    assert sent["mcp-name"] == "picky"
    assert sent["mcp-method"] == "tools/call"


async def test_protocol_rejection_info_inconclusive() -> None:
    server = scripted(
        [tool("t")], {"t": lambda b: error_response(b, -32022, "unsupported", status=400)}
    )
    result = await run_scripted(server)
    assert result.status is Status.INCONCLUSIVE
    assert codes(result)["info"] == ["errors.protocol_rejection"]


async def test_tools_call_not_implemented_is_best_practice() -> None:
    server = scripted(
        [tool("t")],
        {},
        default=lambda b: error_response(b, -32601, "Method not found: tools/call", status=404),
    )
    result = await run_scripted(server)
    assert result.status is Status.WARN
    assert set(codes(result)["warn"]) == {"errors.tools_call_not_implemented"}
    assert finding(result, "errors.tools_call_not_implemented").level is Level.BEST_PRACTICE


# --------------------------------------------------------------------------- SSE stream contents


async def test_sse_iserror_parsed() -> None:
    result = await run_scripted(scripted([tool("t")], {}, sse=True))
    assert result.status is Status.PASS


async def test_sse_log_without_loglevel_fails() -> None:
    def with_log(b: dict[str, Any]) -> httpx2.Response:
        note = {
            "jsonrpc": "2.0",
            "method": "notifications/message",
            "params": {"level": "info", "data": "hi"},
        }
        return ok(b, is_error(actionable_for(b)), sse=True, extra_messages=(note,))

    result = await run_scripted(scripted([tool("chatty")], {"chatty": with_log}, sse=True))
    assert result.status is Status.FAIL
    assert codes(result)["must"] == ["errors.log_without_loglevel"]


async def test_sse_server_request_on_stream_fails() -> None:
    def with_request(b: dict[str, Any]) -> httpx2.Response:
        req = {"jsonrpc": "2.0", "id": "srv-1", "method": "roots/list", "params": {}}
        return ok(b, is_error(actionable_for(b)), sse=True, extra_messages=(req,))

    result = await run_scripted(scripted([tool("nosy")], {"nosy": with_request}, sse=True))
    assert codes(result)["must"] == ["errors.server_request_on_stream"]


async def test_sse_progress_without_token_is_info() -> None:
    def with_progress(b: dict[str, Any]) -> httpx2.Response:
        prog = {
            "jsonrpc": "2.0",
            "method": "notifications/progress",
            "params": {"progressToken": "x", "progress": 1},
        }
        return ok(b, is_error(actionable_for(b)), sse=True, extra_messages=(prog,))

    result = await run_scripted(scripted([tool("slowish")], {"slowish": with_progress}, sse=True))
    assert result.status is Status.PASS
    assert codes(result)["info"] == ["errors.progress_notification"]


async def test_sse_without_response_frame_is_missing_response() -> None:
    def no_response(_b: dict[str, Any]) -> httpx2.Response:
        note = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse_body(note)
        )

    result = await run_scripted(scripted([tool("t")], {"t": no_response}))
    assert codes(result)["warn"] == ["errors.missing_response"]


# --------------------------------------------------------------------------- 429 retry


async def test_429_with_retry_after_is_retried_once() -> None:
    hits: list[str] = []

    def flaky(b: dict[str, Any]) -> httpx2.Response:
        hits.append(b["id"])
        if len(hits) == 1:
            return httpx2.Response(429, headers={"retry-after": "0"})
        return ok(b, is_error(actionable_for(b)))

    result = await run_scripted(scripted([tool("flaky")], {"flaky": flaky}))
    assert result.status is Status.PASS
    assert hits == ["errors:0:flaky", "errors:0:flaky"]
    assert result.requests == 4  # list + 2 x flaky + unknown


async def test_429_without_or_with_long_retry_after_not_retried() -> None:
    for headers in ({}, {"retry-after": "3600"}):

        def busy(_b: dict[str, Any], h: dict[str, str] = headers) -> httpx2.Response:
            return httpx2.Response(429, headers=h)

        server = scripted([tool("busy")], {}, default=busy)
        result = await run_scripted(server, timeout=2)
        assert result.status is Status.INCONCLUSIVE
        assert len(calls(server)) == 2  # one busy probe (no retry) + unknown


# --------------------------------------------------------------------------- unknown tool probe


async def test_unknown_tool_executed_warns() -> None:
    server = scripted([tool("t")], {wire.UNKNOWN_TOOL_NAME: lambda b: ok(b, success("ran"))})
    result = await run_scripted(server)
    assert result.status is Status.WARN
    x = finding(result, "errors.unknown_tool_executed")
    assert x.subject == "(unknown tool)"
    assert x.tool == wire.UNKNOWN_TOOL_NAME
    assert x.level is Level.BEST_PRACTICE


async def test_unknown_tool_jsonrpc_error_accepted() -> None:
    server = scripted(
        [tool("t")],
        {
            wire.UNKNOWN_TOOL_NAME: lambda b: error_response(
                b, -32602, "Unknown tool: nope (see tools/list)", status=400
            )
        },
    )
    result = await run_scripted(server)
    assert result.status is Status.PASS


# =========================================================================== L2: ASGI fixtures


async def test_reference_server_passes() -> None:
    app = reference_server.build_app()
    result = await run_asgi(app, lifespan=True)
    assert result.status is Status.PASS, codes(result)
    assert result.summary == "Tool errors carry actionable messages"
    assert result.failures == result.warnings == result.info == []
    assert result.requests == 4  # tools/list + 2 probes + unknown probe


async def test_json_response_mode_passes() -> None:
    result = await run_asgi(reference_server.build_app(json_response=True), lifespan=True)
    assert result.status is Status.PASS, codes(result)


async def test_reference_without_output_schema_passes() -> None:
    result = await run_asgi(reference_server.build_app(output_schema=False), lifespan=True)
    assert result.status is Status.PASS, codes(result)


async def test_reference_bare_lists_nothing_inconclusive() -> None:
    result = await run_asgi(reference_server.build_app(bare=True), lifespan=True)
    assert result.status is Status.INCONCLUSIVE
    assert "tool listing unavailable" in result.summary


@pytest.mark.parametrize("sse", [False, True])
async def test_misbehaving_default_passes(sse: bool) -> None:
    result = await run_asgi(misbehaving_server.build_app(Toggles(sse=sse)))
    assert result.status is Status.PASS, codes(result)


@pytest.mark.parametrize(
    ("style", "status", "code"),
    [
        ("jsonrpc", Status.PASS, None),
        ("traceback", Status.WARN, "errors.non_actionable_messages"),
        ("bare", Status.WARN, "errors.non_actionable_messages"),
        ("success", Status.FAIL, "errors.accepted_invalid_input"),
    ],
)
async def test_error_style_rows(style: str, status: Status, code: str | None) -> None:
    result = await run_asgi(misbehaving_server.build_app(Toggles(error_style=style)))  # type: ignore[arg-type]
    assert result.status is status, codes(result)
    if code is not None:
        x = finding(result, code)
        assert x.level is (Level.MUST if status is Status.FAIL else Level.BEST_PRACTICE)
    if style == "success":
        assert result.summary == "2 tool(s) executed on schema-invalid input"
        assert "errors.unknown_tool_executed" in codes(result)["warn"]


async def test_catalogue_both_error_shapes_judged_actionable() -> None:
    cfg = ClientConfig(url=f"{ASGI_BASE_URL}/mcp", max_tools=3)
    transcript = Transcript()
    app = misbehaving_server.build_app(
        catalogue_toggles("err_good", "err_rpc_32602_200", "err_rpc_32602_400")
    )
    raw = RawClient(
        cfg, transcript, transport=httpx2.ASGITransport(app, client=("127.0.0.1", ASGI_CLIENT_PORT))
    )
    result = await run_with(raw, cfg, transcript)
    assert result.status is Status.PASS, codes(result)
    statuses = [e.status for e in transcript.exchanges if e.rpc_method == "tools/call"]
    assert statuses == [200, 200, 400, 200]


async def test_catalogue_null_id_on_400_is_should() -> None:
    result = await run_catalogue("err_rpc_nullid_400")
    assert result.status is Status.WARN
    assert codes(result)["warn"] == ["errors.id_null_on_4xx"]


async def test_catalogue_unexpected_success_missing_required_fails() -> None:
    result = await run_catalogue("ok_garbage")
    assert result.status is Status.FAIL
    x = finding(result, "errors.accepted_invalid_input")
    assert x.evidence["probe"]["strategy"] == "missing-required"
    assert x.evidence["got"]["content"][0]["text"] == "Report queued"


async def test_catalogue_wrongtype_ok_warns() -> None:
    result = await run_catalogue("wrongtype_ok")
    assert result.status is Status.WARN
    assert codes(result)["warn"] == ["errors.accepted_wrong_type"]
    assert finding(result, "errors.accepted_wrong_type").evidence["probe"]["arguments"] == {
        "q": 12345
    }


async def test_catalogue_generic_message_ratio_warns() -> None:
    result = await run_catalogue("err_good", "err_generic", "err_traceback")
    assert result.status is Status.WARN
    assert result.summary == "2 of 4 probed tools return non-actionable errors"


async def test_catalogue_empty_content_non_actionable() -> None:
    result = await run_catalogue("err_empty_content")
    assert finding(result, "errors.non_actionable_message").evidence["probe"][
        "heuristics_failed"
    ] == ["A1", "A4"]


@pytest.mark.parametrize(
    ("name", "code", "status"),
    [
        ("err_rpc_32042", "errors.forbidden_error_code", Status.FAIL),
        ("err_rpc_32050", "errors.reserved_error_code", Status.FAIL),
        ("err_rpc_32000", "errors.legacy_error_code", Status.WARN),
        ("err_rpc_32603", "errors.non_actionable_message", Status.WARN),
        ("err_no_resulttype", "errors.missing_result_type", Status.FAIL),
        ("err_structured", "errors.structured_content_present", Status.PASS),
        ("input_required_requests", "errors.undeclared_input_requests", Status.FAIL),
        ("input_required_empty", "errors.empty_input_required", Status.FAIL),
        ("ctype_text", "errors.bad_content_type", Status.FAIL),
        ("sse_notification_message", "errors.log_without_loglevel", Status.FAIL),
    ],
)
async def test_catalogue_rows(name: str, code: str, status: Status) -> None:
    result = await run_catalogue(name)
    assert result.status is status, codes(result)
    finding(result, code)


async def test_catalogue_forbidden_code_summary() -> None:
    result = await run_catalogue("err_rpc_32042")
    assert result.summary == "Forbidden error code -32042 from `err_rpc_32042`"


async def test_catalogue_input_required_state_inconclusive() -> None:
    result = await run_catalogue("input_required_state")
    assert result.status is Status.INCONCLUSIVE
    assert codes(result)["info"] == ["errors.input_required_state_only"]
    assert codes(result)["must"] == []
    # the fixture's requestState is an object; the schema says string → SDK validator complaint
    assert codes(result)["warn"] == ["errors.malformed_result"]


@pytest.mark.parametrize("name", ["http_500_html", "http_403_text"])
async def test_catalogue_infrastructure_inconclusive(name: str) -> None:
    result = await run_catalogue(name)
    assert result.status is Status.INCONCLUSIVE
    assert result.summary.startswith("Not evaluated: HTTP")
    assert result.failures == result.warnings == []


async def test_catalogue_sse_iserror_parsed() -> None:
    result = await run_catalogue("sse_iserror")
    assert result.status is Status.PASS, codes(result)


async def test_catalogue_safe_policy_never_calls_unannotated() -> None:
    toggles = catalogue_toggles("destructive_norequired", "err_good", catalogue=False)
    app = misbehaving_server.build_app(toggles)
    result = await run_asgi(app)
    names = called_names(app.state.fixture.log)
    assert "destructive_norequired" not in names
    assert names == ["err_good", wire.UNKNOWN_TOOL_NAME]
    assert [x.subject for x in skipped(result)] == ["destructive_norequired"]
    assert "--probe all" in skipped(result)[0].message
    assert result.status is Status.PASS


async def test_catalogue_probe_all_includes_destructive() -> None:
    toggles = catalogue_toggles("destructive_norequired", catalogue=False)
    app = misbehaving_server.build_app(toggles)
    result = await run_asgi(app, probe="all")
    entry = next(e for e in app.state.fixture.log if e["method"] == "tools/call")
    assert entry["body"]["params"] == {
        "_meta": entry["body"]["params"]["_meta"],
        "name": "destructive_norequired",
        "arguments": {"q": 12345},
    }
    assert result.status is Status.PASS, codes(result)


@pytest.mark.parametrize("probe", ["safe", "all"])
async def test_catalogue_permissive_schema_untestable(probe: str) -> None:
    toggles = catalogue_toggles("readonly_permissive", catalogue=False)
    app = misbehaving_server.build_app(toggles)
    result = await run_asgi(app, probe=probe)
    assert called_names(app.state.fixture.log) == [wire.UNKNOWN_TOOL_NAME]
    assert "permissive" in skipped(result)[0].message
    assert result.status is Status.INCONCLUSIVE
    assert result.summary.startswith(
        "No probe-safe tools" if probe == "safe" else "No testable tools"
    )


async def test_catalogue_unicode_name_base64_mcp_name() -> None:
    toggles = catalogue_toggles("工具_unicode")
    app = misbehaving_server.build_app(toggles)
    result = await run_asgi(app, max_tools=1)
    entry = next(e for e in app.state.fixture.log if e["method"] == "tools/call")
    assert entry["headers"]["mcp-name"] == encode_header_value("工具_unicode")
    assert result.status is Status.PASS, codes(result)  # the fixture decoded the sentinel


async def test_catalogue_x_mcp_header_prefers_missing_required() -> None:
    toggles = catalogue_toggles("hdr_tool")
    app = misbehaving_server.build_app(toggles)
    for probe in ("safe", "all"):
        app.state.fixture.clear()
        result = await run_asgi(app, probe=probe, max_tools=1)
        entry = next(e for e in app.state.fixture.log if e["method"] == "tools/call")
        assert entry["body"]["params"]["arguments"] == {}
        assert not [h for h in entry["headers"] if h.startswith("mcp-param-")]
        assert result.status is Status.PASS, codes(result)


async def test_catalogue_unknown_tool_probe_iserror_shape() -> None:
    app = misbehaving_server.build_app()
    result = await run_asgi(app)
    unknown = [e for e in app.state.fixture.log if e["method"] == "tools/call"][-1]
    assert unknown["body"]["params"]["name"] == wire.UNKNOWN_TOOL_NAME
    assert unknown["body"]["params"]["arguments"] == {}
    assert result.status is Status.PASS


async def test_catalogue_max_tools_truncates() -> None:
    app = misbehaving_server.build_app(Toggles(error_shapes_catalogue=True))
    result = await run_asgi(app, max_tools=5)
    assert len([e for e in app.state.fixture.log if e["method"] == "tools/call"]) == 5 + 1
    assert sum(1 for x in skipped(result) if "--max-tools 5" in x.message) == 30 - 5 - 2


async def test_misbehaving_no_tools_inconclusive() -> None:
    result = await run_asgi(misbehaving_server.build_app(Toggles(tools=())))
    assert result.status is Status.INCONCLUSIVE
    assert result.summary == "Server exposes no tools"


async def test_misbehaving_tools_list_404_inconclusive() -> None:
    result = await run_asgi(misbehaving_server.build_app(Toggles(pages=())))
    assert result.status is Status.INCONCLUSIVE
    assert "tool listing unavailable" in result.summary


async def test_misbehaving_status_401_everything_inconclusive() -> None:
    result = await run_asgi(misbehaving_server.build_app(Toggles(status=401)))
    assert result.status is Status.INCONCLUSIVE
    assert "--token" in result.summary


# =========================================================================== L3: TCP hazards


@pytest.mark.integration
@pytest.mark.slow
async def test_timeout_inconclusive(misbehaving_url: Callable[[Toggles], str]) -> None:
    url = misbehaving_url(catalogue_toggles("slow"))
    cfg = ClientConfig(url=url, timeout=1.0, max_tools=1)
    transcript = Transcript()
    t0 = time.perf_counter()
    result = await run_with(RawClient(cfg, transcript), cfg, transcript)
    wall = time.perf_counter() - t0
    assert wall < 3.0, wall
    assert result.status is Status.INCONCLUSIVE
    assert "budget" in result.summary
    assert result.failures == result.warnings == []


@pytest.mark.integration
async def test_request_log_contains_only_allowed_requests(
    misbehaving_url: Callable[[Toggles], str],
) -> None:
    url = misbehaving_url(Toggles(error_shapes_catalogue=True))
    cfg = ClientConfig(url=url, max_tools=30)
    report = await run_checks(cfg, [ErrorsCheck()])
    log = fetch_request_log(url)
    assert {(e["verb"], e["method"]) for e in log} == {
        ("POST", "tools/list"),
        ("POST", "tools/call"),
    }
    names = called_names(log)
    assert "destructive_norequired" not in names
    assert "readonly_permissive" not in names
    assert names[-1] == wire.UNKNOWN_TOOL_NAME
    assert names.count(wire.UNKNOWN_TOOL_NAME) == 1
    for entry in log:
        if entry["method"] != "tools/call":
            continue
        tool_name = entry["body"]["params"]["name"]
        listed = next(
            (
                t
                for t in [*misbehaving_server.DEFAULT_TOOLS, *CATALOGUE_TOOLS]
                if t["name"] == tool_name
            ),
            None,
        )
        if listed is None:
            assert tool_name == wire.UNKNOWN_TOOL_NAME
            continue
        assert listed["annotations"]["readOnlyHint"] is True
        from jsonschema import Draft202012Validator

        assert not Draft202012Validator(listed["inputSchema"]).is_valid(
            entry["body"]["params"]["arguments"]
        )
    errors_result = next(c for c in report.checks if c.name == "errors")
    assert errors_result.status is Status.FAIL  # ok_garbage et al.


@pytest.mark.integration
async def test_runner_reference_server_passes(reference_url: str) -> None:
    report = await run_checks(ClientConfig(url=reference_url), [ErrorsCheck()])
    result = next(c for c in report.checks if c.name == "errors")
    assert result.status is Status.PASS, codes(result)
    assert report.summary.exit_code == 0
    assert report.summary.failed == 0
    assert report.summary.warned == 0
