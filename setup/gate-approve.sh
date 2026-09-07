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
# The artifacts root moved when the root became a repo project (RUNBOOK 5a);
# one helper resolves it from the run's own output_root so this cannot rot again.
AD="$(bash "$HERE/run-artifacts.sh" "$RUN_ID")" \
  || { echo "GATE_APPROVE=FAIL no artifacts dir for $RUN_ID"; exit 1; }
CHAIN_ENV=()
while IFS= read -r line; do
  [ -n "$line" ] && CHAIN_ENV+=("$line")
done < <(python3 "$HERE/chain-env.py" "$AD" \
           --control-dir "${ARCHON_CONTROL_DIR:-$HOME/.archon/control/codex-lite}" 2>/dev/null || true)
echo "GATE_APPROVE=ENV vars=${#CHAIN_ENV[@]} run=$(basename "$AD" | cut -c1-8)"
env "${CHAIN_ENV[@]}" DISABLE_OMC=1 archon workflow approve "$RUN_ID" "$@" </dev/null
