set -euo pipefail
ROOT="$PROJECT_ROOT"
RID="$(basename "$ARTIFACTS_DIR")"
test -n "${ARCHON_BUGFIX_CHAIN_STATE-}" || { echo "POST_APPROVAL=FAIL no chain state"; exit 1; }
test -n "${ARCHON_ATTESTATION_DIR-}" || { echo "POST_APPROVAL=FAIL no attestation dir"; exit 1; }
python3 $ARCHON_LAYER/setup/bugfix-contract.py validate-current-manifest --artifacts "$ARTIFACTS_DIR" --kind lite-approval
SEAL="$ARCHON_ATTESTATION_DIR/$RID-lite-approval.json"
python3 $ARCHON_LAYER/setup/controller-attest.py lite-approval --verify --artifacts "$ARTIFACTS_DIR" --chain-state "$ARCHON_BUGFIX_CHAIN_STATE" --out "$SEAL"
echo "POST_APPROVAL=PASS lite-manifest-and-attestation-current"
