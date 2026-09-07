#!/usr/bin/env python3
"""Verify an agent no-change claim against pinned profile checks.

The claim file is advisory. This gate only produces a closure intent when the
trusted profile explicitly enables no-change closure, every pinned verifier
passed at baseline and final, and the relevant work product stayed unchanged.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

DIGEST = re.compile(r"^[a-f0-9]{64}$")


def load(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        value = json.load(fh)
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must be an object")
    return value


def write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as output:
        json.dump(value, output, indent=2)
        output.write("\n")
        temporary = Path(output.name)
    os.replace(temporary, path)


def passed_ids(record: dict) -> set[str]:
    if record.get("passed") is not True:
        return set()
    commands = record.get("commands")
    if not isinstance(commands, list):
        return set()
    return {
        item["id"]
        for item in commands
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and item.get("exitCode") == 0
    }


def verified_empty_work_product(record: dict, label: str) -> dict:
    product = record.get("workProduct")
    if not isinstance(product, dict):
        raise ValueError(f"{label} verification is missing work product proof")
    if not isinstance(product.get("sha256"), str) or not DIGEST.fullmatch(product["sha256"]):
        raise ValueError(f"{label} verification has invalid work product digest")
    if not isinstance(product.get("files"), list):
        raise ValueError(f"{label} verification has invalid work product file list")
    if product["files"]:
        raise ValueError("no-change closure cannot carry a changed work product")
    return product


def verify(artifacts: Path) -> dict:
    context = load(artifacts / "run-context.json")
    profile = context.get("profile")
    if not isinstance(profile, dict):
        raise ValueError("run context is missing profile")
    policy = profile.get("noChangeClosure")
    if not isinstance(policy, dict) or policy.get("enabled") is not True:
        raise ValueError("no-change closure is not enabled by the trusted profile")
    verifier_ids = policy.get("verifierIds")
    if not isinstance(verifier_ids, list) or not verifier_ids:
        raise ValueError("no-change closure has no pinned verifier ids")
    verifier_set = set(verifier_ids)

    claim = load(artifacts / "no-change-claim.json")
    if claim.get("category") != "already-satisfied":
        raise ValueError("claim is not an already-satisfied no-change claim")
    if claim.get("source") != "agent":
        raise ValueError("claim source must be the agent, not a verifier")

    baseline = load(artifacts / "baseline-verification.json")
    final = load(artifacts / "verification.json")
    baseline_product = verified_empty_work_product(baseline, "baseline")
    final_product = verified_empty_work_product(final, "final")
    if claim.get("workProduct") != baseline_product:
        raise ValueError("claim work product does not match trusted baseline proof")
    if "round" in final and claim.get("round") != final.get("round"):
        raise ValueError("no-change claim is stale for the final verification round")
    if not verifier_set <= passed_ids(baseline):
        raise ValueError("baseline verification did not pass every pinned verifier")
    if not verifier_set <= passed_ids(final):
        raise ValueError("final verification did not pass every pinned verifier")
    if baseline_product != final_product:
        raise ValueError("work product changed between baseline and final verification")

    intent = {
        "schemaVersion": 1,
        "lifecycleResult": "fulfilled-no-change",
        "claim": claim,
        "verifierIds": verifier_ids,
        "workProduct": final_product,
    }
    write(artifacts / "no-change-closure-intent.json", intent)
    return intent


def main() -> int:
    try:
        verify(Path(sys.argv[1]))
    except (IndexError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"NO_CHANGE_CLOSURE=FAIL {exc}")
        return 1
    print("NO_CHANGE_CLOSURE=PASS lifecycleResult=fulfilled-no-change")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
