#!/usr/bin/env bash
# gate-approve.sh -- `archon workflow approve` with the launcher's chain env.
#
# post-approval-integrity runs controller-attest.py and needs
# ARCHON_BUGFIX_CHAIN_STATE / ARCHON_ATTESTATION_DIR, which archon-run.py
# exports at LAUNCH. A bare `archon workflow approve` has neither, so releasing
# a bugfix gate the documented way fails one node later with
# "POST_APPROVAL=... no private chain state" -- for a human operator exactly as
# for an agent. Same reconstruction resume.sh uses, from chain-env.py.
#
# Releasing the gate remains a HUMAN decision; this only fixes the plumbing.
# Usage: gate-approve.sh <run-id> [archon args...]
set -euo pipefail
RUN_ID="${1:?usage: gate-approve.sh <run-id> [archon args...]}"; shift || true
HERE="$(cd "$(dirname "$0")" && pwd)"
ARTIFACTS_ROOT="${ARCHON_ARTIFACTS_ROOT:-$HOME/.archon/workspaces/_folder/goodword/artifacts/runs}"
AD=""
for d in "$ARTIFACTS_ROOT/$RUN_ID"*; do [ -d "$d" ] && AD="$d" && break; done
[ -n "$AD" ] || { echo "GATE_APPROVE=FAIL no artifacts dir for $RUN_ID"; exit 1; }
CHAIN_ENV=()
while IFS= read -r line; do
  [ -n "$line" ] && CHAIN_ENV+=("$line")
done < <(python3 "$HERE/chain-env.py" "$AD" \
           --control-dir "${ARCHON_CONTROL_DIR:-$HOME/.archon/control/codex-lite}" 2>/dev/null || true)
echo "GATE_APPROVE=ENV vars=${#CHAIN_ENV[@]} run=$(basename "$AD" | cut -c1-8)"
env "${CHAIN_ENV[@]}" DISABLE_OMC=1 archon workflow approve "$RUN_ID" "$@" </dev/null
