#!/usr/bin/env bash
# Linux compat smoke — dev scaffolding, NOT in package.sh's MANIFEST.
#
# Builds the gist payload from the live tree (package.sh, no --publish), then
# runs install.sh and the ported shell/Python snippets inside a linux/amd64
# container. Every assertion is printed as OK/FAIL and the script exits non-zero
# on the first FAIL.
#
#   bash .archon/setup/linux-smoke/run.sh
#
# WHAT THIS CANNOT PROVE — it is an install-path and portability smoke, nothing
# more. All of the following still need the desktop-Linux pilot (milestone M4.3):
#   * a real billed lane run. T11 runs the toy lane's `preflight` node — the one
#     that asserts the staged skills and derives the run identity — but the other
#     27 nodes include 11 that need a logged-in `claude`, plus real repos, .env
#     files, and the archon binary itself (stubbed here).
#   * a live Claude session LOADING the two operator skills. T10 proves the
#     discovery contract (SKILL.md through the symlink, frontmatter name matching
#     the directory, real description) and T12 proves no workflow node declares
#     them — but only a real session on Linux proves they are offered to a model.
#   * agent-browser against real Chromium and its system libraries
#   * Vite/bun bind behaviour and localhost-vs-::1 resolution under a real server
#   * AWS SSO, gh org access, and a genuine compound-engineering 3.2.0 cache
#   * AVX2 availability on the operator's actual CPU
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ARCHON="$(cd "$HERE/../.." && pwd)"
IMAGE=archon-linux-smoke
FAILS=0
ok()   { echo "OK    $1"; }
bad()  { echo "FAIL  $1"; FAILS=$((FAILS+1)); }

command -v docker >/dev/null 2>&1 || { echo "FAIL  docker is required for this smoke"; exit 1; }

echo "=== build payload from the live tree ==="
bash "$ARCHON/setup/package.sh" || { echo "FAIL  package.sh did not build"; exit 1; }
test -d "$ARCHON/dist/gist" || { echo "FAIL  no dist/gist"; exit 1; }

echo "=== stage container context ==="
CTX="$(mktemp -d /tmp/archon-linux-smoke.XXXXXX)"
trap 'rm -rf "$CTX"' EXIT
cp -R "$ARCHON/dist/gist" "$CTX/gist"
cp "$HERE/Dockerfile" "$CTX/Dockerfile"

# ---------------------------------------------------------------------------
# The in-container script. Kept here (rather than as a third file) so run.sh and
# the assertions it makes stay in one place.
# ---------------------------------------------------------------------------
cat > "$CTX/inside.sh" <<'INSIDE'
set -uo pipefail
FAILS=0
ok()  { echo "OK    $1"; }
bad() { echo "FAIL  $1"; FAILS=$((FAILS+1)); }
sec() { echo; echo "--- $1"; }

GIST=/smoke/gist
ROOT=/work/root
STUBS=/opt/stubs
export HOME=/root

# --- fake Goodword root ----------------------------------------------------
mkdir -p "$ROOT" /work/remotes
git config --global user.email smoke@example.invalid
git config --global user.name  "linux smoke"
git config --global init.defaultBranch main
for r in api web-app; do
  git init -q --bare "/work/remotes/$r.git"
  git init -q "$ROOT/$r"
  ( cd "$ROOT/$r" && echo x > README.md && git add -A && git commit -qm init \
    && git remote add origin "/work/remotes/$r.git" && git push -q origin main )
  printf 'STUB=1\n' > "$ROOT/$r/.env"
done
mkdir -p "$ROOT/goodword-kb/wiki"
( cd "$ROOT/goodword-kb" && git init -q && git add -A 2>/dev/null; true )

# --- fake pinned compound-engineering 3.2.0 cache --------------------------
CE="$HOME/.claude/plugins/cache/compound-engineering-plugin/compound-engineering/3.2.0/skills"
mkdir -p "$CE/ce-code-review" "$CE/ce-doc-review"
# Frontmatter shape mirrors the real 3.2.0 skills (name + description), so the
# T10 discovery contract is checked uniformly rather than exempting the fixtures.
cat > "$CE/ce-code-review/SKILL.md" <<'CEEOF'
---
name: ce-code-review
description: Stub standing in for compound-engineering 3.2.0 ce-code-review during the Linux smoke.
---
Tokens the lane's preflight and stage-skills.sh assert: mode:headless and the "verdict" contract key.
CEEOF
cat > "$CE/ce-doc-review/SKILL.md" <<'CEEOF'
---
name: ce-doc-review
description: Stub standing in for compound-engineering 3.2.0 ce-doc-review during the Linux smoke.
---
Token the lane's preflight and stage-skills.sh assert: mode:headless.
CEEOF

