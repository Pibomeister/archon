set -uo pipefail
# STDOUT CARRIES ONE BARE JSON LINE AND NOTHING ELSE, exactly as the parent
# does: this overlay replaces the parent body wholesale, but it does NOT replace
# the sibling `review` node, whose `when: $round-pre.review == 'run'` still
# parses this node's output. A human line on stdout here makes that condition
# unparseable, and an unparseable `when:` silently skips the reviewer while the
# run still reports SUCCESS (RUNBOOK 3).
if [ -n "${ARTIFACTS_DIR-}" ]; then exec 2> >(tee -a "$ARTIFACTS_DIR/node-round-pre.out" >&2); fi
# The ONLY thing this lane does differently is the cap: one review round, to
# match the converge overlay. Seed it rather than branch on it, so the decision
# procedure below is byte-for-byte the parent's -- a second implementation of
# reuse, identity and base validation is exactly the drift the lite overlays
# keep producing, and it is why review-input.json has to be written by the same
# code on both lanes for the inherited review-gate's GATE_5 to mean anything.
# Seeded only when absent: an operator who raised the cap and resumed keeps it.
test -f "$ARTIFACTS_DIR/round-cap.txt" || echo 1 > "$ARTIFACTS_DIR/round-cap.txt"
# Reuse is not a lite/full distinction. A lite resume that re-reviews a candidate
# whose envelope already matches is the same wasted invocation it is on the full
# lane, and the lite lane inherits the gate, fix-plan and commit-fixer that make
# the completion records meaningful. What lite does not inherit is converge, so
# no decision.json is ever written here and the terminal-replay branch is dead
# code on this lane rather than a behaviour difference.
python3 /Users/eduardopicazo/Documents/Workspace/Goodword/.archon/setup/round-state.py pre "$ARTIFACTS_DIR"
