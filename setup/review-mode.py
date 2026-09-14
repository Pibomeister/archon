#!/usr/bin/env python3
"""Does round N re-read the whole diff, or only what round N-1 changed?

A reviewer re-reading the full candidate every round pays the full candidate's
cost every round, and rounds 3+ are mostly re-deriving findings rounds 1-2
already settled. When the previous round only applied P2/P3 fixes, the only new
material in the tree is that round's own edits, so review them against the
previous round's pre-fix HEAD and leave the rest alone.

Anything that is not demonstrably cosmetic falls back to the full diff: round 1
has no predecessor, a P0/P1 fix can change behaviour the rest of the diff
depends on, and a previous round that cannot be read is not evidence of
anything. An applied entry with no `severity` is the pre-2026-09-13 fixer
contract and reads as escalating here, the same worst case review-yield.py
assumes for it.

Usage: review-mode.py <artifacts-dir> <N>
Writes round-<N>/review-mode.txt (full|delta) and round-<N>/review-base.txt,
prints exactly one line, exits 0.
"""
import json
import os
import sys

# Only these two are cosmetic. Anything else -- P0, P1, a severity this script
# has never heard of, or no severity at all -- reads as escalating.
COSMETIC = {"P2", "P3"}
FULL_BASE = "origin/main"


def delta_base(prev_dir):
    """Previous round's pre-fix HEAD, or None when round N cannot go delta."""
    try:
        applied = json.load(open(os.path.join(prev_dir, "fixer-result.json"),
                                 encoding="utf-8"))["applied"]
        for e in applied:
            if not isinstance(e, dict) or e.get("severity") not in COSMETIC:
                return None
        head = open(os.path.join(prev_dir, "pre-head.txt"), encoding="utf-8").read()
        return head.strip() or None
    except Exception:
        return None


def main():
    ad = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        n = int(sys.argv[2])
    except (IndexError, ValueError):
        n = 1
    base = delta_base(os.path.join(ad, f"round-{n - 1}")) if n > 1 else None
    mode, base = ("delta", base) if base else ("full", FULL_BASE)
    out = os.path.join(ad, f"round-{n}")
    os.makedirs(out, exist_ok=True)
    for name, text in (("review-mode.txt", mode), ("review-base.txt", base)):
        with open(os.path.join(out, name), "w", encoding="utf-8") as f:
            f.write(text + "\n")
    print(f"REVIEW_MODE={mode} base={base}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
