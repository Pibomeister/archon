#!/usr/bin/env python3
"""W8 gate helper: validate the fixer's typed result. Non-empty failed partition
is FIXER_BLOCKED in production — a distinct semantic exit, never more iterations.
The optional "incomplete" partition (transient incompletions: budget, tooling)
passes this gate; converge separately refuses to CONVERGE while any exist, so
they ride into the next round instead of blocking the run.
Usage: check-fixer-result.py <fixer-result.json>"""
import json
import sys

d = json.load(open(sys.argv[1], encoding="utf-8"))
missing = [k for k in ("applied", "failed", "advisory") if k not in d]
if missing:
    sys.exit(f"missing keys: {missing}")
if d["failed"]:
    sys.exit(f"FIXER_BLOCKED: failed partition not empty: {d['failed']}")
print(
    f"APPLIED={len(d['applied'])} ADVISORY={len(d['advisory'])} "
    f"INCOMPLETE={len(d.get('incomplete', []))} FAILED=0"
)
