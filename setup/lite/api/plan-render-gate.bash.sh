set -euo pipefail
H="$ARTIFACTS_DIR/plan-review.html"
test -s "$H" || { echo "RENDER_GATE=FAIL no plan-review.html"; exit 1; }
for m in GIST KB MAP PLAN REVIEW CRITIC DECIDE; do
  grep -q "<!-- $m -->" "$H" || { echo "RENDER_GATE=FAIL missing section $m"; exit 1; }
done
RID="$(basename "$ARTIFACTS_DIR")"
grep -qF "$RID" "$H" || { echo "RENDER_GATE=FAIL DECIDE box does not carry this run's id verbatim"; exit 1; }
for v in approve reject abandon; do
  grep -qF "archon workflow $v" "$H" || { echo "RENDER_GATE=FAIL DECIDE box missing the $v command"; exit 1; }
done
cmp -s "$ARTIFACTS_DIR/plan.md" "$ARTIFACTS_DIR/plan.post-snapshot.md" || { echo "RENDER_GATE=FAIL renderer modified plan.md"; exit 1; }
# show-me: put the packet in front of the human exactly when the gate pauses.
# xdg-open first: on some Linux distros /usr/bin/open is util-linux's link to
# openvt(1) — a different program, not a missing one. The file:// path is
# printed unconditionally; "opened in browser" is claimed only when true.
OPENER="$(command -v xdg-open 2>/dev/null || command -v open 2>/dev/null || true)"
OPENED=no
if [ -n "$OPENER" ] && "$OPENER" "$H" >/dev/null 2>&1; then OPENED=yes; fi
if [ "$OPENED" = yes ]; then
  echo "RENDER_GATE=PASS packet=file://$H (opened in browser)"
else
  echo "RENDER_GATE=PASS packet=file://$H (no browser opener — open that path yourself)"
fi
