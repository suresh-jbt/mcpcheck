"""Run the selected checks sequentially and assemble the report (PLAN.md §6.9-§6.10).

The handshake check is the gate: when it is inconclusive for an environment
reason or fails for an era reason, every dependent check is reported as
``inconclusive`` with code ``runner.not_run``. When handshake is deselected a
silent internal gate (one ``tools/list`` on ``main``) decides instead.
"""

from __future__ import annotations

import platform
import time
import traceback
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import anyio

from mcpcheck import __version__, compat, wire
from mcpcheck.checks.base import Check, CheckContext, Findings
from mcpcheck.checks.session import amend_result
from mcpcheck.client import ClientConfig, RawClient, ServerUnreachable, Transcript
from mcpcheck.models import (
    CheckName,
    CheckResult,
    Options,
    Report,
    Request,
    Status,
    Summary,
    aggregate_exit_code,
)
from mcpcheck.sdk_compat import installed_sdk_version
from mcpcheck.wire import OutcomeKind

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

# handshake failure codes that mean "wrong era" (dependents cannot be judged)
_ERA_FAIL_CODES = frozenset({"handshake.initialize_required", "handshake.version_unsupported"})
# a 2xx that is not a JSON-RPC response: a web page, a REST API, a broken envelope
_NOT_JSONRPC_CODE = "handshake.not_jsonrpc"
# handshake inconclusive codes that do NOT block (era confirmed)
_NON_BLOCKING_INCONCLUSIVE = frozenset({"handshake.client_capability_demanded"})
_GATE_BLOCKING_KINDS = frozenset(
    {
        OutcomeKind.AUTH_REQUIRED,
        OutcomeKind.FORBIDDEN,
        OutcomeKind.NOT_MCP_ENDPOINT,
        OutcomeKind.UNREACHABLE,
    }
)


def gate_reason(handshake_result: CheckResult) -> str | None:
    """Return the blocking reason, or ``None`` when dependents may run (§6.10)."""
    status = handshake_result.status
    if status is Status.INCONCLUSIVE:
        codes = {f.code for f in (*handshake_result.warnings, *handshake_result.info)}
        if codes & _NON_BLOCKING_INCONCLUSIVE:
            return None
        return handshake_result.summary
    if status is Status.FAIL:
        codes = {f.code for f in handshake_result.failures}
        if codes & _ERA_FAIL_CODES:
            return handshake_result.summary
        if codes == {_NOT_JSONRPC_CODE}:
            # dependents would only re-describe the same non-JSON-RPC body as header-validation
            # or cache defects; the handshake failure (exit 1) already names the problem
            return handshake_result.summary
        return None
    if status is Status.ERROR:
        return (
            handshake_result.error.message if handshake_result.error else handshake_result.summary
        )
    return None


def inconclusive_result(check: Check, *, code: str, reason: str, strict: bool) -> CheckResult:
    """A ``runner.not_run`` result for a check the gate blocked."""
    f = Findings(check, strict=strict)
    f.info(code, f"not run: {reason}", subject=check.name)
    f.inconclusive(f"Not run: {reason}")
    return f.to_result()


def error_result(
    check: Check, message: str, *, strict: bool, type_: str = "RunAborted"
) -> CheckResult:
    """An ``error`` result for a check that could not run at all (exit 2)."""
    f = Findings(check, strict=strict)
    f.error(message, type_=type_)
    f.summary(f"Error: {message}")
    return f.to_result()


async def _internal_gate(ctx: CheckContext) -> None:
    """Silent gate used when handshake is deselected: one ``tools/list`` on ``main``."""
    ctx.current_check = ""
    ctx.transcript.current_check = "runner"
    reply = await ctx.raw.post("tools/list", purpose="internal gate", check="runner")
    ctx.seed_tools_page0(reply)
    outcome = wire.classify_reply(
        reply, sent_origin=ctx.cfg.origin is not None, sent_token=bool(ctx.cfg.token)
    )
    if outcome.kind in _GATE_BLOCKING_KINDS:
        ctx.gate_blocked = outcome.detail
    elif outcome.ok:
        ctx.protocol_version = wire.PROTOCOL_VERSION
        result = reply.result or {}
        meta = result.get("_meta")
        if isinstance(meta, dict) and isinstance(meta.get(wire.META_SERVER_INFO), dict):
            ctx.server_info = meta[wire.META_SERVER_INFO]


