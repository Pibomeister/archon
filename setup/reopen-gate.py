#!/usr/bin/env python3
"""gate-tests companion for a reopened repository stage.

A feature-reopen dispatches the stage with reopen-context.json (bound by
params.json's feature_reopen_context_sha256). The implementer is required to
address that reason; a run that changes nothing has not, and letting it through
re-verifies the exact candidate the integration run already failed (chain
42b42a13, run aab1f254: GATE_TESTS=PASS outcome=NO_CHANGE).

Usage: reopen-gate.py <artifacts-dir> <CHANGED|NO_CHANGE>
Exit 0 with REOPEN_GATE=SKIP (no reopen) or REOPEN_GATE=PASS; exit 1 with IMPLEMENT=FAIL."""
import hashlib
import json
import sys
from pathlib import Path


def fail(message: str) -> None:
    print(f"IMPLEMENT=FAIL {message}")
    sys.exit(1)


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[2] not in {"CHANGED", "NO_CHANGE"}:
        fail("usage: reopen-gate.py <artifacts-dir> <CHANGED|NO_CHANGE>")
    artifacts, outcome = Path(sys.argv[1]), sys.argv[2]
    try:
        params = json.loads((artifacts / "params.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"params.json unreadable: {exc}")
    if not isinstance(params, dict):
        fail("params.json is not an object")
    expected = params.get("feature_reopen_context_sha256")
    if not expected:
        print("REOPEN_GATE=SKIP no reopen context")
        return
    context = artifacts / "reopen-context.json"
    try:
        actual = hashlib.sha256(context.read_bytes()).hexdigest()
    except OSError:
        actual = None
    if actual != expected:
        fail("reopen-context.json missing or altered")
    try:
        files = " ".join((artifacts / "reopen-blocked.txt").read_text(encoding="utf-8", errors="replace").split())
    except FileNotFoundError:
        files = ""
    except OSError as exc:
        fail(f"reopen-blocked.txt unreadable: {exc}")
    if files:
        print(f"IMPLEMENT=FAIL reopen needs files outside the approved allowlist: {files}")
        print("  The allowlist is approval-bound; adding a file is a human act. Per file:")
        print(f"  python3 <.archon>/setup/archon-run.py feature-scope-amend {params.get('run_id')} "
              f"--chain {params.get('logical_chain_id')} --add-file <file> --reason \"<why>\"")
        sys.exit(1)
    if outcome == "NO_CHANGE":
        fail("reopen produced no change")
    print("REOPEN_GATE=PASS")


if __name__ == "__main__":
    main()
