#!/usr/bin/env bash
# Joint api + goodword-mcp integration scenario command for run-joint-integration.py.
#
# Usage: joint-api-mcp-e2e.sh <jest-testPathPattern> [api-port] [extra jest args...]
#
# A numeric second argument is the api port; anything else (e.g. `-t debrief`)
# is passed through to jest, and the port falls back to ARCHON_API_PORT.
#
# The runner hands us disposable candidate worktrees through
# ARCHON_REPO_API_WORKTREE / ARCHON_REPO_GOODWORD_MCP_WORKTREE (cwd is the
# integration run's artifacts dir). Those checkouts carry no node_modules and
# no .env, and nothing boots the api for the MCP e2e tests, so this script:
#   1. installs both repos if needed and copies the api .env from the stage
#      worktree (ARCHON_REPO_API_SOURCE_WORKTREE) into the candidate checkout;
#   2. boots the api candidate on <api-port> against the local dev stack;
#   3. mints a JWT for each of its two whitelisted identities through the local
#      OTP flow, signing in first and signing up only when no user exists yet;
#   4. runs the goodword-mcp jest pattern with GOODWORD_API_URL and both
#      GOODWORD_API_TOKEN and GOODWORD_API_TOKEN_SECOND set;
#   5. prints ARCHON_INTEGRATION_TESTS=<passed> (the runner's count contract)
#      and exits non-zero on any failed test.
set -euo pipefail

PATTERN="${1:?usage: joint-api-mcp-e2e.sh <jest-testPathPattern> [api-port] [extra jest args...]}"
shift
PORT="${ARCHON_API_PORT:-4213}"
case "${1-}" in
  ''|*[!0-9]*) ;;
  *) PORT="$1"; shift ;;
esac
JEST_EXTRA=("$@")
API_WT="${ARCHON_REPO_API_WORKTREE:?ARCHON_REPO_API_WORKTREE is required}"
MCP_WT="${ARCHON_REPO_GOODWORD_MCP_WORKTREE:?ARCHON_REPO_GOODWORD_MCP_WORKTREE is required}"
ENV_SRC="${ARCHON_REPO_API_SOURCE_WORKTREE:-$API_WT}"
OTP_EMAIL="${ARCHON_OTP_TEST_EMAIL:-edy@goodword.com}"
OTP_EMAIL_SECOND="${ARCHON_OTP_SECOND_EMAIL:-edy+archon2@goodword.com}"
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

json_field() {
  python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2]) or '')" "$1" "$2" 2>/dev/null || true
}

# Sign in first, sign up only when there is no user yet. A scenario that needs a
# second identity used to mint its own inside the test file, signing up every
# time, so the first rerun against the same stack died with "User already
# exists". Signup here is the fallback, runs at most once per email per stack,
# and every later run takes the signin path above it.
mint_token() {
  local email="$1" tag="$2" token session
  curl -s -o "$OUT/$tag-initiate.json" -X POST "$URL/auth/otp/initiate" \
    -H 'content-type: application/json' -d "{\"email\":\"$email\"}"
  curl -s -o "$OUT/$tag-signin.json" -X POST "$URL/auth/otp/signin/verify" \
    -H 'content-type: application/json' -d "{\"email\":\"$email\",\"code\":\"$OTP_CODE\"}"
  token=$(json_field "$OUT/$tag-signin.json" token)
  if [ -z "$token" ]; then
    curl -s -o "$OUT/$tag-signup-code.json" -X POST "$URL/auth/otp/signup/code" \
      -H 'content-type: application/json' -d "{\"email\":\"$email\",\"code\":\"$OTP_CODE\"}"
    curl -s -o "$OUT/$tag-signup-session.json" -X POST "$URL/auth/signup/session" \
      -H 'content-type: application/json' -d "{\"email\":\"$email\"}"
    session=$(json_field "$OUT/$tag-signup-session.json" sessionId)
    if [ -n "$session" ]; then
      curl -s -o "$OUT/$tag-signup-complete.json" -X POST "$URL/auth/signup/complete" \
        -H 'content-type: application/json' -d "{\"sessionId\":\"$session\"}"
      token=$(json_field "$OUT/$tag-signup-complete.json" token)
    fi
  fi
  test -n "$token" || return 1
  printf '%s' "$token"
}

TOKEN=$(mint_token "$OTP_EMAIL" otp) \
  || { echo "JOINT_E2E=FAIL could not mint a token for $OTP_EMAIL (see $OUT/otp-*.json)"; exit 1; }
# Both identities belong to the harness, not to a test file: a scenario that
# needs a non-owner (403 paths, ownership checks) gets one it did not create.
TOKEN_SECOND=$(mint_token "$OTP_EMAIL_SECOND" otp-second) \
  || { echo "JOINT_E2E=FAIL could not mint a token for $OTP_EMAIL_SECOND (see $OUT/otp-second-*.json)"; exit 1; }

RC=0
(cd "$MCP_WT" && GOODWORD_API_URL="$URL" GOODWORD_API_TOKEN="$TOKEN" \
  GOODWORD_API_TOKEN_SECOND="$TOKEN_SECOND" \
  mise x node@20 -- env NODE_OPTIONS=--experimental-vm-modules pnpm exec jest \
  --testPathPatterns "$PATTERN" ${JEST_EXTRA[@]+"${JEST_EXTRA[@]}"} --json --outputFile "$OUT/jest-result.json" 2>&1 | tee "$OUT/jest.log") || RC=$?

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
