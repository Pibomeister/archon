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
--require-finding-id makes the reviewer's `finding_id` mandatory on every entry.
It is a flag rather than the default because this script is shared by five lanes
and only the v2 claude lane's fixer prompt emits the field; turning it on for
all of them fails every round in bugfix, lite and web on a contract their
prompts were never given. The v2 lane passes it from round-state.py's converge.

Usage: check-fixer-result.py <fixer-result.json> [--require-finding-id]"""
import json
import sys

require_finding_id = "--require-finding-id" in sys.argv[1:]
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError) as exc:
    # Typed, never a traceback: converge runs this and an untyped death is
    # unreadable to the operator and to the node stress harness.
    sys.exit(f"FIXER_BLOCKED: result unreadable [{sys.argv[1]}]: {exc}")
if not isinstance(d, dict):
    sys.exit(f"FIXER_BLOCKED: result is not an object [{sys.argv[1]}]")
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
# Every entry must carry the reviewer's finding_id verbatim. The ledger keys on
# it, so an entry without one mints a SECOND entry instead of moving the
# reviewer's to `applied` -- and a finding that never reaches `applied` can
# never reach `closed`, so positive closure deadlocks on a finding that was in
# fact repaired. B's replay produced 36 entries from a much smaller real
# population with three repaired P1s unclosed at the cap.
if require_finding_id:
    for partition in ("applied", "advisory", "deferred", "incomplete",
                      "cross_repo", "pin_conflict"):
        for entry in d.get(partition) or []:
            if not isinstance(entry, dict) or not isinstance(entry.get("finding_id"), str) \
                    or not entry["finding_id"].strip():
                sys.exit(f"FIXER_BLOCKED: {partition} entry missing finding_id: "
                         f"{str(entry)[:120]}")
print(
    f"APPLIED={len(d['applied'])} ADVISORY={len(d['advisory'])} "
    f"INCOMPLETE={len(d.get('incomplete', []))} CROSS_REPO={len(cross_repo)} "
    f"PIN_CONFLICT={len(pin_conflict)} FAILED=0"
)
