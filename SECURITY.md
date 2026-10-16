# Security policy

## Supported versions

Only the newest release of `mcpcheck` receives fixes. Reproduce against it
(`uvx mcpcheck@latest --version`) before reporting.

| Version | Supported |
|---------|-----------|
| latest 0.x release | yes |
| older releases | no, upgrade |

## Reporting a vulnerability

Please report security issues privately through GitHub's
[private vulnerability reporting](https://github.com/suresh-jbt/mcpcheck/security/advisories/new)
for this repository (Security tab, "Report a vulnerability"). If that is not
possible, email suresh@jarvisbitz.com with "mcpcheck security" in the subject.

Please **do not** open a public issue, discussion or pull request for a
suspected vulnerability.

Include, where you can:

- a description of the issue and its impact,
- the `mcpcheck` version (`mcpcheck --version`), Python version and OS,
- the command you ran (with the token removed) and, if relevant, a minimal
  server that triggers the behaviour (the toggle-driven
  `tests/fixtures/misbehaving_server.py` is a good starting point),
- any suggested fix.

You will get an acknowledgement within 7 days. Confirmed issues are fixed in a
patch release and credited in the changelog unless you prefer otherwise.

## Threat model and scope

`mcpcheck` is a command-line client. It sends HTTP requests to the single URL
you give it and prints a report. Its public promise, stated in the README
under *Safety: what mcpcheck sends* and in `--help`, is that it is read-only:
list methods and `server/discover`; `tools/call` only with arguments proven
invalid against the tool's own `inputSchema` and only to tools annotated
read-only or non-destructive (plus one call to a non-existent tool name);
header probes that cannot execute anything; one `OPTIONS` request; and, only
with `--legacy-probes`, one `initialize` and one headers-only `GET`. Keep that
in mind when deciding what is a vulnerability *in `mcpcheck`*.

**In scope**

- A token or custom header passed with `--token`/`--header` leaking into the
  human report, the JSON report, `--verbose` output, log files or any request
  to a host other than `SERVER_URL` (including via redirects).
- Following a redirect to another origin, or a `301`/`302`/`303` redirect that
  downgrades a `POST` to a `GET`, or sending credentials to another origin.
- Any way a *malicious server* can make `mcpcheck` execute code, read local
  files, exhaust memory or hang indefinitely: for example through an unbounded
  SSE stream, a pathological or deeply nested JSON Schema, an oversized body,
  or a network `$ref` or icon URL (the spec says schemas MUST NOT be
  dereferenced over the network, and `mcpcheck` must never do so).
- `mcpcheck` calling a tool with *valid* arguments, calling an unannotated or
  destructive tool under the default `--probe safe`, reading a resource
  (`resources/read`), rendering a prompt (`prompts/get`), sending `DELETE`,
  notifications or `subscriptions/listen`. The tool is documented as
  read-only; **a probe that could have side effects is a bug of the highest
  severity.**
- TLS verification being skipped without `--insecure`, or `--insecure` not
  being recorded in the report.

**Out of scope**

- Findings about the *server you tested*. A failing `origin` check means the
  server is vulnerable to DNS rebinding, not `mcpcheck`. Report it to the
  server's maintainers.
- Anything caused by passing `--insecure`, `--probe all`, `--legacy-probes`
  (which may create a session on dual-era servers) or a token you did not
  intend to share. Those flags are explicit opt-ins and documented as such.
- A server that ignores its own `inputSchema` and executes an annotated
  read-only tool on schema-invalid input. `mcpcheck` reports that as the
  `errors.accepted_invalid_input` finding; the residual risk is disclosed in
  the README.
- Vulnerabilities in dependencies that `mcpcheck` does not trigger. Report
  those upstream (for the `mcp` SDK, see its own
  [security policy](https://github.com/modelcontextprotocol/python-sdk/security/policy)).
- The content of the MCP specification itself.
