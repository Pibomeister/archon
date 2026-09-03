#!/usr/bin/env python3
"""Stable MCP entrypoint selecting a protected per-chain GitNexus index."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
import control_contract


def fail(msg: str):
    print(f"GITNEXUS_DISPATCH=FAIL {msg}", file=sys.stderr)
    raise SystemExit(1)


def verify_chain_state(path: Path, chain_id: str, index: Path, commit: str) -> None:
    try:
        data = control_contract.verify_chain_state(
            control_contract.secure_read_json(path), chain_id=chain_id
        )
    except control_contract.ControlContractError as exc:
        fail(str(exc))
    baseline = data.get("baseline") or {}
    gitnexus = baseline.get("gitnexus") or {}
    expected_index = Path(gitnexus.get("index_path", "")).resolve()
    expected_commit = gitnexus.get("commit") or (baseline.get("commits") or {}).get("api")
    if expected_index != index.resolve():
        fail("dispatcher index path is not the chain-pinned index")
    if expected_commit != commit:
        fail("dispatcher commit is not the chain-pinned commit")


def pinned_runner(index: Path) -> Path:
    try:
        meta = json.loads((index / ".gitnexus" / "meta.json").read_text(encoding="utf-8"))
        invoked = meta["runnerIdentity"]["invokedArtifact"]
        runner = Path(invoked["path"])
        expected_digest = invoked["digest"]
        info = runner.lstat()
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        fail(f"pinned analyzer artifact unavailable: {exc}")
    if not runner.is_absolute() or stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        fail("pinned analyzer artifact must be an absolute regular non-symlink file")
    actual_digest = "sha256:" + hashlib.sha256(runner.read_bytes()).hexdigest()
    if not isinstance(expected_digest, str) or not hmac.compare_digest(actual_digest, expected_digest):
        fail("pinned analyzer artifact digest mismatch")
    return runner


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    raw = os.environ.get("ARCHON_GITNEXUS_INDEX", "")
    commit = os.environ.get("ARCHON_GITNEXUS_COMMIT", "")
    if not raw or len(commit) != 40 or any(c not in "0123456789abcdefABCDEF" for c in commit):
        fail("protected index path/commit missing")
    p = Path(raw)
    try:
        info = p.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            fail("index must be real directory")
        p = p.resolve()
        run_shim = p / ".gitnexus/run.cjs"
        ri = run_shim.lstat()
        if stat.S_ISLNK(ri.st_mode) or not stat.S_ISREG(ri.st_mode):
            fail("runner shim must be real file")
    except OSError as exc:
        fail(f"index unavailable: {exc}")
    chain_state = os.environ.get("ARCHON_BUGFIX_CHAIN_STATE")
    chain_id = os.environ.get("ARCHON_BUGFIX_CHAIN_ID")
    if chain_state or chain_id:
        if not chain_state or not chain_id:
            fail("chain id and chain state path must be provided together")
        verify_chain_state(Path(chain_state), chain_id, p, commit)
    r = subprocess.run(["git", "-C", str(p), "rev-parse", "HEAD"], capture_output=True, text=True)
    if r.returncode or r.stdout.strip() != commit:
        fail(f"commit mismatch expected={commit} actual={r.stdout.strip()}")
    if args.check:
        pinned_runner(p)
        print(f"GITNEXUS_DISPATCH=OK index={p} commit={commit} chain={chain_id or 'none'}")
        return
    os.execvp("node", ["node", str(pinned_runner(p)), "mcp"])


if __name__ == "__main__":
    main()
