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

    # Is the ticket's lookup key a QUANTITY the product reported, or a stable
    # identifier? That decides whether a second query shape is worth demanding.
    # A quantity is a measurement, and two ways of measuring it disagree the
    # moment a row is soft-deleted or counted under a slightly different
    # definition -- so one shape returning nothing says nothing about whether
    # the subject exists. An id, an email, a trace id is not a measurement:
    # there is exactly one sensible lookup, and demanding a second gets
    # satisfied with a junk query that measures nothing.
    try:
        plan = json.loads((args.artifacts / "evidence-plan.json").read_text(encoding="utf-8"))
        derived_key = any(i.get("kind") == "fingerprint" and i.get("resolution") == "given"
                          for i in plan.get("identifiers") or [])
    except Exception:
        derived_key = False  # a refinement, not a safety gate: never fail on its absence

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
            # sql_alternate is OPTIONAL in general and REQUIRED only when the
            # lookup key is a derived quantity (see derived_key above). It is
            # always validated when present.
            #
            # Illustration. Two runs of one ticket, minutes apart against the
            # same database, split on exactly this: one matched a reported
            # "1,171 rows / 24 notes / 672 actions" against the summary the
            # product had recorded and found the row at once; the other
            # recomputed those counts by aggregating child rows, matched
            # nothing, and concluded the occurrence was unattributable.
            alt = str(probe.get("sql_alternate") or "").strip().rstrip(";")
            if not alt and derived_key:
                fail(f"probe {probe['id']}: this ticket's lookup key is a reported quantity, "
                     "not a stable id, so the identification probe requires sql_alternate -- "
                     "the same subject measured the other way. probe-run spends it only if "
                     "sql returns no rows.")
            if not alt:
                continue
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
