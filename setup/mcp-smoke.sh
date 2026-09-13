#!/usr/bin/env bash
# goodword-mcp boot smoke. Builds the server, starts it on a synthetic env, and
# asserts both sides of its auth wall: the unauthenticated 200 surface
# (/favicon.ico) and the token-less 401 on /mcp.
#
# Usage: mcp-smoke.sh <worktree> <artifacts-dir> <port>
#
# The port is validated, never trusted: a run resumed from a params.json written
# before goodword-mcp declared a smoke stack carries an empty api_port, and an
# unguarded "${3}" would bind an empty port and die with a shell error instead of
# a typed line.
#
# BUILDING IS SAFE FOR THE LOCAL-CANDIDATE GATE. goodword-mcp's .gitignore
# ignores dist/, so the `git status --porcelain --untracked-files=all` check in
# trusted-local-candidate.sh -- a hard LOCAL_CANDIDATE=FAIL -- still sees a clean
# tree after `pnpm build` writes dist/. Do not drop the build: `node
# dist/index.js` has no entrypoint without it, and the favicon assertion also
# needs copy-static-assets.mjs to have placed dist/assets/favicon.ico.
#
# MCP_SMOKE_START_CMD, when set, replaces the build-and-start pair with that one
# command, run in the worktree under the same synthetic env. It exists so
# setup/tests/test_mcp_smoke.py can substitute a stub HTTP server; no lane sets
# it.
set -uo pipefail
WT="${1:?SMOKE=FAIL no worktree}"
AD="${2:?SMOKE=FAIL no artifacts dir}"
PORT="${3:?SMOKE=FAIL no port}"

fail() {
  echo "SMOKE=FAIL $1" | tee "$AD/smoke-result.txt"
  exit 1
}

# port_pids: lsof -> ss -> fuser (lsof is often absent on Linux). rc 1 only when
# no backend exists at all.
port_pids() { # $1 = port
  local out=""
  if command -v lsof >/dev/null 2>&1; then
    out="$(lsof -ti ":$1" 2>/dev/null || true)"
  elif command -v ss >/dev/null 2>&1; then
    out="$(ss -ltnp "sport = :$1" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u || true)"
  elif command -v fuser >/dev/null 2>&1; then
    out="$(fuser -n tcp "$1" 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+$' || true)"
  else
    return 1
  fi
  [ -n "$out" ] && printf '%s\n' "$out"
  return 0
}

cd "$WT" || fail "worktree $WT unreadable"

# The five variables config.ts hard-requires, as syntactically valid placeholders
# pointing at a domain that cannot resolve: this smoke asserts the pre-network
# 401 branch, so nothing here is ever dialled. WORKOS_AUDIENCE must be
# URL-parseable -- index.ts does new URL(audience).origin on that very branch.
export PORT
export GOODWORD_API_URL="https://mcp-smoke.invalid/api"
export WORKOS_JWKS_URL="https://mcp-smoke.invalid/.well-known/jwks.json"
export WORKOS_ISSUER="https://mcp-smoke.invalid"
export WORKOS_AUDIENCE="https://mcp-smoke.invalid/mcp"
export WORKOS_AS_METADATA_URL="https://mcp-smoke.invalid/.well-known/oauth-authorization-server"

if [ -n "${MCP_SMOKE_START_CMD-}" ]; then
  bash -c "$MCP_SMOKE_START_CMD" > "$AD/mcp-boot.log" 2>&1 &
else
  mise x node@20 -- pnpm build > "$AD/mcp-build.log" 2>&1 \
    || fail "pnpm build (build log in artifacts)"
  mise x node@20 -- node dist/index.js > "$AD/mcp-boot.log" 2>&1 &
fi
SRV=$!
# node spawns children that outlive the parent: sweep the PORT on exit, not just $SRV.
trap 'kill "$SRV" 2>/dev/null; sleep 1; P=$(port_pids "$PORT"); test -n "$P" && kill $P 2>/dev/null; true' EXIT

# Liveness before every poll: a process that already exited must fail the boot
# on the spot, not burn the whole readiness budget and report a generic timeout.
CODE=000
for _ in $(seq 1 60); do
  sleep 1
  kill -0 "$SRV" 2>/dev/null || fail "mcp-boot exited before ready (boot log in artifacts)"
  CODE=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:$PORT/favicon.ico" || echo 000)
  test "$CODE" = "200" && break
done
test "$CODE" = "200" || fail "favicon code=$CODE (boot log in artifacts)"

# The header is the assertion, not just the status: a 401 from a crashed proxy
# carries no WWW-Authenticate, and would otherwise read as a working auth wall.
HDRS="$AD/mcp-unauth.headers"
UCODE=$(curl -s -o /dev/null -D "$HDRS" -w '%{http_code}' -X POST \
  -H 'Content-Type: application/json' --data '{}' \
  "http://localhost:$PORT/mcp" || echo 000)
test "$UCODE" = "401" || fail "unauth /mcp code=$UCODE expected=401"
grep -qi '^www-authenticate:[[:space:]]*Bearer' "$HDRS" \
  || fail "unauth /mcp 401 carries no WWW-Authenticate: Bearer header"

echo "SMOKE=PASS mcp-boot=ok favicon=200 unauth-mcp=401" | tee "$AD/smoke-result.txt"
