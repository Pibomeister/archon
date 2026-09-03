set -euo pipefail
ROOT=/Users/eduardopicazo/Documents/Workspace/Goodword
python3 "$ROOT/.archon/setup/bugfix-contract.py" write-lite-approval-manifest --artifacts "$ARTIFACTS_DIR"
RID="$(basename "$ARTIFACTS_DIR")"
test -n "${ARCHON_BUGFIX_CHAIN_STATE-}" || { echo "LITE_ATTESTATION=FAIL no chain state"; exit 1; }
test -n "${ARCHON_ATTESTATION_DIR-}" || { echo "LITE_ATTESTATION=FAIL no attestation dir"; exit 1; }
SEAL="$ARCHON_ATTESTATION_DIR/$RID-lite-approval.json"
python3 "$ROOT/.archon/setup/controller-attest.py" lite-approval --artifacts "$ARTIFACTS_DIR" --chain-state "$ARCHON_BUGFIX_CHAIN_STATE" --out "$SEAL"
python3 "$ROOT/.archon/setup/controller-attest.py" lite-approval --verify --artifacts "$ARTIFACTS_DIR" --chain-state "$ARCHON_BUGFIX_CHAIN_STATE" --out "$SEAL"
H="$ARTIFACTS_DIR/rca-review.html"
test -s "$H" || { echo "RENDER_GATE=FAIL no rca-review.html"; exit 1; }
for m in GIST SYMPTOM EVIDENCE CHAIN EXPERIMENT RESIDUALS CRITIC FIX TEST DECIDE; do
  grep -q "<!-- $m -->" "$H" || { echo "RENDER_GATE=FAIL missing section $m"; exit 1; }
done
RID="$(basename "$ARTIFACTS_DIR")"
grep -qF "$RID" "$H" || { echo "RENDER_GATE=FAIL DECIDE box does not carry this run's id verbatim"; exit 1; }
for v in approve reject abandon; do
  grep -qF "archon workflow $v" "$H" || { echo "RENDER_GATE=FAIL DECIDE box missing the $v command"; exit 1; }
done
cmp -s "$ARTIFACTS_DIR/rca.md" "$ARTIFACTS_DIR/rca.pre.md" || { echo "RENDER_GATE=FAIL renderer modified rca.md"; exit 1; }
cmp -s "$ARTIFACTS_DIR/causal-chain.json" "$ARTIFACTS_DIR/causal-chain.pre.json" || { echo "RENDER_GATE=FAIL renderer modified causal-chain.json"; exit 1; }
# xdg-open first: /usr/bin/open on some Linux distros is openvt(1), a
# different program. The file:// path is printed unconditionally.
OPENER="$(command -v xdg-open 2>/dev/null || command -v open 2>/dev/null || true)"
OPENED=no
if [ -n "$OPENER" ] && "$OPENER" "$H" >/dev/null 2>&1; then OPENED=yes; fi
if [ "$OPENED" = yes ]; then
  echo "RENDER_GATE=PASS packet=file://$H (opened in browser)"
else
  echo "RENDER_GATE=PASS packet=file://$H (no browser opener - open that path yourself)"
fi
