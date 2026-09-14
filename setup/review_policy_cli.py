#!/usr/bin/env python3
"""Retain bounded review briefs and immutable per-task evidence checkpoints."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

import review_policy as policy
from review_qualification import retained_file


def checkpoint(root: Path, name: str, value: dict) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name):
        raise policy.ReviewPolicyError("invalid checkpoint name")
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise policy.ReviewPolicyError("checkpoint directory must not be a symlink")
    data = policy.canonical_bytes(value) + b"\n"
    path = root / (name + ".json")
    with (root / ".checkpoint.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.is_symlink():
            raise policy.ReviewPolicyError("checkpoint must not be a symlink")
        if path.exists():
            if path.read_bytes() != data:
                raise policy.ReviewPolicyError("checkpoint conflict; retain a separate attempt")
            return path
        fd, temporary = tempfile.mkstemp(dir=root, prefix=".checkpoint-")
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            directory = os.open(root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return path


def prepare(request: dict, evidence_root: Path, worktree: Path) -> dict:
    plan = policy.required_review_plan(request["change"])
    candidate = plan["candidate"]
    refs = request.get("evidence", [])
    if not isinstance(refs, list) or not refs:
        raise policy.ReviewPolicyError("bounded brief requires retained context evidence")
    evidence_paths = [retained_file(evidence_root, ref) for ref in refs]
    approved_plan = (evidence_root / request["approved_plan"]).resolve()
    if approved_plan not in evidence_paths:
        raise policy.ReviewPolicyError("approved plan must be included in hashed context evidence")
    diff = subprocess.run(["git", "-C", str(worktree), "diff", "--no-ext-diff",
                           "--no-textconv", "--binary", candidate["base"],
                           candidate["head"], "--"], capture_output=True, check=True).stdout
    head = subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD"],
                          capture_output=True, check=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(worktree), "status", "--porcelain"],
                           capture_output=True, check=True, text=True).stdout
    if head != candidate["head"] or dirty:
        raise policy.ReviewPolicyError("review needs the clean exact candidate checkout")
    context = {"plan": plan, "approved_plan": request["approved_plan"],
               "invariants": request["invariants"], "open_finding_ids": request["open_finding_ids"],
               "evidence": refs}
    return {**plan, "approved_plan": str(approved_plan),
            "invariants": request["invariants"], "open_finding_ids": request["open_finding_ids"],
            "evidence": refs, "context_digest": policy.digest(context),
            "diff_sha256": hashlib.sha256(diff).hexdigest(),
            "diff": diff.decode("utf-8", errors="strict"),
            "skill_arguments": ["mode:report-only", f"base:{candidate['base']}",
                                f"plan:{approved_plan}"],
            "model": "gpt-5.6-sol", "reasoning_effort": "medium"}


def normalize(root: Path, task: str, request: dict) -> dict:
    source = request["source_response"]
    checkpoint(root, task + ".source", source)
    attempt = request.get("format_retry_attempts", 0)
    if type(attempt) is not int or attempt not in {0, 1}:
        raise policy.ReviewPolicyError("one format-only retry is allowed")
    normalized = request["normalized"]
    checkpoint(root, task + f".normalization-{attempt}", normalized)
    try:
        findings = policy.validate_normalized_blockers(source, normalized, attempt)
    except policy.ReviewPolicyError as exc:
        checkpoint(root, task + f".unresolved-{attempt}",
                   {"status": "unresolved", "reason": str(exc), "source_digest": policy.digest(source)})
        raise
    result = {"status": "complete", "source_digest": policy.digest(source), "findings": findings}
    checkpoint(root, task + ".normalized", result)
    return result


def ledger_revision(previous: list[dict], current: list[dict]) -> dict:
    policy.validate_findings_ledger(previous)
    policy.validate_findings_ledger(current)
    indexed = {finding["finding_id"]: finding for finding in current}
    for finding in previous:
        successor = indexed.get(finding["finding_id"])
        if successor is None:
            raise policy.ReviewPolicyError(f"ledger dropped finding {finding['finding_id']}")
        for field in ("violated_invariant", "affected_paths", "severity", "evidence", "owning_repository"):
            if successor.get(field) != finding.get(field):
                raise policy.ReviewPolicyError(f"immutable finding field changed: {finding['finding_id']}.{field}")
    return {"policy": policy.POLICY, "schema_version": policy.SCHEMA_VERSION,
            "previous_digest": policy.digest(previous), "findings": current}


def validate_checkpoint(action: str, request: dict, root: Path) -> dict:
    if action == "coverage":
        retained_file(root, request["raw_output"])
        if not request["author_id"] or not all(request["expected"].get(key) for key in ("source_digest", "context_digest")):
            raise policy.ReviewPolicyError("coverage requires author and expected source/context digests")
        policy.validate_coverage_record(request["record"], request["candidate"],
                                        request["author_id"], request["expected"])
    else:
        retained_file(root, request["retained_output"])
        if request["retained_output"]["sha256"] != request["record"]["retained_output_digest"]:
            raise policy.ReviewPolicyError("receipt does not bind the retained command output")
        policy.validate_verification_receipt(request["record"], request["candidate"], request["expected"])
    return request


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "normalize", "converge", "repair-packet",
                                           "coverage", "receipt", "ledger", "repair-attempts"])
    parser.add_argument("request", type=Path)
    parser.add_argument("--checkpoints", type=Path, required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--worktree", type=Path)
    args = parser.parse_args()
    try:
        request = json.loads(args.request.read_text())
        if args.action == "prepare":
            if args.worktree is None:
                raise policy.ReviewPolicyError("prepare requires --worktree")
            result = prepare(request, args.request.parent, args.worktree)
        elif args.action == "normalize":
            result = normalize(args.checkpoints, args.task, request)
        elif args.action in {"coverage", "receipt"}:
            result = validate_checkpoint(args.action, request, args.request.parent)
        elif args.action == "ledger":
            previous = json.loads(retained_file(args.request.parent, request["previous"]).read_text())
            result = ledger_revision(previous["findings"], request["findings"])
        elif args.action == "repair-attempts":
            result = policy.no_progress_decision(request["attempts"])
        elif args.action == "repair-packet":
            missing = policy.validate_repair_packet(request["packet"], request["stage_allowance"])
            result = {"status": "scope-amendment-required" if missing else "authorized",
                      "missing_paths": missing}
        else:
            result = policy.validate_convergence(**request)
        path = checkpoint(args.checkpoints, args.task, result)
        print(json.dumps({"checkpoint": str(path), "result": result}))
        blocked = result.get("status") == "scope-amendment-required" or result.get("decision") == "bounded_diagnosis"
        return 1 if blocked else 0
    except (ValueError, OSError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
