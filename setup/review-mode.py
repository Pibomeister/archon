#!/usr/bin/env python3
"""Does round N rediscover the candidate, or verify last round's repair?

v1 asked a cheaper question -- re-read everything, or only what changed -- and
measured the answer: `delta` rounds were not cheaper (10.4 and 19.1 minutes
against a 14.4-minute mean). Reading less of the same diff does not shrink a
review whose cost is the persona fan-out. So `delta` is retired for this lane
and the axis is different: a `verify` round does not rediscover at all. It is
given the ledger and asks one question per entry, which is bounded by the
finding count rather than by the diff.

`verify` is chosen when `round-N-1/decision.json` says so. That file is
`converge`'s decision, written with the evidence in front of it -- whether a P0
was applied, whether a repair expanded the design, whether a new blocker
appeared. Inferring the mode here from `fixer-result.json` alone, as v1 did,
re-derives a worse version of a decision that was already made correctly.
Anything else is `full`: round 1, an unreadable predecessor, no decision file.

The chosen base is validated before the round is spent. A base that is not a
commit, or is not an ancestor of HEAD, produces a diff that is not this
candidate's change -- and v1's fallback to `origin/main` hid exactly that,
silently reviewing a different slice at full price.

Usage: review-mode.py <artifacts-dir> <N> [--repo <worktree>]
Writes round-<N>/review-scope.txt (full|verify), round-<N>/review-base.txt, and
in verify mode round-<N>/review-findings.json. Prints one REVIEW_MODE line and,
in verify mode, the findings count. Exits 1 on REVIEW_BASE=FAIL.
"""
import argparse
import json
import os
import subprocess
import sys

FULL_BASE = "origin/main"
MODES = ("full", "verify")


def read_text(path):
    """That file's stripped contents, or None when it cannot be read."""
    try:
        return open(path, encoding="utf-8").read().strip() or None
    except Exception:
        return None


def next_mode(prev_dir):
    """The mode `converge` persisted for this round, or None."""
    try:
        with open(os.path.join(prev_dir, "decision.json"), encoding="utf-8") as f:
            decision = json.load(f)
    except Exception:
        return None
    mode = decision.get("next_mode")
    return mode if mode in MODES else None


def ledger_for(prev_dir):
    """The previous round's ledger, or None. A verify round with no ledger has
    nothing to verify, so its mode is not honoured."""
    try:
        with open(os.path.join(prev_dir, "ledger.json"), encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    return data if isinstance(data, list) and data else None


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args],
                          capture_output=True, text=True)


def validate_base(repo, base):
    """Item 6. Returns a reason string, or None when the base is usable.

    The expensive direction is safe: refusing a base costs a typed stop an
    operator reads, while accepting an unreachable one costs a full-price review
    of the wrong diff that every downstream gate then treats as this round's.
    """
    if not repo or not os.path.isdir(repo):
        return None
    if git(repo, "cat-file", "-e", f"{base}^{{commit}}").returncode != 0:
        return "missing"
    if git(repo, "merge-base", "--is-ancestor", base, "HEAD").returncode != 0:
        return "not-ancestor"
    return None


def baseline_behind(repo):
    """Upstream commits not in this candidate. Informational: a candidate built
    on a stale base still reviews correctly, but a reviewer told nothing about
    it reads an absent upstream change as the candidate's omission."""
    if not repo or not os.path.isdir(repo):
        return None
    p = git(repo, "rev-list", "--count", "HEAD..origin/main")
    if p.returncode != 0:
        return None
    try:
        return int(p.stdout.strip())
    except ValueError:
        return None


def decide(ad, n):
    """(mode, base, ledger). Base is a sha, or origin/main when nothing readable
    names one."""
    prev = os.path.join(ad, f"round-{n - 1}") if n > 1 else None
    ledger = None
    if prev and next_mode(prev) == "verify":
        ledger = ledger_for(prev)
        base = read_text(os.path.join(prev, "pre-head.txt"))
        if ledger and base:
            # A base equal to this round's own pre-head means nothing was
            # committed since: there is no repair to verify, so rediscover.
            if base != read_text(os.path.join(ad, f"round-{n}", "pre-head.txt")):
                return "verify", base, ledger
    boot = read_text(os.path.join(ad, "bootstrap-head.txt")) if ad else None
    return "full", boot or FULL_BASE, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("artifacts", nargs="?", default="")
    ap.add_argument("round", nargs="?", default="1")
    ap.add_argument("--repo", default=None,
                    help="candidate worktree; without it the base is not "
                         "validated because there is nothing to validate against")
    a = ap.parse_args()
    ad = a.artifacts
    try:
        n = int(a.round)
    except ValueError:
        n = 1

    mode, base, ledger = decide(ad, n)
    # Every base is validated, `origin/main` included: item 6 removed the
    # fallback, and a fallback that is itself unvalidated is the same hole one
    # name further along.
    reason = validate_base(a.repo, base)
    if reason:
        print(f"REVIEW_BASE=FAIL base={base} reason={reason}")
        return 1

    # No artifacts dir means a caller bug, not round-1 of a run in $PWD: print
    # the safe answer rather than minting a round-1/ wherever this was invoked.
    if ad:
        out = os.path.join(ad, f"round-{n}")
        os.makedirs(out, exist_ok=True)
        for name, text in (("review-scope.txt", mode), ("review-base.txt", base)):
            with open(os.path.join(out, name), "w", encoding="utf-8") as f:
                f.write(text + "\n")
        if mode == "verify":
            with open(os.path.join(out, "review-findings.json"), "w",
                      encoding="utf-8") as f:
                json.dump(ledger, f, indent=2, sort_keys=True)

    if mode == "verify":
        print(f"REVIEW_MODE=verify base={base} findings={len(ledger)}")
    else:
        print(f"REVIEW_MODE=full base={base}")
    behind = baseline_behind(a.repo)
    if behind:
        print(f"BASELINE_BEHIND upstream={behind}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