# --- stub executables ------------------------------------------------------
mkdir -p "$STUBS"
for c in bun pnpm agent-browser claude; do
  printf '#!/bin/sh\nexit 0\n' > "$STUBS/$c"
done
cat > "$STUBS/mise" <<'EOF'
#!/bin/sh
case "$*" in
  *node@20*) echo v20.19.0 ;;
  *node@22*) echo v22.14.0 ;;
  *) exit 0 ;;
esac
EOF
cat > "$STUBS/gh" <<'EOF'
#!/bin/sh
case "$1 ${2-}" in "auth status") exit 0 ;; esac
exit 0
EOF
cat > "$STUBS/aws" <<'EOF'
#!/bin/sh
case "$*" in *"get-caller-identity"*) echo '{"Account":"000000000000"}'; exit 0 ;; esac
exit 0
EOF
cat > "$STUBS/archon" <<'EOF'
#!/bin/sh
case "$*" in
  "--version") echo "Archon CLI v0.8.0"; exit 0 ;;
  *"workflow run register-probe"*)
      echo "REGISTER_PROBE_OK"; echo "BASE_BRANCH=[main]"; exit 0 ;;
  *"validate workflows"*)
      for w in babysit cleanup full-sdlc-api full-sdlc-web register-probe; do
        printf '  %s  ok\n' "$w"
      done
      exit 0 ;;