async def run_checks(
    cfg: ClientConfig,
    checks: Sequence[Check],
    *,
    progress: Callable[[str], None] | None = None,
    transcript: Transcript | None = None,
) -> Report:
    """Run ``checks`` in the given (canonical) order and return the :class:`Report`.

    Pass your own ``transcript`` to inspect every exchange afterwards (``--verbose``).
    """
    transcript = transcript if transcript is not None else Transcript()
    t0 = time.perf_counter()
    results: list[CheckResult] = []
    async with RawClient(cfg, transcript, label="main") as raw:
        ctx = CheckContext(cfg, transcript, raw)
        aborted: str | None = None
        selected_names = [c.name for c in checks]
        if "handshake" not in selected_names:
            try:
                with anyio.fail_after(cfg.check_budget):
                    await _internal_gate(ctx)
            except TimeoutError:
                aborted = f"internal gate exceeded {cfg.check_budget:g}s"
            except ServerUnreachable as exc:
                aborted = f"server unreachable ({type(exc.cause).__name__})"
            except Exception as exc:  # noqa: BLE001 — a server reply must never escape as a traceback
                inner = compat.unwrap_exception_group(exc)
                aborted = f"internal error in the gate request: {inner!r}"
        for check in checks:
            if aborted:
                results.append(error_result(check, f"not run: {aborted}", strict=cfg.strict))
                continue
            if ctx.gate_blocked and check.name != "handshake":
                results.append(
                    inconclusive_result(
                        check, code="runner.not_run", reason=ctx.gate_blocked, strict=cfg.strict
                    )
                )
                continue
            f = Findings(check, strict=cfg.strict)
            if progress is not None:
                progress(f"running {check.name}…")
            ctx.current_check = check.name
            transcript.current_check = check.name
            before = len(transcript)
            try:
                with anyio.fail_after(cfg.check_budget):
                    await check.run(ctx, f)
            except TimeoutError:
                f.error(f"check exceeded {cfg.check_budget:g}s", type_="TimeoutError")
            except ServerUnreachable as exc:
                f.error(str(exc), type_="ServerUnreachable")
                aborted = f"server unreachable ({type(exc.cause).__name__})"
            except Exception as exc:  # noqa: BLE001 — last line of defence, reported as status error
                inner = compat.unwrap_exception_group(exc)
                f.error(
                    f"internal error: {inner!r}",
                    type_=type(inner).__name__,
                    traceback=traceback.format_exc(),
                )
            finally:
                transcript.current_check = ""
            results.append(f.to_result(requests=len(transcript) - before))
            if check.name == "handshake":
                ctx.gate_blocked = gate_reason(results[-1])
        # PLAN.md §7.2 step 3g: later checks' exchanges may reveal Mcp-Session-Id minting
        # the session check could not see while it ran; amend its result before reporting.
        results = [
            amend_result(r, transcript)
            if r.name == CheckName.SESSION and r.status is not Status.ERROR
            else r
            for r in results
        ]
        return assemble_report(cfg, results, ctx, transcript, started=t0)


def assemble_report(
    cfg: ClientConfig,
    results: list[CheckResult],
    ctx: CheckContext,
    _transcript: Transcript,
    *,
    started: float,
) -> Report:
    """Build the :class:`Report` from the per-check results and the context."""
    statuses = [r.status for r in results]
    selected = {r.name for r in results}
    summary = Summary(
        total=len(results),
        passed=statuses.count(Status.PASS),
        warned=statuses.count(Status.WARN),
        failed=statuses.count(Status.FAIL),
        inconclusive=statuses.count(Status.INCONCLUSIVE),
        errored=statuses.count(Status.ERROR),
        skipped=[name for name in CheckName if name not in selected],
        exit_code=aggregate_exit_code(statuses, strict=cfg.strict),
    )
    report = Report(
        mcpcheck_version=__version__,
        sdk_version=installed_sdk_version(),
        python_version=platform.python_version(),
        server=cfg.url,
        request=Request(
            origin=cfg.origin,
            auth="bearer" if cfg.token else "none",
            extra_headers=[name for name, _ in cfg.extra_headers],
            tls_verify=cfg.verify_tls,
        ),
        options=Options(
            strict=cfg.strict,
            probe=cfg.probe,
            max_tools=cfg.max_tools,
            timeout=cfg.timeout,
            legacy_probes=cfg.legacy_probes,
        ),
        protocol_version=ctx.protocol_version,
        server_supported_versions=ctx.server_supported_versions,
        server_info=ctx.server_info,
        era=ctx.era,
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
        checks=results,
        summary=summary,
    )
    return scrub_report(report, cfg.secrets)


def scrub_report(report: Report, secrets: Sequence[str]) -> Report:
    """Return ``report`` with every secret removed from every string it carries (§6.13).

    Finding messages, evidence, check summaries/details and ``server_info`` are
    server-influenced: a server that echoes our request headers would otherwise
    print the ``--token`` into stdout, the ``-o`` file and the JSON report. The
    ``-vv`` transcript is scrubbed separately as it is recorded.
    """
    if not secrets:
        return report
    data = wire.scrub_value(report.model_dump(mode="python", round_trip=True), secrets)
    scrubbed = Report.model_validate(data)
    # model_validate cannot carry the private --strict flag that drives ``passed``
    for before, after in zip(report.checks, scrubbed.checks, strict=True):
        after.with_strict(_strict_of(before))
    return scrubbed


def _strict_of(result: CheckResult) -> bool:
    return bool(result._strict)


__all__ = [
    "assemble_report",
    "error_result",
    "gate_reason",
    "inconclusive_result",
    "run_checks",
    "scrub_report",
]
