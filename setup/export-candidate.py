#!/usr/bin/env python3
"""Export an untrusted candidate proposal inside the agent container, never release authority."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess


def git(worktree: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-C", str(worktree), *args],
        text=True, capture_output=True, check=True, timeout=30,
    )
    return result.stdout.strip()


def export_candidate(artifacts: Path) -> None:
    params = json.loads((artifacts / "params.json").read_text(encoding="utf-8"))
    worktree = params.get("worktree") if isinstance(params, dict) else None
    if not isinstance(worktree, str) or not worktree:
        raise ValueError("params.json must name the worktree")
    repo = Path(worktree)
    bundle = artifacts / "candidate.bundle"
    proposal_path = artifacts / "candidate.json"
    if any(os.path.lexists(path) for path in (bundle, proposal_path)):
        raise ValueError("candidate output already exists; preserve it and use a fresh guarded run")
    if git(repo, "status", "--porcelain=v1", "--untracked-files=normal"):
        raise ValueError("candidate worktree is dirty")
    commit = git(repo, "rev-parse", "--verify", "HEAD^{commit}")
    tree = git(repo, "rev-parse", "--verify", commit + "^{tree}")
    if not all(re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", oid) for oid in (commit, tree)):
        raise ValueError("malformed candidate identity")
    git(repo, "update-ref", "refs/candidates/sealed", commit)
    git(repo, "bundle", "create", str(bundle), "refs/candidates/sealed")
    with proposal_path.open("x", encoding="utf-8") as output:
        json.dump({"schema": "archon.candidate-proposal.v1", "commit": commit, "tree": tree}, output, sort_keys=True)
        output.write("\n")
    print("CANDIDATE_EXPORT=PROPOSED authority=none")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", required=True, type=Path)
    args = parser.parse_args()
    try:
        export_candidate(args.artifacts.resolve())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"CANDIDATE_EXPORT=FAIL {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
