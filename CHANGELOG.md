# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Before 1.0.0, minor versions may change the CLI or the JSON report; the report
carries a `schema_version` so consumers can detect that. Finding codes are
stable identifiers: adding one is a non-breaking change, renaming or removing one
is listed under *Changed* or *Removed*.

## [Unreleased]

### Fixed

- A deeply nested JSON body now gets the same verdict on every Python. The parse
  previously relied on CPython raising `RecursionError`, whose threshold moved
  from 996 (3.10/3.11) to ~10000 (3.12/3.13) to a stack-dependent value in 3.14,
  so the same hostile response was reported as "JSON nests too deeply" on one
  interpreter and "JSON array" on another. mcpcheck now applies its own bound
  (`wire.MAX_JSON_DEPTH`, 512), which no real result approaches.
- The progress spinner is no longer animated into a redirected stderr. It was
  gated on rich's `is_terminal`, which is true whenever `FORCE_COLOR` is set --
  as GitHub Actions and many CI runners do -- so `mcpcheck ... 2> log` wrote
  escape sequences into the log. An animation now requires a real TTY; colour
  still follows `FORCE_COLOR`/`NO_COLOR`.

### Added

- Eight read-only conformance checks against the MCP 2026-07-28 revision over
  Streamable HTTP, run in a fixed order: `handshake`, `session`, `discover`,
  `protocol`, `schemas`, `errors`, `cache`, `origin`. `handshake` gates the
  rest: when a server requires `initialize`, rejects 2026-07-28 or cannot be
  reached, the other checks are reported as not run instead of failing.
- Severity model: every finding carries a stable `code`
  (`<check>.<snake_slug>`), a normative `level` (`must` fails the run,
  `should` and `best-practice` warn) and the specification clause URL it
  enforces; informational findings (`info`) are shown with `-v` and never
  affect the verdict. Registry of all codes in `mcpcheck.checks.codes`.
- Exit codes: `0` all selected checks passed (warnings and inconclusive checks
  allowed), `1` a check failed (or warned/was inconclusive under `--strict`),
  `2` could not run (usage error, unreachable server, TLS failure, incompatible
  `mcp` SDK, crash), `130` interrupted.
- Human report (`✓ ✗ ! ? E` glyphs with an ASCII fallback, spec URL per
  check) and a versioned JSON report (`schema_version` 1) via `--json` or
  `--output FILE` (`-` for stdout); `--json-schema` prints the report's JSON
  Schema, also shipped as `schema/mcpcheck-report.v1.schema.json`. The JSON
  report is written even when the run exits `1` or `2`.
- Command-line options: `SERVER_URL`; `--token TEXT` (env `MCPCHECK_TOKEN`);
  `-H, --header KEY:VALUE`; `--origin TEXT`; `--timeout SECONDS` (default
  10.0); `-c, --check NAME` and `--skip NAME` (repeatable or comma-separated,
  case-insensitive, canonical order preserved); `--probe [safe|all|none]`
  (default `safe`); `--max-tools N` (default 20); `--legacy-probes`;
  `--strict`; `--json`; `-o, --output FILE`; `--json-schema`;
  `--list-checks`; `-v, --verbose` (`-vv` for headers and bodies, secrets
  redacted); `--no-color` (env `NO_COLOR`); `--insecure`; `-V, --version`;
  `-h, --help`. `python -m mcpcheck` is equivalent to `mcpcheck`.
- Read-only request policy, documented in the README and enforced by the test
  suite: list methods and `server/discover` only, `tools/call` solely with
  arguments proven invalid against the tool's own `inputSchema` and solely to
  tools annotated `readOnlyHint: true` or `destructiveHint: false` (plus one
  call to a non-existent tool name), header probes that cannot execute
  anything, one `OPTIONS` request; `initialize` and a headers-only `GET` only
  with `--legacy-probes`. Never `resources/read`, `prompts/get`, `DELETE`, a
  cross-origin redirect or a network `$ref`.
- Raw HTTP client over `httpx2` that sees what the official SDK normalises
  away (missing cache hints, `Mcp-Session-Id`, legacy result shapes, HTTP
  status codes) and uses the SDK's own validators as a second witness.
- Bundled test fixtures: a conformant reference server on the official `mcp`
  SDK (`python -m tests.fixtures.reference_server`) and a toggle-driven
  misbehaving server (`python -m tests.fixtures.misbehaving_server`), both
  binding a free port on `127.0.0.1`.
- Supported environments: Python 3.10 to 3.14, `mcp` SDK 2.2 to any 2.x,
  Linux, macOS and Windows. Python 3.10 support ends at 1.0 or when the `mcp`
  SDK drops it, whichever comes first.
- `protocol` probes 11-13 validate `Mcp-Name` on a `tools/call` that names a
  tool which does not exist (nothing can execute): a missing header
  (`protocol.missing_mcp_name_accepted`), a mismatching header
  (`protocol.name_header_mismatch_accepted`), a rejection with the wrong
  status or code (`protocol.name_header_wrong_rejection`) and a Base64-encoded
  header that the server fails to decode before comparing
  (`protocol.name_header_not_decoded`); all `must`. Skipped under
  `--probe none`.
- `cache.item_malformed` (`must`): a prompt without `name`, a resource or
  template without `uri`, or any other prompt/resource item the SDK's own
  validator rejects, judged per page like the `schemas` check does for tools.
- `handshake.capability_error_malformed`, `discover.capability_error_malformed`
  (`must`, `data.requiredCapabilities` is not a non-empty string array) and
  `handshake.wrong_status_for_32021`, `discover.wrong_status_for_32021`
  (`must`, `-32021` without HTTP 400); the checks stay inconclusive for the
  verdict they cannot reach but report the observable MUSTs.
