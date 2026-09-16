#!/usr/bin/env python3
"""The round ledger: what has ever been found on this candidate, and where it is now.

v1 converged on a verdict. A verdict is a claim about the moment it was written,
so a round could close by asserting "Ready with fixes" while a P1 found two
rounds earlier had been applied, never verified, and forgotten -- and nothing in
the lane could tell the difference between "fixed" and "last seen being fixed".

The ledger replaces the verdict with a finite set. Every finding that has ever
entered this candidate has an entry and a state. `applied` is what the fixer
says it did; `closed` is what a verification round saw at a head. Convergence is
closure of the set, not agreement of an adjective.

Two properties are load-bearing and both exist because a resume re-runs a node
whose work already landed:

1. **Idempotent.** Merging the same envelope or the same fixer result twice
   leaves the ledger identical. The merge is a pure upsert keyed by finding id,
   so this holds by construction rather than by bookkeeping.
2. **No downgrade of a verified closure.** A `closed` entry whose evidence head
   is at or after the repair being merged stays `closed`. Without this, the
   commit-fixer re-run after a kill reads its own older `fixer-result.json` and
   moves a verified-closed P1 back to `applied` -- which the positive closure
   requirement then reads as unconverged, forever.

Entries are `{id, severity, state, title, evidence, head, round, attempt}`.
Historical entries are never dropped; `copy-forward` carries them into the next
round so round N's ledger is the whole history, not the round's own findings.

`review-summary.json` is still written on every envelope merge, with exactly the
shape `write-review-summary.py` writes, because `exit-gate` and
`round-reclaim.sh` both read it and neither knows about this file.

Usage:
  ledger.py <artifacts> merge-envelope <round> <envelope> [--repo <path>]
  ledger.py <artifacts> merge-fixer <round> <fixer-result.json> <attempt> [--repo <path>]
  ledger.py <artifacts> copy-forward <from-round> <to-round>
  ledger.py <artifacts> closure <round>
  ledger.py <artifacts> residuals <round> <out.json>
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from finding_key import key_of  # noqa: E402

STATES = ("open", "applied", "closed", "regressed", "deferred", "pinned",
          "waived", "filed", "incomplete", "pin_conflict")
BLOCKING = ("P0", "P1")
VERDICTS = ("Ready to merge", "Ready with fixes", "Not ready")
# The fixer's partitions, and the ledger state each one means. `advisory` is the
# fixer's word for "reported, not applied"; `deferred` is the ledger's.
PARTITION_STATE = {
    "applied": "applied", "advisory": "deferred", "deferred": "deferred",
    "failed": "open", "incomplete": "incomplete", "pinned": "pinned",
    "waived": "waived", "cross_repo": "filed", "filed": "filed",
    "pin_conflict": "pin_conflict",
}


def finding_id(file, title):
    """Contract section 4. Recomputed here rather than trusted from the envelope,
    so a reviewer that omitted or mis-hashed the field still keys correctly and
    a restated finding still collides with its original entry."""
    return hashlib.sha1(
        (key_of(file or "") + "|" + key_of(title or "")).encode("utf-8")
    ).hexdigest()[:12]


def round_dir(ad, n):
    return os.path.join(ad, f"round-{n}")


def load(ad, n):
    try:
        with open(os.path.join(round_dir(ad, n), "ledger.json"), encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    return data if isinstance(data, list) else []


def save(ad, n, entries):
    d = round_dir(ad, n)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "ledger.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def at_or_after(closed_head, repair_head, closed_round, repair_round, repo):
    """Is the closure evidence at or after the repair being merged?

    Wrongly answering yes converges on an unfixed blocker; wrongly answering no
    only costs another verification round. So git ancestry decides it when a
    repo is available, and without one the answer is yes ONLY when the rounds
    say so unambiguously -- never on a guess."""
    if closed_head and closed_head == repair_head:
        return True
    if repo and closed_head and repair_head:
        p = subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor",
                            repair_head, closed_head],
                           capture_output=True, text=True)
        if p.returncode in (0, 1):
            return p.returncode == 0
    return int(closed_round or 0) > int(repair_round or 0)


def upsert(entries, incoming, repo):
    """Merge one incoming entry. Pure upsert keyed by id, so merging twice is a
    no-op; the state guard is the only thing that can reject an update."""
    by_id = {e["id"]: e for e in entries}
    cur = by_id.get(incoming["id"])
    if cur is None:
        entries.append(incoming)
        return "new"
    if cur.get("state") == "closed" and incoming.get("state") != "closed":
        if at_or_after(cur.get("head"), incoming.get("head"),
                       cur.get("round"), incoming.get("round"), repo):
            return "kept-closed"
    # Severity only ever rises: a finding restated as worse is worse. The
    # validator downgrades inside the review session, before the merge.
    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    if order.get(incoming.get("severity"), 9) > order.get(cur.get("severity"), 9):
        incoming["severity"] = cur.get("severity")
    before = dict(cur)
    cur.update({k: v for k, v in incoming.items() if v not in (None, "")})
    return "unchanged" if cur == before else "updated"


def envelope_objects(text):
    """Every JSON object in the envelope, in order. Reviewers wrap their JSON in
    prose and in fences; scanning for objects reads both without a contract for
    where they sit."""
    out = []
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = dec.raw_decode(text, m.start())
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def envelope_verdict(text):
    m = re.search(r"Verdict:\s*[*_`]*\s*(" + "|".join(VERDICTS) + ")", text)
    return m.group(1) if m else ""


def envelope_head(text):
    m = re.search(r"(?m)^Head:\s*([0-9a-f]{7,40})\s*$", text)
    return m.group(1) if m else ""


def envelope_scope(text):
    m = re.search(r"(?m)^Scope:\s*(full|verify)\s*$", text)
    return m.group(1) if m else "full"


def write_summary(ad, n, verdict, degraded, residual):
    """Byte-shape compatibility with write-review-summary.py:9. `round-reclaim.sh`
    greps this file for a non-empty `"verdict": "..."` and `exit-gate` indexes
    `['verdict']`; neither has heard of the ledger."""
    path = os.path.join(round_dir(ad, n), "review-summary.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"verdict": verdict, "residual_count": residual,
                   "degraded": degraded}, f)
    return path


def merge_envelope(ad, n, envelope, repo):
    text = open(envelope, encoding="utf-8", errors="replace").read()
    scope, head = envelope_scope(text), envelope_head(text)
    entries = load(ad, n)
    counts = {"new": 0, "updated": 0, "unchanged": 0, "kept-closed": 0}
    if scope == "verify":
        for obj in envelope_objects(text):
            for d in obj.get("dispositions") or []:
                if not isinstance(d, dict) or d.get("state") not in ("closed", "open", "regressed"):
                    continue
                cited = d.get("file") and d.get("line")
                # Contract section 6: a closure without a cited line at the
                # current head is not closure. Reading it as `open` is the only
                # safe direction -- the alternative converges on a claim.
                state = d["state"] if (d["state"] != "closed" or cited) else "open"
                counts[upsert(entries, {
                    "id": d.get("id"), "state": state, "round": n, "head": head,
                    "evidence": f"{d.get('file')}:{d.get('line')} {d.get('evidence', '')}"[:500],
                }, repo)] += 1
    for obj in envelope_objects(text):
        for f in obj.get("findings") or []:
            if not isinstance(f, dict) or f.get("severity") not in ("P0", "P1", "P2", "P3"):
                continue
            fid = finding_id(f.get("file"), f.get("title"))
            counts[upsert(entries, {
                "id": fid, "severity": f["severity"], "state": "open",
                "title": str(f.get("title", ""))[:300], "round": n, "head": head,
                "evidence": str(f.get("evidence", ""))[:500],
            }, repo)] += 1
    save(ad, n, entries)
    verdict = envelope_verdict(text)
    degraded = "Code review degraded (headless mode)" in text
    residual = sum(1 for e in entries if e.get("state") not in ("closed", "waived"))
    write_summary(ad, n, verdict, degraded, residual)
    print(f"LEDGER_MERGE round={n} source=envelope scope={scope} "
          f"new={counts['new']} updated={counts['updated']} "
          f"unchanged={counts['unchanged']} kept_closed={counts['kept-closed']} "
          f"total={len(entries)} verdict=[{verdict}]")
    return 0


def merge_fixer(ad, n, result_path, attempt, repo):
    with open(result_path, encoding="utf-8") as f:
        result = json.load(f)
    entries = load(ad, n)
    head = ""
    try:
        head = open(os.path.join(round_dir(ad, n), "pre-head.txt"),
                    encoding="utf-8").read().strip()
    except Exception:
        pass
    counts = {"new": 0, "updated": 0, "unchanged": 0, "kept-closed": 0}
    for partition, state in PARTITION_STATE.items():
        for e in result.get(partition) or []:
            if not isinstance(e, dict):
                continue
            title = str(e.get("finding", e.get("title", "")))
            fid = e.get("finding_id") or finding_id(e.get("file"), title)
            entry = {
                "id": fid, "state": state, "round": n, "head": head,
                "attempt": int(attempt), "title": title[:300],
                "evidence": str(e.get("action", ""))[:500],
            }
            if e.get("severity") in ("P0", "P1", "P2", "P3"):
                entry["severity"] = e["severity"]
            if e.get("design_expanded"):
                entry["design_expanded"] = True
            if e.get("symbol"):
                entry["symbol"] = e["symbol"]
            counts[upsert(entries, entry, repo)] += 1
    save(ad, n, entries)
    print(f"LEDGER_MERGE round={n} source=fixer attempt={attempt} "
          f"new={counts['new']} updated={counts['updated']} "
          f"unchanged={counts['unchanged']} kept_closed={counts['kept-closed']} "
          f"total={len(entries)}")
    return 0


def copy_forward(ad, src, dst):
    """Carry the history into the next round.

    When round `src` has no ledger at all, the nearest earlier round that does
    is copied instead. An empty ledger is not "no findings": it satisfies the
    positive closure requirement vacuously, so one round that failed before its
    merge -- a killed review-gate, a round the operator opened by hand -- would
    otherwise erase every open P0 and let the next round converge. Measured on
    v1 run 8ddb9cce, whose round 6 wrote no fixer-result.json: a straight copy
    carried 0 of 36 entries into round 7.
    """
    entries = load(ad, src)
    if not entries:
        for back in range(int(src) - 1, 0, -1):
            earlier = load(ad, back)
            if earlier:
                print(f"LEDGER_COPY_BACKFILL from={back} (round {src} has no ledger) "
                      f"total={len(earlier)}")
                entries = earlier
                break
    if load(ad, dst):
        # Round N+1 already has a ledger: a resume re-opened the round after the
        # copy landed. Overwriting it would throw away this round's own merges.
        print(f"LEDGER_COPY round={dst} from={src} skipped=already-present "
              f"total={len(load(ad, dst))}")
        return 0
    save(ad, dst, entries)
    print(f"LEDGER_COPY round={dst} from={src} total={len(entries)}")
    return 0


def closure(ad, n):
    """The positive closure requirement: every P0/P1 that ever entered the
    ledger must be `closed`. `applied` is the fixer's claim, not evidence."""
    entries = load(ad, n)
    closed = sum(1 for e in entries if e.get("state") == "closed")
    opened = sum(1 for e in entries if e.get("state") in ("open", "applied", "incomplete"))
    regressed = sum(1 for e in entries if e.get("state") == "regressed")
    new = sum(1 for e in entries if int(e.get("round") or 0) == int(n))
    unclosed = [e for e in entries
                if e.get("severity") in BLOCKING and e.get("state") != "closed"]
    print(f"CLOSURE round={n} closed={closed} open={opened} "
          f"regressed={regressed} new={new}")
    for e in unclosed:
        print(f"CLOSURE_UNCLOSED id={e['id']} severity={e.get('severity')} "
              f"state={e.get('state')} round={e.get('round')} "
              f"title=[{str(e.get('title', ''))[:120]}]")
    return 1 if unclosed else 0


