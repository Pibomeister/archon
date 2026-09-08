#!/usr/bin/env python3
"""Derive occurrence-window.json MECHANICALLY from an identification probe.

The RCA declares which columns of its occurrence-identification probe carry the
subject ids and the timestamps; this re-runs that one query with CSV output and
reads them. Nothing is asserted by a model: the window is what the matched row
says, and a probe matching zero or several rows has identified nothing.

Runs inside probe-run (a bash node) because the Codex sandbox denies model
nodes network access -- every network call in this lane lives in a bash node.

Usage: occurrence-window.py --artifacts <dir>   (PG* already exported)
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import subprocess
from pathlib import Path

TIMEOUT_SECONDS = 120
PAD = dt.timedelta(minutes=5)


def out(message: str) -> None:
    print(message)


def parse_ts(value: str) -> dt.datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    text = text.replace(" ", "T", 1)
    if text.endswith("+00"):
        text = text[:-3] + "+00:00"
    try:
        stamp = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.timezone.utc)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifacts", type=Path, required=True)
    args = ap.parse_args()
    ad = args.artifacts

    try:
        probes = json.loads((ad / "probe.json").read_text(encoding="utf-8")).get("probes") or []
    except Exception as exc:
        out(f"OCCURRENCE_WINDOW=SKIPPED reason=probe-json-unreadable {exc}")
        return
    chosen = next((p for p in probes if p.get("occurrence_subject_columns")
                   and p.get("occurrence_time_columns")), None)
    if chosen is None:
        out("OCCURRENCE_WINDOW=SKIPPED reason=no-identification-probe")
        return

    result = subprocess.run(
        ["psql", "-X", "-v", "ON_ERROR_STOP=1", "--csv", "-c", chosen["sql"]],
        capture_output=True, encoding="utf-8", errors="replace", timeout=TIMEOUT_SECONDS)
    if result.returncode != 0:
        out(f"OCCURRENCE_WINDOW=UNAVAILABLE reason=query-failed probe={chosen['id']}")
        return
    rows = list(csv.DictReader(io.StringIO(result.stdout)))
    if len(rows) != 1:
        # Zero means not found; several means the fingerprint is not distinguishing.
        # Either way nothing is identified, and guessing here would manufacture
        # attribution authority out of an ambiguous match.
        out(f"OCCURRENCE_WINDOW=SKIPPED reason=match-count={len(rows)} probe={chosen['id']}")
        return
    row = rows[0]

    subjects = []
    for column in chosen["occurrence_subject_columns"]:
        value = str(row.get(column, "")).strip()
        if value:
            subjects.append({"kind": column, "value": value})
    stamps = [s for s in (parse_ts(row.get(c, "")) for c in chosen["occurrence_time_columns"]) if s]
    if not subjects or not stamps:
        out(f"OCCURRENCE_WINDOW=SKIPPED reason=declared-columns-absent probe={chosen['id']}")
        return

    document = {
        "start": (min(stamps) - PAD).astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "end": (max(stamps) + PAD).astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "subjects": subjects,
        "evidence": f"{chosen['id']}: columns {','.join(chosen['occurrence_time_columns'])} of the single matched row",
    }
    (ad / "occurrence-window.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    out(f"OCCURRENCE_WINDOW=OK probe={chosen['id']} subjects={len(subjects)} "
        f"start={document['start']} end={document['end']}")


if __name__ == "__main__":
    main()
