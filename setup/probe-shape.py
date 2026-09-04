#!/usr/bin/env python3
"""probe.json shape/safety validator: SELECT-only, single statement, bounded.

Factored out of rca-shape.sh because probe-run now executes these queries
BEFORE rca-gate runs (so probe results exist as occurrence evidence at
decision time). Two callers, one contract: a second inline copy would drift,
and the drift would be an unvalidated statement reaching production.

Usage: probe-shape.py <artifacts-dir> [--token PREFIX]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

WRITE_KEYWORDS = re.compile(
    r"(?i)\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|vacuum)\b"
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("artifacts", type=Path)
    ap.add_argument("--token", default="PROBE_SHAPE=FAIL")
    args = ap.parse_args()

    def fail(message: str) -> None:
        print(f"{args.token} {message}")
        raise SystemExit(1)

    try:
        document = json.loads((args.artifacts / "probe.json").read_text(encoding="utf-8"))
    except Exception as exc:
        fail(f"probe.json missing or malformed: {exc}")

    probes = document.get("probes")
    if not (isinstance(probes, list) and len(probes) <= 3):
        fail("probe.json probes must be a list of at most 3")
    if not (probes or document.get("none_reason")):
        fail("probe.json: empty probes requires none_reason")
    for probe in probes:
        if not (probe.get("id") and probe.get("question") and probe.get("sql")):
            fail("probe entry missing id/question/sql")
        sql = probe["sql"].strip().rstrip(";")
        if not re.match(r"(?is)^(select|with)\b", sql):
            fail(f"probe {probe['id']}: must start with SELECT/WITH")
        if WRITE_KEYWORDS.search(sql):
            fail(f"probe {probe['id']}: write/DDL keyword rejected")
        if ";" in sql:
            fail(f"probe {probe['id']}: single statement only")
        limits = [int(x) for x in re.findall(r"(?i)\blimit\s+(\d+)\b", sql)]
        aggregate_only = bool(re.search(r"(?i)\b(count|sum|avg|min|max)\s*\(", sql))
        if not aggregate_only and not limits:
            fail(f"probe {probe['id']}: row-returning query requires LIMIT <= 100")
        if limits and max(limits) > 100:
            fail(f"probe {probe['id']}: LIMIT exceeds 100")
        # Optional, and only meaningful together: probe-run reads these columns
        # off the matched row to derive the occurrence window mechanically.
        declared = [probe.get("occurrence_subject_columns"), probe.get("occurrence_time_columns")]
        if any(d is not None for d in declared):
            if not all(isinstance(d, list) and d and all(isinstance(c, str) and c.strip() for c in d)
                       for d in declared):
                fail(f"probe {probe['id']}: occurrence_subject_columns and "
                     "occurrence_time_columns must both be non-empty string lists")
    print(f"PROBE_SHAPE=OK probes={len(probes)}")


if __name__ == "__main__":
    main()
