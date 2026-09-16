set -uo pipefail
# STDOUT CARRIES ONE BARE JSON LINE AND NOTHING ELSE, exactly as the parent
# does: this overlay replaces the parent body wholesale, but it does NOT replace
# the sibling `review` node, whose `when: $round-pre.review == 'run'` still
# parses this node's output. A human line on stdout here makes that condition
# unparseable, and an unparseable `when:` silently skips the reviewer while the
# run still reports SUCCESS (RUNBOOK 3).
if [ -n "${ARTIFACTS_DIR-}" ]; then exec 2> >(tee -a "$ARTIFACTS_DIR/node-round-pre.out" >&2); fi
# LITE lane: one review round, so there is no reuse decision to make and no
# decision.json to replay -- `pre-lite` always answers `review: run`, defaults
# the durable cap to 1 to match the converge overlay, and skips the parent's
# pending-commit reconciliation. It still writes review-input.json with the same
# constituents as the parent, which is what makes the inherited review-gate's
# GATE_5 identity check apply to this lane too. v1's GATE_4 had a SKIP path here
# because the lite overlay never wrote a review-mode.txt; GATE_5 has no SKIP
# path, because the identity is written on both lanes by the same code.
python3 /Users/eduardopicazo/Documents/Workspace/Goodword/.archon/setup/round-state.py "$ARTIFACTS_DIR" pre-lite
