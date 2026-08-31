set -euo pipefail
# Shape, contract and premise-citation checks live in setup/plan-shape.sh so
# plan-converge can re-run exactly the same checks on an ACCEPT round. A
# second inline copy of the cited() helper would be two contracts drifting
# apart, and the drift would show up as a plan that passes here and fails
# after the critic loop, or worse, the reverse.
eval "$(bash /Users/eduardopicazo/Documents/Workspace/Goodword/.archon/setup/params-env.sh "$ARTIFACTS_DIR/params.json")"
SHAPE=0
OUT=$(bash /Users/eduardopicazo/Documents/Workspace/Goodword/.archon/setup/plan-shape.sh \
  "$ARTIFACTS_DIR" "$WT" "$SPEC" 2>&1) || SHAPE=$?
if [ "$SHAPE" != 0 ]; then
  # This node keeps its own typed token: SNAPSHOT=FAIL <reason> is what the
  # runbook and every existing grep over run logs look for.
  printf '%s\n' "$OUT" | sed 's/^PLAN_SHAPE=FAIL/SNAPSHOT=FAIL/'
  exit 1
fi
cp "$ARTIFACTS_DIR/plan.md" "$ARTIFACTS_DIR/plan.pre.md"
echo "SNAPSHOT=OK"
# LITE lane: no doc-review stage, so the render gate compares the renderer's
# input against this snapshot instead of plan.post-docreview.md.
cp "$ARTIFACTS_DIR/plan.md" "$ARTIFACTS_DIR/plan.post-snapshot.md"
