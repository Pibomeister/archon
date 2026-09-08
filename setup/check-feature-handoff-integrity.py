#!/usr/bin/env python3
"""Validate the controller-written web-lane handoff integrity receipt."""
import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("PREMISE_STRIP=FAIL usage: check-feature-handoff-integrity.py <handoff-integrity.json>")
        return 1
    path = Path(sys.argv[1])
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"PREMISE_STRIP=FAIL handoff-integrity unreadable: {exc}")
        return 1
    checks = result.get("checks")
    if not isinstance(checks, dict) or not checks:
        print("PREMISE_STRIP=FAIL handoff integrity has no checks")
        return 1
    failed = sorted(k for k, v in checks.items() if v is not True)
    if failed:
        print("PREMISE_STRIP=FAIL handoff checks failed " + ",".join(failed))
        return 1
    print("HANDOFF_INTEGRITY=PASS checks=" + str(len(checks)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
