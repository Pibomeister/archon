#!/usr/bin/env python3
"""Validate feature-derived browser evidence from the UAT node."""
import json
import sys
from pathlib import Path


def load_object(path: Path, label: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"UAT_GATE=FAIL {label} unreadable: {exc}")
    if not isinstance(data, dict):
        raise SystemExit(f"UAT_GATE=FAIL {label} must be an object")
    return data


def entry_ids(value, label: str) -> set[str]:
    if not isinstance(value, list):
        raise SystemExit(f"UAT_GATE=FAIL {label} must be an array")
    ids = set()
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("criterion"), str):
            raise SystemExit(f"UAT_GATE=FAIL {label} entries need criterion ids")
        ids.add(item["criterion"])
    return ids


def main() -> int:
    if len(sys.argv) != 4:
        print("UAT_GATE=FAIL usage: check-browser-evidence.py <requirements.json> <uat-result.json> <screenshot>")
        return 1
    req_path, result_path, screenshot = map(Path, sys.argv[1:4])
    req = load_object(req_path, "browser-evidence")
    result = load_object(result_path, "uat-result")
    required = req.get("required")
    if not isinstance(required, list) or not required:
        print("UAT_GATE=FAIL browser-evidence has no required criteria")
        return 1
    required_ids = set()
    for item in required:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            print("UAT_GATE=FAIL browser-evidence required entries need ids")
            return 1
        required_ids.add(item["id"])
    failed_ids = entry_ids(result.get("failed", []), "failed")
    if failed_ids:
        print("UAT_GATE=FAIL failed criteria: " + ",".join(sorted(failed_ids)))
        return 1
    passed_ids = entry_ids(result.get("passed", []), "passed")
    missing = sorted(required_ids - passed_ids)
    if missing:
        print("UAT_GATE=FAIL missing required criteria: " + ",".join(missing))
        return 1
    evidence = result.get("evidence", [])
    if not isinstance(evidence, list) or str(screenshot) not in {str(v) for v in evidence}:
        print("UAT_GATE=FAIL screenshot not listed in evidence")
        return 1
    if not screenshot.is_file() or screenshot.stat().st_size <= 0:
        print("UAT_GATE=FAIL no screenshot")
        return 1
    print("UAT_PASSED=" + str(len(passed_ids)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
