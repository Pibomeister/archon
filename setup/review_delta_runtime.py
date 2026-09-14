#!/usr/bin/env python3
"""Executable risk-delta-v1 review runtime boundary.

This helper is deliberately file-based: workflow nodes prepare bounded review
assignments, independent reviewer nodes write one response file per slot, and
the gate checkpoints each completed slot without starting agents itself.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any

import review_policy as policy
import feature_chain as chain
import review_session
from review_policy_cli import checkpoint, ledger_revision


MAX_SLOTS = 9
DEFAULT_ROUND_CAP = 4
VERIFICATION_REQUIRED = "VERIFICATION_REQUIRED"
AUTHORITY_SCHEMA = "archon.review-delta-authority.v1"
SCOPE_AUTHORITY_KEYS = [
    "risk_areas",
    "cross_store",
    "impact_tooling_missing",
    "impact_evidence_available",
    "baseline_invalid",
    "impact_unbounded",
    "invariant_changed",
    "public_contract_changed",
    "permission_boundary_changed",
    "shared_dependency_changed",
    "subsystem_changed",
    "source_changed",
    "dependency_changed",
    "configuration_changed",
    "verification_environment_changed",
    "affected_subsystems",
]
MUTABLE_REVIEW_STATE_KEYS = {
    "coverage_records",
    "findings",
    "segment_requirements",
    "receipts",
}


def read_json(path: Path, default: Any | None = None) -> Any:
    if not path.exists():
        if default is not None:
            return default
        raise policy.ReviewPolicyError(f"missing JSON file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = policy.canonical_bytes(value) + b"\n"
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def git(worktree: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def git_bytes(worktree: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        check=True,
        capture_output=True,
    ).stdout


def require_clean_head(worktree: Path) -> str:
    head = git(worktree, "rev-parse", "HEAD")
    if git(worktree, "status", "--porcelain"):
        raise policy.ReviewPolicyError("review needs a clean exact candidate checkout")
    return head


def is_ancestor(worktree: Path, ancestor: str, descendant: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(worktree), "merge-base", "--is-ancestor", ancestor, descendant],
        capture_output=True,
        text=True,
    )
    if proc.returncode not in {0, 1}:
        raise policy.ReviewPolicyError(proc.stderr.strip() or "git ancestry check failed")
    return proc.returncode == 0


def commit_distance(worktree: Path, base: str, head: str) -> int:
    return int(git(worktree, "rev-list", "--count", f"{base}..{head}") or "0")


def source_tree_digest(worktree: Path) -> str:
    tree = git(worktree, "rev-parse", "HEAD^{tree}")
    return hashlib.sha256(tree.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def state_path(artifacts: Path, explicit: Path | None) -> Path:
    return explicit if explicit is not None else artifacts / "review-state.json"


def request_path(artifacts: Path, explicit: Path | None) -> Path:
    return explicit if explicit is not None else artifacts / "review-request.json"


def digest_state_items(state: dict[str, Any], keys: list[str]) -> str:
    return policy.digest({key: state.get(key) for key in keys})


def controller_seed_body(state: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in state.items()
        if key != "controller_mac" and key not in MUTABLE_REVIEW_STATE_KEYS
    }


def verify_authority(artifacts: Path, state: dict[str, Any]) -> dict[str, Any]:
    control = os.environ.get("ARCHON_CONTROL_DIR")
    chain_id = os.environ.get("ARCHON_FEATURE_CHAIN_ID")
    if not control or not chain_id:
        raise policy.ReviewPolicyError("feature-chain controller authority is required")
    try:
        controller_state = chain.read_state(Path(control), chain_id)
    except (OSError, ValueError, KeyError) as exc:
        raise policy.ReviewPolicyError(f"cannot verify review-state controller authority: {exc}") from exc
    if state.get("kind") != "risk-delta-review-state-seed":
        raise policy.ReviewPolicyError("review-state seed kind is unsupported")
    if state.get("logical_chain_id") != controller_state.get("logical_chain_id"):
        raise policy.ReviewPolicyError("review-state seed chain mismatch")
    body = controller_seed_body(state)
    mac = state.get("controller_mac")
    if not isinstance(mac, str) or mac != chain.hmac_sha256(controller_state["chain_secret"], body):
        raise policy.ReviewPolicyError("review-state controller MAC mismatch")
    policy_record = state.get("policy")
    if not isinstance(policy_record, dict) or policy_record.get("qualification_status") != "qualified":
        raise policy.ReviewPolicyError("review policy is not qualified")
    protected = state.get("protected_inputs")
    if not isinstance(protected, dict):
        raise policy.ReviewPolicyError("review-state protected_inputs are required")
    for key in ("repo", "baseline_base", "author_id", "captured_source_digest"):
        if protected.get(key) != state.get(key):
            raise policy.ReviewPolicyError(f"review-state protected input mismatch: {key}")
    expected_digests = {
        "required_checks_digest": digest_state_items(state, ["required_receipts", "receipt_expectations"]),
        "scope_inputs_digest": digest_state_items(state, SCOPE_AUTHORITY_KEYS),
        "coverage_provenance_digest": policy.digest(state.get("trusted_coverage_provenance", [])),
    }
    for key, expected in expected_digests.items():
        if protected.get(key) != expected:
            raise policy.ReviewPolicyError(f"review-state protected input mismatch: {key}")
    return state


def infer_worktree(artifacts: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.resolve()
    params = read_json(artifacts / "params.json")
    worktree = params.get("worktree") or params.get("WT")
    if not isinstance(worktree, str) or not worktree:
        raise policy.ReviewPolicyError("worktree is required when params.json lacks worktree")
    return Path(worktree).resolve()


@contextlib.contextmanager
def state_lock(artifacts: Path):
    artifacts.mkdir(parents=True, exist_ok=True)
    with (artifacts / ".review-runtime.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def completed_records(state: dict[str, Any]) -> list[dict[str, Any]]:
    records = state.get("coverage_records", [])
    if not isinstance(records, list):
        raise policy.ReviewPolicyError("coverage_records must be a list")
    return [record for record in records if record.get("status") == "complete"]


def valid_segment_heads(worktree: Path, state: dict[str, Any], head: str) -> list[str]:
    segments = state.get("segment_requirements", [])
    if not isinstance(segments, list):
        raise policy.ReviewPolicyError("segment_requirements must be a list")
    records = completed_records(state)
    valid: list[str] = []
    for index in range(1, len(segments) + 1):
        prefix = segments[:index]
        candidate = {
            "repo": policy._segment_candidate(prefix[0])["repo"],
            "base": policy._segment_candidate(prefix[0])["base"],
            "head": policy._segment_candidate(prefix[-1])["head"],
        }
        segment_plan = {
            "policy": policy.POLICY,
            "schema_version": policy.SCHEMA_VERSION,
            "candidate": candidate,
            "segment_requirements": prefix,
        }
        try:
            policy.validate_segment_chain(segment_plan, candidate)
            policy.validate_segment_requirements(segment_plan, records)
        except policy.ReviewPolicyError:
            continue
        if is_ancestor(worktree, candidate["head"], head):
            valid.append(candidate["head"])
    return valid


def choose_review_base(worktree: Path, state: dict[str, Any], head: str) -> tuple[str, bool, bool]:
    baseline = policy._require_commit(state.get("baseline_base"), "baseline base")
    candidates = [baseline, *valid_segment_heads(worktree, state, head)]
    fully_covered = head in candidates
    best = min(candidates, key=lambda base: commit_distance(worktree, base, head))
    return best, best == baseline and not valid_segment_heads(worktree, state, head), fully_covered


def round_cap(artifacts: Path, state: dict[str, Any]) -> int:
    if (artifacts / "round-cap.txt").exists():
        text = (artifacts / "round-cap.txt").read_text(encoding="utf-8").strip()
        if not text.isdigit():
            raise policy.ReviewPolicyError(f"round-cap.txt is not an integer: [{text}]")
        return int(text)
    value = state.get("round_cap", DEFAULT_ROUND_CAP)
    if not isinstance(value, int) or value < 1:
        raise policy.ReviewPolicyError("round_cap must be a positive integer")
    return value


def current_round(artifacts: Path) -> int:
    path = artifacts / "round.txt"
    if not path.exists():
        return 0
    text = path.read_text(encoding="utf-8").strip()
    if not text.isdigit():
        raise policy.ReviewPolicyError(f"round.txt is not an integer: [{text}]")
    return int(text)


def open_mandatory_findings(state: dict[str, Any]) -> list[dict[str, Any]]:
    findings = state.get("findings", [])
    if not isinstance(findings, list):
        raise policy.ReviewPolicyError("findings must be a list")
    return [finding for finding in findings if finding.get("status", "open") == "open" and policy.is_mandatory_finding(finding)]


def change_for_segment(state: dict[str, Any], repo: str, base: str, head: str, initial: bool) -> dict[str, Any]:
    impact_missing = state.get("impact_tooling_missing")
    if impact_missing is None:
        impact_missing = not bool(state.get("impact_evidence_available"))
    change = {
        "mode": "initial" if initial else "delta",
        "candidate": policy.exact_candidate(repo, base, head),
        "production_changed": state.get("production_changed", True),
        "risk_areas": state.get("risk_areas", []),
        "cross_store": state.get("cross_store", False),
        "impact_tooling_missing": impact_missing,
    }
    for flag in (
        "baseline_invalid",
        "impact_unbounded",
        "invariant_changed",
        "public_contract_changed",
        "permission_boundary_changed",
        "shared_dependency_changed",
        "subsystem_changed",
        "source_changed",
        "dependency_changed",
        "configuration_changed",
        "verification_environment_changed",
    ):
        if state.get(flag):
            change[flag] = True
    return change


def context_payload(state: dict[str, Any], plan: dict[str, Any], diff_sha: str) -> dict[str, Any]:
    return {
        "plan": plan,
        "approved_plan": state.get("approved_plan"),
        "worktree": state.get("worktree"),
        "invariants": state.get("invariants", []),
        "affected_paths": state.get("affected_paths", []),
        "affected_subsystems": state.get("affected_subsystems", []),
        "affected_callers_readers": state.get("affected_callers_readers", []),
        "open_findings": open_mandatory_findings(state),
        "diff_sha256": diff_sha,
    }


def runtime_source_digest(state: dict[str, Any]) -> str:
    digest = state.get("captured_source_digest") or state.get("source_digest")
    if digest:
        return policy._require_sha(digest, "captured source digest")
    return policy.digest({"helper": "review_delta_runtime.py", "policy": policy.POLICY})


def existing_current_review(artifacts: Path, head: str, request_digest: str) -> dict[str, Any] | None:
    path = artifacts / "current-review.json"
    if not path.exists():
        return None
    current = read_json(path)
    candidate = current.get("candidate", {})
    if candidate.get("head") == head and current.get("status") == "pending" and current.get("request_digest") == request_digest:
        return current
    return None


def slot_flags(assignments: list[dict[str, Any]]) -> dict[str, str]:
    pending = {assignment["slot"] for assignment in assignments if assignment.get("status") == "pending"}
    return {f"review_{index}": "yes" if index in pending else "no" for index in range(1, MAX_SLOTS + 1)}


def refresh_review_state_if_default(artifacts: Path, state_file: Path) -> None:
    if state_file.resolve() != (artifacts / "review-state.json").resolve():
        return
    control = os.environ.get("ARCHON_CONTROL_DIR")
    chain_id = os.environ.get("ARCHON_FEATURE_CHAIN_ID")
    if control and chain_id:
        try:
            chain.refresh_review_state(Path(control), chain_id, artifacts)
        except chain.FeatureChainError as exc:
            raise policy.ReviewPolicyError(str(exc)) from exc


def prepare_review(artifacts: Path, worktree: Path, state_file: Path, request_file: Path | None) -> dict[str, Any]:
    refresh_review_state_if_default(artifacts, state_file)
    state = read_json(state_file)
    verify_authority(artifacts, state)
    if not state.get("segment_requirements"):
        for path in artifacts.glob("round-*/review-summary.json"):
            summary = read_json(path)
            if summary.get("policy") != policy.POLICY:
                raise policy.ReviewPolicyError(
                    "HISTORICAL_COVERAGE_IMPORT_REQUIRED: retain and validate the completed "
                    "legacy review before dispatch; do not restart a full review"
                )
    request = read_json(request_file, {}) if request_file is not None and request_file.exists() else {}
    head = require_clean_head(worktree)
    base, initial, fully_covered = choose_review_base(worktree, state, head)
    repo = policy._require_text(state.get("repo"), "repo")
    if fully_covered:
        previous = read_json(artifacts / "current-review.json", {})
        current = {**previous, "policy": policy.POLICY, "schema_version": policy.SCHEMA_VERSION,
                   "status": "covered", "round": current_round(artifacts),
                   "candidate": policy.exact_candidate(repo, base, head), "assignments": [],
                   "brief": {"open_findings": open_mandatory_findings(state)}}
        atomic_write_json(artifacts / "current-review.json", current)
        return {**slot_flags([]), "current_review": str(artifacts / "current-review.json"), "reused": False}

    change = change_for_segment(state, repo, base, head, initial)
    if (change.get("baseline_invalid") or change.get("impact_unbounded")) and not state.get("full_review_recovery_approved"):
        raise policy.ReviewPolicyError("full-candidate recovery requires explicit full_review_recovery_approved")
    if any(change.get(flag) for flag in (
        "invariant_changed",
        "public_contract_changed",
        "permission_boundary_changed",
        "shared_dependency_changed",
        "subsystem_changed",
    )) and not state.get("affected_subsystems"):
        raise policy.ReviewPolicyError("affected-subsystem review requires affected_subsystems context")
    plan = policy.required_review_plan(change)
    diff = git_bytes(worktree, "diff", "--no-ext-diff", "--no-textconv", "--binary", base, head, "--")
    diff_sha = hashlib.sha256(diff).hexdigest()
    source_digest = runtime_source_digest(state)
    context = context_payload(state, plan, diff_sha)
    context_digest = policy.digest(context)
    request_digest = policy.digest({
        "candidate": plan["candidate"],
        "scope": plan["scope"],
        "responsibilities": plan["required_responsibilities"],
        "source_digest": source_digest,
        "context_digest": context_digest,
    })
    reused = existing_current_review(artifacts, head, request_digest)
    if reused is not None:
        return {**slot_flags(reused["assignments"]), "current_review": str(artifacts / "current-review.json"), "round": reused["round"], "reused": True}

    round_number = current_round(artifacts)
    cap = round_cap(artifacts, state)
    if round_number >= cap:
        raise policy.ReviewPolicyError(f"ROUND_CAP_REACHED round={round_number} cap={cap}")
    round_number += 1
    (artifacts / "round.txt").write_text(f"{round_number}\n", encoding="utf-8")
    round_dir = artifacts / f"round-{round_number}"
    round_dir.mkdir(parents=True, exist_ok=True)
    (round_dir / "pre-head.txt").write_text(f"{head}\n", encoding="utf-8")

    responsibilities = plan["required_responsibilities"]
    if len(responsibilities) > MAX_SLOTS:
        raise policy.ReviewPolicyError(f"review requires {len(responsibilities)} slots; max is {MAX_SLOTS}")
    (round_dir / "review-diff.patch").write_bytes(diff)
    coverage_id = f"round-{round_number}-{head[:12]}"
    assignments = []
    for index, responsibility in enumerate(responsibilities, start=1):
        response_file = round_dir / f"review-response-{index}.json"
        assignments.append({
            "slot": index,
            "coverage_id": coverage_id,
            "responsibility": responsibility,
            "candidate": plan["candidate"],
            "response_file": str(response_file),
            "checkpoint_command": [
                "python3",
                str(Path(__file__).resolve()),
                "complete",
                "--artifacts",
                str(artifacts),
                "--slot",
                str(index),
            ],
            "source_digest": source_digest,
            "context_digest": context_digest,
            "response_schema": {
                "reviewer_id": "optional controller-bound session id; if present it must match the retained session receipt",
                "model": policy.REQUIRED_MODEL,
                "reasoning_effort": policy.REQUIRED_REASONING_EFFORT,
                "raw_output": "retained reviewer output",
                "findings": [{
                    "finding_id": "immutable id",
                    "violated_invariant": "required",
                    "affected_paths": ["repo-relative path"],
                    "severity": "P0|P1|P2|P3",
                    "evidence": "required",
                    "owning_repository": repo,
                    "status": "open|closed|advisory",
                    "independent_closure": "required for closed mandatory findings",
                }],
            },
            "status": "pending",
        })
    segment = {
        "coverage_id": coverage_id,
        "type": "initial" if initial else "delta",
        "candidate": plan["candidate"],
        "author_id": policy._require_text(state.get("author_id"), "author id"),
        "source_digest": source_digest,
        "context_digest": context_digest,
        "risk_areas": state.get("risk_areas", []),
        "cross_store": state.get("cross_store", False),
        "impact_tooling_missing": change.get("impact_tooling_missing", False),
    }
    current = {
        "policy": policy.POLICY,
        "schema_version": policy.SCHEMA_VERSION,
        "status": "pending",
        "round": round_number,
        "round_dir": str(round_dir),
        "candidate": plan["candidate"],
        "scope": plan["scope"],
        "approved_plan": state.get("approved_plan"),
        "worktree": str(worktree),
        "assignments": assignments,
        "segment_requirement": segment,
        "review_batches": plan["review_batches"],
        "request_digest": request_digest,
        "diff_sha256": diff_sha,
        "diff_path": str(round_dir / "review-diff.patch"),
        "brief": context,
        "skill_arguments": ["mode:report-only", f"base:{base}"],
        "model": policy.REQUIRED_MODEL,
        "reasoning_effort": policy.REQUIRED_REASONING_EFFORT,
    }
    atomic_write_json(artifacts / "current-review.json", current)
    return {**slot_flags(assignments), "current_review": str(artifacts / "current-review.json"), "round": round_number, "reused": False}


def response_path(artifacts: Path, current: dict[str, Any], assignment: dict[str, Any]) -> Path:
    configured = Path(assignment["response_file"])
    if configured.is_absolute():
        return configured
    path = artifacts / configured
    if path.exists():
        return path
    return Path(current["round_dir"]) / assignment["response_file"]


def read_response_json(path: Path, checkpoints: Path, assignment: dict[str, Any]) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    checkpoint_prefix = f"{assignment['coverage_id']}.slot-{assignment['slot']}"
    try:
        response = json.loads(raw)
    except json.JSONDecodeError as exc:
        checkpoint(checkpoints, f"{checkpoint_prefix}.source", {"raw_output": raw, "parse_error": str(exc)})
        checkpoint(checkpoints, f"{checkpoint_prefix}.unresolved", {"status": "unresolved", "reason": "malformed reviewer JSON"})
        raise policy.ReviewPolicyError("malformed reviewer JSON")
    if not isinstance(response, dict):
        checkpoint(checkpoints, f"{checkpoint_prefix}.source", {"raw_output": raw, "parse_error": "response must be an object"})
        raise policy.ReviewPolicyError("review response must be an object")
    response.setdefault("raw_output", raw)
    return response


def findings_from_response(response: dict[str, Any]) -> list[dict[str, Any]]:
    if "source_response" in response or "normalized" in response:
        return policy.validate_normalized_blockers(
            response["source_response"],
            response["normalized"],
            response.get("format_retry_attempts", 0),
        )
    if response.get("malformed"):
        raise policy.ReviewPolicyError("malformed reviewer output requires normalized blocker accounting")
    findings = response.get("findings", [])
    if not isinstance(findings, list):
        raise policy.ReviewPolicyError("review findings must be a list")
    return findings


def author_session_ids(state: dict[str, Any]) -> set[str]:
    sessions = state.get("author_session_ids", [])
    if not isinstance(sessions, list):
        raise policy.ReviewPolicyError("author_session_ids must be a list")
    out: set[str] = set()
    for item in sessions:
        if isinstance(item, dict) and isinstance(item.get("session_id"), str):
            out.add(item["session_id"])
        elif isinstance(item, str):
            out.add(item)
    return out


def coverage_from_response(response: dict[str, Any], assignment: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    reviewer_id = policy._require_text(receipt.get("session_id"), "review session id")
    record = response.get("coverage_record")
    if isinstance(record, dict):
        if record.get("reviewer_id") != reviewer_id:
            raise policy.ReviewPolicyError("coverage reviewer does not match controller-bound session")
        if record.get("model") != receipt.get("model") or record.get("reasoning_effort") != receipt.get("reasoning_effort"):
            raise policy.ReviewPolicyError("coverage model does not match controller-bound session")
        if record.get("coverage_id") != assignment["coverage_id"] or record.get("responsibilities") != [assignment["responsibility"]]:
            raise policy.ReviewPolicyError("coverage exceeds the assigned reviewer responsibility")
        return record
    return {
        "policy": policy.POLICY,
        "schema_version": policy.SCHEMA_VERSION,
        "candidate": assignment["candidate"],
        "coverage_id": assignment["coverage_id"],
        "responsibilities": [assignment["responsibility"]],
        "source_digest": assignment["source_digest"],
        "context_digest": assignment["context_digest"],
        "reviewer_id": reviewer_id,
        "model": receipt.get("model", policy.REQUIRED_MODEL),
        "reasoning_effort": receipt.get("reasoning_effort", policy.REQUIRED_REASONING_EFFORT),
        "status": response.get("status", "complete"),
    }


def merge_records(existing: list[dict[str, Any]], additions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = list(existing)
    fingerprints = {policy.digest(record) for record in out}
    for record in additions:
        fingerprint = policy.digest(record)
        if fingerprint not in fingerprints:
            out.append(record)
            fingerprints.add(fingerprint)
    return out


def merge_findings(existing: list[dict[str, Any]], additions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not additions:
        return existing
    previous = {"findings": existing}
    current_by_id = {finding["finding_id"]: dict(finding) for finding in existing}
    for finding in additions:
        current_by_id[finding["finding_id"]] = finding
    current = list(current_by_id.values())
    ledger_revision(previous["findings"], current)
    return current


def complete_reviews(artifacts: Path, state_file: Path, slot: int | None = None) -> dict[str, Any]:
    with state_lock(artifacts):
        state = read_json(state_file)
        verify_authority(artifacts, state)
        current = read_json(artifacts / "current-review.json")
        if current.get("status") != "pending":
            return {"status": current.get("status"), "completed": 0}
        assignments = current["assignments"]
        selected = [assignment for assignment in assignments if slot is None or assignment["slot"] == slot]
        if slot is not None and not selected:
            raise policy.ReviewPolicyError(f"unknown review slot: {slot}")
        checkpoints = artifacts / "review-checkpoints"
        added_records: list[dict[str, Any]] = []
        added_findings: list[dict[str, Any]] = []
        completed = 0
        missing: list[str] = []
        for assignment in selected:
            if assignment.get("status") == "complete":
                continue
            path = response_path(artifacts, current, assignment)
            if not path.exists():
                missing.append(path.name)
                continue
            response = read_response_json(path, checkpoints, assignment)
            checkpoint_prefix = f"{assignment['coverage_id']}.slot-{assignment['slot']}"
            checkpoint(checkpoints, f"{checkpoint_prefix}.source", response)
            try:
                receipt = review_session.verify_session_provenance(artifacts, current, assignment, response)
            except (OSError, ValueError, KeyError) as exc:
                raise policy.ReviewPolicyError(f"review session provenance failed: {exc}") from exc
            if receipt["session_id"] in author_session_ids(state):
                raise policy.ReviewPolicyError("reviewer session matches an author session")
            record = coverage_from_response(response, assignment, receipt)
            policy.validate_coverage_record(record, assignment["candidate"], state.get("author_id"), {
                "source_digest": assignment["source_digest"],
                "context_digest": assignment["context_digest"],
            })
            findings = findings_from_response(response)
            checkpoint(checkpoints, f"{checkpoint_prefix}.coverage", record)
            added_records.append(record)
            added_findings.extend(findings)
            assignment["status"] = "complete"
            assignment["reviewer_id"] = record["reviewer_id"]
            completed += 1
        state["coverage_records"] = merge_records(state.get("coverage_records", []), added_records)
        state["findings"] = merge_findings(state.get("findings", []), added_findings)
        segments = state.setdefault("segment_requirements", [])
        if all(assignment.get("status") == "complete" for assignment in assignments):
            current["status"] = "complete"
            segment = current["segment_requirement"]
            if not any(existing.get("coverage_id") == segment["coverage_id"] for existing in segments):
                segments.append(segment)
            mandatory = [finding for finding in state.get("findings", []) if finding.get("status", "open") == "open" and policy.is_mandatory_finding(finding)]
            summary = {
                "verdict": "Ready with fixes" if mandatory else "Ready to merge",
                "residual_count": len(mandatory),
                "degraded": False,
                "policy": policy.POLICY,
            }
            atomic_write_json(Path(current["round_dir"]) / "review-summary.json", summary)
        current["assignments"] = assignments
        atomic_write_json(artifacts / "current-review.json", current)
        atomic_write_json(state_file, state)
        if missing and slot is None:
            raise policy.ReviewPolicyError(f"missing review responses: {', '.join(missing)}")
        return {"status": current["status"], "completed": completed, "pending": [assignment["slot"] for assignment in assignments if assignment.get("status") != "complete"]}


def convergence_plan(state: dict[str, Any], candidate: dict[str, str]) -> dict[str, Any]:
    plan = policy.required_review_plan({"mode": "delta", "candidate": candidate})
    plan["segment_requirements"] = state.get("segment_requirements", [])
    plan["required_receipts"] = state.get("required_receipts", [])
    plan["receipt_expectations"] = state.get("receipt_expectations", {})
    return plan


def current_round_dir(artifacts: Path) -> Path | None:
    round_number = current_round(artifacts)
    if round_number < 1:
        return None
    return artifacts / f"round-{round_number}"


def run_existing_converge_gates(artifacts: Path, worktree: Path) -> None:
    round_dir = current_round_dir(artifacts)
    if round_dir is None:
        raise policy.ReviewPolicyError("CONVERGE=FAIL no review round")
    pre_head = round_dir / "pre-head.txt"
    if not pre_head.exists():
        raise policy.ReviewPolicyError("CONVERGE=FAIL missing round pre-head")
    if git(worktree, "rev-parse", "HEAD") != pre_head.read_text(encoding="utf-8").strip():
        raise policy.ReviewPolicyError("segment requirements must end at the final candidate head")
    fixer_result = round_dir / "fixer-result.json"
    if not fixer_result.exists():
        raise policy.ReviewPolicyError("CONVERGE=FAIL missing fixer-result.json")
    subprocess.run(
        ["python3", str(Path(__file__).resolve().parent / "check-fixer-result.py"), str(fixer_result)],
        check=True,
        capture_output=True,
        text=True,
    )
    data = read_json(fixer_result)
    cross_repo = data.get("cross_repo") or []
    if cross_repo:
        atomic_write_json(artifacts / "cross-repo-findings.json", cross_repo)
        repos = ",".join(sorted({str(item.get("producer_repo", "?")) for item in cross_repo if isinstance(item, dict)}))
        raise policy.ReviewPolicyError(f"CROSS_REPO_FINDING count={len(cross_repo)} repos={repos}")
    allowlist = artifacts / "files-allowlist.json"
    bootstrap = artifacts / "bootstrap-head.txt"
    if not allowlist.exists():
        raise policy.ReviewPolicyError("CONVERGE=FAIL missing files-allowlist.json")
    if not bootstrap.exists():
        raise policy.ReviewPolicyError("CONVERGE=FAIL missing bootstrap-head.txt")
    subprocess.run(
        [
            "python3",
            str(Path(__file__).resolve().parent / "check-scope.py"),
            str(allowlist),
            str(worktree),
            bootstrap.read_text(encoding="utf-8").strip(),
            "--round",
            str(current_round(artifacts)),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def converge(artifacts: Path, worktree: Path, state_file: Path) -> dict[str, Any]:
    refresh_review_state_if_default(artifacts, state_file)
    state = read_json(state_file)
    verify_authority(artifacts, state)
    head = require_clean_head(worktree)
    repo = policy._require_text(state.get("repo"), "repo")
    baseline = policy._require_commit(state.get("baseline_base"), "baseline base")
    candidate = policy.exact_candidate(repo, baseline, head)
    run_existing_converge_gates(artifacts, worktree)
    plan = convergence_plan(state, candidate)
    result = policy.validate_convergence(
        candidate,
        plan,
        state.get("coverage_records", []),
        state.get("findings", []),
        state.get("receipts", []),
    )
    checkpoint(artifacts / "review-checkpoints", "converged", result)
    return {"status": "converged", "promise": "locally review-covered exact candidate", "result": result}


def is_progress_error(message: str) -> bool:
    return any(fragment in message for fragment in (
        "segment requirements must end at the final candidate head",
        "missing required coverage records",
        "missing review coverage",
        "unresolved mandatory findings",
    ))


def is_verification_required(message: str) -> bool:
    return any(fragment in message for fragment in (
        "missing required verification receipts",
        "verification receipt",
    ))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "complete", "converge"])
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--worktree", type=Path)
    parser.add_argument("--review-state", type=Path)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--slot", type=int)
    args = parser.parse_args()
    try:
        artifacts = args.artifacts.resolve()
        artifacts.mkdir(parents=True, exist_ok=True)
        state_file = state_path(artifacts, args.review_state)
        if args.action == "prepare":
            result = prepare_review(artifacts, infer_worktree(artifacts, args.worktree), state_file, request_path(artifacts, args.request))
        elif args.action == "complete":
            result = complete_reviews(artifacts, state_file, args.slot)
        else:
            try:
                result = converge(artifacts, infer_worktree(artifacts, args.worktree), state_file)
            except policy.ReviewPolicyError as exc:
                if is_verification_required(str(exc)):
                    print(json.dumps({"status": VERIFICATION_REQUIRED, "reason": str(exc)}, sort_keys=True))
                    return 1
                if is_progress_error(str(exc)):
                    print(json.dumps({"status": "progressed", "reason": str(exc)}, sort_keys=True))
                    return 0
                raise
        print(json.dumps(result, sort_keys=True))
        if args.action == "converge" and result.get("status") == "converged":
            print("<promise>REVIEW_CONVERGED</promise>")
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