esac
exit 0
EOF
chmod +x "$STUBS"/*
BASE_PATH="$PATH"
export PATH="$STUBS:$BASE_PATH"

# ===========================================================================
sec "T1  negative control: no stubs — must fail on missing commands, NOT on the OS"
OUT="$(PATH="$BASE_PATH" bash "$GIST/install.sh" --root "$ROOT" 2>&1)"
if echo "$OUT" | grep -q 'unsupported OS'; then
  bad "T1 the uname gate is still present — the B1 edit did not land"
else
  ok "T1 no OS assertion in the failure output"
fi
echo "$OUT" | grep -q 'FAIL  command missing: bun' \
  && ok "T1 fails on the real missing precondition (bun)" \
  || bad "T1 did not fail on a missing command"
echo "$OUT" | grep -q 'PASS  browser opener: /usr/bin/xdg-open' \
  && ok "T1 opener probe resolves xdg-open" \
  || bad "T1 opener probe did not resolve xdg-open"

# ===========================================================================
sec "T2  full install with stubs — DONE, zero FAIL"
LOG=/work/install.log
bash "$GIST/install.sh" --root "$ROOT" -y > "$LOG" 2>&1; RC=$?
grep -q '=== DONE — next steps ===' "$LOG" \
  && ok "T2 reached DONE" || { bad "T2 did not reach DONE (rc=$RC)"; tail -30 "$LOG"; }
NF=$(grep -c '^FAIL' "$LOG" || true)
[ "$NF" = 0 ] && ok "T2 zero FAIL lines" || { bad "T2 $NF FAIL line(s)"; grep '^FAIL' "$LOG"; }
grep -q 'PASS  port inspection: /usr/bin/lsof' "$LOG" \
  && ok "T2 port-tool assertion present" || bad "T2 no port-tool assertion"
grep -q 'PASS  python3 >= 3.7' "$LOG" \
  && ok "T2 python floor asserted" || bad "T2 python floor not asserted"
for c in curl diff; do
  grep -q "PASS  command: $c" "$LOG" && ok "T2 asserts $c" || bad "T2 does not assert $c"
done

# ===========================================================================
sec "T3  rendered payload carries no placeholder and no absolute author path"
if grep -rl '{{GOODWORD_ROOT}}' "$ROOT/.archon" >/dev/null 2>&1; then
  bad "T3 unrendered placeholder survives in \$ROOT/.archon"
  grep -rl '{{GOODWORD_ROOT}}' "$ROOT/.archon" | head -5
else
  ok "T3 no unrendered placeholder"
fi
if grep -rlE '/Use''rs/' "$ROOT/.archon" >/dev/null 2>&1; then
  bad "T3 an author-machine absolute path leaked into the payload"
  grep -rlE '/Use''rs/' "$ROOT/.archon" | head -5
else
  ok "T3 no author-machine path in the payload"
fi
grep -q "$ROOT" "$ROOT/.archon/workflows/full-sdlc-api.yaml" \
  && ok "T3 workflows rendered with this root" || bad "T3 root not rendered into workflows"

# ===========================================================================
sec "T4  staged skills — four resolving links"
for s in ce-code-review ce-doc-review archon-sdlc archon-install; do
  L="$ROOT/.claude/skills/$s"
  if [ -L "$L" ] && [ -f "$L/SKILL.md" ]; then ok "T4 $s resolves"; else bad "T4 $s missing or dangling"; fi
done

# ===========================================================================
sec "T5  opener block — honest message with and without xdg-open"
python3 - "$ROOT/.archon/workflows/full-sdlc-api.yaml" /work/opener.sh <<'PY'
import re, sys, textwrap
src = open(sys.argv[1], encoding="utf-8").read()
m = re.search(r'^([ \t]*)OPENER="\$\(command -v xdg-open.*?\n\1fi\n', src, re.S | re.M)
assert m, "could not extract the opener block from the plan-render gate"
block = textwrap.dedent(m.group(0))
assert "opened in browser" in block and "no browser opener" in block, block
open(sys.argv[2], "w", encoding="utf-8").write('set -uo pipefail\nH="$1"\n' + block)
PY
touch /work/packet.html

# 5a: an opener that SUCCEEDS -> the "opened in browser" branch, and it really ran.
printf '#!/bin/sh\nprintf "%%s\\n" "$1" > /work/opener.called\nexit 0\n' > "$STUBS/xdg-open"
chmod +x "$STUBS/xdg-open"
rm -f /work/opener.called
O1="$(bash /work/opener.sh /work/packet.html 2>&1)"
echo "$O1" | grep -q 'packet=file:///work/packet.html' \
  && ok "T5a prints the file:// path" || bad "T5a no file:// path: $O1"
echo "$O1" | grep -q '(opened in browser)' \
  && ok "T5a claims 'opened in browser' when the opener succeeded" || bad "T5a did not claim open: $O1"
grep -q '/work/packet.html' /work/opener.called 2>/dev/null \
  && ok "T5a the opener was actually invoked on the packet" || bad "T5a opener was never invoked"
rm -f "$STUBS/xdg-open"

# 5b: the REAL xdg-open, which exists here but fails (headless, no browser, no
# DISPLAY). Present-but-failing must not produce a false "opened" claim — that is
# precisely the lie the old unconditional message told.
O2="$(bash /work/opener.sh /work/packet.html 2>&1)"
echo "$O2" | grep -q 'packet=file:///work/packet.html' \
  && ok "T5b prints the file:// path when the opener fails" || bad "T5b lost the path: $O2"
if echo "$O2" | grep -q 'opened in browser'; then
  bad "T5b falsely claims 'opened in browser' though xdg-open failed: $O2"
else
  ok "T5b no false 'opened in browser' when the opener fails"
fi

# 5c: no opener resolvable at all.
XDG="$(command -v xdg-open)"
mv "$XDG" "$XDG.hidden"
O3="$(bash /work/opener.sh /work/packet.html 2>&1)"
mv "$XDG.hidden" "$XDG"
echo "$O3" | grep -q 'packet=file:///work/packet.html' \
  && ok "T5c still prints the file:// path with no opener at all" || bad "T5c lost the path: $O3"
echo "$O3" | grep -q 'no browser opener' \
  && ok "T5c says why it did not open" || bad "T5c gave no reason: $O3"

# ===========================================================================
sec "T6  port_pids — lsof backend, then ss backend"
python3 - "$ROOT/.archon/workflows/full-sdlc-api.yaml" /work/portlib.sh <<'PY'
import re, sys, textwrap
src = open(sys.argv[1], encoding="utf-8").read()
m = re.search(r'^([ \t]*)port_pids\(\) \{.*?\n\1\}\n', src, re.S | re.M)
assert m, "could not extract port_pids from the workflow"
block = textwrap.dedent(m.group(0))
for backend in ("lsof", "ss", "fuser"):
    assert backend in block, backend
open(sys.argv[2], "w", encoding="utf-8").write(block)
PY
python3 - <<'PY' &
import socket, time
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", 4123)); s.listen(1); time.sleep(60)
PY
LPID=$!
sleep 1
# shellcheck disable=SC1091
. /work/portlib.sh
GOT="$(port_pids 4123)"
[ "$GOT" = "$LPID" ] && ok "T6 lsof backend returns the listener PID ($GOT)" \
  || bad "T6 lsof backend returned '$GOT', expected $LPID"
[ -z "$(port_pids 4124)" ] && ok "T6 free port returns nothing" || bad "T6 free port returned PIDs"
LSOF="$(command -v lsof)"; SS="$(command -v ss)"; FUSER="$(command -v fuser)"
mv "$LSOF" "$LSOF.hidden"
GOT2="$(port_pids 4123)"
mv "$LSOF.hidden" "$LSOF"
[ "$GOT2" = "$LPID" ] && ok "T6 ss backend returns the listener PID ($GOT2)" \
  || bad "T6 ss backend returned '$GOT2', expected $LPID"
# No backend at all must be rc 1 — never a silent "port is free".
( for b in "$LSOF" "$SS" "$FUSER"; do mv "$b" "$b.hidden"; done
  . /work/portlib.sh; port_pids 4123 >/dev/null; echo "rc=$?" > /work/nobackend.rc
  for b in "$LSOF" "$SS" "$FUSER"; do mv "$b.hidden" "$b"; done )
grep -q 'rc=1' /work/nobackend.rc \
  && ok "T6 no backend returns rc 1 (hard stop, not 'free')" || bad "T6 no-backend rc was $(cat /work/nobackend.rc)"
kill "$LPID" 2>/dev/null

# ===========================================================================
sec "T7  Python helpers under the C locale with em-dash payloads"
# LC_ALL=C alone does NOT reproduce the bug: PEP 538 coerces the C locale to
# C.UTF-8 on python >= 3.7, so getpreferredencoding() still returns UTF-8 and a
# bare open() succeeds. Disabling that coercion (and UTF-8 mode) is what actually
# produces the ASCII codec, which is the condition the fix is for.
ASCII_ENV="LC_ALL=C LANG=C PYTHONCOERCECLOCALE=0 PYTHONUTF8=0"
# shellcheck disable=SC2086
ENC="$(env $ASCII_ENV python3 -c 'import locale;print(locale.getpreferredencoding())')"
echo "LOCALE preferred encoding under \$ASCII_ENV: $ENC"
case "$ENC" in
  ANSI_X3.4-1968|ascii|ASCII|US-ASCII) ok "T7 the ASCII-locale condition is real here" ;;
  *) bad "T7 could not produce an ASCII locale ($ENC) — the rest of T7 proves nothing" ;;
esac
mkdir -p /work/round-2
cat > /work/round-2/fixer-result.json <<'JSON'
{"applied": ["fixed the boot path — twice"],
 "failed": [],
 "advisory": [{"finding": "pre-existing drift — out of scope", "action": "Waived: unrelated to this unit — see the plan"}],
 "incomplete": []}
JSON
cat > /work/premises.json <<'JSON'
[{"id": 1, "question": "does the row-status gate survivors' sync — really?", "answer": "no — see below"}]
JSON
H="$ROOT/.archon/setup"
# shellcheck disable=SC2086
run_c() { env $ASCII_ENV python3 "$@" 2>&1; }

# NEGATIVE CONTROL: with the encoding un-pinned, this same fixture MUST blow up.
# Without this, a green T7 could just mean the locale never mattered.
# shellcheck disable=SC2086
NEG="$(env $ASCII_ENV python3 -c 'open("/work/round-2/fixer-result.json").read()' 2>&1)"
if echo "$NEG" | grep -q 'UnicodeDecodeError'; then
  ok "T7 negative control: a bare open() DOES raise UnicodeDecodeError here"
else
  bad "T7 negative control did not raise — the helpers below would pass regardless of the fix: $NEG"
fi
for probe in \
  "check-fixer-result.py /work/round-2/fixer-result.json" \
  "update-waivers.py /work/round-2/fixer-result.json /work/waivers.md" \
  "strip-premise-answers.py /work/premises.json /work/questions.json" \
  "write-review-summary.py /work/summary.json Ready false 0"
do
  # shellcheck disable=SC2086
  OUT="$(run_c $H/$probe)"; RC=$?
  NAME="${probe%% *}"
  if [ $RC -eq 0 ] && ! echo "$OUT" | grep -q 'UnicodeDecodeError\|UnicodeEncodeError'; then
    ok "T7 $NAME clean under LANG=C"
  else
    bad "T7 $NAME failed under LANG=C: $OUT"
  fi
done
grep -q 'out of scope' /work/waivers.md \
  && ok "T7 waiver ledger round-tripped the em-dash entry" || bad "T7 waiver ledger content wrong"

# ===========================================================================
sec "T10  skill-discovery contract for every staged skill"
python3 - "$ROOT/.claude/skills" <<'PY' && ok "T10 all staged skills satisfy the discovery contract" || bad "T10 discovery contract violated"
import os, sys, yaml
root = sys.argv[1]
expected = {"ce-code-review", "ce-doc-review", "archon-sdlc", "archon-install"}
seen, problems = set(), []
for name in sorted(os.listdir(root)):
    d = os.path.join(root, name)
    sk = os.path.join(d, "SKILL.md")
    if not os.path.isfile(sk):
        continue
    seen.add(name)
    # Claude Code discovers a project skill by <dir>/SKILL.md + YAML frontmatter.
    text = open(sk, encoding="utf-8").read()
    if not text.startswith("---\n"):
        problems.append(name + ": no leading frontmatter fence")
        continue
    fm = yaml.safe_load(text.split("---\n", 2)[1])
    if not isinstance(fm, dict):
        problems.append(name + ": frontmatter is not a mapping")
        continue
    if fm.get("name") != name:
        problems.append(name + ": frontmatter name=" + repr(fm.get("name")) + " != directory name")
    if not isinstance(fm.get("description"), str) or len(fm["description"]) < 20:
        problems.append(name + ": missing or trivial description")
    # The gist flattens on '__' and install.sh un-flattens on it.
    if "__" in name or any("__" in f for _, _, fs in os.walk(d) for f in fs):
        problems.append(name + ": '__' in a skill path component")
    kind = "symlink" if os.path.islink(d) else "dir"
    print("    %s: name+description ok, %d bytes via %s" % (name, len(text), kind))
missing = expected - seen
if missing:
    problems.append("missing skills: %s" % sorted(missing))
for b in problems:
    print("    VIOLATION:", b)
sys.exit(1 if problems else 0)
PY
# Readable through the symlink from an unrelated cwd (nodes start at the folder root).
( cd / && head -1 "$ROOT/.claude/skills/archon-sdlc/SKILL.md" >/dev/null ) \
  && ok "T10 skills resolve from a foreign cwd" || bad "T10 skill unreadable from a foreign cwd"

# ===========================================================================
sec "T11  the toy lane's own preflight node, executed on Linux"
# This is literally node 1 of `archon workflow run full-sdlc-api <toy spec>` — the
# node that asserts the STAGED SKILLS, the ports, the billing guard, and derives
# the run's identity. The 11 AI/approval nodes after it need a logged-in claude
# and cannot run here; see the header.
python3 - "$ROOT/.archon/workflows/full-sdlc-api.yaml" /work/preflight.sh <<'PY'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
node = next(n for n in d["nodes"] if n["id"] == "preflight")
open(sys.argv[2], "w", encoding="utf-8").write(node["bash"])
PY
export ARTIFACTS_DIR=/work/artifacts
mkdir -p "$ARTIFACTS_DIR"
export ARGUMENTS="$ROOT/.omc/research/toy-feature-spec.md"
test -f "$ARGUMENTS" && ok "T11 toy spec installed at the documented path" || bad "T11 toy spec missing"
PF="$(bash /work/preflight.sh 2>&1)"; PFRC=$?
echo "$PF" | sed 's/^/    /'
[ $PFRC -eq 0 ] && ok "T11 preflight exits 0 on Linux" || bad "T11 preflight exited $PFRC"
echo "$PF" | grep -q 'PREFLIGHT=PASS' && ok "T11 PREFLIGHT=PASS" || bad "T11 no PREFLIGHT=PASS"
echo "$PF" | grep -q 'slug=toy-feature-spec branch=archon/toy-feature-spec' \
  && ok "T11 run identity derived correctly" || bad "T11 params derivation wrong"
python3 -c "
import json
d = json.load(open('$ARTIFACTS_DIR/params.json', encoding='utf-8'))
assert d['branch'] == 'archon/toy-feature-spec', d
assert d['worktree'] == '$ROOT/api/.worktrees/toy-feature-spec', d
print('    params.json:', d)
" && ok "T11 params.json durable and correct" || bad "T11 params.json wrong"
# Negative control: preflight must FAIL when a staged skill is missing.
mv "$ROOT/.claude/skills/ce-code-review" /work/ce-code-review.hidden
NEG="$(bash /work/preflight.sh 2>&1)"; NEGRC=$?
mv /work/ce-code-review.hidden "$ROOT/.claude/skills/ce-code-review"
if [ $NEGRC -ne 0 ] && echo "$NEG" | grep -q 'PREFLIGHT=FAIL staged ce-code-review missing'; then
  ok "T11 negative control: preflight fails loudly when a skill is unstaged"
else
  bad "T11 negative control did not fire (rc=$NEGRC): $NEG"
fi

# ===========================================================================
sec "T12  node-scope discipline — workflows must NOT load the operator skills"
DECL="$(grep -h 'skills:' "$ROOT"/.archon/workflows/*.yaml | tr -d ' ' | sort -u)"
echo "$DECL" | sed 's/^/    /'
if echo "$DECL" | grep -q 'archon-sdlc\|archon-install'; then
  bad "T12 a workflow node declares an operator skill — the WORKFLOW-NODE-STOP design is broken"
else
  ok "T12 no workflow node declares archon-sdlc or archon-install"
fi
if echo "$DECL" | grep -q 'skills:\[ce-code-review\]' && echo "$DECL" | grep -q 'skills:\[ce-doc-review\]'; then
  ok "T12 the only declared skills are the two CE ones"
else
  bad "T12 unexpected skills declaration set"
fi

echo
[ "$FAILS" = 0 ] && echo "LINUX_SMOKE=PASS" || echo "LINUX_SMOKE=FAIL ($FAILS)"
exit "$FAILS"
INSIDE

# ---------------------------------------------------------------------------
# Guard-removal cases run as separate container invocations so a broken host
# cannot leak into the main pass.
# ---------------------------------------------------------------------------
cat > "$CTX/nogrep.sh" <<'NOGREP'
set -uo pipefail
# T8: with no grep resolvable, install.sh must fail LOUDLY rather than print a
# passing billing guard it never evaluated.
mv /usr/bin/grep /usr/bin/grep.hidden
OUT="$(bash /smoke/gist/install.sh --root /tmp/nonexistent-root 2>&1)"; RC=$?
mv /usr/bin/grep.hidden /usr/bin/grep
echo "$OUT"
echo "rc=$RC"
echo "$OUT" | /usr/bin/grep -q 'cannot resolve a grep binary' \
  || { echo "FAIL  T8 install.sh did not fail loudly without grep"; exit 1; }
[ "$RC" -ne 0 ] || { echo "FAIL  T8 exit code was 0"; exit 1; }
if echo "$OUT" | /usr/bin/grep -q 'billing guard'; then
  echo "FAIL  T8 printed a billing-guard verdict with no working grep"; exit 1
fi
echo "OK    T8 install.sh fails loudly with no grep, and prints no billing verdict"
NOGREP

cat > "$CTX/py36.sh" <<'PY36'
set -uo pipefail
# T9: the python floor must FAIL on 3.6, and say so.
OUT="$(bash /smoke/gist/install.sh --root /work/root 2>&1)"
echo "$OUT" | grep -q 'FAIL  python3 3.7+ required' \
  || { echo "FAIL  T9 the 3.7 floor did not fire on $(python3 -V 2>&1)"; echo "$OUT" | head -20; exit 1; }
echo "OK    T9 python floor fires on $(python3 -V 2>&1)"
PY36

echo "=== build image ($IMAGE, linux/amd64) ==="
docker build --platform linux/amd64 -t "$IMAGE" "$CTX" >/dev/null || { echo "FAIL  docker build"; exit 1; }

echo
echo "=== main pass (T1-T7) ==="
docker run --rm --platform linux/amd64 -v "$CTX:/smoke:ro" "$IMAGE" bash /smoke/inside.sh \
  && ok "main pass" || bad "main pass"

echo
echo "=== T8: install.sh with grep removed ==="
docker run --rm --platform linux/amd64 -v "$CTX:/smoke:ro" "$IMAGE" bash /smoke/nogrep.sh \
  && ok "T8" || bad "T8"

echo
echo "=== T9: python 3.6 floor (python:3.6-slim) ==="
docker run --rm --platform linux/amd64 -v "$CTX:/smoke:ro" python:3.6-slim bash -c '
  set -e
  mkdir -p /work/root/api /work/root/web-app /work/root/goodword-kb/wiki
  touch /work/root/api/.git /work/root/web-app/.git
  bash /smoke/py36.sh' \
  && ok "T9" || bad "T9"

echo
if [ "$FAILS" = 0 ]; then
  echo "LINUX_SMOKE=PASS (install path + portability only — see the header for what this does NOT prove)"
else
  echo "LINUX_SMOKE=FAIL ($FAILS)"
fi
exit "$FAILS"