def residuals(ad, n, out):
    """P2/P3 dispositions the run carries out. A P0/P1 never reaches this file:
    closure is what gates convergence, and it admits no disposition but closed."""
    entries = load(ad, n)
    rows = [e for e in entries
            if e.get("severity") in ("P2", "P3")
            and e.get("state") in ("deferred", "pinned", "waived")]
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, sort_keys=True)
    by_state = {}
    for e in rows:
        by_state[e["state"]] = by_state.get(e["state"], 0) + 1
    print(f"RESIDUALS round={n} count={len(rows)} " +
          " ".join(f"{k}={v}" for k, v in sorted(by_state.items())))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("artifacts")
    ap.add_argument("command")
    ap.add_argument("args", nargs="*")
    ap.add_argument("--repo", default=None,
                    help="candidate worktree; enables git ancestry for the "
                         "no-downgrade rule instead of round order alone")
    a = ap.parse_args()
    try:
        if a.command == "merge-envelope":
            return merge_envelope(a.artifacts, int(a.args[0]), a.args[1], a.repo)
        if a.command == "merge-fixer":
            return merge_fixer(a.artifacts, int(a.args[0]), a.args[1],
                               a.args[2] if len(a.args) > 2 else 1, a.repo)
        if a.command == "copy-forward":
            return copy_forward(a.artifacts, int(a.args[0]), int(a.args[1]))
        if a.command == "closure":
            return closure(a.artifacts, int(a.args[0]))
        if a.command == "residuals":
            return residuals(a.artifacts, int(a.args[0]), a.args[1])
    except (IndexError, ValueError) as exc:
        print(f"LEDGER=FAIL {a.command}: {exc}")
        return 2
    print(f"LEDGER=FAIL unknown command: {a.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
