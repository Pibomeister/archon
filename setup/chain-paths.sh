#!/usr/bin/env bash
# Locate a bugfix run's chain state and attestation directory.
#
# Nodes that verify attestations need ARCHON_BUGFIX_CHAIN_STATE and
# ARCHON_ATTESTATION_DIR. archon-run.py sets both when IT runs the workflow, so
# they are present on the launch pass and ABSENT on any pass driven by the CLI --
# which is what the gate packet's own DECIDE box tells the operator to use.
# `archon workflow approve <run>` therefore recorded the approval and then died
# at post-approval-integrity with "POST_APPROVAL=FAIL no chain state", a message
# about a file that exists and is perfectly readable.
#
# Neither variable is a credential or a permission. They are paths, derivable
# from the run's own bugfix-chain.json plus the control dir, and the actual guard
# is controller-attest.py --verify, which checks the seal's MAC against the chain
# secret. Locating the file differently cannot weaken that: a wrong file fails
# verification.
#
# usage: eval "$(bash chain-paths.sh <artifacts-dir>)"
# Prints export lines. Existing values always win, so a launcher-set env is never
# second-guessed. Exits 1 with a typed line if the chain id cannot be read.
set -uo pipefail
AD=${1:?artifacts dir}

if [ -n "${ARCHON_BUGFIX_CHAIN_STATE-}" ] && [ -n "${ARCHON_ATTESTATION_DIR-}" ]; then
  exit 0
fi

CONTROL_DIR=${ARCHON_CONTROL_DIR:-$HOME/.archon/control/codex-lite}
CHAIN_ID=$(python3 - "$AD/bugfix-chain.json" <<'PY' 2>/dev/null
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["logical_chain_id"])
PY
)
if [ -z "$CHAIN_ID" ]; then
  echo "CHAIN_PATHS=FAIL cannot read logical_chain_id from $AD/bugfix-chain.json" >&2
  exit 1
fi

STATE="$CONTROL_DIR/bugfix-chains/$CHAIN_ID.json"
if [ ! -f "$STATE" ]; then
  echo "CHAIN_PATHS=FAIL no chain state at $STATE (set ARCHON_CONTROL_DIR if the run used a different control dir)" >&2
  exit 1
fi

[ -n "${ARCHON_BUGFIX_CHAIN_STATE-}" ] || printf 'export ARCHON_BUGFIX_CHAIN_STATE=%q\n' "$STATE"
[ -n "${ARCHON_ATTESTATION_DIR-}" ] || printf 'export ARCHON_ATTESTATION_DIR=%q\n' "$CONTROL_DIR/attestations"
