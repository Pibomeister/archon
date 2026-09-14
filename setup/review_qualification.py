#!/usr/bin/env python3
"""Validate matched review replay evidence without launching a model."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


BLOCKERS = frozenset({
    "malformed-claim-disclosure", "recipient-resurrection",
    "reconciliation-audit-id-reuse", "missing-iam-permission",
    "incorrect-index-types", "http-contract-mismatch",
})
UNIT = "provider-total-tokens-cached-input-counted-once"
METRICS = ("gross_tokens", "cached_input_tokens", "uncached_input_tokens",
           "output_tokens", "active_ms", "repeated_reads",
           "new_validated_defects", "reopened_findings")


class QualificationError(ValueError):
    pass


def retained_file(root: Path, reference: dict) -> Path:
    if not isinstance(reference, dict):
        raise QualificationError("missing retained evidence reference")
    name = reference.get("path")
    if not isinstance(name, str) or not name:
        raise QualificationError("missing evidence path")
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise QualificationError("evidence must be a retained file inside the packet")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != reference.get("sha256"):
        raise QualificationError(f"stale evidence: {name}")
    return path


def metrics(value: dict) -> dict:
    if not isinstance(value, dict) or value.get("accounting_unit") != UNIT:
        raise QualificationError("accounting unit must preserve provider total tokens")
    for key in METRICS:
        if type(value.get(key)) is not int or value[key] < 0:
            raise QualificationError(f"missing or invalid metric: {key}")
    if value["gross_tokens"] != sum(value[key] for key in (
            "cached_input_tokens", "uncached_input_tokens", "output_tokens")):
        raise QualificationError("gross tokens disagree with input/output accounting")
    return {key: value[key] for key in METRICS}


def replay_side(root: Path, side: dict, required: set[str], seen_samples: set[str]) -> dict:
    if side.get("model") != "gpt-5.6-sol" or side.get("reasoning_effort") != "medium":
        raise QualificationError("replay is not Sol/medium")
    if side.get("status") != "completed" or side.get("independent") is not True:
        raise QualificationError("replay is unfinished or lacks independent review")
    retained_file(root, side.get("raw_review"))
    usage_path = retained_file(root, side.get("usage"))
    usage = json.loads(usage_path.read_text())
    sample_id = usage.get("sample_id")
    if not isinstance(sample_id, str) or not sample_id or sample_id in seen_samples:
        raise QualificationError("usage receipt needs a unique recorded sample identity")
    seen_samples.add(sample_id)
    measured = metrics(usage)
    if not required.issubset(set(side.get("responsibilities", []))):
        raise QualificationError("replay dropped required review responsibilities")
    return measured


def qualify(packet: dict, root: Path) -> dict:
    if packet.get("schema") != "archon.review-qualification.v1":
        raise QualificationError("unsupported qualification schema")
    pairs = packet.get("matched_repairs")
    if not isinstance(pairs, list) or len(pairs) != len(BLOCKERS):
        raise QualificationError("qualification requires exactly six historical defect fixtures")
    detected: set[str] = set()
    identities: set[str] = set()
    seen_samples: set[str] = set()
    totals = {side: dict.fromkeys(METRICS, 0) for side in ("historical", "risk_delta")}
    for pair in pairs:
        identity = pair.get("id")
        if not isinstance(identity, str) or not identity or identity in identities:
            raise QualificationError("repair IDs must be unique and nonempty")
        identities.add(identity)
        snapshot = retained_file(root, pair.get("snapshot"))
        snapshot_digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        required = set(pair.get("required_responsibilities", []))
        blockers = set(pair.get("known_blockers", []))
        if identity not in BLOCKERS or blockers != {identity} or not required:
            raise QualificationError("each historical fixture must map to its exact named blocker")
        for side in totals:
            evidence = pair.get(side, {})
            if evidence.get("snapshot_sha256") != snapshot_digest:
                raise QualificationError("reviews do not use the identical repair snapshot")
            measured = replay_side(root, evidence, required, seen_samples)
            for key, value in measured.items():
                totals[side][key] += value
        found = set(pair["risk_delta"].get("detected_blockers", []))
        if not blockers.issubset(found):
            raise QualificationError(f"missed replay blockers: {sorted(blockers - found)}")
        detected.update(blockers)
    if detected != BLOCKERS:
        raise QualificationError(f"missing replay fixtures: {sorted(BLOCKERS - detected)}")
    old = totals["historical"]["gross_tokens"]
    new = totals["risk_delta"]["gross_tokens"]
    if old <= 0 or new <= 0:
        raise QualificationError("both policies need actual nonzero review costs")
    if new * 100 > old * 40:
        raise QualificationError("repeated-review token reduction is below 60 percent")
    return {"schema": "archon.review-qualification-result.v1", "policy": "risk-delta-v1",
            "status": "qualified", "accounting_unit": UNIT, "totals": totals,
            "reduction_percent": (old - new) * 100 / old,
            "matched_repairs": sorted(identities), "detected_blockers": sorted(detected)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packet", type=Path)
    args = parser.parse_args()
    try:
        result = qualify(json.loads(args.packet.read_text()), args.packet.parent)
    except (QualificationError, OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
