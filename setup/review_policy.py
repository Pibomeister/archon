#!/usr/bin/env python3
"""Pure helpers for the opt-in Archon risk-delta-v1 review policy.

The controller and CLI own persistence, locking, and dispatch.  This module owns
small JSON-shaped decisions that can be checkpointed by those callers without
starting agents or mutating run state.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any


POLICY = "risk-delta-v1"
SCHEMA_VERSION = 1
REQUIRED_MODEL = "gpt-5.6-sol"
REQUIRED_REASONING_EFFORT = "medium"
MAX_CONCURRENT_REVIEWERS = 3
INITIAL_RESPONSIBILITIES = (
    "correctness_contracts",
    "testing_maintainability_standards",
)
DELTA_RESPONSIBILITY = "independent_repair_review"
CALLER_READER_TRACE = "caller_reader_trace"
RISK_RESPONSIBILITIES = {
    "authorization": "security",
    "public_access": "security",
    "sensitive_data": "security",
    "transactions": "data_reliability",
    "concurrency": "data_reliability",
    "migrations": "data_reliability",
    "recovery": "data_reliability",
    "query_volume": "performance",
    "cardinality": "performance",
    "resource_bounds": "performance",
    "cli_behavior": "cli",
    "automation_contracts": "cli",
}
MANDATORY_SEVERITIES = {"P0", "P1", "P2"}
SEVERITIES = MANDATORY_SEVERITIES | {"P3"}
SHA256_RE = re.compile(r"[0-9a-f]{64}", re.I)
COMMIT_RE = re.compile(r"[0-9a-f]{40}", re.I)


class ReviewPolicyError(ValueError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _require_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReviewPolicyError(f"{label} must be an object")
    return value


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewPolicyError(f"{label} is required")
    return value.strip()


def _require_sha(value: Any, label: str) -> str:
    text = _require_text(value, label).lower()
    if not SHA256_RE.fullmatch(text):
        raise ReviewPolicyError(f"{label} must be a sha256 digest")
    return text


def _require_commit(value: Any, label: str) -> str:
    text = _require_text(value, label).lower()
    if not COMMIT_RE.fullmatch(text):
        raise ReviewPolicyError(f"{label} must be a 40-character commit")
    return text


def _unique_texts(values: Any, label: str) -> list[str]:
    if not isinstance(values, (list, tuple)):
        raise ReviewPolicyError(f"{label} must be a list")
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = _require_text(value, label)
        if text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _validate_policy_record(record: dict[str, Any], label: str) -> None:
    if record.get("policy") != POLICY:
        raise ReviewPolicyError(f"{label} must use policy {POLICY}")
    if record.get("schema_version") != SCHEMA_VERSION:
        raise ReviewPolicyError(f"{label} must use schema version {SCHEMA_VERSION}")


def exact_candidate(repo: str, base: str, head: str) -> dict[str, str]:
    return {
        "repo": _require_text(repo, "candidate repo"),
        "base": _require_commit(base, "candidate base"),
        "head": _require_commit(head, "candidate head"),
    }


def _candidate_from(value: Any) -> dict[str, str]:
    candidate = _require_dict(value, "candidate")
    return exact_candidate(candidate.get("repo"), candidate.get("base"), candidate.get("head"))


def _responsibilities_for(change: dict[str, Any]) -> list[str]:
    mode = change.get("mode", "delta")
    if mode not in {"initial", "delta"}:
        raise ReviewPolicyError("change mode must be initial or delta")
    required = list(INITIAL_RESPONSIBILITIES if mode == "initial" else (DELTA_RESPONSIBILITY,))
    for risk in _unique_texts(change.get("risk_areas", []), "risk areas"):
        responsibility = RISK_RESPONSIBILITIES.get(risk)
        if responsibility is None:
            raise ReviewPolicyError(f"unknown risk area: {risk}")
        required.append(responsibility)
    if change.get("cross_store"):
        required.append("data_reliability")
    if change.get("impact_tooling_missing"):
        required.append(CALLER_READER_TRACE)
    return _dedupe(required)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _review_scope(change: dict[str, Any]) -> str:
    if change.get("mode", "delta") == "initial":
        return "full-candidate"
    if change.get("baseline_invalid") or change.get("impact_unbounded"):
        return "full-candidate"
    if any(change.get(flag) for flag in (
        "invariant_changed",
        "public_contract_changed",
        "permission_boundary_changed",
        "shared_dependency_changed",
        "subsystem_changed",
    )):
        return "affected-subsystem"
    return "bounded-delta"


def _batches(responsibilities: list[str]) -> list[list[str]]:
    return [
        responsibilities[index:index + MAX_CONCURRENT_REVIEWERS]
        for index in range(0, len(responsibilities), MAX_CONCURRENT_REVIEWERS)
    ]


def required_review_plan(change: dict[str, Any]) -> dict[str, Any]:
    """Return the required cumulative review coverage for a candidate change."""
    change = _require_dict(change, "change")
    candidate = _candidate_from(change.get("candidate"))
    responsibilities = _responsibilities_for(change)
    return {
        "policy": POLICY,
        "schema_version": SCHEMA_VERSION,
        "candidate": candidate,
        "scope": _review_scope(change),
        "required_responsibilities": responsibilities,
        "review_batches": _batches(responsibilities),
        "requires_independent_review": bool(change.get("production_changed", True)),
    }


def validate_coverage_record(
    record: dict[str, Any],
    candidate: dict[str, Any] | None = None,
    author_id: str | None = None,
    expected: dict[str, Any] | None = None,
) -> set[str]:
    record = _require_dict(record, "coverage record")
    _validate_policy_record(record, "coverage record")
    expected_candidate = _candidate_from(candidate) if candidate is not None else None
    actual = _candidate_from(record.get("candidate"))
    if expected_candidate is not None and actual != expected_candidate:
        raise ReviewPolicyError("coverage record does not match the exact candidate")
    if record.get("status") != "complete":
        raise ReviewPolicyError("coverage record is not complete")
    source_digest = _require_sha(record.get("source_digest"), "coverage source digest")
    context_digest = _require_sha(record.get("context_digest"), "coverage context digest")
    reviewer = _require_text(record.get("reviewer_id"), "coverage reviewer id")
    if author_id is not None and reviewer == author_id:
        raise ReviewPolicyError("production changes require a reviewer different from the author")
    model = _require_text(record.get("model"), "coverage model")
    reasoning_effort = _require_text(record.get("reasoning_effort"), "coverage reasoning effort")
    if model != REQUIRED_MODEL:
        raise ReviewPolicyError(f"coverage model must be {REQUIRED_MODEL}")
    if reasoning_effort != REQUIRED_REASONING_EFFORT:
        raise ReviewPolicyError(f"coverage reasoning effort must be {REQUIRED_REASONING_EFFORT}")
    if expected:
        if expected.get("source_digest") != source_digest:
            raise ReviewPolicyError("coverage source digest does not match expected captured source")
        if expected.get("context_digest") != context_digest:
            raise ReviewPolicyError("coverage context digest does not match expected bounded context")
    responsibilities = set(_unique_texts(record.get("responsibilities"), "coverage responsibilities"))
    if not responsibilities:
        raise ReviewPolicyError("coverage record must include responsibilities")
    return responsibilities


def is_mandatory_finding(finding: dict[str, Any]) -> bool:
    if finding.get("mandatory") is True:
        return True
    return finding.get("severity") in MANDATORY_SEVERITIES


def validate_findings_ledger(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(entries, list):
        raise ReviewPolicyError("findings ledger must be a list")
    seen: set[str] = set()
    unresolved: list[dict[str, Any]] = []
    for entry in entries:
        entry = _require_dict(entry, "finding")
        finding_id = _require_text(entry.get("finding_id"), "finding id")
        if finding_id in seen:
            raise ReviewPolicyError(f"duplicate finding id: {finding_id}")
        seen.add(finding_id)
        _require_text(entry.get("violated_invariant"), "violated invariant")
        if not _unique_texts(entry.get("affected_paths"), "affected paths"):
            raise ReviewPolicyError(f"finding {finding_id} must name affected paths")
        severity = _require_text(entry.get("severity"), "finding severity")
        if severity not in SEVERITIES:
            raise ReviewPolicyError(f"finding {finding_id} has invalid severity")
        _require_text(entry.get("evidence"), "finding evidence")
        _require_text(entry.get("owning_repository"), "owning repository")
        status = entry.get("status", "open")
        if status not in {"open", "closed", "advisory"}:
            raise ReviewPolicyError(f"finding {finding_id} has invalid status")
        if status == "closed":
            _require_commit(entry.get("repair_commit"), f"finding {finding_id} repair commit")
            regression = _require_dict(entry.get("regression_evidence"), f"finding {finding_id} regression evidence")
            if not regression:
                raise ReviewPolicyError(f"finding {finding_id} regression evidence is empty")
            repair_author = _require_text(entry.get("repair_author_id"), f"finding {finding_id} repair author")
            closure = _require_dict(entry.get("independent_closure"), f"finding {finding_id} independent closure")
            if closure.get("status") != "closed":
                raise ReviewPolicyError(f"finding {finding_id} closure must be closed")
            reviewer = _require_text(closure.get("reviewer_id"), f"finding {finding_id} closure reviewer")
            if reviewer == repair_author:
                raise ReviewPolicyError(f"finding {finding_id} closure is not independent")
            if _require_commit(closure.get("repair_commit"), f"finding {finding_id} closure repair commit") != entry["repair_commit"]:
                raise ReviewPolicyError(f"finding {finding_id} closure is not bound to the repair commit")
            _validate_regression_evidence(finding_id, regression)
        elif is_mandatory_finding(entry):
            unresolved.append(entry)
    return unresolved


def _validate_regression_evidence(finding_id: str, regression: dict[str, Any]) -> None:
    _require_text(regression.get("failure"), f"finding {finding_id} regression failure")
    if regression.get("executed") is not True:
        raise ReviewPolicyError(f"finding {finding_id} regression must be executed")
    receipt = _require_dict(regression.get("receipt"), f"finding {finding_id} regression receipt")
    _require_text(receipt.get("command"), f"finding {finding_id} regression receipt command")
    if receipt.get("exit_status") != 0:
        raise ReviewPolicyError(f"finding {finding_id} regression receipt must be successful")


def validate_normalized_blockers(
    source_response: dict[str, Any],
    normalized: dict[str, Any],
    format_retry_attempts: int,
) -> list[dict[str, Any]]:
    """Validate normalized blocker records while retaining malformed originals."""
    source_response = _require_dict(source_response, "source response")
    normalized = _require_dict(normalized, "normalized response")
    if not isinstance(format_retry_attempts, int) or format_retry_attempts < 0:
        raise ReviewPolicyError("format retry attempts must be a non-negative integer")
    raw_output = _require_text(source_response.get("raw_output"), "source raw output")
    if format_retry_attempts > 1:
        raise ReviewPolicyError("format-only repair retry limit exceeded")
    if source_response.get("malformed") and format_retry_attempts == 0:
        raise ReviewPolicyError("malformed reviewer output requires one format-only repair attempt")
    source_blockers = _unique_texts(source_response.get("blocker_ids"), "source blocker ids")
    if not source_blockers and re.search(r"\bP[012]\b", raw_output):
        raise ReviewPolicyError("source blocker inventory is empty despite blocker severity markers")
    findings = normalized.get("findings")
    if not isinstance(findings, list):
        raise ReviewPolicyError("normalized findings must be a list")
    accounted: set[str] = set()
    normalized_entries: list[dict[str, Any]] = []
    for item in findings:
        item = _require_dict(item, "normalized finding")
        _require_text(item.get("finding_id"), "normalized finding id")
        _require_text(item.get("violated_invariant"), "normalized violated invariant")
        if not _unique_texts(item.get("affected_paths"), "normalized affected paths"):
            raise ReviewPolicyError("normalized finding must name affected paths")
        severity = _require_text(item.get("severity"), "normalized severity")
        if severity not in SEVERITIES:
            raise ReviewPolicyError("normalized finding has invalid severity")
        _require_text(item.get("evidence"), "normalized evidence")
        _require_text(item.get("owning_repository"), "normalized owning repository")
        status = item.get("status", "open")
        if status not in {"open", "closed", "advisory"}:
            raise ReviewPolicyError("normalized finding has invalid status")
        normalized_entries.append(item)
        ids = item.get("source_blocker_ids", item.get("source_blocker_id", []))
        if isinstance(ids, str):
            ids = [ids]
        mapped = _unique_texts(ids, "normalized source blocker ids")
        if mapped and status == "advisory":
            raise ReviewPolicyError("normalized blocker finding cannot be advisory")
        accounted.update(mapped)
    missing = [blocker for blocker in source_blockers if blocker not in accounted]
    if missing:
        raise ReviewPolicyError(f"normalized result dropped source blockers: {', '.join(missing)}")
    if raw_output and normalized.get("source_raw_output_sha256") != hashlib.sha256(raw_output.encode("utf-8")).hexdigest():
        raise ReviewPolicyError("normalized result is not bound to the retained raw reviewer output")
    return normalized_entries


def validate_verification_receipt(
    receipt: dict[str, Any],
    candidate: dict[str, Any],
    expected: dict[str, Any],
) -> dict[str, Any]:
    receipt = _require_dict(receipt, "verification receipt")
    _validate_policy_record(receipt, "verification receipt")
    if _candidate_from(receipt.get("candidate")) != _candidate_from(candidate):
        raise ReviewPolicyError("verification receipt does not match the exact candidate")
    required_expected = (
        "command",
        "source_tree_digest",
        "dependencies_digest",
        "configuration_digest",
        "environment_fingerprint",
        "retained_output_digest",
    )
    for key in required_expected:
        if key not in expected:
            raise ReviewPolicyError(f"verification receipt expected {key} is required")
    for key in ("command", "source_tree_digest", "dependencies_digest", "configuration_digest", "environment_fingerprint", "retained_output_digest"):
        if key == "command":
            _require_text(receipt.get(key), key)
            if receipt.get(key) != expected.get(key):
                raise ReviewPolicyError("verification receipt command is stale")
        else:
            _require_sha(receipt.get(key), key)
            _require_sha(expected.get(key), f"expected {key}")
            if receipt.get(key) != expected.get(key):
                raise ReviewPolicyError(f"verification receipt {key} is stale")
    if not isinstance(receipt.get("exit_status"), int):
        raise ReviewPolicyError("verification receipt exit status is required")
    if expected.get("require_success") and receipt.get("exit_status") != 0:
        raise ReviewPolicyError("verification receipt did not pass")
    return receipt


def _coverage_expectation_for(plan: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    coverage_id = _require_text(record.get("coverage_id"), "coverage id")
    expectations = plan.get("coverage_expectations")
    if not isinstance(expectations, dict) or coverage_id not in expectations:
        raise ReviewPolicyError(f"missing coverage expectation for {coverage_id}")
    expected = _require_dict(expectations[coverage_id], f"coverage expectation {coverage_id}")
    return {
        "source_digest": _require_sha(expected.get("source_digest"), f"coverage expectation {coverage_id} source digest"),
        "context_digest": _require_sha(expected.get("context_digest"), f"coverage expectation {coverage_id} context digest"),
    }


def _has_contiguous_coverage(segments: list[dict[str, str]], start: str, finish: str) -> bool:
    frontier = {start}
    visited = set(frontier)
    while frontier:
        current = frontier.pop()
        if current == finish:
            return True
        for segment in segments:
            if segment["base"] == current and segment["head"] not in visited:
                visited.add(segment["head"])
                frontier.add(segment["head"])
    return False


def validate_coverage_chain(coverage_records: list[dict[str, Any]], candidate: dict[str, Any]) -> None:
    expected_candidate = _candidate_from(candidate)
    segments: list[dict[str, str]] = []
    for record in coverage_records:
        segment = _candidate_from(_require_dict(record, "coverage record").get("candidate"))
        if segment["repo"] != expected_candidate["repo"]:
            raise ReviewPolicyError("coverage record repository does not match the final candidate")
        segments.append(segment)
    if not _has_contiguous_coverage(segments, expected_candidate["base"], expected_candidate["head"]):
        raise ReviewPolicyError("coverage records do not form a contiguous baseline-to-head chain")


def _records_by_coverage_id(coverage_records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in coverage_records:
        coverage_id = _require_text(_require_dict(record, "coverage record").get("coverage_id"), "coverage id")
        grouped.setdefault(coverage_id, []).append(record)
    return grouped


def _required_segment_requirements(plan: dict[str, Any]) -> list[dict[str, Any]]:
    segments = plan.get("segment_requirements")
    if not isinstance(segments, list) or not segments:
        raise ReviewPolicyError("review plan must declare segment requirements")
    return [_require_dict(segment, "segment requirement") for segment in segments]


def _segment_expectation(segment: dict[str, Any]) -> dict[str, str]:
    return {
        "source_digest": _require_sha(segment.get("source_digest"), "segment source digest"),
        "context_digest": _require_sha(segment.get("context_digest"), "segment context digest"),
    }


def _segment_candidate(segment: dict[str, Any]) -> dict[str, str]:
    return _candidate_from(segment.get("candidate"))


def _segment_type(segment: dict[str, Any]) -> str:
    segment_type = _require_text(segment.get("type"), "segment type")
    if segment_type not in {"initial", "delta"}:
        raise ReviewPolicyError(f"invalid segment type: {segment_type}")
    return segment_type


def _segment_required_responsibilities(segment: dict[str, Any]) -> list[str]:
    change = dict(segment)
    change["mode"] = _segment_type(segment)
    return _responsibilities_for(change)


def validate_segment_chain(plan: dict[str, Any], candidate: dict[str, Any]) -> list[dict[str, Any]]:
    final_candidate = _candidate_from(candidate)
    segments = _required_segment_requirements(plan)
    seen_ids: set[str] = set()
    previous_head: str | None = None
    for index, segment in enumerate(segments):
        segment_id = _require_text(segment.get("coverage_id"), "segment coverage id")
        if segment_id in seen_ids:
            raise ReviewPolicyError(f"duplicate segment coverage id: {segment_id}")
        seen_ids.add(segment_id)
        segment_type = _segment_type(segment)
        segment_candidate = _segment_candidate(segment)
        if segment_candidate["repo"] != final_candidate["repo"]:
            raise ReviewPolicyError("segment repository does not match the final candidate")
        if segment_candidate["base"] == segment_candidate["head"]:
            raise ReviewPolicyError(f"segment {segment_id} must cover a non-empty commit range")
        if index == 0:
            if segment_type != "initial":
                raise ReviewPolicyError("first segment must be an initial full baseline review")
            if segment_candidate["base"] != final_candidate["base"]:
                raise ReviewPolicyError("first segment must start at the final candidate base")
        else:
            if segment_type != "delta":
                raise ReviewPolicyError("only the first segment may be initial")
            if segment_candidate["base"] != previous_head:
                raise ReviewPolicyError("segment requirements must be ordered and contiguous")
        previous_head = segment_candidate["head"]
    if previous_head != final_candidate["head"]:
        raise ReviewPolicyError("segment requirements must end at the final candidate head")
    return segments


def _validate_initial_reviewers(segment_id: str, records: list[dict[str, Any]]) -> None:
    correctness_reviewers: set[str] = set()
    testing_reviewers: set[str] = set()
    for record in records:
        responsibilities = set(_unique_texts(record.get("responsibilities"), "coverage responsibilities"))
        reviewer = _require_text(record.get("reviewer_id"), "coverage reviewer id")
        if "correctness_contracts" in responsibilities:
            correctness_reviewers.add(reviewer)
        if "testing_maintainability_standards" in responsibilities:
            testing_reviewers.add(reviewer)
    if not correctness_reviewers or not testing_reviewers:
        raise ReviewPolicyError(f"segment {segment_id} initial review is incomplete")
    if correctness_reviewers & testing_reviewers:
        raise ReviewPolicyError(f"segment {segment_id} initial responsibilities require separate reviewers")


def validate_segment_requirements(plan: dict[str, Any], coverage_records: list[dict[str, Any]]) -> set[str]:
    grouped = _records_by_coverage_id(coverage_records)
    segments = _required_segment_requirements(plan)
    required_ids = [_require_text(segment.get("coverage_id"), "segment coverage id") for segment in segments]
    extras = sorted(set(grouped) - set(required_ids))
    if extras:
        raise ReviewPolicyError(f"unexpected coverage records: {', '.join(extras)}")
    covered_all: set[str] = set()
    for segment in segments:
        segment_id = _require_text(segment.get("coverage_id"), "segment coverage id")
        records = grouped.get(segment_id, [])
        if not records:
            raise ReviewPolicyError(f"missing required coverage records: {segment_id}")
        segment_candidate = _segment_candidate(segment)
        expected = _segment_expectation(segment)
        author_id = _require_text(segment.get("author_id"), f"segment {segment_id} author id")
        segment_covered: set[str] = set()
        for record in records:
            segment_covered.update(validate_coverage_record(record, segment_candidate, author_id, expected))
        required = set(_segment_required_responsibilities(segment))
        missing = sorted(required - segment_covered)
        if missing:
            raise ReviewPolicyError(f"segment {segment_id} missing review coverage: {', '.join(missing)}")
        if _segment_type(segment) == "initial":
            _validate_initial_reviewers(segment_id, records)
        covered_all.update(segment_covered)
    return covered_all


def validate_repair_packet(packet: dict[str, Any], stage_allowance: dict[str, Any]) -> list[str]:
    packet = _require_dict(packet, "repair packet")
    stage_allowance = _require_dict(stage_allowance, "stage allowance")
    allowed = set(_unique_texts(stage_allowance.get("files"), "allowed files"))
    packet_paths: list[str] = []
    for key in ("production_files", "test_files", "contract_files", "configuration_files"):
        packet_paths.extend(_unique_texts(packet.get(key, []), key))
    missing = sorted(path for path in packet_paths if path not in allowed)
    if packet.get("behavioral_repair"):
        regression = _require_dict(packet.get("regression"), "behavioral repair regression")
        regression_path = _require_text(regression.get("path"), "regression path")
        if regression_path not in packet.get("test_files", []):
            raise ReviewPolicyError("behavioral repair regression must be included in test files")
        if not regression.get("exercises_failure"):
            raise ReviewPolicyError("behavioral repair regression must exercise the defect")
    return missing


def no_progress_decision(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(attempts, list):
        raise ReviewPolicyError("repair attempts must be a list")
    by_finding: dict[str, list[dict[str, Any]]] = {}
    for attempt in attempts:
        attempt = _require_dict(attempt, "repair attempt")
        finding_id = _require_text(attempt.get("finding_id"), "repair attempt finding id")
        by_finding.setdefault(finding_id, []).append(attempt)
    for finding_id, history in by_finding.items():
        if len(history) >= 2 and not any(attempt.get("progress") is True for attempt in history[-2:]):
            return {"decision": "bounded_diagnosis", "finding_id": finding_id}
    return {"decision": "continue", "finding_id": attempts[-1]["finding_id"] if attempts else None}


def validate_convergence(
    candidate: dict[str, Any],
    plan: dict[str, Any],
    coverage_records: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    receipts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    plan = _require_dict(plan, "review plan")
    _validate_policy_record(plan, "review plan")
    if plan.get("yield_stop"):
        raise ReviewPolicyError("yield-stop cannot accept unreviewed fixes")
    expected_candidate = _candidate_from(candidate)
    if _candidate_from(plan.get("candidate")) != expected_candidate:
        raise ReviewPolicyError("review plan does not match the exact candidate")
    if not isinstance(coverage_records, list):
        raise ReviewPolicyError("coverage records must be a list")
    validate_segment_chain(plan, expected_candidate)
    covered = validate_segment_requirements(plan, coverage_records)
    unresolved = validate_findings_ledger(findings)
    if unresolved:
        ids = ", ".join(entry["finding_id"] for entry in unresolved)
        raise ReviewPolicyError(f"unresolved mandatory findings: {ids}")
    required_receipts = _unique_texts(plan.get("required_receipts"), "required receipts")
    if not required_receipts:
        raise ReviewPolicyError("review plan must require exact-candidate verification receipts")
    receipt_expectations = _require_dict(plan.get("receipt_expectations"), "receipt expectations")
    receipt_commands: set[str] = set()
    for receipt in receipts or []:
        command = _require_text(receipt.get("command"), "receipt command")
        if command not in receipt_expectations:
            raise ReviewPolicyError(f"unexpected verification receipt command: {command}")
        expected_receipt = dict(_require_dict(receipt_expectations[command], f"receipt expectation {command}"))
        expected_receipt["command"] = command
        expected_receipt["require_success"] = True
        validated = validate_verification_receipt(receipt, expected_candidate, expected_receipt)
        receipt_commands.add(validated["command"])
    missing_receipts = sorted(set(required_receipts) - receipt_commands)
    if missing_receipts:
        raise ReviewPolicyError(f"missing required verification receipts: {', '.join(missing_receipts)}")
    return {
        "policy": POLICY,
        "schema_version": SCHEMA_VERSION,
        "candidate": expected_candidate,
        "covered_responsibilities": sorted(covered),
        "unresolved_mandatory_findings": [],
        "converged": True,
    }
