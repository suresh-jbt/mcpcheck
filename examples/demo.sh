#!/usr/bin/env bash
# One-command mcpcheck demo: a conformant MCP 2026-07-28 server, then a broken one.
#
#   bash examples/demo.sh
#
# Starts examples/demo_server.py on a free loopback port, runs mcpcheck against it
# (expects exit 0), restarts it with three deliberate flaws and runs mcpcheck again
# (expects exit 1). The server is always killed on the way out, including on Ctrl-C.
#
# Needs nothing but `uv` inside a clone of https://github.com/suresh-jbt/mcpcheck.
# Override the command under test with MCPCHECK, e.g.
#   MCPCHECK=mcpcheck bash examples/demo.sh                  # an mcpcheck on PATH
#   MCPCHECK="uvx mcpcheck@0.1.0" bash examples/demo.sh      # once 0.1.0 is on PyPI
set -euo pipefail

BREAKS="cache,origin,output-schema"
READY_TIMEOUT_S=15

here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
repo=$(dirname -- "$here")
server_py="$here/demo_server.py"

die() { printf '\ndemo.sh: %s\n' "$*" >&2; exit 1; }
rule() { printf '\n== %s\n\n' "$1"; }

# --- how to run mcpcheck, and how to run the demo server --------------------------
if [ -n "${MCPCHECK:-}" ]; then
  read -r -a mcpcheck <<<"$MCPCHECK"
elif [ -f "$repo/pyproject.toml" ] && command -v uv >/dev/null 2>&1; then
  mcpcheck=(uv run --quiet mcpcheck)          # inside the repository
elif command -v mcpcheck >/dev/null 2>&1; then
  mcpcheck=(mcpcheck)                         # installed on PATH
elif command -v uvx >/dev/null 2>&1; then
  mcpcheck=(uvx mcpcheck)                     # from PyPI, nothing installed
else
  die "no mcpcheck found. Install uv (https://docs.astral.sh/uv/) or set MCPCHECK=..."
fi
if [ -f "$repo/pyproject.toml" ] && command -v uv >/dev/null 2>&1; then
  python=(uv run --quiet python)
elif command -v python3 >/dev/null 2>&1; then
  python=(python3)                            # needs the `mcp` SDK importable
else
  die "no Python found to run $server_py"
fi
[ -f "$server_py" ] || die "cannot find $server_py"

# --- server lifecycle -------------------------------------------------------------
log=$(mktemp)
pid=''
url=''

stop_server() {
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  fi
  pid=''
}
cleanup() { stop_server; rm -f "$log"; }
# Separate traps: a bare `trap cleanup INT` would clean up and then *carry on*, so
# Ctrl-C would restart the server and the demo would report success. Exit instead,
# with the conventional 128+signal status.
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

start_server() {  # $1: --break value, or '' for the conformant server
  stop_server
  : >"$log"
  if [ -n "$1" ]; then
    "${python[@]}" "$server_py" --port 0 --break "$1" 2>"$log" &
  else
    "${python[@]}" "$server_py" --port 0 2>"$log" &
  fi
  pid=$!
  deadline=$(($(date +%s) + READY_TIMEOUT_S))
  while :; do
    url=$(sed -n 's|.*listening on \(http[^ ]*\).*|\1|p' "$log")
    [ -n "$url" ] && break
    kill -0 "$pid" 2>/dev/null || { cat "$log" >&2; die "the demo server exited during startup"; }
    [ "$(date +%s)" -ge "$deadline" ] && { cat "$log" >&2; die "the demo server was not ready within ${READY_TIMEOUT_S}s"; }
    sleep 0.1
  done
  cat "$log"
}

run_mcpcheck() {  # $1: the exit code this run must produce
  printf '$ %s %s\n\n' "${mcpcheck[*]}" "$url"
  set +e
  "${mcpcheck[@]}" "$url"
  rc=$?
  set -e
  [ "$rc" -eq "$1" ] || die "expected mcpcheck to exit $1, got $rc"
  printf '\n-> mcpcheck exited %s, as expected.\n' "$rc"
}

# --- act 1: a conformant server ---------------------------------------------------
rule "1/2  A conformant MCP 2026-07-28 server (examples/demo_server.py)"
start_server ''
run_mcpcheck 0

# --- act 2: the same server with three deliberate flaws ---------------------------
rule "2/2  The same server started with --break $BREAKS"
start_server "$BREAKS"
explain() {  # one line per feature in $BREAKS, so the two can never drift apart
  IFS=',' read -r -a features <<<"$1"
  for feature in "${features[@]}"; do
    case "$feature" in
      cache)         effect='no ttlMs/cacheScope on */list results  -> cache fails (MUST)' ;;
      origin)        effect='any Origin accepted                    -> origin fails (MUST)' ;;
      output-schema) effect='tools publish no outputSchema          -> schemas warns' ;;
      errors)        effect='a failing tools/call returns a trace   -> errors warns' ;;
      discover)      effect='no serverInfo in server/discover _meta -> discover warns' ;;
      session)       effect='mints an Mcp-Session-Id header         -> session warns' ;;
      initialize)    effect='demands the legacy handshake           -> handshake fails (MUST)' ;;
      *)             effect='see examples/README.md for its effect' ;;
    esac
    printf '%-15s %s\n' "$feature:" "$effect"
  done
  printf '\n'
}
explain "$BREAKS"
run_mcpcheck 1

# --- the same report, machine-readable --------------------------------------------
rule "Every non-passing check as JSON (mcpcheck --json)"
if command -v jq >/dev/null 2>&1; then
  printf '$ %s %s --json | jq -r %s\n\n' "${mcpcheck[*]}" "$url" \
    "'.checks[] | select(.status != \"pass\") | [.name, .status, (.failures + .warnings)[0].code] | @tsv'"
  "${mcpcheck[@]}" "$url" --json |
    jq -r '.checks[] | select(.status != "pass") | [.name, .status, (.failures + .warnings)[0].code] | @tsv' || true
else
  printf 'jq is not installed, skipping the JSON slice. The full report is on stdout with:\n'
  printf '  %s %s --json\n' "${mcpcheck[*]}" "$url"
fi

rule "Next"
# Interpolated, not quoted: these must be the commands that work *here*, which is
# `uv run ...` inside a clone and a bare `mcpcheck` only when one is on PATH.
cat <<NEXT
* break something else:   ${python[*]} examples/demo_server.py --list-breaks
* read the server:        examples/demo_server.py  (commented, official mcp SDK)
* check your own server:  ${mcpcheck[*]} http://127.0.0.1:<your-port>/mcp
* see every HTTP exchange: add -vv;  machine-readable report: add --json
NEXT
