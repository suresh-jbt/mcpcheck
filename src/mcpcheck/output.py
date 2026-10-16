"""Human report, ``--verbose`` transcript and JSON writer (PLAN.md §8.5-§8.7).

This is the only module allowed to print. Server-controlled strings always pass
through :func:`sanitize` and ``Text.append`` (never rich markup interpolation).
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

from rich.console import Console
from rich.text import Text

from mcpcheck import __version__
from mcpcheck.models import SCHEMA_ID, Level, Report, Status
from mcpcheck.wire import MAX_ITEMS_SHOWN, PROTOCOL_VERSION

if TYPE_CHECKING:
    from collections.abc import Iterable

    from mcpcheck.client import Exchange

NAME_WIDTH = 14
BODY_PREVIEW = 4096
# C0/C1 controls and ESC, plus the invisible Unicode characters a server-controlled name
# could use to reorder or hide what the terminal shows (Trojan-Source style): zero-width
# and bidi marks (U+200B-U+200F), bidi embeddings/overrides (U+202A-U+202E), bidi isolates
# (U+2066-U+2069), the line/paragraph separators (U+2028/U+2029) and the BOM (U+FEFF).
_CONTROL_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u200b-\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069\ufeff]"
)
REPLACEMENT = "?"
"""Stand-in for a neutralized character; ASCII so that a non-UTF-8 console can print it."""

GLYPHS_UNICODE: dict[Status, str] = {
    Status.PASS: "✓",
    Status.FAIL: "✗",
    Status.WARN: "!",
    Status.INCONCLUSIVE: "?",
    Status.ERROR: "E",
}
GLYPHS_ASCII: dict[Status, str] = {
    Status.PASS: "+",
    Status.FAIL: "x",
    Status.WARN: "!",
    Status.INCONCLUSIVE: "?",
    Status.ERROR: "E",
}
STYLES: dict[Status, str] = {
    Status.PASS: "green",
    Status.FAIL: "bold red",
    Status.WARN: "yellow",
    Status.INCONCLUSIVE: "cyan",
    Status.ERROR: "bold red",
}


def make_console(
    *, stderr: bool = False, no_color: bool = False, file: IO[str] | None = None
) -> Console:
    """A console with markup/highlighting off and colour per ``--no-color``/``NO_COLOR``."""
    disable = no_color or bool(os.environ.get("NO_COLOR"))
    return Console(
        file=file,
        stderr=stderr,
        highlight=False,
        markup=False,
        soft_wrap=True,
        color_system=None if disable else "auto",
    )


def sanitize(text: str, *, limit: int = 500) -> str:
    """Neutralize server-controlled text: controls and invisible marks → ``?``, newlines → spaces, ≤limit chars."""
    text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").replace("\t", " ")
    text = _CONTROL_RE.sub(REPLACEMENT, text)
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def glyphs(console: Console) -> dict[Status, str]:
    """Status glyphs for this console (ASCII fallback when stdout is not UTF)."""
    return GLYPHS_ASCII if console.options.ascii_only else GLYPHS_UNICODE


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def final_line(report: Report) -> str:
    """The closing sentence (§8.5 templates)."""
    s = report.summary
    n, f, w, i, e = s.total, s.failed, s.warned, s.inconclusive, s.errored
    seconds = f"{report.duration_ms / 1000:.1f}"
    if e > 0:
        first = next((c for c in report.checks if c.status is Status.ERROR), None)
        why = sanitize(first.error.message if first and first.error else "error", limit=120)
        return f"Run incomplete: {e} of {n} checks could not run ({why}). See --json for details."
    if f > 0:
        parts = [f"{f} of {n} checks failed"]
        if w:
            parts.append(_plural(w, "warning"))
        if i:
            parts.append(f"{i} inconclusive")
        return ", ".join(parts) + ". See --json for details."
    if report.options.strict and (w + i) > 0:
        return f"{f + w + i} of {n} checks failed, {w + i} promoted by --strict. See --json for details."
    if w == 0 and i == 0:
        return f"All {n} checks passed in {seconds}s."
    if i == n:
        return f"No conclusive checks: {i} inconclusive in {seconds}s."
    parts = [f"All {n - i} checks passed"]
    if w:
        parts.append(_plural(w, "warning"))
    if i:
        parts.append(f"{i} inconclusive")
    return ", ".join(parts) + f" in {seconds}s."


def server_line(report: Report) -> str:
    """Second header line: identity, spoken version, check count."""
    first_error = next((c for c in report.checks if c.status is Status.ERROR), None)
    if (
        first_error is not None
        and all(c.status is Status.ERROR for c in report.checks)
        and first_error.error
    ):
        return f"server: unreachable — {sanitize(first_error.error.message, limit=200)}"
    info = report.server_info or {}
    name = info.get("name")
    version = info.get("version")
    if isinstance(name, str) and name:
        ident = sanitize(name, limit=80)
        if isinstance(version, str) and version:
            ident += f" {sanitize(version, limit=40)}"
    else:
        ident = "(no serverInfo)"
    speaks = (
        f"speaks {report.protocol_version}"
        if report.protocol_version
        else "protocol version unknown"
    )
    return f"server: {ident} · {speaks} · {_plural(report.summary.total, 'check')}"


class HumanRenderer:
    """Render the human-readable table (stdout)."""

    def __init__(self, console: Console, *, verbose: int = 0) -> None:
        self.console = console
        self.verbose = verbose
        self.glyphs = glyphs(console)

    def _line(self, *parts: tuple[str, str | None]) -> None:
        text = Text()
        for content, style in parts:
            text.append(content, style=style)
        self.console.print(text)

    def render(self, report: Report) -> None:
        """Print header lines, one row per check (with detail lines) and the final line."""
        sep = " | " if self.console.options.ascii_only else " · "
        self._line(
            (
                f"mcpcheck {__version__}{sep}MCP {PROTOCOL_VERSION}{sep}{sanitize(report.server, limit=300)}",
                None,
            )
        )
        self._line((server_line(report).replace(" · ", sep), None))
        self._line(("", None))
        for check in report.checks:
            self._render_check(check)
        self._line(("", None))
        self._line((final_line(report), None))

    def _render_check(self, check: Any) -> None:
        status: Status = check.status
        glyph = self.glyphs[status]
        style = STYLES[status]
        self._line(
            (f"{glyph} ", style),
            (f"{check.name.value:<{NAME_WIDTH}}", style),
            (sanitize(check.summary), None),
        )
        if status is Status.ERROR and check.error is not None:
            self._line(
                (
                    f"    error: {sanitize(check.error.type)}: {sanitize(check.error.message)}",
                    "bold red",
                )
            )
        items = [*check.failures, *check.warnings]
        if self.verbose:
            items.extend(check.info)
        shown = items if self.verbose else items[:MAX_ITEMS_SHOWN]
        for finding in shown:
            is_failure = finding.level is Level.MUST and finding in check.failures
            is_info = finding in check.info
            if is_info:
                mark, mark_style, label = "i", "dim", "info"
            elif is_failure:
                mark, mark_style, label = (
                    self.glyphs[Status.FAIL],
                    STYLES[Status.FAIL],
                    finding.level.value,
                )
            else:
                mark, mark_style, label = (
                    self.glyphs[Status.WARN],
                    STYLES[Status.WARN],
                    finding.level.value,
                )
            self._line(
                (f"    {mark} ", mark_style),
                (f"{sanitize(finding.subject, limit=120)}: {sanitize(finding.message)}", None),
                (f"  [{label}]", "dim"),
            )
        hidden = len(items) - len(shown)
        if hidden > 0:
            self._line((f"    … and {hidden} more (use --verbose or --json)", "dim"))
        if status in (Status.FAIL, Status.WARN):
            self._line((f"    spec: {check.spec_url}", "dim"))


class TranscriptRenderer:
    """Render the ``--verbose`` transcript (stderr)."""

    def __init__(self, console: Console, *, verbose: int = 1) -> None:
        self.console = console
        self.verbose = verbose

    def render(self, exchanges: Iterable[Exchange]) -> None:
        """Print every exchange, with a divider whenever the check or connection changes."""
        last: tuple[str, str] | None = None
        for ex in exchanges:
            key = (ex.check, ex.connection)
            if key != last:
                self.console.print(
                    Text(f"# check: {ex.check or '-'}  conn={ex.connection}", style="dim")
                )
                last = key
            self.render_exchange(ex)

    def render_exchange(self, ex: Exchange) -> None:
        """One ``>`` request line and one ``<`` response line (+ headers/bodies at ``-vv``)."""
        tags = f" {' '.join(ex.tags)}" if ex.tags else ""
        rpc = f"  {ex.rpc_method}" if ex.rpc_method else ""
        self.console.print(Text(f"> {ex.seq} {ex.verb} {sanitize(ex.url, limit=300)}{rpc}{tags}"))
        if self.verbose >= 2:
            for name, value in ex.request_headers:
                self.console.print(Text(f">   {name}: {sanitize(value)}"))
            if ex.request_body:
                self.console.print(Text(f">   {_preview(ex.request_body)}"))
        if ex.error and ex.status == 0:
            self.console.print(Text(f"< {ex.seq} ERROR {sanitize(ex.error)}", style="bold red"))
            return
        media = next((v for k, v in ex.response_headers if k.lower() == "content-type"), "")
        media = media.split(";")[0].strip() or "(no content-type)"
        suffix = f"  ERROR {sanitize(ex.error)}" if ex.error else ""
        self.console.print(
            Text(f"< {ex.seq} {ex.status} {sanitize(media)} {ex.elapsed_ms}ms{suffix}")
        )
        if self.verbose >= 2:
            for name, value in ex.response_headers:
                self.console.print(Text(f"<   {name}: {sanitize(value)}"))
            if ex.response_body:
                self.console.print(Text(f"<   {_preview(ex.response_body)}"))


def _preview(body: str) -> str:
    if len(body) > BODY_PREVIEW:
        return (
            sanitize(body[:BODY_PREVIEW], limit=BODY_PREVIEW + 1)
            + f" …[+{len(body) - BODY_PREVIEW} bytes]"
        )
    return sanitize(body, limit=BODY_PREVIEW + 1)


def report_to_json(report: Report, *, ensure_ascii: bool = False) -> str:
    """Serialize a report (key order = model field order; ``$schema`` first).

    ``ensure_ascii=True`` escapes non-ASCII characters so the text survives a
    console whose encoding is not UTF-8 (the JSON stays byte-for-byte equivalent).
    """
    data: dict[str, Any] = {"$schema": SCHEMA_ID}
    data.update(report.model_dump(mode="json"))
    return json.dumps(data, indent=2, ensure_ascii=ensure_ascii) + "\n"


def is_utf8_stream(stream: IO[str]) -> bool:
    """Whether ``stream`` encodes UTF-8 (unknown encodings count as UTF-8)."""
    encoding = getattr(stream, "encoding", None) or "utf-8"
    return encoding.lower().replace("-", "").replace("_", "") in ("utf8", "utf8sig")


def write_json(report: Report, *, path: str | None = None, stream: IO[str] | None = None) -> None:
    """Write the JSON report to ``stream`` and/or atomically to ``path`` (temp file + ``os.replace``).

    The file is always UTF-8; the stream gets ASCII escapes instead when its
    encoding is not UTF-8, so a legacy locale cannot make the write fail halfway.
    """
    if stream is not None:
        stream.write(report_to_json(report, ensure_ascii=not is_utf8_stream(stream)))
        stream.flush()
    if path is not None:
        text = report_to_json(report)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".mcpcheck-", suffix=".json", dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            Path(tmp).replace(target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def print_schema(schema_text: str, stream: IO[str] | None = None) -> None:
    """Print the shipped JSON Schema verbatim."""
    (stream or sys.stdout).write(schema_text if schema_text.endswith("\n") else schema_text + "\n")


__all__ = [
    "GLYPHS_ASCII",
    "GLYPHS_UNICODE",
    "REPLACEMENT",
    "HumanRenderer",
    "TranscriptRenderer",
    "final_line",
    "glyphs",
    "is_utf8_stream",
    "make_console",
    "print_schema",
    "report_to_json",
    "sanitize",
    "server_line",
    "write_json",
]
