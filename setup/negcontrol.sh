#!/usr/bin/env bash
# The causality prover ("if you didn't fix it, it ain't fixed"): revert every
# commit after the RED commit, re-run the repro, and demand it fails again
# WITH the predicted signature — proving the fix, not a flake or an env
# change, made the test pass. Mechanism is commit-then-revert-then-reset,
# NEVER stash: stash captures untracked droppings (.env and friends) and
# makes crash recovery ambiguous, while `git reset --hard <recorded sha>` is
# unambiguous. The revert range "$RED"..HEAD reverses only post-test commits,
# so the repro test itself survives the revert by construction. A trap
# restores HEAD on every exit path. Every failure line names its recovery.
# Usage: negcontrol.sh <worktree> <red-sha> <artifacts-dir> <tag>
set -euo pipefail
WT="${1:?usage: negcontrol.sh <worktree> <red-sha> <artifacts-dir> <tag>}"
RED="${2:?usage: negcontrol.sh <worktree> <red-sha> <artifacts-dir> <tag>}"
AD="${3:?usage: negcontrol.sh <worktree> <red-sha> <artifacts-dir> <tag>}"
TAG="${4:?usage: negcontrol.sh <worktree> <red-sha> <artifacts-dir> <tag>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$AD/negcontrol-$TAG.txt"
FT="$AD/failing-test.json"
SIG=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1], encoding='utf-8'))['predicted_failure_signature'])" "$FT")
TESTF=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1], encoding='utf-8'))['test_file'])" "$FT")
cd "$WT"
test -z "$(git status --porcelain | grep -vE '^\?\? \.env$|^ M pnpm-lock\.yaml$')" || { echo "NEGCONTROL=FAIL dirty tree (commit or reset the worktree, then resume)"; exit 1; }
test -z "$(git diff "$RED" -- "$TESTF")" || { echo "NEGCONTROL=FAIL repro test drifted since RED (restore it: git -C $WT checkout $RED -- $TESTF, commit, then resume)"; exit 1; }
PRE=$(git rev-parse HEAD)
test "$PRE" != "$RED" || { echo "NEGCONTROL=FAIL no fix commits after RED (nothing to revert; the fix loop has not landed a fix)"; exit 1; }
# Restore on EVERY exit path — success included (idempotent after the manual reset below).
trap 'git reset --hard "$PRE" >/dev/null 2>&1 || true' EXIT
git revert --no-commit "$RED"..HEAD >/dev/null 2>&1 || { echo "NEGCONTROL=FAIL revert conflict (tree restored to $PRE by trap; the fix does not cleanly reverse — engineer review)"; exit 1; }
RC=0
bash "$HERE/run-repro.sh" "$WT" "$AD" "$OUT" >/dev/null 2>&1 || RC=$?
git reset --hard "$PRE" >/dev/null
if [ "$RC" = 97 ]; then echo "NEGCONTROL=FAIL repro harness error (see $OUT; tree restored to $PRE)"; exit 1; fi
if [ "$RC" = 0 ]; then echo "NEGCONTROL=FAIL fix not causal (repro passed without the fix) — the GREEN was a flake or an env change; re-open the fix loop"; exit 1; fi
grep -qF "$SIG" "$OUT" || { echo "NEGCONTROL=FAIL refailed without predicted signature (see $OUT) — the failure mode changed under revert; engineer review"; exit 1; }
test "$(git rev-parse HEAD)" = "$PRE" || { echo "NEGCONTROL=FAIL HEAD drifted (recover: git -C $WT reset --hard $PRE)"; exit 1; }
test -z "$(git status --porcelain | grep -vE '^\?\? \.env$|^ M pnpm-lock\.yaml$')" || { echo "NEGCONTROL=FAIL tree left dirty (recover: git -C $WT reset --hard $PRE)"; exit 1; }
echo "NEGCONTROL=PASS refail signature matched (tag=$TAG)" | tee -a "$OUT"
