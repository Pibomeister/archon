#!/usr/bin/env python3
"""Print the bugfix chain env a run's LAUNCHER exported, as KEY=VALUE lines.

archon-run.py sets these via chain_env() when it starts a bugfix run. Nothing
else did, so both documented post-launch controls -- `resume.sh` after a typed
stop, and `archon workflow approve` at a gate -- reached the first attestation
node and died with "no private chain state", having re-paid for the work.

One source, two callers, so the two paths cannot drift.

Usage: chain-env.py <artifacts-dir> [--control-dir DIR]
Exits 0 printing nothing when the run has no bugfix-chain.json (feature lanes).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

DEFAULT_CONTROL = Path.home() / ".archon" / "control" / "codex-lite"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("artifacts", type=Path)
    ap.add_argument("--control-dir", type=Path, default=DEFAULT_CONTROL)
    args = ap.parse_args()

    chain_file = args.artifacts / "bugfix-chain.json"
    if not chain_file.is_file():
        return
    chain = json.loads(chain_file.read_text(encoding="utf-8"))
    cid = chain["logical_chain_id"]
    baseline = chain.get("baseline") or {}
    gitnexus = baseline.get("gitnexus") or {}
    commits = baseline.get("commits") or {}
    env = {
        "ARCHON_BUGFIX_CHAIN_ID": cid,
        "ARCHON_BUGFIX_CHAIN_STATE": str(args.control_dir / "bugfix-chains" / f"{cid}.json"),
        "ARCHON_ATTESTATION_DIR": str(args.control_dir / "attestations"),
        "ARCHON_GITNEXUS_COMMIT": str(gitnexus.get("commit") or commits.get("api", "")),
        "ARCHON_API_BASELINE": str(commits.get("api", "")),
        "ARCHON_WEB_BASELINE": str(commits.get("web-app", "")),
    }
    if gitnexus.get("index_path"):
        env["ARCHON_GITNEXUS_INDEX"] = str(gitnexus["index_path"])
    for key, value in env.items():
        if value:
            print(f"{key}={value}")


if __name__ == "__main__":
    main()
