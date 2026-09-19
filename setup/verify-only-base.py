#!/usr/bin/env python3
"""bootstrap companion for a --verify-only feature-reopen.

A verify-only reopen re-verifies a fix that is already in the stage worktree
(committed by a stopped run, or by hand) without running the implement node.
bootstrap-head.txt is the base every downstream gate diffs against (gate-tests'
change check, deslop, scope, review, local-candidate squash). Left at the
worktree HEAD it would make the fix invisible: every gate would see an empty
change (chain 42b42a13, run e21573ca). So the base becomes the previous
candidate head the reopen recorded, and the change under verification is
exactly previous-candidate..worktree.

The implement node normally writes commit-msg.txt; with it skipped, the subject
of the oldest commit since the previous head is reused (or a fallback when the
fix is still uncommitted).

Usage: verify-only-base.py <artifacts-dir> <worktree>
Exit 0 with VERIFY_ONLY=SKIP (not verify-only) or VERIFY_ONLY=BASE; exit 1 with VERIFY_ONLY=FAIL."""
import json
import re
import subprocess
import sys
from pathlib import Path


def fail(message: str) -> None:
    print(f"VERIFY_ONLY=FAIL {message}")
    sys.exit(1)


def git(worktree: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", worktree, *args], capture_output=True, text=True)


def main() -> None:
    if len(sys.argv) != 3:
        fail("usage: verify-only-base.py <artifacts-dir> <worktree>")
    artifacts, worktree = Path(sys.argv[1]), sys.argv[2]
    try:
        params = json.loads((artifacts / "params.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"params.json unreadable: {exc}")
    if params.get("feature_verify_only") != "yes":
        print("VERIFY_ONLY=SKIP")
        return
    previous = str(params.get("feature_previous_head") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", previous):
        fail(f"feature_previous_head is not a commit sha: {previous!r}")
    if git(worktree, "merge-base", "--is-ancestor", previous, "HEAD").returncode != 0:
        fail(f"previous head {previous[:12]} is not an ancestor of the worktree HEAD")
    (artifacts / "bootstrap-head.txt").write_text(previous + "\n", encoding="utf-8")
    message = artifacts / "commit-msg.txt"
    if not message.is_file() or not message.read_text(encoding="utf-8").strip():
        subjects = git(worktree, "log", "--reverse", "--format=%s", f"{previous}..HEAD").stdout.splitlines()
        subject = subjects[0] if subjects else f"fix({params.get('repo', 'repo')}): verify-only reopen of {params.get('slug', 'stage')}"
        message.write_text(subject + "\n", encoding="utf-8")
    head = git(worktree, "rev-parse", "HEAD").stdout.strip()
    print(f"VERIFY_ONLY=BASE previous={previous} head={head}")


if __name__ == "__main__":
    main()
