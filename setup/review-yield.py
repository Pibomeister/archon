#!/usr/bin/env python3
"""Is the review loop still yielding findings, or trading the same P2s?

A round cap stops a loop that churns. It says nothing about a loop that has
run out of things to find: two consecutive rounds each applying at most one P2
fix, with nothing incomplete, is a reviewer polishing rather than reviewing.
This prints that measurement every round so the shape is on the record before
any lane is allowed to converge on it.

`applied`, `maxsev`, `reraised` and `reraised_advisory` are all the CURRENT
round's. `verdict` is the only field computed across the round pair.

`reraised` counts current-round applied entries whose normalized finding key is
already in the waiver ledger -- a finding the fixer declined with rationale and
a later reviewer raised again anyway. It is the metric that says whether the
review prompt's waiver rule is being honoured.

`reraised_advisory` is the same measurement on the findings the fixer waived
AGAIN this round: current-round advisory entries whose key was already waived in
an EARLIER round. The earlier-round restriction is not cosmetic -- the lanes run
update-waivers.py immediately before this script, so this round's own advisory is
in the ledger by the time it is read, and counting it would report every waiver
as a re-raise.

Usage: review-yield.py <artifacts-dir> <round-N>
Prints exactly one line and exits 0 always. Converge captures this in a `$(...)`,
so a traceback here would yield an empty line and silently disable its reader
rather than failing it.
"""
import json
import os
import sys

from finding_key import key_of as _key

RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
NAME = {v: k for k, v in RANK.items()}
CONVERGE_RANK = RANK["P2"]


def _round_facts(path):
    """(count, maxsev_rank, incomplete, keys, advisory_keys), or None when the
    round cannot be read.

    None is not zero. round-reclaim.sh can decrement the durable counter, so a
    round directory may be absent or hold an aborted attempt; reading that as
    "no findings applied" would buy a convergence the run never earned. A result
    without an `applied` key is the same thing: an artifact no round produced.
    """
    try:
        d = json.load(open(path, encoding="utf-8"))
        applied = d["applied"]
        ranks, keys = [], []
        for e in applied:
            sev = e.get("severity") if isinstance(e, dict) else None
            ranks.append(RANK.get(sev, RANK["P0"]))
            keys.append(_key(e.get("finding", "") if isinstance(e, dict) else e))
        # An empty round has nothing above the convergence threshold.
        maxsev = min(ranks) if ranks else CONVERGE_RANK
        advisory = [_key(e.get("finding", "")) for e in (d.get("advisory") or [])
                    if isinstance(e, dict)]
        return len(applied), maxsev, len(d.get("incomplete") or []), keys, advisory
    except Exception:
        return None


def _ledger_keys(path, skip_round=None):
    """Waived finding keys. Absent on any pre-ledger run and on a first round
    with no advisory, so its absence is an empty ledger, never an error.
    `skip_round` drops the entries this round just wrote, so a first-time waiver
    is not read back as a re-raise of itself."""
    try:
        entries = json.load(open(path, encoding="utf-8"))["entries"]
        return {_key(e.get("finding", "")) for e in entries
                if isinstance(e, dict) and str(e.get("round")) != str(skip_round)} - {""}
    except Exception:
        return set()


def _diminishing(facts):
    count, maxsev, incomplete = facts[:3]
    return count <= 1 and incomplete == 0 and maxsev >= CONVERGE_RANK


def main():
    ad = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        n = int(sys.argv[2])
    except (IndexError, ValueError):
        n = 0
    cur = _round_facts(os.path.join(ad, f"round-{n}", "fixer-result.json"))
    prev = _round_facts(os.path.join(ad, f"round-{n - 1}", "fixer-result.json"))
    count, maxsev, _, keys, advisory = cur or (0, RANK["P0"], 1, [], [])
    ledger = os.path.join(ad, "waivers.json")
    waived = _ledger_keys(ledger)
    reraised = sum(1 for k in keys if k in waived)
    waived_before = _ledger_keys(ledger, skip_round=n)
    reraised_advisory = sum(1 for k in advisory if k in waived_before)
    ok = (n >= 2 and cur is not None and prev is not None
          and _diminishing(cur) and _diminishing(prev))
    print(f"REVIEW_YIELD=OK verdict={'DIMINISHING' if ok else 'CONTINUE'} "
          f"applied={count} maxsev={NAME[maxsev]} reraised={reraised} "
          f"reraised_advisory={reraised_advisory}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
