#!/usr/bin/env python3
"""Create and verify controller-owned manifest attestations."""
from __future__ import annotations

import argparse
import hmac
import json
import os
from pathlib import Path

import control_contract


def fail(message: str) -> None:
    print(f"CONTROLLER_ATTEST=FAIL {message}")
    raise SystemExit(1)


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"JSON unavailable at {path}: {exc}")
    if not isinstance(value, dict):
        fail(f"JSON must be an object: {path}")
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=["proof", "approval", "lite-approval"])
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--chain-state", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    try:
        state = control_contract.verify_chain_state(
            control_contract.secure_read_json(args.chain_state)
        )
    except control_contract.ControlContractError as exc:
        fail(str(exc))

    manifest = load_json(args.artifacts / f"{args.kind}-manifest.json")
    run_id = state.get("current_run_id") or args.artifacts.name
    artifact_name = args.artifacts.name
    if len(artifact_name) == 32 and all(c in "0123456789abcdefABCDEF" for c in artifact_name):
        if run_id != artifact_name:
            fail("artifact directory/run mismatch")
    if manifest.get("chain_id") != state.get("logical_chain_id") or manifest.get("run_id") != run_id:
        fail("manifest run/chain mismatch")

    if args.verify:
        seal = load_json(args.out)
        authority_mac = seal.pop("authority_mac", None)
        try:
            expected = control_contract.hmac_sha256(state["chain_secret"], seal)
        except (KeyError, control_contract.ControlContractError) as exc:
            fail(str(exc))
        if (not isinstance(authority_mac, str)
                or not hmac.compare_digest(authority_mac, expected)
                or seal.get("manifest_hash") != manifest.get("semantic_hash")
                or seal.get("manifest_type") != args.kind):
            fail("seal authority/manifest mismatch")
        print(f"CONTROLLER_ATTEST=VERIFIED kind={args.kind} hash={manifest['semantic_hash']}")
        return

    if args.kind == "proof":
        recovery = load_json(args.artifacts / "proof-recovery.json")
        if recovery.get("state") != "CONVERGED":
            fail("proof recovery not converged")
        role = "blind-verifier"
    elif args.kind == "approval":
        try:
            round_number = int((args.artifacts / "rca-round.txt").read_text().strip())
        except (OSError, ValueError) as exc:
            fail(f"critic round unavailable: {exc}")
        critique = load_json(args.artifacts / f"rca-round-{round_number}" / "critique.json")
        if critique.get("verdict") != "ACCEPT":
            fail("final critic did not ACCEPT")
        role = "final-critic"
    else:
        classification = load_json(args.artifacts / "fix-classification.json")
        if classification.get("implementation_result") not in {
            "FULL_FIX", "PARTIAL_FIX", "CLASS_HARDENING", "NO_CODE_CHANGE"
        }:
            fail("lite classification is not approval-ready")
        role = "lite-rca-gate"
    if args.kind in {"approval", "lite-approval"}:
        debug = load_json(args.artifacts / "debug-phase.json")
        failures = int((state.get("counters") or {}).get("causal_fix_failures", 0))
        if debug.get("fix_attempt_count") != failures:
            fail("debug-phase fix_attempt_count does not match protected chain failures")
        if failures >= 3:
            receipt = state.get("architecture_review_receipt")
            if not isinstance(receipt, dict) or receipt.get("approved") is not True:
                fail("three causal fix failures require a protected architecture review receipt")
            if debug.get("architecture_review_required") is not True:
                fail("architecture review must remain explicit after three failed fixes")

    seal = {
        "schema_version": 1,
        "authority": "controller",
        "manifest_type": args.kind,
        "role": role,
        "provider": manifest["provider"],
        "chain_id": manifest["chain_id"],
        "run_id": run_id,
        "manifest_hash": manifest["semantic_hash"],
        "verdict": "ACCEPT",
    }
    seal["authority_mac"] = control_contract.hmac_sha256(state["chain_secret"], seal)
    try:
        control_contract.secure_write_json(args.out, seal)
    except control_contract.ControlContractError as exc:
        fail(str(exc))
    print(f"CONTROLLER_ATTEST=OK kind={args.kind} role={role} hash={manifest['semantic_hash']}")


if __name__ == "__main__":
    main()
