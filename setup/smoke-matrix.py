#!/usr/bin/env python3
"""Apply structured Playwright outcomes to an Archon smoke matrix."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any


FAILURE_CLASSES = {"product", "harness", "infrastructure", "unknown"}


def _messages(test: dict[str, Any]) -> list[str]:
    messages: list[str] = []
    for result in test.get("results", []):
        error = result.get("error") or {}
        if error.get("message"):
            messages.append(str(error["message"]))
        for item in result.get("errors", []):
            if item.get("message"):
                messages.append(str(item["message"]))
    return messages


def _failure_annotations(test: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for annotation in test.get("annotations", []):
        if annotation.get("type") == "failure_class":
            value = str(annotation.get("description", "")).strip().lower()
            if value in FAILURE_CLASSES:
                values.add(value)
    return values


def _classify_failure(annotations: set[str], message: str) -> str:
    # A broken locator cannot erase a visible wrong answer.
    # Once the test supplied structured evidence, never scrape marker strings
    # from its stack/source snippet: that snippet may quote both helper branches.
    if annotations:
        return "product" if "product" in annotations else "harness"
    if "SMOKE_PRODUCT_FAIL" in message:
        return "product"
    if "SMOKE_HARNESS_DRIFT" in message:
        return "harness"
    if "infrastructure" in annotations:
        return "infrastructure"
    return "unknown"


def report_outcomes(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    outcomes: dict[str, dict[str, Any]] = {}

    def walk(suite: dict[str, Any]) -> None:
        for spec in suite.get("specs", []):
            messages: list[str] = []
            annotations: set[str] = set()
            for test in spec.get("tests", []):
                messages.extend(_messages(test))
                annotations.update(_failure_annotations(test))
            message = "\n".join(dict.fromkeys(messages))
            ok = bool(spec.get("ok"))
            outcomes[str(spec.get("title", ""))] = {
                "ok": ok,
                "failure_class": None if ok else _classify_failure(annotations, message),
                "observed": None if ok else (message[:1000] or "Playwright failed without error text"),
            }
        for child in suite.get("suites", []):
            walk(child)

    for suite in report.get("suites", []):
        walk(suite)
    return outcomes


def apply_report(
    matrix: dict[str, Any],
    report: dict[str, Any] | None,
    report_error: str | None = None,
) -> dict[str, Any]:
    outcomes = report_outcomes(report or {})
    for row in matrix["rows"]:
        if row.get("kind") != "auto":
            continue
        outcome = outcomes.get(row.get("spec_title"))
        if outcome:
            row["result"] = "pass" if outcome["ok"] else "fail"
            row["failure_class"] = outcome["failure_class"]
            row["observed"] = outcome["observed"]
        else:
            row["result"] = "not-run"
            row["failure_class"] = "infrastructure"
            row["observed"] = report_error or "Playwright report contained no matching spec title"
    return matrix


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--error-file", type=Path)
    args = parser.parse_args()
    matrix = json.loads(args.matrix.read_text(encoding="utf-8"))
    fallback_error = None
    if args.error_file and args.error_file.exists():
        fallback_error = args.error_file.read_text(encoding="utf-8").strip()[:1000] or None
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        report_error = None
    except (OSError, json.JSONDecodeError) as exc:
        report = None
        report_error = fallback_error or f"Playwright report unavailable or malformed: {type(exc).__name__}"
    apply_report(matrix, report, report_error)
    temporary = args.matrix.with_suffix(args.matrix.suffix + ".tmp")
    temporary.write_text(json.dumps(matrix, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, args.matrix)
    counts: dict[str, int] = {}
    for row in matrix["rows"]:
        if row.get("kind") == "auto":
            result = row.get("result", "not-run")
            counts[result] = counts.get(result, 0) + 1
    print("SMOKE_AUTO rows:", counts)


if __name__ == "__main__":
    main()
