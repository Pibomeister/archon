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

# Two bounds, because a reclaim buys a real review and reviews cost money.
#
#   per round number -- a node that dies at the same round every time walks the
#   counter up to the cap instead of spinning there forever;
#   per run          -- a budget, default 2, overridable via
#                       round-reclaim-cap.txt.
#
# The budget was 1, and run 38d72218 showed why that is too tight: round 2 died
# to a cost cap and round 3 returned mid-fan-out. Two DISTINCT infrastructure
# failures, both real, both since fixed -- and the run had to be hand-carried
# past the second one. A budget of 2 absorbs unrelated infrastructure flakes
# without ever absorbing a systematic one, since a third means the failure is
# the norm rather than the exception.
LEDGER="$AD/round-reclaimed.txt"
grep -qxF "$PREFIX$N" "$LEDGER" 2>/dev/null && { echo "$N"; exit 0; }
BUDGET=$(cat "$AD/round-reclaim-cap.txt" 2>/dev/null || echo 2)
case "$BUDGET" in ''|*[!0-9]*) BUDGET=2 ;; esac
USED=$( [ -s "$LEDGER" ] && wc -l < "$LEDGER" | tr -d ' ' || echo 0 )
if [ "$USED" -ge "$BUDGET" ]; then
  echo "ROUND_RECLAIM=EXHAUSTED used=$USED budget=$BUDGET (this run has already been given $USED rounds back; a further unproven round is the norm, not a flake -- look at why the review keeps dying before raising round-reclaim-cap.txt)" >&2
  echo "$N"; exit 0
fi
printf '%s\n' "$PREFIX$N" >> "$LEDGER"
echo "ROUND_RECLAIM=$PREFIX$N proof=$PROOF (the round produced no verdict: it was billed, not spent; reclaiming it once)" >&2
echo "$((N-1))"
