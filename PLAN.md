# mcpcheck — Implementation Plan

**Status:** Draft v1 · 2026-10-16 · targets MCP revision **2026-07-28** · `mcp` SDK `>=2.2,<3` (verified on 2.2.0 and 2.3.0) · Python 3.10–3.14

**How to read this document.** Sections 1–4 are for the decision maker: what is being built, what the user must still decide (§2), and where the plan knowingly departs from the handoff (§3). Section 5 is the normative register every verdict is traced to. Sections 6–9 are the engineering contract (architecture, one subsection per check, CLI/JSON contract, safety model); they are written so that a developer can implement a file from its subsection alone. Sections 10–13 are the delivery plan (tests, packaging, docs, milestones). Appendices hold the verified SDK facts, the URL map and the reviewer resolution log. Conventions: `T:` `B:` `V:` `DI:` `TL:` `R:` `P:` `CA:` `PG:` `CL:` `DP:` `SBP:` `TI:` `SCH:` `LG:` are the saved spec pages (`scratchpad/spec/md_basic_transports_streamable-http.md`, `md_basic_index.md`, `md_basic_versioning.md`, `md_server_discover.md`, `md_server_tools.md`, `md_server_resources.md`, `md_server_prompts.md`, `md_server_utilities_caching.md`, `md_server_utilities_pagination.md`, `md_changelog.md`, `md_deprecated.md`, `md_security_best_practices.md`, `md_basic_transports_index.md`, `schema.txt`, and `agents/synthesizer/logging.md` fetched today) with `cat -n` line numbers; every normative quote also carries its public `URL#anchor` (Appendix B). Anything not executed or fetched is marked **UNVERIFIED**. Code blocks are skeletons and signatures, not implementations.

---

## 1. Executive summary

**What.** `mcpcheck` is a Python CLI (PyPI, MIT) that answers one question in a few seconds: *does this Streamable HTTP MCP server implement the 2026-07-28 revision?* It runs seven read-only checks (`handshake`, `session`, `discover`, `schemas`, `errors`, `cache`, `origin`) plus a proposed eighth (`protocol`, §7.8), prints one line per check with a spec URL for every finding, emits a versioned JSON report for CI, and exits 0/1/2.

**Why.** 2026-07-28 removes `initialize` and sessions, makes `server/discover` mandatory, turns headers (`MCP-Protocol-Version`, `Mcp-Method`, `Mcp-Name`) into protocol, requires result-level cache hints, and mandates HTTP 403 for forged `Origin`. A server written for 2025-11-25 looks healthy to old clients and silently fails new ones. The official `mcp` SDK *hides* most of these defects as a client (it defaults missing hints, clamps negative `ttlMs`, echoes `Mcp-Session-Id`, drops tools with bad `x-mcp-header`, collapses HTTP status into `-32603`), so a conformance tool has to look at the raw wire.

