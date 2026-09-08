#!/usr/bin/env python3
"""Is a critic loop converging, or churning?

A round cap exists to stop churn: a critic and a reviser trading the same
objection forever. It is a blunt instrument, and on a loop that is actually
closing findings it cuts the work off one round short. Two full RCA runs on one
ticket ended at `RCA_PLAN_ROUND_CAP round=3 cap=3` with the critic's blocking
count falling every round -- 6 findings (2 blocking), then 5 (2), then 2 (1).
The third round was one revision from ACCEPT and the cap spent the run instead.

Converging means the count of BLOCKING findings -- the same P0/P1-at-confidence
>=75 measure rca-converge already gates ACCEPT on -- strictly decreased from the
previous round to this one. Fewer total findings is not enough: a round that
trades two P1s for one P1 and drops three P3s has not moved the thing that
blocks.

Usage: critic-converging.py <artifacts-dir> <round-N> <round-dir-prefix>
Prints `yes` or `no`. Never raises: an unreadable critique is `no`, because a
loop whose own output cannot be parsed has not demonstrated anything.
"""
import json
import os
import sys

BLOCKING = {"P0", "P1"}
MIN_CONFIDENCE = 75


def blocking(path):
    """None when the round's verdict cannot be read at all."""
    try:
        findings = json.load(open(path, encoding="utf-8")).get("findings") or []
    except Exception:
        return None
    n = 0
    for f in findings:
        if not isinstance(f, dict) or f.get("severity") not in BLOCKING:
            continue
        try:
            if int(str(f.get("confidence"))) >= MIN_CONFIDENCE:
                n += 1
        except (TypeError, ValueError):
            n += 1  # an uncoercible confidence counts as blocking, never as progress
    return n


def main():
    ad, n, prefix = sys.argv[1], sys.argv[2], sys.argv[3]
    try:
        n = int(n)
    except ValueError:
        print("no")
        return 0
    if n < 2:
        print("no")  # nothing to compare against
        return 0
    now = blocking(os.path.join(ad, f"{prefix}{n}", "critique.json"))
    prev = blocking(os.path.join(ad, f"{prefix}{n - 1}", "critique.json"))
    if now is None or prev is None:
        print("no")
        return 0
    # Zero blocking findings is ACCEPT's job, not this one's: converge handles
    # that path and must not be pre-empted here.
    print("yes" if 0 < now < prev else "no")
    return 0


if __name__ == "__main__":
    sys.exit(main())
