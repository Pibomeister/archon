set -euo pipefail
SK=/Users/eduardopicazo/Documents/Workspace/Goodword/.claude/skills
test -f "$SK/ce-code-review/SKILL.md" || { echo "PREFLIGHT=FAIL staged ce-code-review missing"; exit 1; }
test -f "$SK/ce-doc-review/SKILL.md"  || { echo "PREFLIGHT=FAIL staged ce-doc-review missing"; exit 1; }
# Dual contract: CE 3.2.0 carries the markers in SKILL.md; newer CE moved
# mode:headless (now an alias for mode:agent) into references/modes-and-output.md
# and the verdict field into references/finish-review.md. Either satisfies.
{ grep -q 'mode:headless' "$SK/ce-code-review/SKILL.md" && grep -q '"verdict"' "$SK/ce-code-review/SKILL.md"; }         || { grep -q 'mode:headless' "$SK/ce-code-review/references/modes-and-output.md" 2>/dev/null && grep -q '"verdict"' "$SK/ce-code-review/references/finish-review.md" 2>/dev/null; }         || { echo "PREFLIGHT=FAIL ce-code-review carries neither review contract (headless envelope nor agent JSON)"; exit 1; }
command -v bun >/dev/null || { echo "PREFLIGHT=FAIL bun missing"; exit 1; }
command -v gh >/dev/null || { echo "PREFLIGHT=FAIL gh missing"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "PREFLIGHT=FAIL gh unauthenticated"; exit 1; }
aws sts get-caller-identity >/dev/null 2>&1 || { echo "PREFLIGHT=FAIL aws session expired - run: aws login"; exit 1; }
# Billing guard: runs MUST bill the Claude subscription (OAuth /login), never the API.
# Any of these outranks the subscription login in Claude Code's auth precedence.
for v in ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN CLAUDE_API_KEY CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_PROFILE; do
  test -z "$(printenv "$v")" || { echo "PREFLIGHT=FAIL $v is set - would bill API, not subscription"; exit 1; }
done
grep -q 'apiKeyHelper' "$HOME/.claude/settings.json" 2>/dev/null && { echo "PREFLIGHT=FAIL apiKeyHelper configured - would override subscription"; exit 1; }
# port_pids: PIDs holding a TCP port. lsof is preinstalled on macOS and
# frequently absent on Linux, so try lsof -> ss -> fuser. rc 1 means NO
# backend exists at all; it never means "the port is free". Without this a
# missing lsof made the busy-port check silently pass and let two runs collide.
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
port_pids 4123 >/dev/null || { echo "PREFLIGHT=FAIL no port-inspection tool (need lsof, ss, or fuser)"; exit 1; }
test -z "$(port_pids 4123)" || { echo "PREFLIGHT=FAIL port 4123 busy"; exit 1; }
test -d /Users/eduardopicazo/Documents/Workspace/Goodword/api/.git || { echo "PREFLIGHT=FAIL api repo missing"; exit 1; }
# Run identity: message = absolute spec path (empty = hard fail). Single derivation point.
bash /Users/eduardopicazo/Documents/Workspace/Goodword/.archon/setup/resolve-params.sh \
  /Users/eduardopicazo/Documents/Workspace/Goodword "${ARGUMENTS-}" "$ARTIFACTS_DIR"
test -d /Users/eduardopicazo/Documents/Workspace/Goodword/goodword-kb/wiki || { echo "PREFLIGHT=FAIL knowledge base missing"; exit 1; }
git -C /Users/eduardopicazo/Documents/Workspace/Goodword/goodword-kb status --porcelain | sort > "$ARTIFACTS_DIR/kb-pre-porcelain.txt"
echo "PREFLIGHT=PASS"
# LITE lane: one review round. converge reads this durable cap; the lite
# converge overlay treats a landed fix on round 1 as converged (fixes are
# re-gated mechanically by post-fix-gate, not re-read by a reviewer).
printf '1\n' > "$ARTIFACTS_DIR/round-cap.txt"
echo "LITE_ROUND_CAP=1"
