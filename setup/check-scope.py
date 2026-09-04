#!/usr/bin/env python3
"""Converge scope guard: every path changed since bootstrap (committed or not)
must be in the plan's files-allowlist.json. A breach is a hard human stop —
legitimate scope growth is a human editing the allowlist and resuming.
Usage: check-scope.py <files-allowlist.json> <worktree> <base-sha>
                      [--round N] [--exclude <path> ...] [--stage]

--stage makes this a PRE-commit gate for the nodes that used to run
`git add -A`. That staged whatever an agent happened to leave in the worktree,
so a scratch probe became a commit and only converge's post-hoc check caught it
— after the breach was already in history and only a human could clear it
(observed: a round-2 fixer committed __tests__/zzz-timing-check.spec.ts). With
--stage nothing outside the allowlist is ever staged, and a stray stops the
round while it is still a deletable file. Pass base-sha HEAD in this mode: the
committed diff belongs to converge, this call judges the working tree only."""
import json
import subprocess
import sys

args = sys.argv[1:]
allowlist_path, worktree, base = args[0], args[1], args[2]
round_no = None
stage = False
excludes = {".env"}
i = 3
while i < len(args):
    if args[i] == "--round":
        round_no = args[i + 1]
        i += 2
    elif args[i] == "--exclude":
        excludes.add(args[i + 1])
        i += 2
    elif args[i] == "--stage":
        stage = True
        i += 1
    else:
        sys.exit(f"SCOPE_GUARD=FAIL unknown argument {args[i]}")

# Typed, never a traceback: this now runs inside commit nodes, and a node that
# dies untyped is unreadable to the operator and to the stress harness.
try:
    allowed = set(json.load(open(allowlist_path, encoding="utf-8")))
except Exception as exc:
    tag = "COMMIT_SCOPE" if "--stage" in args else "SCOPE_GUARD"
    print(f"{tag}=FAIL unreadable files-allowlist.json [{allowlist_path}]: {exc}")
    sys.exit(1)

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
    if stage:
        for p in breaches:
            print(f"COMMIT_SCOPE=STRAY file={p}")
        print("COMMIT_SCOPE=FAIL nothing staged (delete the stray, or a human expands "
              "files-allowlist.json — the edit is the approval — then resume)")
    else:
        tag = f"SCOPE_BREACH round={round_no}" if round_no else "SCOPE_BREACH"
        for p in breaches:
            print(f"{tag} file={p}")
    sys.exit(1)

if stage:
    # Only allowlisted paths, one at a time: `git add -- <path>` stages a
    # deletion as readily as an edit, and a path the round never touched is a
    # silent no-op. Excluded paths (.env, pnpm-lock.yaml) are tolerated dirty
    # by the converge cleanliness check and must not be committed here.
    for p in sorted(allowed):
        subprocess.run(["git", "-C", worktree, "add", "--", p], capture_output=True)
    print(f"COMMIT_SCOPE=OK allowlisted={len(allowed)}")
    sys.exit(0)

print(f"SCOPE_OK files={len(changed)}")
