#!/usr/bin/env python3
"""Lock the prompt doctrine that every lane shares, so a fix lands in all of them.

Usage: lane-doctrine.py check | update

The five lanes are NOT one generated graph on the full<->lite axis the way they
are on the claude<->codex axis: `setup/lite/<lane>/*.prompt.md` overlays replace
a parent node's prompt WHOLESALE (measured 31-63% of overlay lines do not exist
in the parent). So an edit to a parent prompt is silently not inherited by the
lite lane -- the observed failure: the review persona-sizing rule had to be
patched at five sites, and bugfix-lite took the yaml half of the change and not
the prompt half.

This locks the intersection: for every prompt node that appears in 2+ lanes,
the substantive lines those lanes AGREE on today. Dropping such a line from
some lanes but not others is drift and fails `check`. Changing a shared line
everywhere on purpose is a one-command `update`, the same contract as
CODEX_DRIFT: deliberate regeneration, never a silent divergence.
"""
import json
import os
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ARCHON = os.path.dirname(HERE)
LOCK = os.path.join(HERE, "lane-doctrine.lock.json")
LANES = ["full-sdlc-api", "bugfix", "full-sdlc-web", "full-sdlc-api-lite", "bugfix-lite"]
# Short lines are headings, bullets and punctuation that collide by accident
# across unrelated prompts; only substantive sentences are doctrine.
MIN_LEN = 40


def walk(nodes):
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        yield n
        for key in ("loop_group", "body"):
            v = n.get(key)
            if isinstance(v, dict):
                yield from walk(v.get("nodes"))
            elif isinstance(v, list):
                yield from walk(v)


def lane_prompts(workflows_dir):
    """{node_id: {lane: set(substantive lines)}} across every lane."""
    out = {}
    for lane in LANES:
        path = os.path.join(workflows_dir, lane + ".yaml")
        doc = yaml.safe_load(open(path, encoding="utf-8"))
        for n in walk(doc.get("nodes")):
            if not n.get("prompt"):
                continue
            lines = {l.strip() for l in n["prompt"].splitlines() if len(l.strip()) >= MIN_LEN}
            out.setdefault(n["id"], {})[lane] = lines
    return out


def shared(workflows_dir):
    """The lines every lane that HAS the node agrees on, for 2+ lane nodes."""
    locked = {}
    for nid, per_lane in lane_prompts(workflows_dir).items():
        if len(per_lane) < 2:
            continue
        common = set.intersection(*per_lane.values())
        if common:
            locked[nid] = sorted(common)
    return locked


def check(workflows_dir):
    """(ok, message). Reports only lines the lock has that the lanes no longer share."""
    if not os.path.exists(LOCK):
        return False, "LANE_DOCTRINE=FAIL no lock file; run lane-doctrine.py update"
    locked = json.load(open(LOCK, encoding="utf-8"))
    per_node = lane_prompts(workflows_dir)
    drift = []
    for nid, lines in locked.items():
        per_lane = per_node.get(nid, {})
        if not per_lane:
            drift.append(f"  node {nid}: gone from every lane")
            continue
        for line in lines:
            missing = sorted(lane for lane, have in per_lane.items() if line not in have)
            if missing and len(missing) < len(per_lane):
                drift.append(f"  node {nid}: dropped by {','.join(missing)}: {line[:100]}")
    if drift:
        return False, (
            "LANE_DOCTRINE=FAIL shared prompt doctrine diverged across lanes\n"
            + "\n".join(drift)
            + "\n  Fix every lane (lite lanes take the edit in setup/lite/<lane>/*.prompt.md,"
            "\n  then derive-lite.py + derive-codex.py), or if the removal is deliberate"
            "\n  everywhere, re-lock with: python3 .archon/setup/lane-doctrine.py update"
        )
    return True, f"LANE_DOCTRINE=OK nodes={len(locked)} lines={sum(len(v) for v in locked.values())}"


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    wf = os.path.join(ARCHON, "workflows")
    if cmd == "update":
        locked = shared(wf)
        json.dump(locked, open(LOCK, "w", encoding="utf-8"), indent=1, sort_keys=True)
        print(f"LANE_DOCTRINE=UPDATED nodes={len(locked)} lines={sum(len(v) for v in locked.values())}")
        return 0
    if cmd != "check":
        print(__doc__)
        return 2
    ok, msg = check(wf)
    print(msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
