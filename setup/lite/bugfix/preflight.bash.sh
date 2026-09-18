set -euo pipefail
if [ -n "${ARTIFACTS_DIR-}" ]; then exec > >(tee -a "$ARTIFACTS_DIR/node-preflight.out") 2> >(tee -a "$ARTIFACTS_DIR/node-preflight.out" >&2); fi
: "${ARCHON_LAYER:?PREFLIGHT=FAIL ARCHON_LAYER unset}"
: "${PROJECT_ROOT:?PREFLIGHT=FAIL PROJECT_ROOT unset}"
test -f "$ARCHON_LAYER/setup/resolve-params.sh" || { echo "PREFLIGHT=FAIL ARCHON_LAYER missing helpers"; exit 1; }
ROOT="$PROJECT_ROOT"
if [ -n "${ARCHON_BUGFIX_CONTINUATION_SEED:-}" ]; then
  python3 "$ARCHON_LAYER/setup/archon-run.py" import-continuation --artifacts "$ARTIFACTS_DIR"
fi
python3 "$ARCHON_LAYER/setup/repo-policy.py" snapshot --root "$ROOT" --artifacts "$ARTIFACTS_DIR" \
  || { echo "PREFLIGHT=FAIL repository policy snapshot"; exit 1; }
SK="$ROOT/.claude/skills"
test -f "$SK/ce-code-review/SKILL.md" || { echo "PREFLIGHT=FAIL staged ce-code-review missing"; exit 1; }
# Dual contract: CE 3.2.0 carries the markers in SKILL.md; newer CE moved
# mode:headless (now an alias for mode:agent) into references/modes-and-output.md
# and the verdict field into references/finish-review.md. Either satisfies.
{ grep -q 'mode:headless' "$SK/ce-code-review/SKILL.md" && grep -q '"verdict"' "$SK/ce-code-review/SKILL.md"; }         || { grep -q 'mode:headless' "$SK/ce-code-review/references/modes-and-output.md" 2>/dev/null && grep -q '"verdict"' "$SK/ce-code-review/references/finish-review.md" 2>/dev/null; }         || { echo "PREFLIGHT=FAIL ce-code-review carries neither review contract (headless envelope nor agent JSON)"; exit 1; }
# Billing guard: runs MUST bill the Claude subscription (OAuth /login), never the API.
# Any of these outranks the subscription login in Claude Code's auth precedence.
for v in ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN CLAUDE_API_KEY CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_PROFILE; do
  test -z "$(printenv "$v")" || { echo "PREFLIGHT=FAIL $v is set - would bill API, not subscription"; exit 1; }
done
grep -q 'apiKeyHelper' "$HOME/.claude/settings.json" 2>/dev/null && { echo "PREFLIGHT=FAIL apiKeyHelper configured - would override subscription"; exit 1; }
# port_pids: lsof -> ss -> fuser (lsof is often absent on Linux). rc 1 means
# NO backend exists at all; it never means "the port is free".
port_pids() { # $1 = port
  local out=""
  if command -v lsof >/dev/null 2>&1; then
    out="$(lsof -ti "tcp:$1" -sTCP:LISTEN 2>/dev/null || true)"
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
# Tool presence only. The ports themselves are ALLOCATED below, not
# asserted free: port-alloc.sh walks past an occupied slot, which is what
# lets a second run of this lane start while the first is still up.
port_pids 4124 >/dev/null || { echo "PREFLIGHT=FAIL no port-inspection tool (need lsof, ss, or fuser)"; exit 1; }
# 4124/3124 are parent bases; derive-lite substitutes them to 4126/3126.
bash "$ARCHON_LAYER/setup/profile-preflight.sh" 4124 bugfix-lite.yaml 3124 --checkouts
eval "$(bash "$ARCHON_LAYER/setup/params-env.sh" "$ARTIFACTS_DIR/params.json")"
cp "$SPEC" "$ARTIFACTS_DIR/bug-report.md"
mkdir -p "$ARTIFACTS_DIR/evidence"
python3 "$ARCHON_LAYER/setup/archon-run.py" capabilities --artifacts "$ARTIFACTS_DIR"
command -v docker-compose >/dev/null 2>&1 || echo "PREFLIGHT_WARN legacy docker-compose missing - integration-kind repro unavailable"
echo "PREFLIGHT=PASS"
