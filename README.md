# mcpcheck

[![PyPI](https://img.shields.io/pypi/v/mcpcheck.svg)](https://pypi.org/project/mcpcheck/)
[![Python versions](https://img.shields.io/pypi/pyversions/mcpcheck.svg)](https://pypi.org/project/mcpcheck/)
[![CI](https://github.com/suresh-jbt/mcp-check/actions/workflows/ci.yml/badge.svg)](https://github.com/suresh-jbt/mcp-check/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/pypi/l/mcpcheck.svg)](https://github.com/suresh-jbt/mcp-check/blob/main/LICENSE)
[![MCP spec 2026-07-28](https://img.shields.io/badge/MCP%20spec-2026--07--28-blue)](https://modelcontextprotocol.io/specification/2026-07-28)

Check that an MCP server conforms to the **2026-07-28** revision of the [Model Context Protocol](https://modelcontextprotocol.io/specification/2026-07-28) over Streamable HTTP. Read-only. Zero config. One command.

```console
$ uvx mcpcheck https://api.example.com/mcp
mcpcheck 0.1.0 · MCP 2026-07-28 · https://api.example.com/mcp
server: mcpcheck-misbehaving-server 0.1.0 · speaks 2026-07-28 · 8 checks

✓ handshake     Server does not require `initialize`
✓ session       No `Mcp-Session-Id` issued or expected; lists identical across 3 connections
! discover      `server/discover` responds correctly (1 warning)
    ! result._meta["io.modelcontextprotocol/serverInfo"]: server does not identify itself  [should]
    spec: https://modelcontextprotocol.io/specification/2026-07-28/server/discover
✓ protocol      Header validation and version errors conform
✗ schemas       1 of 3 tools have invalid inputSchema (+1 more issue)
    ✗ tool:search_orders: inputSchema is not a valid JSON Schema under https://json-schema.org/draft/2020-12/schema: 'nonsense' is not valid under any of the given schemas (at $.tools[2].inputSchema.properties.query.type)  [must]
    ! tool:search_orders: no outputSchema (optional per spec); publishing one lets clients validate structuredContent  [best-practice]
    spec: https://modelcontextprotocol.io/specification/2026-07-28/server/tools#tool
✓ errors        Tool errors carry actionable messages
✗ cache         8 cache-hint violations: tools/list ttlMs missing; tools/list cacheScope missing (+6 more)
    ✗ tools/list: result has no ttlMs  [must]
    ✗ tools/list: result has no cacheScope  [must]
    ✗ prompts/list: result has no ttlMs  [must]
    ✗ prompts/list: result has no cacheScope  [must]
    ✗ resources/list: result has no ttlMs  [must]
    … and 3 more (use --verbose or --json)
    spec: https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/caching#cacheable-results
✓ origin        Origin header validated

2 of 8 checks failed, 1 warning. See --json for details.
```

That is a real run against the deliberately misbehaving server that ships in this repository's test suite (only the URL was changed). Every line names the clause it enforces; every finding carries its normative weight.

## Why mcpcheck

The 2026-07-28 revision breaks every Streamable HTTP server that still relies on the old handshake:

- **No handshake, no sessions.** `initialize` and `Mcp-Session-Id` are gone. Every request carries its protocol version and client capabilities in `_meta`, and list results must not vary per connection ([changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog#major-changes), [statelessness](https://modelcontextprotocol.io/specification/2026-07-28/basic#statelessness)).
- **`server/discover` is mandatory** and replaces `initialize` as the place where capabilities and supported versions are published ([discovery](https://modelcontextprotocol.io/specification/2026-07-28/server/discover)).
- **Headers became part of the protocol.** `MCP-Protocol-Version` on every POST, `Mcp-Method` on every request, `Mcp-Name` on `tools/call`; mismatches are rejected with HTTP 400 and dedicated error codes ([standard request headers](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http#standard-request-headers), [server validation](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http#server-validation)).
- **Cache hints are required.** `ttlMs` and `cacheScope` MUST appear on `server/discover` and on every `*/list` result ([cacheable results](https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/caching#cacheable-results)).
- **Origin validation is a MUST**, with a mandated HTTP 403 for forged origins ([Streamable HTTP transport](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)).

A server written for 2025-11-25 still answers `initialize` and looks healthy in old clients while silently failing new ones. `mcpcheck` tells you, in a few seconds and without executing a single tool, which requirements your server already meets and points you at the exact clause for each one it does not.

## Installation

```shell
uvx mcpcheck https://api.example.com/mcp        # run without installing (uv)
pipx run mcpcheck https://api.example.com/mcp   # pipx
uv tool install mcpcheck                         # or install as a tool
pip install mcpcheck
```

Requires Python 3.10 or newer (3.10 support ends at 1.0 or when the `mcp` SDK drops it, whichever comes first) and is built on the official [`mcp`](https://pypi.org/project/mcp/) Python SDK (2.x). Pin a version in CI with `uvx mcpcheck@0.1.0`.

## Quick start

```shell
mcpcheck https://api.example.com/mcp                      # run all eight checks
mcpcheck https://api.example.com/mcp --token "$TOKEN"     # authenticated server (or MCPCHECK_TOKEN)
mcpcheck https://api.example.com/mcp --json               # machine-readable report on stdout
mcpcheck https://api.example.com/mcp --check origin,cache # only some checks
mcpcheck https://api.example.com/mcp --skip errors        # everything except one check
mcpcheck https://api.example.com/mcp --strict             # warnings and inconclusive checks also fail the run
mcpcheck https://api.example.com/mcp -vv                  # every HTTP exchange, secrets redacted
python -m mcpcheck --help                                 # also works as a module
```

Against a conformant server the whole run looks like this (real output, bundled reference server, see [Testing locally](#testing-locally-against-the-reference-server)):

```console
$ mcpcheck http://127.0.0.1:64782/mcp
mcpcheck 0.1.0 · MCP 2026-07-28 · http://127.0.0.1:64782/mcp
server: mcpcheck-reference-server 0.1.0 · speaks 2026-07-28 · 8 checks

✓ handshake     Server does not require `initialize`
✓ session       No `Mcp-Session-Id` issued or expected; lists identical across 3 connections
✓ discover      `server/discover` responds correctly
✓ protocol      Header validation and version errors conform
✓ schemas       All 2 tools publish valid schemas
✓ errors        Tool errors carry actionable messages
✓ cache         Cache hints valid on 4 list methods (4 pages)
✓ origin        Origin header validated

All 8 checks passed in 0.1s.
```

## What it checks

Each check is independent, cites the clause it enforces, and classifies every finding by normative weight: **must** (spec MUST / MUST NOT; fails the run), **should** (spec SHOULD; a warning) or **best-practice** (`mcpcheck`'s opinion where the spec is permissive; a warning). Warnings affect the exit code only with `--strict`. `mcpcheck --list-checks` prints the same eight names.

| Check | What `mcpcheck` verifies | Levels | Spec |
|-------|--------------------------|--------|------|
| `handshake` | A `tools/list` with per-request `_meta` and the required headers succeeds on a fresh connection with no `initialize` first, and the result is 2026-shaped (`resultType`, `_meta` serverInfo). Servers that *also* answer `initialize` (dual-era) pass; servers that *require* it, or reject 2026-07-28 with `-32022`, fail. This check gates the others: when it fails or cannot be evaluated the rest are reported as not run. | must / should | [Statelessness](https://modelcontextprotocol.io/specification/2026-07-28/basic#statelessness), [Backward compatibility](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning#backward-compatibility-with-initialization-based-versions) |
| `session` | No `Mcp-Session-Id` is minted, echoed or required; a bogus one is ignored; `tools/list`, `prompts/list` and `resources/list` return the same set on 3 fresh connections and after other requests on the same connection (MUST NOT vary); tool definitions are stable and tools come back in a deterministic order (SHOULD). | must / should / best-practice | [Earlier Streamable HTTP revisions](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http#earlier-streamable-http-revisions), [Tools capabilities](https://modelcontextprotocol.io/specification/2026-07-28/server/tools#capabilities) |
| `discover` | `server/discover` is implemented (MUST); the result has `resultType`, `supportedVersions`, a `capabilities` object and valid cache hints; `_meta` carries `io.modelcontextprotocol/serverInfo` (SHOULD); every capability implied by a non-empty list result is declared and every declared capability answers its list method (MUST). | must / should / best-practice | [Discovery](https://modelcontextprotocol.io/specification/2026-07-28/server/discover), [DiscoverResult](https://modelcontextprotocol.io/specification/2026-07-28/server/discover#discoverresult), [`_meta`](https://modelcontextprotocol.io/specification/2026-07-28/basic#_meta) |
| `protocol` | A header/`_meta` version mismatch yields HTTP 400 + `-32020`; an unsupported version yields 400 + `-32022` with `data.supported`; a missing or miscased `Mcp-Method` or a missing `_meta` field is rejected; a `tools/call` naming a tool that does not exist is rejected when `Mcp-Name` is missing or differs from `params.name`, and a Base64-encoded `Mcp-Name` is decoded before comparing; an omitted `Mcp-Param-*` header is rejected (when an annotated tool exposes an `x-mcp-header` parameter); an unknown method yields 404 + `-32601`. A header-less request that is *accepted* with a 2026-shaped result is a warning (serving it legacy-shaped, or rejecting it for lacking a session, is the dual-era MAY). | must / should | [Protocol version header](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http#protocol-version-header), [Server validation](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http#server-validation) |
| `schemas` | Every tool's `inputSchema` is a JSON Schema object with root `type: "object"`, valid under its declared or default (2020-12) dialect, without fetching any `$ref` (MUST); `x-mcp-header` annotations obey their constraints (MUST); tool names follow the naming rules (SHOULD); every tool declares an `outputSchema` (best practice: the spec says *optional*). | must / should / best-practice | [Tool](https://modelcontextprotocol.io/specification/2026-07-28/server/tools#tool), [Schema validation](https://modelcontextprotocol.io/specification/2026-07-28/basic#schema-validation), [`x-mcp-header`](https://modelcontextprotocol.io/specification/2026-07-28/server/tools#x-mcp-header) |
| `errors` | Calling an annotated read-only tool with arguments that violate its own `inputSchema` is rejected, either as a JSON-RPC error or as a result with `isError: true`; both are conformant. A success on arguments that miss a required property or fall outside an `enum` fails (servers MUST validate all tool inputs); a success on wrong-typed arguments is a warning (lenient coercion). The error message is a concise, specific sentence rather than a stack trace, and a call to a tool that does not exist is rejected (best practice). | must / should / best-practice | [Error handling](https://modelcontextprotocol.io/specification/2026-07-28/server/tools#error-handling) |
| `cache` | `tools/list`, `prompts/list`, `resources/list` and `resources/templates/list` results carry a numeric `ttlMs >= 0` and a `cacheScope` of exactly `public` or `private` on every page, with the same `cacheScope` across pages (MUST; a fractional `ttlMs` is a warning, the spec's prose says integer while its schema says number); every prompt has a `name` and every resource a `uri` (schema-typed fields, MUST); a `public` list whose item names look user-specific is flagged (best practice). `server/discover` hints are checked by `discover`. | must / should / best-practice | [Cacheable results](https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/caching#cacheable-results), [Security considerations](https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/caching#security-considerations) |
| `origin` | A request with `Origin: https://evil.example.com` is answered with HTTP 403 (MUST); any other rejection status fails too, because the spec mandates 403. Rejecting same-origin or absent `Origin`, or accepting `Origin: null`, is a best-practice warning; the spec is silent about those. | must / should / best-practice | [Streamable HTTP transport](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http), [Security best practices](https://modelcontextprotocol.io/docs/2026-07-28/tutorials/security/security_best_practices) |

`mcpcheck` is deliberately opinionated in a few labelled places (`best-practice` in every report): it recommends an `outputSchema` on every tool, its `errors` heuristics encode what makes an error message useful to a language model, and it warns about `public` cache scopes and `Origin` handling the spec does not regulate. Nothing else in `mcpcheck` fails a server for behaviour the specification permits.

## Output

The human report prints one line per check with a glyph (`✓` pass, `✗` fail, `!` warning, `?` inconclusive, `E` error), the first findings under each failing or warning check, and the spec URL. `-v` adds the informational findings, `-vv` prints every HTTP exchange on stderr. ASCII glyphs are used automatically when stdout cannot render UTF-8.

With `--json`, or `--output FILE` (which keeps the human report on stdout), you get one JSON document. This is a real report trimmed to one check with `jq '.checks |= map(select(.name == "schemas"))'`:

```json
{
  "$schema": "https://raw.githubusercontent.com/suresh-jbt/mcp-check/main/schema/mcpcheck-report.v1.schema.json",
  "schema_version": 1,
  "mcpcheck_version": "0.1.0",
  "sdk_version": "2.3.0",
  "python_version": "3.13.5",
  "spec_revision": "2026-07-28",
  "server": "https://api.example.com/mcp",
  "request": {
    "origin": null,
    "auth": "none",
    "extra_headers": [],
    "tls_verify": true
  },
  "options": {
    "strict": false,
    "probe": "safe",
    "max_tools": 20,
    "timeout": 10.0,
    "legacy_probes": false
  },
  "protocol_version": "2026-07-28",
  "server_supported_versions": [
    "2026-07-28"
  ],
  "server_info": {
    "name": "mcpcheck-misbehaving-server",
    "version": "0.1.0"
  },
  "era": "unknown",
  "timestamp": "2026-10-16T11:37:46Z",
  "duration_ms": 58,
  "checks": [
    {
      "name": "schemas",
      "title": "Tool inputSchema/outputSchema are valid JSON Schema",
      "status": "fail",
      "summary": "1 of 3 tools have invalid inputSchema (+1 more issue)",
      "details": "Evaluated 3 tools over 1 tools/list page; schemas validated with jsonschema under their declared or default dialect; no $ref or icon was fetched.",
      "spec_url": "https://modelcontextprotocol.io/specification/2026-07-28/server/tools#tool",
      "failures": [
        {
          "code": "schemas.input_schema_invalid",
          "level": "must",
          "subject": "tool:search_orders",
          "message": "inputSchema is not a valid JSON Schema under https://json-schema.org/draft/2020-12/schema: 'nonsense' is not valid under any of the given schemas (at $.tools[2].inputSchema.properties.query.type)",
          "spec_url": "https://modelcontextprotocol.io/specification/2026-07-28/basic#schema-validation",
          "evidence": {
            "path": "$.tools[2].inputSchema.properties.query.type",
            "page": 0,
            "got": "nonsense",
            "dialect": "https://json-schema.org/draft/2020-12/schema",
            "expected": "valid https://json-schema.org/draft/2020-12/schema schema"
          },
          "tool": "search_orders",
          "issue": "inputSchema is not a valid JSON Schema under https://json-schema.org/draft/2020-12/schema: 'nonsense' is not valid under any of the given schemas (at $.tools[2].inputSchema.properties.query.type)"
        }
      ],
      "warnings": [
        {
          "code": "schemas.output_schema_missing",
          "level": "best-practice",
          "subject": "tool:search_orders",
          "message": "no outputSchema (optional per spec); publishing one lets clients validate structuredContent",
          "spec_url": "https://modelcontextprotocol.io/specification/2026-07-28/server/tools#output-schema",
          "evidence": {
            "path": "$.tools[2].outputSchema",
            "page": 0,
            "expected": "a JSON Schema describing structuredContent"
          },
          "tool": "search_orders",
          "issue": "no outputSchema (optional per spec); publishing one lets clients validate structuredContent"
        }
      ],
      "info": [],
      "duration_ms": 2,
      "requests": 0,
      "error": null,
      "severity": "must",
      "passed": false
    }
  ],
  "summary": {
    "total": 8,
    "passed": 5,
    "warned": 1,
    "failed": 2,
    "inconclusive": 0,
    "errored": 0,
    "skipped": [],
    "exit_code": 1
  }
}
```

Finding `code`s (`cache.ttl_missing`, `schemas.input_schema_invalid`, `origin.evil_accepted`, ...) are stable identifiers you can grep or suppress on; adding a code is a non-breaking change, renaming or removing one is not. The report format is versioned by `schema_version`; the JSON Schema ships in the repository as [`schema/mcpcheck-report.v1.schema.json`](https://github.com/suresh-jbt/mcp-check/blob/main/schema/mcpcheck-report.v1.schema.json) and is printed by `mcpcheck --json-schema`. Your token is never written to the report; only `"auth": "bearer"` and the *names* of any `--header` values are. A useful one-liner for CI logs:

```shell
jq -r '.checks[] | select(.status != "pass") | "\(.status)\t\(.name)\t\(.summary)"' mcpcheck.json
```

## Exit codes

| Code | Meaning |
|:----:|---------|
| `0` | Every selected check passed. Warnings and inconclusive checks (nothing could be judged, e.g. HTTP 401 without `--token`) are allowed unless `--strict` |
| `1` | At least one selected check failed (a MUST was violated). With `--strict`, a warning or an inconclusive check also produces `1` |
| `2` | `mcpcheck` could not run: bad arguments, connection refused or timed out, TLS failure, an incompatible `mcp` SDK, or an internal error |
| `130` | Interrupted (SIGINT) |

A JSON-RPC error that *is* a server answer (`-32601`, `-32602`, `-32020`, `-32022`, ...) is check data, never exit `2`. When `--output FILE` is given the JSON report is written even on exit `1` or `2`.

## Use in CI

```yaml
name: MCP conformance
on: [push, pull_request]
jobs:
  mcpcheck:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: astral-sh/setup-uv@v10.3.0
      - name: Start the MCP server under test
        run: |
          python -m my_server --port 8000 &
          # Probe with an empty POST, not GET: a GET to a server that still hosts the legacy SSE
          # stream never returns. Any HTTP answer (e.g. 400) means the endpoint is up.
          timeout 30 bash -c 'until curl -s -o /dev/null -m 5 -X POST -H "Content-Type: application/json" --data "{}" http://127.0.0.1:8000/mcp; do sleep 0.5; done'
      - name: Check conformance with MCP 2026-07-28
        env:
          MCPCHECK_TOKEN: ${{ secrets.MCP_TEST_TOKEN }}   # omit for unauthenticated servers
        run: uvx mcpcheck@0.1.0 http://127.0.0.1:8000/mcp --output mcpcheck.json
      - uses: actions/upload-artifact@v7
        if: always()
        with: {name: mcpcheck-report, path: mcpcheck.json}
```

Gate only on MUST violations (the default) or add `--strict` to fail on warnings too. To make the step advisory while keeping real failures fatal, append `|| [ $? -eq 1 ]` to the `run` line: it lets exit `1` through and keeps exit `2` (could not run) fatal.

## Options

<!-- options:start -->
| Option | Description |
|---|---|
| `--token TEXT` | Bearer token sent as `Authorization: Bearer <TEXT>`. [env var: MCPCHECK_TOKEN] |
| `-H, --header KEY:VALUE` | Extra request header; repeatable. |
| `--origin TEXT` | Origin header to send on every request (default: none). |
| `--timeout SECONDS` | Per-request timeout. [default: 10.0; x>0] |
| `-c, --check NAME` | Run only these checks; repeatable or comma-separated. Names: handshake, session, discover, protocol, schemas, errors, cache, origin. |
| `--skip NAME` | Skip these checks; repeatable or comma-separated. |
| `--probe [safe\|all\|none]` | Which tools the errors check may call with invalid arguments: `safe` = only tools annotated readOnlyHint=true or destructiveHint=false, `all` = every tool, `none` = no calls. [default: safe] |
| `--max-tools N` | Maximum number of tools the errors check probes. [default: 20; x>=1] |
| `--legacy-probes` | Also send a legacy `initialize` request and a headers-only GET to classify dual-era servers. |
| `--strict` | Treat warnings and inconclusive checks as failures (exit code 1). |
| `--json` | Print the JSON report to stdout instead of the human report. |
| `-o, --output FILE` | Write the JSON report to FILE as well (`-` = stdout, implies --json). |
| `--json-schema` | Print the JSON Schema of the report and exit. |
| `--list-checks` | List the available checks and exit. |
| `-v, --verbose` | Show every HTTP exchange on stderr; -vv adds headers and bodies (secrets redacted). |
| `--no-color` | Disable colored output (NO_COLOR=1 does the same). |
| `--insecure` | Do not verify the server's TLS certificate. |
| `-V, --version` | Show the version and exit. |
| `-h, --help` | Show this message and exit. |
<!-- options:end -->

Validation errors (unknown check name, a name in both `--check` and `--skip`, a URL without scheme or host, a non-positive timeout) exit `2` before any network I/O. `--check` and `--skip` accept comma-separated lists and are case-insensitive; checks always run in the canonical order above regardless of how you list them.

## Safety: what mcpcheck sends

`mcpcheck` is a read-only client. By default it sends only:

- `POST` `server/discover`, `tools/list`, `prompts/list`, `resources/list` and `resources/templates/list`, following `nextCursor` across pages.
- `POST` `tools/call` with arguments that are **proven invalid** against the tool's own `inputSchema` (a missing required property or a wrong-typed value), and **only** to tools whose annotations say `readOnlyHint: true` or `destructiveHint: false`. The spec's default for `destructiveHint` is `true`, so unannotated tools are never called; `--probe all` opts every tool in (with a warning), `--probe none` disables tool calls entirely, `--max-tools` caps their number. Plus one `tools/call` to a tool name that does not exist (`mcpcheck__nonexistent_tool_7f3a`).
- `POST` probes with a deliberately wrong or missing protocol version, a missing or miscased `Mcp-Method`, a missing `_meta` field, an omitted `Mcp-Param-*` header or an unknown method (`mcpcheck/nonexistent`), which no server can execute; three `tools/call` requests to the non-existent tool name with `Mcp-Name` missing, mismatching or Base64-encoded (skipped under `--probe none`); and four `server/discover` requests with different `Origin` headers (one of them `https://evil.example.com`, one `null`).
- One `OPTIONS` preflight.

Opt-in only, with `--legacy-probes`: one legacy `initialize` request (which may allocate a session on dual-era servers; it is never followed by `DELETE`) and one headers-only `GET`.

It **never** sends `resources/read`, `prompts/get`, `subscriptions/listen`, `DELETE`, notifications or a tool call with valid arguments, and it never follows a cross-origin redirect or fetches a `$ref`. Your token goes only to `SERVER_URL` (and to same-origin `307`/`308` redirects, at most five) and is replaced by `****` in `--verbose` output, in the human report and in the JSON report: every finding message, evidence value, check summary and the server's self-reported identity is scrubbed of the `--token` and of every `--header` value, including their URL-encoded and Base64 forms, in case a server echoes your request headers back. TLS certificates are verified unless you pass `--insecure`, which prints a warning and is recorded in the report. Response bodies are capped at 16 MiB and each `nextCursor` walk at 100 pages or 64 MiB, every request has a timeout and every check a budget of ten times `--timeout`, so a hostile or hanging server cannot stall a CI job indefinitely or exhaust its memory; a reply that is not JSON-RPC (an HTML page, a REST API, a body nested too deeply to parse) is a verdict, never a crash.

Expect about 40 requests over up to about 25 connections per full run in your server logs; servers built on the official SDK log the forged-`Origin` request as a warning. Only run `mcpcheck` against servers you own or are authorized to test.

## Troubleshooting

- `? handshake  Not evaluated: HTTP 401 (pass --token)`: the server needs authentication. Pass `--token` or set `MCPCHECK_TOKEN`; the other checks report `Not run` until the handshake can be evaluated.
- `Not evaluated: HTTP 403 (try --origin)`: the server rejects requests whose `Origin` it does not know (or your credentials lack access). Pass `--origin https://<an-allowed-origin>`; it is then sent on every request except the `origin` check's deliberate variations.
- `Not evaluated: redirects (302) to ...`: `mcpcheck` follows only same-origin `307`/`308` redirects. Run it against the final URL.
- `Not evaluated: no MCP endpoint at URL (HTTP 404 text/html)` or `✗ handshake  Non-MCP response to \`tools/list\` (HTTP 200 text/html)`: wrong path, or an HTML page in front of the API. Check the URL you give clients; the other checks report `Not run` because nothing downstream can be judged without a JSON-RPC answer.
- `? errors  No probe-safe tools (N listed; annotate readOnlyHint/destructiveHint or use --probe all)`: annotate the tools that are safe to call, or opt in with `--probe all` if you accept that every tool receives one schema-invalid call.
- A WAF, CDN or API gateway in front of the server can turn a well-formed request into `-32020` (`Not evaluated: well-formed request rejected with -32020 (intermediary?)`), strip the `Mcp-*` headers, inject its own `Origin` policy or answer the unknown-method probe with an HTML 404. Run `mcpcheck` against the origin server directly as well; a difference between the two runs is itself a finding worth fixing.
- A load balancer that pins sessions or caches `tools/list` can make the `session` check report lists that vary per connection or `Mcp-Session-Id` headers you never set. Compare with a run against a single instance.
- `Run incomplete`, `ConnectError` or an exit code of `2`: the endpoint is unreachable from this machine, TLS failed, or the request timed out. `-v` shows the exchange; `--timeout` raises the per-request budget.

## Limitations and non-goals

- Streamable HTTP only: no stdio, no legacy HTTP+SSE transport.
- Not the official conformance suite. `mcpcheck` complements [`modelcontextprotocol/conformance`](https://github.com/modelcontextprotocol/conformance) with a read-only, CI-friendly view of the 2026-07-28 MUSTs that a client can observe.
- No authorization flows: a bearer token or extra headers are passed through, nothing is negotiated.
- Never executes a tool with valid input, never reads a resource or renders a prompt; `resources/read` cache hints are therefore not verified (the `cache` check says so in its details).
- Not a security scanner beyond the `Origin` MUST; HTTP cache headers are not evaluated.
- Never resolves a network `$ref` or fetches an icon, so schemas that depend on remote documents are reported as unverifiable.
- No retries or backoff beyond one wait on `429 Retry-After` in `errors` and one retry on a 5xx in `handshake`.
- Targets 2026-07-28 only; 2026-10-13 appears in the spec's version index but had no published pages when this version was released.

## Compatibility

| | Supported |
|---|---|
| MCP specification | **2026-07-28**. `2026-10-13` is listed in the spec's version index but has no published pages as of October 2026 |
| Servers | Any Streamable HTTP server, including dual-era servers that also answer `initialize`; both `application/json` and `text/event-stream` responses |
| Python | 3.10 to 3.14 (3.15 experimental in CI). 3.10 support ends at 1.0 or when `mcp` 2.x drops it, whichever comes first |
| `mcp` SDK | 2.2 to any 2.x; CI tests 2.2.0 and the newest 2.x |
| Platforms | Linux, macOS, Windows |

Where the official SDK hides a non-conformance when used as a client (it fills in missing `ttlMs`/`cacheScope`, echoes `Mcp-Session-Id`, drops tools with invalid `x-mcp-header` values and collapses HTTP errors), `mcpcheck` inspects the raw HTTP exchange instead and uses the SDK's own validators as a second witness.

## Testing locally against the reference server

The repository ships a small, fully conformant reference server built on the official SDK (it is part of the test suite, not of the installed package):

```console
$ git clone https://github.com/suresh-jbt/mcp-check && cd mcp-check && uv sync
$ uv run python -m tests.fixtures.reference_server --port 0
mcpcheck reference server 0.1.0 listening on http://127.0.0.1:64782/mcp
```

Then, in another shell, `uv run mcpcheck http://127.0.0.1:64782/mcp` prints the all-pass report shown under [Quick start](#quick-start) and exits `0`. Flags: `--port N` (`0` = a free port, printed on stderr), `--json-response`, `--stateful` (dual-era: also answers `initialize`), `--no-origin-check`, `--require-token TOKEN`, `--no-output-schema`, `--empty`, `--bare`. The suite also runs every check against a deliberately misbehaving server whose behaviour is chosen by toggles, for example:

```shell
uv run python -m tests.fixtures.misbehaving_server --port 0 --toggle omit_ttl --toggle omit_cache_scope --toggle discover_no_server_info
```

See [CONTRIBUTING.md](https://github.com/suresh-jbt/mcp-check/blob/main/CONTRIBUTING.md) for the full list and for how to add a check.

## Contributing

Bug reports, wrong-verdict reports (attach the `--json` report; it never contains your token) and new-check proposals (they must cite a spec clause) are welcome. Start with [CONTRIBUTING.md](https://github.com/suresh-jbt/mcp-check/blob/main/CONTRIBUTING.md); security issues go through [SECURITY.md](https://github.com/suresh-jbt/mcp-check/blob/main/SECURITY.md). This project follows the [Contributor Covenant](https://github.com/suresh-jbt/mcp-check/blob/main/CODE_OF_CONDUCT.md).

## License

MIT, see [LICENSE](https://github.com/suresh-jbt/mcp-check/blob/main/LICENSE).
