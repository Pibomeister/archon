#!/usr/bin/env bash
# Goodword Archon SDLC — teammate installer. Run from a clone of the setup gist:
#   bash install.sh --root /absolute/path/to/Goodword [-y]
# Idempotent. Asserts the dev environment (never installs it), pins archon CLI
# v0.8.0, renders the .archon/ payload with your root path, stages CE skills,
# registers the folder project, and validates the workflows. -y additionally
# merges allowlist.json into <root>/.claude/settings.json (diff shown first).
set -uo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
# Split literal: this file ships inside the payload it renders, and must not
# contain the contiguous placeholder or the render would rewrite this line.
PLACEHOLDER='{{GOODWORD''_ROOT}}'
# CE plugin: validated baseline 3.2.0, tolerated forward. The version is a
# proxy — the real dependency is the review CONTRACT that stage-skills.sh and
# every lane preflight verify. This precondition only reports what the cache
# holds; stage-skills.sh owns the pick, preferring a cached 3.2.0, then the
# vendored 3.2.0 snapshot shipped in this payload, then (last resort) a newer
# cached version whose contract markers hold.
CE_BASE="$HOME/.claude/plugins/cache/compound-engineering-plugin/compound-engineering"
CE_VALIDATED="3.2.0"
CE_MIN="3.2.0"
ARCHON_PIN="v0.8.0"

# Prints "<version> <skills-dir>" for the newest installed CE version >= CE_MIN
# that has a skills/ dir; prints nothing (rc 1) when none qualifies.
ce_resolve() {
  [ -d "$CE_BASE" ] || return 1
  python3 - "$CE_BASE" "$CE_MIN" <<'PY'
import os, re, sys
base, floor = sys.argv[1], sys.argv[2]
def key(v):
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", v)
    return tuple(map(int, m.groups())) if m else None
fl = key(floor)
cands = sorted(
    (key(d), d) for d in os.listdir(base)
    if key(d) and key(d) >= fl and os.path.isdir(os.path.join(base, d, "skills"))
)
if not cands:
    sys.exit(1)
v = cands[-1][1]
print(v, os.path.join(base, v, "skills"))
PY
}

