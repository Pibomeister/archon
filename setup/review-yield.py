#!/usr/bin/env python3
"""Is the review loop still yielding findings, or trading the same P2s?

A round cap stops a loop that churns. It says nothing about a loop that has
run out of things to find: two consecutive rounds each applying at most one P2
fix, with nothing incomplete, is a reviewer polishing rather than reviewing.
This prints that measurement every round so the shape is on the record before
any lane is allowed to converge on it.

`applied`, `maxsev` and `reraised` are all the CURRENT round's. `verdict` is
the only field computed across the round pair.

`reraised` counts current-round applied entries whose normalized finding key is
already in the waiver ledger -- a finding the fixer declined with rationale and
a later reviewer raised again anyway. It is the metric that says whether the
review prompt's waiver rule is being honoured.

Usage: review-yield.py <artifacts-dir> <round-N>
Prints exactly one line and exits 0 always. Converge captures this in a `$(...)`,
so a traceback here would yield an empty line and silently disable its reader
rather than failing it.
"""
import json
import os
import re
import sys

RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
NAME = {v: k for k, v in RANK.items()}
CONVERGE_RANK = RANK["P2"]
KEY_LEN = 80


def _key(text):
    """Normalized finding key: casefold, strip everything outside [a-z0-9 ],
    collapse whitespace, truncate. Must stay identical to update-waivers.py's."""
    s = re.sub(r"[^a-z0-9\s]", "", str(text).casefold())
    return re.sub(r"\s+", " ", s).strip()[:KEY_LEN]


def _round_facts(path):
    """(count, maxsev_rank, incomplete, keys), or None when the round cannot be read.

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
        return len(applied), maxsev, len(d.get("incomplete") or []), keys
    except Exception:
        return None


def _ledger_keys(path):
    """Waived finding keys. Absent on any pre-ledger run and on a first round
    with no advisory, so its absence is an empty ledger, never an error."""
    try:
        entries = json.load(open(path, encoding="utf-8"))["entries"]
        return {_key(e.get("finding", "")) for e in entries if isinstance(e, dict)} - {""}
    except Exception:
        return set()


def _diminishing(facts):
    count, maxsev, incomplete, _ = facts
    return count <= 1 and incomplete == 0 and maxsev >= CONVERGE_RANK


def main():
    ad = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        n = int(sys.argv[2])
    except (IndexError, ValueError):
        n = 0
    cur = _round_facts(os.path.join(ad, f"round-{n}", "fixer-result.json"))
    prev = _round_facts(os.path.join(ad, f"round-{n - 1}", "fixer-result.json"))
    count, maxsev, _, keys = cur or (0, RANK["P0"], 1, [])
    waived = _ledger_keys(os.path.join(ad, "waivers.json"))
    reraised = sum(1 for k in keys if k in waived)
    ok = (n >= 2 and cur is not None and prev is not None
          and _diminishing(cur) and _diminishing(prev))
    print(f"REVIEW_YIELD=OK verdict={'DIMINISHING' if ok else 'CONTINUE'} "
          f"applied={count} maxsev={NAME[maxsev]} reraised={reraised}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
