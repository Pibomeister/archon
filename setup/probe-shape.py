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
        # null and [] both mean "not an identification probe" -- writing []
        # is how a model naturally says not-applicable, and occurrence-window.py
        # already treats it as absent, so rejecting it here made the validator
        # stricter than its own consumer.
        declared = [probe.get("occurrence_subject_columns") or [],
                    probe.get("occurrence_time_columns") or []]
        if any(declared):
            if not all(isinstance(d, list) and d and all(isinstance(c, str) and c.strip() for c in d)
                       for d in declared):
                fail(f"probe {probe['id']}: occurrence_subject_columns and "
                     "occurrence_time_columns must both be non-empty string lists "
                     "when either is set (use [] or omit both on a non-identification probe)")
            # An identification probe carries a SECOND, differently-shaped
            # query. A stated fingerprint can be matched two ways -- against
            # the summary the product recorded, or by recomputing the counts
            # from child rows -- and they are different measurements that
            # disagree the moment a row is soft-deleted or counted under a
            # slightly different definition. Two runs of this ticket proved it
            # decides everything: da65d1b3 read metadata->'summary' and found
            # the import instantly; 57309e15 recomputed from
            # user_import_actions, matched zero rows, and shipped
            # class-hardening for an occurrence sitting right there. Writing
            # the second shape costs nothing at authoring time, and probe-run
            # only spends it when the first returns no rows.
            alt = str(probe.get("sql_alternate") or "").strip().rstrip(";")
            if not alt:
                fail(f"probe {probe['id']}: an identification probe requires sql_alternate, "
                     "a differently-shaped query for the same subject (recorded summary vs "
                     "recomputed counts); probe-run spends it only if sql returns no rows")
            if " ".join(alt.split()).lower() == " ".join(sql.split()).lower():
                fail(f"probe {probe['id']}: sql_alternate is the same query as sql; "
                     "it must measure the subject a different way to be worth running")
            if not re.match(r"(?is)^(select|with)\b", alt):
                fail(f"probe {probe['id']}: sql_alternate must start with SELECT/WITH")
            if WRITE_KEYWORDS.search(alt) or ";" in alt:
                fail(f"probe {probe['id']}: sql_alternate write/DDL or multiple statements")
            alt_limits = [int(x) for x in re.findall(r"(?i)\blimit\s+(\d+)\b", alt)]
            if alt_limits and max(alt_limits) > 100:
                fail(f"probe {probe['id']}: sql_alternate LIMIT exceeds 100")
    print(f"PROBE_SHAPE=OK probes={len(probes)}")


if __name__ == "__main__":
    main()
