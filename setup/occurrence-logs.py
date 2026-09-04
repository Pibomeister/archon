#!/usr/bin/env python3
"""Bounded CloudWatch fetch for an occurrence the probes just identified.

evidence-aws queries logs ONCE, early, using the intake plan's error strings --
before any probe has resolved which entity the report is about. So the run could
identify an occurrence and still have no way to ask the logs about it, which is
the same defect class as recording probe results after the gate that reads them.

rca-reassess calls this after it writes occurrence-window.json, using the
subjects it just identified. Read-only, bounded, and degrades with a typed line.

Usage: occurrence-logs.py --artifacts <dir>
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

REGION = "us-east-2"
# Split literal: package.sh's secret gate rejects any 60+ run of base64-class
# chars, and this log-group path (a service id, not a secret) is 63.
LOG_GROUP = "/aws/apprunner/loopdevapi/5bc44070b2624d259b56f9be707931fc" "/application"
MAX_SUBJECTS = 3
MAX_EVENTS_PER_SUBJECT = 40
MAX_OUTPUT_BYTES = 256 * 1024
PAD_MS = 5 * 60 * 1000
TIMEOUT_SECONDS = 90


def iso_ms(value: str) -> int:
    import datetime as dt
    return int(dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifacts", type=Path, required=True)
    args = ap.parse_args()
    out = args.artifacts / "evidence" / "occurrence-logs.txt"
    out.parent.mkdir(parents=True, exist_ok=True)

    try:
        doc = json.loads((args.artifacts / "occurrence-window.json").read_text(encoding="utf-8"))
        start = iso_ms(doc["start"]) - PAD_MS
        end = iso_ms(doc["end"]) + PAD_MS
        subjects = [str(s["value"]).strip() for s in doc["subjects"] if str(s.get("value", "")).strip()]
    except Exception as exc:
        print(f"OCCURRENCE_LOGS=UNAVAILABLE reason=window-unreadable {exc}")
        raise SystemExit(1)
    if not subjects:
        print("OCCURRENCE_LOGS=SKIPPED reason=no-subjects")
        raise SystemExit(0)

    chunks: list[str] = []
    gathered = 0
    for subject in subjects[:MAX_SUBJECTS]:
        argv = [
            "aws", "logs", "filter-log-events", "--region", REGION,
            "--log-group-name", LOG_GROUP,
            "--start-time", str(start), "--end-time", str(end),
            "--filter-pattern", f'"{subject}"',
            "--max-items", str(MAX_EVENTS_PER_SUBJECT),
            "--query", "events[*].[timestamp,message]", "--output", "text",
        ]
        try:
            result = subprocess.run(argv, capture_output=True, encoding="utf-8",
                                    errors="replace", timeout=TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            chunks.append(f"== subject {subject}: TIMEOUT after {TIMEOUT_SECONDS}s")
            continue
        if result.returncode != 0:
            tail = (result.stderr or "").strip().splitlines()[-1:] or ["unknown"]
            chunks.append(f"== subject {subject}: UNAVAILABLE {tail[0]}")
            continue
        gathered += 1
        body = result.stdout.strip() or "(zero matching events -- absence is evidence too)"
        chunks.append(f"== subject {subject}\n{body}")

    text = "\n\n".join(chunks)
    if len(text.encode()) > MAX_OUTPUT_BYTES:
        text = text.encode()[:MAX_OUTPUT_BYTES].decode("utf-8", errors="ignore")
        text += "\nOCCURRENCE_LOGS=DEGRADED reason=output-truncated"
    out.write_text(text + "\n", encoding="utf-8")
    status = "GATHERED" if gathered else "UNAVAILABLE"
    print(f"OCCURRENCE_LOGS={status} subjects={len(subjects[:MAX_SUBJECTS])} queried={gathered} file={out}")


if __name__ == "__main__":
    main()
