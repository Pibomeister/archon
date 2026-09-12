#!/usr/bin/env bash
# Joint api + goodword-mcp integration scenario command for run-joint-integration.py.
#
# Usage: joint-api-mcp-e2e.sh <jest-testPathPattern> [api-port]
#
# The runner hands us disposable candidate worktrees through
# ARCHON_REPO_API_WORKTREE / ARCHON_REPO_GOODWORD_MCP_WORKTREE (cwd is the
# integration run's artifacts dir). Those checkouts carry no node_modules and
# no .env, and nothing boots the api for the MCP e2e tests, so this script:
#   1. installs both repos if needed and copies the api .env from the stage
#      worktree (ARCHON_REPO_API_SOURCE_WORKTREE) into the candidate checkout;
#   2. boots the api candidate on <api-port> against the local dev stack;
#   3. mints a JWT through the local OTP flow (whitelisted test email + code);
#   4. runs the goodword-mcp jest pattern with GOODWORD_API_URL/TOKEN set;
#   5. prints ARCHON_INTEGRATION_TESTS=<passed> (the runner's count contract)
#      and exits non-zero on any failed test.
set -euo pipefail

PATTERN="${1:?usage: joint-api-mcp-e2e.sh <jest-testPathPattern> [api-port]}"
PORT="${2:-4213}"
API_WT="${ARCHON_REPO_API_WORKTREE:?ARCHON_REPO_API_WORKTREE is required}"
MCP_WT="${ARCHON_REPO_GOODWORD_MCP_WORKTREE:?ARCHON_REPO_GOODWORD_MCP_WORKTREE is required}"
ENV_SRC="${ARCHON_REPO_API_SOURCE_WORKTREE:-$API_WT}"
OTP_EMAIL="${ARCHON_OTP_TEST_EMAIL:-edy@goodword.com}"
OTP_CODE="${ARCHON_OTP_TEST_CODE:-123456}"
URL="http://localhost:$PORT"
OUT="$PWD/joint-api-mcp-e2e"
mkdir -p "$OUT"

port_pids() { lsof -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null || true; }
if [ -n "$(port_pids "$PORT")" ]; then
  echo "JOINT_E2E=FAIL port $PORT already in use"; exit 1
fi

if [ ! -f "$API_WT/.env" ]; then
  test -f "$ENV_SRC/.env" || { echo "JOINT_E2E=FAIL no api .env at $ENV_SRC/.env"; exit 1; }
  cp "$ENV_SRC/.env" "$API_WT/.env"
fi
if [ ! -d "$API_WT/node_modules" ]; then
  (cd "$API_WT" && bun install --frozen-lockfile > "$OUT/api-install.log" 2>&1) || { echo "JOINT_E2E=FAIL api install (see $OUT/api-install.log)"; exit 1; }
fi
if [ ! -d "$MCP_WT/node_modules" ]; then
  (cd "$MCP_WT" && mise x node@20 -- pnpm install --frozen-lockfile > "$OUT/mcp-install.log" 2>&1) || { echo "JOINT_E2E=FAIL mcp install (see $OUT/mcp-install.log)"; exit 1; }
fi

# The server must not inherit this script's stdout/stderr: run-joint-integration.py
# reads our output through a pipe and waits for EOF, so a server still holding
# that pipe stalls the runner until its 30-minute timeout even after the tests
# passed. Start it in its own session with stdio on files, and tear the whole
# session down on exit (bun start spawns children that outlive the parent).
SRV=$(cd "$API_WT" && PORT="$PORT" python3 - "$OUT/api-boot.log" <<'PY'
import os, subprocess, sys
log = open(sys.argv[1], "ab")
proc = subprocess.Popen(["bun", "start"], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
print(proc.pid)
PY
)
trap 'kill -- -"$SRV" 2>/dev/null; sleep 1; P=$(port_pids "$PORT"); test -n "$P" && kill $P 2>/dev/null; true' EXIT

CODE=000
for _ in $(seq 1 90); do
  CODE=$(curl -s -o /dev/null -w '%{http_code}' "$URL/api-docs-json" || echo 000)
  test "$CODE" = "200" && break
  sleep 2
done
test "$CODE" = "200" || { echo "JOINT_E2E=FAIL api boot code=$CODE (see $OUT/api-boot.log)"; exit 1; }

curl -s -o "$OUT/otp-initiate.json" -X POST "$URL/auth/otp/initiate" \
  -H 'content-type: application/json' -d "{\"email\":\"$OTP_EMAIL\"}"
curl -s -o "$OUT/otp-verify.json" -X POST "$URL/auth/otp/signin/verify" \
  -H 'content-type: application/json' -d "{\"email\":\"$OTP_EMAIL\",\"code\":\"$OTP_CODE\"}"
TOKEN=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('token') or '')" "$OUT/otp-verify.json" 2>/dev/null || true)
test -n "$TOKEN" || { echo "JOINT_E2E=FAIL could not mint a token for $OTP_EMAIL (see $OUT/otp-*.json)"; exit 1; }

RC=0
(cd "$MCP_WT" && GOODWORD_API_URL="$URL" GOODWORD_API_TOKEN="$TOKEN" \
  mise x node@20 -- env NODE_OPTIONS=--experimental-vm-modules pnpm exec jest \
  --testPathPatterns "$PATTERN" --json --outputFile "$OUT/jest-result.json" 2>&1 | tee "$OUT/jest.log") || RC=$?

read -r PASSED FAILED <<<"$(python3 - "$OUT/jest-result.json" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print(int(d.get("numPassedTests", 0)), int(d.get("numFailedTests", 0)))
except Exception:
    print(0, 1)
PY
)"
echo "ARCHON_INTEGRATION_TESTS=$PASSED"
if [ "$RC" != 0 ] || [ "$FAILED" != 0 ] || [ "$PASSED" = 0 ]; then
  echo "JOINT_E2E=FAIL rc=$RC passed=$PASSED failed=$FAILED (see $OUT/jest.log)"; exit 1
fi
echo "JOINT_E2E=PASS passed=$PASSED"
