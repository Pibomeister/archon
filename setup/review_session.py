#!/usr/bin/env python3
"""Bind review assignments to Codex thread events in the private controller store."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re

import feature_chain as chain


def _receipt_root(control: Path) -> Path:
    return chain._ensure_private_dir(control / "review-sessions", "review session directory")


def _binding(state: dict, run_id: str) -> Path:
    current = state.get("current_run", {})
    if state.get("provider") != "codex" or current.get("run_id") != run_id:
        raise chain.FeatureChainError("review session does not belong to the current Codex run")
    if state.get("review_policy", {}).get("qualification_status") != "qualified":
        raise chain.FeatureChainError("review session requires a qualified policy")
    return Path(current["artifacts_dir"])


def bind(control: Path, chain_id: str, run_id: str, session_id: str, slot: int | None,
         role: str, model: str, effort: str) -> dict:
    if not chain.RUN_ID_RE.fullmatch(run_id) or not re.fullmatch(r"[a-zA-Z0-9-]{8,80}", session_id):
        raise chain.FeatureChainError("invalid review session identity")
    if model != "gpt-5.6-sol" or effort != "medium":
        raise chain.FeatureChainError("review session must run Sol/medium")
    if role not in {"reviewer", "fixer"} or (role == "reviewer" and slot not in range(1, 10)):
        raise chain.FeatureChainError("invalid review session role or slot")
    with chain.chain_lock(control, chain_id):
        state = chain.read_state(control, chain_id)
        artifacts = _binding(state, run_id)
        current = json.loads((artifacts / "current-review.json").read_text())
        if role == "reviewer":
            matches = [item for item in current["assignments"] if item["slot"] == slot]
            if len(matches) != 1 or matches[0]["status"] != "pending":
                raise chain.FeatureChainError("review slot is not awaiting a session")
            assignment = matches[0]
        else:
            assignment = {"context_digest": chain.digest(current["candidate"])}
        body = {"schema": "archon.review-session.v1", "logical_chain_id": chain_id,
                "run_id": run_id, "session_id": session_id, "role": role, "slot": slot,
                "round": current["round"], "candidate": current["candidate"],
                "context_digest": assignment["context_digest"], "model": model,
                "reasoning_effort": effort}
        root = _receipt_root(control)
        filename = f"{run_id}.{current['round']}.{role}-{slot or 0}.{session_id}.json"
        path = root / filename
        sealed = {**body, "receipt_mac": chain.hmac_sha256(state["chain_secret"], body)}
        if path.exists() and chain._secure_read(path) != sealed:
            raise chain.FeatureChainError("review session receipt conflicts with retained provenance")
        chain._secure_write(path, sealed)
        return body


def verify_session_provenance(artifacts: Path, current: dict, assignment: dict,
                              response: dict) -> dict:
    control = Path(os.environ["ARCHON_CONTROL_DIR"])
    chain_id = os.environ["ARCHON_FEATURE_CHAIN_ID"]
    state = chain.read_state(control, chain_id)
    run_id = state.get("current_run", {}).get("run_id")
    if _binding(state, run_id).resolve() != artifacts.resolve():
        raise chain.FeatureChainError("review provenance artifact binding mismatch")
    matches = []
    for path in _receipt_root(control).glob(f"{run_id}.{current['round']}.reviewer-{assignment['slot']}.*.json"):
        receipt = chain._secure_read(path)
        body = {key: value for key, value in receipt.items() if key != "receipt_mac"}
        expected_mac = chain.hmac_sha256(state["chain_secret"], body)
        if not chain.hmac.compare_digest(str(receipt.get("receipt_mac", "")), expected_mac):
            raise chain.FeatureChainError("review session provenance MAC mismatch")
        if (body.get("candidate") == assignment["candidate"]
                and body.get("context_digest") == assignment["context_digest"]
                and body.get("run_id") == run_id and body.get("logical_chain_id") == chain_id
                and body.get("round") == current["round"] and body.get("slot") == assignment["slot"]
                and body.get("role") == "reviewer"):
            matches.append(body)
    claimed = response.get("reviewer_id")
    if claimed:
        matches = [item for item in matches if item["session_id"] == claimed]
    if len(matches) != 1:
        raise chain.FeatureChainError("missing or ambiguous controller-bound reviewer session")
    receipt = matches[0]
    if receipt["model"] != "gpt-5.6-sol" or receipt["reasoning_effort"] != "medium":
        raise chain.FeatureChainError("review provenance model mismatch")
    for path in _receipt_root(control).glob(f"{run_id}.*.fixer-0.{receipt['session_id']}.json"):
        author = chain._secure_read(path)
        body = {key: value for key, value in author.items() if key != "receipt_mac"}
        if not chain.hmac.compare_digest(str(author.get("receipt_mac", "")), chain.hmac_sha256(state["chain_secret"], body)):
            raise chain.FeatureChainError("fixer session provenance MAC mismatch")
        raise chain.FeatureChainError("reviewer session also authored a repair")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-dir", type=Path, required=True)
    parser.add_argument("--chain", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--slot", type=int)
    parser.add_argument("--role", choices=["reviewer", "fixer"], required=True)
    args = parser.parse_args()
    try:
        bind(args.control_dir, args.chain, args.run, args.session, args.slot, args.role,
             os.environ.get("ARCHON_CODEX_PINNED_MODEL", ""),
             os.environ.get("ARCHON_CODEX_PINNED_REASONING_EFFORT", ""))
    except (ValueError, OSError, KeyError) as exc:
        print(f"REVIEW_SESSION=FAIL {exc}")
        return 1
    print("REVIEW_SESSION=BOUND")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
