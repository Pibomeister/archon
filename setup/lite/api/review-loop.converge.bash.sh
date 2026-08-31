set -uo pipefail
N=$(cat "$ARTIFACTS_DIR/round.txt"); RD="$ARTIFACTS_DIR/round-$N"
exec > >(tee "$RD/converge.txt") 2>&1
eval "$(bash /Users/eduardopicazo/Documents/Workspace/Goodword/.archon/setup/params-env.sh "$ARTIFACTS_DIR/params.json")"
cd "$WT"
V=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['verdict'])" "$RD/review-summary.json" 2>/dev/null || echo UNKNOWN)
FIXOK=NO
python3 /Users/eduardopicazo/Documents/Workspace/Goodword/.archon/setup/check-fixer-result.py "$RD/fixer-result.json" >/dev/null 2>&1 && FIXOK=YES
CLEAN=NO; test -z "$(git status --porcelain | grep -v '^?? \.env')" && CLEAN=YES
MOVED=YES; test "$(git rev-parse HEAD)" = "$(cat "$RD/pre-head.txt")" && MOVED=NO
INC=$(python3 -c "import json,sys;print(len(json.load(open(sys.argv[1])).get('incomplete',[])))" "$RD/fixer-result.json" 2>/dev/null || echo 0)
echo "ROUND=$N verdict=[$V] fixer_ok=$FIXOK clean=$CLEAN head_moved=$MOVED incomplete=$INC"
# Scope guard: any change outside the plan's allowlist is a human stop.
# Legitimate scope growth = a human edits files-allowlist.json and resumes.
python3 /Users/eduardopicazo/Documents/Workspace/Goodword/.archon/setup/check-scope.py \
  "$ARTIFACTS_DIR/files-allowlist.json" "$WT" \
  "$(cat "$ARTIFACTS_DIR/bootstrap-head.txt")" --round "$N" || exit 1
if [ "$FIXOK" = NO ]; then echo "FIXER_BLOCKED round=$N"; exit 1; fi
# Waiver ledger: record this round's advisory declines so later rounds
# do not re-litigate them without new evidence.
python3 /Users/eduardopicazo/Documents/Workspace/Goodword/.archon/setup/update-waivers.py \
  "$RD/fixer-result.json" "$ARTIFACTS_DIR/waivers.md"
# Durable round cap: counted against round.txt (survives resumes), not
# loop iterations. accept-residuals.txt is a HUMAN act, never an agent's.
CAP=$(cat "$ARTIFACTS_DIR/round-cap.txt" 2>/dev/null || echo 1)
ACCEPT=NO; test -f "$ARTIFACTS_DIR/accept-residuals.txt" && ACCEPT=YES
# LITE convergence contract (one review round, user-accepted tradeoff):
# a Ready verdict converges even when the fixer landed changes. Those hunks
# are NOT re-read by a reviewer; post-fix-gate re-runs typecheck + lint and
# exit-gate re-runs the plan's tests + scope check, and the PR body lists
# them under "Reviewer-unverified fixes" from lite-fixes-unreviewed.txt.
# "Not ready" keeps the parent's semantics: with the cap at 1 it is
# ROUND_CAP_REACHED unless a human wrote accept-residuals.txt.
case "$V" in
  "Ready to merge"|"Ready with fixes")
    if [ "$CLEAN" != YES ]; then echo "CONVERGE=FAIL dirty tree after fixer round=$N"; exit 1; fi
    if [ "$INC" != 0 ] && [ "$ACCEPT" != YES ]; then
      echo "ROUND_CAP_REACHED round=$N (fixer left $INC incomplete item(s); lite lane has no second round — write accept-residuals.txt or relaunch on full-sdlc-api)"; exit 1
    fi
    if [ "$MOVED" = YES ]; then
      {
        echo "fixer_commit=$(git rev-parse HEAD)"
        echo "pre_round_head=$(cat "$RD/pre-head.txt")"
        echo "applied_findings=$(python3 -c "import json,sys;print(len(json.load(open(sys.argv[1])).get('applied',[])))" "$RD/fixer-result.json" 2>/dev/null || echo unknown)"
        echo "files:"
        git diff --name-only "$(cat "$RD/pre-head.txt")" HEAD | sed 's/^/  /'
      } > "$ARTIFACTS_DIR/lite-fixes-unreviewed.txt"
      echo "LITE_FIXES_UNREVIEWED round=$N $(git diff --name-only "$(cat "$RD/pre-head.txt")" HEAD | wc -l | tr -d ' ') file(s) landed by the fixer and not re-reviewed (see lite-fixes-unreviewed.txt)"
    fi
    echo "CONVERGED round=$N (lite: single review round)"
    echo "<promise>REVIEW_CONVERGED</promise>"
    ;;
  "Not ready")
    if [ "$MOVED" = NO ]; then echo "NO_PROGRESS round=$N (Not ready and nothing changed)"; exit 1; fi
    if [ "$N" -ge "$CAP" ]; then
      if [ "$ACCEPT" = YES ]; then
        echo "CONVERGED round=$N (human accepted residuals)"
        echo "<promise>REVIEW_CONVERGED</promise>"
      else
        echo "ROUND_CAP_REACHED round=$N"; exit 1
      fi
    else
      echo "ROUND_PROGRESSED round=$N (Not ready; fixes landed; re-review next round)"
    fi
    ;;
  *) echo "CONVERGE=FAIL unknown verdict [$V]"; exit 1 ;;
esac
