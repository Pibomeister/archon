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
# The proof must be the round's PRODUCT, not a file with the right name. Run
# 38d72218 round 3 showed why: the review node returned while its personas were
# still running, so review-envelope.txt existed at 22KB, truncated mid-persona,
# and review-summary.json read {"verdict": ""}. Every downstream gate rejects an
# empty verdict, so a round that produced one produced nothing -- but a
# file-existence check called it spent. Hence the optional pattern: the proof
# file must also SAY something.
#
# usage: round-reclaim.sh <artifacts-dir> <N> <round-dir-prefix> <proof-file>
#                         [proof-pattern]
set -uo pipefail
AD=${1:?artifacts dir}; N=${2:?counter}; PREFIX=${3:?prefix}; PROOF=${4:?proof file}
PATTERN=${5-}

# Never rewrite a counter we cannot parse; the caller validates it separately.
case "$N" in ''|*[!0-9]*) echo "$N"; exit 0 ;; esac
[ "$N" -gt 0 ] || { echo "$N"; exit 0; }

# Proof present => the round really happened, whatever the gate then decided.
P="$AD/$PREFIX$N/$PROOF"
if [ -s "$P" ]; then
  if [ -z "$PATTERN" ] || grep -qE "$PATTERN" "$P"; then echo "$N"; exit 0; fi
fi

# ONE reclaim per RUN, not per round number. A second unproven round means
# something is durably wrong, not unlucky, and the run should stop at the cap
# rather than keep buying attempts: the worst case this adds is exactly one
# extra review, whatever the cap is.
LEDGER="$AD/round-reclaimed.txt"
if [ -s "$LEDGER" ]; then echo "$N"; exit 0; fi
printf '%s\n' "$PREFIX$N" >> "$LEDGER"
echo "ROUND_RECLAIM=$PREFIX$N proof=$PROOF (the round produced no verdict: it was billed, not spent; reclaiming it once)" >&2
echo "$((N-1))"
