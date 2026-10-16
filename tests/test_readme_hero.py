"""The README's hero block is a real run, and this is the run that produces it.

The block at the top of ``README.md`` is the project's main credibility artefact, so it
must not be allowed to drift: here the exact misbehaving-server configuration behind it is
rebuilt, all eight checks are run against it and the rendered report is compared with the
README character for character (only the server URL is rewritten, which the README says it
does). A changed summary string, glyph, elision or ordering fails this test instead of
quietly turning the first screen of the README into fiction.

The configuration is the three toggles ``README.md`` documents under *Testing locally*
(:data:`HERO_TOGGLES`) plus a ``tools`` listing whose third tool has an invalid
``inputSchema`` — the ``--set tools=...`` the hero run used.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from rich.console import Console

from mcpcheck import output
from mcpcheck.checks import select
from mcpcheck.client import ClientConfig
from mcpcheck.runner import run_checks
from tests.conftest import ThreadServer
from tests.fixtures.misbehaving_server import (
    DEFAULT_TOOLS,
    READ_ONLY_ANNOTATIONS,
    Toggles,
    build_app,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

README = Path(__file__).resolve().parent.parent / "README.md"
HERO_URL = "https://api.example.com/mcp"
CONSOLE_WIDTH = 100

INVALID_SCHEMA_TOOL: dict[str, Any] = {
    "name": "search_orders",
    "description": "Search orders by free text (read-only).",
    "inputSchema": {
        "type": "object",
        # `"type": "nonsense"` is not a JSON Schema type: this is the hero's `✗ schemas`.
        "properties": {"query": {"type": "nonsense", "description": "Query text."}},
        "required": ["query"],
    },
    "annotations": dict(READ_ONLY_ANNOTATIONS),
}
"""The third tool of the hero listing: invalid ``inputSchema``, no ``outputSchema``."""

HERO_TOGGLES = Toggles(
    discover_no_server_info=True,
    omit_ttl=True,
    omit_cache_scope=True,
    tools=(*DEFAULT_TOOLS, INVALID_SCHEMA_TOOL),
)
"""Exactly the server the README's hero block was captured against."""


def hero_block() -> str:
    """The README's first ``console`` block, without its ``$ ...`` prompt line."""
    block = README.read_text(encoding="utf-8").split("```console\n", 1)[1].split("```", 1)[0]
    first, _, rest = block.partition("\n")
    assert first.startswith("$ "), f"the hero block does not open with a prompt: {first!r}"
    assert "mcpcheck" in first, first
    return rest


@pytest.fixture
def hero_server() -> Iterator[ThreadServer]:
    """The misbehaving server in :data:`HERO_TOGGLES`'s configuration, on a free port."""
    server = ThreadServer(build_app(HERO_TOGGLES), lifespan="off").start()
    try:
        yield server
    finally:
        server.stop()


@pytest.mark.integration
@pytest.mark.anyio
async def test_hero_block_is_the_report_this_configuration_produces(
    hero_server: ThreadServer,
) -> None:
    """Re-running the hero's own configuration reproduces the README block exactly."""
    report = await run_checks(ClientConfig(url=hero_server.url), select())
    buf = io.StringIO()
    console = Console(
        file=buf,
        color_system=None,
        highlight=False,
        markup=False,
        soft_wrap=True,
        width=CONSOLE_WIDTH,
    )
    output.HumanRenderer(console).render(report)
    rendered = buf.getvalue().replace(hero_server.url, HERO_URL)

    assert rendered == hero_block(), (
        "README.md's hero block is no longer what this run prints. Re-capture it with\n"
        "  NO_COLOR=1 COLUMNS=100 uv run mcpcheck <url>\n"
        "against tests/test_readme_hero.HERO_TOGGLES, and rewrite the URL to " + HERO_URL
    )