- `cache.ttl_not_integral` and `discover.ttl_not_integral` (`should`): a
  finite fractional `ttlMs` such as `1.5` (the caching page's prose says
  integer, `schema.ts` says `number`). `cache.ttl_not_integer` and
  `discover.ttl_not_integer` keep `must` for a `ttlMs` that is not a JSON
  number at all (string, boolean, null, NaN, Infinity).
- `session.walk_truncated` (`info`): a list walk stopped by mcpcheck's own
  page or byte budget; `session.cursor_loop` (`should`) is now emitted only
  for a cursor the server really repeated.
- Each `nextCursor` walk stops after 100 pages or 64 MiB of response bodies
  (`WALK_BYTE_BUDGET`) and the listing is reported as incomplete
  (inconclusive), so a server that stays under the 16 MiB per-request cap can
  no longer drive mcpcheck to multi-gigabyte memory use.
- A runnable demo: `examples/demo_server.py` (a commented, fully conformant
  2026-07-28 support-desk server on the official SDK, with `--break FEATURE`
  for one realistic non-conformance at a time), `examples/demo.sh` and
  `make demo` (one command: check the conformant server, then the same server
  with `--break cache,origin,output-schema`), `examples/README.md` with the
  verified table of what each flaw does to which check, a `Makefile` of the
  usual entry points and a CI smoke step that runs the demo on every push.
  The README is organised around it: a "Try it" block, an "Examples" section
  of recipes (tokens, `--origin`, single-check loops, `--probe none`,
  `--strict` gating, report diffing, `jq` one-liners, hooks, version pinning,
  `-vv` debugging) and a "Fixing what it finds" section that pairs the most
  common findings with the demo server's own code.
- `.gitattributes` pins `eol=lf` for shell scripts and the `Makefile`, so a
  Windows clone with Git's default `core.autocrlf=true` still gets a
  `examples/demo.sh` that `bash` and shellcheck accept.
- `examples/` is inside mypy's scope, so the demo server is held to the same
  strict typing bar as the code it teaches.
- `tests/test_readme_hero.py` rebuilds the misbehaving-server configuration
  behind the README's hero block, re-runs all eight checks against it and
  asserts the rendered report still matches the README character for
  character. `tests/test_examples.py` additionally pins the README's
  `make help` and `--list-breaks` blocks, every flag the docs pass to
  `demo_server.py`, the demo's committed `100755` mode and the commands its
  closing "Next" block hands over.
- `examples/demo.sh` now exits `130`/`143` on SIGINT/SIGTERM (it used to clean
  up and carry on, finishing with a misleading exit `0`), and derives its
  per-flaw explanation lines from `BREAKS=` so the two cannot drift apart.

### Changed

- `discover.meta_reserved_key_unknown` is `info` (was `should`): official
  extensions define further `io.modelcontextprotocol/` keys.
  `discover.capability_undeclared_empty` and `discover.next_cursor_present`
  are `best-practice` (were `should`): the specification does not forbid
  either.
- A handshake that fails with `handshake.not_jsonrpc` (a 2xx that is not a
  JSON-RPC response: an HTML page, a REST API) now gates the dependent checks,
  which report `Not run` instead of attributing header-validation or cache
  defects to a web page. The run still exits `1`.
- A header-less request that a dual-era server rejects for lacking a session
  (`-32600 "Missing session ID"`, the `mcp` 1.x gate) is reported as the
  dual-era MAY (`protocol.dual_era_server`, info) instead of
  `protocol.headerless_rejection_malformed`; header-less exchanges are also
  excluded from the `session` check's `Mcp-Session-Id` scan, so the SDK's own
  `--stateful` reference server passes under `--strict`.
- The security best practices citation points at its canonical page under
  `/docs/2026-07-28/tutorials/security/` (the `/specification/` path answers
  a 308 redirect).
- `-o FILE` validates that the target directory exists and is writable before
  any network I/O (usage error, exit `2`); a write failure after the run is
  reported on stderr with exit `2` instead of a traceback. Ctrl-C at any point
  exits `130`. stdout and stderr tolerate non-UTF-8 consoles
  (`PYTHONIOENCODING=ascii`): non-encodable characters are escaped and the
  JSON report is written with ASCII escapes, never a crash.

### Security

- The `--token` and every `-H` value, in literal, URL-encoded and Base64
  form, are scrubbed from the whole report before it is rendered or written:
  finding messages, evidence (values under `authorization`/`x-api-key`/`*token*`
  keys wholesale), check summaries and details, and the server's self-reported
  `serverInfo`. Previously only the `-vv` transcript was redacted, so a server
  that echoed request headers into an error message, a `serverInfo` name or a
  redirect `Location` could print the token to stdout, the JSON report and the
  `-o` file.
- A deeply nested JSON body (JSON or SSE) is a parse error and a conformance
  verdict (`not_jsonrpc`), not a `RecursionError`; a corrupt `Content-Encoding`
  body (`httpx2.DecodingError`) is an inconclusive transport outcome; an
  exception in the internal gate request is reported as `error` for every
  check (exit `2`) instead of escaping as a traceback.
- Human output neutralizes Unicode bidi overrides, zero-width characters,
  line/paragraph separators and the BOM in server-controlled text (Trojan-Source
  style reordering), in addition to C0/C1 controls.

[Unreleased]: https://github.com/suresh-jbt/mcpcheck/commits/main
