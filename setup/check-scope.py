#!/usr/bin/env python3
"""Converge scope guard: every path changed since bootstrap (committed or not)
must be in the plan's files-allowlist.json. A breach is a hard human stop —
legitimate scope growth is a human editing the allowlist and resuming.
Usage: check-scope.py <files-allowlist.json> <worktree> <base-sha>
                      [--round N] [--exclude <path> ...] [--stage]
                      [--quarantine <dir>]

--stage makes this a PRE-commit gate for the nodes that used to run
`git add -A`. That staged whatever an agent happened to leave in the worktree,
so a scratch probe became a commit and only converge's post-hoc check caught it
— after the breach was already in history and only a human could clear it
(observed: a round-2 fixer committed __tests__/zzz-timing-check.spec.ts). With
--stage nothing outside the allowlist is ever staged, and a stray stops the
round while it is still a deletable file. Pass base-sha HEAD in this mode: the
committed diff belongs to converge, this call judges the working tree only.

--quarantine makes the gate resolve a stray instead of paging a human, along
the one axis that actually discriminates the two things agents leave behind.
Run 38d72218 produced both, in the same directory:

  commit-import.util.ts        a NEW file a Blocking repo rule required, whose
                               absence from the allowlist was an accident of
                               the allowlist predating the finding
  zzz-timing-check.spec.ts     a scratch probe an agent wrote to check a claim

The first shares a stem with an allowlisted production file in its own
directory (commit-import.service.ts); the second shares nothing with anything.
So a NEW, UNTRACKED file whose stem matches an allowlisted file in the same
directory is the same unit and is adopted into the allowlist, recorded in
allowlist-auto-expansion.json. Any other new file is moved -- never deleted --
under <dir>/strays/, where a human can retrieve it. A MODIFIED tracked file
outside the allowlist is neither: it is an edit to code someone else owns, and
that still stops the round for a human."""
import json
import os
import shutil
import subprocess
import sys

args = sys.argv[1:]
allowlist_path, worktree, base = args[0], args[1], args[2]
round_no = None
stage = False
quarantine = None
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
    elif args[i] == "--quarantine":
        quarantine = args[i + 1]
        i += 2
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


def is_untracked(path):
    """`??` in porcelain: the file did not exist at HEAD, so nobody owns it."""
    out = git("status", "--porcelain", "--", path)
    return any(l.startswith("??") for l in out.splitlines() if l.strip())


def stem(path):
    """The name before the first dot: foo.service.ts, foo.util.ts, foo.spec.ts
    all stem to foo.

    This encodes dot-segmented file naming, and it fails SAFE where that
    convention does not hold. In Python foo.py stems to foo but test_foo.py
    stems to test_foo; in Go foo.go and foo_test.go likewise differ. Those
    files simply do not match, so adoptable() says no and the file is
    quarantined instead — the conservative outcome. The rule is permissive
    only where the convention it reads is actually being used."""
    return os.path.basename(path).split(".", 1)[0]


def repository_list_scope():
    return os.environ.get("ARCHON_FEATURE_SCOPE") == "repositories"


def adoptable(path):
    """A new file belongs to the unit when an allowlisted file in its own
    directory shares its stem. Anything looser adopts unrelated work; anything
    stricter cannot express the sibling a repo rule mandates."""
    d, st = os.path.dirname(path), stem(path)
    return any(os.path.dirname(a) == d and stem(a) == st for a in allowed)


if breaches and stage and quarantine:
    adopted, moved, blocked = [], [], []
    for b in breaches:
        if not is_untracked(b):
            blocked.append(b)          # an edit to a file someone else owns
        elif adoptable(b):
            adopted.append(b)
        else:
            moved.append(b)
    if blocked:
        for b in blocked:
            print(f"COMMIT_SCOPE=STRAY file={b} (modified, not new: outside the allowlist)")
        print("COMMIT_SCOPE=FAIL nothing staged (a human expands files-allowlist.json — "
              "the edit is the approval — or reverts the file, then resume)")
        sys.exit(1)
    if adopted and repository_list_scope():
        for b in adopted:
            print(f"COMMIT_SCOPE=STRAY file={b} "
                  "(new sibling outside the approved repository-stage allowlist)")
        print("COMMIT_SCOPE=FAIL nothing staged (repository-list stages require the "
              "approved joint-plan allowlist; no auto-expansion applied)")
        sys.exit(1)
    for b in moved:
        dest = os.path.join(quarantine, "strays", b)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(os.path.join(worktree, b), dest)
        print(f"COMMIT_SCOPE=QUARANTINED file={b} -> {dest} "
              "(new file, unrelated to any allowlisted file: kept, not committed)")
    if adopted:
        allowed |= set(adopted)
        with open(allowlist_path, "w", encoding="utf-8") as fh:
            json.dump(sorted(allowed), fh, indent=1)
        record = os.path.join(quarantine, "allowlist-auto-expansion.json")
        prior = []
        if os.path.exists(record):
            try:
                prior = json.load(open(record, encoding="utf-8"))
            except Exception:
                prior = []
        prior.extend({"path": b, "sibling_of": sorted(
            a for a in allowed if os.path.dirname(a) == os.path.dirname(b)
            and stem(a) == stem(b) and a != b)} for b in adopted)
        with open(record, "w", encoding="utf-8") as fh:
            json.dump(prior, fh, indent=1)
        for b in adopted:
            print(f"COMMIT_SCOPE=ADOPTED file={b} "
                  "(new sibling of an allowlisted file in the same directory)")
    breaches = []

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
