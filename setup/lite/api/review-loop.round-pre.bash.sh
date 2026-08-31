set -euo pipefail
C="$ARTIFACTS_DIR/round.txt"
N=$(cat "$C" 2>/dev/null || echo 0)
# Hand-editable counter: reject junk before it is incremented or used as a path.
case "$N" in '') N=0 ;; *[!0-9]*) echo "ROUND_PRE=FAIL round.txt is not an integer: [$N]"; exit 1 ;; esac
# LITE lane: default cap 1 (one review round), matching the converge overlay.
# Durable cap, checked BEFORE the round is spent (same doctrine as plan-round-pre,
# full-sdlc-api.yaml: a cap enforced only in converge is walked past by resuming
# after every group failure). accept-residuals.txt is the human bypass.
CAP=$(cat "$ARTIFACTS_DIR/round-cap.txt" 2>/dev/null || echo 1)
case "$CAP" in ''|*[!0-9]*) CAP=1 ;; esac
if [ "$N" -ge "$CAP" ] && [ ! -f "$ARTIFACTS_DIR/accept-residuals.txt" ]; then echo "ROUND_CAP_REACHED round=$N cap=$CAP (pre-round: a resume does not buy another review; write accept-residuals.txt or raise round-cap.txt)"; exit 1; fi
N=$((N+1)); echo "$N" > "$C"
RD="$ARTIFACTS_DIR/round-$N"; mkdir -p "$RD"
eval "$(bash /Users/eduardopicazo/Documents/Workspace/Goodword/.archon/setup/params-env.sh "$ARTIFACTS_DIR/params.json")"
cd "$WT"
git rev-parse HEAD > "$RD/pre-head.txt"
ls -1d "${CE_REVIEW_ROOT:-/tmp/compound-engineering/ce-code-review}"/*/ 2>/dev/null | LC_ALL=C sort > "$RD/prerun-dirs.txt" || true
touch "$RD/prerun-dirs.txt"
echo "ROUND=$N head=$(cat "$RD/pre-head.txt")"
