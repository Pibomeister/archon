#!/usr/bin/env python3
"""Build and validate bounded recent-PR context for bugfix RCA."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

DECISIONS = {"solves", "partial", "unrelated", "superseded"}
STOP = {
    "the", "and", "for", "with", "from", "when", "this", "that", "fails", "failed", "should", "people",
    "bug", "bugs", "burke", "caroline", "channel", "created", "creator", "dell", "downstream", "eng", "file",
    "goodword", "issue", "linear", "nodes", "normalized", "not", "only", "patrick", "raw", "read", "report",
    "reported", "slack", "symptom", "tracked", "utc", "where", "who",
}
ALIASES = {
    "company": {"affiliation", "employer", "organization", "corporate"},
    "name": {"named", "identity", "literal"},
    "search": {"retrieval", "planner", "facet", "ranking"},
    "connection": {"network", "colleague", "coworker"},
}


def fail(message: str) -> None:
    print(f"CHANGE_CONTEXT=FAIL {message}")
    raise SystemExit(1)


def tokens(text: str) -> set[str]:
    return {word.lower() for word in re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", text) if word.lower() not in STOP}


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def build(artifacts: Path) -> None:
    report = (artifacts / "bug-report-normalized.md").read_text(encoding="utf-8", errors="replace")
    report = report.split("<trace-context>", 1)[0]
    report_terms = tokens(report[:6000])
    for term in list(report_terms):
        report_terms.update(ALIASES.get(term, set()))
    evidence = artifacts / "evidence"
    candidates = []
    for repo in ("api", "web-app"):
        for state, filename in (("open", f"open-prs-{repo}.json"), ("merged", f"merged-prs-{repo}.json")):
            rows = read_json(evidence / filename, [])
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("number"), int):
                    continue
                title_terms = tokens(str(row.get("title", "")) + " " + str(row.get("headRefName", "")))
                files = [item.get("path") for item in row.get("files", []) if isinstance(item, dict) and item.get("path")]
                file_terms = tokens(" ".join(files))
                overlap = sorted(report_terms & (title_terms | file_terms))
                score = len(report_terms & title_terms) * 3 + len(report_terms & file_terms)
                if score < 1:
                    continue
                candidates.append({
                    "id": f"{repo}#{row['number']}", "repo": repo, "number": row["number"],
                    "state": state, "title": row.get("title"), "url": row.get("url"),
                    "head_ref": row.get("headRefName"), "merged_at": row.get("mergedAt"),
                    "updated_at": row.get("updatedAt"), "files": files[:40],
                    "matched_terms": overlap, "score": score,
                })
    candidates.sort(key=lambda row: (row["score"], row.get("merged_at") or row.get("updated_at") or ""), reverse=True)
    payload = {"schema_version": 1, "report_terms": sorted(report_terms), "candidates": candidates[:15]}
    (artifacts / "change-context.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"CHANGE_CONTEXT=OK candidates={len(payload['candidates'])}")


def validate(artifacts: Path) -> None:
    context = read_json(artifacts / "change-context.json", None)
    assessment = read_json(artifacts / "change-context-assessment.json", None)
    if not isinstance(context, dict) or not isinstance(assessment, dict):
        fail("context or assessment missing")
    expected = {row["id"] for row in context.get("candidates", [])}
    rows = assessment.get("assessments")
    if not isinstance(rows, list):
        fail("assessments must be a list")
    actual = {row.get("id") for row in rows if isinstance(row, dict)}
    if actual != expected or len(rows) != len(expected):
        fail(f"candidate coverage mismatch missing={sorted(expected-actual)} extra={sorted(actual-expected)}")
    for row in rows:
        if row.get("decision") not in DECISIONS or not str(row.get("evidence", "")).strip() or not str(row.get("reason", "")).strip():
            fail(f"invalid assessment for {row.get('id')}")
    print(f"CHANGE_CONTEXT_ASSESSMENT=PASS candidates={len(expected)}")


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("action", choices=("build", "validate")); parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args(); artifacts = args.artifacts.resolve()
    if args.action == "build": build(artifacts)
    else: validate(artifacts)


if __name__ == "__main__": main()
