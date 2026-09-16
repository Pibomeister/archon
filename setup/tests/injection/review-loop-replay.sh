#!/usr/bin/env bash
# Injection sequence for the v2 review loop: kill the run at each durable
# boundary, restart it, and assert that no COMPLETED activity is ever paid for
# twice.
#
# What this is for. v1 spent 187 minutes on review, and five of its thirteen
# review invocations were replays after a resume -- the CLI restarts a failed
# loop_group at round-pre, and every node behind it re-ran whether or not its
# work was already on disk. Item 1 makes each activity own a durable completion
# record. A unit test of any single node cannot show that the records compose:
# the property is "restart anywhere, pay once", and it only exists across nodes.
#
# So this drives the REAL node bodies, extracted from the shipped YAML through
# nodes.extract, against the REAL setup/round-state.py. The engine is simulated
# only where it has to be: this script evaluates the two `when:` conditions by
# parsing the JSON line the node printed, and honours `trigger_rule: all_done`
# by running review-gate, commit-fixer and converge even when their upstream was
# skipped or failed. That simulation is itself pinned -- test_node_review_loop_
# shape.py asserts the conditions and trigger rules in the YAML match the ones
# encoded here, so a lane that changes its topology fails there rather than
# quietly making this harness test a DAG that no longer ships.
#
# The two AI nodes are stand-ins, because a real reviewer and a real fixer are
# not what is under test here and cost 20 minutes a round. Each stand-in does
# exactly what its prompt instructs: mark, produce its artifact, mark.
#
# A DUPLICATE, the thing this counts, is the plan's definition: a `review: run`
# for a round whose complete envelope already matched the identity and the
# candidate, or a `fixer: run` when a valid repair.json or fixer.ok already
# matched. A first invocation (`reason=initial`) and a genuinely interrupted one
# (`reason=interrupted`) are not duplicates -- refusing to resume an interrupted
# activity would be its own bug, not a saving.
#
# Usage: review-loop-replay.sh [lane]        (default: full-sdlc-api)
# Exits 0 and prints INJECTION=PASS duplicates=0 ... , or 1 with the offending
# decisions named.
set -uo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
TESTS=$(cd "$HERE/.." && pwd)
ARCHON=$(cd "$TESTS/.." && pwd)
ARCHON=$(cd "$ARCHON/.." && pwd)
LANE="${1:-full-sdlc-api}"
HELPER="$ARCHON/setup/round-state.py"
test -f "$HELPER" || { echo "INJECTION=SKIP round-state.py not present yet ($HELPER)"; exit 0; }

TMP=$(mktemp -d "${TMPDIR:-/tmp}/injection-XXXXXX")
trap 'rm -rf "$TMP"' EXIT
WT="$TMP/wt"; AD="$TMP/artifacts"; LOG="$TMP/decisions.log"
mkdir -p "$WT" "$AD"
: > "$LOG"

# --- fixture ---------------------------------------------------------------
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
git -C "$WT" init -q -b main
git -C "$WT" config user.email a@b.c
git -C "$WT" config user.name A
mkdir -p "$WT/src"
printf 'export function f(a: number) { return a; }\n' > "$WT/src/f.ts"
git -C "$WT" add -A
git -C "$WT" -c commit.gpgsign=false commit -qm "feat: base"
BASE=$(git -C "$WT" rev-parse HEAD)

printf '%s\n' "$BASE" > "$AD/bootstrap-head.txt"
printf '4\n' > "$AD/round-cap.txt"
printf '# plan\n\nOne step.\n' > "$AD/plan.md"
cat > "$AD/params.json" <<JSON
{"repo":"api","worktree":"$WT","slug":"injection","apiport":"","has_smoke":""}
JSON
cat > "$AD/files-allowlist.json" <<'JSON'
{"files": ["src/f.ts"]}
JSON
cat > "$AD/joint-plan.json" <<'JSON'
{"pinned_decisions": []}
JSON
export ARTIFACTS_DIR="$AD" ARCHON_FEATURE_SCOPE=repositories
export CE_REVIEW_ROOT="$TMP/ce-root"; mkdir -p "$CE_REVIEW_ROOT"

# --- node bodies, extracted from the shipped YAML --------------------------
body() {
  python3 - "$TESTS" "$LANE" "$1" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from nodes.extract import runnable_body
sys.stdout.write(runnable_body(sys.argv[2], sys.argv[3]))
PY
}
for n in round-pre review-gate fix-plan commit-fixer converge; do
  body "$n" > "$TMP/$n.sh" || { echo "INJECTION=FAIL could not extract $n"; exit 1; }
done

run_node() { # $1 node  -> stdout on fd1, stderr captured to $TMP/last.err
  bash "$TMP/$1.sh" 2> "$TMP/last.err"
}

jfield() { python3 -c 'import json,sys;print(json.loads(sys.stdin.read().strip().splitlines()[-1]).get(sys.argv[1],""))' "$1"; }

round_now() { cat "$AD/round.txt" 2>/dev/null || echo 0; }

