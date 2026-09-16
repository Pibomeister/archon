#!/usr/bin/env python3
"""W8 gate helper: validate the fixer's typed result. Non-empty failed partition
is FIXER_BLOCKED in production — a distinct semantic exit, never more iterations.
The optional "incomplete" partition (transient incompletions: budget, tooling)
passes this gate; converge separately refuses to CONVERGE while any exist, so
they ride into the next round instead of blocking the run.

The optional "pin_conflict" partition is the same shape of arrangement: a P0/P1
whose only repair would change a pinned symbol whose allowed_change is `none`.
It passes here and blocks in converge (row 1), because the resolution is a human
act — revert, `feature-pin-amend`, or re-plan — and not another round.

"design_expanded" on an applied entry says the repair added a method, an exported
symbol, or a call into a pinned symbol. The next round reads it and goes `full`,
so a wrong type here silently downgrades a full round to a verify round.
Usage: check-fixer-result.py <fixer-result.json>"""
import json
import sys

d = json.load(open(sys.argv[1], encoding="utf-8"))
missing = [k for k in ("applied", "failed", "advisory") if k not in d]
if missing:
    sys.exit(f"missing keys: {missing}")
if d["failed"]:
    sys.exit(f"FIXER_BLOCKED: failed partition not empty: {d['failed']}")
cross_repo = d.get("cross_repo", [])
for entry in cross_repo:
    for field in ("finding", "action", "producer_repo"):
        if not entry.get(field):
            sys.exit(f"FIXER_BLOCKED: cross_repo entry missing {field}")
pin_conflict = d.get("pin_conflict", [])
if not isinstance(pin_conflict, list):
    sys.exit("FIXER_BLOCKED: pin_conflict must be a list")
for entry in pin_conflict:
    if not isinstance(entry, dict):
        sys.exit("FIXER_BLOCKED: pin_conflict entry must be an object")
    for field in ("finding", "action", "symbol"):
        if not entry.get(field):
            sys.exit(f"FIXER_BLOCKED: pin_conflict entry missing {field}")
for entry in d["applied"]:
    if isinstance(entry, dict) and "design_expanded" in entry \
            and not isinstance(entry["design_expanded"], bool):
        sys.exit(f"FIXER_BLOCKED: design_expanded must be a boolean: {entry['design_expanded']!r}")
print(
    f"APPLIED={len(d['applied'])} ADVISORY={len(d['advisory'])} "
    f"INCOMPLETE={len(d.get('incomplete', []))} CROSS_REPO={len(cross_repo)} "
    f"PIN_CONFLICT={len(pin_conflict)} FAILED=0"
)
