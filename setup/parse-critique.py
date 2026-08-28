#!/usr/bin/env python3
"""Validate a critic finding envelope (plan-critic / rca-critic) and print the
converge-loop's typed summary line. A finding below confidence 50 is dropped
before counting; the printed per-kind counts are of findings at or above 75
(the "blocking" tier that sizes plan-revise's/rca-revise's obligation).
Usage: parse-critique.py <critique.json> --round N
Prints: CRITIQUE round=N verdict=<ACCEPT|REVISE|REJECT> scope=<n> regression=<n> gap=<n> verifiability=<n>
On malformed input: CRITIC_GATE=FAIL <reason>, exit 1. Nothing else on success."""
import json
import sys
from typing import NoReturn

VERDICTS = {"ACCEPT", "REVISE", "REJECT"}
SEVERITIES = {"P0", "P1", "P2"}
CONFIDENCES = {50, 75, 100}
SOURCES = {"spec", "plan", "impact", "repo"}


def fail(reason) -> NoReturn:
    sys.exit(f"CRITIC_GATE=FAIL {reason}")


def main():
    args = sys.argv[1:]
    if len(args) < 3 or args[1] != "--round":
        fail("usage: parse-critique.py <critique.json> --round N")
    path, round_no = args[0], args[2]

    try:
        obj = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError) as e:
        fail(f"cannot read/parse {path}: {e}")

    if not isinstance(obj, dict):
        fail("top level is not an object")
    verdict = obj.get("verdict")
    if verdict not in VERDICTS:
        fail(f"verdict out of enum: {verdict!r}")
    findings = obj.get("findings")
    if not isinstance(findings, list):
        fail("findings is not a list")

    kept = []
    for i, f in enumerate(findings):
        if not isinstance(f, dict):
            fail(f"finding {i} is not an object")
        kind = f.get("kind")
        severity = f.get("severity")
        confidence = f.get("confidence")
        section = f.get("section")
        evidence = f.get("evidence")
        recommendation = f.get("recommendation")
        if not (isinstance(kind, str) and kind.strip()):
            fail(f"finding {i} missing kind")
        if severity not in SEVERITIES:
            fail(f"finding {i} severity out of enum: {severity!r}")
        if confidence not in CONFIDENCES:
            fail(f"finding {i} confidence out of enum: {confidence!r}")
        if not (isinstance(section, str) and section.strip()):
            fail(f"finding {i} missing section")
        if not (isinstance(evidence, list) and evidence):
            fail(f"finding {i} missing evidence")
        for j, e in enumerate(evidence):
            if (
                not isinstance(e, dict)
                or e.get("source") not in SOURCES
                or not (isinstance(e.get("quote"), str) and e["quote"].strip())
            ):
                fail(f"finding {i} evidence {j} malformed")
        if not (isinstance(recommendation, str) and recommendation.strip()):
            fail(f"finding {i} missing recommendation")
        if confidence >= 50:
            kept.append(f)

    blocking = [f for f in kept if f["confidence"] >= 75]
    counts = {"scope": 0, "regression": 0, "gap": 0, "verifiability": 0}
    for f in blocking:
        if f["kind"] in counts:
            counts[f["kind"]] += 1

    print(
        f"CRITIQUE round={round_no} verdict={verdict} "
        f"scope={counts['scope']} regression={counts['regression']} gap={counts['gap']} "
        f"verifiability={counts['verifiability']}"
    )


if __name__ == "__main__":
    main()
