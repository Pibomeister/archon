#!/usr/bin/env python3
"""Trusted exact-candidate verification with private, reusable command receipts."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import time

import feature_chain as chain
import review_policy as policy


def files_digest(roots: list[Path]) -> str:
    result = hashlib.sha256()
    visited: set[Path] = set()
    for root in roots:
        if not root.exists():
            result.update(str(root).encode() + b"\0missing\n")
            continue
        paths = [root] if root.is_file() else (
            Path(directory) / name
            for directory, subdirs, names in os.walk(root, followlinks=True)
            if not _seen_directory(Path(directory), visited, subdirs, result)
            for name in sorted(names)
        )
        for path in paths:
            result.update(str(path).encode() + b"\0")
            if path.is_symlink():
                result.update(os.readlink(path).encode() + b"\0")
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    result.update(chunk)
            result.update(b"\n")
    return result.hexdigest()


def _seen_directory(path: Path, visited: set[Path], subdirs: list[str], result) -> bool:
    for name in sorted(subdirs):
        child = path / name
        if child.is_symlink():
            result.update(str(child).encode() + b"\0" + os.readlink(child).encode() + b"\n")
    resolved = path.resolve()
    if resolved in visited:
        subdirs.clear()
        return True
    visited.add(resolved)
    subdirs[:] = sorted(name for name in subdirs if name not in {".cache", ".git"})
    return False


def fingerprint(worktree: Path, argv: list[str]) -> dict:
    tree = subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD^{tree}"],
                          check=True, capture_output=True, text=True).stdout.strip()
    executable = shutil.which(argv[0])
    if executable is None:
        raise chain.FeatureChainError(f"verification executable is unavailable: {argv[0]}")
    return {"source_tree_digest": hashlib.sha256(tree.encode()).hexdigest(),
            "dependencies_digest": files_digest([worktree / "node_modules", worktree / "pnpm-lock.yaml",
                                                  worktree / "bun.lock", worktree / "package-lock.json", Path(executable)]),
            "configuration_digest": files_digest(sorted(worktree.glob(".env*"))),
            "environment_fingerprint": policy.digest({"argv": argv, "environment": {
                key: value for key, value in os.environ.items() if key not in {"_", "SHLVL"}}})}


def command_argv(value: object) -> list[str]:
    argv = shlex.split(value) if isinstance(value, str) else value.get("argv") if isinstance(value, dict) else None
    if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) and arg for arg in argv):
        raise chain.FeatureChainError("verification requires approved command argv")
    return argv


def verify(artifacts: Path) -> list[dict]:
    control = Path(os.environ["ARCHON_CONTROL_DIR"])
    chain_id = os.environ["ARCHON_FEATURE_CHAIN_ID"]
    state = chain.read_state(control, chain_id)
    current = state["current_run"]
    if Path(current["artifacts_dir"]).resolve() != artifacts.resolve():
        raise chain.FeatureChainError("verification artifacts do not belong to current run")
    repo = current["repo"]
    worktree = Path(state["worktrees"][repo]["worktree"])
    if chain.repo_is_dirty(worktree):
        raise chain.FeatureChainError("verification requires a clean candidate")
    head = chain.repo_head(worktree, repo)
    candidate = policy.exact_candidate(repo, state["worktrees"][repo]["baseline"], head)
    commands = state["stages"][repo]["plan"]["verification"]
    ids = chain.review_state_required_receipts(state, repo)
    if not commands or len(ids) != len(set(ids)):
        raise chain.FeatureChainError("verification commands are empty or have duplicate identities")
    root = chain._ensure_private_dir(control / "review-verification", "review verification root")
    private = chain._ensure_private_dir(root / current["run_id"], "run review verification root")
    logs = artifacts / "review-verification"
    logs.mkdir(exist_ok=True)
    receipts = []
    for command_id, command in zip(ids, commands):
        argv = command_argv(command)
        expected = fingerprint(worktree, argv)
        key = policy.digest({"candidate": candidate, "command": command_id, "argv": argv, **expected})
        path = private / (key + ".json")
        if path.exists():
            saved = chain._secure_read(path)
            receipt = saved["receipt"]
            mac = chain.hmac_sha256(state["chain_secret"], {"receipt": receipt})
            output = Path(receipt["retained_output"])
            if (not chain.hmac.compare_digest(str(saved.get("receipt_mac", "")), mac)
                    or not output.is_file() or chain.file_digest(output) != receipt["retained_output_digest"]):
                raise chain.FeatureChainError("retained verification receipt or output drifted")
        else:
            attempt = str(time.time_ns())
            output = logs / (key + "." + attempt + ".log")
            with output.open("wb") as stream:
                result = subprocess.run(argv, cwd=worktree, stdout=stream, stderr=subprocess.STDOUT, timeout=1800)
            if chain.repo_is_dirty(worktree) or chain.repo_head(worktree, repo) != head:
                raise chain.FeatureChainError("verification changed the candidate")
            if expected != fingerprint(worktree, argv):
                raise chain.FeatureChainError("verification dependency/configuration/environment changed during command")
            receipt = {"schema_version": 1, "policy": policy.POLICY, "candidate": candidate,
                       "command": command_id, "argv": argv, **expected,
                       "exit_status": result.returncode, "retained_output": str(output),
                       "retained_output_digest": chain.file_digest(output)}
            receipt_path = path if result.returncode == 0 else private / (key + ".failed." + attempt + ".json")
            chain._secure_write(receipt_path, {"receipt": receipt, "receipt_mac": chain.hmac_sha256(state["chain_secret"], {"receipt": receipt})})
        receipts.append(receipt)
        if receipt["exit_status"] != 0:
            raise chain.FeatureChainError(f"VERIFICATION_REQUIRED failed {command_id}; retained output {receipt['retained_output']}")
    return receipts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    try:
        receipts = verify(args.artifacts)
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        print(f"REVIEW_VERIFICATION=FAIL {exc}")
        return 1
    print(json.dumps({"status": "verified", "commands": [item["command"] for item in receipts]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
