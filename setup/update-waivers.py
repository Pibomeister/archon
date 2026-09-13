#!/usr/bin/env python3
"""Waiver ledger: append each advisory entry from a round's fixer result to
waivers.md and mirror it into waivers.json keyed by a normalized finding.
The ledger is what stops later review rounds from re-litigating a recorded
scope decision without new evidence. An entry is skipped when its normalized
key is already in waivers.json OR its raw 80-char prefix is already present in
waivers.md: ledgers written before waivers.json existed carry only the raw
prefix, and a multi-line heading from such a ledger back-fills to a key that
cannot match.
Usage: update-waivers.py <round-N/fixer-result.json> <waivers.md>"""
import json
import os
import re
import sys

PREFIX_LEN = 80
SCHEMA = "archon.waiver-ledger.v1"
HEADING = re.compile(r"^## (?:\[round ([^\]]*)\] )?(.*)$")


def collapse(text):
    return " ".join(text.split())


def key_of(text):
    return collapse(re.sub(r"[^a-z0-9\s]", "", text.casefold()))[:PREFIX_LEN]


result_path, ledger_path = sys.argv[1], sys.argv[2]
json_path = os.path.join(os.path.dirname(ledger_path), "waivers.json")
d = json.load(open(result_path, encoding="utf-8"))
advisory = d.get("advisory", [])

m = re.search(r"round-(\d+)", result_path)
round_no = m.group(1) if m else "?"

existing = ""
if os.path.isfile(ledger_path):
    existing = open(ledger_path, encoding="utf-8").read()

entries = []
if os.path.isfile(json_path):
    entries = json.load(open(json_path, encoding="utf-8")).get("entries", [])
keys = {e.get("key") for e in entries}

for line in existing.splitlines():
    h = HEADING.match(line)
    if not h:
        continue
    finding = collapse(h.group(2))
    k = key_of(finding)
    if not k or k in keys:
        continue
    keys.add(k)
    entries.append({"key": k, "finding": finding, "rationale": "", "round": h.group(1) or "?"})

added = 0
lines = []
if not existing:
    lines.append("# Waiver ledger\n")
    lines.append(
        "Advisory findings the fixer declined with rationale. Reviewers must not\n"
        "re-raise a waived finding as actionable without specific new evidence.\n"
    )
for e in advisory:
    raw = e.get("finding", "").strip()
    finding = collapse(raw)
    rationale = e.get("action", "").strip()
    if not finding:
        continue
    k = key_of(finding)
    if k in keys or raw[:PREFIX_LEN] in existing:
        continue
    lines.append(f"\n## [round {round_no}] {finding}\n\n{rationale}\n")
    existing += finding
    keys.add(k)
    entries.append({"key": k, "finding": finding, "rationale": rationale, "round": round_no})
    added += 1

if added:
    with open(ledger_path, "a", encoding="utf-8") as f:
        f.writelines(lines)
if entries:
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"schema": SCHEMA, "entries": entries}, f, indent=2)
        f.write("\n")
print(f"WAIVERS_ADDED={added} TOTAL_ADVISORY={len(advisory)} LEDGER_KEYS={len(entries)}")