# Absolute grep/head. Hardcoding the /usr/bin path was a deliberate workaround for
# a shell alias/function shadowing `grep`, but this script runs without `set -e`:
# where that binary is absent the call returns 127 and every guard built on it
# SILENTLY PASSES — including the billing guard below. Resolve once, fail loudly.
resolve_bin() { # $1 = name -> absolute path on stdout, rc 1 if unresolvable
  local n="$1" p
  for p in "/usr/bin/$n" "/bin/$n"; do
    [ -x "$p" ] && { printf '%s' "$p"; return 0; }
  done
  p="$(command -v "$n" 2>/dev/null || true)"
  case "$p" in /*) [ -x "$p" ] && { printf '%s' "$p"; return 0; } ;; esac
  return 1
}
GREP="$(resolve_bin grep)" || { echo "FAIL  cannot resolve a grep binary on this host"; exit 1; }
HEAD="$(resolve_bin head)" || { echo "FAIL  cannot resolve a head binary on this host"; exit 1; }

ROOT=""
MERGE_ALLOWLIST=no
while [ $# -gt 0 ]; do
  case "$1" in
    --root) ROOT="${2:-}"; shift 2 ;;
    -y) MERGE_ALLOWLIST=yes; shift ;;
    *) echo "usage: install.sh --root <abs-path-to-Goodword> [-y]"; exit 2 ;;
  esac
done

FAILS=0
pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1"; FAILS=$((FAILS+1)); }

echo "=== 0. Root validation ==="
case "$ROOT" in
  /*) : ;;
  *) echo "FAIL  --root must be an absolute path (got: '${ROOT:-<empty>}')"; exit 1 ;;
esac
test -d "$ROOT" || { echo "FAIL  root does not exist: $ROOT"; exit 1; }
test -e "$ROOT/api/.git"      && pass "api repo at \$ROOT/api"            || fail "api repo missing: $ROOT/api/.git (clone the api repo under the root)"
test -e "$ROOT/web-app/.git"  && pass "web-app repo at \$ROOT/web-app"    || fail "web-app repo missing: $ROOT/web-app/.git (clone the web-app repo under the root)"
test -d "$ROOT/goodword-kb/wiki" && pass "knowledge base at \$ROOT/goodword-kb" || fail "knowledge base missing: $ROOT/goodword-kb/wiki (clone goodword-kb under the root)"
[ "$FAILS" = 0 ] || { echo "=== ABORT: fix the root layout first ($FAILS failures) ==="; exit 1; }

echo "=== 1. Preconditions (asserted, never installed) ==="
# Capability probe, not an OS assertion: the only platform-specific thing the
# workflows do is surface human packets with a browser opener. xdg-open is probed
# first because on some Linux distros /usr/bin/open is util-linux's link to
# openvt(1) — a different program, not a missing one.
OPENER="$(command -v xdg-open 2>/dev/null || command -v open 2>/dev/null || true)"
[ -n "$OPENER" ] && pass "browser opener: $OPENER ($(uname -s))" || fail "no browser opener ($(uname -s)): install xdg-open (Linux) — human packets would print a file:// path only"

for cmd in bun pnpm mise gh python3 agent-browser claude aws git curl diff; do
  command -v "$cmd" >/dev/null 2>&1 && pass "command: $cmd" || fail "command missing: $cmd (install it, then re-run)"
done

# Port ownership (preflight/cleanup own 4123 and 3123). Any ONE of these backends
# is enough — the workflows try them in this order. Without one, a "port busy"
# check would silently pass and let two runs collide.
PORT_TOOL="$(command -v lsof 2>/dev/null || command -v ss 2>/dev/null || command -v fuser 2>/dev/null || true)"
[ -n "$PORT_TOOL" ] && pass "port inspection: $PORT_TOOL" || fail "no port-inspection tool: install one of lsof, ss (iproute2), or fuser (psmisc)"

# Python floor: the helpers use subprocess.run(capture_output=...), which raises
# TypeError on 3.6 (RHEL 7/8, Ubuntu 18.04 system python).
if command -v python3 >/dev/null 2>&1; then
  python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 7) else 1)' 2>/dev/null \
    && pass "python3 >= 3.7 ($(python3 -V 2>&1))" \
    || fail "python3 3.7+ required (helpers use subprocess capture_output=); found $(python3 -V 2>&1)"
fi

if command -v mise >/dev/null 2>&1; then
  mise x node@20 -- node -v 2>/dev/null | "$GREP" -q '^v20' && pass "mise node@20" || fail "node 20 via mise unavailable (mise install node@20)"
  mise x node@22 -- node -v 2>/dev/null | "$GREP" -q '^v22' && pass "mise node@22" || fail "node 22 via mise unavailable (mise install node@22)"
fi

if command -v gh >/dev/null 2>&1; then
  gh auth status >/dev/null 2>&1 && pass "gh authenticated" || fail "gh not authenticated (gh auth login)"
  git -C "$ROOT/api" ls-remote --heads origin >/dev/null 2>&1 && pass "fetch access to api remote (Goodword org)" || fail "cannot fetch $ROOT/api origin — check your GitHub org access"
  git -C "$ROOT/web-app" ls-remote --heads origin >/dev/null 2>&1 && pass "fetch access to web-app remote" || fail "cannot fetch $ROOT/web-app origin — check your GitHub org access"
fi

aws sts get-caller-identity >/dev/null 2>&1 && pass "aws session valid" || fail "aws session expired or unconfigured — run: aws login (SSO creds last ~15 min; also needed before every run)"

BILLING_OK=yes
for v in ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN CLAUDE_API_KEY CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_PROFILE; do
  if [ -n "$(printenv "$v" || true)" ]; then fail "billing guard: $v is set — unset it; a key var silently flips billing from subscription to metered API"; BILLING_OK=no; fi
done
if "$GREP" -q 'apiKeyHelper' "$HOME/.claude/settings.json" 2>/dev/null; then
  fail "billing guard: apiKeyHelper configured in ~/.claude/settings.json — remove it"; BILLING_OK=no
fi
[ "$BILLING_OK" = yes ] && pass "billing guard: no API-key vars, no apiKeyHelper (subscription OAuth stays active)"
echo "INFO  claude login itself cannot be verified statically — make sure you ran /login (subscription OAuth); the first workflow run proves it"

test -f "$ROOT/api/.env"     && pass "api/.env present"     || fail "api/.env missing — obtain it from a teammate (never distributed in this gist)"
test -f "$ROOT/web-app/.env" && pass "web-app/.env present" || fail "web-app/.env missing — obtain it from a teammate (never distributed in this gist)"

if CE_RESOLVED="$(ce_resolve)"; then
  CE_VER="${CE_RESOLVED%% *}"
  CE_CACHE="${CE_RESOLVED#* }"
  if [ "$CE_VER" = "$CE_VALIDATED" ]; then
    pass "compound-engineering plugin $CE_VER (validated baseline)"
  else
    pass "compound-engineering plugin $CE_VER (>= $CE_MIN)"
    echo "WARN  CE $CE_VER is newer than the validated baseline $CE_VALIDATED."
    echo "      Staging prefers the validated contract: a cached $CE_VALIDATED, else the"
    echo "      vendored $CE_VALIDATED snapshot shipped in this payload (announced with a"
    echo "      NOTE). A newer cache version is staged only as a last resort, and only"
    echo "      when its contract markers hold. No action needed."
  fi
else
  pass "compound-engineering: no cached version >= $CE_MIN — staging will use the vendored $CE_VALIDATED snapshot shipped in this payload"
  echo "INFO  Installing the plugin is still recommended for the other CE skills:"
  echo "        claude plugin marketplace add EveryInc/compound-engineering-plugin"
  echo "        claude plugin install compound-engineering@compound-engineering-plugin"
fi

[ "$FAILS" = 0 ] || { echo "=== ABORT: $FAILS precondition failure(s) above — fix and re-run ==="; exit 1; }

echo "=== 2. Archon CLI ($ARCHON_PIN pinned) ==="
CUR="$(archon --version 2>/dev/null </dev/null | "$HEAD" -1 || true)"
if echo "$CUR" | "$GREP" -q "Archon CLI $ARCHON_PIN"; then
  pass "archon $ARCHON_PIN already installed"
else
  [ -n "$CUR" ] && echo "INFO  replacing '$CUR' with pinned $ARCHON_PIN in ~/.local/bin"
  TMPI="$(mktemp -d)"
  curl -fsSL https://archon.diy/install -o "$TMPI/archon-install.sh" || { echo "FAIL  could not download archon installer"; exit 1; }
  VERSION="$ARCHON_PIN" INSTALL_DIR="$HOME/.local/bin" bash "$TMPI/archon-install.sh" || { echo "FAIL  archon install failed"; exit 1; }
  rm -rf "$TMPI"
  archon --version </dev/null | "$GREP" -q "Archon CLI $ARCHON_PIN" || { echo "FAIL  archon on PATH is not $ARCHON_PIN — is ~/.local/bin on your PATH before other installs?"; exit 1; }
  pass "archon $ARCHON_PIN installed to ~/.local/bin"
fi
case ":$PATH:" in *":$HOME/.local/bin:"*) : ;; *) echo "WARN  ~/.local/bin is not on PATH — add it to your shell profile" ;; esac

echo "=== 3. Render .archon/ payload into \$ROOT ==="
found=0
for flat in "$SRC"/archon__*; do
  [ -e "$flat" ] || continue
  found=$((found+1))
  rel="$(basename "$flat")"; rel="${rel#archon__}"; rel="${rel//__//}"
  dest="$ROOT/.archon/$rel"
  mkdir -p "$(dirname "$dest")"
  python3 - "$flat" "$dest" "$ROOT" "$PLACEHOLDER" <<'PY'
import sys
src, dest, root, ph = sys.argv[1:5]
data = open(src, encoding="utf-8").read()
open(dest, "w", encoding="utf-8").write(data.replace(ph, root))
PY
  case "$dest" in *.sh) chmod +x "$dest" ;; esac
done
[ "$found" -gt 0 ] || { echo "FAIL  no archon__* payload files next to install.sh — run from a full gist clone"; exit 1; }
cp "$SRC/VERSION" "$ROOT/.archon/VERSION"
pass "rendered $found payload files into $ROOT/.archon (VERSION $(cat "$ROOT/.archon/VERSION"))"
mkdir -p "$ROOT/.omc/research"
cp "$SRC/toy-feature-spec.md" "$ROOT/.omc/research/toy-feature-spec.md"
pass "toy fixture at \$ROOT/.omc/research/toy-feature-spec.md"

echo "=== 4. Stage CE skills (project-scope symlinks) ==="
bash "$ROOT/.archon/setup/stage-skills.sh" "$ROOT" || { echo "FAIL  skill staging failed (CE version drift?) — see output above"; exit 1; }
pass "CE skills staged"

echo "=== 5. One-time folder registration (register-probe) ==="
REG_LOG="$(mktemp)"
( cd "$ROOT" && DISABLE_OMC=1 archon workflow run register-probe --folder --no-worktree "setup" </dev/null >"$REG_LOG" 2>&1 )
REG_RC=$?
if [ $REG_RC -ne 0 ] || ! "$GREP" -q "REGISTER_PROBE_OK" "$REG_LOG"; then
  echo "FAIL  register-probe did not pass (exit $REG_RC) — full output:"; cat "$REG_LOG"; exit 1
fi
"$GREP" -q "BASE_BRANCH=\[main\]" "$REG_LOG" || { echo "FAIL  BASE_BRANCH did not resolve to [main] — check .archon/config.yaml"; cat "$REG_LOG"; exit 1; }
pass "folder registered: ARTIFACTS_DIR populated, BASE_BRANCH=[main]"
rm -f "$REG_LOG"

echo "=== 6. Validate workflows ==="
# archon validates its own bundled workflows too, and those can error on this
# machine (e.g. a bundled MCP config we don't ship) — gate on OUR workflows only.
VAL_LOG="$(mktemp)"
( cd "$ROOT" && archon validate workflows </dev/null >"$VAL_LOG" 2>&1 ) || true
VAL_FAIL=0
for w in babysit bugfix bugfix-smoke-deployed cleanup full-sdlc-api full-sdlc-web register-probe; do
  if "$GREP" -Eq "^[[:space:]]+$w[[:space:]]+ok[[:space:]]*$" "$VAL_LOG"; then
    pass "workflow validates: $w"
  else
    echo "FAIL  workflow not ok: $w — validator output:"
    "$GREP" -A4 "  $w " "$VAL_LOG" || cat "$VAL_LOG"
    VAL_FAIL=1
  fi
done
[ "$VAL_FAIL" = 0 ] || exit 1
rm -f "$VAL_LOG"

echo "=== 7. Permissions allowlist ==="
if [ "$MERGE_ALLOWLIST" = yes ]; then
  python3 - "$SRC/allowlist.json" "$ROOT/.claude/settings.json" <<'PY'
import json, os, sys
src, dest = sys.argv[1:3]
add = json.load(open(src, encoding="utf-8"))["permissions"]["allow"]
cur = {}
if os.path.exists(dest):
    cur = json.load(open(dest, encoding="utf-8"))
before = json.dumps(cur, indent=2, sort_keys=True)
allow = cur.setdefault("permissions", {}).setdefault("allow", [])
new = [r for r in add if r not in allow]
allow.extend(new)
after = json.dumps(cur, indent=2, sort_keys=True)
print(f"merging {len(new)} new rule(s) into {dest} ({len(add)-len(new)} already present)")
for r in new:
    print(f"  + {r}")
if new:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w", encoding="utf-8") as f:
        f.write(after + "\n")
PY
  pass "allowlist merged additively into \$ROOT/.claude/settings.json"
else
  echo "INFO  skipped (re-run with -y to merge the derived allowlist into \$ROOT/.claude/settings.json; it is additive and shows every added rule)"
fi

echo
echo "=== DONE — next steps ==="
echo "1. Before every run: 'aws login' (SSO creds last ~15 min; api boot reads Secrets Manager)."
echo "2. Read the runbook: $ROOT/.archon/RUNBOOK.md"
echo "   (Driving it from Claude Code? The 'archon-sdlc' skill is staged at"
echo "    \$ROOT/.claude/skills/archon-sdlc — say 'archon-sdlc' in a session at the root.)"
echo "3. Start the toy dry-run from the root:"
echo "     cd $ROOT"
echo "     DISABLE_OMC=1 archon workflow run full-sdlc-api \"$ROOT/.omc/research/toy-feature-spec.md\" </dev/null 2>&1 | tee /tmp/archon-run.log"
echo "4. The run pauses at the plan gate. plan-review.html opens in your browser when an"
echo "   opener is available; either way the gate prints the file:// path. Release with:"
echo "     archon workflow approve <run-id> </dev/null >/tmp/archon-approve.log 2>&1 &"
echo "   (backgrounded — approve resumes the run inside the approving CLI process)."
echo "5. Artifacts land under ~/.archon/workspaces/<run-id>/ ; full logs in the file you teed."
echo "6. Runs bill YOUR Claude subscription quota (5-hour windows). Budget caps are quota guards."
echo "7. After a shipped run: the KB gains one wiki/change-history file — run kb:compound in an"
echo "   interactive Claude session to promote its candidates. That curation duty is yours."
