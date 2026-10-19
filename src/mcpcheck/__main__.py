"""Command-line entry point: ``mcpcheck [OPTIONS] SERVER_URL`` (PLAN.md §8.1-§8.4).

All argument validation happens before any network I/O (usage errors exit 2).
"""

from __future__ import annotations

import contextlib
import functools
import logging
import os
import sys
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import anyio
import click

from mcpcheck import runner as _runner
from mcpcheck.checks import NAMES, SelectionError, select
from mcpcheck.client import ClientConfig, Transcript
from mcpcheck.output import (
    HumanRenderer,
    TranscriptRenderer,
    make_console,
    print_schema,
    write_json,
)
from mcpcheck.sdk_compat import SdkIncompatibleError, require_sdk

if TYPE_CHECKING:
    from collections.abc import Sequence

    from rich.console import Console
    from rich.status import Status

    from mcpcheck.checks.base import Check
    from mcpcheck.models import Report

EXIT_INTERRUPTED = 130
_URL_HINT = "must be an absolute http:// or https:// URL, e.g. https://api.example.com/mcp"


def shipped_schema_text() -> str:
    """The packaged ``schema/report.v1.json`` as text."""
    return files("mcpcheck").joinpath("schema/report.v1.json").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- callbacks


def _parse_server_url(_ctx: click.Context, _param: click.Parameter, value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise click.BadParameter(_URL_HINT)
    if parts.username is not None or parts.password is not None:
        raise click.BadParameter("must not contain credentials; use --token or --header instead")
    if parts.fragment or value.endswith("#"):
        raise click.BadParameter("must not contain a #fragment")
    try:
        parts.port  # noqa: B018 — validates the port range
    except ValueError as exc:
        raise click.BadParameter(_URL_HINT) from exc
    return value


def _parse_header(
    _ctx: click.Context, _param: click.Parameter, value: tuple[str, ...]
) -> tuple[tuple[str, str], ...]:
    out: list[tuple[str, str]] = []
    for item in value:
        name, sep, val = item.partition(":")
        name = name.strip()
        if not sep or not name or any(ch.isspace() for ch in name):
            raise click.BadParameter(f"'{item}' is not in KEY:VALUE form")
        out.append((name, val.strip()))
    return tuple(out)


def _parse_origin(_ctx: click.Context, _param: click.Parameter, value: str | None) -> str | None:
    if value is None:
        return None
    parts = urlsplit(value)
    ok = (
        parts.scheme
        and parts.netloc
        and not parts.path
        and not parts.query
        and not parts.fragment
        and parts.username is None
    )
    if not ok:
        raise click.BadParameter(
            "must be a serialized origin such as https://app.example.com (no path)"
        )
    return value


def _parse_check_names(
    _ctx: click.Context, _param: click.Parameter, value: tuple[str, ...]
) -> tuple[str, ...]:
    names: list[str] = []
    for item in value:
        for raw in item.split(","):
            name = raw.strip().lower()
            if not name:
                continue
            if name not in NAMES:
                choices = ", ".join(f"'{n}'" for n in NAMES)
                raise click.BadParameter(f"'{name}' is not one of {choices}.")
            if name not in names:
                names.append(name)
    return tuple(names)


def _print_schema_and_exit(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    if not value or ctx.resilient_parsing:
        return
    print_schema(shipped_schema_text())
    ctx.exit(0)


def _validate_output_path(
    _ctx: click.Context, _param: click.Parameter, value: str | None
) -> str | None:
    """``-o FILE``: the nearest existing ancestor must be a writable directory (no I/O yet).

    ``click.Path(writable=True)`` only inspects a file that already exists; a
    read-only or non-directory parent would otherwise surface as a traceback
    *after* the run, with exit code 1.
    """
    if value is None or value == "-":
        return value
    target = Path(value)
    if target.exists() and target.is_dir():
        raise click.BadParameter(f"'{value}' is a directory")
    ancestor = target.parent if str(target.parent) else Path()
    while not ancestor.exists():
        if ancestor.parent == ancestor:
            break
        ancestor = ancestor.parent
    if not ancestor.is_dir():
        raise click.BadParameter(f"'{ancestor}' is not a directory")
    if not os.access(ancestor, os.W_OK | os.X_OK):
        raise click.BadParameter(f"directory '{ancestor}' is not writable")
    return value


def _list_checks_and_exit(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    if not value or ctx.resilient_parsing:
        return
    from mcpcheck.checks import REGISTRY

    width = max(len(c.name) for c in REGISTRY) + 2
    for cls in REGISTRY:
        click.echo(f"{cls.name:<{width}}{cls.title}")
    ctx.exit(0)


# --------------------------------------------------------------------------- command


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("server_url", metavar="SERVER_URL", callback=_parse_server_url)
@click.option(
    "--token",
    envvar="MCPCHECK_TOKEN",
    show_envvar=True,
    metavar="TEXT",
    help="Bearer token sent as 'Authorization: Bearer <TEXT>'.",
)
@click.option(
    "-H",
    "--header",
    "headers",
    multiple=True,
    metavar="KEY:VALUE",
    callback=_parse_header,
    help="Extra request header; repeatable.",
)
@click.option(
    "--origin",
    metavar="TEXT",
    callback=_parse_origin,
    help="Origin header to send on every request (default: none).",
)
@click.option(
    "--timeout",
    type=click.FloatRange(min=0, min_open=True),
    default=10.0,
    show_default=True,
    metavar="SECONDS",
    help="Per-request timeout.",
)
@click.option(
    "-c",
    "--check",
    "checks",
    multiple=True,
    metavar="NAME",
    callback=_parse_check_names,
    help=f"Run only these checks; repeatable or comma-separated. Names: {', '.join(NAMES)}.",
)
@click.option(
    "--skip",
    "skips",
    multiple=True,
    metavar="NAME",
    callback=_parse_check_names,
    help="Skip these checks; repeatable or comma-separated.",
)
@click.option(
    "--probe",
    type=click.Choice(["safe", "all", "none"]),
    default="safe",
    show_default=True,
    help=(
        "Which tools the errors check may call with invalid arguments: 'safe' = only tools "
        "annotated readOnlyHint=true or destructiveHint=false, 'all' = every tool, 'none' = no calls."
    ),
)
@click.option(
    "--max-tools",
    type=click.IntRange(min=1),
    default=20,
    show_default=True,
    metavar="N",
    help="Maximum number of tools the errors check probes.",
)
@click.option(
    "--legacy-probes",
    is_flag=True,
    help="Also send a legacy 'initialize' request and a headers-only GET to classify dual-era servers.",
)
@click.option(
    "--strict",
    is_flag=True,
    help="Treat warnings and inconclusive checks as failures (exit code 1).",
)
@click.option(
    "--json",
    "json_",
    is_flag=True,
    help="Print the JSON report to stdout instead of the human report.",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(dir_okay=False, writable=True, allow_dash=True),
    callback=_validate_output_path,
    metavar="FILE",
    help="Write the JSON report to FILE as well ('-' = stdout, implies --json).",
)
@click.option(
    "--json-schema",
    is_flag=True,
    is_eager=True,
    expose_value=False,
    callback=_print_schema_and_exit,
    help="Print the JSON Schema of the report and exit.",
)
@click.option(
    "--list-checks",
    is_flag=True,
    is_eager=True,
    expose_value=False,
    callback=_list_checks_and_exit,
    help="List the available checks and exit.",
)
@click.option(
    "-v",
    "--verbose",
    count=True,
    help="Show every HTTP exchange on stderr; -vv adds headers and bodies (secrets redacted).",
)
@click.option("--no-color", is_flag=True, help="Disable colored output (NO_COLOR=1 does the same).")
@click.option("--insecure", is_flag=True, help="Do not verify the server's TLS certificate.")
@click.version_option(
    None,
    "-V",
    "--version",
    package_name="jbt-mcpcheck",  # distribution name, not the command
    prog_name="mcpcheck",
    message="%(prog)s %(version)s",
)
@click.pass_context
def main(
    ctx: click.Context,
    server_url: str,
    token: str | None,
    headers: tuple[tuple[str, str], ...],
    origin: str | None,
    timeout: float,
    checks: tuple[str, ...],
    skips: tuple[str, ...],
    probe: str,
    max_tools: int,
    legacy_probes: bool,
    strict: bool,
    json_: bool,
    output: str | None,
    verbose: int,
    no_color: bool,
    insecure: bool,
) -> None:
    """Check an MCP server for conformance to the MCP 2026-07-28 revision over Streamable HTTP.

    SERVER_URL is the full URL of the MCP endpoint, e.g. https://api.example.com/mcp.

    mcpcheck is read-only: it sends server/discover, the */list methods, tools/call
    with deliberately invalid arguments to annotated read-only tools, header probes
    that cannot execute anything, and one OPTIONS request. Exit status is 0 when
    every selected check passes, 1 when a check fails, and 2 when mcpcheck could
    not run: a usage error, a network failure, a crash, or a run in which every
    check was inconclusive, so nothing about the server was verified.
    """
    try:
        _main(
            ctx,
            server_url,
            token,
            headers,
            origin,
            timeout,
            checks,
            skips,
            probe,
            max_tools,
            legacy_probes,
            strict,
            json_,
            output,
            verbose,
            no_color,
            insecure,
        )
    except KeyboardInterrupt:
        # Ctrl-C outside anyio.run (SDK gate, rendering) would otherwise be click's
        # "Aborted!" with exit 1, which CI reads as "a check failed"
        make_console(stderr=True, no_color=no_color).print("interrupted")
        ctx.exit(EXIT_INTERRUPTED)


def _main(
    ctx: click.Context,
    server_url: str,
    token: str | None,
    headers: tuple[tuple[str, str], ...],
    origin: str | None,
    timeout: float,
    checks: tuple[str, ...],
    skips: tuple[str, ...],
    probe: str,
    max_tools: int,
    legacy_probes: bool,
    strict: bool,
    json_: bool,
    output: str | None,
    verbose: int,
    no_color: bool,
    insecure: bool,
) -> None:
    # 1. validation (no I/O yet)
    try:
        selected = select(checks, skips)
    except SelectionError as exc:
        raise click.UsageError(str(exc)) from None
    if output == "-":
        json_, output = True, None
    _tolerate_console_encoding()

    # 2. SDK gate
    err_console = make_console(stderr=True, no_color=no_color)
    try:
        sdk = require_sdk()
    except SdkIncompatibleError as exc:
        err_console.print(f"error: {exc}")
        ctx.exit(2)
    for warning in sdk.warnings:
        err_console.print(f"warning: {warning}")
    if insecure:
        err_console.print("warning: --insecure disables TLS certificate verification")
    _configure_logging(verbose)

    # 3. config
    cfg = ClientConfig(
        url=server_url,
        token=token or None,
        extra_headers=headers,
        origin=origin,
        timeout=timeout,
        verify_tls=not insecure,
        probe=probe,  # type: ignore[arg-type]
        max_tools=max_tools,
        legacy_probes=legacy_probes,
        strict=strict,
    )

    # 4. run
    transcript = Transcript()
    report = _run(cfg, selected, transcript, err_console=err_console, quiet=json_ or verbose > 0)
    if report is None:
        ctx.exit(EXIT_INTERRUPTED)

    # 5. output
    code = report.summary.exit_code
    try:
        if verbose:
            TranscriptRenderer(err_console, verbose=verbose).render(transcript)
        if json_:
            write_json(report, stream=sys.stdout)
        else:
            HumanRenderer(make_console(no_color=no_color), verbose=verbose).render(report)
        if output is not None:
            try:
                write_json(report, path=output)
            except OSError as exc:
                err_console.print(f"error: cannot write report to {output}: {exc}")
                ctx.exit(2)
        sys.stdout.flush()
    except BrokenPipeError:
        sys.stderr.flush()
        os._exit(code)
    ctx.exit(code)


def _tolerate_console_encoding() -> None:
    """Never let a non-UTF-8 console (``PYTHONIOENCODING=ascii``, a legacy locale) crash the report.

    Characters the console cannot encode are written as backslash escapes instead.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(ValueError, OSError):  # closed or exotic stream: keep going
            reconfigure(errors="backslashreplace")


def _run(
    cfg: ClientConfig,
    selected: Sequence[Check],
    transcript: Transcript,
    *,
    err_console: Console,
    quiet: bool,
) -> Report | None:
    """Drive the runner under anyio (spinner on a TTY stderr); returns ``None`` on SIGINT."""
    status_ctx: Status | None = None
    # `Console.is_terminal` is True whenever FORCE_COLOR is set -- which CI runners
    # commonly do -- so gating on it animates the spinner into a redirected log.
    # Colour still follows FORCE_COLOR/NO_COLOR; only the animation needs a real TTY.
    if not quiet and sys.stderr.isatty():
        status_ctx = err_console.status("starting…", spinner="dots")
        status_ctx.start()

    def progress(message: str) -> None:
        if status_ctx is not None:
            status_ctx.update(message)

    try:
        return anyio.run(
            functools.partial(
                _runner.run_checks, cfg, list(selected), progress=progress, transcript=transcript
            ),
            backend="asyncio",
        )
    except KeyboardInterrupt:
        err_console.print("interrupted")
        return None
    finally:
        if status_ctx is not None:
            status_ctx.stop()


def _configure_logging(verbose: int) -> None:
    level = logging.DEBUG if verbose >= 2 else logging.WARNING
    logging.getLogger("mcpcheck").setLevel(level)
    mcp_logger = logging.getLogger("mcp")
    if verbose >= 2:
        logging.basicConfig(
            level=logging.WARNING, stream=sys.stderr, format="%(name)s: %(message)s"
        )
        mcp_logger.setLevel(logging.WARNING)
    else:
        mcp_logger.addHandler(logging.NullHandler())
        mcp_logger.propagate = False


if __name__ == "__main__":  # pragma: no cover
    main(prog_name="mcpcheck")
