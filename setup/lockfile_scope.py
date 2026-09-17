#!/usr/bin/env python3
"""A package-manager lockfile follows its manifest.

An implementer who adds a dependency changes package.json AND the repository's
lockfile. Plans list package.json; they rarely list the lockfile. Every scope
reader used to treat the lockfile as either an ordinary path (a breach the plan
never meant) or a blanket `--exclude pnpm-lock.yaml` (never staged, so the
candidate shipped a manifest its frozen install rejects and the tree stayed
dirty). The rule, in one place for check-scope.py and feature_chain.py:

  - a lockfile change is in scope when the allowlist names the lockfile, or
    when an allowlisted package.json changed in the same diff; it is then
    staged and committed with the manifest;
  - a lockfile change with no in-scope manifest change is a breach, except an
    UNCOMMITTED one in a repository whose profile declares that its install
    rewrites the lockfile (`lockfile_install_drift`, web-app): that drift is
    tolerated and never staged, exactly as the lanes always treated it.

Lockfile names and the drift declaration come from repo-profile.sh, so a repo
never inherits another repo's lockfile name."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = "package.json"


def profile(repo: str) -> dict | None:
    out = subprocess.run(["bash", str(HERE / "repo-profile.sh"), "--json"],
                         capture_output=True, encoding="utf-8", check=True).stdout
    return json.loads(out)["profiles"].get(repo)


def repo_beside(allowlist_path: str) -> str | None:
    """The run's repo from the params.json that sits beside its allowlist.
    No params.json means no known repo, and no lockfile rule applies."""
    params = Path(os.path.abspath(allowlist_path)).parent / "params.json"
    try:
        data = json.loads(params.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    repo = data.get("repo", "api") if isinstance(data, dict) else None
    return repo if isinstance(repo, str) else None


def judge(prof: dict | None, allowed: set[str], committed: set[str],
          uncommitted: set[str]) -> tuple[set[str], set[str]]:
    """(lockfiles in scope, lockfiles tolerated as install drift)."""
    if not prof:
        return set(), set()
    changed = committed | uncommitted
    manifest = any(os.path.basename(p) == MANIFEST and p in allowed for p in changed)
    in_scope, tolerated = set(), set()
    for lock in set(prof.get("lockfiles", [])) & changed:
        if lock in allowed or manifest:
            in_scope.add(lock)
        elif prof.get("lockfile_install_drift") and lock not in committed:
            tolerated.add(lock)
    return in_scope, tolerated
