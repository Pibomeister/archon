#!/usr/bin/env python3
"""M2.5 selective gen:api patch (M0.9 binding: full regen is BANNED in the workflow).

Takes the committed api-client.d.ts and a freshly generated full regen, and applies
ONLY the diff hunks whose changed lines mention the feature marker. Everything else
(accumulated drift from unrelated endpoints) is rejected and reported — never
silently dropped.

Usage: selective-genapi-patch.py <committed-file> <fresh-regen-file> <marker> [marker2 ...]
Exits non-zero if no hunk matched any marker (the feature failed to appear).
"""
import difflib
import sys

committed_path, fresh_path = sys.argv[1], sys.argv[2]
markers = [m.lower() for m in sys.argv[3:]]
assert markers, "at least one marker required"

committed = open(committed_path, encoding="utf-8").read().splitlines(keepends=True)
fresh = open(fresh_path, encoding="utf-8").read().splitlines(keepends=True)

sm = difflib.SequenceMatcher(None, committed, fresh, autojunk=False)
out = []
accepted = rejected = 0
for tag, i1, i2, j1, j2 in sm.get_opcodes():
    if tag == "equal":
        out.extend(committed[i1:i2])
        continue
    changed_text = "".join(committed[i1:i2] + fresh[j1:j2]).lower()
    if any(m in changed_text for m in markers):
        out.extend(fresh[j1:j2])
        accepted += 1
    else:
        out.extend(committed[i1:i2])
        rejected += 1

open(committed_path, "w", encoding="utf-8").write("".join(out))
print(f"ACCEPTED_HUNKS={accepted} REJECTED_DRIFT_HUNKS={rejected}")
if accepted == 0:
    committed_text = "".join(committed).lower()
    if all(m in committed_text for m in markers):
        print("PATCH_ALREADY_APPLIED (idempotent re-run: markers present, remaining hunks are drift)")
        sys.exit(0)
    sys.exit("FAIL: no hunk mentioned the feature markers - endpoint missing from regen?")
