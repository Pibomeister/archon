#!/usr/bin/env python3
"""Waiver ledger: append each advisory entry from a round's fixer result to
waivers.md unless an entry with the same finding-text prefix is already there.
The ledger is what stops later review rounds from re-litigating a recorded
scope decision without new evidence.
Usage: update-waivers.py <round-N/fixer-result.json> <waivers.md>"""
import json
import os
import re
import sys

PREFIX_LEN = 80

result_path, ledger_path = sys.argv[1], sys.argv[2]
d = json.load(open(result_path, encoding="utf-8"))
advisory = d.get("advisory", [])

m = re.search(r"round-(\d+)", result_path)
round_no = m.group(1) if m else "?"

existing = ""
if os.path.isfile(ledger_path):
    existing = open(ledger_path, encoding="utf-8").read()

added = 0
lines = []
if not existing:
    lines.append("# Waiver ledger\n")
    lines.append(
        "Advisory findings the fixer declined with rationale. Reviewers must not\n"
        "re-raise a waived finding as actionable without specific new evidence.\n"
    )
for e in advisory:
    finding = e.get("finding", "").strip()
    rationale = e.get("action", "").strip()
    if not finding:
        continue
    if finding[:PREFIX_LEN] in existing:
        continue
    lines.append(f"\n## [round {round_no}] {finding}\n\n{rationale}\n")
    existing += finding[:PREFIX_LEN]
    added += 1

if added:
    with open(ledger_path, "a", encoding="utf-8") as f:
        f.writelines(lines)
print(f"WAIVERS_ADDED={added} TOTAL_ADVISORY={len(advisory)}")