**How.** A thin raw JSON-RPC client over `httpx2` (the SDK's own HTTP library) sends modern-shaped requests and records every exchange; checks are pure classifiers over `RawReply` objects; the SDK is used only for constants, pure helpers (`mcp.shared.inbound.*`), the strict `validate_server_result` witness, and the in-repo reference fixture server. Verdicts follow one rule: a spec MUST/MUST NOT violation is a `must`-level finding (check fails, exit 1); SHOULD and mcpcheck opinion are warnings (exit 0 unless `--strict`); things mcpcheck could not observe are `inconclusive`, never failures.

**How long.** Eight milestones (§13). A single engineer familiar with asyncio/pydantic: M0–M2 (scaffold, wire layer, fixtures) ≈ 3 days; M3–M4 (seven checks) ≈ 6 days; M5–M7 (CLI polish, docs, release) ≈ 3 days. Roughly 2.5 weeks to `0.1.0` on PyPI, 4.5k lines of source and 6k of tests.

**Biggest risks.** (1) Public safety promise vs. the `errors` check: probing tools with invalid input is only read-only if the server validates inputs, which is the very property being tested — resolved by never calling unannotated/destructive tools by default (§9). (2) Interface drift across the eleven design sections — resolved here by making §6 and §8 the only owners of models, flags, codes and layout (Appendix C). (3) Spec-site anchors are Mintlify-generated and fragile; all used today were HTTP-200, but a link-check job is needed. (4) Python 3.10 is EOL and the SDK main branch requires ≥3.11; a future `mcp` 2.x may stop installing on 3.10. (5) Revision 2026-10-13 is listed but unpublished; constants are pluggable but check logic is not.

---

## 2. Decisions required from the user

Every question raised by the eleven sections was merged; duplicates collapsed to one row. The plan proceeds on the **recommendation** column unless told otherwise.

| # | Question | Recommendation | Alternatives | Impact if deferred |
|---|---|---|---|---|
| U1 | Adopt the eighth check `protocol` (header/version/method MUSTs, §7.8) for 0.1.0? | **Yes.** It covers five client-observable MUSTs none of the seven handoff checks cover (T:252-275, T:583-629, B:382-384); probes are side-effect free. `CheckName` enum, `--help`, README and schema are written for eight names. | Ship seven and fold `-32022`/`404+-32601` observations into `handshake`/`discover` (already there); or ship `protocol` as opt-in (`--check protocol` only). | Deferral past M0 forces a `schema_version` bump later (the `check_name` enum is closed). Decide before M0. |
| U2 | Default probe policy for the `errors` check (`--probe`). | **`safe`**: call a tool only when `annotations.readOnlyHint is True` **or** `annotations.destructiveHint is False` (explicit; absence = destructive per schema default, SCH ToolAnnotations). Unannotated tools are never called by default; the check is `inconclusive` with a hint. `--probe all` adds every tool with a proven-invalid payload; `--probe none` disables calls. | `all` as default (handoff's implicit behaviour; executes destructive tools on lenient servers); `annotated-AND` (readOnly AND not destructive: stricter, almost always inconclusive). | None technically, but the README/SECURITY promise must match; decide before M4. |
| U3 | Exit code for `inconclusive` checks (401 without `--token`, 403, 5xx, nothing probe-safe). | **0 by default, 1 under `--strict`**; `error` (transport failure/timeout/crash) is always 2; precedence 2 > 1 > 0. The handshake check is the gate: when it is inconclusive, dependents are `inconclusive` with code `runner.not_run` (not `error`). | Always 2 ("could not test"); or always 1. | Decide before M3 (runner); changes CI semantics and README exit-code table. |
| U4 | Legacy diagnostic probes (`initialize`+`DELETE` in handshake; `GET` expecting 405). | **Opt-in via `--legacy-probes`, default off.** They never change a PASS/FAIL, they create server-side session state on dual-era servers, and `DELETE` is a state-changing verb. `DELETE` is never sent even when opted in (sessions time out server-side). Era label reads `unknown (run with --legacy-probes)`. | On by default with cleanup `DELETE` (handshake section's wish); drop entirely. | None; cosmetic. |
| U5 | Ship the conformant reference server inside the wheel? | **No for 0.1.0 (tests-only)**: `uv run python -m tests.fixtures.reference_server --port 0`. Keeps runtime deps to the SDK; README "Testing locally" documents the clone+uv path; the handoff's `--port 8765` is shown as an example only. | Ship as `mcpcheck.reference_server` (+starlette/uvicorn as hard deps, `mcpcheck selftest`). | None; README text differs. Decide before M6. |
| U6 | Python floor: keep 3.10 (handoff) although 3.10 is EOL (2026-10-01) and SDK main requires ≥3.11? | **Keep 3.10 for 0.1.0**, with a CHANGELOG/README note that 3.10 support ends at 1.0 or when `mcp` 2.x drops it; `compat.py` and CI enforce it. | Raise to 3.11 now (no shims, smaller matrix). | Decide before M0 (classifiers, matrix, ruff target). |
| U7 | Rename the GitHub repo `mcp-check` → `mcpcheck` before first release? | **Yes** (GitHub redirects; nothing published yet; one `sed` over drafts). | Keep `mcp-check` and add a "PyPI: mcpcheck · GitHub: mcp-check" line. | URLs in README/pyproject/SECURITY. Decide before M6. |
| U8 | Default `Origin` header on ordinary requests. | **Send none unless `--origin` is given.** The SDK and every non-browser client send none; T:57-61 constrains only *present-and-invalid* Origins; strict allowlists would otherwise turn a whole run inconclusive. The `origin` check derives its own same-origin probe value. No automatic 403→retry fallback. | Handoff's "auto from URL" (with or without 403 fallback). | Decide before M1 (ClientConfig default). |
| U9 | Inputs the plan cannot invent | LICENSE copyright holder line; `authors = [{name, email}]` in pyproject; Code-of-Conduct enforcement contact (CoC line 40). | — | Blocks M0 (LICENSE) and M6 (CoC). |
| U10 | Severity for a non-403 rejection of a forged `Origin` (400/401/404/5xx or 200+JSON-RPC error). | **`must`-level fail** (T:59-61 is a MUST; the severity model admits no override). Message states the security property held. | Warning (origin section's wish; required a one-off exception to the severity rule). | None. |
| U11 | Result shape of `server` in the JSON report. | Keep the handoff's string `server: "<url>"` and add a sibling `request: {origin, auth, extra_headers, tls_verify}` object (additive). | Object-valued `server` (cli-output draft; breaks the handoff sketch). | Decide before M0 (schema). |

**Decided by default (override if you disagree).** `-H/--header KEY:VALUE` and `-o/--output FILE` are included. `--max-tools` defaults to 20. The anonymous (token-less) cache probe is dropped for 0.1.0 (name heuristic kept; listed as future work). Session uses 3 fresh connections (constant), the bogus-`Mcp-Session-Id` probe is always on. Integral-float `ttlMs` (`60000.0`) is a best-practice warning in both `discover` and `cache`. `supportedVersions` omitting `2026-07-28` is a best-practice warning (not a fail). Missing `outputSchema` is a best-practice warning. Entry point is `anyio.run(..., backend="asyncio")`. Human table is printed after all checks finish (progress spinner on stderr when a TTY); the `--verbose` transcript goes to stderr only (no embedded transcript in JSON; `--transcript FILE` is future work). Glyphs `✓ ✗ ! ? E` with ASCII fallback `+ x ! ? E`, chosen by stdout encoding (no `--ascii` flag). Golden files (not a snapshot library) for output tests; thread-based uvicorn fixtures; three issue forms with blank issues disabled; five badges; Contributor Covenant 2.1 verbatim; `click>=8.2` floor; `httpx2>=2.10`; `pytest>=9`; `pytest-asyncio` not used.

---

## 3. Ground truth vs. handoff — deviations

| # | Handoff said | Reality (GROUND_TRUTH / verified) | What the plan does |
|---|---|---|---|
| 1 | SDK `mcp` 2.2.0 | PyPI latest is 2.3.0; both installed and verified; only wire-visible differences: `streamable_http_client(max_sse_event_size=)` exists only in 2.3.0, 2.2.0 sends `params._meta: {}` pre-negotiation, legacy `initialize` reply has `capabilities.experimental: {}` on 2.2.0 only. | Pin `mcp>=2.2,<3`; CI matrix tests 2.2.0 and latest 2.x; never pass version-gated kwargs; `sdk_version` written into the report (§6.11). |
| 2 | `httpx>=0.27` | The SDK depends on **`httpx2`** (`>=2.10.0`); `httpx` is not installed. | Dependency `httpx2>=2.10`; all raw I/O via `httpx2.AsyncClient`; `rich>=13.9.4` to match the SDK extra. |
| 3 | `SPEC_TRANSPORT=…/transports/streamable-http`, `SPEC_ERRORS=…/errors`, `SPEC_CACHE=…/cache`, `SPEC_ORIGIN=…/security#origin-validation` | All four 404. Real pages: `basic/transports/streamable-http`, `basic#error-codes`, `server/utilities/caching`, Origin rule is in the transport page (heading id `security-&-endpoint`). | `spec_urls.py` holds only verified URLs (Appendix B); `tests/test_spec_urls.py` forbids the four dead paths. |
| 4 | `discover`: result has top-level `serverInfo{name,version}`; verify HTTP `Cache-Control`/`ETag`. | `serverInfo` lives in `_meta["io.modelcontextprotocol/serverInfo"]` and is SHOULD (DI:100-101). Cacheability is result-level `ttlMs`/`cacheScope` (MUST, CA:15-18); no 2026-07-28 text mentions HTTP cache headers; the SDK server emits none. | §7.3 validates `resultType`, `supportedVersions`, `capabilities`, hints, `_meta` keys; `serverInfo` absence is a `should` warning; HTTP cache headers are info only. |
| 5 | "Tools MUST declare `output_schema`" | `outputSchema` is **Optional** (TL:299-301; SCH:4912 `outputSchema?:`); `inputSchema` MUST be a valid JSON Schema object (TL:290-293). | §7.4: `inputSchema` rules are `must`; missing `outputSchema` is a labelled `best-practice` warning; `--strict` promotes warnings. |
| 6 | `cache`: per-item `ttlMs`/`cacheScope`, enum `public/private/user`, ≤20 % missing tolerated, "public but user-specific → FAIL" | Hints are **result-level** (CA:15-23, SCH:4827-4828); enum is exactly `"public"|"private"` (CA:46); applies to `tools/list`, `prompts/list`, `resources/list`, `resources/templates/list`, `server/discover`, `resources/read`. | §7.6 checks every page of four list methods (discover hints via the shared validator); any omission is `must`; `"user"` is invalid; user-specific heuristic is a `best-practice` warning; `resources/read` not probed (read-only). |
| 7 | `errors`: "MUST have `is_error` true"; "SHOULD have `structured_content`" | Both JSON-RPC `-32602` and `isError:true` are conformant (TL:734-776); no SHOULD exists for `structuredContent` on errors; `destructiveHint` defaults to **true** (SCH ToolAnnotations); `InputRequiredResult` exists. | §7.5 accepts both shapes; actionability heuristics are `best-practice`; probe policy per U2; `input_required` handled. |
| 8 | `session`: 3 sequential calls | Spec MUST NOT: list results vary per connection (TL:62-69, R:76-83, P:59-66). | §7.2 uses 3 fresh TCP connections, compares tool/prompt/resource sets, bogus-id probe. |
| 9 | `origin`: "SHOULD reject evil (403 or JSON-RPC error)" | MUST 403 when Origin present and invalid (T:59-61); spec silent on absent Origin. | §7.7: non-403 rejection is `must` fail (U10); absent/same-origin/`null` outcomes are warnings/info. |
| 10 | Seven checks | New client-observable MUSTs exist (header mismatch -32020, unsupported version -32022, `Mcp-Method` required, unknown method 404). | Proposed `protocol` check §7.8 (U1). |
| 11 | Test server `verify_server.py` on port 8765 | Does not exist on this machine; port 8765 is held by an unrelated process. | In-repo fixtures (`tests/fixtures/reference_server.py`, `misbehaving_server.py`), always `127.0.0.1:0`. |
| 12 | CI matrix 3.10–3.13 | Python 3.14 released; 3.15 exists (experimental). | Matrix 3.10–3.14 + 3.15 experimental cell. |
| 13 | Revision listing | 2026-10-13 is listed but has **no published pages** (index/changelog 404, 0 hits in llms.txt). | Target 2026-07-28; `PROTOCOL_VERSION` and `spec_urls.py` are the only version-bearing constants (§15). |
| 14 | `pytest-asyncio` | The SDK and its tests use anyio's bundled pytest plugin; both plugins in auto mode warn, and `filterwarnings=error` fails the suite. | anyio plugin only; `pytest-timeout` added. |
| 15 | "Use SDK `ClientSession` + `StreamableHTTPTransport`" | `ClientSession` loses HTTP status/headers (`MCPError(-32603)`), defaults omitted hints, clamps negative `ttlMs`, drops bad-header tools, cannot omit/tamper headers, and keeping a session open across anyio cancel scopes raises `RuntimeError` (reviewer-reproduced). | Raw `httpx2` client for every verdict; SDK for constants/helpers/witness only (§6.1). |
| 16 | `--origin` default auto from URL | Spec silent on absent Origin; SDK sends none. | U8: none by default. |
| 17 | Human output / JSON sketch | Kept where spec-correct; two header lines, counts in the final line, `warn`/`inconclusive`/`error` statuses, `failures[]`+`warnings[]`, `summary.warned/inconclusive/errored/exit_code` added. | §8; all additive to the handoff's keys (`server`, `protocol_version`, `timestamp`, `checks[].name/passed/details/failures`, `summary.total/passed/failed`). |
| 18 | User-Agent `mcpcheck/<version> (+https://github.com/...)` | Repo URL known. | `mcpcheck/<version> (+https://github.com/suresh-jbt/mcp-check)` (changes with U7). |

---

## 4. Goals, non-goals, scope

**Goals.** (G1) Prove or disprove 2026-07-28 conformance for the client-observable MUSTs over Streamable HTTP, read-only. (G2) Every finding cites the clause (`spec_url`) and its normative weight (`level`). (G3) Deterministic, diffable JSON for CI (`schema_version` 1, stable finding `code`s, exit codes 0/1/2). (G4) Correct against both SDK-based and hand-rolled servers, including dual-era servers (which MAY also answer `initialize`, V:130-132, V:184-185). (G5) Works unchanged on `mcp` 2.2.0 … latest 2.x and Python 3.10–3.14.

**Non-goals (handoff list, kept).** Legacy HTTP+SSE transport; stdio; authorization flows (only `--token`/`-H` passthrough); tool execution with valid input; `resources/read`; `prompts/get`; server-side codegen; GUI/dashboard; daemon/monitoring; proxy/MITM.

**Additions to the non-goals list.** Not the official conformance suite (`modelcontextprotocol/conformance`); not a security scanner beyond the Origin MUST; HTTP cache headers are not evaluated; `resources/read` cache hints are therefore unverified (disclosed in `cache` details); no network `$ref` resolution ever (B:303-304); 2026-10-13 is not targeted until published; no retries/backoff beyond one 429 `Retry-After` wait in `errors` and one 5xx retry in `handshake`.

**Requests mcpcheck sends (complete list; this is the public promise in README/SECURITY/--help).** `POST` `server/discover`, `tools/list`, `prompts/list`, `resources/list`, `resources/templates/list` (with `cursor` continuation); `POST tools/call` with arguments proven invalid against the tool's own `inputSchema`, only for tools eligible under `--probe` (default: annotated read-only/non-destructive) plus one call to a non-existent tool name; `POST` probes with deliberately malformed headers/versions/unknown method (`protocol`, `origin`) that cannot execute anything; one `OPTIONS` preflight (headers only, `origin` check). Opt-in only (`--legacy-probes`): one legacy `initialize` POST and one headers-only `GET`. Never: `resources/read`, `prompts/get`, `subscriptions/listen`, `DELETE`, notifications, valid-argument tool calls, cross-origin redirects.

---

## 5. Protocol baseline — requirements register (condensed by check)

Level: **MUST** → finding `level: must` → check `fail`; **SHOULD** → `level: should` → warning; schema-typed fields (schema.ts is normative, B:238-242) count as MUST; mcpcheck opinion is `best-practice`. Client-side obligations mcpcheck itself honours are listed last.

### 5.1 handshake
| ID | Quote | Level | Source → URL |
|---|---|---|---|
| HS-2 | "Servers **MUST NOT** rely on prior requests over the same connection to establish context (e.g., capabilities, protocol version, client identity). Every request supplies this metadata in its `_meta` field." | MUST NOT | B:193-195 → `basic#statelessness` |
| HS-3 | "Servers that declare the `tools` capability **MUST** respond to `tools/list` requests with the set of tools currently available … This set **MAY** be empty" | MUST/MAY | TL:62-64 → `server/tools#capabilities` |
| HS-4 | "the server **MUST** return either `Content-Type: application/json` (a single JSON object) or `Content-Type: text/event-stream`" | MUST | T:87-90 → `…streamable-http#sending-messages` |
| HS-5 | "Result responses **MUST** include the same ID as the request … **MUST** include a `result` field … The `result` **MUST** include a `resultType` field" | MUST | B:72-75 → `basic#result-responses` |
| HS-6 | "A `resultType` of any value unrecognized by the client **MUST** be considered invalid." | MUST | B:86 |
| HS-8 | "On `400 Bad Request`, the client **SHOULD** inspect the response body … If the body contains a recognized modern JSON-RPC error, the server speaks a modern version … If the body is empty or is not a recognized modern JSON-RPC error, fall back to `initialize`" (era rule) | client SHOULD | T:649-660 → `…#backward-compatibility`; V:145-148 |
| HS-10/11 | dual-era servers **MAY** implement both behaviours and serve both eras on one endpoint | MAY | V:130-132, V:176-185 → `basic/versioning#compatibility-matrix` |
| HS-12 | modern-only server **SHOULD** name supported versions in any error to `initialize` | SHOULD | V:156-159 (only with `--legacy-probes`) |
| HS-13 | "Servers **SHOULD** include … `io.modelcontextprotocol/serverInfo`" in every result `_meta` | SHOULD | B:398-404 → `basic#_meta` |
| HS-15/16/17 | unsupported version → 400 + `-32022`; unknown method → 404 + `-32601`; `server/discover` MUST exist | MUST | T:263-275; DI:9-11 |
| HS-18 | SSE: notifications MAY precede; server **MUST NOT** send JSON-RPC requests on the stream; final response **SHOULD** terminate the stream | MAY/MUST NOT/SHOULD | T:109-122 → `…#receiving-messages` |

### 5.2 session
| ID | Quote | Level | Source → URL |
|---|---|---|---|
| S1 | "An `Mcp-Session-Id` header on a request: ignore it, and do not mint or echo session IDs." (context: server supporting only this revision **SHOULD**) | SHOULD | T:675-682 → `…#earlier-streamable-http-revisions`; removal itself CL:14 |
| S4 | = HS-2 (statelessness) | MUST NOT | B:193-195 |
| S5 | "Servers **SHOULD NOT** require that a client reuse the same connection or process" | SHOULD NOT | B:198-199 |
| S6/S7/S8 | tool/resource/prompt sets "**MUST NOT** vary per-connection or as a side effect of other requests on the connection. The set **MAY** vary by the authorization presented" | MUST NOT/MAY | TL:62-69; R:76-83; P:59-66 → `server/{tools,resources,prompts}#capabilities` (prompts anchor verified today) |
| S9 | "Servers **SHOULD** return tools in a deterministic order" | SHOULD | TL:71-74 |
| S13 | empty string is a valid cursor; missing `nextCursor` = end | client MUST/SHOULD | PG:100-109 → `…pagination#implementation-guidelines` |
| S14 | "Closing the SSE response stream **MUST** be treated by the server as cancellation" | MUST (server) | T:235-239 → justifies early close |

### 5.3 discover
| ID | Quote | Level | Source → URL |
|---|---|---|---|
| N1/N2 | "Servers **MUST** implement it [`server/discover`]" | MUST | DI:9-11; V:75-77; CL:18 |
| N3/N4 | `resultType` required; unrecognized → invalid; unsupported extension → revert to core or reject | MUST | B:75, B:86; V:123-125 |
| N5/N6/N7 | hints MUST on `server/discover`; `ttlMs >= 0`; `cacheScope` `"public"|"private"` | MUST | CA:15-18, CA:62, CA:46 → `…caching#cacheable-results`, `#time-to-live-ttl-field`, `#cache-scope-field` |
| N8 | `interface DiscoverResult { _meta?; resultType; supportedVersions: string[]; capabilities: ServerCapabilities; instructions?; ttlMs: number; cacheScope; [key]: unknown }` | schema | SCH:4023-4031 → `schema#discoverresult` |
| N9/N10 | `serverInfo` SHOULD; `Implementation` requires `name`+`version` | SHOULD/schema | DI:100-101; SCH:4187-4194 |
| N12/N13 | capability MUST be declared when primitive supported; declared capability MUST answer its list | MUST | TL:47, R:43, P:43-44; TL:62, R:76, P:59 |
| N14-N17 | capabilities open set; extension ids MUST follow `_meta` key rules with prefix; `_meta` key format; reserved keys not to be assumed | info/MUST | SCH:4243; V:87-89; B:332-346; B:329-330 → `basic#_meta` |
| N18 | `logging` capability deprecated; new implementations SHOULD NOT adopt | SHOULD NOT | SCH:4251-4252; DP:14-16 → `deprecated#deprecated` |
| N23 | SSE: no server requests; notifications MUST relate; SHOULD terminate; SHOULD `X-Accel-Buffering: no` | MUST/SHOULD | T:112-134 |
| N24 | "The server **MUST NOT** emit `notifications/message` for a request that does not include this field [`io.modelcontextprotocol/logLevel`]" | MUST NOT | LG:65-66 (fetched today; was CL:22 only) → `server/utilities/logging` |
| N25 | "JSON-RPC messages **MUST** be UTF-8 encoded." | MUST | TI:31 → `basic/transports` |
| N26 | reserved sub-range `-32020..-32099` MUST NOT emit undefined codes; `-32002`/`-32042` MUST NOT; `-32000..-32019` SHOULD NOT | MUST NOT/SHOULD NOT | B:119-146 → `basic#error-codes` |

### 5.4 schemas
| ID | Quote | Level | Source → URL |
|---|---|---|---|
| S-1 | "`inputSchema` … **MUST** be a valid JSON Schema object (not `null`)" | MUST | TL:290-293 → `server/tools#tool` |
| S-2 | `inputSchema: { $schema?; type: "object"; [key]: unknown }` | schema | SCH:4911 |
| S-3/S-4 | "Schemas **MUST** be valid according to their declared or default dialect"; default 2020-12; RECOMMENDED 2020-12 | MUST/RECOMMENDED | B:297-299, B:253-260 → `basic#schema-validation`, `#schema-dialect` |
| S-5 | unsupported dialect → **MUST** handle gracefully (client) | MUST (us) | B:291-294 |
| S-6/S-7 | `outputSchema` Optional; may be any JSON Schema; `structuredContent` any JSON value | optional | TL:299-301; CL:51-54 |
| S-8/S-9 | `x-mcp-header` six MUSTs; clients MUST exclude violating tools | MUST | TL:344-366; T:377-410 → `server/tools#x-mcp-header` |
| S-10 | SHOULD NOT mark sensitive parameters with `x-mcp-header` | SHOULD NOT | TL:395-398 |
| S-11 | tool-name SHOULDs (1–128 chars, charset, unique) | SHOULD | TL:311-316 → `#tool-names` |
| S-12/S-13 | MUST NOT dereference network `$ref`; SHOULD bound schema depth/size | MUST NOT/SHOULD (us) | B:301-320 → `basic#$ref-resolution`, `#composition-keyword-resource-use` |
| S-17 | icon URIs HTTPS/`data:`; clients MUST reject unsafe schemes | MUST (client) | B:476-481 → `basic#icons` |
| S-18 | `Tool`/`ListToolsResult` interfaces | schema | SCH:4906-4915, 4823-4831 |

### 5.5 errors
| ID | Quote | Level | Source → URL |
|---|---|---|---|
| E1 | "Servers **MUST**: Validate all tool inputs" | MUST | TL:783-784 → `server/tools#security-considerations` |
| E2/E3 | protocol errors as JSON-RPC errors (example `-32602`); execution errors as `isError: true` with actionable feedback | descriptive | TL:734-776 → `#error-handling` |
| E5 | error responses MUST carry same id, `error.code` integer + `message` | MUST | B:105-107 → `basic#error-responses` |
| E6 | `Error.message` "SHOULD be limited to a concise single sentence" | SHOULD | SCH:431-440 (`schema#error` anchor UNVERIFIED) |
| E7-E9 | error-code ranges (= N26) | MUST NOT/SHOULD NOT | B:119-146 |
| E10 | `resultType` required / unrecognized invalid | MUST | B:75, B:86 |
| E11-E13 | content type MUST; no server requests on stream; no `notifications/message` without `logLevel` | MUST | T:87-90, T:109-116, LG:65-66 |
| E14 | "Servers **MUST** include at least one of `inputRequests` or `requestState` in every `InputRequiredResult`" / "**MUST NOT** send an `inputRequests` that the client has not declared support for" | MUST | MRTR:250,252 → `basic/patterns/mrtr#server-requirements-basic-workflow` |
| E15/E16 | `Mcp-Name` REQUIRED; non-ASCII Base64 sentinel; `Mcp-Param-*` mirroring MUST; omitted header with body value → server MUST reject | MUST (client/server) | T:289-298, T:491-509, T:365-368, T:547-567 |
| E17/E18 | annotations untrusted; `destructiveHint` default true; human in the loop SHOULD | MUST (client)/SHOULD | TL:304-307; SCH ToolAnnotations; TL:33-35 |

### 5.6 cache
| ID | Quote | Level | Source → URL |
|---|---|---|---|
| CA-1 | "Servers MUST include caching hints on results with `resultType: "complete"` returned by … `server/discover` `tools/list` `prompts/list` `resources/list` `resources/templates/list` `resources/read`" | MUST | CA:15-23 → `…caching#cacheable-results` |
| CA-2 | `input_required` interim results carry no hints | info | CA:25-27 |
| CA-3/4/5 | `ttlMs` "is an integer value in milliseconds"; "**MUST** provide a `ttlMs` value that is `>= 0`"; `0` = immediately stale (legal) | def/MUST | CA:45-46, CA:62, CA:54-55 |
| CA-7/8/11 | `public` shareable by anyone; `private` MUST NOT be shared across auth contexts; guidance on choosing; servers MUST be aware public may be shared from authenticated endpoints | MUST (caches)/guidance | CA:100-110, CA:175-181 → `#cache-scope-field`, `#security-considerations` |
| CA-9/10 | `ttlMs` MAY differ per page; "**MUST** apply the same `cacheScope` to all response pages" | MAY/MUST | CA:157-171 → `#interaction-with-pagination` |
| SCH-1 | `ttlMs: number; cacheScope: "public"|"private"; nextCursor?: string` on all four list results | schema | SCH:4827-4828, 2541-2542, 2777-2778, 3174-3175 |
| B-1 | "clients **MUST** treat an absent `resultType` as `"complete"`" | client MUST | B:87 |

### 5.7 origin
| ID | Quote | Level | Source → URL |
|---|---|---|---|
| N1 | "Servers **MUST** validate the `Origin` header on all incoming connections to prevent DNS rebinding attacks." | MUST | T:57-58 → `…streamable-http` (heading id `security-&-endpoint`) |
| N2 | "If the `Origin` header is present and invalid, servers **MUST** respond with HTTP 403 Forbidden. The HTTP response body **MAY** comprise a JSON-RPC *error response* that has no `id`." | MUST/MAY | T:59-61 |
| N4/N5 | SHOULD bind localhost; SHOULD authenticate (not client-testable) | SHOULD | T:62-64 |
| N8 | 401 for invalid/expired tokens; 403 for insufficient scope | MUST/SHOULD | AUTH:291-343 → `basic/authorization#error-handling` |
| N9 | DNS-rebinding threat model | info | SBP:580-654 → `…security_best_practices#local-mcp-server-compromise` |

### 5.8 protocol (proposed)
| ID | Quote | Level | Source → URL |
|---|---|---|---|
| P1 | "Every POST request to the MCP endpoint **MUST** include an `MCP-Protocol-Version` header." | MUST | T:252-253 → `…#protocol-version-header` |
| P2 | "The header value **MUST** match the `io.modelcontextprotocol/protocolVersion` field … If the values do not match, the server **MUST** reject the request with `400 Bad Request` and a `HeaderMismatch` JSON-RPC error" | MUST | T:257-261 |
| P3 | unsupported version → **MUST** 400 + `UnsupportedProtocolVersionError` (`-32022`, `data.supported[]`, `data.requested`) | MUST | T:263-269; SCH:706-716 → `schema#unsupportedprotocolversionerror` |
| P4 | unknown method → **MUST** 404 + `-32601` | MUST | T:271-275 |
| P5 | header-less request: server supporting pre-2025-06-18 clients **MAY** treat as 2025-03-26; otherwise **MUST** reject | MAY/MUST | T:277-281 |
| P6 | `Mcp-Method` REQUIRED on all requests; `Mcp-Name` on `tools/call`/`resources/read`/`prompts/get` | REQUIRED | T:287-294 → `#standard-request-headers` |
| P7 | header names case-insensitive, values case-sensitive; servers **MUST** reject header/body mismatch with 400 + `-32020`; missing required standard header is a failure condition | MUST | T:573-629 → `#server-validation` |
| P8 | request missing a required `_meta` field → **MUST** `-32602`, HTTP **MUST** 400 | MUST | B:382-384 → `basic#_meta` |
| P9 | `-32021` MUST carry `data.requiredCapabilities`, HTTP 400 | MUST | B:389-394 |
| P10 | invalid cursor **SHOULD** → `-32602` | SHOULD | PG:113 → `…pagination#error-handling` |

### 5.9 Client obligations mcpcheck honours (not verdict inputs)
`Accept: application/json, text/event-stream` (T:75-76); `MCP-Protocol-Version` equal to `_meta` (T:252-257); `Mcp-Method` on every POST (T:287-294); `Mcp-Name` Base64 sentinel for non-ASCII or sentinel-looking names (T:491-509); `_meta` carries `protocolVersion` + `clientCapabilities` (`{}` is valid) and SHOULD `clientInfo` (B:375-387); ids non-null (B:48-49); cursors opaque, `""` continues (PG:104-109); never dereference network `$ref` (B:303-304); treat annotations as untrusted (TL:304-307); support both response content types (T:90).

---

## 6. Architecture (reconciled)

### 6.1 Design principles
1. **Raw first, SDK second.** All verdicts come from `RawClient` over `httpx2`. The SDK `ClientSession` is **not used at runtime** (deviation 15, §3): it hides exactly what the checks must see, and keeping one open across per-check cancel scopes raises `RuntimeError: Attempted to exit a cancel scope that isn't the current task's current cancel scope` (reproduced on mcp 2.3.0/anyio 4.15.1 by the reviewer). The SDK supplies: constants (`mcp.client.session.MCP_*_HEADER`, `MODERN_PROTOCOL_VERSIONS`, `mcp_types.*_META_KEY`, error codes), pure helpers (`mcp.shared.inbound.encode_header_value`, `find_invalid_x_mcp_header`, `x_mcp_header_map`, `mcp_param_headers`, `ERROR_CODE_HTTP_STATUS`), the strict witness `mcp_types.methods.validate_server_result(method, "2026-07-28", raw)`, typed models for display, and the reference fixture server. All verified on 2.2.0 and 2.3.0 (Appendix A).
2. **One owner per contract.** §6 owns models, `RawClient`, `CheckContext`, outcome classification, shared constants and exit codes; §8 owns flags, codes grammar, human layout and JSON schema. Check sections (§7) provide only logic, codes and summary text.
3. **Checks are independent but share fetches.** Any check runs alone via `--check`; list results are fetched once per run through `CheckContext` (same token/Origin by construction). Only `session` (fresh connections by design) and `origin`/`protocol` (header variants by design) send duplicate requests.
4. **Sequential, canonical order, deterministic.** No concurrency between checks (session must see untouched connections; WAF-tripping probes run last; transcript numbering is stable).
5. **Spec-grounded severities.** `level` is derived from the quoted RFC keyword; only `--strict` promotes. No per-check overrides.
6. **Read-only** per §4's request list, enforced by a fixture-side request-log test (§10.6).

### 6.2 Package layout
```
mcp-check/                                  (repo; rename → mcpcheck per U7)
├── pyproject.toml  README.md  LICENSE  CHANGELOG.md  CONTRIBUTING.md  SECURITY.md  CODE_OF_CONDUCT.md
├── uv.lock  .python-version  .pre-commit-config.yaml
├── schema/mcpcheck-report.v1.schema.json   (repo copy; identical to the shipped file, test-asserted)
├── scripts/gen_report_schema.py  scripts/check_readme.py
├── .github/{dependabot.yml, CODEOWNERS, PULL_REQUEST_TEMPLATE.md, ISSUE_TEMPLATE/*, workflows/{ci.yml,release.yml,codeql.yml}}
├── src/mcpcheck/
│   ├── __init__.py          __version__ = importlib.metadata.version("mcpcheck") (PackageNotFoundError → "0+unknown")
│   ├── __main__.py          click command `main`; `python -m mcpcheck`
│   ├── py.typed
│   ├── compat.py            3.10 shims: ExceptionGroup import, unwrap_exception_group()
│   ├── sdk_compat.py        require_sdk(): two asserts + actionable ImportError (§6.12)
│   ├── spec_urls.py         verified URLs (Appendix B)
│   ├── wire.py              pure: envelope(), default_headers(), SSE/JSON parsing, classify_reply(), redact(), constants
│   ├── client.py            ClientConfig, RawClient, RawReply, Exchange, Transcript, ServerUnreachable
│   ├── probe_policy.py      is_probe_eligible(), build_invalid_arguments(), build_plans()
│   ├── models.py            Status, Level, CheckName, Finding, CheckError, CheckResult, Summary, Report (pydantic v2)
│   ├── output.py            make_console(), sanitize(), HumanRenderer, TranscriptRenderer, write_json()  (only module allowed to print)
│   ├── runner.py            run_checks(): sequencing, gate, per-check budget, verdict/exit aggregation
│   ├── schema/report.v1.json
│   └── checks/
│       ├── __init__.py      REGISTRY (ordered tuple of classes), NAMES, select()
│       ├── codes.py         FINDING_CODES registry: code → (level, one-line description, spec_url)
│       ├── base.py          Check ABC, Findings builder, CheckContext
│       ├── handshake.py session.py discover.py schemas.py errors.py cache.py origin.py
│       └── protocol.py      (U1)
└── tests/
    ├── __init__.py  conftest.py  helpers/scripted.py  golden/
    ├── fixtures/{__init__.py, reference_server.py, misbehaving_server.py}
    ├── test_wire.py test_client.py test_models.py test_report_schema.py test_spec_urls.py test_sdk_contract.py test_py310.py
    ├── test_probe_policy.py test_output.py test_cli.py test_readme.py
    ├── test_<check>.py ×8   test_integration.py
```

### 6.3 Data flow
```
ClientConfig ─► RawClient.post() ─► RawReply ─► wire.classify_reply() ─► Outcome
                      │                                   │
                      └─► Transcript[Exchange] (stderr -v) └─► Check.run(ctx, f: Findings) ─► CheckResult
Runner: gate(handshake) → sequential checks (anyio.fail_after per check) → Report → output.render_human | write_json → exit code
```
Sequence for a full run: CLI validates args (exit 2 on error, no I/O) → `sdk_compat.require_sdk()` → `anyio.run(run_checks, cfg, selected)` → runner opens `RawClient("main")` → for each selected check in canonical order: `with anyio.fail_after(cfg.check_budget)`: `check.run(ctx, f)` → result list → `Report` → human table (stdout) or JSON (stdout/`--output`) → `sys.exit(report.summary.exit_code)`.

### 6.4 Shared constants (single table; every section references these names)
| Constant (module) | Value | Rationale |
|---|---|---|
| `PROTOCOL_VERSION` (wire) | `"2026-07-28"` | asserted `== mcp_types.LATEST_PROTOCOL_VERSION` in `test_sdk_contract` |
| `MODERN_ERROR_CODES` (wire) | `{-32020, -32021, -32022}` | T:650-657 era rule |
| `REQUEST_TIMEOUT` | `--timeout`, default 10.0 s | httpx2 `Timeout(t)` **and** total per-request budget `anyio.fail_after(t)` around the whole read (SSE keepalives defeat per-chunk read timeouts — verified) |
| `CHECK_BUDGET` | `10 × --timeout` | per-check wall clock; no flag |
| `MAX_BODY_BYTES` | 16 MiB | streamed; exceeding → `inconclusive` for that request |
| `SSE_TERMINATION_GRACE_S` | 2.0 | wait for EOF after the response frame only in checks that report T:122 (handshake, discover); others close immediately (T:235-239) |
| `MAX_PAGES` | 100 | per list method (matches SDK `_relist_tool` cap) |
| `SESSION_CONNECTIONS` | 3 | fresh TCP connections in `session` |
| `MAX_TOOLS` | `--max-tools`, default 20 | `errors` probe cap |
| `TTL_SANITY_MS` | 2 592 000 000 (30 d) | best-practice warning threshold |
| `MAX_SCHEMA_DEPTH` / `MAX_SCHEMA_NODES` | 64 / 5000 | `check_schema` RecursionError at ~128 on CPython 3.13 (verified) |
| `MAX_ITEMS_SHOWN` | 5 | finding lines per check in human output |
| `UNKNOWN_TOOL_NAME` | `mcpcheck__nonexistent_tool_7f3a` | deterministic (golden output, log review) |
| `EVIL_ORIGIN` | `https://evil.example.com` | RFC 2606 name |
| `USER_AGENT` | `mcpcheck/<ver> (+https://github.com/suresh-jbt/mcp-check)` | GROUND_TRUTH 13 |

### 6.5 `client.py`
```python
@dataclass(frozen=True)
class ClientConfig:
    url: str  # http(s) only, validated by CLI
    token: str | None = None  # Authorization: Bearer
    extra_headers: tuple[tuple[str, str], ...] = ()  # -H
    origin: str | None = None  # None = send no Origin (U8)
    timeout: float = 10.0
    verify_tls: bool = True  # --insecure → False
    max_redirects: int = 5
    probe: Literal["safe", "all", "none"] = "safe"
    max_tools: int = 20
    legacy_probes: bool = False
    strict: bool = False
    user_agent: str = USER_AGENT

    @property
    def check_budget(self) -> float:
        return 10 * self.timeout


class ServerUnreachable(
    Exception
):  # connect refused / DNS / TLS / UnsupportedProtocol → run exit 2
    def __init__(self, url: str, cause: BaseException): ...


@dataclass(frozen=True)
class RawReply:
    status: int  # 0 when no HTTP response (error set)
    headers: httpx2.Headers  # case-insensitive; .raw keeps wire casing (session needs both)
    media_type: str  # "application/json" | "text/event-stream" | "" | other (params stripped)
    charset: str | None
    messages: tuple[dict[str, Any], ...]  # every JSON-RPC object, SSE frames in order
    raw: bytes  # capped at MAX_BODY_BYTES
    truncated: bool
    parse_error: str | None  # "invalid JSON", "not utf-8", ...
    elapsed_ms: int
    request_id: int | str | None
    sse_event_ids: int  # legacy resumability noise (info)
    stream_terminated: bool | None  # SSE only; None when not waited for
    redirects_followed: int
    redirect_blocked: str | None  # Location of a 301/302/303 or cross-origin 307/308
    error: str | None  # transport/read problem text

    def response_for(
        self, req_id
    ) -> dict | None: ...  # first message with result/error and matching id (or null id)
    @property
    def response(self) -> dict | None: ...
    @property
    def rpc_error(
        self,
    ) -> tuple[int, str, Any] | None: ...  # (code, message, data); non-int code → None + finding
    @property
    def notifications(self) -> tuple[dict, ...]: ...
    @property
    def server_requests(self) -> tuple[dict, ...]: ...  # method AND id → T:114 MUST NOT


class RawClient:
    """One httpx2.AsyncClient per instance; a new instance == a new TCP connection pool."""

    def __init__(
        self,
        cfg: ClientConfig,
        transcript: Transcript,
        *,
        label: str = "main",
        send_token: bool = True,
    ) -> None: ...
    async def post(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        meta_version: str
        | None = PROTOCOL_VERSION,  # None → omit the _meta envelope (legacy-shaped)
        header_version: str
        | None
        | Unset = UNSET,  # UNSET → same as meta_version; None → omit header
        headers: Mapping[str, str] | None = None,  # add/override, case-insensitive
        omit_headers: Iterable[str] = (),  # e.g. ("mcp-method",)
        omit_meta_keys: Iterable[str] = (),  # protocol check: drop clientCapabilities etc.
        req_id: int | str | None | Unset = UNSET,  # UNSET → counter; None → notification
        client_info: bool = True,
        wait_for_eof: bool = False,
        purpose: str = "",
        check: str = "",
    ) -> RawReply: ...
    async def http(
        self, verb: Literal["GET", "OPTIONS"], *, headers=None, purpose="", check=""
    ) -> RawReply:
        """Headers-only probe: stream, read status+headers (+≤1 SSE event), close. Bounded by anyio.fail_after(min(timeout, 5))."""

    async def aclose(self) -> None: ...
```
`post()` algorithm: (1) `wire.envelope()` + `wire.default_headers()`; apply overrides; the `main` client **asserts** that no tampering kwargs are used (keeps the shared cache trustworthy) — tampering happens only on `ctx.fresh()` clients. (2) `async with self._http.stream("POST", url, content=body, headers=h)` under `anyio.fail_after(cfg.timeout)`; follow only same-origin 307/308 (`response.next_request` is populated with `follow_redirects=False` — verified; 302 would turn POST into GET — verified) up to `max_redirects`, else record `redirect_blocked`. (3) Classify `content-type`; JSON → read ≤ `MAX_BODY_BYTES`, parse one object (array → finding, batching removed); SSE → `httpx2.EventSource(response)` async iteration, skip comment lines, collect every `data:` JSON object, **stop after the message carrying our id**; if `wait_for_eof` wait ≤ `SSE_TERMINATION_GRACE_S` and set `stream_terminated`. (4) Append `Exchange` (redacted request headers, body, status, headers, body ≤ 64 KiB, elapsed, purpose, connection label) to `Transcript`. (5) Exceptions: `httpx2.ConnectError`, `ConnectTimeout`, `UnsupportedProtocol` (incl. TLS failures) → `ServerUnreachable`; `ReadTimeout`, `ReadError`, `RemoteProtocolError`, budget `TimeoutError`, `MAX_BODY_BYTES` → `RawReply(status=0 or partial, error=...)`.

`wire.py` pure functions: `envelope(method, params, *, req_id, meta_version, client_info, client_capabilities={}, omit_meta_keys)`, `default_headers(body, *, header_version)` (`accept`, `content-type`, `mcp-protocol-version`, `mcp-method`, `mcp-name` via `encode_header_value` for `tools/call`/`prompts/get`/`resources/read`), `classify_content_type`, `parse_json_body`, `parse_sse_events`, `redact(headers)`, `derive_origin(url)`, `classify_reply(reply) -> Outcome` (§6.6), `META_KEY_RE`, `RESERVED_PREFIX_RE`.

### 6.6 Outcome classification (one table, used by every check)
`wire.classify_reply(reply: RawReply, *, sent_origin: bool, sent_token: bool) -> Outcome(kind, status_hint, detail)`:

| `kind` | Condition | Default status in a non-gate check | Handshake (gate) status |
|---|---|---|---|
| `ok` | 2xx, JSON or SSE, response with `result`, id matches | evaluate | PASS path (shape rules §7.1) |
| `rpc_error` | a JSON-RPC `error` message (any HTTP status) | per check (code-specific rows below) | per §7.1 table |
| `modern_rejection` | 400 + code ∈ `MODERN_ERROR_CODES` | `-32020` → inconclusive + `should` warning "well-formed request rejected (intermediary?)"; `-32021` → inconclusive + `should` warning naming `data.requiredCapabilities`; `-32022` → inconclusive "server does not support 2026-07-28" | `-32020` → **fail** (`handshake.header_mismatch_on_valid_request`); `-32021` → inconclusive (era confirmed, dependents still run); `-32022` → **fail** (`handshake.version_unsupported`) |
| `method_not_found` | 404 + `-32601` | per check (`skipped`/not-implemented semantics) | fallback to `server/discover` |
| `non_modern_rejection` | 4xx/5xx or 200 with a JSON-RPC error not in the modern set, or 400 with empty/non-JSON body | inconclusive (`runner.not_run` if gate failed) | **fail** (era rule T:655-660) |
| `not_mcp_endpoint` | 404/405/406/410/421 **without** JSON-RPC body (HTML/text/empty) | inconclusive "no MCP endpoint at URL" | inconclusive |
| `auth_required` | 401 (any body); 407 | inconclusive "pass --token" / "token rejected" | inconclusive (gate blocks dependents) |
| `forbidden` | 403 without JSON-RPC body (or with, when an Origin was sent) | inconclusive "try --origin / check credentials" | inconclusive (gate blocks) |
| `server_error` | 5xx | inconclusive (one retry after 1 s in the gate only) | inconclusive after retry |
| `redirected` | 301/302/303, or cross-origin 307/308 | inconclusive "re-run against Location" | inconclusive |
| `not_jsonrpc_2xx` | 2xx with media type ∉ {json, sse}, unparsable body, non-UTF-8, JSON array, or 202 to a request | **fail** `level: must` (T:87-90, TI:31) in the check that sent it | fail |
| `transport` | `RawReply.error` set (read timeout, body cap, protocol error) | inconclusive | inconclusive |
| `unreachable` | `ServerUnreachable` raised | check `error`, remaining checks `error` "not run: server unreachable", exit 2 | same |

Rule of thumb: infrastructure (5xx, auth, redirects, timeouts) is never a conformance failure; a *server answer* that violates a MUST is.

### 6.7 `checks/base.py`
```python
class Status(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"
    ERROR = "error"


class Level(str, Enum):
    MUST = "must"
    SHOULD = "should"
    BEST_PRACTICE = "best-practice"


# (str, Enum), never enum.StrEnum (3.11+)


class Findings:
    def must(
        self,
        code: str,
        message: str,
        *,
        subject: str,
        spec_url: str,
        evidence: Mapping | None = None,
    ) -> None: ...
    def should(self, code, message, *, subject, spec_url, evidence=None) -> None: ...
    def best_practice(
        self, code, message, *, subject, evidence=None, spec_url: str | None = None
    ) -> None: ...
    def info(
        self, code, message, *, subject, evidence=None
    ) -> None: ...  # → CheckResult.info[] (never affects status)
    def inconclusive(self, reason: str) -> None: ...
    def summary(self, text: str) -> None: ...  # ≤100 chars, no trailing period
    def details(self, text: str) -> None: ...
    def requests(self, n: int) -> None: ...


class Check(ABC):
    name: ClassVar[str]
    title: ClassVar[str]
    spec_url: ClassVar[str]
    order: ClassVar[int]

    @abstractmethod
    async def run(self, ctx: CheckContext, f: Findings) -> None: ...


class CheckContext:
    cfg: ClientConfig
    transcript: Transcript
    raw: RawClient  # "main"
    era: Era = Era.UNKNOWN
    protocol_version: str | None = None
    server_info: dict | None = None
    gate_blocked: str | None = None  # set by runner after handshake

    def fresh(self, label: str, *, send_token: bool = True) -> AsyncContextManager[RawClient]: ...
    async def tools_list_pages(
        self,
    ) -> list[
        RawReply
    ]: ...  # cached; full nextCursor walk ("" continues; stop on absent/null; loop guard; MAX_PAGES)
    async def prompts_list_pages(self) -> list[RawReply]: ...
    async def resources_list_pages(self) -> list[RawReply]: ...
    async def resource_templates_list_pages(self) -> list[RawReply]: ...
    async def discover(self) -> RawReply: ...  # cached
    async def tools(self) -> list[dict]: ...  # raw dicts from all pages (never SDK-filtered)
    async def capabilities(self) -> dict | None: ...  # from discover result, raw
    def seed_tools_page0(
        self, reply: RawReply
    ) -> None: ...  # handshake stores its fresh-connection reply as page 0
```
Verdict derivation lives in the runner: `error` if exception; else `fail` if any `must` finding; else `inconclusive` if `inconclusive()` was called; else `warn` if any `should`/`best-practice` finding; else `pass`. Checks never compute status.

### 6.8 Registry and canonical order
`REGISTRY = (HandshakeCheck, SessionCheck, DiscoverCheck, ProtocolCheck, SchemasCheck, ErrorsCheck, CacheCheck, OriginCheck)`; `NAMES = tuple(c.name for c in REGISTRY)`. `select(checks, skips)` starts from `REGISTRY` or the `--check` subset (canonical order regardless of CLI order), removes `--skip`; a name in both → `UsageError`; empty → `UsageError`. Order rationale: handshake first (gate; seeds the shared tools/list page 0 and sets era); session second (untouched connections); discover/protocol (cheap, no tool calls); schemas before errors (errors needs raw tools + policy); cache after lists exist; origin last (most likely to trip a WAF).

### 6.9 `runner.py`
```python
async def run_checks(
    cfg: ClientConfig, checks: Sequence[Check], *, progress: Callable[[str], None] | None = None
) -> Report:
    transcript = Transcript()
    t0 = time.perf_counter()
    async with RawClient(cfg, transcript, label="main") as raw:
        ctx = CheckContext(cfg, transcript, raw)
        results = []
        aborted: str | None = None
        for check in checks:
            if aborted:
                results.append(error_result(check, f"not run: {aborted}"))
                continue
            if ctx.gate_blocked and check.name != "handshake":
                results.append(
                    inconclusive_result(check, code="runner.not_run", reason=ctx.gate_blocked)
                )
                continue
            f = Findings(check)
            progress and progress(f"running {check.name}…")
            try:
                with anyio.fail_after(
                    cfg.check_budget
                ):  # raises builtin TimeoutError on 3.10 and 3.13 (verified)
                    await check.run(ctx, f)
            except TimeoutError:
                f.error(f"check exceeded {cfg.check_budget:g}s")
            except ServerUnreachable as e:
                f.error(str(e))
                aborted = f"server unreachable ({type(e.cause).__name__})"
            except Exception as e:  # noqa: BLE001 — last line of defence
                f.error(
                    f"internal error: {compat.unwrap_exception_group(e)!r}",
                    traceback=traceback.format_exc(),
                )
            results.append(f.to_result())
            if check.name == "handshake":
                ctx.gate_blocked = gate_reason(results[-1])  # None unless blocking (§6.10)
        return assemble_report(cfg, results, ctx, transcript, started=t0)
```
When `handshake` is deselected (`--check cache`), the runner runs a **silent internal gate**: one `tools/list` on `main` (which is also the cached page 0), classified with §6.6; it blocks on `auth_required`/`forbidden`/`not_mcp_endpoint`/`unreachable` only, and produces no `CheckResult`.

### 6.10 Gate rule and exit codes
`gate_reason(handshake_result)`: blocks dependents (status `inconclusive`, code `runner.not_run`, summary `Not run: <reason>`) when handshake is `inconclusive` for an environment reason (401/403/not-MCP-endpoint/redirect/network/5xx) **or** `fail` for an era reason (legacy-only server, `-32022` without 2026-07-28); does **not** block on handshake shape failures (`resultType` missing, id mismatch, serverInfo) nor on `-32021`.

| Status | exit (default) | exit (`--strict`) |
|---|---|---|
| pass | 0 | 0 |
| warn | 0 | 1 |
| inconclusive | 0 | 1 |
| fail | 1 | 1 |
| error | 2 | 2 |

Run exit = max over selected checks (2 > 1 > 0); recorded in `summary.exit_code`. Usage errors → 2 (click `UsageError.exit_code == 2`, verified); `SdkIncompatibleError` → 2; `KeyboardInterrupt` → 130 (partial report not printed); `BrokenPipeError` → flush stderr, `os._exit(code)`. Legacy-only server ⇒ handshake fail ⇒ exit 1 ("does this server conform to 2026-07-28? no"). 401 without token ⇒ inconclusive everywhere ⇒ exit 0 (1 with `--strict`) with a loud `pass --token` hint.

### 6.11 `models.py` (pydantic v2, `extra="forbid"`, snake_case wire names)
```python
class Finding(BaseModel):
    code: str = Field(
        pattern=r"^[a-z]+\.[a-z0-9_]+$"
    )  # "<check>.<snake_slug>", registered in checks/codes.py
    level: Level
    subject: str  # tool name, RPC method, header, "tools/list page 2", probe id
    message: str  # one sanitized sentence, ≤500 chars
    spec_url: str | None = None  # absent only for best-practice
    evidence: dict[str, Any] | None = (
        None  # ≤2 KiB serialized; home for got/expected/http_status/rpc_code/probe/request/path/page/hint
    )
    tool: str | None = None  # handoff compat (tool name when subject is a tool)

    @computed_field
    def issue(self) -> str:
        return self.message  # handoff compat


class CheckError(BaseModel):
    type: str
    message: str


class CheckResult(BaseModel):
    name: CheckName
    title: str
    status: Status
    summary: str
    details: str = ""
    spec_url: str
    failures: list[Finding] = []
    warnings: list[Finding] = []
    info: list[Finding] = []
    duration_ms: int = Field(ge=0)
    requests: int = 0
    error: CheckError | None = None

    @computed_field
    def severity(self) -> Level | None: ...  # highest level among failures+warnings
    @computed_field
    def passed(
        self,
    ) -> bool: ...  # "did not raise the exit code": pass; warn/inconclusive unless strict


class Summary(BaseModel):
    total: int
    passed: int
    warned: int
    failed: int
    inconclusive: int
    errored: int
    skipped: list[CheckName]
    exit_code: Literal[0, 1, 2]


class Request(BaseModel):
    origin: str | None
    auth: Literal["none", "bearer"]
    extra_headers: list[str] = []
    tls_verify: bool


class Options(BaseModel):
    strict: bool
    probe: Literal["safe", "all", "none"]
    max_tools: int
    timeout: float
    legacy_probes: bool


class Report(BaseModel):
    schema_version: Literal[1] = 1
    mcpcheck_version: str
    sdk_version: str
    python_version: str
    spec_revision: Literal["2026-07-28"] = "2026-07-28"
    server: str  # URL as given (handoff shape, U11)
    request: Request
    options: Options
    protocol_version: str | None
    server_supported_versions: list[str] | None
    server_info: dict | None
    era: Literal["modern-only", "dual-era", "legacy-only", "unknown"]
    timestamp: str  # "%Y-%m-%dT%H:%M:%SZ"
    duration_ms: int
    checks: list[CheckResult]
    summary: Summary
```
Placement rule: `must` → `failures[]`; `should`/`best-practice` → `warnings[]`; `info` → `info[]`. `scripts/gen_report_schema.py` writes `src/mcpcheck/schema/report.v1.json` from `Report.model_json_schema(mode="serialization")` plus `$schema`/`$id` and the conditional `allOf` rules of §8.7; `tests/test_report_schema.py` asserts in-sync, metaschema-valid, example-valid, and that the repo copy `schema/mcpcheck-report.v1.schema.json` is byte-identical.

### 6.12 `sdk_compat.py`, `compat.py`, Python 3.10 rules
`require_sdk()` imports the symbols listed in Appendix A (an `ImportError` becomes `SdkIncompatibleError("mcpcheck needs mcp>=2.2,<3 exposing <symbol>; installed mcp <ver>. Fix: pip install -U 'mcp>=2.2,<3'")`, exit 2) and asserts `LATEST_PROTOCOL_VERSION == "2026-07-28"` and `"2026-07-28" in MODERN_PROTOCOL_VERSIONS` (otherwise a stderr warning "installed SDK targets a newer revision; results apply to 2026-07-28", not an error). Nothing else; the old 25-symbol table is dropped (Appendix C).

Forbidden in `src/` (verified missing on CPython 3.10): `enum.StrEnum`, `except*`, builtin `ExceptionGroup` (import from `compat`, backed by the `exceptiongroup` package that anyio pulls in on <3.11), `asyncio.timeout`/`TaskGroup` (use anyio), `typing.Self`, `tomllib`, `datetime.UTC`, `BaseException.add_note`. Allowed: `from __future__ import annotations`, `X | Y`, `match`, `dataclass(slots=True)`, `importlib.resources.files`. Enforced by ruff `UP` (target inferred from `requires-python`), mypy `python_version = "3.10"`, the 3.10 CI cell, and `tests/test_py310.py` (`ast.parse(src, feature_version=(3, 10))` over `src/` plus a grep for `StrEnum|asyncio.timeout|asyncio.TaskGroup|except\*`).

### 6.13 Logging and redaction
stdlib `logging`; `mcpcheck` logger WARNING by default, DEBUG at `-vv`; `logging.getLogger("mcp")` gets a `NullHandler` unless `-vv`. `wire.redact()` masks `authorization`, `proxy-authorization`, `cookie`, `set-cookie`, `x-api-key` and any header whose name contains `token|secret|password` as `****` (Bearer keeps the scheme word), and scrubs every literal occurrence of `--token` and `-H` values from headers, bodies and URLs before anything reaches a console, the transcript or the report.

### 6.14 Adding a check (extensibility)
Create `checks/foo.py` with `class FooCheck(Check)` (`name`, `title`, `spec_url`, `order`, `run`), register codes in `codes.py`, append to `REGISTRY`, add `tests/test_foo.py` (MockTransport scenarios + one fixture row), add a README table row, add the name to `CheckName`/schema enum (additive within `schema_version` 1). `--check foo`, renderers and exit aggregation need no change.

---

## 7. Check specifications

Each subsection has the same ten parts: (1) purpose and spec basis (register IDs from §5), (2) inputs, (3) algorithm with exact wire shapes, (4) verdict rules (code → level → spec), (5) one-line summaries, (6) finding-record mapping with examples, (7) edge cases, (8) safety, (9) tests, (10) spec URL constants and deviations. Codes follow `^[a-z]+\.[a-z0-9_]+$` and are registered in `checks/codes.py`. Human layout, glyphs and column widths are owned by §8.5; checks supply only `summary` text.

### 7.1 handshake

**(1) Purpose.** Establish, with one complete 2026-07-28 request on a **fresh** connection and no prior `initialize`, that the server serves modern per-request-metadata clients statelessly; classify the era; record the protocol version answered. It is the gate (§6.10). Register: HS-2…HS-18 (§5.1).

**(2) Inputs.** `ctx.fresh("handshake")` (HS-2 is about the first request on a connection); `cfg.token`, `cfg.origin`, `cfg.legacy_probes`. Writes `ctx.era`, `ctx.protocol_version`, `ctx.server_info`, `ctx.seed_tools_page0(reply)`. Runs first; `--check handshake` alone makes 1–3 POSTs.

**(3) Algorithm.**
Step 0 — primary request (exact; verified to yield `200 application/json`, result keys `_meta, cacheScope, resultType, tools, ttlMs` against the SDK fixture):
```http
POST /mcp HTTP/1.1
Accept: application/json, text/event-stream
Content-Type: application/json
MCP-Protocol-Version: 2026-07-28
Mcp-Method: tools/list
User-Agent: mcpcheck/<ver> (+https://github.com/suresh-jbt/mcp-check)
Authorization: Bearer <token>        # only with --token
Origin: <value>                      # only with --origin

{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{},"io.modelcontextprotocol/clientInfo":{"name":"mcpcheck","version":"<ver>"}}}}
```
Step 1 — `reply = await raw.post("tools/list", wait_for_eof=True)` on the fresh client; one retry after 1 s on 5xx. Step 2 — `outcome = classify_reply(reply)` (§6.6, gate column). Step 3 — on `method_not_found`: repeat with `server/discover` (same `_meta`); a 200 with `resultType:"complete"`, `supportedVersions ∋ "2026-07-28"`, `capabilities` object ⇒ PASS "verified via server/discover"; if that result declares `capabilities.tools`, the 404 on `tools/list` is a `must` finding (HS-3); if discover is also `-32601` ⇒ fail `handshake.discover_missing`. Step 4 — shape sub-checks on `ok` (table below). Step 5 — **only with `--legacy-probes`** (U4): header-less, `_meta`-less `{"jsonrpc":"2.0","id":2,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{…}}}` on a second fresh client; 200 + `result.protocolVersion` ⇒ era `dual-era` (or `legacy-only` when the primary failed) and record `legacy_protocol_version`; 4xx/404 ⇒ `modern-only`, with a `should` warning when the error names no `\d{4}-\d{2}-\d{2}` (HS-12); any minted `Mcp-Session-Id` is recorded in `info` only (V:180-182); **no `DELETE`**. Without the flag era is `unknown` when the primary passes (details: "legacy initialize: not probed (--legacy-probes)"). Step 6 — set ctx fields; seed page 0 when the primary produced a result (even if shape-failed).

**(4) Verdict rules.**
| Observation | Code | Level | Spec |
|---|---|---|---|
| 200/SSE result, `id == 1`, `resultType == "complete"` | — (pass) | — | HS-2..HS-5 |
| result present, no `resultType` (pre-2026 shape; real mcp 1.30 stateless does this) | `handshake.legacy_shaped_result` | must | B:75 |
| `resultType` ≠ `"complete"` (incl. `input_required` — `tools/list` surface is `ListToolsResult` only, verified) | `handshake.unexpected_result_type` | must | B:86 |
| `response.id != 1` | `handshake.id_mismatch` | must | B:72 |
| `result.tools` not a list | `handshake.tools_not_list` | must | SCH:4823-4831 |
| `_meta["io.modelcontextprotocol/serverInfo"]` absent or lacking string `name`/`version` | `handshake.server_info_missing` | should | B:398-404, SCH:4187-4194 |
| SSE frame that is a JSON-RPC request | `handshake.server_request_on_stream` | must | T:114 |
| SSE not terminated within `SSE_TERMINATION_GRACE_S` | `handshake.stream_not_terminated` | should | T:122 |
| 400 + `-32022`, `data.supported ∌ 2026-07-28` | `handshake.version_unsupported` ("supports: …") | must | T:263-269 |
| 400 + `-32022`, `data.supported ∋ 2026-07-28` | `handshake.version_unsupported` + `handshake.contradictory_unsupported` | must | T:263-269 |
| `-32022` `data` malformed (`UnsupportedProtocolVersionErrorData.model_validate` fails) | `handshake.unsupported_data_malformed` | must | SCH:706-716 |
| 400 + `-32020` on our well-formed request | `handshake.header_mismatch_on_valid_request` (hint: intermediary stripping headers) | must | T:583-602 |
| 400 + `-32021` | inconclusive; `handshake.client_capability_demanded` (`data.requiredCapabilities`) | should | B:389-394 |
| `non_modern_rejection` (any status; e.g. mcp 1.x `400 -32600 "Bad Request: Missing session ID"`, `404 -32600 "Session not found"`, `-32600 "Unsupported protocol version"`, `-32602 "...not initialized"`, empty-body 400) | `handshake.initialize_required` with `evidence.fingerprint` ∈ {`legacy_session`, `legacy_version_reject`, `not_initialized`, `non_modern_error`, `empty_400`} (labels only; verified strings for mcp 1.30) | must | T:655-660, V:145-148, B:193-195 |
| `not_jsonrpc_2xx` (HTML, bad JSON, non-UTF-8, 202, array) | `handshake.not_jsonrpc` | must | T:81-90, TI:31 |
| `method_not_found` on both `tools/list` and `server/discover` | `handshake.discover_missing` | must | DI:9-11 |
| discover fallback declares `capabilities.tools` but `tools/list` is 404 | `handshake.tools_declared_but_unimplemented` | must | TL:62 |
| `auth_required` / `forbidden` / `not_mcp_endpoint` / `redirected` / `server_error` ×2 / `transport` | inconclusive (gate blocks) | — | §6.6 |
| `Mcp-Session-Id` response header on the modern reply | **no finding here**; `evidence.session_id_seen=true`; `session` owns it | — | CL:14 |
| legacy `initialize` error names no version (`--legacy-probes`) | `handshake.initialize_error_names_no_version` | should | V:156-159 |
| `--legacy-probes`: dual-era observed | `handshake.dual_era` in `info[]` | info | V:130-132 |

**(5) Summaries.** pass: `Server does not require \`initialize\``; pass + legacy probe answered: `… (also serves legacy clients)`; via discover: `… (no tools; verified via server/discover)`; `initialize_required`: `Server requires \`initialize\` (HTTP 400, -32600 "Bad Request: Missing session ID")`; `legacy_shaped_result`: `Server answered a 2026-07-28 request with a pre-2026 result (no resultType)`; `version_unsupported`: `Modern server does not support 2026-07-28 (supports: 2027-01-01)`; `not_jsonrpc`: `Non-MCP response to \`tools/list\` (HTTP 200 text/html)`; inconclusive: `Not evaluated: HTTP 401 (pass --token)` / `Not evaluated: HTTP 403 (try --origin)` / `Not evaluated: no MCP endpoint at URL (HTTP 404 text/html)` / `Not evaluated: redirects (302) to <Location>` / `Not evaluated: cannot connect (ConnectError)`.

**(6) Finding mapping.** `subject` = `"tools/list"` | `"server/discover"` | `"initialize"`; `evidence` = `{http_status, media_type, rpc_code, rpc_message≤300, got, expected, fingerprint?, hint?, connection:"handshake"}`. Example:
```json
{"code":"handshake.initialize_required","level":"must","subject":"tools/list",
 "message":"Server rejected a complete 2026-07-28 request with a non-modern error; it requires the legacy initialize handshake",
 "spec_url":"https://modelcontextprotocol.io/specification/2026-07-28/basic#statelessness",
 "evidence":{"http_status":400,"rpc_code":-32600,"rpc_message":"Bad Request: Missing session ID","fingerprint":"legacy_session",
             "expected":"HTTP 200 with a result (resultType \"complete\"), or a recognized modern error (-32020/-32021/-32022)",
             "hint":"see basic/transports/streamable-http#backward-compatibility","session_id_seen":true}}
```

**(7) Edge cases.** Total per-request budget via `anyio.fail_after` (keepalive SSE defeats per-chunk timeouts — verified); 401/403/404-HTML/5xx/redirect → inconclusive, never fail; `Content-Type` compared on media type, non-UTF-8 charset → `not_jsonrpc`; same-origin 307/308 followed ≤5, 302 blocked (would downgrade POST→GET — verified); `nextCursor` not followed here (page 0 seeded; `ctx.tools_list_pages()` continues on `main`); empty `tools: []` passes; dual-era SDK server passes (modern path never mints ids — verified both SDK versions); huge body → `transport` inconclusive; SSE `id:` fields ignored.

**(8) Safety.** One `tools/list` (+ optional `server/discover`); with `--legacy-probes` one `initialize` that may allocate a server-side session (idle timeout 1800 s on SDK servers) — disclosed in README; no `DELETE`. Token sent only to the configured URL and same-origin redirects.

**(9) Tests** (`tests/test_handshake.py`; F1 = reference server, F2 = misbehaving server toggles/variants; all parametrised `sse ∈ {False, True}` where applicable):
| Test | Fixture | Expected |
|---|---|---|
| `test_pass_reference[_json_response]` | F1 default / `--json-response` | pass; era `unknown` (no legacy probe); `ctx.protocol_version == "2026-07-28"`; page 0 seeded |
| `test_pass_legacy_probes_dual_era` | F1 `--stateful`, `--legacy-probes` | pass; era `dual-era`; `info` has `handshake.dual_era`; no `DELETE` in request log |
| `test_fail_legacy_stateful_1x_shape` | F2 `require_initialize` (400 `-32600 "Bad Request: Missing session ID"`, id `"server-error"`) | fail `handshake.initialize_required`, fingerprint `legacy_session` |
| `test_fail_legacy_version_reject` | F2 variant (400 `-32600 "…Unsupported protocol version… 2024-11-05, …"`) | fail; `server_supported_versions` parsed from message (`source: message`) |
| `test_fail_legacy_shaped_result` | F2 `legacy_shape` | fail `handshake.legacy_shaped_result`; page 0 still seeded |
| `test_fail_unsupported[_contradiction][_bad_data]` | F2 `v32022` variants | fail with the three codes |
| `test_fail_header_mismatch_on_valid` | F2 `always_32020` | fail |
| `test_inconclusive_missing_capability` | F2 `always_32021` | inconclusive + should warning; dependents **not** blocked |
| `test_fallback_discover_no_tools` / `test_fail_discover_missing` / `test_fail_tools_declared_404` | F2 `no_tools` / `nothing` / `tools_declared_404` | pass via discover / fail / fail |
| `test_warn_serverinfo_missing`, `test_fail_wrong_id`, `test_fail_input_required_on_list`, `test_fail_202`, `test_fail_sse_server_request`, `test_warn_sse_lingers` (returns within ~2 s), `test_pass_sse_progress_then_result` | F2 variants | as named |
| `test_inconclusive_401/403/404_html/500_after_retry (2 POSTs)/302/cross_origin_307 (token never sent cross-origin)/timeout_budget (--timeout 2, wall < 3 s)/connect_refused` | F2 / closed port | inconclusive; gate blocks dependents; exit 0 (1 `--strict`); connect refused → error, exit 2 |
| `test_fail_empty_400`, `test_fail_bad_json`, `test_fail_huge_body→inconclusive` | F2 | as named |
| `test_origin_header_only_with_flag` | F1 request log | default no `Origin`; `--origin X` present |
| `test_legacy_1x_real` (`@legacy`, env-gated, CI job with `mcp<2`) | mcp 1.30 FastMCP | fail `legacy_session`; stateless variant fail `legacy_version_reject` |

**(10) URLs/deviations.** `spec_url` = `SPEC_STATELESSNESS`; findings cite `SPEC_RESULT_RESPONSES`, `SPEC_META`, `SPEC_BACKCOMPAT_HTTP`, `SPEC_PROTOCOL_VERSION_HEADER`, `SPEC_DISCOVER`, `SPEC_TOOLS_CAPABILITIES`. Deviations from handoff: result must be 2026-shaped (B:75) and id must match; string matching only labels, the era rule (T:655-660) decides; `Mcp-Session-Id` is not double-counted (session owns it); INCONCLUSIVE added; fallback to `server/discover`; raw client not `ClientSession` (pre-adopt requests are legacy-shaped, `MCPError` loses status/headers, `discover()` after `adopt()` is a no-op — all verified); legacy probe opt-in (U4); redirects same-origin 307/308 only.

### 7.2 session

**(1) Purpose.** Prove transport-level statelessness for modern traffic: no `Mcp-Session-Id` minted/echoed/required, list results identical across fresh connections and unaffected by other requests on a connection. Register S1–S14 (§5.2).

**(2) Inputs.** `ctx.fresh(f"session-{k}")` ×(`SESSION_CONNECTIONS`+1); `ctx.capabilities()` (decides whether `resources/list`/`prompts/list` are attempted; if unknown, try once and treat `-32601` as unsupported); `ctx.tools()` as "sample 0" (run cache, no traffic); `ctx.transcript` for the post-scan. Runs second; `--check session` alone works (sample 0 absent).

**(3) Algorithm.** Modern-shaped body as in §7.1 (`Mcp-Method: tools/list|prompts/list|resources/list`; later pages add `"cursor"`). For k = 1..3, on one fresh connection, in order: (a) `tools/list` full walk → `tools_first`; (b) `resources/list` walk (key `uri`) if declared/unknown; (c) `prompts/list` walk (key `name`); (d) `tools/list` again → `tools_second` (tests "side effect of other requests on the connection", S6). Record per response: status, `headers.get_list("mcp-session-id")`, wire casing from `headers.raw`, media type, messages. SSE: close as soon as the response frame arrives (S14). Then (e) bogus-id probe on a 4th fresh connection: `tools/list` walk with `Mcp-Session-Id: mcpcheck-bogus-<16 hex>` — expect 200, identical defs, no echo/mint. (f) Compare with `canon(item) = json.dumps(item, sort_keys=True, separators=(",",":"), ensure_ascii=False)`: `keys` (sorted identities), `defs` (sorted canon), `order`. (g) Transcript scan: every modern exchange recorded so far (handshake + own) whose response carries `mcp-session-id` with a value we did not send → `session.session_id_minted` (subject = the method). Legacy-era exchanges (header-less/`initialize`, only with `--legacy-probes`) are excluded (S10). The runner repeats the scan after all checks and, if new occurrences appear, appends them to the session result's `warnings` before the report is rendered (human table is printed after all checks, so no amendment problem).

**(4) Verdict rules.**
| Code | Condition | Level | Spec |
|---|---|---|---|
| `session.required` | modern `tools/list` without session header rejected AND (`rpc_message ~ /session/i` OR handshake fingerprint `legacy_session`) | must | B:193-195 (S4) |
| `session.bogus_id_rejected` | bogus-id probe non-2xx/JSON-RPC error while sample 1 succeeded | must | B:193-195, T:675-682 |
| `session.list_varies_per_connection` | `keys` differ between fresh samples (same token) | must | TL:62-69 / R:76-83 / P:59-66 |
| `session.list_varies_within_connection` | `tools_first` vs `tools_second` differ on one connection | must | TL:65-66 |
| `session.list_varies_with_session_header` | bogus-id defs ≠ sample 1 | must | TL:62-69 |
| `session.session_id_minted` | modern response carries an id we did not send | should | T:680-681; CL:14 |
| `session.session_id_echoed` | bogus value echoed back | should | T:680-681 |
| `session.definition_unstable` | same keys, different definition bytes (first differing JSON pointer in evidence) | should | TL:62-69 (conservative) |
| `session.order_not_deterministic` | same defs, different order (tools) | should | TL:71-74 |
| `session.list_changed_during_run` | sample 0 keys ≠ sample 1 | best-practice | TL:64 "MAY change over time" |
| `session.cursor_loop` | repeated cursor or > `MAX_PAGES` | should | PG:96-98 |
| `session.partial_samples` | some connections failed, ≥2 valid | should | B:198-199 |
| inconclusive | <2 valid samples for a non-session reason (401/403/5xx/timeout/not-MCP) | — | §6.6 |
Dropped from the session design: the `GET`-expects-405 probe (T:678-679 is scoped to modern-only servers; a dual-era server MAY host the legacy stream — moved to `--legacy-probes` as info, §7.8) and `--probe-delete`.

**(5) Summaries.** pass: `No \`Mcp-Session-Id\` issued or expected; lists identical across 3 connections`; `required`: `Server requires \`Mcp-Session-Id\` on modern requests (HTTP 400, -32600 "…Missing session ID")`; `bogus_id_rejected`: `Server enforces \`Mcp-Session-Id\` (bogus id → HTTP 404 "Session not found")`; `list_varies_per_connection`: `tools/list varies per connection (3 connections, 3 distinct tool sets)`; `session_id_minted`: `\`Mcp-Session-Id\` minted on modern responses (not required; SHOULD not mint)`; `order_not_deterministic`: `tools/list order differs across connections (set identical)`; inconclusive: `Not evaluated: HTTP 401 (pass --token)`. Several findings → highest-priority first + ` (+N more)`.

**(6) Finding mapping.** `subject` = `"Mcp-Session-Id"` | `"tools/list"` | `"resources/list"` | `"prompts/list"`; `evidence` = `{connection: 1|…|"bogus"|null, method, http_status, rpc_code, header_as_sent, value, sets: {conn1:[…],…}, first_diff_pointer}`.
```json
{"code":"session.list_varies_per_connection","level":"must","subject":"tools/list",
 "message":"tool set differs between fresh connections although the same Authorization was presented",
 "spec_url":"https://modelcontextprotocol.io/specification/2026-07-28/server/tools#capabilities",
 "evidence":{"connection":2,"http_status":200,"sets":{"1":["alpha","beta"],"2":["alpha","beta","gamma"],"3":["alpha"]}}}
```

**(7) Edge cases.** Timeouts per request (`anyio.fail_after`), check budget 10×; 401/407 → inconclusive; 403 → inconclusive with Origin hint; 404+`-32601` → inconclusive "tools/list not implemented"; 400+modern codes → inconclusive; 429/5xx → inconclusive (5xx with `/session/i` message → `session.required`); 202/non-JSON → sample invalid; redirects per client policy; `""` cursor continues; empty lists equal → pass; dual-era `--stateful` SDK server passes (legacy exchanges mint ids, modern path never — verified); duplicate names compared as multisets; invalid-`x-mcp-header` tools present consistently in raw JSON; load balancers with divergent backends will fail `list_varies_per_connection` (details show per-connection sets).

**(8) Safety.** List methods only; 4 fresh connections; bogus id cannot act on state (never obtained/terminated); ≈14–20 requests with all three capabilities.

**(9) Tests** (`tests/test_session.py`; variants from `misbehaving_server.Toggles`; `X-Client-Port` header added by the fixture for freshness assertions): `test_pass_sdk_stateless` ✔, `test_pass_sdk_stateful_modern_path` ✔, `test_pass_json_response` ✔, `test_pass_paged` (2 pages ×3 conns), `test_pass_empty_list`, `test_pass_sse`, `test_pass_empty_string_cursor` (second request carries `"cursor": ""`), `test_fresh_connections` (3 distinct ports) ✔, `test_fail_required` ✔ (`require_session_id`: both `session.required` and `session.bogus_id_rejected`), `test_warn_minted` ✔ (`mint_session_id`; evidence shows wire casing), `test_warn_echoed`, `test_fail_vary_conn` ✔, `test_fail_vary_count` ✔ (three variation codes), `test_warn_order` ✔ (`nondeterministic_order`), `test_warn_definition_unstable`, `test_inconclusive_401` (+ `--token x` → pass), `test_inconclusive_timeout` (`--timeout 0.5`), `test_inconclusive_too_large`, `test_transcript_scan_flags_other_checks` (`mint_session_id`, run discover first → `session.session_id_minted` with subject `server/discover`), `test_scan_ignores_legacy_exchanges`, `test_versions_matrix`. ✔ = reproduced by the prototype on both SDK versions.

**(10) URLs/deviations.** `spec_url` = `SPEC_EARLIER_REVISIONS`; findings cite `SPEC_STATELESSNESS`, `SPEC_TOOLS_CAPABILITIES`, `SPEC_RESOURCES_CAPABILITIES`, `SPEC_PROMPTS_CAPABILITIES` (anchor verified today), `SPEC_PAGINATION_GUIDELINES`. Deviations: fresh connections instead of sequential calls; list-invariance MUSTs added; minted-not-required is `should` (T:675-682 is SHOULD; dual-era allowed V:184-185); bogus-id probe added; raw client; legacy-era ids never findings; pagination walked; GET probe removed from this check.

### 7.3 discover

**(1) Purpose.** Prove `server/discover` is implemented and well-formed (required fields, hints, version advertisement, identity) and that advertised capabilities agree with the list operations actually served. Register N1–N28 (§5.3).

**(2) Inputs.** `ctx.discover()` (cached; one POST on `main`), `ctx.tools_list_pages()`, `ctx.prompts_list_pages()`, `ctx.resources_list_pages()`, `ctx.resource_templates_list_pages()` for the cross-check (lazily fetched; `--check discover` alone costs ≤4 extra read-only POSTs); the shared pure `validate_cache_hints()` from §7.6. No fresh connection needed. SDK pieces: `mcp_types.methods.validate_server_result`, `mcp_types.DiscoverResult`, `Implementation`, `UnsupportedProtocolVersionErrorData`, `SERVER_INFO_META_KEY`, `KNOWN_PROTOCOL_VERSIONS`.

**(3) Algorithm.** Step 0 — `POST` with `Mcp-Method: server/discover`, `params` = `{_meta}` only (DI:15), no `Mcp-Name`. Verified against the SDK fixture: 200 JSON, result keys `_meta, cacheScope, capabilities, instructions, resultType, supportedVersions, ttlMs`. Step 1 — `classify_reply` (§6.6); specific rows: `method_not_found` → fail `discover.not_implemented` (N1); `-32601` with non-404 status → additionally `discover.wrong_status_for_32601` (T:271-275); `-32022` → fail `discover.version_unsupported` (+ `contradictory_unsupported`, `unsupported_data_malformed`, `wrong_status_for_32022` as in §7.1); `-32020` → fail `discover.header_mismatch_on_valid_request`; `-32021` → inconclusive + should; `-32602` on our valid request → fail `discover.invalid_params_on_valid_request`; other JSON-RPC error → fail `discover.unexpected_error` plus N26 range findings (`discover.forbidden_error_code` must for `-32002`/`-32042`/undefined `-3202x..-32099`; `discover.legacy_error_code` should for `-32000..-32019`); 405 → fail `discover.post_rejected` (T:45-47); `not_jsonrpc_2xx` → fail `discover.not_jsonrpc`; infra outcomes → inconclusive. Step 2 — SSE handling: parser per WHATWG (blank-line events, `\r\n`, comment lines, multi-line `data:`); zero responses → fail; >1 response → fail; server request → fail `discover.server_request_on_stream` (T:114); `notifications/message` → fail `discover.log_without_loglevel` (LG:65-66); `notifications/progress` → info (we sent no token; T:112-113 "MUST relate" is not clearly violated); not terminated in grace → should `discover.stream_not_terminated`; no `X-Accel-Buffering: no` → should `discover.no_accel_buffering_header`. Step 3 — envelope: `jsonrpc != "2.0"`, `id` null/mismatch (type-sensitive), both/neither `result`/`error` → must findings; continue validating after an id mismatch. Step 4 — `validate_discover_result(result) -> list[Finding]` (pure):
| Field | Rule → code (level) |
|---|---|
| `resultType` | missing → `discover.result_type_missing` (must, B:75); not string → must; `"complete"` ok; `"input_required"` → `discover.unexpected_result_type` (must; non-MRTR operation, SCH surface) and skip hint checks; other → `discover.unrecognized_result_type` (must, B:86, V:123-125) |
| `supportedVersions` | missing/not list/element not string → `discover.supported_versions_invalid` (must, SCH:4026); empty or `2026-07-28` absent while this request succeeded → `discover.supported_versions_omit_served` (best-practice; note: `ClientSession.adopt()` raises `RuntimeError` for such servers — verified); duplicates → should; non-date-shaped entries → **info** (mcp-types says future ids need not be date-shaped) |
| `capabilities` | missing/not object → `discover.capabilities_invalid` (must, SCH:4027); known key present with `null` → `discover.capability_null` (must; schema requires object; mcp-types swallows it — verified); non-boolean `listChanged`/`subscribe` → must (mcp-types lax-coerces `"yes"` — verified, so raw check needed); `logging` present → `discover.logging_deprecated` (should, DP:14-16); `extensions` keys must match `META_KEY_RE` and contain `/` → `discover.extension_id_invalid` (must, V:87-89), values must be objects (must); unknown keys → info (SCH:4243) |
| `_meta` | not object → must; keys not matching `META_KEY_RE` → `discover.meta_key_invalid` (must, B:332-346); `serverInfo` absent → `discover.server_info_missing` (should, DI:100-101); present but `Implementation.model_validate` fails → `discover.server_info_malformed` (must; `validate_server_result` does not look inside serverInfo — verified); `websiteUrl` not http(s) / icon `src` not https/data → best-practice; unknown reserved-prefix keys → should (B:329-330) |
| `instructions` | absent → info; not string → must; length in details |
| `ttlMs` / `cacheScope` | via shared `validate_cache_hints("server/discover", 0, result, code_prefix="discover")` (§7.6): missing → must (CA:15-18); not JSON int (bool/str/null/fractional float) → must; integral float → best-practice; `< 0` → must (CA:62); `> TTL_SANITY_MS` → best-practice; scope not exactly `public|private` → must (CA:46) |
| extra keys | top-level `serverInfo`/`protocolVersion` → `discover.legacy_initialize_fields` (best-practice: half-ported legacy server); `nextCursor` → should "not paginated"; others info |
Step 5 — SDK safety net: `validate_server_result("server/discover","2026-07-28",result)`; each `ValidationError` loc not already covered → `discover.schema_validation` (must). Step 6 — capability cross-check for P ∈ {tools, prompts, resources} (resources counts `resources/list` OR `resources/templates/list` with ≥1 item):
| declared | list outcome | finding |
|---|---|---|
| yes | ok (incl. 0 items — TL:63 MAY be empty) | info "consistent" |
| yes | `-32601` | `discover.capability_declared_but_unimplemented` (must, TL:62/R:76/P:59) |
| yes | other error/transport | should "could not verify" |
| no | ok ≥1 item | `discover.capability_undeclared` (must, TL:47/R:43/P:43-44) |
| no | ok 0 items | should "served empty but undeclared; declare it or answer -32601" |
| no | `-32601` / transport | info |
Step 7 — compose; set `ctx.protocol_version`, `ctx.server_info`, `Report.server_supported_versions`.

**(4) Verdict** per §6.7 rollup. Opinionated items labelled best-practice: HTTP `Cache-Control`/`ETag`/… are **info only** (no normative text; SDK server emits none — verified); O2 date shape → info; O3 omitted served version → best-practice; O4 integral float → best-practice; O5 legacy fields → best-practice; body cap → inconclusive.

**(5) Summaries.** pass: `\`server/discover\` responds correctly` (+` (2 warnings)`); `not_implemented`: `\`server/discover\` not implemented (-32601)`; `version_unsupported`: `Server does not support 2026-07-28 (supports: 2025-11-25)`; other error: `\`server/discover\` rejected a valid request (-32020: <message ≤60>)`; HTTP-level: `\`server/discover\` returned HTTP 405`; not JSON: `\`server/discover\` response is not JSON-RPC (Content-Type text/html)`; id: `\`server/discover\` response id "other" does not match request id 1`; fields: `\`server/discover\` result missing ttlMs, cacheScope` / `… result invalid: cacheScope "user" (+2 more)`; cross-check: `Capabilities inconsistent: tools/list works but capabilities.tools undeclared`; inconclusive: `Not evaluated: HTTP 401 (pass --token)`.

**(6) Finding mapping.** `subject` = JSON-pointer-like path (`result.ttlMs`, `result._meta["io.modelcontextprotocol/serverInfo"]`, `result.capabilities.extensions["bad"]`, `http.status`, `rpc.id`, `capabilities.tools ↔ tools/list`); `evidence` = `{got, expected, rfc_level, hint, http_status, media_type, elapsed_ms, cache_headers(info)}`.
```json
{"code":"discover.server_info_missing","level":"should","subject":"result._meta[\"io.modelcontextprotocol/serverInfo\"]",
 "message":"server does not identify itself","spec_url":"https://modelcontextprotocol.io/specification/2026-07-28/server/discover#discoverresult",
 "evidence":{"got":null,"expected":"{\"name\": string, \"version\": string}","hint":"Servers SHOULD include io.modelcontextprotocol/serverInfo in every result's _meta (basic#_meta)."}}
```

**(7) Edge cases.** Timeouts/401/403/5xx/redirects → inconclusive (403 hint depends on `sent_origin`); JSON without `Content-Type` → fail on header but body still validated; 2 MiB `instructions` fine; `input_required` → must + hints skipped; unknown future versions (2026-10-13) → info; id compared type-sensitively (`1` ≠ `"1"`); `Mcp-Session-Id` not judged here.

**(8) Safety.** One `server/discover` POST (pure query, DI:9-10) + up to four cached list fetches; nothing else.

**(9) Tests** (`tests/test_discover.py`): pure `CASES` table over `validate_discover_result` (42 payload variants from the prototype) + integration: `test_pass_reference[_json][_stateful]`, `test_pass_no_cache_hints_defaults` (`MCPServer` without `cache_hints` emits `ttlMs:0 cacheScope:"private"` — verified), `test_pass_no_primitives` (`--empty`: all three capabilities declared, empty lists), `test_pass_bare_lowlevel` (`capabilities: {}`, lists -32601), `test_pass_sse`, `test_warn_sse_no_accel`, `test_warn_sse_not_terminated` (~2 s), `test_fail_sse_notifications_message`, `test_fail_sse_server_request`, `test_fail_not_implemented_404/_200_status`, `test_fail_unsupported_version[_contradiction][_bad_data][_wrong_status]`, `test_fail_header_mismatch`, `test_inconclusive_32021`, `test_fail_32602`, `test_fail_forbidden_error_code`, `test_fail_missing_hints`, `test_fail_negative_ttl`, `test_fail_ttl_string/bool/fraction` (pass the SDK validator — proves raw checks needed), `test_warn_ttl_integral_float`, `test_fail_cachescope_user/_case`, `test_fail_missing_resulttype`, `test_fail_unknown_resulttype`, `test_fail_input_required_resulttype`, `test_warn_supported_versions_omit_2026`, `test_fail_supported_versions_empty_type/_nonstring`, `test_info_supported_versions_nondate`, `test_warn_serverinfo_absent`, `test_fail_serverinfo_malformed`, `test_warn_serverinfo_top_level`, `test_fail_capabilities_missing/_null/_tools_true`, `test_pass_capabilities_unknown_key`, `test_warn_capabilities_logging`, `test_fail_extensions_bad_identifier`, `test_fail_listchanged_not_boolean`, `test_fail_meta_bad_key`, `test_fail_instructions_not_string`, `test_warn_nextcursor_present`, `test_fail_html_200`, `test_fail_json_without_content_type`, `test_fail_json_array`, `test_fail_wrong_id/_null_id` (< 1 s; SDK would hang 10 s), `test_inconclusive_401[_empty]/403/503/timeout`, `test_fail_405/406→inconclusive(not_mcp_endpoint)/202/500→inconclusive`, `test_pass_redirect_307_same`, `test_inconclusive_redirect_302/_cross`, `test_info_cache_headers`, `test_pass_huge_instructions`, `test_inconclusive_body_too_large`, `test_fail_xcheck_undeclared_tools`, `test_fail_xcheck_declared_404`, `test_warn_xcheck_undeclared_empty`, `test_xcheck_fetches_each_list_once` (1 discover + ≤4 list POSTs), `test_sdk_versions_matrix`.

**(10) URLs/deviations.** `spec_url` = `SPEC_DISCOVER`; findings cite `SPEC_DISCOVER_RESULT`, `SPEC_VERSION_NEGOTIATION`, `SPEC_EXTENSION_NEGOTIATION`, `SPEC_RESULT_RESPONSES`, `SPEC_ERROR_CODES`, `SPEC_META`, `SPEC_CACHEABLE_RESULTS`, `SPEC_TTL`, `SPEC_CACHE_SCOPE`, `SPEC_*_CAPABILITIES`, `SPEC_PROTOCOL_VERSION_HEADER`, `SPEC_SENDING_MESSAGES`, `SPEC_RECEIVING_MESSAGES`, `SPEC_LOGGING`, `SPEC_TRANSPORTS_UTF8`, `SPEC_SCHEMA_DISCOVER_RESULT`, `SPEC_SCHEMA_UNSUPPORTED_VERSION`, `SPEC_SCHEMA_IMPLEMENTATION`, `SPEC_SCHEMA_SERVER_CAPABILITIES`, `SPEC_DEPRECATED`. Deviations: serverInfo in `_meta` and SHOULD; HTTP cache headers info-only; MUST checks added (resultType, shape, hints, 404 for -32601, -32022 data, `_meta` key format, extension ids, SSE rules); inconclusive status; raw client; two-directional cross-check; the `data{}` block and separate `observations[]` of the draft are folded into `evidence`/`info[]`.

### 7.4 schemas

**(1) Purpose.** Fetch the complete `tools/list` and verify every tool definition: `inputSchema` present, object-typed, root `type:"object"`, valid under its dialect; `outputSchema` valid when present; `x-mcp-header` MUSTs (a conforming client silently drops violating tools); tool-name SHOULDs; opinionated "every tool should publish an `outputSchema`". Never calls a tool. Register S-1…S-19 (§5.4).

**(2) Inputs.** `ctx.tools_list_pages()` (shared; page 0 seeded by handshake), `ctx.capabilities()` (decides skipped-vs-fail on `-32601`), `cfg.strict`. No fresh connection, no SDK session (the SDK cross-check witness of the draft is dropped — no runtime `ClientSession`; `validate_server_result("tools/list", …)` per page is the witness instead). Libraries: `jsonschema>=4.20` (`validator_for`, `check_schema`, `Draft202012Validator`), `referencing.Registry()` (empty: no network `$ref`), `mcp.shared.inbound.find_invalid_x_mcp_header`/`x_mcp_header_map`.

Why raw JSON is mandatory (verified both SDK versions): `mcp_types.Tool.model_validate` raises on `inputSchema: null/true/[]/missing` (`dict_type`/`missing`) and on `outputSchema: true`; `validate_server_result("tools/list","2026-07-28",…)` additionally rejects `inputSchema: {}`/`{"type":"array"}`/`{"type":["object"]}` (`InputSchema` requires literal `type:"object"`); `ClientSession.list_tools()` silently drops invalid-`x-mcp-header` tools (`logger.warning` only). So the SDK either crashes on the servers this check diagnoses or masks the defect.

**(3) Algorithm.** (1) Pages via `ctx.tools_list_pages()` (exact wire as §7.1; page n adds `"cursor"`); byte-identical to what `ClientSession.list_tools(params=PaginatedRequestParams(cursor=…))` sends — verified. (2) Reply classification: `ok` → parse; 200 with JSON-RPC error on a cursor page (`-32602`) → `schemas.pagination_rejected_own_cursor` (should, PG:96-98/113) and stop; `method_not_found` → if `capabilities.tools` declared → fail `schemas.tools_list_unimplemented` (must, TL:62) else inconclusive `Not evaluated: server does not advertise tools`; `modern_rejection`/`auth_required`/`forbidden`/`not_mcp_endpoint`/`server_error`/`transport`/`redirected` → inconclusive; `not_jsonrpc_2xx` → fail `schemas.not_jsonrpc`. (3) Page parse: `result` object, `result.tools` list else `schemas.listing_malformed` (must, SCH:4829); non-object element → `schemas.tool_malformed` for `tool:#<page>.<index>`; `nextCursor` absent/null → done; string (incl. `""`) → continue; other type → `schemas.pagination_malformed` (must, SCH:4826); loop/`MAX_PAGES` → `schemas.pagination_incomplete` (should) and status at most inconclusive if no must findings. (4) Per-tool rules (`subject = "tool:<name>"` or `"tool:#p.i"`):
| # | Rule | Code | Level | Detection |
|---|---|---|---|---|
| R1 | tool object shape | `schemas.tool_malformed` | must | `mcp_types.Tool.model_validate`; keep locs whose first element is not `inputSchema`/`outputSchema` |
| R2 | `inputSchema` present | `schemas.input_schema_missing` | must | TL:293 |
| R3 | `inputSchema` is an object | `schemas.input_schema_not_object` | must | `null`/bool/list/str/number (TL:293) |
| R4 | root `type == "object"` literal | `schemas.input_schema_type` | must | SCH:4911; missing or `["object"]` fail; reference client rejects the whole listing otherwise |
| R5 | dialect + validity | `schemas.input_schema_invalid` (must) / `schemas.dialect_unsupported` (should) / `schemas.dialect_not_2020_12` (should, B:260) / `schemas.schema_unverifiable` (should, B:317-320) | | pipeline below |
| R6 | `outputSchema` | absent/`null` → `schemas.output_schema_missing` (best-practice); bool/list/str/number → `schemas.output_schema_not_object` (must, SCH:4912); dict → same pipeline → `schemas.output_schema_invalid` (must); annotation-only/`{}`/`true` → `schemas.output_schema_permissive` (best-practice). No `type:"object"` requirement (S-7) | | |
| R7 | `x-mcp-header` | `schemas.x_mcp_header_invalid` | must | `find_invalid_x_mcp_header(raw inputSchema)`; verified reason strings e.g. `"property 'amount': x-mcp-header is only permitted on integer/string/boolean properties (got 'number')"`, `"… is not an RFC 9110 token"`, `"… duplicates property 'b'"`, `"… not reachable via a pure \`properties\` chain"`; message appends "conforming Streamable HTTP clients MUST exclude this tool from tools/list (TL:360-366)" |
| R8 | sensitive param mirrored | `schemas.x_mcp_header_sensitive` | should (heuristic) | `SENSITIVE_RE = (pass(word|wd)?|secret|token|api[_-]?key|authorization|credential|ssn|credit[_-]?card)` on path or token from `x_mcp_header_map` |
| R9 | name syntax | `schemas.name_invalid` | should | `^[A-Za-z0-9_.\-]{1,128}$` (TL:311-315) |
| R10 | external `$ref` | `schemas.ref_external` | should | static walk: local iff starts with `#` or document part equals a declared `$id`; never fetched (B:303-312) |
| R11 | icon scheme | `schemas.icon_src_scheme` | best-practice | `urlsplit(src).scheme ∉ {https, data}` (B:481) |
| X | duplicate names (exact, case-sensitive) | `schemas.name_duplicate` | should | TL:316 |
Schema pipeline: `resolve_dialect(s)` → `None` when `$schema` not a string (→ `*_invalid`: `validator_for({"$schema": 42})` raises `AttributeError` — verified); `validator_for(s, default=_UNKNOWN)` → unknown URI → `dialect_unsupported` (validity unasserted); `schema_bounds(s)` iterative depth/node count → beyond `MAX_SCHEMA_DEPTH`/`MAX_SCHEMA_NODES` → `schema_unverifiable` (skip `check_schema`, which raises `RecursionError` at ~128 levels — verified); else `cls.check_schema(s)` → `SchemaError.message`/`json_path` → `*_invalid`. Verified outcomes: `{}`/`true`/`false` pass; `{"type":"nonsense"}` → "'nonsense' is not valid under any of the given schemas" at `$.type`; `{"required":"a"}` → "'a' is not of type 'array'"; draft-07 array `items` valid only under draft-07; `{"$ref":"https://…"}` passes (nothing fetched). `is_trivially_permissive(s)`: `s is True` or dict with no keys outside `{"$schema","$id","$comment","title","description","examples","default","deprecated","readOnly","writeOnly"}`. (5) Witness: `validate_server_result("tools/list","2026-07-28",page_result)` per page; locs under `tools[i].inputSchema|outputSchema` must already be covered by R2–R6, otherwise `schemas.sdk_rejects_listing` (must: a wire-shape violation mcpcheck failed to model); ignore `ttlMs`/`cacheScope`/`resultType` locs (cache/handshake own them). (6) Counts and headline: pool = must findings if any else warnings; group by code; headline = group touching most distinct tools; ties by table order.

**(4) Verdict** per §6.7. `--strict` promotes all warnings (global rule).

**(5) Summaries** (`PHRASE[code]`): pass: `All 12 tools publish valid schemas` / `No tools advertised (nothing to check)`; warn: `3 of 12 tools missing outputSchema` / `2 of 12 tools have non-conforming names (+1 more warning)`; fail: `2 of 12 tools have invalid inputSchema` / `1 of 12 tools dropped by conforming clients (invalid x-mcp-header)` / `4 of 12 tools have schema errors (+3 more issues)`; inconclusive: `Not evaluated: HTTP 401 (pass --token)` / `Listing incomplete (cursor loop after 2 pages)` / `Not evaluated: server does not advertise tools (tools/list → -32601)`. Phrases: input codes → "have invalid inputSchema"; output invalid/not-object → "have invalid outputSchema"; x-mcp-header → "dropped by conforming clients (invalid x-mcp-header)"; `output_schema_missing` → "missing outputSchema"; `output_schema_permissive` → "have a permissive outputSchema"; names → "have non-conforming names"; dialect → "use a non-default schema dialect"; `ref_external` → "reference external $ref"; `schema_unverifiable` → "exceed schema bounds (not validated)"; sensitive → "mirror sensitive parameters into headers"; icons → "have non-https icon URIs"; malformed → "have schema errors".

**(6) Finding mapping.** `subject` = `tool:<name>`; `tool` = name; `evidence` = `{path: "$.tools[4].inputSchema.properties.amount", page, got (≤200 chars), expected, dialect, promoted_by_strict?}`.
```json
{"code":"schemas.x_mcp_header_invalid","level":"must","subject":"tool:execute_sql","tool":"execute_sql",
 "message":"property 'amount': x-mcp-header is only permitted on integer/string/boolean properties (got 'number') — conforming Streamable HTTP clients MUST exclude this tool from tools/list",
 "spec_url":"https://modelcontextprotocol.io/specification/2026-07-28/server/tools#x-mcp-header",
 "evidence":{"path":"$.tools[4].inputSchema.properties.amount","page":0,"got":{"type":"number","x-mcp-header":"Amount"},
             "expected":"x-mcp-header only on integer/string/boolean properties reachable via a pure properties chain; non-empty RFC 9110 token; case-insensitively unique"}}
```

**(7) Edge cases.** Read timeout on page n>1 keeps collected tools (`pagination_incomplete`, inconclusive); page 1 → inconclusive; `outputSchema: null` treated as absent (note "explicit null"); `$schema` null/non-string handled; trailing-`#` dialect URIs resolve (URIDict); `RecursionError` caught per tool; `find_invalid_x_mcp_header` is stack-based and total; determinism: same listing → same records in same order; legacy-shaped result (no hints) still has `tools` and is evaluated; dual-era irrelevant.

**(8) Safety.** `tools/list` POSTs only (≤ `MAX_PAGES`); no `$ref`/icon fetches (B:303-307, B:485); bounded CPU (~0.5 s per 5000-property schema measured).

**(9) Tests** (`tests/test_schemas.py`; pure `evaluate_listing(pages) -> list[Finding]` layer + `rpc_server.make_app(tools=…, pages=…, status=…, loop_cursor=…)` via ASGITransport): `test_pass_reference` (all tools declare outputSchema — fixture requirement), `test_warn_missing_output_schema` (`--no-output-schema` fixture toggle), `test_strict_promotes` (`promoted_by_strict`), `test_empty_list`, `test_input_schema_null/_missing_type/_type_array_literal/_invalid_metaschema` (path `$.tools[0].inputSchema.required`), `test_draft07_declared` (warn only), `test_dialect_unsupported`, `test_dollar_schema_not_string`, `test_output_schema_true/_invalid/_array_ok/_permissive`, `test_xmcp_header_invalid_reason_strings` (guards SDK drift on both matrix cells), `test_xmcp_header_duplicate_ci`, `test_xmcp_header_unreachable`, `test_xmcp_header_sensitive`, `test_name_rules` (`name_invalid` ×4, `name_duplicate` ×2), `test_ref_external_not_fetched` (socket-blocking fixture), `test_depth_cap_no_recursionerror`, `test_icon_scheme`, `test_tool_malformed` (items `tool:#0.0`, `tool:#0.1`, `tool:t`), `test_pagination_three_pages_empty_cursor` (wire: cursors `c1`, `""`), `test_pagination_loop`, `test_pagination_next_cursor_not_string`, `test_pagination_error_on_page_2`, `test_listing_malformed`, `test_401/403/400_protocol/5xx/timeout → inconclusive`, `test_404_skipped_without_capability`, `test_404_fail_with_capability`, `test_sse_response_parsed`, `test_unexpected_content_type → fail not_jsonrpc`, `test_reuses_shared_listing` (zero HTTP), `test_summary_headline_selection`.

**(10) URLs/deviations.** `spec_url` = `SPEC_TOOL`; findings cite `SPEC_TOOLS_CAPABILITIES`, `SPEC_TOOL_NAMES`, `SPEC_X_MCP_HEADER`, `SPEC_OUTPUT_SCHEMA`, `SPEC_SCHEMA_VALIDATION`, `SPEC_SCHEMA_DIALECT`, `SPEC_IMPL_REQUIREMENTS`, `SPEC_REF_RESOLUTION` (`#$ref-resolution`, `$` kept), `SPEC_COMPOSITION_BOUNDS`, `SPEC_ICONS`, `SPEC_SCHEMA_EXTENSION`, `SPEC_PAGINATION_GUIDELINES`, `SPEC_PAGINATION_ERRORS`, `SPEC_CHANGELOG_MINOR`. Deviations: `outputSchema` optional → best-practice warning; `{}` → permissive warning (valid 2020-12), booleans → must (schema types object); raw JSON authoritative; MUSTs added (inputSchema rules, x-mcp-header, structural); SHOULDs added (names, dialect, sensitive headers, external $ref, icons, bounds); three-tier model; pagination; reference fixture gives every tool an `outputSchema`; `validate_server_result` is the witness instead of a live SDK session.

### 7.5 errors

**(1) Purpose.** Verify that a server rejects schema-invalid `tools/call` requests in one of the two conformant shapes (JSON-RPC error, or `CallToolResult` with `isError: true`), and that the error text is something a model could act on — without ever executing a tool with valid input. Register E1–E18 (§5.5).

**(2) Inputs.** `ctx.tools()` (raw, all pages; `None` → inconclusive), `ctx.raw` (`main`, keep-alive, sequential probes), `cfg.probe`, `cfg.max_tools`, `cfg.timeout`. **Raw only** (deviation 15): `ClientSession.call_tool` loses HTTP status (401/403/429/500 → `MCPError(-32603)`), raises `RuntimeError` on `InputRequiredResult`, raises `pydantic.ValidationError` on a result lacking `resultType`, and may raise `RuntimeError` from `validate_tool_result` on an unexpected success — all verified. SDK helpers used: `encode_header_value`, `find_invalid_x_mcp_header`, `x_mcp_header_map`, `mcp_param_headers` (all verified present on 2.2.0/2.3.0), `validate_server_result("tools/call", …)` (surface entry exists — verified).

**(3) Algorithm.** Step 0 — `--probe none` → inconclusive `Skipped: --probe none`; no tools → inconclusive `Server exposes no tools`; listing unavailable → inconclusive. Step 1 — per-tool static plan (`probe_policy.py`, pure):
```python
@dataclass(frozen=True)
class ToolPlan:
    name: str
    index: int
    read_only: bool  # annotations.get("readOnlyHint") is True
    destructive: bool  # annotations.get("destructiveHint") is not False   (schema default true, SCH ToolAnnotations)
    header_map: dict[tuple[str, ...], str]
    candidates: list[tuple[dict[str, Any], str]]  # (arguments, strategy) ordered B > A > C > D
    skip_reason: str | None


def is_probe_eligible(tool: Mapping, policy: Literal["safe", "all"]) -> tuple[bool, str]:
    """safe: readOnlyHint is True or destructiveHint is False (explicit). all: any tool with ≥1 candidate."""


def build_invalid_arguments(input_schema: Any) -> list[tuple[dict, str]]:
    """B missing-required ({} when required non-empty); A wrong-type-required (every required prop wrong-typed);
    C wrong-type-optional; D unexpected-property (only when additionalProperties is literally false).
    Keep a candidate only if validator_for(schema, default=Draft202012Validator)(schema, registry=referencing.Registry()).is_valid(args) is False."""
```
`wrong_value`: `enum`/`const` → sentinel `"mcpcheck-invalid-enum-member-7f3a"`; typed → `{"string":12345,"integer":"mcpcheck-not-an-integer","number":"mcpcheck-not-a-number","boolean":"mcpcheck-not-a-boolean","array":"mcpcheck-not-an-array","object":"mcpcheck-not-an-object","null":"mcpcheck-not-null"}`; type list → first JSON type not listed; untyped/`anyOf`/`$ref` → unsure (`None`). Untestable reasons: inputSchema not an object; `check_schema` fails; unresolvable `$ref`; no surviving candidate ("permissive inputSchema"). Verified: `echo_required` → `({"message":12345,"repeat":"mcpcheck-not-an-integer"}, A)` and `({}, B)`; `{}`/`{"type":"object"}` untestable. Tools whose `find_invalid_x_mcp_header` returns a reason are never called (TL:360-363; schemas reports them). With a non-empty `header_map`, prefer B and drop A/C candidates that assign annotated paths unless `--probe all`; when a candidate does carry annotated values emit `Mcp-Param-*` via `mcp_param_headers` (verified rendering `{"Mcp-Param-Region": "12345"}`).
Step 2 — selection (U2): `safe` → eligible tools only, best candidate; `all` → every tool with ≥1 candidate (one-line stderr warning before probing); truncate to `MAX_TOOLS` in listing order; record every skip reason. Step 3 — one probe per selected tool:
```http
POST {endpoint}
Accept: application/json, text/event-stream
Content-Type: application/json
MCP-Protocol-Version: 2026-07-28
Mcp-Method: tools/call
Mcp-Name: {encode_header_value(tool)}          # Base64 sentinel for non-ASCII or sentinel-looking names (verified: "=?base64?…?=")
Mcp-Param-{Name}: {rendered}                   # only for annotated paths present in args
{"jsonrpc":"2.0","id":"errors:{i}:{tool}","method":"tools/call","params":{"_meta":{…},"name":"{tool}","arguments":{args}}}
```
No `progressToken`, no `logLevel`, `clientCapabilities: {}` (so any `notifications/message` or `inputRequests` is a MUST violation). 429 with `Retry-After ≤ timeout` → sleep and retry once. Step 4 — classify (first match):
| Observation | outcome | code (level) |
|---|---|---|
| transport / read timeout | inconclusive:timeout | — |
| 401 | inconclusive:auth | — (`WWW-Authenticate` in evidence) |
| 403/404/405/4xx without JSON-RPC body | inconclusive | — |
| 5xx without JSON-RPC body | inconclusive | — (intermediary indistinguishable) |
| 2xx with media type ∉ {json, sse} | fail | `errors.bad_content_type` (must, T:87-90) |
| SSE message with `id` and `method` | — | `errors.server_request_on_stream` (must, T:114) |
| SSE `notifications/message` | — | `errors.log_without_loglevel` (must, LG:65-66) |
| SSE `notifications/progress` (no token sent) | — | info |
| no JSON-RPC response | inconclusive | `errors.missing_response` (should) |
| `id` ≠ request id; `id: null` on a 4xx | fail / warn | `errors.id_mismatch` (must; `should` when `id: null` on 4xx — B:105 permits null only for unreadable requests, ours was well-formed) |
| error `code` not int or `message` not non-empty string | fail | `errors.malformed_error` (must, B:106-107) |
| `-32020` | inconclusive:header-mismatch | info (headers sent recorded) |
| `-32021`/`-32022` | inconclusive:protocol | info |
| `-32601` (404) | judged | `errors.tools_call_not_implemented` (best-practice: inferred, server lists tools) |
| `-32002`/`-32042` | judged | `errors.forbidden_error_code` (must, B:139-146) |
| other `-32099..-32020` | judged | `errors.reserved_error_code` (must, B:124-129) |
| `-32019..-32000` | judged | `errors.legacy_error_code` (should, B:119-123) |
| other error (incl. `-32602`, `-32603`) | judged `jsonrpc_error` | message judged (Step 5) |
| result without string `resultType` | judged + | `errors.missing_result_type` (must, B:75) |
| `input_required` with `inputRequests` | fail | `errors.undeclared_input_requests` (must, MRTR:252) |
| `input_required` with neither `inputRequests` nor `requestState` | fail | `errors.empty_input_required` (must, MRTR:250) |
| `input_required` with `requestState` only | inconclusive | info (legal load shedding) |
| `resultType` ∉ {complete, input_required} | fail | `errors.unknown_result_type` (must, B:86) |
| `isError: true` | judged `is_error_result` | text = joined `content[].text`; `structuredContent` presence → info |
| `isError` absent/false | judged `success` | strategy B/enum → `errors.accepted_invalid_input` (must, TL:783-784); A/C/D → `errors.accepted_wrong_type` (should: lenient coercion is a defensible choice) |
Secondary: `validate_server_result("tools/call","2026-07-28",result)` inside `try/except ValidationError` → `errors.malformed_result` (should) for locs not already covered.
Step 5 — actionability (best-practice): A1 `len(m.strip()) >= 20`; A2 not in the generic set `{error, failed, failure, internal, internal error, unknown, unknown error, bad request, invalid, invalid params, invalid request, invalid arguments, something went wrong, an error occurred, exception, server error, tool error, validation error, validation failed}` and not a bare HTTP status `^(http\s*)?[1-5]\d{2}(\s+[a-z ]+)?$`; A3 no stack trace (`traceback \(most recent call last\)`, `^\s*file ".+", line \d+`, `^\s+at \S+ \(.+:\d+:\d+\)`, `\bstack ?trace\b`, leading `^[a-z_][\w.]*(exception|error): `); A4 mentions a `properties` key (normalised, `_`/`-` removed) or the tool name or a schema keyword (`required|expected|must be|should be|missing|invalid value|not a valid|type|integer|string|boolean|number|enum|one of|format|minimum|maximum|unknown tool`); A5 (JSON-RPC errors only, SHOULD E6) single concise sentence else `errors.verbose_error_message` (should). Verified: SDK text `"Error executing tool echo_required: 1 validation error for … repeat Field required …"` passes A1–A4; `"Unknown tool: …"` passes. Step 6 — unknown-tool probe (always, 1 request): `name = UNKNOWN_TOOL_NAME`, `arguments: {}`; JSON-RPC error or `isError:true` accepted; a non-error result → `errors.unknown_tool_executed` (best-practice: TL:734-751 is descriptive). Step 7 — `ratio = actionable / judged` over error shapes; `ratio < 0.8` → `errors.non_actionable_messages` (best-practice) plus one `errors.non_actionable_message` (best-practice) per failing tool with `heuristics_failed`.

**(4) Verdict** per §6.7: any must → fail; ratio < 0.8 or other warnings → warn; all selected probes inconclusive → inconclusive (`Inconclusive: HTTP 401 on tools/call (check --token)` when any auth); zero eligible tools → inconclusive `No probe-safe tools (12 listed; annotate readOnlyHint/destructiveHint or use --probe all)`.

**(5) Summaries.** pass: `Tool errors carry actionable messages`; fail: `1 tool(s) executed on schema-invalid input` / `Forbidden error code -32042 from \`err_rpc_32042\`` / `\`tools/call\` returned text/plain (not JSON-RPC)` / `\`X\` result lacks resultType` / `\`X\` requested undeclared client input`; warn: `2 of 4 probed tools return non-actionable errors` / `Tool errors actionable; 1 warning (legacy_error_code)`; inconclusive as above.

**(6) Finding mapping.** `subject` = tool name or `"(unknown tool)"`; `tool` = name; `evidence.probe = {strategy, arguments, request_id, http_status, media_type, shape, error_code, heuristics_failed}`, `evidence.got` (≤2000 chars), `evidence.expected`.
```json
{"code":"errors.accepted_invalid_input","level":"must","subject":"export_report","tool":"export_report",
 "message":"Tool returned a non-error result for arguments that violate its inputSchema (all required properties omitted)",
 "spec_url":"https://modelcontextprotocol.io/specification/2026-07-28/server/tools#security-considerations",
 "evidence":{"probe":{"strategy":"missing-required","arguments":{},"request_id":"errors:5:export_report","http_status":200,"media_type":"application/json","shape":"success","error_code":null,"heuristics_failed":[]},
             "got":{"resultType":"complete","isError":false,"content":[{"type":"text","text":"Report queued"}]},
             "expected":"JSON-RPC error (e.g. -32602) or CallToolResult with isError:true — servers MUST validate all tool inputs"}}
```

**(7) Edge cases.** Per-probe total budget `anyio.fail_after(timeout)` via `RawClient` (never `client.post` buffering); worst case `MAX_TOOLS × timeout` + 1; 4xx with JSON-RPC body treated as JSON-RPC error; duplicate names → first occurrence; permissive schemas listed as untestable even under `all`; dual-era irrelevant; legacy-shaped reply (no `resultType`) → `missing_result_type`; `-32020` → inconclusive with headers sent (header contract subtleties may be ours); 1 MiB body cap on probes (`MAX_BODY_BYTES` applies; message truncation 2000/160); non-ASCII names always Base64 (raw non-ASCII header makes httpx2 raise `UnicodeEncodeError` — verified); deterministic payloads.

**(8) Safety.** Only payloads proven invalid by `jsonschema` (empty `referencing.Registry()`); default calls only explicitly annotated read-only/non-destructive tools (annotations untrusted, TL:304-307, so even they get invalid input only); unannotated/destructive tools are listed as skipped with the `--probe all` hint; `--probe all` prints a warning; no code path constructs valid arguments. Residual risk (README): a non-conformant server that ignores its own schema could execute an annotated tool on garbage input — exactly the `accepted_invalid_input` finding.

**(9) Tests** (`tests/test_errors.py`; F1 reference server with `lookup_weather`/`echo_required` both `readOnlyHint: true`; F2 `error_shapes_server` where the tool name selects the reply: `err_good`, `err_rpc_32602_200/_400`, `err_rpc_nullid_400`, `err_rpc_32000/32042/32050/32603`, `err_generic`, `err_traceback`, `err_empty_content`, `err_no_resulttype`, `err_structured`, `ok_garbage`, `input_required_state/_requests/_empty`, `http_500_html`, `http_403_text`, `ctype_text`, `slow`, `sse_iserror`, `sse_notification_message`, `hdr_tool`, `destructive_norequired`, `readonly_permissive`, `工具_unicode`, `wrongtype_ok`; toggles `--auth-required`, `--unknown-tool-succeeds`, `--no-tools`, request log). Table: `test_reference_server_passes` (2 tools + unknown probe), `test_json_response_mode_passes`, `test_both_error_shapes_judged_actionable` (http_status 200/200/400), `test_null_id_on_400_is_should`, `test_unexpected_success_missing_required_fails`, `test_unexpected_success_wrong_type_warns`, `test_generic_message_ratio_warns` (`2 of 4 …`; heuristics A1/A2/A3), `test_empty_content_non_actionable`, `test_forbidden_code_32042_fails`, `test_reserved_code_32050_fails`, `test_legacy_code_32000_warns`, `test_internal_error_not_actionable`, `test_missing_resulttype_fails`, `test_structured_content_is_info_only`, `test_input_required_state_inconclusive`, `test_input_required_with_requests_fails`, `test_input_required_empty_fails`, `test_401_inconclusive`, `test_500_html_inconclusive`, `test_bad_content_type_fails`, `test_sse_iserror_parsed`, `test_sse_log_without_loglevel_fails`, `test_timeout_inconclusive` (`--timeout 1`, wall < 3 s), `test_safe_policy_never_calls_unannotated` (`destructive_norequired` **never requested** under `safe`, listed as skipped), `test_probe_all_includes_destructive` (payload `{"q": 12345}`), `test_permissive_schema_untestable`, `test_unicode_name_base64_mcp_name`, `test_x_mcp_header_prefers_missing_required` (no `Mcp-Param-*`), `test_x_mcp_header_all_mode_emits_param_header`, `test_unknown_tool_probe_iserror_shape`, `test_unknown_tool_executed_warns`, `test_max_tools_truncates` (exactly N+1 POSTs), `test_no_tools_inconclusive`, `test_probe_none` (zero requests); pure: `test_build_invalid_arguments[16 schemas]` (every payload `not is_valid`), `test_judge_message[...]`, `test_encode_header_value_sentinel` (regression guard; verified today).

**(10) URLs/deviations.** `spec_url` = `SPEC_TOOL_ERRORS`; findings cite `SPEC_TOOLS_SECURITY`, `SPEC_ERROR_RESPONSES`, `SPEC_ERROR_CODES`, `SPEC_RESULT_RESPONSES`, `SPEC_SENDING_MESSAGES`, `SPEC_RECEIVING_MESSAGES`, `SPEC_STANDARD_HEADERS`, `SPEC_VALUE_ENCODING`, `SPEC_PARAM_HEADERS`, `SPEC_MRTR_SERVER_REQS`, `SPEC_LOGGING`. Deviations: both error shapes accepted; `structuredContent` info-only; heuristics labelled best-practice and ratio → warn; probe policy (U2) with no `{}`-on-unannotated-tools default; MUST violations fail regardless of ratio; raw client; `Mcp-Name`/`Mcp-Param-*` handled; `InputRequiredResult`, unknown tool, 401 handled; bounded by `--max-tools`. The former UNVERIFIED items (`encode_header_value` sentinel encoding; `tools/call` validator entry) are now verified — no fallback encoder, no `KeyError` guard.

### 7.6 cache

**(1) Purpose.** Verify every page of `tools/list`, `prompts/list`, `resources/list`, `resources/templates/list` carries result-level `ttlMs` (JSON integer ≥ 0) and `cacheScope` (exactly `"public"|"private"`), same scope on all pages, plus a heuristic warning for `"public"` lists that look user-specific. `server/discover` hints are validated by the discover check through the shared pure function below; `resources/read` is not probed (read-only). Register CA-1…B-1 (§5.6).

**(2) Inputs.** `ctx.*_list_pages()` (shared — the cache check sends **no requests of its own** when another check already fetched them; with `--check cache` alone the context fetches 4 methods on `main`), `cfg.token` (heuristic gate), `ctx.capabilities()` (details only), `ctx.era` (details line). No SDK session: `ClientSession.send_request` clamps negative `ttlMs` to 0 (`_clamp_inbound_ttl`), strips hints pre-negotiation, raises `ValidationError` post-adopt, loses HTTP status — verified. Constants `ITEM_KEY = {"tools/list":"tools","prompts/list":"prompts","resources/list":"resources","resources/templates/list":"resourceTemplates"}`, `USER_TOKENS = {my, me, user, account, profile, self}`.

**(3) Algorithm.** For each method: pages from context (classification §6.6: `method_not_found` → "not implemented" skip; `-32022` → inconclusive whole check; other modern rejection / 403-with-body / other JSON-RPC error / 5xx / not-JSON / id mismatch → `cache.unevaluated` (should) for that method; 401/403-no-body/network on the first method → inconclusive). Pure validator shared with discover:
```python
def validate_cache_hints(method: str, page: int, result: Mapping[str, Any], *, token_supplied: bool,
                         code_prefix: str = "cache") -> tuple[str | None, list[Finding]]:
    """Pure; shared with discover, which passes code_prefix="discover" so codes keep the <check>.<slug> grammar."""
    subject = method if page == 0 else f"{method} page {page}"
    if result.get("resultType") == "input_required": return "input_required", []       # CA:25-27 (the must finding is the caller's)
    # absent resultType => complete (B:87)
    ...  # ttlMs: missing → cache.ttl_missing (must, CA-1); type(v) is not int:
         #        bool/str/None/fractional float → cache.ttl_not_integer (must, CA:45 + SCH number→int interop);
         #        integral float (60000.0) → cache.ttl_integral_float (best-practice);
         #        v < 0 → cache.ttl_negative (must, CA:62); v > TTL_SANITY_MS → cache.ttl_implausible (best-practice)
         # cacheScope: missing → cache.scope_missing (must); not in ("public","private") → cache.scope_invalid (must, CA:46)
         # heuristic: scope == "public" and token_supplied and any item name/uri/uriTemplate tokenises to USER_TOKENS → cache.public_user_specific_names (best-practice, CA:107-110/175-181)
def looks_user_specific(name: str) -> bool:   # camelCase split then split on non-alphanumerics; "list_my_orders", "listMyOrders", "user_profile" match; "resumeUpload" does not
```
Pagination: scope differs from page 0 → `cache.scope_inconsistent_pages` (must, CA:168-171) once per method; `ttlMs` differences logged only (CA:157-158); `nextCursor` non-string → `cache.cursor_not_string` (must, SCH); repeated cursor → `cache.cursor_loop` (should). Witness: `validate_server_result(method,"2026-07-28",result)` locs appended to details (it accepts `"5"`, `5.0`, `True` — verified — so it never drives verdicts). Anonymous token-less probe: **dropped for 0.1.0** (decided by default, §2); listed in §15.

**(4) Verdict** per §6.7; `cache.unevaluated` makes the check `warn`; all methods `-32601` → inconclusive `Skipped: no list method implemented`.

**(5) Summaries.** pass: `Cache hints valid on 4 list methods (6 pages)`; warn: `Cache hints valid; 1 warning: tools/list cacheScope public but item names look user-specific`; fail: `4 cache-hint violations: tools/list ttlMs missing; prompts/list ttlMs missing (+2 more)`; inconclusive: `Not evaluated: HTTP 401 on tools/list (pass --token)` / `Not evaluated: server does not support 2026-07-28 (supports: 2025-11-25)` / `Skipped: no list method implemented (all four answered -32601)`.

**(6) Finding mapping.** `subject` = `"tools/list"` or `"tools/list page 1"`; `evidence` = `{method, page, got, expected, pages_scopes?}`.
```json
{"code":"cache.scope_inconsistent_pages","level":"must","subject":"tools/list page 1","message":"cacheScope differs across pages of one list request",
 "spec_url":"https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/caching#interaction-with-pagination",
 "evidence":{"method":"tools/list","page":1,"got":{"page0":"public","page1":"private"},"expected":"same cacheScope on every page"}}
```

**(7) Edge cases.** Legacy-shaped 2025 result (no hints) → fail (genuine MUST) with details line "pre-2026 result shape; see handshake"; empty lists still need hints; `ttlMs: 0` legal (note "all ttlMs are 0"); `1e3` parses as float → not-integer; `10**20` → implausible warning (covers JS 2^53); `input_required` on a list → hints not required here (handshake/discover report the must); dual-era irrelevant; SDK `MCPServer` without `cache_hints` emits `ttlMs:0 cacheScope:"private"` and passes (verified) — README must not assume non-zero TTLs from an arbitrary server.

**(8) Safety.** List methods only, via the shared cache; zero extra requests in a full run; `resources/read` excluded and disclosed.

**(9) Tests** (`tests/test_cache.py`; `rogue_server.make_app(variants)` via `httpx2.ASGITransport` — verified for JSON/SSE/`no-ttl`): `test_pure_validate_matrix` (ok / missing / null / -1 / 5.5 / 5.0 / "5" / True / 10**12 / scope user / PUBLIC / missing), `test_pure_user_specific_tokeniser`, `test_reference_server_passes` (`… 4 list methods (4 pages)`), `test_sdk_server_without_cache_hints_passes`, `test_missing_ttl_fails`, `test_negative_ttl_fails`, `test_float_ttl_fails`, `test_integral_float_ttl_warns`, `test_string_bool_null_ttl_fail`, `test_huge_ttl_warns`, `test_missing_scope_fails`, `test_scope_user_fails`, `test_scope_case_sensitive`, `test_absent_resulttype_still_requires_hints`, `test_input_required_skips_hints`, `test_paginated_all_pages_checked` (`(6 pages)`), `test_page2_missing_hints_fails`, `test_page2_scope_differs_fails_once`, `test_cursor_loop_warns` (exactly 2 requests), `test_empty_cursor_continues`, `test_public_user_names_warn_only_with_token`, `test_private_scope_no_heuristic`, `test_method_not_implemented_skipped`, `test_other_jsonrpc_error_unevaluated`, `test_401_without_token_inconclusive`, `test_html_500_inconclusive`, `test_id_mismatch_unevaluated`, `test_unsupported_version_inconclusive`, `test_sse_response_parsed`, `test_timeout_inconclusive`, `test_all_unimplemented_inconclusive`, `test_uses_shared_listing_zero_requests`, `test_json_record_shape`.

**(10) URLs/deviations.** `spec_url` = `SPEC_CACHEABLE_RESULTS`; findings cite `SPEC_TTL`, `SPEC_CACHE_SCOPE`, `SPEC_CACHE_PAGINATION`, `SPEC_CACHE_SECURITY`, `SPEC_PAGINATION_GUIDELINES`, `SPEC_RESULT_RESPONSES`. Deviations: result-level not per-item; no `"user"` scope; no 20 % tolerance; four methods + discover (via shared validator), `resources/read` excluded; "public but user-specific" is a best-practice warning; type strictness added (integral float downgraded to warning for cross-check consistency); pagination added; sanity warning added; five-state status; raw client; no HTTP cache headers; shared list cache (no own fetches); anonymous probe dropped.

### 7.7 origin

**(1) Purpose.** Prove the deployed endpoint refuses requests whose `Origin` names a foreign web origin (DNS rebinding / CSRF). A fail here is a real vulnerability. Register N1–N9 (§5.7).

**(2) Inputs.** `ctx.fresh(f"origin-{probe}")` per probe (O7: "all incoming connections" read as every TCP connection; verified three ways to get distinct peer ports), `cfg.origin`, `cfg.token`, `wire.derive_origin(url)` (`httpx2.URL(url).scheme + "://" + netloc`; verified: `https://api.example.com:443/mcp → https://api.example.com`, `http://[::1]:9000/mcp → http://[::1]:9000`, uppercase host lowered; do not use `httpx2.URL.origin`). Raw only: `ClientMessageMetadata(headers=)` is not forwarded by the SDK and 403 collapses to `MCPError(-32603)`.

**(3) Algorithm.** Body for P1–P4 = `server/discover` envelope (always implemented, DI:9-11, never 404 on tool-less servers). Common headers as §7.3 plus `connection: close`. Probes, sequential, each on its own connection:
| Step | id | Extra header | Inspected |
|---|---|---|---|
| P1 | `origin:evil` | `origin: https://evil.example.com` | status; `www-authenticate`; media type; JSON-RPC messages; body ≤200 chars |
| P2 | `origin:same` | `origin: <--origin or derive_origin(url)>` | same |
| P3 | `origin:none` | none (control) | same |
| P4 | `origin:null` | `origin: null` | same |
| P5 | `origin:preflight` (info) | `OPTIONS` with `origin: https://evil.example.com`, `access-control-request-method: POST`, `access-control-request-headers: content-type,mcp-method,mcp-protocol-version[,authorization]` | status, `access-control-allow-origin`, `access-control-allow-credentials`, `allow` |
`classify(reply) -> Probe(outcome ∈ {RESULT, RPC_ERROR, HTTP_REJECT, NO_RESULT_2XX, REDIRECT, TRANSPORT}, status, rpc_code, rpc_has_id, media_type, www_authenticate, location, body_excerpt)` (`class Outcome(str, Enum)` — not `StrEnum`). Control = first of (P3, P2) with `RESULT`; if none: P3 401, or 403 with `insufficient_scope`, or 403-no-body on both P3 and P2 → baseline AUTH (N8); P3 `RPC_ERROR` not 401/403 (e.g. legacy-only 404/-32601) → RPC_LAYER; else DOWN. `processed_like_control(p1) := outcome, status, rpc_code identical`.

**(4) Verdict rules.**
| Probe observation | Code | Level | Spec |
|---|---|---|---|
| P1 403 (any body) | pass; 403 body is a JSON-RPC error **with** an id → `origin.evil_403_body_has_id` info | — | T:59-61 |
| P1 `RESULT` (JSON or SSE), any 2xx | `origin.evil_accepted` | must | T:57-61 |
| P1 identical to control (baseline RPC_LAYER) | `origin.not_validated` ("identical reply with and without the evil Origin") | must | T:57-58 |
| P1 rejected with status ≠ 403 (400/401/404/5xx, or 2xx + JSON-RPC error) | `origin.evil_rejected_wrong_status` (message: security property held; status MUST be 403) | **must** (U10) | T:59-61 |
| P1 `NO_RESULT_2XX` / `REDIRECT` / `TRANSPORT` while control succeeded | `origin.evil_rejection_unconfirmed` / `origin.evil_redirected` / `origin.evil_connection_closed|timeout` | should | T:59-61 |
| P2 rejected while control succeeded | `origin.same_origin_rejected` (hint `--origin <allowed>`) | best-practice (spec silent) | — |
| P3 rejected while P2 succeeded | `origin.no_origin_rejected` (non-browser clients incl. the SDK send none) | best-practice (spec silent) | — |
| P4 `RESULT` or identical to control | `origin.null_accepted` (opaque origin — WHATWG Fetch, external, UNVERIFIED in a browser today) | best-practice | — |
| P5 ACAO = evil or `*` | `origin.cors_preflight_permissive` | info | — |
| no control, baseline AUTH/DOWN | inconclusive | — | N8 |
Status per §6.7. Localhost targets are not exempt (N3/N9); SDK footgun: `MCPServer.streamable_http_app(host="0.0.0.0")` disables auto Origin validation even when bound to 127.0.0.1 (verified both versions) → correct fail.

**(5) Summaries.** pass: `Origin header validated`; `evil_accepted`: `Evil Origin accepted: https://evil.example.com got HTTP 200 + result (expected 403)`; `not_validated`: `Origin not validated: identical reply with and without Origin https://evil.example.com`; `evil_rejected_wrong_status`: `Evil Origin rejected with HTTP 400 (spec requires 403)`; warnings: `Evil Origin rejected (403); same-origin http://127.0.0.1:8080 also rejected (403)` / `…; requests without Origin rejected (403)` / `…; "Origin: null" accepted`; inconclusive: `Not evaluated: HTTP 401 on every probe (pass --token)` / `Not evaluated: no baseline response (ConnectError: …)`.

**(6) Finding mapping.** `subject` = probe id; `evidence` = `{request: {method, origin, fresh_connection: true}, http_status, media_type, rpc_code, got, expected, body_excerpt}`.
```json
{"code":"origin.evil_accepted","level":"must","subject":"origin:evil",
 "message":"The server processed a request carrying a foreign Origin; a page on https://evil.example.com, or any hostname DNS-rebound to this server, can drive it from a victim's browser",
 "spec_url":"https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http#security-&-endpoint",
 "evidence":{"request":{"method":"server/discover","origin":"https://evil.example.com","fresh_connection":true},"http_status":200,"media_type":"application/json","got":"JSON-RPC result for server/discover","expected":"HTTP 403 Forbidden (body optional: JSON-RPC error without id)"}}
```

**(7) Edge cases.** SDK 403 body is text `Invalid Origin header` with **no** content-type (verified) — classify on status + JSON-RPC presence; WAF 403 pages are HTML; 429 on control → inconclusive; redirects never followed by probes (a followed 302 would convert to GET and carry Origin — verified); 3xx on control → inconclusive; proxies that strip `Origin` make P1 a `RESULT` → fail (correct: the deployed endpoint accepts cross-origin); empty `Origin:` not probed (SDK treats as absent, not browser-producible); `--origin` replaces P2's value verbatim.

**(8) Safety.** `server/discover` ×4 + `OPTIONS` ×1, all read-only; 5 connections; the SDK logs `Invalid Origin header: https://evil.example.com` at WARNING per probe (README note); RFC 2606 name implicates no third party.

**(9) Tests** (`tests/test_origin.py`; SDK fixture flags `--no-origin-check`, `--require-token TOKEN`, `--app-host 0.0.0.0`, `--json-response`, `--stateful`; `misbehaving_server` `origin_mode` ∈ {`reject_400_jsonrpc`, `reject_200_jsonrpc_error`, `allowlist_app_origin`, `require_origin`, `accept_null`, `accept_all_sse`, `legacy_only`, `redirect_evil`, `reset_evil`, `hang_evil`, `cors_reflect`}): `test_pass_sdk_default` (✔ evil/null→403, same/none→200), `test_pass_json_response`, `test_pass_with_token`, `test_inconclusive_401`, `test_fail_sdk_disabled` ✔, `test_fail_sdk_host_0000` ✔, `test_fail_wrong_status_400_jsonrpc` (must), `test_fail_200_jsonrpc_error`, `test_warn_same_origin_rejected` (+ `--origin https://app.example.com` → pass), `test_warn_no_origin_rejected`, `test_warn_null_accepted`, `test_fail_sse_result`, `test_fail_legacy_only_processed`, `test_warn_redirect` (no GET recorded), `test_warn_connection_closed`, `test_warn_timeout` (wall < 2 s), `test_inconclusive_control_down`, `test_info_cors_reflect`, `test_fresh_connections` (4 distinct peer ports), `test_derive_origin` (table), `test_classify_matrix` (recorded `RawReply`s), `test_sdk_matrix[220,230]`.

**(10) URLs/deviations.** `spec_url` = `SPEC_TRANSPORT` (fragment-free for README; JSON findings may carry `SPEC_ORIGIN_FRAGMENT` = `…#security-&-endpoint`, both resolve — verified; `#security-endpoint` does not exist); also `SPEC_SECURITY_LOCAL`, `SPEC_AUTH_ERRORS`, `SPEC_DISCOVER`. Deviations: probe method `server/discover`; MUST 403 enforced (non-403 rejection fails); same-origin/no-origin have no spec basis → best-practice; `null` and `OPTIONS` added; inconclusive; fresh connection per probe; baseline comparison; probes never follow redirects; SDK not used; localhost not exempt.

### 7.8 Proposed: `protocol` check (pending U1)

**(1) Purpose.** Verify the header/version/method MUSTs of 2026-07-28 that a read-only client can provoke without executing anything. Register P1–P10 (§5.8). Each probe is a `tools/list` (or `server/discover` on tool-less servers) with one deliberate defect, on a fresh connection, classified with §6.6.

**(2) Inputs.** `ctx.fresh(f"protocol-{n}")`; `ctx.tools()` (for an optional `Mcp-Param` omission probe on a `safe`-eligible tool with an `x-mcp-header` property — only when `--probe` ≠ `none`); `cfg.legacy_probes` (GET probe).

**(3) Probes and verdicts.**
| # | Probe (deviation from the normal request) | Expected | Code if not | Level | Spec |
|---|---|---|---|---|---|
| 1 | header `MCP-Protocol-Version: 2025-11-25`, `_meta` `2026-07-28` | 400 + `-32020` | `protocol.header_mismatch_accepted` (200) / `protocol.header_mismatch_wrong_status` (other) | must | T:257-261, T:583-602 |
| 2 | omit `MCP-Protocol-Version` (`_meta` present) | 400 + `-32020`; 200 legacy-shaped is permitted only for pre-2025-06-18 support | `protocol.headerless_accepted` (200) | should (T:277-281 MAY) | T:277-281 |
| 3 | header and `_meta` both `1900-01-01` | 400 + `-32022` with `data.supported ∋ 2026-07-28`, `data.requested == "1900-01-01"` | `protocol.unsupported_version_not_rejected` / `protocol.unsupported_data_missing` / `protocol.unsupported_wrong_status` | must | T:263-269, SCH:706-716 |
| 4 | omit `Mcp-Method` | 400 + `-32020` | `protocol.missing_mcp_method_accepted` | must | T:287-294, T:621-624 |
| 5 | `Mcp-Method: Tools/List` (value case) | 400 + `-32020` | `protocol.method_header_case_accepted` | must | T:575-577, T:583-585 |
| 6 | `_meta` without `io.modelcontextprotocol/clientCapabilities` | 400 + `-32602` | `protocol.missing_meta_accepted` / `protocol.missing_meta_wrong_code|status` | must | B:382-384 |
| 7 | method `mcpcheck/nonexistent` (`Mcp-Method` matching) | 404 + `-32601` | `protocol.unknown_method_wrong_status` (200/400) / `protocol.unknown_method_not_jsonrpc` | must | T:271-275 |
| 8 | `tools/list` with `cursor: "mcpcheck-invalid-cursor-7f3a"` | `-32602` (or a normal page: servers ignoring cursors are not violating a MUST) | `protocol.invalid_cursor_not_rejected` | should | PG:113 |
| 9 | (optional, `--probe` ≠ none, eligible tool with `x-mcp-header`) `tools/call` with the body value present and the `Mcp-Param-*` header omitted, arguments otherwise proven invalid | 400 + `-32020` | `protocol.param_header_omission_accepted` | must | T:553-567 |
| 10 | (`--legacy-probes` only) headers-only `GET` with `MCP-Protocol-Version: 2026-07-28` | 405 `Allow: POST` (modern-only servers SHOULD) | `protocol.legacy_get_hosted` | info (dual-era MAY host it) | T:678-679 |
| any | response carries `-32002`/`-32042` or undefined `-3202x..-32099` | — | `protocol.forbidden_error_code` | must | B:124-146 |
| any SSE | missing `X-Accel-Buffering: no` | — | `protocol.no_accel_buffering_header` | should | T:133-134 |
Verified against the SDK fixture: mismatch → 400 `-32020`; `2099-01-01` → 400 `-32022` with `data.supported/requested`; GET with header → 405 `Allow: POST`; header-less GET opens an endless legacy SSE stream (hence headers-only read under `anyio.fail_after(min(timeout, 5))`).

**(4)–(6)** Status per §6.7; summaries: pass `Header validation and version errors conform`; fail `Version/header mismatch accepted (MCP-Protocol-Version 2025-11-25 vs _meta 2026-07-28 → HTTP 200)` etc.; `subject` = probe label, `evidence` = `{sent_headers_diff, http_status, rpc_code, data}`.

**(7)–(8)** All probes are list/discover requests or malformed requests that cannot dispatch; 8–10 fresh connections; runs after `discover`, before `schemas`. Intermediaries may answer 400 without JSON-RPC body (T:631-635) → for probes expecting 400, accept a bare 400 as pass with an info note.

**(9) Tests** (`tests/test_protocol.py`; `misbehaving_server` toggles `ignore_version_mismatch`, `serve_headerless_as_legacy`, `unknown_method_status=200`, `legacy_error_codes`, `accept_missing_meta`, `ignore_cursor`, `x_accel_buffering=False`): one pass test on the reference server (both response modes), one fail/warn test per row, `test_param_header_omission` with `hdr_tool`, `test_legacy_get_info` under `--legacy-probes`.

**(10) URLs.** `spec_url` = `SPEC_PROTOCOL_VERSION_HEADER`; also `SPEC_STANDARD_HEADERS`, `SPEC_SERVER_VALIDATION`, `SPEC_META`, `SPEC_ERROR_CODES`, `SPEC_PAGINATION_ERRORS`, `SPEC_PARAM_HEADERS`, `SPEC_EARLIER_REVISIONS`. If U1 is rejected: delete `checks/protocol.py`, remove `"protocol"` from `CheckName`/`NAMES`/schema enum/README/CHANGELOG ("seven"), keep the fixture toggles; the `-32022`/`404+-32601` observations remain covered by handshake/discover.

---

## 8. CLI and output contract

### 8.1 Command signature and frozen option set (0.1.0)
`mcpcheck [OPTIONS] SERVER_URL` — one click command; `python -m mcpcheck` via `if __name__ == "__main__": main(prog_name="mcpcheck")`. All validation failures exit 2 before any network I/O.

| Option | Type / default | Env | Validation / behaviour | Source |
|---|---|---|---|---|
| `SERVER_URL` | positional | — | absolute `http://`/`https://` with host; no userinfo, no `#fragment`; path/query kept verbatim; schemeless `localhost:8000/mcp` rejected (urlsplit parses scheme `localhost` — verified) | handoff |
| `--token TEXT` | str | `MCPCHECK_TOKEN` (`show_envvar`) | `Authorization: Bearer <TEXT>`; never echoed | handoff (+env) |
| `-H, --header KEY:VALUE` | repeatable | — | split on first `:`; values redacted, names only in JSON | addition |
| `--origin TEXT` | none | — | `scheme://host[:port]` without path; sent on every request when given (U8) | handoff (default changed) |
| `--timeout SECONDS` | `FloatRange(min=0, min_open=True)`, 10.0 | — | httpx2 `Timeout(t)` and total per-request budget | handoff |
| `-c, --check NAME` | repeatable + comma-separated, case-insensitive | — | names from `NAMES`; unknown → `BadParameter`; order never changes | handoff (+comma) |
| `--skip NAME` | same | — | both lists naming X → error; nothing left → error | handoff |
| `--probe [safe\|all\|none]` | `safe` | — | §7.5 policy (U2) | addition |
| `--max-tools N` | `IntRange(min=1)`, 20 | — | `errors` probe cap | addition |
| `--legacy-probes` | flag | — | handshake `initialize` probe + protocol GET probe (U4) | addition |
| `--strict` | flag | — | warn and inconclusive → exit 1 | addition (GROUND_TRUTH 5) |
| `--json` | flag | — | stdout = one JSON document only | handoff |
| `-o, --output FILE` | `Path(dir_okay=False, writable=True, allow_dash=True)` | — | JSON to FILE **and** human report on stdout; `-` implies `--json` | addition |
| `--json-schema` | eager | — | prints shipped schema, exits 0, no URL needed (verified eager callback pattern) | addition |
| `--list-checks` | eager | — | `name<pad>title` lines | addition |
| `-v, --verbose` | count | — | `-v` one line per exchange on stderr; `-vv` headers+bodies (redacted), SDK `mcp` logger at WARNING | handoff (+`-vv`) |
| `--no-color` | flag | `NO_COLOR` | `color_system=None` (rich `no_color=True` keeps bold — verified) | addition |
| `--insecure` | flag | — | `verify=False`; stderr warning; `request.tls_verify=false` | addition |
| `-V, --version` | `version_option(package_name="mcpcheck", message="%(prog)s %(version)s")` | — | prints exactly `mcpcheck 0.1.0`; raises `RuntimeError` from a bare checkout — always test the editable install | handoff |
| `-h, --help` | — | — | — | handoff (+`-h`) |
Dropped from the drafts: `--ascii` (encoding-based fallback instead), `--print-schema` (→ `--json-schema`), `--check-timeout` (constant 10×), all per-check knobs (`--connections`, `--no-session-probe`, `--no-legacy-get`, `--probe-delete`, `--max-pages`, `--max-response-bytes`, `--probe annotated`, `--max-probe-tools`, `--probe-tool`, `--no-anon-probe`, `--max-body`) → constants (§6.4).

Decorator skeleton:
```python
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
    help="Which tools the errors check may call with invalid arguments: 'safe' = only tools annotated readOnlyHint=true or destructiveHint=false, 'all' = every tool, 'none' = no calls.",
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
    package_name="mcpcheck",
    prog_name="mcpcheck",
    message="%(prog)s %(version)s",
)
def main(
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
) -> None: ...
```
Help text (click wraps at 80 columns; the snapshot test pins `terminal_width=80`; regenerate from the final decorator — TO-FILL-FROM-REAL-RUN):
```
Usage: mcpcheck [OPTIONS] SERVER_URL

  Check an MCP server for conformance to the MCP 2026-07-28 revision over
  Streamable HTTP.

  SERVER_URL is the full URL of the MCP endpoint, e.g.
  https://api.example.com/mcp.

  mcpcheck is read-only: it sends server/discover, the */list methods,
  tools/call with deliberately invalid arguments to annotated read-only tools,
  header probes that cannot execute anything, and one OPTIONS request. Exit
  status is 0 when every selected check passes, 1 when a check fails, and 2
  when mcpcheck could not run (usage error, network failure, crash).

Options:
  --token TEXT             Bearer token sent as 'Authorization: Bearer
                           <TEXT>'.  [env var: MCPCHECK_TOKEN]
  -H, --header KEY:VALUE   Extra request header; repeatable.
  --origin TEXT            Origin header to send on every request (default:
                           none).
  --timeout SECONDS        Per-request timeout.  [default: 10.0; x>0]
  -c, --check NAME         Run only these checks; repeatable or comma-
                           separated. Names: handshake, session, discover,
                           protocol, schemas, errors, cache, origin.
  --skip NAME              Skip these checks; repeatable or comma-separated.
  --probe [safe|all|none]  Which tools the errors check may call with invalid
                           arguments: 'safe' = only tools annotated
                           readOnlyHint=true or destructiveHint=false, 'all' =
                           every tool, 'none' = no calls.  [default: safe]
  --max-tools N            Maximum number of tools the errors check probes.
                           [default: 20; x>=1]
  --legacy-probes          Also send a legacy 'initialize' request and a
                           headers-only GET to classify dual-era servers.
  --strict                 Treat warnings and inconclusive checks as failures
                           (exit code 1).
  --json                   Print the JSON report to stdout instead of the
                           human report.
  -o, --output FILE        Write the JSON report to FILE as well ('-' =
                           stdout, implies --json).
  --json-schema            Print the JSON Schema of the report and exit.
  --list-checks            List the available checks and exit.
  -v, --verbose            Show every HTTP exchange on stderr; -vv adds
                           headers and bodies (secrets redacted).
  --no-color               Disable colored output (NO_COLOR=1 does the same).
  --insecure               Do not verify the server's TLS certificate.
  -V, --version            Show the version and exit.
  -h, --help               Show this message and exit.
```

### 8.2 Usage errors (exit 2; verified texts for the click-generated ones)
| Input | `Error:` line |
|---|---|
| `mcpcheck` | `Missing argument 'SERVER_URL'.` |
| `localhost:8000/mcp`, `ftp://h/mcp`, `http://` | `Invalid value for 'SERVER_URL': must be an absolute http:// or https:// URL, e.g. https://api.example.com/mcp` |
| `http://u:p@h/mcp` | `Invalid value for 'SERVER_URL': must not contain credentials; use --token or --header instead` |
| `http://h/mcp#x` | `Invalid value for 'SERVER_URL': must not contain a #fragment` |
| `-c origin,sesion` | `Invalid value for '-c' / '--check': 'sesion' is not one of 'handshake', 'session', 'discover', 'protocol', 'schemas', 'errors', 'cache', 'origin'.` |
| `-c origin --skip origin` | `--check and --skip both name 'origin'.` |
| `--skip` of every name | `No checks left to run after applying --skip.` |
| `--origin https://a.example/path` | `Invalid value for '--origin': must be a serialized origin such as https://app.example.com (no path)` |
| `-H X-Api-Key` | `Invalid value for '-H' / '--header': 'X-Api-Key' is not in KEY:VALUE form` |
| `--timeout 0` | `Invalid value for '--timeout': 0.0 is not in the range x>0.` |

### 8.3 Run lifecycle
1. Parse/validate (exit 2 before I/O). 2. `require_sdk()`. 3. Build `ClientConfig`, consoles (§8.5). 4. `anyio.run(run_checks, cfg, selected, backend="asyncio")` with a transient `rich.status` spinner on stderr when stderr is a TTY and not `--json`. 5. The runner's gate is the handshake check (or the silent internal gate when deselected) — **no separate preflight** (Appendix C). 6. After all checks: human table (stdout) or JSON (stdout) and/or `--output` file (temp file + `os.replace`). JSON is always written when requested, even on exit 1/2. 7. `sys.exit(summary.exit_code)`. `KeyboardInterrupt` → stderr `interrupted`, exit 130; `BrokenPipeError` → flush stderr, `os._exit(code)`.

### 8.4 Exit-code matrix (= §6.10, restated for CI readers)
| Situation | `checks[].status` | exit | `--strict` |
|---|---|---|---|
| all selected pass | pass | 0 | 0 |
| only SHOULD/best-practice findings | warn | 0 | 1 |
| nothing could be judged (401/403/5xx/no eligible tool/gate blocked) | inconclusive | 0 | 1 |
| a MUST violated | fail | 1 | 1 |
| legacy-only server / `-32022` without 2026-07-28 | handshake fail, rest inconclusive | 1 | 1 |
| transport failure, timeout, crash, unreachable | error | 2 | 2 |
| usage error, SDK incompatible | — | 2 | 2 |
| `--version/--help/--list-checks/--json-schema` | — | 0 | 0 |
| SIGINT | — | 130 | 130 |
A JSON-RPC error that *is* a server answer (`-32601`, `-32602`, `-32020`, `-32022`, …) is check data, never exit 2. Exception classes mapped to exit 2 (hierarchy verified in httpx2 2.13.1): `httpx2.TransportError` → `TimeoutException`, `NetworkError` (`ConnectError`, `ReadError`, `WriteError`, `CloseError`), `ProtocolError`, `ProxyError`, `UnsupportedProtocol`, `SSEError`; `httpx2.InvalidURL` (not an `HTTPError` — catch separately); `httpx2.StreamError`; `json.JSONDecodeError` on something that is not even JSON-RPC-shaped.

### 8.5 Human output (stdout)
Console: `Console(stderr=…, highlight=False, markup=False, soft_wrap=True, color_system=None if (no_color or NO_COLOR) else "auto")`. Server strings go through `sanitize()` (C0/C1 and `\x1b` → U+FFFD, newlines → spaces, ≤500 chars) and `Text.append` (never markup interpolation). Glyphs by `console.options.ascii_only` (stdout encoding not UTF — verified for `cp1252`/`ascii`):

| status | glyph | ASCII | style |
|---|---|---|---|
| pass | `✓` U+2713 | `+` | green |
| fail | `✗` U+2717 | `x` | bold red |
| warn | `!` | `!` | yellow |
| inconclusive | `?` | `?` | cyan |
| error | `E` | `E` | bold red |
`⚠`, `▲`, `×`, `–`, `-` are not used (double-width/ambiguous or reserved).

Layout (row = `f"{glyph} {name:<14}{summary}"`, matching the handoff sample exactly):
```
mcpcheck 0.1.0 · MCP 2026-07-28 · https://api.example.com/mcp
server: example-server 2.4.0 · speaks 2026-07-28 · 8 checks            ← variants: "(no serverInfo)", "unreachable — ConnectError: …"

✓ handshake     Server does not require `initialize`
✓ session       No `Mcp-Session-Id` issued or expected; lists identical across 3 connections
! discover      `server/discover` responds correctly (1 warning)
    ! result._meta["io.modelcontextprotocol/serverInfo"]: server does not identify itself  [should]
    spec: https://modelcontextprotocol.io/specification/2026-07-28/server/discover#discoverresult
✓ protocol      Header validation and version errors conform
✗ schemas       2 of 12 tools have invalid inputSchema (+3 more issues)
    ✗ tool:search_orders: inputSchema is not a valid JSON Schema 2020-12: 'nonsense' is not valid under any of the given schemas  [must]
    ! tool:get_invoice: outputSchema absent; optional per spec  [best-practice]
    … and 3 more (use --verbose or --json)
    spec: https://modelcontextprotocol.io/specification/2026-07-28/server/tools#tool
? errors        No probe-safe tools (12 listed; annotate readOnlyHint/destructiveHint or use --probe all)
✗ cache         2 cache-hint violations: prompts/list ttlMs missing; prompts/list cacheScope missing
    ✗ prompts/list: result has no ttlMs  [must]
    ✗ prompts/list: result has no cacheScope  [must]
    spec: https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/caching#cacheable-results
✓ origin        Origin header validated

2 of 8 checks failed, 1 warning, 1 inconclusive. See --json for details.
```
Detail lines: failures then warnings, at most `MAX_ITEMS_SHOWN` unless `-v`, then dim `… and k more (use --verbose or --json)`; `spec:` (dim) only for fail/warn; `error: <type>: <message>` for `error` status; `info[]` only with `-v`. Final line templates (`N` selected, `F/W/I/E`, `T` seconds): `E>0` → `Run incomplete: {E} of {N} checks could not run ({first error}). See --json for details.`; `F>0` → `{F} of {N} checks failed[, {W} warning(s)][, {I} inconclusive]. See --json for details.`; `--strict` promotions → `{F+W+I} of {N} checks failed, {W+I} promoted by --strict. See --json for details.`; all pass → `All {N} checks passed in {T}s.`; pass with warnings → `All {N-I} checks passed[, {W} warning(s)][, {I} inconclusive] in {T}s.`; all inconclusive → `No conclusive checks: {I} inconclusive in {T}s.` Header line 2 says "speaks" (V:14-15: there is no negotiation). Deviations from the handoff sample (listed): two header lines added (version/server identity for bug reports), counts in the final line, three extra statuses, 14-column name padding preserved. `--list-checks` prints the fixed titles: handshake `Server does not require initialize`; session `No Mcp-Session-Id; list results stable across connections`; discover `server/discover implemented and well-formed`; protocol `Protocol headers and version errors validated`; schemas `Tool inputSchema/outputSchema are valid JSON Schema`; errors `Invalid tool calls are rejected with useful errors`; cache `ttlMs/cacheScope present and valid on list results`; origin `Forged Origin header rejected with HTTP 403`.

### 8.6 `--verbose` transcript (stderr)
```
# check: handshake  conn=handshake
> 1 POST https://api.example.com/mcp  tools/list
>   accept: application/json, text/event-stream
>   content-type: application/json
>   mcp-protocol-version: 2026-07-28
>   mcp-method: tools/list
>   authorization: Bearer ****
>   user-agent: mcpcheck/0.1.0 (+https://github.com/suresh-jbt/mcp-check)
>   {"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{…}}}
< 1 200 application/json 48ms
<   content-length: 612
<   {"jsonrpc":"2.0","id":1,"result":{"resultType":"complete",…}}
```
`-v`: dividers, `> seq METHOD url rpc-method [tags]` (tags: `[no Origin]`, `[Origin: …]`, `[no MCP-Protocol-Version]`, `[Mcp-Method: Tools/List]`), `< seq status media-type elapsed` or `< seq ERROR Class: message`. `-vv`: all headers and bodies (JSON compact as sent; SSE as `event:`/`data:` lines; truncated at 4096 chars with ` …[+N bytes]`; non-UTF-8 → `<N bytes, not UTF-8>`). Redaction per §6.13; tests assert the token never appears in stdout, stderr or JSON. With `--json`, stderr stays empty unless `-v`.

### 8.7 JSON report
Writer: `json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"`; key order = model field order. `timestamp` = `datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")`. Stability: `schema_version` integer `1`; additive changes (new optional fields, codes, `check_name` values, `evidence` keys) happen in minor releases; removals/renames/type changes bump to 2 (next major; during 0.x the next minor, announced in CHANGELOG). Repo copy `schema/mcpcheck-report.v1.schema.json` == package data `src/mcpcheck/schema/report.v1.json` (test-asserted). Consumer validation: `check-jsonschema --schemafile schema/mcpcheck-report.v1.schema.json mcpcheck.json`.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://raw.githubusercontent.com/suresh-jbt/mcp-check/main/schema/mcpcheck-report.v1.schema.json",
  "title": "mcpcheck report",
  "description": "Machine-readable result of one mcpcheck run (schema_version 1). Additive changes keep schema_version; removals or type changes bump it.",
  "type": "object", "additionalProperties": false,
  "required": ["schema_version","mcpcheck_version","sdk_version","python_version","spec_revision","server","request","options","protocol_version","server_supported_versions","server_info","era","timestamp","duration_ms","checks","summary"],
  "properties": {
    "$schema": {"type": "string", "format": "uri"},
    "schema_version": {"const": 1},
    "mcpcheck_version": {"type": "string"},
    "sdk_version": {"type": "string", "description": "importlib.metadata.version('mcp')"},
    "python_version": {"type": "string"},
    "spec_revision": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
    "server": {"type": "string", "format": "uri", "description": "SERVER_URL as given (handoff shape); userinfo never present."},
    "request": {"type": "object", "additionalProperties": false, "required": ["origin","auth","extra_headers","tls_verify"],
      "properties": {"origin": {"type": ["string","null"]}, "auth": {"type": "string", "enum": ["none","bearer"]},
                     "extra_headers": {"type": "array", "items": {"type": "string"}, "description": "Header names only."}, "tls_verify": {"type": "boolean"}}},
    "options": {"type": "object", "additionalProperties": false, "required": ["strict","probe","max_tools","timeout","legacy_probes"],
      "properties": {"strict": {"type": "boolean"}, "probe": {"type": "string", "enum": ["safe","all","none"]}, "max_tools": {"type": "integer", "minimum": 1},
                     "timeout": {"type": "number", "exclusiveMinimum": 0}, "legacy_probes": {"type": "boolean"}}},
    "protocol_version": {"type": ["string","null"], "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$", "description": "The version mcpcheck sent and the server served; null if no modern request succeeded."},
    "server_supported_versions": {"type": ["array","null"], "items": {"type": "string"}, "description": "From discover.supportedVersions or -32022 data.supported."},
    "server_info": {"type": ["object","null"], "required": ["name","version"], "properties": {"name": {"type": "string"}, "version": {"type": "string"}, "title": {"type": "string"}}, "additionalProperties": true,
                    "description": "Copy of _meta['io.modelcontextprotocol/serverInfo'] (self-reported, unverified)."},
    "era": {"type": "string", "enum": ["modern-only","dual-era","legacy-only","unknown"]},
    "timestamp": {"type": "string", "format": "date-time", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"},
    "duration_ms": {"type": "integer", "minimum": 0},
    "checks": {"type": "array", "items": {"$ref": "#/$defs/check"}, "description": "One entry per selected check in canonical order; deselected checks are listed in summary.skipped."},
    "summary": {"type": "object", "additionalProperties": false,
      "required": ["total","passed","warned","failed","inconclusive","errored","skipped","exit_code"],
      "properties": {"total": {"type": "integer", "minimum": 0}, "passed": {"type": "integer", "minimum": 0}, "warned": {"type": "integer", "minimum": 0},
                     "failed": {"type": "integer", "minimum": 0}, "inconclusive": {"type": "integer", "minimum": 0}, "errored": {"type": "integer", "minimum": 0},
                     "skipped": {"type": "array", "items": {"$ref": "#/$defs/check_name"}, "uniqueItems": true}, "exit_code": {"type": "integer", "enum": [0,1,2]}}}
  },
  "$defs": {
    "check_name": {"type": "string", "enum": ["handshake","session","discover","protocol","schemas","errors","cache","origin"]},
    "status": {"type": "string", "enum": ["pass","warn","fail","inconclusive","error"]},
    "level": {"type": "string", "enum": ["must","should","best-practice"]},
    "finding": {"type": "object", "additionalProperties": false, "required": ["code","level","subject","message","issue"],
      "properties": {"code": {"type": "string", "pattern": "^[a-z]+\\.[a-z0-9_]+$"}, "level": {"$ref": "#/$defs/level"}, "subject": {"type": "string"},
                     "message": {"type": "string", "maxLength": 500}, "spec_url": {"type": "string", "format": "uri"},
                     "evidence": {"type": "object", "additionalProperties": true}, "tool": {"type": "string"}, "issue": {"type": "string", "description": "Handoff compatibility: same text as message."}}},
    "check": {"type": "object", "additionalProperties": false,
      "required": ["name","title","status","severity","passed","summary","details","spec_url","failures","warnings","info","duration_ms","requests","error"],
      "properties": {"name": {"$ref": "#/$defs/check_name"}, "title": {"type": "string"}, "status": {"$ref": "#/$defs/status"},
                     "severity": {"oneOf": [{"$ref": "#/$defs/level"}, {"type": "null"}]}, "passed": {"type": "boolean", "description": "true iff this check did not raise the exit code."},
                     "summary": {"type": "string", "maxLength": 100}, "details": {"type": "string"}, "spec_url": {"type": "string", "format": "uri"},
                     "failures": {"type": "array", "items": {"$ref": "#/$defs/finding"}}, "warnings": {"type": "array", "items": {"$ref": "#/$defs/finding"}},
                     "info": {"type": "array", "items": {"$ref": "#/$defs/finding"}}, "duration_ms": {"type": "integer", "minimum": 0}, "requests": {"type": "integer", "minimum": 0},
                     "error": {"type": ["object","null"], "required": ["type","message"], "properties": {"type": {"type": "string"}, "message": {"type": "string"}}, "additionalProperties": false}},
      "allOf": [
        {"if": {"properties": {"status": {"const": "fail"}}}, "then": {"properties": {"failures": {"minItems": 1}, "passed": {"const": false}}}},
        {"if": {"properties": {"status": {"const": "pass"}}}, "then": {"properties": {"failures": {"maxItems": 0}, "warnings": {"maxItems": 0}, "passed": {"const": true}, "severity": {"type": "null"}}}},
        {"if": {"properties": {"status": {"const": "error"}}}, "then": {"properties": {"error": {"type": "object"}, "passed": {"const": false}}}}
      ]}
  }
}
```
The cli-output prototype of this schema (without `info`, `request`, `era`, `sdk_version`, `python_version`, `protocol` and the string `server`) was verified against `Draft202012Validator.check_schema`, positive and five negative examples; the additions are mechanical and are re-verified by `tests/test_report_schema.py` in M0.

Example report (TO-FILL-FROM-REAL-RUN before README embedding; this one reflects the §8.5 table):
```json
{
  "$schema": "https://raw.githubusercontent.com/suresh-jbt/mcp-check/main/schema/mcpcheck-report.v1.schema.json",
  "schema_version": 1, "mcpcheck_version": "0.1.0", "sdk_version": "2.3.0", "python_version": "3.13.5", "spec_revision": "2026-07-28",
  "server": "https://api.example.com/mcp",
  "request": {"origin": null, "auth": "bearer", "extra_headers": [], "tls_verify": true},
  "options": {"strict": false, "probe": "safe", "max_tools": 20, "timeout": 10.0, "legacy_probes": false},
  "protocol_version": "2026-07-28", "server_supported_versions": ["2026-07-28"], "server_info": {"name": "example-server", "version": "2.4.0"}, "era": "unknown",
  "timestamp": "2026-10-16T14:32:11Z", "duration_ms": 1342,
  "checks": [
    {"name": "handshake", "title": "Server does not require initialize", "status": "pass", "severity": null, "passed": true,
     "summary": "Server does not require `initialize`", "details": "tools/list with per-request _meta and MCP-Protocol-Version/Mcp-Method headers returned HTTP 200 application/json (resultType complete, 12 tools) on a fresh connection.",
     "spec_url": "https://modelcontextprotocol.io/specification/2026-07-28/basic#statelessness", "failures": [], "warnings": [], "info": [], "duration_ms": 61, "requests": 1, "error": null},
    {"name": "cache", "title": "ttlMs/cacheScope present and valid on list results", "status": "fail", "severity": "must", "passed": false,
     "summary": "2 cache-hint violations: prompts/list ttlMs missing; prompts/list cacheScope missing",
     "details": "Checked result-level hints on tools/list (1 page), prompts/list (1 page), resources/list (1 page), resources/templates/list (not implemented, -32601). server/discover hints validated by the discover check; resources/read not probed (read-only policy).",
     "spec_url": "https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/caching#cacheable-results",
     "failures": [
       {"code": "cache.ttl_missing", "level": "must", "subject": "prompts/list", "message": "result has no ttlMs", "issue": "result has no ttlMs",
        "spec_url": "https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/caching#cacheable-results", "evidence": {"method": "prompts/list", "page": 0, "got": null, "expected": "JSON integer >= 0"}},
       {"code": "cache.scope_missing", "level": "must", "subject": "prompts/list", "message": "result has no cacheScope", "issue": "result has no cacheScope",
        "spec_url": "https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/caching#cache-scope-field", "evidence": {"method": "prompts/list", "page": 0, "got": null, "expected": "\"public\" | \"private\""}}],
     "warnings": [], "info": [], "duration_ms": 3, "requests": 0, "error": null}
  ],
  "summary": {"total": 8, "passed": 4, "warned": 1, "failed": 2, "inconclusive": 1, "errored": 0, "skipped": [], "exit_code": 1}
}
```
(`total: 8` with two checks shown is illustrative; a real embed keeps all checks or notes the trim.)

Reserved runner codes in `codes.py`: `runner.not_run` (gate blocked / aborted), `runner.internal_error`.

### 8.8 CI snippets (single owner; README embeds these)
GitHub Actions (action refs verified by the packaging section: `actions/checkout@v7`, `astral-sh/setup-uv@v10.3.0`, `actions/upload-artifact@v7`; `uvx mcpcheck@0.1.0` pin syntax verified):
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
          # stream never returns (verified). Any HTTP answer (e.g. 400) means the endpoint is up.
          timeout 30 bash -c 'until curl -s -o /dev/null -m 5 -X POST -H "Content-Type: application/json" --data "{}" http://127.0.0.1:8000/mcp; do sleep 0.5; done'
      - name: Check conformance with MCP 2026-07-28
        env:
          MCPCHECK_TOKEN: ${{ secrets.MCP_TEST_TOKEN }}   # omit for unauthenticated servers
        run: uvx mcpcheck@0.1.0 http://127.0.0.1:8000/mcp --output mcpcheck.json
      - uses: actions/upload-artifact@v7
        if: always()
        with: {name: mcpcheck-report, path: mcpcheck.json}
```
Gate only on MUST violations (default) or add `--strict`; make it advisory with `|| [ $? -eq 1 ]` (lets exit 1 through, keeps exit 2 fatal). GitLab (`python:3.13-slim`, no curl; `allow_failure.exit_codes` UNVERIFIED today):
```yaml
mcp-conformance:
  stage: test
  image: python:3.13-slim
  before_script: [pip install --quiet mcpcheck==0.1.0]
  script:
    - python -m my_server --port 8000 &
    - for i in $(seq 1 60); do python -c "import socket; socket.create_connection(('127.0.0.1', 8000), timeout=1)" 2>/dev/null && break; sleep 0.5; done
    - mcpcheck http://127.0.0.1:8000/mcp --strict --output mcpcheck.json
  artifacts: {when: always, paths: [mcpcheck.json], expire_in: 30 days}
  allow_failure: {exit_codes: [1]}
```
Reading the artifact: `jq -r '.checks[] | select(.status != "pass") | "\(.status)\t\(.name)\t\(.summary)"' mcpcheck.json`.

---

## 9. Safety model

**Read-only guarantees (public promise; identical wording in README, SECURITY.md, `--help`).** Exactly the request list of §4. Enforced by `tests/test_integration.py::test_request_log_contains_only_allowed_requests`, which runs a full default run against the reference fixture's request log and asserts every `(verb, method)` is in the allow-list, and by `test_probe_policy.py` (no code path builds schema-valid arguments; `is_probe_eligible` is False for unannotated tools under `safe`).

**Probe policy (U2).** `safe` (default): a tool is called only when its own annotations declare `readOnlyHint: true` or `destructiveHint: false`; payload = best proven-invalid candidate (`missing-required` preferred); annotations are untrusted (TL:304-307), which is why even eligible tools only ever receive schema-invalid input. `all`: every tool with a proven-invalid payload, after a stderr warning. `none`: no `tools/call`. The unknown-tool probe (`UNKNOWN_TOOL_NAME`, `arguments: {}`) always runs under `safe`/`all`. `--max-tools` bounds volume. Residual risk disclosed: a server that ignores its own `inputSchema` could execute an annotated tool on garbage input; that is the `errors.accepted_invalid_input` finding.

**Legacy probes (U4).** Off by default. `--legacy-probes` adds one legacy `initialize` (may allocate a server-side session on dual-era servers; never followed by `DELETE`) and one headers-only `GET`.

**Redaction.** §6.13: `Authorization`/`-H` values and any header matching `token|secret|password|cookie|x-api-key` are `****` everywhere; literal secret values scrubbed from bodies/URLs; the JSON report records `request.auth: "bearer"` and header *names* only. Tests grep stdout/stderr/JSON for the token.

**TLS and redirects.** `verify=True` unless `--insecure` (stderr warning, recorded in `request.tls_verify`); `SSL_CERT_FILE` honoured via httpx2 `trust_env` (UNVERIFIED for httpx2). Redirects: same-origin 307/308 only, ≤5, POST preserved; 301/302/303 and cross-origin redirects are never followed (POST→GET downgrade and token leakage — verified), reported as inconclusive with the `Location`.

**Bounds.** Per-request total budget `--timeout`; per-check `10×`; body cap 16 MiB; schema depth/node caps; `MAX_PAGES`; sequential execution; one 429 retry (errors) and one 5xx retry (handshake). Nothing from the server is executed, dereferenced (`$ref`, icons) or trusted (`serverInfo`, annotations are displayed as-is).

**Footprint users should expect in server logs.** ~20–60 requests on ~10 connections per full run; one request with `Origin: https://evil.example.com` (SDK servers log a WARNING); several fresh connections; deliberately malformed header probes (`protocol`) and invalid tool calls (`errors`). Only run against servers you own or are authorized to test.

---

## 10. Testing strategy

### 10.1 Framework decisions
pytest ≥ 9 with **anyio's bundled plugin** (`-p anyio`; `pytest-asyncio` is not installed — both in auto mode warn, and `filterwarnings = ["error"]` turns that into a failure); session-scoped `anyio_backend` fixture returning `"asyncio"` (required for session-scoped async fixtures — verified); `pytest-timeout` (60 s global), `pytest-cov`, `pytest-xdist` (safe: every server binds its own port). `addopts = "-ra --strict-config --strict-markers -p anyio --import-mode=importlib"`, `xfail_strict = true`. Markers: `integration` (uvicorn on a real socket), `slow` (>1 s by design), `sdk_matrix`, `legacy` (needs an `mcp<2` environment; skipped unless `MCPCHECK_LEGACY_FIXTURE` is set). Legacy `initialize()` calls in tests emit `mcp.shared.exceptions.MCPDeprecationWarning` (verified present) and must be wrapped in `pytest.warns`. mypy strict covers `tests/`. `tests/__init__.py` and `tests/fixtures/__init__.py` exist (needed for `python -m tests.fixtures.reference_server` and `--import-mode=importlib`).

### 10.2 Test pyramid and transports
| Layer | Exercises | Transport | ≈ count | Budget |
|---|---|---|---|---|
| L1 verdict logic | one `Check.run()` against scripted replies | `httpx2.MockTransport(handler)` (sync or async handler; JSON/SSE/403-text/400-JSON-RPC all pass through — verified) | ~190 | < 4 s |
| L2 real wire shapes, no sockets | checks against the SDK `MCPServer` app and the misbehaving Starlette app | `httpx2.ASGITransport(app, client=("127.0.0.1", N))` | ~90 | < 3 s |
| L3 integration | full `run_checks()` over TCP: SSE streaming, hangs/timeouts, Origin/Host behaviour, fresh connections | uvicorn on `127.0.0.1:0` in a daemon thread | ~45 | < 12 s |
| L4 CLI | exit codes, `--json` schema validity, flags, golden output | `click.testing.CliRunner` | ~30 | < 4 s |
| L5 pure | wire parsing, probe policy generator, schema helpers, report model | none | ~70 | < 1 s |
ASGITransport rules (verified): the SDK app needs its lifespan entered manually (`async with app.router.lifespan_context(app):` — starlette 1.8.0; `asgi-lifespan` is the UNVERIFIED fallback) or it raises `RuntimeError: Task group is not initialized`; request host must be `127.0.0.1`/`localhost` (`testserver` → 421 from the DNS-rebinding guard); **httpx2 timeouts never fire** and `stream()` blocks until the full body arrives, so hang/timeout/endless-SSE tests live in L3 only; keep `raise_app_exceptions=True`. MockTransport: a `ScriptedServer` helper records every request (`method, headers, body`) so tests assert what mcpcheck *sent* (headers, `_meta`, `Mcp-Name`), parametrised `sse ∈ {False, True}` (T:87-90).

```python
# tests/helpers/scripted.py (skeleton; behaviour verified)
def sse_body(*msgs: dict) -> bytes:
    return "".join(f"event: message\ndata: {json.dumps(m)}\n\n" for m in msgs).encode()


@dataclass
class ScriptedServer:
    routes: dict[str, Callable[[dict, httpx2.Request], httpx2.Response]]
    default_status: int = 404
    seen: list[Seen] = field(default_factory=list)

    async def handler(
        self, req: httpx2.Request
    ) -> (
        httpx2.Response
    ): ...  # records Seen(method, headers, body); POST only; -32601 for unknown methods
    def client(self, **kw) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            transport=httpx2.MockTransport(self.handler),
            base_url="http://127.0.0.1:1",
            timeout=httpx2.Timeout(5),
            **kw,
        )


def ok(body, result, *, sse=False, headers=None) -> httpx2.Response: ...
def jsonrpc_error(req_id, code, message, data=None) -> dict: ...
```

### 10.3 Fixture servers
**`tests/fixtures/reference_server.py`** — fully conformant, SDK-based (`MCPServer` with `cache_hints` for all six cacheable methods, two read-only tools `lookup_weather` and `echo_required` **both with `outputSchema`** (fixes the handoff acceptance criterion; `--no-output-schema` toggle for the WARN test), one resource, one prompt, `TransportSecuritySettings`, deterministic `serverInfo = {"name": "mcpcheck-reference-server", "version": "0.1.0"}`, fixed tool order). API: `build_app(*, json_response=False, stateless_http=True, origin_check=True, host="127.0.0.1", output_schema=True, empty=False, bare=False) -> Starlette`; `main(argv)` with `--port` (**default 0**, prints `mcpcheck reference server 0.1.0 listening on http://127.0.0.1:<port>/mcp` to stderr), `--json-response`, `--stateful`, `--no-origin-check`, `--require-token TOKEN`, `--app-host HOST`, `--no-output-schema`, `--empty`, `--bare`. Verified wire behaviour: modern `tools/list` → 200 JSON with `ttlMs:300000 cacheScope:"public" resultType _meta.serverInfo`; `server/discover` → all required keys; evil Origin → 403 text `Invalid Origin header` without content-type; progress call → SSE; header-less request → legacy SSE 2025-shaped; `2099-01-01` → 400 `-32022`; GET with 2026 header → 405; unknown tool / invalid args → 200 `isError:true`.

**`tests/fixtures/misbehaving_server.py`** — hand-rolled Starlette JSON-RPC app (no lifespan needed; runs under ASGITransport and uvicorn — prototype verified on both SDK venvs), `build_app(toggles: Toggles) -> Starlette`, `main --port 0 --toggle X --set k=v`, adds `X-Client-Port` to every response, exposes `/__log` (request log) for assertions. `Toggles` (frozen dataclass; **default instance passes every check** — regression guard that toggles, not the baseline, cause findings):
```python
@dataclass(frozen=True)
class Toggles:
    # handshake / session
    require_initialize: bool = False
    legacy_shape: bool = False
    mint_session_id: bool = False
    require_session_id: bool = False
    echo_session_id: bool = False
    vary_tools_per_connection: bool = False
    vary_tools_per_request: bool = False
    nondeterministic_order: bool = False
    vary_description: bool = False
    # discover
    no_discover: bool = False
    discover_no_server_info: bool = False
    discover_wrong_versions: bool = False
    capability_mismatch: bool = False
    discover_variant: str | None = (
        None  # 42 payload variants: no-hints, neg-ttl, ttl-str, scope-user, caps-null, meta-badkey, ...
    )
    # schemas
    tools: tuple[dict, ...] = DEFAULT_TOOLS
    pages: tuple[dict, ...] | None = None
    loop_cursor: bool = False
    # errors
    error_style: Literal["actionable", "traceback", "bare", "jsonrpc", "success"] = "actionable"
    error_shapes_catalogue: bool = False  # the 25 named tools of §7.5(9)
    # cache
    omit_ttl: bool = False
    negative_ttl: bool = False
    float_ttl: float | None = None
    omit_cache_scope: bool = False
    cache_scope: str = "public"
    scope_varies_across_pages: bool = False
    user_specific_public: bool = False
    # origin
    origin_mode: str = "sdk_like"  # reject_400_jsonrpc | reject_200_jsonrpc_error | allowlist_app_origin | require_origin | accept_null | accept_all | accept_all_sse | legacy_only | redirect_evil | reset_evil | hang_evil | cors_reflect
    # protocol
    ignore_version_mismatch: bool = False
    serve_headerless_as_legacy: bool = False
    unknown_method_status: int = 404
    legacy_error_codes: bool = False
    accept_missing_meta: bool = False
    ignore_cursor: bool = False
    # transport shape
    sse: bool = False
    slow_seconds: float = 0.0
    endless_sse: bool = False
    x_accel_buffering: bool = True
    status: int | None = None
    content_type: str | None = None
    body: bytes | None = None  # raw HTTP overrides (401, 403 text, 404 html, 500, 202, redirects)
```

### 10.4 Expected-results tables
Reference server (both SDK versions × both response modes): **every check `pass`, exit 0**, `summary.failed == 0`, `summary.warned == 0` (`cache` may put an `info` entry about `resources/read` not probed). This is the handoff acceptance criterion, tested as `test_acceptance_reference_all_pass`.

Misbehaving server, one toggle per row (parametrised integration test asserting `code` present, `status`, and `level`):
| Toggle | Check | Code | Status/level | Spec |
|---|---|---|---|---|
| `require_initialize` | handshake | `handshake.initialize_required` | fail/must; dependents `runner.not_run` inconclusive | T:655-660 |
| `legacy_shape` | handshake | `handshake.legacy_shaped_result` | fail/must | B:75 |
| `require_session_id` | session | `session.required`, `session.bogus_id_rejected` | fail/must | B:193-195 |
| `mint_session_id` | session | `session.session_id_minted` | warn/should | T:680-681 |
| `echo_session_id` | session | `session.session_id_echoed` | warn/should | T:680-681 |
| `vary_tools_per_connection` | session | `session.list_varies_per_connection` | fail/must | TL:62-69 |
| `vary_tools_per_request` | session | `…within_connection`, `…with_session_header` too | fail/must | TL:65-66 |
| `nondeterministic_order` | session | `session.order_not_deterministic` | warn/should | TL:71-74 |
| `no_discover` | discover | `discover.not_implemented` | fail/must | DI:9-11 |
| `discover_no_server_info` | discover | `discover.server_info_missing` | warn/should | DI:100-101 |
| `discover_wrong_versions` | discover | `discover.supported_versions_omit_served` | warn/best-practice | DI:96-97 |
| `capability_mismatch` | discover | `discover.capability_undeclared` | fail/must | TL:47 |
| `discover_variant=no-hints` | discover | `discover.ttl_missing`, `discover.scope_missing` (shared validator with `code_prefix="discover"`) | fail/must | CA:15-18 |
| `tools` with `inputSchema: null` / `{}` / `{"required":"a"}` | schemas | `input_schema_not_object` / `input_schema_type` / `input_schema_invalid` | fail/must | TL:293, SCH:4911, B:299 |
| `tools` without `outputSchema` | schemas | `schemas.output_schema_missing` | warn/best-practice; fail with `--strict` | TL:299 |
| `tools` with `outputSchema: {}` | schemas | `schemas.output_schema_permissive` | warn/best-practice | — |
| `tools` with bad `x-mcp-header` | schemas | `schemas.x_mcp_header_invalid` | fail/must | TL:344-366 |
| `error_style=traceback` / `bare` | errors | `errors.non_actionable_messages` | warn/best-practice | heuristic |
| `error_style=jsonrpc` | errors | — | pass | TL:730-779 |
| `error_style=success` | errors | `errors.accepted_invalid_input` | fail/must | TL:783-784 |
| `omit_ttl` / `omit_cache_scope` | cache | `cache.ttl_missing` / `cache.scope_missing` | fail/must | CA:15-23 |
| `negative_ttl` | cache | `cache.ttl_negative` | fail/must | CA:62 |
| `float_ttl=60000.0` | cache | `cache.ttl_integral_float` | warn/best-practice | — |
| `cache_scope="user"` | cache | `cache.scope_invalid` | fail/must | CA:46 |
| `scope_varies_across_pages` | cache | `cache.scope_inconsistent_pages` | fail/must | CA:168-171 |
| `user_specific_public` (+token) | cache | `cache.public_user_specific_names` | warn/best-practice | CA:107-110 |
| `origin_mode=accept_all` | origin | `origin.evil_accepted` | fail/must | T:57-61 |
| `origin_mode=reject_400_jsonrpc` | origin | `origin.evil_rejected_wrong_status` | fail/must | T:59-61 |
| `origin_mode=require_origin` | origin | `origin.no_origin_rejected` | warn/best-practice | — |
| `ignore_version_mismatch` | protocol | `protocol.header_mismatch_accepted` | fail/must | T:257-261 |
| `serve_headerless_as_legacy` | protocol | `protocol.headerless_accepted` | warn/should | T:277-281 |
| `unknown_method_status=200` | protocol | `protocol.unknown_method_wrong_status` | fail/must | T:271-275 |
| `legacy_error_codes` | protocol | `protocol.forbidden_error_code` | fail/must | B:139-146 |
| `accept_missing_meta` | protocol | `protocol.missing_meta_accepted` | fail/must | B:382-384 |
| `sse=True` alone | all | — | all pass | T:87-90 |
| `x_accel_buffering=False` + `sse` | discover/protocol | `*.no_accel_buffering_header` | warn/should | T:133-134 |
Combination row: `require_initialize` → handshake fail, every dependent `inconclusive` with `runner.not_run`, exit 1. Environment rows: `status=401` → every check inconclusive, exit 0 (1 with `--strict`), summary mentions `--token`; closed port → all `error`, exit 2, < 2 s; `endless_sse` → check finishes within `10×timeout` as `error`/inconclusive, never hangs; `slow_seconds=2` with `--timeout 0.5` → inconclusive within ~1 s.

### 10.5 Unit scenarios per check (L1/L2)
Each `tests/test_<check>.py` has (a) the MockTransport scenarios listed in its §7 part (9), each `@pytest.mark.parametrize("sse", [False, True])`, (b) one ASGI pass test against `reference_server.build_app()`, (c) ASGI/uvicorn tests for its rows in §10.4. Cross-cutting `tests/test_wire.py` / `test_client.py`: SSE parser (multi-line `data:`, `\r\n`, `: ping` comments, `event:` other than message, trailing partial frame, `id:` counting), `content-type; charset=utf-8`, 4xx mapping preserved (the point of the raw layer), redirect policy (307 same-origin ≤5 followed; 302 and cross-origin blocked with `redirect_blocked`), header overrides/omissions, `main` client asserting against tampering, `User-Agent`/`Authorization`/`-H` presence, redaction, `classify_reply` matrix (every row of §6.6), body cap, total budget vs keepalive SSE (L3).

### 10.6 Integration and safety tests (`tests/test_integration.py`)
Thread-based uvicorn fixture (`uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=free_port(), log_level="warning", lifespan="on", access_log=False))` in a daemon thread; readiness = poll `server.started` ≤10 s; teardown `should_exit=True; join(5)`; signal capture skipped off the main thread — verified); session-scoped `reference_url`/`reference_url_json`, module-scoped `misbehaving_url(toggles)`. Tests: `test_acceptance_reference_all_pass` (exit 0, every status `pass`), both response modes; every §10.4 row; `test_request_log_contains_only_allowed_requests` (default run against the reference fixture: every `(verb, method)` in the §4 allow-list; no `resources/read`, `prompts/get`, `initialize`, `DELETE`); `test_legacy_probes_adds_initialize_and_get_only`; `test_stateful_reference_passes` (dual-era allowed); hazards: `endless_sse`, `slow_seconds`, header-less GET hazard (protocol GET probe only with the 2026 header, headers-only read), connection refused, TLS verify (`https://` to a plain server → error, not crash); meta-test: no `uvicorn-fixture` thread survives the session.

### 10.7 Pure tests
`tests/test_probe_policy.py`: property test — every generated `arguments` is `not Draft202012Validator(schema, registry=Registry()).is_valid(...)`; `target_param ∈ properties`; no network `$ref`; `{}`/`{"type":"object"}`/`true` → `[]`; `is_probe_eligible` False for unannotated or `destructiveHint: true`, True for `readOnlyHint: true` or `destructiveHint: false`; determinism. `tests/test_models.py`/`test_report_schema.py`: schema in sync (regenerate in memory), metaschema-valid, example valid, five negatives (`fail` with empty `failures`, `exit_code: 3`, code `Discover.X`, `+00:00` timestamp, extra top-level key), repo copy identical. `tests/test_codes.py`: every code emitted anywhere in the suite is registered in `codes.py` and matches the regex. `tests/test_spec_urls.py`: every constant starts with `SPEC_BASE`; none of the four dead handoff paths; `network` marker: each URL answers 200 (skipped by default). `tests/test_sdk_contract.py` (`sdk_matrix`): the Appendix A facts. `tests/test_py310.py`: AST feature-version guard + forbidden-construct grep. `tests/test_readme.py`: options table == `--help` options; every `REGISTRY` class name appears in the README table (`for cls in REGISTRY: assert f"| \`{cls.name}\` |" in table`); no relative links.

### 10.8 CLI tests (`tests/test_cli.py`)
`CliRunner()` (click ≥ 8.2 separates streams; `mix_stderr` no longer exists — verified: `CliRunner.__init__(charset, env, echo_stdin, catch_exceptions, capture)`), `catch_exceptions=False`, `terminal_width=80`, env `{"COLUMNS":"100","NO_COLOR":"1","TERM":"dumb","TZ":"UTC"}`. Table: all pass (exit 0, 8 `✓`/`+` lines); failures (exit 1, `x origin`, final line); `--json` (parses, validates against the shipped schema, `summary.total == len(checks)`, timestamp `Z`, nothing else on stdout); `-c origin,cache` (two checks, canonical order); `--skip errors`; `-c x --skip x` → 2; `--strict` on a warn-only run → 1; bad URLs → 2 with the §8.2 text; unreachable → 2 in < 2 s; timeout (`slow`) → inconclusive/0 or error/2 per §6.6; `--version` == `importlib.metadata.version`; `--origin`/`-H`/`--token` observed on the wire (fixture echo) and never in any stream (`-vv` included); `--verbose` transcript on stderr only; `--list-checks`/`--json-schema` exit 0 without URL; help snapshot; subprocess smoke `python -m mcpcheck <url>`; SIGINT subprocess test → 130. Golden files (`tests/golden/all_pass.txt`, `mixed.txt`, `json_pretty.json`) normalised (`127.0.0.1:\d+` → `PORT`, timestamps, version); `pytest --update-goldens` rewrites; CI never passes it; the README embeds the normalised goldens.

### 10.9 SDK matrix
CI re-locks per cell (`uv lock --upgrade-package "mcp==2.2.0"` / `mcp`) and runs the whole suite. Tests must not assert SDK-server error prose (2.2.0 pre-negotiation `-32602` text differs) nor exact legacy `initialize` replies (`capabilities.experimental: {}` only on 2.2.0). `test_sdk_contract.py` pins: `validate_server_result("tools/list","2026-07-28",{"tools":[]})` raises for `cacheScope/resultType/ttlMs`; `ttlMs:-1` → `greater_than_equal`, `"user"` → `literal_error`, `"5"`/`5.0`/`True` accepted (so raw checks stay); `find_invalid_x_mcp_header` reason for a `number` property; `MODERN_PROTOCOL_VERSIONS == ("2026-07-28",)`; `mcp.shared.inbound.ERROR_CODE_HTTP_STATUS[-32601] == 404`; `encode_header_value("=?base64?literal?=")` is re-encoded; `("tools/call","2026-07-28") in SERVER_RESULTS`; `MCPDeprecationWarning` importable; `ToolAnnotations()` fields default `None`.

### 10.10 Speed budget and flakiness controls
Total < 30 s locally (`-m "not slow"` < 15 s; `-m "not integration"` < 10 s); ports always `127.0.0.1:0` read back from the socket (never `localhost`, never 8765); readiness by polling, not sleeping; hangs bounded by pytest-timeout and `anyio.fail_after` around every live await; hazard tests only in L3; `filterwarnings=error`; xdist-safe; determinism via fixed `serverInfo`, tool order, `TZ=UTC`, `COLUMNS=100`, `NO_COLOR=1`, normalised ports/timestamps; never `mcp.client.client.Client` (its cache serves repeats without HTTP). Coverage: `fail_under = 85` overall; soft targets `checks/*` ≥ 95 %, `client.py` ≥ 90 %, `output.py` ≥ 80 %; `tests/fixtures/*` excluded.

---

## 11. Packaging, CI/CD and release

### 11.1 Version single-source
`[project].version = "0.1.0"` is the only place the version is written; the CLI reads it back via `importlib.metadata.version("mcpcheck")`; `--version` prints exactly `mcpcheck 0.1.0`; the release workflow refuses a tag whose `vX.Y.Z` ≠ `uv version --short`. Bump with `uv version --bump patch|minor|major`, commit, tag.

### 11.2 `pyproject.toml` (draft; edits vs. the packaging draft marked ◀)
```toml
[build-system]
requires = ["hatchling>=1.27"]
build-backend = "hatchling.build"

[project]
name = "mcpcheck"
version = "0.1.0"
description = "Validate an MCP server's conformance to the MCP 2026-07-28 revision over Streamable HTTP (read-only)."
readme = "README.md"
requires-python = ">=3.10"
license = "MIT"                              # PEP 639; no "License ::" classifier
license-files = ["LICENSE"]
authors = [{ name = "<U9: real name>", email = "<U9: email>" }]
keywords = ["mcp", "model-context-protocol", "conformance", "validator", "streamable-http", "json-rpc", "cli"]
classifiers = [
  "Development Status :: 3 - Alpha", "Environment :: Console", "Framework :: AsyncIO", "Framework :: Pydantic :: 2",
  "Intended Audience :: Developers", "Operating System :: OS Independent",
  "Programming Language :: Python :: 3", "Programming Language :: Python :: 3 :: Only",
  "Programming Language :: Python :: 3.10", "Programming Language :: Python :: 3.11", "Programming Language :: Python :: 3.12",
  "Programming Language :: Python :: 3.13", "Programming Language :: Python :: 3.14",
  "Topic :: Software Development :: Quality Assurance", "Topic :: Software Development :: Testing", "Typing :: Typed",
]
dependencies = [
  "mcp>=2.2,<3",
  "httpx2>=2.10",        # ◀ the SDK's own floor; the package is httpx2, not httpx
  "anyio>=4.9",
  "pydantic>=2.12",
  "click>=8.2",          # ◀ CliRunner streams always separate; mix_stderr removed
  "rich>=13.9.4",
  "jsonschema>=4.20",
]

[project.urls]
Homepage = "https://github.com/suresh-jbt/mcp-check"      # ◀ U7 may rename
Documentation = "https://github.com/suresh-jbt/mcp-check#readme"
Repository = "https://github.com/suresh-jbt/mcp-check"
Issues = "https://github.com/suresh-jbt/mcp-check/issues"
Changelog = "https://github.com/suresh-jbt/mcp-check/blob/main/CHANGELOG.md"

[project.scripts]
mcpcheck = "mcpcheck.__main__:main"

[dependency-groups]
dev = [{ include-group = "test" }, { include-group = "lint" }, { include-group = "typecheck" }]
test = ["pytest>=9", "pytest-cov>=6.0", "pytest-timeout>=2.3", "pytest-xdist>=3.6",      # ◀ pytest>=9; NO pytest-asyncio
        "starlette>=0.27", "uvicorn>=0.31.1"]                                               # fixture servers (SDK deps anyway)
lint = ["ruff>=0.17", "readme-renderer[md]>=46"]                                            # ◀ for scripts/check_readme.py
typecheck = ["mypy>=2.0"]

[tool.uv]
required-version = ">=0.12"
default-groups = ["dev"]

[[tool.uv.index]]
name = "testpypi"
url = "https://test.pypi.org/simple/"
publish-url = "https://test.pypi.org/legacy/"
explicit = true

[tool.hatch.build.targets.sdist]
exclude = ["/.github", "/.pre-commit-config.yaml", "/uv.lock", "/.python-version", "/dist", "/reports", "/htmlcov", "/.coverage*", "/.venv"]
# hatchling includes src/mcpcheck/py.typed and src/mcpcheck/schema/report.v1.json by default (verified by the reviewer with a probe wheel);
# the CI build job still asserts their presence.

[tool.ruff]
line-length = 100
src = ["src", "tests"]                       # target-version inferred (py310) from requires-python
[tool.ruff.lint]
select = ["E","W","F","I","UP","B","ASYNC","C4","SIM","RUF","PT","PERF","S","N","ISC","PIE","T20","RET","ARG","TC","FA","PTH","PGH","LOG","G","TRY","EM","BLE","A","FURB","FLY","D"]
ignore = ["E501","ISC001","D105","D107","TRY003","EM101","EM102"]
[tool.ruff.lint.per-file-ignores]
"tests/**" = ["S101","S105","S106","D","ARG001","ARG002"]
"src/mcpcheck/output.py" = ["T201"]          # the one place allowed to print()
[tool.ruff.lint.isort]
known-first-party = ["mcpcheck"]
[tool.ruff.lint.pydocstyle]
convention = "google"
[tool.ruff.format]
docstring-code-format = true

[tool.mypy]
python_version = "3.10"
strict = true
plugins = ["pydantic.mypy"]
files = ["src", "tests"]
warn_unreachable = true
pretty = true
show_error_codes = true
enable_error_code = ["ignore-without-code", "redundant-expr", "truthy-bool", "possibly-undefined"]
[[tool.mypy.overrides]]
module = "tests.*"
disallow_untyped_decorators = false
[tool.pydantic-mypy]
init_forbid_extra = true
init_typed = true
warn_required_dynamic_aliases = true

[tool.pytest.ini_options]
minversion = "9.0"                           # ◀
testpaths = ["tests"]
addopts = "-ra --strict-config --strict-markers -p anyio --import-mode=importlib"
xfail_strict = true
filterwarnings = ["error"]
timeout = 60
markers = ["integration: exercises the in-repo fixture HTTP server (free port, 127.0.0.1)",
           "slow: > 1 s by design", "sdk_matrix: asserts SDK facts", "legacy: needs an mcp<2 environment", "network: hits the public spec site"]

[tool.coverage.run]
branch = true
source = ["mcpcheck"]
parallel = true
relative_files = true
[tool.coverage.paths]
source = ["src/mcpcheck", "*/site-packages/mcpcheck"]
[tool.coverage.report]
show_missing = true
skip_empty = true
fail_under = 85
exclude_also = ["if TYPE_CHECKING:", "@overload", "raise NotImplementedError", "if __name__ == .__main__.:", "except PackageNotFoundError:"]
```

### 11.3 `.github/workflows/ci.yml` (draft; SHAs verified by the packaging section on 2026-10-16, Dependabot keeps them fresh)
```yaml
name: CI
on: { push: { branches: [main] }, pull_request: {}, workflow_dispatch: {} }
permissions: { contents: read }
concurrency: { group: "${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}", cancel-in-progress: true }
env: { UV_VERSION: "0.13.1", FORCE_COLOR: "1", PYTHONUNBUFFERED: "1" }
defaults: { run: { shell: bash } }
jobs:
  lint:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with: { persist-credentials: false }
      - uses: astral-sh/setup-uv@1c37ad07a6a961277cf70c0d37d6f313000f5884 # v10.3.0
        with: { version: "${{ env.UV_VERSION }}", enable-cache: true, python-version: "3.14" }
      - run: uv lock --check
      - run: uv sync --locked --only-group lint
      - run: uv run --no-sync ruff check --output-format=github .
      - run: uv run --no-sync ruff format --check --diff .
      - name: README is PyPI-safe (dev mode)                       # ◀ added
        run: uv run --no-sync python scripts/check_readme.py README.md
      - name: zizmor (GitHub Actions security audit)
        env: { GH_TOKEN: "${{ github.token }}" }
        run: uvx zizmor@1.31.1 --min-severity=medium .github/workflows
  typecheck:
    runs-on: ubuntu-latest
    needs: lint
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with: { persist-credentials: false }
      - uses: astral-sh/setup-uv@1c37ad07a6a961277cf70c0d37d6f313000f5884 # v10.3.0
        with: { version: "${{ env.UV_VERSION }}", enable-cache: true, python-version: "3.14" }
      - run: uv sync --locked
      - run: uv run --no-sync mypy
  test:
    name: test (py${{ matrix.python-version }} · mcp ${{ matrix.mcp }} · ${{ matrix.os }})
    runs-on: ${{ matrix.os }}
    needs: [lint, typecheck]
    timeout-minutes: 20
    continue-on-error: ${{ matrix.experimental == true }}
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest]
        python-version: ["3.10", "3.11", "3.12", "3.13", "3.14"]
        mcp: ["2.2.0", "latest"]
        experimental: [false]
        include:
          - { os: macos-latest,   python-version: "3.14", mcp: "latest", experimental: false }
          - { os: windows-latest, python-version: "3.14", mcp: "latest", experimental: false }
          - { os: ubuntu-latest,  python-version: "3.15", mcp: "latest", experimental: true }
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with: { persist-credentials: false }
      - uses: astral-sh/setup-uv@1c37ad07a6a961277cf70c0d37d6f313000f5884 # v10.3.0
        with: { version: "${{ env.UV_VERSION }}", enable-cache: true, python-version: "${{ matrix.python-version }}" }
      - name: Pin mcp for this matrix cell (re-lock, CI only)
        env: { MCP_VERSION: "${{ matrix.mcp }}" }
        run: |
          if [ "$MCP_VERSION" = "latest" ]; then uv lock --upgrade-package mcp; else uv lock --upgrade-package "mcp==$MCP_VERSION"; fi
      - run: uv sync --no-default-groups --group test
      - name: Show resolved versions
        run: uv run --no-sync python -c 'import importlib.metadata as m; [print(f"{n:12s} {m.version(n)}") for n in ("mcp","mcp-types","httpx2","pydantic","anyio","click","rich","jsonschema")]'
      - name: mcpcheck --version matches pyproject.toml
        run: test "$(uv run --no-sync mcpcheck --version)" = "mcpcheck $(uv version --short)"
      - name: pytest (JUnit + coverage XML)
        env: { REPORT_TAG: "${{ matrix.os }}-py${{ matrix.python-version }}-mcp${{ matrix.mcp }}" }
        run: |
          mkdir -p reports
          uv run --no-sync pytest --junitxml="reports/junit-$REPORT_TAG.xml" --cov --cov-report=term-missing --cov-report="xml:reports/coverage-$REPORT_TAG.xml"
      - uses: actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9 # v7.0.2
        if: ${{ !cancelled() }}
        with: { name: "reports-${{ matrix.os }}-py${{ matrix.python-version }}-mcp${{ matrix.mcp }}", path: reports/, if-no-files-found: error, retention-days: 14 }
  lowest:
    name: lowest direct bounds (py3.10, --resolution lowest-direct)
    runs-on: ubuntu-latest
    needs: [lint, typecheck]
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with: { persist-credentials: false }
      - uses: astral-sh/setup-uv@1c37ad07a6a961277cf70c0d37d6f313000f5884 # v10.3.0
        with: { version: "${{ env.UV_VERSION }}", enable-cache: true, python-version: "3.10" }
      - run: uv lock --resolution lowest-direct
      - run: uv sync --no-default-groups --group test
      - run: uv run --no-sync pytest -q
  legacy-1x:                                                      # ◀ added: real mcp 1.x server for the handshake @legacy tests
    runs-on: ubuntu-latest
    needs: [lint, typecheck]
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with: { persist-credentials: false }
      - uses: astral-sh/setup-uv@1c37ad07a6a961277cf70c0d37d6f313000f5884 # v10.3.0
        with: { version: "${{ env.UV_VERSION }}", enable-cache: true, python-version: "3.13" }
      - run: uv sync --no-default-groups --group test
      - run: uv run --isolated --with "mcp<2" --with uvicorn python tests/fixtures/legacy1x_server.py --port 0 --url-file legacy.url &
      - run: sleep 2 && MCPCHECK_LEGACY_FIXTURE="$(cat legacy.url)" uv run --no-sync pytest -q -m legacy
  build:
    name: build sdist + wheel, smoke-test
    runs-on: ubuntu-latest
    needs: [lint, typecheck]
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with: { persist-credentials: false }
      - uses: astral-sh/setup-uv@1c37ad07a6a961277cf70c0d37d6f313000f5884 # v10.3.0
        with: { version: "${{ env.UV_VERSION }}", enable-cache: true, python-version: "3.14" }
      - run: uv build
      - run: uvx twine@7.0.0 check --strict dist/*
      - name: Wheel contains package data                          # ◀ added
        run: unzip -l dist/*.whl | grep -E "mcpcheck/(schema/report.v1.json|py.typed)" | wc -l | grep -q 2
      - name: Smoke-test the wheel in an isolated environment
        run: test "$(uv run --isolated --no-project --with dist/*.whl -- mcpcheck --version)" = "mcpcheck $(uv version --short)"
      - uses: actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9 # v7.0.2
        with: { name: dist, path: dist/, if-no-files-found: error }
```

### 11.4 `.github/workflows/release.yml` (draft; Trusted Publishing, no tokens)
```yaml
name: Release
on: { push: { tags: ["v*"] } }
permissions: { contents: read }
concurrency: { group: "release-${{ github.ref }}", cancel-in-progress: false }
env: { UV_VERSION: "0.13.1", FORCE_COLOR: "1" }
defaults: { run: { shell: bash } }
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with: { persist-credentials: false }
      - uses: astral-sh/setup-uv@1c37ad07a6a961277cf70c0d37d6f313000f5884 # v10.3.0
        with: { version: "${{ env.UV_VERSION }}", enable-cache: false, python-version: "3.14" }   # never restore caches into a release build
      - name: Tag must equal [project].version
        env: { TAG: "${{ github.ref_name }}" }
        run: |
          version="$(uv version --short)"
          [ "${TAG#v}" = "$version" ] || { echo "::error::tag '$TAG' != pyproject version '$version'"; exit 1; }
      - run: uv lock --check
      - run: uv sync --locked && uv run --no-sync pytest -q
      - name: README has no TO-BE-REPLACED markers and renders       # ◀ added
        run: uv run --no-sync python scripts/check_readme.py README.md CHANGELOG.md CONTRIBUTING.md --release
  build:
    runs-on: ubuntu-latest
    needs: verify
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with: { persist-credentials: false }
      - uses: astral-sh/setup-uv@1c37ad07a6a961277cf70c0d37d6f313000f5884 # v10.3.0
        with: { version: "${{ env.UV_VERSION }}", enable-cache: false, python-version: "3.14" }
      - run: uv build
      - run: uvx twine@7.0.0 check --strict dist/*
      - env: { TAG: "${{ github.ref_name }}" }
        run: test "$(uv run --isolated --no-project --with dist/*.whl -- mcpcheck --version)" = "mcpcheck ${TAG#v}"
      - uses: actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9 # v7.0.2
        with: { name: dist, path: dist/, if-no-files-found: error }
  publish-testpypi:
    runs-on: ubuntu-latest
    needs: build
    environment: { name: testpypi, url: "https://test.pypi.org/p/mcpcheck" }
    permissions: { id-token: write }
    steps:
      - uses: actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333 # v8.0.2
        with: { name: dist, path: dist/ }
      - uses: pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33 # v1.14.2
        with: { repository-url: "https://test.pypi.org/legacy/", skip-existing: true, print-hash: true }
  publish-pypi:
    runs-on: ubuntu-latest
    needs: publish-testpypi
    environment: { name: pypi, url: "https://pypi.org/p/mcpcheck" }
    permissions: { id-token: write }
    steps:
      - uses: actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333 # v8.0.2
        with: { name: dist, path: dist/ }
      - uses: pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33 # v1.14.2
        with: { print-hash: true }          # attestations on by default (PEP 740)
  github-release:
    runs-on: ubuntu-latest
    needs: publish-pypi
    permissions: { contents: write }
    steps:
      - uses: actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333 # v8.0.2
        with: { name: dist, path: dist/ }
      - env: { GH_TOKEN: "${{ github.token }}", TAG: "${{ github.ref_name }}" }
        run: |
          pre=""; case "$TAG" in *rc*|*a*|*b*) pre="--prerelease";; esac
          gh release create "$TAG" dist/* --repo "$GITHUB_REPOSITORY" --title "$TAG" --generate-notes --verify-tag $pre
```
One-time setup: PyPI and TestPyPI "pending publisher" entries (owner `suresh-jbt`, repo per U7, workflow `release.yml`, environments `pypi`/`testpypi`); GitHub environments `testpypi` and `pypi` (required reviewer on `pypi`); enable private vulnerability reporting; create labels `bug`, `verdict`, `enhancement`, `new-check`.

### 11.5 `dependabot.yml`, `.pre-commit-config.yaml`, `codeql.yml`
Dependabot: ecosystems `uv` (weekly, grouped minor/patch, cooldown 7 days, ignore `mcp` major), `github-actions` (weekly, grouped), `pre-commit` (monthly). Pre-commit: `pre-commit-hooks v6.0.0` (large files, case conflict, merge conflict, toml/yaml, debug statements, EOF, LF, trailing whitespace); `ruff-pre-commit v0.17.1` (`ruff-check --fix --exit-non-zero-on-fix`, `ruff-format`); `uv-pre-commit 0.13.1` (`uv-lock`); `check-jsonschema 0.38.2` (`check-github-workflows`, `check-dependabot`); `zizmor-pre-commit v1.31.1`; local pre-push hooks `uv run --frozen --no-sync mypy` and `pytest -q -x -m "not integration"`; `ci: skip: [mypy, pytest-fast, uv-lock, zizmor]`. CodeQL workflow for `python` and `actions` (optional; default setup is the zero-YAML alternative). Full drafts are in `scratchpad/agents/packaging-ci/drafts/` and are copied in M0.

### 11.6 First-release runbook (0.1.0)
1. U9 inputs in place (LICENSE holder, authors, CoC contact); U7 rename done; PyPI/TestPyPI pending publishers and GitHub environments created.
2. `uv lock && uv sync && uv run pytest && uv run mypy && uv run ruff check .` green locally on 3.13; `uv lock --upgrade-package "mcp==2.2.0" && uv sync && uv run pytest` green; `git checkout uv.lock && uv sync`.
3. Regenerate README real-run blocks (§12.3), run `python scripts/check_readme.py README.md CHANGELOG.md CONTRIBUTING.md --release`, `uv build && uvx twine@7.0.0 check --strict dist/*`.
4. CHANGELOG: rename `[Unreleased]` → `[0.1.0] - 2026-MM-DD`, add empty `[Unreleased]`, add compare links.
5. `uv version --short` == `0.1.0`; commit; `git tag v0.1.0 && git push --tags`.
6. Watch `release.yml`: verify → build → TestPyPI → (approve) PyPI → GitHub Release; inspect the TestPyPI page rendering before approving `pypi`.
7. Post-release smoke: `uvx mcpcheck@0.1.0 --version`, `uvx mcpcheck@0.1.0 http://127.0.0.1:<port>/mcp` against the fixture; confirm badges; open `[Unreleased]` with the 3.10 sunset note.

---

## 12. README and documentation

### 12.1 Rules
Pure GFM (PyPI renders with `readme_renderer 46.0`/`comrak 0.0.16`/`nh3 0.3.7`; HTML beyond comments is stripped); every link absolute (`https://github.com/suresh-jbt/mcp-check/blob/main/...`); in-page anchors only to headings; hero = static `console` block; severity vocabulary `must`/`should`/`best-practice` (= report `level`); spec links with verified anchors (Appendix B); Origin rule linked to the transport **page** (heading id contains `&`); sentence-case headings, no TOC (< 350 lines), no emoji; placeholders carry the exact marker `TO-BE-REPLACED-WITH-REAL-RUN` inside HTML comments and `scripts/check_readme.py --release` fails while any remain. Badge row: PyPI, pyversions, CI, license, MCP-spec (five). The draft below is the readme-docs section's text with the plan's decisions applied (eight checks, `--probe safe` wording, `--legacy-probes`, tests-only reference server, `speaks` instead of `negotiated`, final-line wording, troubleshooting paragraph).

### 12.2 README.md draft
````markdown
# mcpcheck

[![PyPI](https://img.shields.io/pypi/v/mcpcheck.svg)](https://pypi.org/project/mcpcheck/)
[![Python versions](https://img.shields.io/pypi/pyversions/mcpcheck.svg)](https://pypi.org/project/mcpcheck/)
[![CI](https://github.com/suresh-jbt/mcp-check/actions/workflows/ci.yml/badge.svg)](https://github.com/suresh-jbt/mcp-check/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/pypi/l/mcpcheck.svg)](https://github.com/suresh-jbt/mcp-check/blob/main/LICENSE)
[![MCP spec 2026-07-28](https://img.shields.io/badge/MCP%20spec-2026--07--28-blue)](https://modelcontextprotocol.io/specification/2026-07-28)

Check that an MCP server conforms to the **2026-07-28** revision of the [Model Context Protocol](https://modelcontextprotocol.io/specification/2026-07-28) over Streamable HTTP. Read-only. Zero config. One command.

<!-- TO-BE-REPLACED-WITH-REAL-RUN: verbatim `mcpcheck` output against tests/fixtures/misbehaving_server.py with toggles omit_ttl, omit_cache_scope, one invalid inputSchema, discover_no_server_info; NO_COLOR=1 COLUMNS=100 -->
```console
$ uvx mcpcheck https://api.example.com/mcp

mcpcheck 0.1.0 · MCP 2026-07-28 · https://api.example.com/mcp
server: example-server 2.4.0 · speaks 2026-07-28 · 8 checks

✓ handshake     Server does not require `initialize`
✓ session       No `Mcp-Session-Id` issued or expected; lists identical across 3 connections
! discover      `server/discover` responds correctly (1 warning)
    ! result._meta["io.modelcontextprotocol/serverInfo"]: server does not identify itself  [should]
    spec: https://modelcontextprotocol.io/specification/2026-07-28/server/discover#discoverresult
✓ protocol      Header validation and version errors conform
✗ schemas       1 of 12 tools have invalid inputSchema (+3 more issues)
    ✗ tool:search_orders: inputSchema is not a valid JSON Schema 2020-12: 'nonsense' is not valid under any of the given schemas  [must]
    ! tool:get_invoice: outputSchema absent; optional per spec  [best-practice]
    … and 2 more (use --verbose or --json)
    spec: https://modelcontextprotocol.io/specification/2026-07-28/server/tools#tool
✓ errors        Tool errors carry actionable messages
✗ cache         2 cache-hint violations: prompts/list ttlMs missing; prompts/list cacheScope missing
    ✗ prompts/list: result has no ttlMs  [must]
    ✗ prompts/list: result has no cacheScope  [must]
    spec: https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/caching#cacheable-results
✓ origin        Origin header validated

2 of 8 checks failed, 1 warning. See --json for details.
```

## Why mcpcheck

The 2026-07-28 revision breaks every Streamable HTTP server that still requires the old handshake:

- **No handshake, no sessions.** `initialize` and `Mcp-Session-Id` are gone. Every request carries its protocol version and client capabilities in `_meta`, and list results must not vary per connection ([changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog#major-changes)).
- **`server/discover` is mandatory** ([discovery](https://modelcontextprotocol.io/specification/2026-07-28/server/discover)).
- **Headers became part of the protocol.** `MCP-Protocol-Version` on every POST, `Mcp-Method` on every request, `Mcp-Name` on `tools/call`; mismatches are rejected with HTTP 400 ([transport](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http#standard-request-headers)).
- **Cache hints are required.** `ttlMs` and `cacheScope` MUST appear on `server/discover` and every `*/list` result ([caching](https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/caching#cacheable-results)).
- **Origin validation is a MUST**, with a mandated HTTP 403 for forged origins ([transport, Security & Endpoint](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)).

A server written for 2025-11-25 still answers `initialize` and looks healthy in old clients while silently failing new ones. `mcpcheck` tells you, in a few seconds and without executing a single tool, which requirements your server already meets and points you at the exact clause for each one it does not.

## Installation

```shell
uvx mcpcheck https://api.example.com/mcp        # run without installing (uv)
pipx run mcpcheck https://api.example.com/mcp   # pipx
uv tool install mcpcheck                         # or install as a tool
pip install mcpcheck
```

Requires Python 3.10 or newer (3.10 support ends at 1.0 or when the `mcp` SDK drops it) and is built on the official [`mcp`](https://pypi.org/project/mcp/) Python SDK (2.x). Pin a version in CI with `uvx mcpcheck@0.1.0`.

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

## What it checks

Each check is independent, cites the clause it enforces, and classifies every finding by normative weight: **must** (spec MUST / MUST NOT, fails the run), **should** (spec SHOULD, a warning), or **best-practice** (`mcpcheck`'s opinion where the spec is permissive, a warning). Warnings affect the exit code only with `--strict`.

| Check | What `mcpcheck` verifies | Spec | Levels |
|-------|--------------------------|------|--------|
| `handshake` | A `tools/list` with per-request `_meta` and the required headers succeeds on a fresh connection with no `initialize` first, and the result is 2026-shaped. Servers that *also* answer `initialize` (dual-era) pass; servers that *require* it fail. | [Statelessness](https://modelcontextprotocol.io/specification/2026-07-28/basic#statelessness), [Backward compatibility](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning#backward-compatibility-with-initialization-based-versions) | must / should |
| `session` | No `Mcp-Session-Id` is minted, echoed or required; a bogus one is ignored; `tools/list`, `prompts/list` and `resources/list` return the same set on 3 fresh connections and after other requests on the same connection (MUST NOT vary); tools come back in a deterministic order (SHOULD). | [Earlier Streamable HTTP revisions](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http#earlier-streamable-http-revisions), [Tools capabilities](https://modelcontextprotocol.io/specification/2026-07-28/server/tools#capabilities) | must / should |
| `discover` | `server/discover` is implemented (MUST); the result has `resultType`, `supportedVersions`, a `capabilities` object and valid cache hints; `_meta` carries `io.modelcontextprotocol/serverInfo` (SHOULD); every capability implied by a non-empty list result is declared and every declared capability answers its list (MUST). | [Discovery](https://modelcontextprotocol.io/specification/2026-07-28/server/discover#discoverresult), [`_meta`](https://modelcontextprotocol.io/specification/2026-07-28/basic#_meta) | must / should / best-practice |
| `protocol` | A header/`_meta` version mismatch yields 400 + `-32020`; an unsupported version yields 400 + `-32022` with `data.supported`; a missing `Mcp-Method` or `_meta` field is rejected; an unknown method yields 404 + `-32601`. A header-less request that is *accepted* is a warning (servers that still serve pre-2025-06-18 clients may do that). | [Protocol version header](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http#protocol-version-header), [Server validation](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http#server-validation) | must / should |
| `schemas` | Every tool's `inputSchema` is a JSON Schema object with root `type: "object"`, valid under its declared or default (2020-12) dialect, without fetching network `$ref`s (MUST); `x-mcp-header` annotations obey their constraints (MUST); tool names follow the naming rules (SHOULD); every tool declares an `outputSchema` (best practice: the spec says *Optional*). | [Tool](https://modelcontextprotocol.io/specification/2026-07-28/server/tools#tool), [Schema validation](https://modelcontextprotocol.io/specification/2026-07-28/basic#schema-validation), [`x-mcp-header`](https://modelcontextprotocol.io/specification/2026-07-28/server/tools#x-mcp-header) | must / should / best-practice |
| `errors` | Calling an annotated read-only tool with arguments that violate its own `inputSchema` is rejected, either as a JSON-RPC error (the spec's example uses `-32602`) or as a result with `isError: true`; both are conformant. A success on invalid input fails (servers MUST validate all tool inputs). The error message is a concise, specific sentence rather than a stack trace (best practice). | [Error handling](https://modelcontextprotocol.io/specification/2026-07-28/server/tools#error-handling) | must / should / best-practice |
| `cache` | `tools/list`, `prompts/list`, `resources/list` and `resources/templates/list` results carry an integer `ttlMs >= 0` and a `cacheScope` of exactly `public` or `private` on every page, with the same `cacheScope` across pages (MUST); a `public` list whose item names look user-specific is flagged (best practice). `server/discover` hints are checked by `discover`. | [Cacheable results](https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/caching#cacheable-results), [Security considerations](https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/caching#security-considerations) | must / best-practice |
| `origin` | A request with `Origin: https://evil.example.com` is answered with HTTP 403 (MUST). Rejecting same-origin or absent `Origin`, or accepting `Origin: null`, is reported as a best-practice warning; the spec is silent about those. | [Security & Endpoint](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http) | must / best-practice |

`mcpcheck` is deliberately opinionated in a few labelled places (`best-practice` in every report): it recommends an `outputSchema` on every tool, its `errors` heuristics encode what makes an error message useful to a language model, and it warns about `public` cache scopes and `Origin` handling the spec does not regulate. Nothing else in `mcpcheck` fails a server for behaviour the specification permits.

## Output

The human report prints one line per check with a glyph (`✓` pass, `✗` fail, `!` warning, `?` inconclusive, `E` error), the first findings under each failing check, and the spec URL. With `--json`, or `--output FILE`, you get one JSON document:

<!-- TO-BE-REPLACED-WITH-REAL-RUN: real --json report trimmed to two checks; must validate against schema/mcpcheck-report.v1.schema.json -->
```json
{ "schema_version": 1, "mcpcheck_version": "0.1.0", "spec_revision": "2026-07-28", "server": "https://api.example.com/mcp", "...": "..." }
```

Finding `code`s (`cache.ttl_missing`, `schemas.input_schema_invalid`, …) are stable identifiers you can grep or suppress on. The report format is versioned by `schema_version`; the JSON Schema ships in the repository as [`schema/mcpcheck-report.v1.schema.json`](https://github.com/suresh-jbt/mcp-check/blob/main/schema/mcpcheck-report.v1.schema.json) and is printed by `mcpcheck --json-schema`. Your token is never written to the report; only `"auth": "bearer"` is.

## Exit codes

| Code | Meaning |
|:----:|---------|
| `0` | Every selected check passed. Warnings and inconclusive checks (nothing could be judged, e.g. HTTP 401 without `--token`) are allowed unless `--strict` |
| `1` | At least one selected check failed. With `--strict`, a warning or an inconclusive check also produces `1` |
| `2` | `mcpcheck` could not run: bad arguments, connection refused or timed out, TLS failure, or an internal error |

## Use in CI
(the GitHub Actions snippet of §8.8, verbatim)

## Options
<!-- TO-BE-REPLACED-WITH-REAL-RUN: regenerated from `mcpcheck --help`; tests/test_readme.py keeps it in sync -->
(table of §8.1, columns Option / Description / Default)

## Safety: what mcpcheck sends

`mcpcheck` is a read-only client. By default it sends only: `server/discover`; `tools/list`, `prompts/list`, `resources/list`, `resources/templates/list` (following `nextCursor`); `tools/call` with arguments that violate the tool's own `inputSchema`, **only** to tools whose annotations say `readOnlyHint: true` or `destructiveHint: false` (the spec's default for `destructiveHint` is `true`, so unannotated tools are never called — use `--probe all` to opt in, `--probe none` to disable), plus one call to a tool name that does not exist; header probes with a wrong version, a missing or miscased header, a missing `_meta` field or an unknown method, which no server can execute; and one `OPTIONS` request. With `--legacy-probes` it also sends one legacy `initialize` request (which may create a session on dual-era servers) and one headers-only `GET`. It never reads a resource, renders a prompt, sends `DELETE`, follows a cross-origin redirect or fetches a `$ref`. Your token goes only to `SERVER_URL` (and same-origin `307`/`308` redirects) and is redacted from `--verbose` output. TLS is verified unless you pass `--insecure`. Expect these requests in your server logs; the `origin` check's forged-`Origin` request is logged as a warning by SDK servers. Only run `mcpcheck` against servers you own or are authorized to test.

## Troubleshooting

`? … Not evaluated: HTTP 401` — the server needs authentication: pass `--token` or set `MCPCHECK_TOKEN`. `HTTP 403` — the server rejects requests without an `Origin` or your credentials lack access: try `--origin https://<allowed-origin>`. `redirects (302) to …` — run against the final URL. `No probe-safe tools` — annotate your tools with `readOnlyHint`/`destructiveHint` or use `--probe all`. `Run incomplete … ConnectError` — the endpoint is unreachable from this machine.

## Limitations and non-goals
(§4 non-goals, as bullets; includes "not the official suite: [`modelcontextprotocol/conformance`](https://github.com/modelcontextprotocol/conformance)")

## Compatibility
| | Supported |
|---|---|
| MCP specification | **2026-07-28**. `2026-10-13` is listed in the spec's version index but has no published pages as of October 2026 |
| Servers | Any Streamable HTTP server, including dual-era servers that also answer `initialize`; both `application/json` and `text/event-stream` responses |
| Python | 3.10–3.14 (3.15 experimental in CI); 3.10 support ends at 1.0 or when `mcp` 2.x drops it |
| `mcp` SDK | 2.2 to any 2.x; CI tests 2.2.0 and the newest 2.x |
| Platforms | Linux, macOS, Windows |

Where the official SDK hides a non-conformance as a client (it fills in missing `ttlMs`/`cacheScope`, echoes `Mcp-Session-Id`, drops tools with invalid `x-mcp-header` values and collapses HTTP errors), `mcpcheck` inspects the raw HTTP exchange instead.

## Testing locally against the reference server

The repository ships a small, fully conformant reference server built on the official SDK (used by the test suite):

<!-- TO-BE-REPLACED-WITH-REAL-RUN -->
```console
$ git clone https://github.com/suresh-jbt/mcp-check && cd mcp-check && uv sync
$ uv run python -m tests.fixtures.reference_server --port 0
mcpcheck reference server 0.1.0 listening on http://127.0.0.1:54321/mcp
$ uv run mcpcheck http://127.0.0.1:54321/mcp
…
All 8 checks passed in 1.3s.
```
Flags: `--port N` (0 = free port), `--json-response`, `--stateful` (dual-era), `--no-origin-check`, `--require-token TOKEN`. The suite also runs every check against a deliberately misbehaving server; see [CONTRIBUTING.md](https://github.com/suresh-jbt/mcp-check/blob/main/CONTRIBUTING.md).

## Contributing / License
Bug reports, wrong-verdict reports (attach `--json`) and new-check proposals (must cite a clause) are welcome: [CONTRIBUTING.md](…), [SECURITY.md](…), [Contributor Covenant](…/CODE_OF_CONDUCT.md). MIT — see [LICENSE](…).
````

### 12.3 TO-FILL-FROM-REAL-RUN spots
| # | Spot | Generate with | Acceptance |
|---|---|---|---|
| 1 | Hero block | `NO_COLOR=1 COLUMNS=100 uv run mcpcheck <misbehaving-url>` with toggles `omit_ttl, omit_cache_scope, tools=[one invalid inputSchema + missing outputSchema], discover_no_server_info` | byte-identical to the renderer; no token |
| 2 | JSON block | `mcpcheck <url> --json \| jq '.checks \|= map(select(.name=="cache"))'` | validates with `check-jsonschema`; note the trim |
| 3 | Options table | `scripts/gen_options_table.py` over `click.Context.to_info_dict()`; `tests/test_readme.py` fails on drift | every `--help` option present |
| 4 | Testing-locally blocks | real fixture start line + all-pass run | exit 0; final line equals renderer's |
| 5 | `uvx mcpcheck@0.1.0` pins | `uv version --short` | equals the tag |
| 6 | `--help` snapshot in §8.1 | `COLUMNS=80 mcpcheck --help` | snapshot test |

### 12.4 Companion files
`CONTRIBUTING.md` (setup `uv sync`, `uvx pre-commit install`; re-lock recipe for mcp 2.2.0; running tests/markers; linting; "Adding a check" six steps incl. codes registry, `never` list; releasing). `CHANGELOG.md` (Keep a Changelog 2.0.0; `[Unreleased]` → Added bullets for the eight checks, severity model, `--json`/`--output`/`--json-schema`, exit codes, option list as frozen in §8.1, 3.10 sunset note; feature bullets carry the TO-BE-REPLACED marker until the CLI is frozen in code). `SECURITY.md` (private vulnerability reporting link; in-scope: token leakage incl. via redirects, malicious-server DoS (unbounded SSE, pathological schema, network `$ref`), any valid-input tool call / `resources/read` / `prompts/get` / `DELETE` / cross-origin redirect — "a probe that could have side effects is a bug of the highest severity"; TLS skipped without `--insecure`; out of scope: findings about the tested server, `--insecure`/`--probe all` consequences, dependency CVEs mcpcheck does not trigger). `CODE_OF_CONDUCT.md` = Contributor Covenant 2.1 verbatim, line 40 contact from U9. `.github/ISSUE_TEMPLATE/{bug_report.yml, false_positive.yml (mandatory `--json` and spec clause), new_check.yml, config.yml (blank issues disabled; links to spec, official conformance suite, private reporting)}` — schema-validated drafts in `scratchpad/agents/readme-docs/companions/`. `.github/PULL_REQUEST_TEMPLATE.md` (summary, test plan, checklist incl. CHANGELOG, README tables, stable codes, read-only guarantee). `CODEOWNERS` `* @suresh-jbt`.

### 12.5 PyPI rendering checklist (automated by `scripts/check_readme.py`, items 1–6)
1 no relative links/images; 2 no HTML outside nh3's allowlist, no `target=`, no `data:` images, no mermaid; 3 every `#anchor` has a heading; 4 `readme_renderer.markdown.render(text, variant="GFM")` is not `None`; 5 `--release`: zero markers (README, CHANGELOG, CONTRIBUTING); 6 inline code ignored by the tag rule; 7 `uv build && uvx twine@7.0.0 check --strict dist/*`; 8 manual: eyeball `out.html`/TestPyPI page (hero alignment, escaped pipes `--probe safe\|all\|none`); 9 remember the PyPI description is a per-release snapshot.

---

## 13. Implementation roadmap

Dependencies flow M0 → M1 → M2 → M3 → M4 → M5 → M6 → M7; M2 can start in parallel with M1. Sizes are source+test lines, rough.

| M | Goal | Files | Acceptance (commands) | Size |
|---|---|---|---|---|
| **M0 Scaffold** | installable package, contracts frozen | `pyproject.toml`, `uv.lock`, `.python-version`, `LICENSE` (U9), `README.md` (draft with markers), `CHANGELOG.md`, `.gitignore`; `src/mcpcheck/{__init__,__main__ (stub: --version/--help/--list-checks/--json-schema only),compat,sdk_compat,spec_urls,models}.py`, `checks/{__init__,codes}.py` (every code of §7 registered), `schema/report.v1.json`, repo `schema/…`, `py.typed`; `scripts/gen_report_schema.py`; `tests/{__init__,conftest}.py`, `test_models.py`, `test_report_schema.py`, `test_spec_urls.py`, `test_sdk_contract.py`, `test_py310.py`, `test_codes.py`; `.github/{workflows/ci.yml,workflows/release.yml,dependabot.yml}`, `.pre-commit-config.yaml` | `uv sync && uv run mcpcheck --version` → `mcpcheck 0.1.0`; `uv run mcpcheck --json-schema \| uvx check-jsonschema --check-metaschema -`; `uv run pytest -q`; `uv run mypy`; `uv run ruff check .`; CI lint/typecheck/build green incl. wheel-contents assertion | ~900 |
| **M1 Wire layer** | raw client that sees everything | `wire.py`, `client.py`, `tests/helpers/scripted.py`, `tests/test_wire.py`, `tests/test_client.py` | MockTransport tables pass: JSON/SSE parsing (multi-line, CRLF, comments, `id:`), header overrides/omissions, `main` tamper assertion, redirect policy (307 followed ≤5, 302/cross-origin blocked), 4xx preserved, body cap, `classify_reply` matrix (§6.6 rows), redaction; `test_client::test_total_budget_vs_keepalive_sse` (L3, needs M2) | ~900 |
| **M2 Fixtures** | servers to test against | `tests/fixtures/{__init__,reference_server,misbehaving_server}.py`, `conftest.py` ThreadServer + ASGI fixtures, `tests/test_fixtures.py` | default `Toggles()` and the reference app answer the raw ladder conformantly under ASGITransport; each toggle flips exactly its wire behaviour; `python -m tests.fixtures.reference_server --port 0` prints the URL and serves; no thread/socket leaks | ~1100 |
| **M3 Read-only checks + runner + output** | first useful binary | `checks/base.py`, `runner.py`, `output.py`, `__main__.py` (full), `checks/{handshake,session,discover,cache,schemas}.py`, `tests/test_{handshake,session,discover,cache,schemas,output,cli}.py`, `tests/golden/`, `tests/test_integration.py` (first rows) | `uv run mcpcheck <reference-url> --skip errors,origin,protocol` → exit 0, 5 `pass`; golden `all_pass.txt`; every §10.4 row for these checks; `--json` validates; exit-code matrix via fake runner; token never leaks | ~2400 |
| **M4 Probing checks** | complete check set | `probe_policy.py`, `checks/{errors,origin,protocol}.py` (protocol per U1), `tests/test_probe_policy.py`, `tests/test_{errors,origin,protocol}.py`, remaining `test_integration.py` rows incl. `test_acceptance_reference_all_pass` and `test_request_log_contains_only_allowed_requests` | `uv run mcpcheck <reference-url>` → exit 0, all 8 `pass`; property test on generated payloads; `destructive_norequired` never requested under `safe`; `--probe all` behaviour; misbehaving rows for errors/origin/protocol | ~1600 |
| **M5 Hardening** | matrix green | `compat.py` finishing, 3.10 fixes, Windows/macOS fixes, `tests/test_py310.py`, `legacy1x_server.py` + `@legacy` tests, `--durations` tuning | CI matrix green on 3.10–3.14 × mcp 2.2.0/latest, macOS/Windows cells, lowest-bounds job, legacy-1x job; suite < 30 s locally | ~300 |
| **M6 Docs** | release-ready repo | README real-run blocks, `scripts/check_readme.py`, `scripts/gen_options_table.py`, `tests/test_readme.py`, `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, issue/PR templates, `CODEOWNERS`, repo settings (private reporting, labels, environments), U7 rename | `python scripts/check_readme.py README.md CHANGELOG.md CONTRIBUTING.md --release` ok; `twine check --strict`; TestPyPI page reviewed | ~500 |
| **M7 Release 0.1.0** | on PyPI | CHANGELOG roll, tag `v0.1.0` | `release.yml` green; `uvx mcpcheck@0.1.0 --version`; `uvx mcpcheck@0.1.0 <fixture-url>` exits 0 with all pass; badges render | — |

**Definition of done (handoff acceptance criteria, updated).**
| Criterion | Status in this plan | Verified by |
|---|---|---|
| `pip install mcpcheck` works from PyPI | M7 | post-release smoke (`uvx mcpcheck@0.1.0`) |
| `mcpcheck http://localhost:8765/mcp` exits 0 against the reference server | reference server is in-repo (`tests/fixtures/reference_server.py --port 0`; `--port 8765` works where free) | `test_acceptance_reference_all_pass` (exit 0, every check `pass`, both response modes, both SDK versions) |
| all 7 checks pass against it | all **8** (or 7 if U1 rejected) pass | same |
| `--json` validates against the schema | shipped schema v1; identical repo copy | `test_report_schema.py`, `test_cli::test_json_validates` |
| exit codes 0/1/2 correct | §6.10 table (+130 SIGINT; `--strict` promotion) | `test_cli` exit matrix via fake runner + subprocess tests |
| human output readable and matches format | `{glyph} {name:<14}{summary}` as the handoff sample; deviations listed in §8.5 | golden files |
| CI green on 3.10–3.13 | 3.10–3.14 + experimental 3.15, mcp 2.2.0 × latest, macOS/Windows | ci.yml |
| README renders on GitHub and PyPI | §12 rules + `check_readme.py` + `twine check --strict` | CI lint + release verify |
| MIT LICENSE present | U9 holder | build job (`license-files`) |
| `--version` matches pyproject.toml | single source | CI step in every matrix cell + release verify |

---

## 14. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Public read-only promise vs. `errors` probing a lenient server | default policy calls only explicitly annotated tools with proven-invalid payloads; request-log test; SECURITY.md scope |
| Spec-site anchors (Mintlify ids like `#_meta`, `#security-&-endpoint`, `#$ref-resolution`) change | all anchors HTTP-200 today; `tests/test_spec_urls.py -m network` monthly scheduled job; README links pages where ids are odd |
| `mcp.shared.inbound` helpers are undocumented and may move in a 2.x release | `test_sdk_contract.py` on both matrix cells; `sdk_compat.require_sdk()` gives an actionable error; Dependabot ignores `mcp` majors |
| Python 3.10 EOL; SDK main requires ≥3.11 | U6; `compat.py`; the 3.10 cell fails loudly when `mcp` stops installing → drop 3.10 in the next minor |
| 2026-10-13 revision publishes | `PROTOCOL_VERSION`/`spec_urls.py` are the only version constants; per-version check variants are future work (§15) |
| httpx2 per-chunk read timeouts never fire on keepalive SSE | total per-request `anyio.fail_after` in `RawClient`; L3 test with `endless_sse` |
| anyio cancel-scope misuse (SDK session across checks) | no runtime `ClientSession`; every `async with` opens and closes inside one check |
| WAF/CDN answering 403/429 to unusual requests | control probes with identical headers → inconclusive, never false pass/fail; origin last in order |
| Load balancer with divergent backends fails `session` | details show per-connection sets; documented in README troubleshooting |
| Heuristic false positives (`USER_TOKENS`, `SENSITIVE_RE`, actionability A1–A5) | all `best-practice`, warning-only, labelled; `--strict` opt-in |
| Documentation drift (README promises vs. code) | options table test; markers + release gate; goldens embedded from real runs |
| Windows/macOS fixture behaviour (thread uvicorn, `--import-mode=importlib`) UNVERIFIED locally | CI cells are the first real test (M5) |
| Rate limiting against production servers (20–60 requests) | sequential; one 429 retry; README footprint paragraph |
| Golden/schema churn before 0.1.0 | codes registered in M0; `schema_version` policy; `--update-goldens` |

---

## 15. Open questions and future work

- **2026-10-13 revision**: once published, decide between a `--protocol-version` flag with per-version rule tables vs. a new major; today only constants are pluggable. Re-check era rules, header rules and the Origin wording.
- **stdio transport**: out of scope; would need a different `RawClient` and no Origin/session checks.
- **`resources/read` cache hints**: the one CA-1 operation never verified; candidate opt-in `--allow-resource-read URI`.
- **Anonymous (token-less) cache probe**: dropped for 0.1.0; candidate `--probe-anonymous` to detect `public` lists that vary by authorization (CA:107-110, TL:66-69).
- **`--transcript FILE`**: write the `Exchange` list as JSON for bug reports instead of only stderr text.
- **Legacy (mcp 1.x) interop labels**: fingerprints verified only against mcp 1.30; TypeScript/Java servers may word rejections differently (verdict unaffected).
- **Prompt rendering / `prompts/get` with invalid arguments**: analogous to the errors check; excluded by the handoff.
- **`mcpcheck selftest`**: boot the reference fixture and run all checks (needs shipping the fixture, U5).
- **UNVERIFIED items carried**: `asgi-lifespan` fallback; `SSL_CERT_FILE` via httpx2 `trust_env`; GitLab `allow_failure.exit_codes`; browser `Origin: null` behaviour; `schema#error`/`schema#toolannotations` anchors (Appendix B marks them); Windows/macOS fixture runs; PyPI hero pixel alignment.

---

## Appendix A — Verified SDK cheat-sheet (executed 2026-10-16 on `venv-mcp220` and `venv-mcp230`; byte-identical unless noted)

**Environment.** mcp 2.2.0 / 2.3.0; mcp-types 2.2.0 / 2.3.0; httpx2 2.13.1; anyio 4.15.1; pydantic 2.14.1; click 8.5.0; jsonschema 4.26.0; Python 3.13.5. Scripts: `scratchpad/agents/synthesizer/verify_sdk.py`, `verify_tail.py`.

**Imports (all exist on both versions).**
```python
import mcp_types  # NOT mcp.types for constants (both exist; mcp_types is the canonical package)
from mcp.client.session import (
    ClientSession,
    MCP_METHOD_HEADER,
    MCP_NAME_HEADER,
    MCP_PROTOCOL_VERSION_HEADER,
    MODERN_PROTOCOL_VERSIONS,
)
from mcp.client.streamable_http import (
    streamable_http_client,
    create_mcp_http_client,
    MCP_SESSION_ID,
)
from mcp.shared.inbound import (
    encode_header_value,
    find_invalid_x_mcp_header,
    x_mcp_header_map,
    mcp_param_headers,
    ERROR_CODE_HTTP_STATUS,
)
from mcp.shared.exceptions import MCPError, MCPDeprecationWarning
from mcp_types.methods import validate_server_result, SERVER_RESULTS, CACHEABLE_METHODS
```
**Constants.** `mcp_types.LATEST_PROTOCOL_VERSION == "2026-07-28"`; `MODERN_PROTOCOL_VERSIONS == ("2026-07-28",)`; headers `"mcp-method"`, `"mcp-name"`, `"mcp-protocol-version"`, `MCP_SESSION_ID == "mcp-session-id"`; meta keys `io.modelcontextprotocol/protocolVersion`, `…/clientInfo`, `…/clientCapabilities`, `…/serverInfo`; codes `HEADER_MISMATCH=-32020`, `MISSING_REQUIRED_CLIENT_CAPABILITY=-32021`, `UNSUPPORTED_PROTOCOL_VERSION=-32022`, `METHOD_NOT_FOUND=-32601`, `INVALID_PARAMS=-32602`; `ERROR_CODE_HTTP_STATUS == {-32700:400, -32602:400, -32601:404, -32600:400, -32022:400, -32021:400, -32020:400}`; `CACHEABLE_METHODS == {prompts/list, resources/list, resources/read, resources/templates/list, server/discover, tools/list}`.

**Signatures.** `streamable_http_client(url: str, *, http_client: httpx2.AsyncClient | None = None, terminate_on_close: bool = True)` on 2.2.0; 2.3.0 adds `max_sse_event_size: int | None = 1048576` — **never pass it**. `create_mcp_http_client(headers=None, timeout: httpx2.Timeout | None = None, auth: httpx2.Auth | None = None) -> httpx2.AsyncClient`. `anyio.fail_after(delay: float | None, shield: bool = False, reason: str | None = None)` raises builtin `TimeoutError`. `httpx2.AsyncClient(auth, params, headers, cookies, verify, cert, http1, http2, proxy, mounts, timeout, follow_redirects, limits, max_redirects, …)`; `httpx2.EventSource`, `MockTransport`, `ASGITransport` exist. `httpx2.TransportError` subclasses: `TimeoutException`, `NetworkError`, `ProxyError`, `UnsupportedProtocol`, `ProtocolError`, `SSEError`; `InvalidURL` is not an `HTTPError`. `click.UsageError.exit_code == 2`; `click.testing.CliRunner.__init__(self, charset, env, echo_stdin, catch_exceptions, capture)` — **no `mix_stderr`**.

**Helpers.** `encode_header_value("héllo") == "=?base64?aMOpbGxv?="`; `encode_header_value("=?base64?literal?=") == "=?base64?PT9iYXNlNjQ/bGl0ZXJhbD89?="` (sentinel rule honoured); ASCII passes through. `find_invalid_x_mcp_header({"type":"object","properties":{"amount":{"type":"number","x-mcp-header":"Amount"}}}) == "property 'amount': x-mcp-header is only permitted on integer/string/boolean properties (got 'number')"`. `mcp_param_headers(x_mcp_header_map(schema), {"region": 12345}) == {"Mcp-Param-Region": "12345"}`. `("tools/call","2026-07-28") in SERVER_RESULTS` (union `CallToolResult | InputRequiredResult`); `("server/discover","2025-11-25") not in SERVER_RESULTS`.

**Validator behaviour.** `validate_server_result("tools/list","2026-07-28",{"tools":[]})` raises with locs `cacheScope, resultType, ttlMs` (`missing`); `ttlMs:-1` → `greater_than_equal`; `cacheScope:"user"` → `literal_error`; **accepts** `ttlMs:"5"`, `5.0`, `True` (type-lax → raw checks mandatory). `ListToolsResult.model_validate({"tools":[]})` succeeds with `ttl_ms=0, cache_scope="private", result_type="complete"`, `model_fields_set == {"tools"}` (masks omissions). `ToolAnnotations()` → `read_only_hint is None`, `destructive_hint is None` (test `is True`/`is False`, never truthiness; spec defaults false/true). `Implementation.model_validate({"name":"x"})` fails (`version` required). `ClientCapabilities().model_dump(by_alias=True, exclude_none=True) == {}` (valid `_meta` value). `ClientSession.adopt(DiscoverResult(supported_versions=["2026-07-28"], capabilities=ServerCapabilities()))` sets protocol version `2026-07-28` with no traffic; with `["2025-11-25"]` raises `RuntimeError: No mutually supported modern protocol version`.

**Gotchas (from the sections' executed experiments).** `ClientSession` pre-adopt requests are legacy-shaped; non-JSON 4xx → `MCPError(-32603, "Server returned an error response")`; `_clamp_inbound_ttl` floors negative `ttlMs` to 0 before validation; `discover()` after `adopt()` is a no-op; `list_tools()` drops invalid-`x-mcp-header` tools after modern negotiation with only a `client` logger warning; `ClientMessageMetadata.headers` is not forwarded; `mcp.client.client.Client(mode="auto")` falls back to `initialize()` and caches list results; keeping `streamable_http_client` open across anyio cancel scopes raises `RuntimeError` (reviewer-reproduced); `import mcp.cli` needs typer — never import; `MCPServer.streamable_http_app(host="0.0.0.0")` disables Origin auto-protection; SDK server routes header-less requests to the legacy path (dual-era by construction) and a header-less GET opens an endless SSE stream; SDK 403 body is text `Invalid Origin header` without content-type; `Draft202012Validator.check_schema` recurses to ~96 levels OK, `RecursionError` at ~128 (CPython 3.13, limit 1000); `validator_for({"$schema": 42})` raises `AttributeError`; `json.loads` survives 3000 nesting levels; httpx2 raises `UnicodeEncodeError` for raw non-ASCII header values; `enum.StrEnum`/`except*`/`asyncio.timeout` missing on CPython 3.10.20.

**2.2.0 vs 2.3.0.** Only: `max_sse_event_size` kwarg (2.3.0); pre-negotiation `params._meta: {}` quirk (2.2.0; irrelevant — we never use the session); legacy `initialize` reply includes `capabilities.experimental: {}` on 2.2.0 only. All wire behaviour relied upon is identical.

## Appendix B — Spec URL map (`spec_urls.py`; every anchor HTTP-200 on 2026-10-16 unless marked)

| Constant | URL |
|---|---|
| `SPEC_BASE` | `https://modelcontextprotocol.io/specification/2026-07-28` |
| `SPEC_CHANGELOG`, `SPEC_CHANGELOG_MAJOR`, `SPEC_CHANGELOG_MINOR` | `{SPEC_BASE}/changelog`, `#major-changes`, `#minor-changes` |
| `SPEC_BASIC` | `{SPEC_BASE}/basic` |
| `SPEC_STATELESSNESS` | `{SPEC_BASIC}#statelessness` |
| `SPEC_RESULT_RESPONSES`, `SPEC_ERROR_RESPONSES`, `SPEC_ERROR_CODES` | `{SPEC_BASIC}#result-responses`, `#error-responses`, `#error-codes` |
| `SPEC_META` | `{SPEC_BASIC}#_meta` (NOT `#meta`) |
| `SPEC_SCHEMA_SECTION`, `SPEC_SCHEMA_DIALECT`, `SPEC_IMPL_REQUIREMENTS`, `SPEC_SCHEMA_VALIDATION`, `SPEC_REF_RESOLUTION`, `SPEC_COMPOSITION_BOUNDS`, `SPEC_ICONS` | `{SPEC_BASIC}#schema`, `#schema-dialect`, `#implementation-requirements`, `#schema-validation`, `#$ref-resolution` (`$` kept; `#ref-resolution` does not exist), `#composition-keyword-resource-use`, `#icons` |
| `SPEC_VERSIONING`, `SPEC_VERSION_NEGOTIATION`, `SPEC_EXTENSION_NEGOTIATION`, `SPEC_BACKCOMPAT_INIT`, `SPEC_COMPAT_MATRIX`, `SPEC_VERSIONING_TERMINOLOGY` | `{SPEC_BASE}/basic/versioning`, `#protocol-version-negotiation`, `#extension-negotiation`, `#backward-compatibility-with-initialization-based-versions`, `#compatibility-matrix`, `#terminology` |
| `SPEC_TRANSPORTS_UTF8` | `{SPEC_BASE}/basic/transports` (page; UTF-8 MUST at TI:31) |
| `SPEC_TRANSPORT` | `{SPEC_BASE}/basic/transports/streamable-http` (fragment-free; used in README for the Origin rule) |
| `SPEC_ORIGIN_FRAGMENT` | `{SPEC_TRANSPORT}#security-&-endpoint` (`%26` form also resolves; `#security-endpoint` does not) |
| `SPEC_SENDING_MESSAGES`, `SPEC_RECEIVING_MESSAGES`, `SPEC_CANCELLATION` | `{SPEC_TRANSPORT}#sending-messages`, `#receiving-messages`, `#cancellation` |
| `SPEC_PROTOCOL_VERSION_HEADER`, `SPEC_STANDARD_HEADERS`, `SPEC_VALUE_ENCODING`, `SPEC_CUSTOM_HEADERS`, `SPEC_SCHEMA_EXTENSION`, `SPEC_PARAM_HEADERS`, `SPEC_SERVER_VALIDATION` | `{SPEC_TRANSPORT}#protocol-version-header`, `#standard-request-headers`, `#value-encoding`, `#custom-headers-from-tool-parameters`, `#schema-extension`, `#server-behavior-for-custom-headers`, `#server-validation` |
| `SPEC_BACKCOMPAT_HTTP`, `SPEC_EARLIER_REVISIONS` | `{SPEC_TRANSPORT}#backward-compatibility`, `#earlier-streamable-http-revisions` |
| `SPEC_AUTH_ERRORS`, `SPEC_AUTH_TOKEN_HANDLING` | `{SPEC_BASE}/basic/authorization#error-handling`, `#token-handling` |
| `SPEC_SECURITY_BEST_PRACTICES`, `SPEC_SECURITY_LOCAL` | `{SPEC_BASE}/basic/security_best_practices`, `#local-mcp-server-compromise` |
| `SPEC_MRTR_SERVER_REQS` | `{SPEC_BASE}/basic/patterns/mrtr#server-requirements-basic-workflow` |
| `SPEC_DISCOVER`, `SPEC_DISCOVER_RESULT`, `SPEC_DISCOVER_WHEN_TO_CALL` | `{SPEC_BASE}/server/discover`, `#discoverresult`, `#when-to-call` |
| `SPEC_TOOLS`, `SPEC_TOOLS_CAPABILITIES`, `SPEC_TOOL`, `SPEC_TOOL_NAMES`, `SPEC_X_MCP_HEADER`, `SPEC_OUTPUT_SCHEMA`, `SPEC_TOOL_ERRORS`, `SPEC_TOOLS_SECURITY` | `{SPEC_BASE}/server/tools`, `#capabilities`, `#tool`, `#tool-names`, `#x-mcp-header`, `#output-schema`, `#error-handling`, `#security-considerations` |
| `SPEC_RESOURCES`, `SPEC_RESOURCES_CAPABILITIES` | `{SPEC_BASE}/server/resources`, `#capabilities` |
| `SPEC_PROMPTS`, `SPEC_PROMPTS_CAPABILITIES` | `{SPEC_BASE}/server/prompts`, `#capabilities` (verified today) |
| `SPEC_CACHING`, `SPEC_CACHEABLE_RESULTS`, `SPEC_TTL`, `SPEC_CACHE_SCOPE`, `SPEC_CACHE_PAGINATION`, `SPEC_CACHE_SECURITY` | `{SPEC_BASE}/server/utilities/caching`, `#cacheable-results`, `#time-to-live-ttl-field`, `#cache-scope-field`, `#interaction-with-pagination`, `#security-considerations` |
| `SPEC_PAGINATION`, `SPEC_PAGINATION_GUIDELINES`, `SPEC_PAGINATION_ERRORS` | `{SPEC_BASE}/server/utilities/pagination`, `#implementation-guidelines`, `#error-handling` |
| `SPEC_LOGGING` | `{SPEC_BASE}/server/utilities/logging` (page fetched today; anchor for "Per-request log level" UNVERIFIED — link the page) |
| `SPEC_DEPRECATED` | `{SPEC_BASE}/deprecated#deprecated` |
| `SPEC_SCHEMA_DISCOVER_RESULT`, `SPEC_SCHEMA_UNSUPPORTED_VERSION`, `SPEC_SCHEMA_IMPLEMENTATION`, `SPEC_SCHEMA_SERVER_CAPABILITIES`, `SPEC_SCHEMA_RESULT_META` | `{SPEC_BASE}/schema#discoverresult`, `#unsupportedprotocolversionerror`, `#implementation`, `#servercapabilities`, `#resultmetaobject` |
Not used (do not embed): `schema#cacheableresult` (absent), `schema#error`, `schema#toolannotations`, `schema#listtoolsresult` (UNVERIFIED), `caching#choosing-a-cache-scope` (UNVERIFIED), `tools#structured-content` (UNVERIFIED), and the four dead handoff paths (`/transports/streamable-http`, `/errors`, `/cache`, `/security#origin-validation`). `tests/test_spec_urls.py` enforces this list.

## Appendix C — Reviewer notes (resolution of every blocker and major finding)

| Finding | Resolution |
|---|---|
| B: four probe-safety policies | One policy in `probe_policy.is_probe_eligible` (§7.5, §9, U2): `safe` = `readOnlyHint is True or destructiveHint is False`, unannotated tools never called by default; `{}`-with-required on unannotated tools moved to `--probe all`; `openWorldHint` dropped from the predicate; test `test_safe_policy_never_calls_unannotated` added; README/SECURITY/--help wording identical. |
| B: JSON schema cannot represent check records | §6.11/§8.7 is the single model: `Finding{code, level, subject, message, spec_url?, evidence?, tool?, issue}`, `failures[]`/`warnings[]`/`info[]`; every check §(6) maps item→subject, got/expected/probe/path/page/http_status/rpc_code/hint→`evidence`; severity error→must, warning→should|best-practice (tagged per code), info→`info[]`; skip/unevaluated → `inconclusive` or a `should` finding; `passed` boolean only; `schema_version` int 1; one file `report.v1.json` + `--json-schema`. |
| B: Origin default contradictions | U8: no Origin unless `--origin`; no 403 fallback; session client factory fixed; `request.origin` null; origin check derives its own value. |
| B: inconclusive exit code / gating | §6.10: inconclusive 0 (1 `--strict`), error 2, fail 1, 2>1>0; handshake is the gate, dependents `runner.not_run` inconclusive; per-check exit recommendations deleted; "not JSON-RPC 2xx" = `must` fail in the check that sent it, not exit 2. |
| B: same observation, different verdicts | §6.6 outcome table + decisions: integral float ttlMs → best-practice (both); `input_required` on list/discover → must everywhere; 5xx → inconclusive everywhere; empty-body 400 → fail only in handshake (era rule); legacy-shaped 200 → handshake fail; missing outputSchema → best-practice; traceback → best-practice ratio; absent-Origin rejected → best-practice; handshake 403 → inconclusive; `-32022` → fail in handshake/discover, inconclusive elsewhere (cache runs only when gate passed, so consistent); `Mcp-Session-Id` minted reported once by session. |
| M: severity model violated both ways | Mechanical rule (level from RFC keyword): origin non-403 → must fail (U10); `supportedVersions` omit → best-practice; BodyTooLarge → inconclusive; `capabilities.x: null` → must; integral float → best-practice; `-32601` on tools/call and unknown-tool executed → best-practice; page-2 `-32602` → should (PG:113). |
| M: separate preflight | Removed; handshake (or the silent internal gate when deselected) is the gate; reserved codes `handshake.version_unsupported`/`modern_request_refused`/`discover.inconsistent_unsupported` replaced by the check sections' codes; header line says "speaks". |
| M: read-only promise omits requests | §4 enumerates every request; legacy `initialize`/GET opt-in (U4), `DELETE` never, `OPTIONS` listed, anonymous probe dropped; README/--help/SECURITY/CHANGELOG aligned; request-log test. |
| M: `protocol` check undefined | §7.8 written with a 10-probe verdict table incl. B:382-384, T:575-577, T:553-567, PG:113; enum/help/README written for eight names pending U1. |
| M: errors via SdkSession vs raw | Raw everywhere; `SdkSession`/`ctx.sdk()` deleted; SDK pure helpers kept. |
| M: reference server WARN / shipping | every fixture tool has `outputSchema` (`--no-output-schema` toggle); default port 0; tests-only (U5); acceptance test asserts every status `pass`. |
| M: interface drift (codes, flags, SPEC_ORIGIN, budgets) | codes grammar `^[a-z]+\.[a-z0-9_]+$` and all codes rewritten; flag set frozen in §8.1, per-check knobs → constants §6.4; `SPEC_TRANSPORT` (page) vs `SPEC_ORIGIN_FRAGMENT`; budget 10×timeout; `--timeout` > 0. |
| M: human output differs per section | §8.5 single owner: `{glyph} {name:<14}{summary}`, `✓ ✗ ! ? E`, titles table, final-line templates, two header lines listed as deviations; example report regenerated from real probe designs. |
| M: list cache not shared / session ordering | `CheckContext.*_list_pages()` shared; cache sends nothing of its own; session runs second and scans the transcript (runner re-scans at the end; human table printed after all checks); no finalize hook; `MAX_PAGES` single constant; anon probe needs no token override (dropped). |
| M: `session_required` over-broad, GET probe false positive, HS-14 citation | requires `/session/i` or handshake fingerprint; GET probe moved to protocol under `--legacy-probes` as info; minted ids cite T:680-681 + CL:14 at should. |
| M: `StrEnum`/`except*`/`asyncio.wait_for` | `class Outcome(str, Enum)`; `compat.unwrap_exception_group`; anyio only; `test_py310.py` greps. |
| M: `client.post` buffering relied upon | streaming + total budget mandatory in `RawClient` (§6.5); sentences removed from errors/schemas/cache. |
| M: legacy initialize + DELETE by default | U4 opt-in; DELETE never. |
| M: fresh-connection/ordering contradictions | handshake on `ctx.fresh`, seeds page 0; session second with transcript scan; cache uses shared pages. |
| M: `CliRunner(mix_stderr=False)` | `click>=8.2` floor; plain `CliRunner()`; assert on `result.stdout`/`stderr`. |
| M: SSE grace differs | `SSE_TERMINATION_GRACE_S = 2.0`, only handshake/discover wait. |
| M: GET-loop CI snippet hangs | single CI snippet (§8.8) with `-m 5 -X POST --data '{}'`. |
| M: `server` object vs string | U11: string `server` + sibling `request` object; int `schema_version`; string timestamp; no embedded transcript. |
| M: glyphs/SKIP | `⚠ – -` removed; SKIP → inconclusive with "Skipped:/Not evaluated:" summaries. |
| M: request budget (6–7 discovers, ~60 requests) | no preflight; discover cached; cache/schemas/discover share lists; expected ~20–60 requests documented in §9. |
| M: 73 decisions | collapsed to 11 (§2) + "decided by default". |
| M: hidden constants | §6.4 table. |
| M: packaging wiring gaps | `readme-renderer[md]` in lint group; `check_readme` steps in ci/release; `tests/__init__.py`; `for cls in REGISTRY`; `pytest>=9`; `pytest-asyncio` removed, `pytest-timeout`/`xdist` added. |
| M: README claims | "speaks" not "negotiated"; "breaks every server that still requires `initialize`"; origin/cache rows aligned; `E` glyph and inconclusive exit explained; Troubleshooting added. |
| B (eng): SdkSession across cancel scopes | SdkSession deleted. |
| B (oss): no roadmap | §13 with eight milestones and acceptance commands. |
| Minor accepted | `id: null` on 4xx → should; progress notification → info; `notifications/message` cites LG:65-66 (page fetched); `-32021`/406 unified; UNVERIFIED lists updated (encode sentinel, tools/call validator, `next_request`, `ERROR_CODE_HTTP_STATUS`, hatchling package data, shields license badge); unknown-tool name fixed; anonymous probe dropped; `data{}`/`observations[]`/`failures_from_validation_error`/25-symbol `sdk_compat` deleted; CHANGELOG/CONTRIBUTING bullets carry the release marker. |
| **Rejected / modified** | (a) "5xx → `error` everywhere" (one engineering finding): kept **inconclusive** — a 5xx is a server answer, not a transport failure, and exit 2 should mean "mcpcheck could not run"; one retry in the gate. (b) "`-32021` → PASS/era-confirmed with warning in handshake": kept inconclusive + should, consistent with other checks; it does not block dependents. (c) "integral-float `ttlMs` → error" (cache section): downgraded to best-practice for cross-check consistency (schema type `number`). (d) "ship the reference server" (readme-docs): tests-only (U5) to keep runtime deps minimal. (e) "anonymous probe default on" (cache): dropped for 0.1.0 (unauthenticated requests against authenticated endpoints need an explicit opt-in). (f) "always print human table streaming": table printed after all checks so the session transcript re-scan can amend results consistently. |
