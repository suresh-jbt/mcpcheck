# The mcpcheck demo

Two files and one command. [`demo_server.py`](https://github.com/suresh-jbt/mcpcheck/blob/main/examples/demo_server.py)
is a small, heavily commented MCP server built on the official
[`mcp`](https://pypi.org/project/mcp/) Python SDK that is **fully conformant to
[MCP 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28)** — and that can
be told to misbehave on purpose. [`demo.sh`](https://github.com/suresh-jbt/mcpcheck/blob/main/examples/demo.sh)
runs `mcpcheck` against both versions of it.

## Run it

```shell
git clone https://github.com/suresh-jbt/mcpcheck
cd mcpcheck
bash examples/demo.sh      # or: make demo
```

Nothing but [uv](https://docs.astral.sh/uv/) is required; it takes about three seconds.
The script starts the demo server on a free loopback port, runs `mcpcheck` against it
(exit code 0, eight passes), restarts it with `--break cache,origin,output-schema`, runs
`mcpcheck` again (exit code 1), prints the same report as JSON through `jq`, and always
kills the server on the way out — including on Ctrl-C.

The script uses the `mcpcheck` from the working tree (`uv run mcpcheck`). `MCPCHECK`
overrides that with any command you like — an installed `mcpcheck`, or a published
release once 0.1.0 is on PyPI (it is not yet):

```shell
MCPCHECK=mcpcheck bash examples/demo.sh                # an mcpcheck on PATH
MCPCHECK="uvx mcpcheck@0.1.0" bash examples/demo.sh    # once 0.1.0 is published
```

## What the demo server is

A read-only "support desk": `lookup_order` and `search_faq` (both with an `inputSchema`,
an `outputSchema` and `readOnlyHint: true`), one resource and one prompt. It is worth
reading on its own — every block that exists *because the specification requires it*
carries a one-line comment naming the requirement, so the file doubles as a worked
example of a conformant 2026-07-28 Streamable HTTP server: result-level cache hints on
every list method, `Origin` validation, no sessions and no `initialize`, and error
messages a model can act on.

Run it by hand on a free port and point anything you like at it:

```shell
uv run python examples/demo_server.py --port 0
# demo server listening on http://127.0.0.1:53750/mcp (breaks: none)
uv run mcpcheck http://127.0.0.1:53750/mcp
```

## Breaking it on purpose

```shell
uv run python examples/demo_server.py --list-breaks
uv run python examples/demo_server.py --break cache
uv run python examples/demo_server.py --break cache,origin,session
uv run python examples/demo_server.py --break all
```

Each feature was chosen so that it moves exactly one check. This table is what
`mcpcheck` really reports against each one (`tests/test_examples.py` asserts every row):

| `--break` | What the server does wrong | Check | Status | Finding code | Exit |
|-----------|----------------------------|-------|--------|--------------|------|
| `cache` | strips `ttlMs`/`cacheScope` from every `*/list` result | `cache` | fail (must) | `cache.ttl_missing`, `cache.scope_missing` | 1 |
| `origin` | accepts any `Origin` (no DNS-rebinding protection) | `origin` | fail (must) | `origin.evil_accepted` | 1 |
| `initialize` | demands the legacy `initialize` handshake | `handshake` | fail (must) | `handshake.initialize_required` | 1 |
| `output-schema` | publishes no `outputSchema` on its tools | `schemas` | warn (best-practice) | `schemas.output_schema_missing` | 0 |
| `errors` | answers a failing `tools/call` with a stack trace | `errors` | warn (best-practice) | `errors.non_actionable_messages` | 0 |
| `discover` | omits `serverInfo` from the `server/discover` `_meta` | `discover` | warn (should) | `discover.server_info_missing` | 0 |
| `session` | mints an `Mcp-Session-Id` response header | `session` | warn (should) | `session.session_id_minted` | 0 |
| `all` | every break above at once | `handshake` | fail (must) | `handshake.initialize_required` | 1 |

Two things to notice:

- **Warnings do not fail the run.** `--break discover` exits 0 and prints
  `All 8 checks passed, 1 warning`. Add
  [`--strict`](https://github.com/suresh-jbt/mcpcheck#options) to make warnings and
  inconclusive checks fail too.
- **`handshake` is the era gate.** A server that demands `initialize` cannot be judged
  any further, so `--break initialize` (and `--break all`) reports the remaining seven
  checks as `? … Not run`.

## Pointing mcpcheck at your own server

`mcpcheck` needs nothing but the URL of your Streamable HTTP endpoint, and sends only
read-only requests (see
[Safety: what mcpcheck sends](https://github.com/suresh-jbt/mcpcheck#safety-what-mcpcheck-sends)):

```shell
uv run mcpcheck http://127.0.0.1:8000/mcp                     # local dev server
uv run mcpcheck https://api.example.com/mcp --token "$TOKEN"   # authenticated
uv run mcpcheck https://api.example.com/mcp --json > report.json
uv run mcpcheck https://api.example.com/mcp -vv                # every HTTP exchange
```

`uv run` is the in-clone form. Once 0.1.0 is on PyPI the same commands work as
`uvx mcpcheck <url>` with nothing cloned and nothing installed.

A good way to work through the findings is to keep `examples/demo_server.py` open next to
your own: for every check you fail, the demo server shows the few lines that satisfy it.

## Going deeper: the test fixtures

The test suite ships two more servers, both more extreme than this demo:

- [`tests/fixtures/reference_server.py`](https://github.com/suresh-jbt/mcpcheck/blob/main/tests/fixtures/reference_server.py)
  — the conformant SDK server every check must pass against, with switches for
  authentication, JSON-only responses, empty capabilities and the dual-era handshake.
  Serve it with `make serve-reference`.
- [`tests/fixtures/misbehaving_server.py`](https://github.com/suresh-jbt/mcpcheck/blob/main/tests/fixtures/misbehaving_server.py)
  — a hand-rolled server with around a hundred independent toggles, one per finding
  `mcpcheck` can report. It is how the checks themselves are tested.

Neither is shipped in the wheel; clone the repository to use them. See
[CONTRIBUTING.md](https://github.com/suresh-jbt/mcpcheck/blob/main/CONTRIBUTING.md) for
the development workflow.
