set -euo pipefail
ROOT=/Users/eduardopicazo/Documents/Workspace/Goodword
SETUP="$ROOT/.archon/setup"
eval "$(bash "$SETUP/params-env.sh" "$ARTIFACTS_DIR/params.json")"
TESTF=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1], encoding='utf-8'))['test_file'])" "$ARTIFACTS_DIR/failing-test.json")
SIG=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1], encoding='utf-8'))['predicted_failure_signature'])" "$ARTIFACTS_DIR/failing-test.json")
KIND=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1], encoding='utf-8'))['kind'])" "$ARTIFACTS_DIR/failing-test.json")
# Scope: the RED node may have changed ONLY the test file.
python3 -c "import json,sys;json.dump([sys.argv[2]], open(sys.argv[1],'w'))" "$ARTIFACTS_DIR/red-allowlist.json" "$TESTF"
python3 "$SETUP/check-scope.py" "$ARTIFACTS_DIR/red-allowlist.json" "$WT" "$(cat "$ARTIFACTS_DIR/bootstrap-head.txt")" --exclude pnpm-lock.yaml \
  || { echo "RED_GATE=FAIL scope breach (red node touched more than the test file)"; exit 1; }
test -f "$WT/$TESTF" || { echo "RED_GATE=FAIL test file not created: $TESTF"; exit 1; }
# LITE lane: no deslop pass runs, so the tautology guard is mechanical here.
# A repro test that is skipped, focused, a todo, or asserts a literal cannot
# be the RED->GREEN proof this lane rests on. The signature checks below are
# the real guard; this catches the cheap shapes before spending the runner.
TAUT=$(grep -nE '\.(skip|only|todo)\(|expect\((true|false|1|0)\)\.toBe|toBeTruthy\(\)[[:space:]]*;?[[:space:]]*$|(it|test)\([^)]*\)[[:space:]]*;' "$WT/$TESTF" | head -3 || true)
test -z "$TAUT" || { echo "RED_GATE=FAIL tautology-marker $(echo "$TAUT" | head -1 | cut -c1-120)"; exit 1; }
# LITE lane: the live experiment never runs here, so the parent's premise-evidence
# contract (mocked fixtures must cite experiment.json observations) has nothing to
# check and is omitted deliberately; RUNBOOK §14b says so.
if [ "$KIND" = integration ]; then echo "RED_NOTE=integration mutex: this run owns ports 54322/8001 machine-globally"; fi
RC=0
bash "$SETUP/run-repro.sh" "$WT" "$ARTIFACTS_DIR" "$ARTIFACTS_DIR/red-out.txt" >/dev/null 2>&1 || RC=$?
test "$RC" != 0 || { echo "RED_GATE=FAIL test passed - does not reproduce the bug"; exit 1; }
test "$RC" != 97 || { echo "RED_GATE=FAIL repro harness error (see red-out.txt)"; exit 1; }
grep -Eq 'Cannot find module|Test suite failed to run|SyntaxError' "$ARTIFACTS_DIR/red-out.txt" \
  && { echo "RED_GATE=FAIL error-not-failure (the suite errored instead of the test failing; see red-out.txt)"; exit 1; }
grep -qF "$SIG" "$ARTIFACTS_DIR/red-out.txt" \
  || { echo "RED_GATE=FAIL predicted signature not found (chain wrong -> engineer; over-specific signature -> human edits failing-test.json, the edit is the approval, then resume)"; exit 1; }
echo "RED_GATE=PASS rc=$RC signature matched"
