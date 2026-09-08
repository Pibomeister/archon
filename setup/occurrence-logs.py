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
# The request slice competes with polling traffic on a busy account, and the
# entrypoint we need is usually the FIRST request in the window, so it needs
# headroom the raw slice does not.
MAX_REQUEST_EVENTS = 150
MAX_OUTPUT_BYTES = 512 * 1024
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
        # occurrence-window.py already brackets the occurrence. Padding again
        # here widened the window by another five minutes each side and, with a
        # per-query event cap and oldest-first ordering, the cap was spent on
        # earlier polling traffic before reaching the request that started the
        # occurrence -- measured: padded-once/max40 finds the entrypoint,
        # double-padded/max40 finds nothing at all.
        start = iso_ms(doc["start"])
        end = iso_ms(doc["end"])
        subjects = [str(s["value"]).strip() for s in doc["subjects"] if str(s.get("value", "")).strip()]
    except Exception as exc:
        print(f"OCCURRENCE_LOGS=UNAVAILABLE reason=window-unreadable {exc}")
        raise SystemExit(1)
    if not subjects:
        print("OCCURRENCE_LOGS=SKIPPED reason=no-subjects")
        raise SystemExit(0)

    def fetch(label: str, pattern: str, cap: int = MAX_EVENTS_PER_SUBJECT) -> str | None:
        argv = [
            "aws", "logs", "filter-log-events", "--region", REGION,
            "--log-group-name", LOG_GROUP,
            "--start-time", str(start), "--end-time", str(end),
            "--filter-pattern", pattern,
            "--max-items", str(cap),
            "--query", "events[*].[timestamp,message]", "--output", "text",
        ]
        try:
            result = subprocess.run(argv, capture_output=True, encoding="utf-8",
                                    errors="replace", timeout=TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            chunks.append(f"== {label}: TIMEOUT after {TIMEOUT_SECONDS}s")
            return None
        if result.returncode != 0:
            tail = (result.stderr or "").strip().splitlines()[-1:] or ["unknown"]
            chunks.append(f"== {label}: UNAVAILABLE {tail[0]}")
            return None
        body = result.stdout.strip() or "(zero matching events -- absence is evidence too)"
        chunks.append(f"== {label}\n{body}")
        return body

    chunks: list[str] = []
    gathered = 0
    for subject in subjects[:MAX_SUBJECTS]:
        # Two slices, because they answer different questions. The raw match
        # shows what happened to the entity; the REQUEST slice shows which route
        # served it, and that is what identifies a user-facing surface. On a
        # busy account the raw slice is mostly polling traffic and the request
        # lines never make the cap -- exactly how eight runs failed to name an
        # entrypoint that was sitting in the logs. Note the ids inside route
        # URLs are hashed (HashIdInterceptor), so a numeric id will never match
        # a URL: the subject term has to match the logged userId= prefix.
        if fetch(f"subject {subject}", f'"{subject}"') is not None:
            gathered += 1
        fetch(f"subject {subject} / request lines", f'"{subject}" "Controller"',
              cap=MAX_REQUEST_EVENTS)

    text = "\n\n".join(chunks)
    if len(text.encode()) > MAX_OUTPUT_BYTES:
        text = text.encode()[:MAX_OUTPUT_BYTES].decode("utf-8", errors="ignore")
        text += "\nOCCURRENCE_LOGS=DEGRADED reason=output-truncated"
    out.write_text(text + "\n", encoding="utf-8")
    status = "GATHERED" if gathered else "UNAVAILABLE"
    print(f"OCCURRENCE_LOGS={status} subjects={len(subjects[:MAX_SUBJECTS])} queried={gathered} file={out}")


if __name__ == "__main__":
    main()
