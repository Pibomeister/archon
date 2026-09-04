#!/usr/bin/env bash
# A review/critic round counter is durable and is incremented BEFORE the work it
# counts. So a node killed by its cost cap, its timeout, or a crash leaves the
# counter advanced with nothing to show for it: the round was billed, not spent.
# With the usual cap of 2, one such death halves a run's review iterations and
# two exhaust them having reviewed nothing -- and the run then demands a human
# accept-residuals.txt for a failure that was never about the code.
#
# Given the counter's current value, echo the value the caller should treat as
# "rounds already spent": one less, when the last round left no proof artifact
# behind. Bounded to ONE reclaim per round number via a ledger, so a node that
# dies every time still walks the counter up to the cap instead of spinning.
#
# usage: round-reclaim.sh <artifacts-dir> <N> <round-dir-prefix> <proof-file>
set -uo pipefail
AD=${1:?artifacts dir}; N=${2:?counter}; PREFIX=${3:?prefix}; PROOF=${4:?proof file}

# Never rewrite a counter we cannot parse; the caller validates it separately.
case "$N" in ''|*[!0-9]*) echo "$N"; exit 0 ;; esac
[ "$N" -gt 0 ] || { echo "$N"; exit 0; }

# Proof present => the round really happened, whatever the gate then decided.
[ -s "$AD/$PREFIX$N/$PROOF" ] && { echo "$N"; exit 0; }

# ONE reclaim per RUN, not per round number. A second unproven round means
# something is durably wrong, not unlucky, and the run should stop at the cap
# rather than keep buying attempts: the worst case this adds is exactly one
# extra review, whatever the cap is.
LEDGER="$AD/round-reclaimed.txt"
if [ -s "$LEDGER" ]; then echo "$N"; exit 0; fi
printf '%s\n' "$PREFIX$N" >> "$LEDGER"
echo "ROUND_RECLAIM=$PREFIX$N proof=$PROOF (no proof artifact: the round was billed, not spent; reclaiming it once)" >&2
echo "$((N-1))"