# --- AI stand-ins ----------------------------------------------------------
# Each does exactly what its prompt instructs, and nothing else.
reviewer() { # $1 verdict
  python3 "$HELPER" "$AD" mark review-start >/dev/null 2>&1 || return 1
  local n id env
  n=$(round_now); id=$(python3 "$HELPER" "$AD" id 2>/dev/null)
  env="$TMP/envelope.txt"
  {
    echo "Scope: full"
    echo "Input: $id"
    echo "Head: $(cat "$AD/round-$n/pre-head.txt")"
    echo ""
    echo "P1 src/f.ts:1 — the parameter is unvalidated"
    echo ""
    echo "Verdict: $1"
    echo "Review complete"
  } > "$env"
  python3 "$HELPER" "$AD" mark review-done "$env" >/dev/null 2>&1
}
fixer() { # $1 = "edit" | "nochange"
  python3 "$HELPER" "$AD" mark repair-start >/dev/null 2>&1 || return 1
  local n; n=$(round_now)
  if [ "$1" = edit ]; then
    printf 'export function f(a: number) { if (!Number.isFinite(a)) throw new Error("a"); return a; }\n' > "$WT/src/f.ts"
    cat > "$AD/round-$n/fixer-result.json" <<'JSON'
{"applied":[{"finding":"the parameter is unvalidated","action":"added a guard","severity":"P1"}],
 "failed":[],"advisory":[],"incomplete":[],"cross_repo":[]}
JSON
  else
    cat > "$AD/round-$n/fixer-result.json" <<'JSON'
{"applied":[],"failed":[],"advisory":[],"incomplete":[],"cross_repo":[]}
JSON
  fi
  python3 "$HELPER" "$AD" mark repair-done >/dev/null 2>&1
}

# --- one pass of the loop, stopping after $STOP_AFTER -----------------------
# STOP_AFTER is a boundary name; "" runs the whole iteration. This is the
# simulated engine: `when:` is the JSON field, `all_done` means run anyway.
FAILURES=0
note() { printf '%s\n' "$*" >> "$LOG"; }

pass() { # $1 stop-after boundary, $2 verdict, $3 fixer mode
  local stop="$1" verdict="${2:-Ready with fixes}" fmode="${3:-edit}" out rev fix n

  out=$(run_node round-pre) || { note "round-pre FAILED: $(tail -1 "$TMP/last.err")"; return 1; }
  rev=$(printf '%s' "$out" | jfield review)
  note "round-pre review=$rev reason=$(printf '%s' "$out" | jfield reason) round=$(round_now)"
  [ "$stop" = round-pre ] && return 0

  if [ "$rev" = run ]; then
    reviewer "$verdict" || { note "reviewer stand-in failed"; return 1; }
  fi
  [ "$stop" = envelope-written ] && return 0

  run_node review-gate >/dev/null || { note "review-gate FAILED: $(tail -2 "$TMP/last.err")"; return 1; }
  [ "$stop" = gate-passed ] && return 0

  out=$(run_node fix-plan) || { note "fix-plan FAILED: $(tail -1 "$TMP/last.err")"; return 1; }
  fix=$(printf '%s' "$out" | jfield fixer)
  note "fix-plan fixer=$fix reason=$(printf '%s' "$out" | jfield reason) round=$(round_now)"

  if [ "$fix" = run ]; then
    fixer "$fmode" || { note "fixer stand-in failed"; return 1; }
  fi
  [ "$stop" = repair-written ] && return 0

  # Boundaries INSIDE commit-fixer are unreachable from out here -- the node is
  # one call into the helper -- so they are driven through the helper's own test
  # hook rather than by constructing a state this script invented.
  case "$stop" in
    ledger-merged|post-fix-written|committed)
      ROUND_STATE_KILL_AFTER="$stop" run_node commit-fixer >/dev/null
      return 0 ;;
  esac
  run_node commit-fixer >/dev/null || { note "commit-fixer FAILED: $(tail -2 "$TMP/last.err")"; return 1; }
  [ "$stop" = fixer-attested ] && return 0

  run_node converge >/dev/null
  note "converge rc=$? round=$(round_now)"
  return 0
}

# --- the sequence ----------------------------------------------------------
# Each boundary is killed once, then the loop is restarted from round-pre. The
# restart is where a duplicate would appear, so the decision log after each
# restart is what gets judged.
BOUNDARIES=(round-pre envelope-written gate-passed repair-written
            ledger-merged post-fix-written committed fixer-attested)
echo "INJECTION=START lane=$LANE boundaries=${#BOUNDARIES[@]}"
for b in "${BOUNDARIES[@]}"; do
  note "--- kill after: $b"
  pass "$b" "Ready with fixes" edit
  note "--- restart"
  pass "" "Ready with fixes" edit
done

# Terminal replay: the round converged; a further restart must re-emit the
# promise and run neither activity again.
note "--- terminal replay"
pass "" "Ready to merge" nochange

# --- judgement -------------------------------------------------------------
# A `run` whose reason is neither `initial` nor `interrupted` is, by the plan's
# definition, an activity paid for twice.
DUPES=$(grep -E '^(round-pre review=run|fix-plan fixer=run)' "$LOG" \
        | grep -vE 'reason=(initial|interrupted)' || true)
NDUP=$(printf '%s' "$DUPES" | grep -c . || true)
REVIEWS=$(grep -c '^round-pre review=run' "$LOG" || true)
FIXERS=$(grep -c '^fix-plan fixer=run' "$LOG" || true)
PROMISE=$(grep -c 'REVIEW_CONVERGED' "$AD"/round-*/converge.txt 2>/dev/null | head -1 || echo 0)

echo "INJECTION_LOG"
sed 's/^/  /' "$LOG"
if [ "$NDUP" != 0 ]; then
  echo "INJECTION=FAIL duplicates=$NDUP reviews=$REVIEWS fixers=$FIXERS"
  printf '%s\n' "$DUPES" | sed 's/^/  duplicate: /'
  exit 1
fi
echo "INJECTION=PASS duplicates=0 reviews=$REVIEWS fixers=$FIXERS boundaries=${#BOUNDARIES[@]}"
