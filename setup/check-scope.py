#!/usr/bin/env python3
"""Converge scope guard: every path changed since bootstrap (committed or not)
must be in the plan's files-allowlist.json. A breach is a hard human stop —
legitimate scope growth is a human editing the allowlist and resuming.
Usage: check-scope.py <files-allowlist.json> <worktree> <base-sha>
                      [--round N] [--exclude <path> ...]"""
import json
import subprocess
import sys

args = sys.argv[1:]
allowlist_path, worktree, base = args[0], args[1], args[2]
round_no = None
excludes = {".env"}
i = 3
while i < len(args):
    if args[i] == "--round":
        round_no = args[i + 1]
        i += 2
    elif args[i] == "--exclude":
        excludes.add(args[i + 1])
        i += 2
    else:
        sys.exit(f"SCOPE_GUARD=FAIL unknown argument {args[i]}")

allowed = set(json.load(open(allowlist_path, encoding="utf-8")))

def git(*cmd):
    return subprocess.run(
        ["git", "-C", worktree, *cmd], capture_output=True, encoding="utf-8", check=True
    ).stdout

changed = set()
for line in git("diff", "--name-only", f"{base}..HEAD").splitlines():
    if line.strip():
        changed.add(line.strip())
for line in git("status", "--porcelain").splitlines():
    if not line.strip():
        continue
    path = line[3:]
    if " -> " in path:  # rename: check the destination
        path = path.split(" -> ", 1)[1]
    changed.add(path.strip())

breaches = sorted(p for p in changed if p not in allowed and p not in excludes)
if breaches:
    tag = f"SCOPE_BREACH round={round_no}" if round_no else "SCOPE_BREACH"
    for p in breaches:
        print(f"{tag} file={p}")
    sys.exit(1)
print(f"SCOPE_OK files={len(changed)}")
