#!/usr/bin/env python3
"""Validate an impact.json envelope before a critic prompt may consume it.

Usage: impact_gate.py <impact.json> --round N
Prints: IMPACT_GATE=PASS round=N status=<GATHERED|UNAVAILABLE|SKIPPED> symbols=<n>
On malformed input: IMPACT_GATE=FAIL <reason>, exit 1.
"""
from __future__ import annotations

import json
import sys
from typing import Any, NoReturn

STATUSES = {"GATHERED", "UNAVAILABLE", "SKIPPED"}
RISKS = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}


def fail(reason: str) -> NoReturn:
    print(f"IMPACT_GATE=FAIL {reason}")
    raise SystemExit(1)


def main() -> None:
    args = sys.argv[1:]
    if len(args) < 3 or args[1] != "--round":
        fail("usage: impact_gate.py <impact.json> --round N")
    path, round_no = args[0], args[2]
    if not round_no.isdigit():
        fail(f"round is not an integer: [{round_no}]")

    try:
        obj: Any = json.loads(open(path, encoding="utf-8").read())
    except (OSError, ValueError) as exc:
        fail(f"cannot read/parse {path}: {exc}")

    if not isinstance(obj, dict):
        fail("top level is not an object")
    status = obj.get("status")
    if status not in STATUSES:
        fail(f"status out of enum: {status!r}")
    symbols = obj.get("symbols")
    if not isinstance(symbols, list):
        fail("symbols is not a list")
    if status in {"UNAVAILABLE", "SKIPPED"} and symbols:
        fail(f"{status} must carry an empty symbols array")

    for i, item in enumerate(symbols):
        if not isinstance(item, dict):
            fail(f"symbol {i} is not an object")
        name = item.get("name")
        file = item.get("file")
        callers = item.get("d1_callers")
        risk = item.get("risk")
        if not (isinstance(name, str) and name.strip()):
            fail(f"symbol {i} missing name")
        if not (isinstance(file, str) and file.strip()):
            fail(f"symbol {i} missing file")
        if not isinstance(callers, list):
            fail(f"symbol {i} d1_callers is not a list")
        if any(not isinstance(c, str) or not c.strip() for c in callers):
            fail(f"symbol {i} d1_callers has a blank entry")
        if risk not in RISKS:
            fail(f"symbol {i} risk out of enum: {risk!r}")

    print(f"IMPACT_GATE=PASS round={round_no} status={status} symbols={len(symbols)}")


if __name__ == "__main__":
    main()
