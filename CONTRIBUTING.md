# Contributing to mcpcheck

Thanks for your interest. `mcpcheck` is small and opinionated on purpose: every
check enforces a clause of the MCP 2026-07-28 specification, every finding
carries its normative weight (`must`, `should`, `best-practice`) and the tool
never sends a request that could change state on the server. Contributions
that keep those three properties are very welcome.

## Before you start

- **Bugs and wrong verdicts:** open an issue with the matching form. For a wrong
  verdict (false positive or negative) attach the `--json` report; it contains
  the evidence and never your token.
- **New checks or findings:** open a *New check proposal* issue first. It asks
  for the spec clause, the RFC 2119 level and how a read-only client observes
  the behaviour. Checks that need tool execution with valid input, resource
  reads or prompt rendering will not be accepted.
- **Spec questions** belong to the
  [specification repository](https://github.com/modelcontextprotocol/modelcontextprotocol),
  not here.

## Requirements

- Python 3.10 or newer (the test matrix covers 3.10 to 3.14 and runs 3.15 as
  an experimental cell).
- [uv](https://docs.astral.sh/uv/) 0.12 or newer. Everything below uses it;
  `pip` works too but you are on your own for the lockfile.

## Development setup

```shell
git clone https://github.com/suresh-jbt/mcpcheck.git
cd mcpcheck
uv sync                 # creates .venv from uv.lock with the dev dependency group
uv run mcpcheck --help  # the CLI from your checkout
uvx pre-commit install  # optional: ruff, formatting, lockfile and workflow checks on commit
```

`uv sync` installs the exact versions in `uv.lock`. CI tests both the oldest
supported `mcp` SDK (2.2.0) and the newest 2.x; to reproduce the 2.2.0 cell
locally, re-lock temporarily and restore the committed lock afterwards:

```shell
uv lock --upgrade-package "mcp==2.2.0" && uv sync && uv run pytest -q
git checkout uv.lock && uv sync                       # back to the committed lock
```

## Entry points

`make` (or `make help`) lists every command in this document, and is the
shortest path to the three you need most often:

```shell
make demo       # bash examples/demo.sh: mcpcheck against a conformant server, then a broken one
make check      # lint + typecheck + test, in that order (what CI gates on)
make help       # all targets, with one line each
```

`make` is a thin wrapper: every target is one line of real `uv run ...`
commands, so you can always read the `Makefile` and run them by hand.
`tests/test_examples.py` asserts that every documented target exists and
that the README's `make help` block still matches the `Makefile`.
## Running the tests

```shell
uv run pytest -q                      # everything (about 1700 tests, well under a minute)
uv run pytest -q -n 4                 # in parallel (pytest-xdist)
uv run pytest -q -m "not integration" # unit tests only: no sockets, no uvicorn
uv run pytest --cov                   # with coverage (fails under the threshold in pyproject.toml)
```

Async tests use the `anyio` pytest plugin: mark them `@pytest.mark.anyio`
(`pytest-asyncio` is deliberately not installed). Markers, all declared in
`pyproject.toml` (`--strict-markers`):

| Marker | Meaning |
|---|---|
| `integration` | starts the in-repo fixture servers on a free `127.0.0.1` port |
| `slow` | takes more than a second by design (timeouts, hanging servers) |
| `sdk_matrix` | asserts facts about the installed `mcp` SDK |
| `legacy` | needs an `mcp<2` environment (skipped by default) |
| `network` | hits the public specification site (skipped by default) |

The suite starts two in-repo servers on free ports:

- `tests/fixtures/reference_server.py`: a conformant server on the official
  `mcp` SDK. Every check must PASS against it. Run it yourself with
  `uv run python -m tests.fixtures.reference_server --port 0` (flags:
  `--json-response`, `--stateful`, `--no-origin-check`, `--require-token T`,
  `--app-host H`, `--no-output-schema`, `--empty`, `--bare`).
- `tests/fixtures/misbehaving_server.py`: a hand-rolled Starlette JSON-RPC
  app whose behaviour is chosen by a frozen `Toggles` dataclass
  (`require_initialize`, `mint_session_id`, `omit_ttl`, `origin_mode`, ...).
  Every FAIL and WARN path in `mcpcheck` must be reproduced by one of its
  toggles. Run it with `uv run python -m tests.fixtures.misbehaving_server
  --port 0 --toggle NAME --set FIELD=VALUE`.

A third in-repo server lives outside the test suite and is shipped for
readers rather than for tests: `examples/demo_server.py`, the commented,
fully conformant SDK server the one-command demo points `mcpcheck` at.
`--break FEATURE` introduces one realistic non-conformance at a time, and
the outcome table in `examples/README.md` is asserted row by row by
`tests/test_examples.py` (which check moves, to which status, with which
finding code). CI runs `timeout 120 bash examples/demo.sh` on every push, so
a new check, a renamed finding code or a changed summary string can break
the demo as well as the tests — run `make demo` before you push.
`tests/test_readme_hero.py` rebuilds the exact misbehaving-server
configuration behind the README's hero block and asserts the report still
matches it character for character.

Never bind a fixed port in tests: always bind `("127.0.0.1", 0)` and read the
port back. Golden output files under `tests/golden/` are regenerated with
`MCPCHECK_UPDATE_GOLDENS=1 uv run pytest tests/test_output.py`.

## Linting and typing

```shell
uv run ruff check . && uv run ruff format .   # lint + format (ruff)
uv run mypy                                   # strict, with the pydantic plugin
```

CI runs the same commands; `ruff format --check` and `mypy` must be clean. The
code must stay Python 3.10 compatible (`tests/test_py310.py` guards the syntax).

## Adding a check

A check is one module in `src/mcpcheck/checks/`. To add one:

1. Open the proposal issue (see above) and agree on the name, the clause and
   the level of each finding.
2. Create `src/mcpcheck/checks/<name>.py` with a class deriving from
   `mcpcheck.checks.base.Check`; give it the fixed `name`, `title`, `spec_url`
   and `order`, implement `async def run(self, ctx, f)`, and add the class to
   `REGISTRY` in `src/mcpcheck/checks/__init__.py` so `--check <name>` and
   `--list-checks` see it. Use `ctx.raw` (shared connection), `ctx.fresh(label)`
   (a new connection) and the cached `ctx.*_list_pages()` / `ctx.discover()`
   helpers; never compute a status yourself, the runner derives it from the
   findings.
3. Register every finding code before using it: one
   `_c("<check>.<slug>", severity, "description", spec_url)` line in
   `src/mcpcheck/checks/codes.py` (severity is `must`, `should`,
   `best-practice` or `info`), or `codes.register(...)` at import time of your
   module. `Findings` refuses unregistered codes and codes used at a
   different severity than registered, and `tests/test_codes.py` greps the
   whole tree for unregistered literals. `must` and `should` need a spec URL;
   add new URLs to `src/mcpcheck/spec_urls.py` only after verifying the anchor
   exists on the published page.
4. Make the reference server exercise the PASS path and add a `Toggles` field
   to the misbehaving server for each FAIL/WARN path. Write tests for both:
   scripted (`tests/helpers/scripted.py`), in-process ASGI, and at least one
   TCP test that asserts the fixture's request log contains only allowed
   `(verb, method)` pairs.
5. Judge the raw wire, not the SDK's view: anything the SDK would normalise
   away (missing `ttlMs`/`cacheScope`, `resultType`, `Mcp-Session-Id`
   headers, invalid `x-mcp-header` tools, HTTP status codes) must be read from
   the `RawReply`. The SDK's validators are a second witness, not the judge.
6. Update the README (the *What it checks* table and, if you added a flag,
   run `uv run python scripts/gen_options_table.py README.md`), add a line
   under `[Unreleased]` in `CHANGELOG.md`, and run
   `uv run python scripts/check_readme.py README.md CHANGELOG.md CONTRIBUTING.md`.
7. Keep the demo honest. If the new check can fail against a plausible
   server, give `examples/demo_server.py` a `--break` feature for it, add
   its row to the table in `examples/README.md` and its expectation to
   `BREAK_EXPECTATIONS` in `tests/test_examples.py`; then `make demo` and
   `uv run pytest tests/test_examples.py -q`.

Things a check must **never** do: call a tool with arguments that satisfy its
`inputSchema`, call `resources/read`, `prompts/get` or `subscriptions/listen`,
send `DELETE` or notifications, follow a cross-origin redirect, fetch a network
`$ref` or icon, trust `serverInfo` or tool annotations beyond displaying them,
or fail a server for behaviour the specification permits (label such findings
`best-practice` and keep them warnings). A probe that could have side effects
is a bug of the highest severity; see
[SECURITY.md](https://github.com/suresh-jbt/mcpcheck/blob/main/SECURITY.md).

## Documentation

The README is also the PyPI long description. Keep links absolute
(`https://github.com/suresh-jbt/mcpcheck/blob/main/...`), avoid HTML beyond
what PyPI allows, and keep the real-run blocks real: regenerate them from the
fixture servers (`NO_COLOR=1 COLUMNS=100 uv run mcpcheck <url>`) when output
changes. `tests/test_readme.py` keeps the options table and the checks table in
sync with the code; before a release run:

```shell
uv run python scripts/check_readme.py README.md CHANGELOG.md CONTRIBUTING.md --release
uv build && uvx twine@7.0.0 check --strict dist/*
```

## Releasing (maintainers)

1. On a clean `main`: `uv lock && uv sync && uv run pytest -q && uv run mypy &&
   uv run ruff check .` green; then the 2.2.0 cell (`uv lock --upgrade-package
   "mcp==2.2.0" && uv sync && uv run pytest -q`), then `git checkout uv.lock &&
   uv sync`.
2. Regenerate the README real-run blocks if output changed, run
   `uv run python scripts/check_readme.py README.md CHANGELOG.md CONTRIBUTING.md --release`
   and `uv build && uvx twine@7.0.0 check --strict dist/*`.
3. In `CHANGELOG.md` rename `[Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`, add a
   fresh empty `[Unreleased]` section and update the compare links at the
   bottom.
4. Set the version (`uv version X.Y.Z`; `uv version --short` must print it)
   and commit.
5. `git tag vX.Y.Z && git push --tags`. The release workflow verifies that the
   tag equals the project version, re-runs the tests and the README release
   check, builds, validates the artefacts (`twine check --strict` plus an
   isolated wheel smoke test), waits for approval of the `pypi` environment,
   publishes to PyPI with Trusted Publishing (no API tokens) and creates the
   GitHub release. There is no TestPyPI dry run — the name `mcpcheck` is taken
   on test.pypi.org by an unrelated project — so read the build job's summary
   before approving the `pypi` environment.
6. Smoke-test the published package: `uvx jbt-mcpcheck@X.Y.Z --version` and
   `uvx jbt-mcpcheck@X.Y.Z http://127.0.0.1:<port>/mcp` against the reference
   server must exit `0`. Confirm the badges render, and swap the README's
   static "not yet on PyPI" status badge and Installation note for the real
   PyPI badges (`img.shields.io/pypi/v`, `/pypi/pyversions`, `/pypi/l`).

## Code of conduct

This project follows the [Contributor Covenant 2.1](https://github.com/suresh-jbt/mcpcheck/blob/main/CODE_OF_CONDUCT.md).
By participating you agree to abide by it.

## License

By contributing you agree that your contributions are licensed under the
[MIT License](https://github.com/suresh-jbt/mcpcheck/blob/main/LICENSE).
