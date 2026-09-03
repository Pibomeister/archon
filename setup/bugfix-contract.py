#!/usr/bin/env python3
"""Deterministic contracts shared by Archon bugfix gates.

AI nodes author evidence; this helper decides artifact shape, symptom accounting,
and packet classification. It intentionally uses only the Python standard library.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, NoReturn


class ContractError(ValueError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def hash_artifact(root: Path, relative: str) -> dict[str, Any]:
    """Return a stable semantic hash for an artifact.

    JSON artifacts are hashed after canonical JSON serialization so harmless
    formatting and key-order changes do not invalidate a reviewer attestation.
    Non-JSON artifacts are byte hashed because prose/HTML/markdown ordering is
    semantic for the gates that read them.
    """
    path = safe_artifact_path(root, relative)
    raw = path.read_bytes()
    entry: dict[str, Any] = {"path": relative, "bytes": len(raw)}
    if relative.endswith(".json"):
        try:
            entry["kind"] = "json"
            entry["sha256"] = canonical_hash(json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractError(f"cannot canonicalize JSON artifact {relative}: {exc}") from exc
    else:
        entry["kind"] = "bytes"
        entry["sha256"] = hashlib.sha256(raw).hexdigest()
    return entry


def _nonempty(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field} must be a non-empty string")
    return value.strip()


def _sha256_hex(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ContractError(f"{field} must be sha256 hex")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ContractError(f"{field} must be sha256 hex") from exc
    return value


def _byte_span(value: Any, field: str) -> tuple[int, int]:
    if not isinstance(value, dict):
        raise ContractError(f"{field} must be an object")
    start, end = value.get("start"), value.get("end")
    if not isinstance(start, int) or isinstance(start, bool) or not isinstance(end, int) or isinstance(end, bool):
        raise ContractError(f"{field}.start/end must be integers")
    if start < 0 or end <= start:
        raise ContractError(f"{field} must be a positive byte span")
    return start, end


def validate_ledger(
    data: dict[str, Any], *, artifacts_dir: Path | None = None, require_sealed: bool = False
) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("schema_version") != 2:
        raise ContractError("symptoms schema_version must be 2")
    revision = data.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ContractError("symptoms revision must be a positive integer")
    root_hash = _sha256_hex(data.get("ledger_root_hash"), "ledger_root_hash")
    revision_hash = data.get("ledger_revision_hash")
    if revision_hash is not None:
        _sha256_hex(revision_hash, "ledger_revision_hash")
    elif require_sealed:
        raise ContractError("ledger_revision_hash is required after intake sealing")

    sources = data.get("source_symptoms")
    effective = data.get("effective_symptoms")
    if not isinstance(sources, list) or not sources:
        raise ContractError("no-reportable-symptoms")
    if not isinstance(effective, list) or not effective:
        raise ContractError("no-reportable-symptoms")

    source_ids: list[str] = []
    source_order: list[int] = []
    for index, item in enumerate(sources):
        if not isinstance(item, dict):
            raise ContractError(f"source_symptoms[{index}] must be an object")
        sid = _nonempty(item.get("id"), f"source_symptoms[{index}].id")
        if revision == 1 and sid != f"S{index + 1}":
            raise ContractError(f"source symptom ids must be deterministic S1..Sn, got {sid}")
        if revision > 1 and not (sid.startswith("S") and sid[1:].isdigit()):
            raise ContractError(f"source symptom id must retain S<number> identity, got {sid}")
        _nonempty(item.get("claim"), f"source_symptoms[{index}].claim")
        source_document = _nonempty(item.get("source_document") or item.get("source_file"), f"source_symptoms[{index}].source_document")
        source_field = _nonempty(item.get("source_field"), f"source_symptoms[{index}].source_field")
        quote = _nonempty(item.get("source_quote") or item.get("verbatim_evidence"), f"source_symptoms[{index}].source_quote")
        if len(quote) < 5:
            raise ContractError(f"source symptom {sid} quote is too short")
        span = _byte_span(item.get("byte_span"), f"source symptom {sid} byte_span")
        _nonempty(item.get("expected_behavior"), f"source_symptoms[{index}].expected_behavior")
        _nonempty(item.get("actual_behavior"), f"source_symptoms[{index}].actual_behavior")
        if artifacts_dir is not None:
            doc_path = safe_artifact_path(artifacts_dir, source_document)
            raw = doc_path.read_bytes()
            if span[1] > len(raw):
                raise ContractError(f"source symptom {sid} byte_span exceeds source document")
            try:
                span_text = raw[span[0]:span[1]].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ContractError(f"source symptom {sid} byte_span is not valid utf-8") from exc
            full_text = raw.decode("utf-8", errors="replace")
            if quote not in span_text:
                raise ContractError(f"source symptom {sid} quote not found in byte_span")
            if quote not in full_text:
                raise ContractError(f"source symptom {sid} quote not found in source document")
        order = item.get("source_order")
        if not isinstance(order, int) or isinstance(order, bool) or order < 1:
            raise ContractError(f"source symptom {sid} source_order must be positive")
        if sid in source_ids:
            raise ContractError(f"duplicate source symptom id={sid}")
        source_ids.append(sid)
        source_order.append(order)
    if source_order != sorted(source_order) or len(set(source_order)) != len(source_order):
        raise ContractError("source symptom order must be unique and ascending")

    effective_ids: list[str] = []
    source_to_effective = {sid: [] for sid in source_ids}
    for index, item in enumerate(effective):
        if not isinstance(item, dict):
            raise ContractError(f"effective_symptoms[{index}] must be an object")
        eid = _nonempty(item.get("id"), f"effective_symptoms[{index}].id")
        if revision == 1 and eid != f"E{index + 1}":
            raise ContractError(f"effective symptom ids must be deterministic E1..En, got {eid}")
        if revision > 1 and not (eid.startswith("E") and eid[1:].isdigit()):
            raise ContractError(f"effective symptom id must retain E<number> identity, got {eid}")
        _nonempty(item.get("claim"), f"effective_symptoms[{index}].claim")
        _nonempty(item.get("expected_behavior"), f"effective_symptoms[{index}].expected_behavior")
        _nonempty(item.get("actual_behavior"), f"effective_symptoms[{index}].actual_behavior")
        relation = item.get("relation") or "same-as-source"
        if relation not in {"same-as-source", "split", "merge", "supersedes"}:
            raise ContractError(f"effective symptom {eid} relation out of enum")
        refs = item.get("source_ids")
        if not isinstance(refs, list) or not refs or not all(isinstance(x, str) and x for x in refs):
            raise ContractError(f"effective symptom {eid} needs source_ids")
        if eid in effective_ids:
            raise ContractError(f"duplicate effective symptom id={eid}")
        if len(set(refs)) != len(refs):
            raise ContractError(f"effective symptom {eid} has duplicate source_ids")
        for sid in refs:
            if sid not in source_to_effective:
                raise ContractError(f"effective symptom {eid} references unknown source symptom {sid}")
            source_to_effective[sid].append(eid)
        effective_ids.append(eid)
    missing = [sid for sid, refs in source_to_effective.items() if not refs]
    if missing:
        raise ContractError(f"source symptoms have no effective descendants: {missing}")

    expected_root_hash = canonical_hash(sources)
    if root_hash != expected_root_hash:
        raise ContractError(
            f"ledger_root_hash mismatch expected={expected_root_hash} actual={root_hash}"
        )
    if revision_hash is not None:
        revision_payload = dict(data)
        revision_payload["ledger_revision_hash"] = None
        expected_revision_hash = canonical_hash(revision_payload)
        if revision_hash != expected_revision_hash:
            raise ContractError(
                "ledger_revision_hash mismatch "
                f"expected={expected_revision_hash} actual={revision_hash}"
            )

    return {
        "source_ids": source_ids,
        "effective_ids": effective_ids,
        "source_to_effective": source_to_effective,
        "ledger_hash": canonical_hash(data),
    }


def validate_systematic_debugging(artifacts_dir: Path) -> dict[str, Any]:
    debug = load_json(safe_artifact_path(artifacts_dir, "debug-phase.json"))
    boundary = load_json(safe_artifact_path(artifacts_dir, "boundary-trace.json"))
    pattern = load_json(safe_artifact_path(artifacts_dir, "pattern-comparison.json"))
    proof = load_json(safe_artifact_path(artifacts_dir, "proof-assessment.json"))
    hypotheses = load_json(safe_artifact_path(artifacts_dir, "hypotheses.json"))
    fix_plan = load_json(safe_artifact_path(artifacts_dir, "fix-plan.json"))

    phases = debug.get("phases")
    if not isinstance(phases, list) or phases != ["root-cause-investigation", "pattern-analysis", "hypothesis-testing", "implementation"]:
        raise ContractError("debug-phase.json must record the four systematic debugging phases in order")
    status = debug.get("reproduction_status")
    if status not in {"reproduced", "gather-more", "class-only", "historical-external"}:
        raise ContractError("reproduction_status out of enum")
    if status == "gather-more" and fix_plan.get("approach"):
        raise ContractError("fix plan before reproduction/investigation is not allowed")
    if not debug.get("investigation_complete"):
        raise ContractError("root-cause investigation must be complete before RCA gate")
    if debug.get("fix_attempt_count", 0) >= 3 and not debug.get("architecture_review_required"):
        raise ContractError("three failed fixes require architecture review")

    boundaries = boundary.get("boundaries")
    if not isinstance(boundaries, list) or not boundaries:
        raise ContractError("boundary-trace.json needs at least one boundary")
    for idx, row in enumerate(boundaries):
        if not all(row.get(k) for k in ("component", "input", "output", "evidence")):
            raise ContractError(f"boundary {idx + 1} missing component/input/output/evidence")
    surface = boundary.get("surface_equivalence")
    if not isinstance(surface, dict):
        raise ContractError("boundary-trace.json missing surface_equivalence")
    if boundary.get("schema_version") != 5:
        raise ContractError("boundary-trace.json surface equivalence requires schema_version 5")
    required_surface = (
        "reported_surface", "reported_surface_status", "surface_selection_basis",
        "runtime_entrypoint", "runtime_owner",
        "test_entrypoint", "test_runtime_owner", "smoke_entrypoint",
        "smoke_runtime_owner", "test_matches_runtime", "smoke_matches_runtime",
        "evidence",
    )
    if any(key not in surface for key in required_surface):
        raise ContractError("surface_equivalence missing required field")
    for key in (
        "reported_surface", "surface_selection_basis", "runtime_entrypoint", "runtime_owner",
        "test_entrypoint", "test_runtime_owner", "smoke_entrypoint",
        "smoke_runtime_owner",
    ):
        if not isinstance(surface.get(key), str) or not surface[key].strip():
            raise ContractError(f"surface_equivalence {key} must be non-empty")
    if surface.get("reported_surface_status") not in {"explicit", "runtime-reproduced", "ambiguous"}:
        raise ContractError("surface_equivalence reported_surface_status out of enum")
    evidence = surface.get("evidence")
    if not isinstance(evidence, list) or not evidence or any(
        not isinstance(item, dict)
        or item.get("claim") not in {
            "runtime-entrypoint-to-owner",
            "test-entrypoint-to-owner",
            "smoke-entrypoint-to-owner",
        }
        or not item.get("file")
        or not item.get("quote")
        for item in evidence
    ):
        raise ContractError("surface_equivalence needs typed file/quote evidence")
    evidence_claims = {item["claim"] for item in evidence}
    required_claims = {
        "runtime-entrypoint-to-owner",
        "test-entrypoint-to-owner",
        "smoke-entrypoint-to-owner",
    }
    if evidence_claims != required_claims:
        raise ContractError("surface_equivalence needs evidence for runtime, test, and smoke ownership")
    repro = surface.get("reproduction_equivalence")
    if not isinstance(repro, dict):
        raise ContractError("surface_equivalence missing reproduction_equivalence")
    for key in ("observed_preconditions", "test_preconditions", "material_differences"):
        if not isinstance(repro.get(key), list):
            raise ContractError(f"reproduction_equivalence {key} must be a list")
    if not repro["observed_preconditions"] or not repro["test_preconditions"]:
        raise ContractError("reproduction_equivalence needs observed and test preconditions")
    for difference in repro["material_differences"]:
        if (
            not isinstance(difference, dict)
            or not all(difference.get(key) for key in ("dimension", "observed", "test", "impact"))
            or difference["impact"] not in {"covered-by-secondary-proof", "changes-causal-boundary"}
        ):
            raise ContractError("reproduction_equivalence material difference malformed")
    if fix_plan.get("approach"):
        runtime_owner = surface["runtime_owner"].strip()
        if surface["reported_surface_status"] == "ambiguous":
            raise ContractError("implementation-ready plan requires an identified surface and test/smoke runtime equivalence")
        if surface["test_runtime_owner"].strip() != runtime_owner:
            raise ContractError("surface_equivalence test runtime owner mismatch")
        if surface["smoke_runtime_owner"].strip() != runtime_owner:
            raise ContractError("surface_equivalence smoke runtime owner mismatch")
        if (
            surface.get("test_matches_runtime") is not True
            or surface.get("smoke_matches_runtime") is not True
        ):
            raise ContractError("implementation-ready plan requires an identified surface and test/smoke runtime equivalence")
        if repro.get("equivalent") is not True or any(
            item["impact"] == "changes-causal-boundary"
            for item in repro["material_differences"]
        ):
            raise ContractError("implementation-ready plan requires reproduction precondition equivalence")

    if not pattern.get("working_comparison"):
        raise ContractError("pattern-comparison.json missing working comparison")
    diffs = pattern.get("differences")
    if not isinstance(diffs, list) or not diffs:
        raise ContractError("pattern-comparison.json needs differences")

    if not isinstance(hypotheses, list) or not hypotheses:
        raise ContractError("hypotheses.json empty")
    active = [h for h in hypotheses if h.get("status") in {"open", "confirmed-by-experiment"}]
    if len(active) != 1:
        raise ContractError("exactly one active hypothesis is required")
    selected = str(proof.get("selected_hypothesis_id"))
    if selected != str(active[0].get("id")):
        raise ContractError("proof-assessment selected_hypothesis_id must match the one active hypothesis")
    experiment = proof.get("experiment")
    if not isinstance(experiment, dict) or not all(experiment.get(k) for k in ("prediction", "disconfirming_observation", "result")):
        raise ContractError("proof-assessment experiment needs prediction, disconfirming_observation, and result")
    if proof.get("mechanism_valid") is not True and fix_plan.get("approach"):
        raise ContractError("implementation-ready fix plan requires mechanism_valid=true")
    if proof.get("occurrence_attributed") is not True and proof.get("ticket_resolution_claim") == "fixed":
        raise ContractError("occurrence-unattributed proof cannot claim ticket fixed")
    if proof.get("occurrence_attributed") is True:
        occurrence_sources = proof.get("occurrence_evidence_sources")
        if not isinstance(occurrence_sources, list) or not occurrence_sources or not all(
            isinstance(item, str) and item for item in occurrence_sources
        ):
            raise ContractError("occurrence-attributed proof needs occurrence_evidence_sources")
        provenance = load_json(safe_artifact_path(artifacts_dir, "evidence-provenance.json"))
        rows = provenance.get("sources") if isinstance(provenance, dict) else None
        if not isinstance(rows, list):
            raise ContractError("evidence provenance sources missing")
        now = dt.datetime.now(dt.timezone.utc)
        for source in occurrence_sources:
            if source == "report":
                continue
            candidates = [row for row in rows if row.get("source") == source]
            valid = False
            for row in candidates:
                try:
                    expires = dt.datetime.fromisoformat(str(row.get("expires_at", "")).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if (row.get("status") == "complete"
                        and row.get("completeness") == "complete"
                        and row.get("evidence_kind") == "occurrence"
                        and row.get("occurrence_attribution_valid") is True
                        and expires > now):
                    valid = True
                    break
            if not valid:
                raise ContractError(f"occurrence evidence source is incomplete, stale, or invalidated: {source}")
    return {"reproduction_status": status, "active_hypothesis_id": active[0].get("id")}


DISPOSITIONS = {
    "fixed", "class-hardening-only", "by-design", "separate-ticket",
    "product-semantics", "unresolved",
}


def classify(
    ledger: dict[str, Any],
    dispositions: dict[str, dict[str, Any]],
    *,
    incidental_mechanism: bool,
) -> dict[str, Any]:
    shape = validate_ledger(ledger, require_sealed=True)
    expected = set(shape["effective_ids"])
    actual = set(dispositions)
    if actual != expected:
        raise ContractError(
            f"effective disposition coverage mismatch missing={sorted(expected-actual)} unknown={sorted(actual-expected)}"
        )

    values: dict[str, str] = {}
    for eid in shape["effective_ids"]:
        item = dispositions[eid]
        if not isinstance(item, dict):
            raise ContractError(f"disposition {eid} must be an object")
        value = item.get("disposition")
        if value not in DISPOSITIONS:
            raise ContractError(f"disposition {eid} out of enum: {value}")
        if value == "by-design" and not item.get("authority"):
            raise ContractError(f"by-design requires product authority for {eid}")
        if value == "separate-ticket":
            if item.get("repo") not in {"api", "web-app"} or not item.get("ticket_stub") or not item.get("authority"):
                raise ContractError(f"separate-ticket requires authority, repo, and ticket_stub for {eid}")
        values[eid] = value

    all_fixed = all(v == "fixed" for v in values.values())
    any_fixed = any(v == "fixed" for v in values.values())
    if all_fixed:
        implementation = "FULL_FIX"
    elif any_fixed:
        implementation = "PARTIAL_FIX"
    elif incidental_mechanism:
        implementation = "CLASS_HARDENING"
    else:
        implementation = "NONE"

    if any(v == "product-semantics" for v in values.values()):
        ticket = "PRODUCT_DECISION_NEEDED"
    elif any(v in {"unresolved", "class-hardening-only"} for v in values.values()):
        ticket = "OPEN"
    elif all_fixed:
        ticket = "RESOLVED"
    else:
        ticket = "DISPOSITION_COMPLETE"

    approval = {
        "FULL_FIX": "ship-covered-symptoms",
        "PARTIAL_FIX": "ship-covered-symptoms",
        "CLASS_HARDENING": "ship-hardening-only",
        "NONE": "none",
    }[implementation]

    by_source: dict[str, dict[str, bool]] = {}
    for sid, descendants in shape["source_to_effective"].items():
        states = [values[eid] for eid in descendants]
        by_source[sid] = {
            "all_fixed": all(x == "fixed" for x in states),
            "any_fixed": any(x == "fixed" for x in states),
            "any_product_decision": any(x == "product-semantics" for x in states),
            "any_open": any(x in {"unresolved", "class-hardening-only", "product-semantics"} for x in states),
            "all_disposition_complete": all(x in {"fixed", "by-design", "separate-ticket"} for x in states),
        }

    return {
        "schema_version": 2,
        "implementation_result": implementation,
        "ticket_disposition": ticket,
        "approval_scope": approval,
        "ticket_closure_allowed": ticket == "RESOLVED",
        "open_effective_ids": [eid for eid, value in values.items() if value not in {"fixed", "by-design", "separate-ticket"}],
        "source_rollups": by_source,
    }


def validate_smoke_readiness(artifacts_dir: Path, *, allow_open_ticket: bool = False) -> dict[str, Any]:
    """Fail closed before PR shipment when final evidence still contradicts the ticket.

    The boot smoke only proves the stack is reachable. The in-app matrix and
    immutable symptom classification decide whether the user-facing bug is
    actually shippable. A product-failing auto row is a deterministic blocker;
    harness/infrastructure/unknown rows stay visible for human judgment.
    """
    classification = load_json(safe_artifact_path(artifacts_dir, "fix-classification.json"))
    open_ids = classification.get("open_effective_ids") or []
    if classification.get("ticket_closure_allowed") is not True and not allow_open_ticket:
        raise ContractError(
            "ticket disposition is not RESOLVED; open symptoms require explicit residual acceptance "
            f"before smoke approval/ship (ticket={classification.get('ticket_disposition')} "
            f"implementation={classification.get('implementation_result')} open_effective_ids={open_ids})"
        )

    matrix_path = artifacts_dir / "smoke-matrix.json"
    if not matrix_path.exists():
        return {"auto_rows": 0, "product_failures": []}
    matrix = load_json(safe_artifact_path(artifacts_dir, "smoke-matrix.json"))
    rows = matrix.get("rows")
    if not isinstance(rows, list):
        raise ContractError("smoke-matrix.json rows must be a list")
    product_failures: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or row.get("kind") != "auto":
            continue
        result = row.get("result")
        failure_class = row.get("failure_class")
        if result not in {"pass", "fail", "not-run"}:
            raise ContractError(f"auto smoke row {index + 1} result out of enum")
        if result == "fail" and failure_class == "product":
            product_failures.append(str(row.get("id") or row.get("spec_title") or index + 1))
        if result != "pass":
            if failure_class not in {"product", "harness", "infrastructure", "unknown"}:
                raise ContractError(f"auto smoke row {index + 1} missing failure_class")
            if not str(row.get("observed") or "").strip():
                raise ContractError(f"auto smoke row {index + 1} missing observed evidence")
    if product_failures:
        raise ContractError("auto smoke product failure blocks approval/ship: " + ",".join(product_failures))
    return {
        "auto_rows": sum(1 for row in rows if isinstance(row, dict) and row.get("kind") == "auto"),
        "product_failures": product_failures,
    }


def safe_artifact_path(root: Path, relative: str, *, max_bytes: int = 8 * 1024 * 1024) -> Path:
    root = root.resolve()
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise ContractError(f"unsafe artifact path: {relative}")
    path = root.joinpath(candidate)
    try:
        info = path.lstat()
    except OSError as exc:
        raise ContractError(f"artifact path unavailable: {relative}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ContractError(f"artifact path must be a regular non-symlink file: {relative}")
    try:
        path.resolve().relative_to(root)
    except ValueError as exc:
        raise ContractError(f"artifact path escapes root: {relative}") from exc
    if info.st_size > max_bytes:
        raise ContractError(f"artifact path exceeds size limit: {relative}")
    return path


def validate_causal_coverage(
    ledger: dict[str, Any],
    dispositions_doc: dict[str, Any],
    coverage_doc: dict[str, Any],
    chain: dict[str, Any],
) -> dict[str, Any]:
    shape = validate_ledger(ledger, require_sealed=True)
    revision = ledger["revision"]
    parent = chain.get("parent_run_id")
    root_source_ids = chain.get("root_source_ids")
    if not isinstance(root_source_ids, list) or not root_source_ids or not all(isinstance(x, str) and x for x in root_source_ids):
        raise ContractError("bugfix chain root_source_ids must be a non-empty string list")
    if chain.get("ledger_root_hash") != ledger.get("ledger_root_hash"):
        raise ContractError("bugfix chain ledger_root_hash does not match symptoms ledger")
    dropped = sorted(set(root_source_ids) - set(shape["source_ids"]))
    lineage_errors = []
    if revision > 1 and not parent:
        lineage_errors.append("missing parent lineage")
    if dropped:
        lineage_errors.append("dropped source symptoms: " + ",".join(dropped))
    if lineage_errors:
        raise ContractError("; ".join(lineage_errors))

    rows = dispositions_doc.get("dispositions") if isinstance(dispositions_doc, dict) else None
    if not isinstance(rows, list):
        raise ContractError("symptom-dispositions.json needs dispositions list")
    dispositions = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("symptom_id"):
            raise ContractError("malformed symptom disposition")
        sid = row["symptom_id"]
        if sid in dispositions:
            raise ContractError(f"duplicate symptom disposition: {sid}")
        dispositions[sid] = row

    coverage_rows = coverage_doc.get("coverage") if isinstance(coverage_doc, dict) else None
    if not isinstance(coverage_rows, list):
        raise ContractError("causal-coverage.json needs coverage list")
    coverage = {}
    for row in coverage_rows:
        if not isinstance(row, dict) or not row.get("symptom_id"):
            raise ContractError("malformed causal coverage row")
        sid = row["symptom_id"]
        if sid in coverage:
            raise ContractError(f"duplicate causal coverage row: {sid}")
        coverage[sid] = row

    expected = set(shape["effective_ids"])
    if set(dispositions) != expected or set(coverage) != expected:
        raise ContractError(
            "effective coverage mismatch "
            f"dispositions_missing={sorted(expected-set(dispositions))} "
            f"coverage_missing={sorted(expected-set(coverage))}"
        )
    for eid in shape["effective_ids"]:
        disposition = dispositions[eid].get("disposition")
        row = coverage[eid]
        if disposition == "fixed":
            if row.get("occurrence_attributed") is not True:
                raise ContractError(f"fixed symptom {eid} lacks occurrence attribution")
            if not row.get("cause_id") or not row.get("planned_diff") or not row.get("red_test"):
                raise ContractError(f"fixed symptom {eid} lacks cause/diff/RED coverage")
            if row.get("counterfactual_user_visible") is not True:
                raise ContractError(f"fixed symptom {eid} lacks positive user-visible counterfactual")

    proof = {"incidental_finding": any(r.get("disposition") == "class-hardening-only" for r in rows)}
    classified = classify(ledger, dispositions, incidental_mechanism=proof["incidental_finding"])
    return {"classification": classified, "source_ids": shape["source_ids"], "effective_ids": shape["effective_ids"]}


PROOF_INPUTS = (
    "symptoms.json",
    "rca.md",
    "causal-chain.json",
    "bugfix-chain.json",
    "hypotheses.json",
    "evidence-manifest.json",
    "evidence-provenance.json",
    "proof-assessment.json",
    "chain-verify.json",
    "chain-assessment.json",
    "experiment.json",
    "experiment-result.json",
    "experiment-assessment.json",
    "proof-recovery.json",
)

APPROVAL_INPUTS = (
    "symptom-dispositions.json",
    "causal-coverage.json",
    "fix-plan.json",
    "failing-test.json",
    "verify.json",
    "files-allowlist.json",
)

LITE_APPROVAL_INPUTS = (
    "symptoms.json", "rca.md", "causal-chain.json", "hypotheses.json",
    "proof-assessment.json", "symptom-dispositions.json", "causal-coverage.json",
    "fix-plan.json", "failing-test.json", "files-allowlist.json",
    "fix-classification.json", "evidence-manifest.json", "debug-phase.json",
    "boundary-trace.json", "pattern-comparison.json",
)


def _chain_identity(chain: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": _nonempty(chain.get("provider"), "bugfix-chain.provider"),
        "chain_id": _nonempty(chain.get("logical_chain_id"), "bugfix-chain.logical_chain_id"),
        "run_id": _nonempty(chain.get("current_run_id") or chain.get("run_id"), "bugfix-chain.current_run_id"),
        "root_run_id": chain.get("root_run_id"),
        "parent_run_id": chain.get("parent_run_id"),
    }


def _manifest_hash_base(manifest: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in manifest.items() if k not in {"semantic_hash", "written_at"}}


def finalize_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    manifest = dict(manifest)
    manifest["semantic_hash"] = canonical_hash(_manifest_hash_base(manifest))
    return manifest


def write_json_atomic(path: Path, value: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def validate_proof_relationships(artifacts_dir: Path, effective_ids: list[str]) -> None:
    verify = load_json(safe_artifact_path(artifacts_dir, "chain-verify.json"))
    chain = load_json(safe_artifact_path(artifacts_dir, "chain-assessment.json"))
    experiment = load_json(safe_artifact_path(artifacts_dir, "experiment.json"))
    result = load_json(safe_artifact_path(artifacts_dir, "experiment-result.json"))
    assessment = load_json(safe_artifact_path(artifacts_dir, "experiment-assessment.json"))
    recovery = load_json(safe_artifact_path(artifacts_dir, "proof-recovery.json"))
    verify_verdict = (verify.get("comparison") or {}).get("verdict")
    if verify_verdict not in {"agree", "conflict", "cannot_determine"}:
        raise ContractError("chain-verify comparison verdict out of enum")
    if chain.get("verdict") != verify_verdict:
        raise ContractError("chain assessment does not match blind verifier verdict")
    if chain.get("active_symptom_ids") != effective_ids:
        raise ContractError("chain assessment active symptoms do not match the sealed ledger")
    experiment_verdict = assessment.get("verdict")
    if experiment_verdict not in {"confirm", "conflict", "ambiguous", "degraded", "skipped"}:
        raise ContractError("experiment assessment verdict out of enum")
    result_status = (result.get("result") or {}).get("status")
    if result_status == "timeout" and experiment_verdict != "degraded":
        raise ContractError("timed-out experiment must be assessed as degraded")
    if experiment.get("skipped") is True and experiment_verdict != "skipped":
        raise ContractError("skipped experiment must have a skipped assessment")
    if recovery.get("state") != "CONVERGED":
        raise ContractError("proof manifest requires CONVERGED proof recovery")
    if recovery.get("active_symptom_ids") != effective_ids:
        raise ContractError("proof recovery active symptoms do not match the sealed ledger")
    if recovery.get("chain_verdict") != chain.get("verdict"):
        raise ContractError("proof recovery chain verdict is stale")
    if recovery.get("experiment_verdict") != experiment_verdict:
        raise ContractError("proof recovery experiment verdict is stale")


def build_proof_manifest(artifacts_dir: Path) -> dict[str, Any]:
    ledger = load_json(safe_artifact_path(artifacts_dir, "symptoms.json"))
    shape = validate_ledger(ledger, require_sealed=True)
    chain = load_json(safe_artifact_path(artifacts_dir, "bugfix-chain.json"))
    identity = _chain_identity(chain)
    validate_proof_relationships(artifacts_dir, shape["effective_ids"])
    artifact_hashes = {name: hash_artifact(artifacts_dir, name) for name in PROOF_INPUTS}
    manifest = {
        "schema_version": 1,
        "manifest_type": "proof",
        **identity,
        "ledger_root_hash": ledger.get("ledger_root_hash"),
        "ledger_revision_hash": ledger.get("ledger_revision_hash"),
        "source_ids": shape["source_ids"],
        "effective_ids": shape["effective_ids"],
        "artifact_hashes": artifact_hashes,
    }
    return finalize_manifest(manifest)


def build_approval_manifest(artifacts_dir: Path) -> dict[str, Any]:
    ledger = load_json(safe_artifact_path(artifacts_dir, "symptoms.json"))
    shape = validate_ledger(ledger, require_sealed=True)
    chain = load_json(safe_artifact_path(artifacts_dir, "bugfix-chain.json"))
    proof_manifest = load_json(safe_artifact_path(artifacts_dir, "proof-manifest.json"))
    validate_current_manifest(artifacts_dir, "proof")
    dispositions_doc = load_json(safe_artifact_path(artifacts_dir, "symptom-dispositions.json"))
    coverage_doc = load_json(safe_artifact_path(artifacts_dir, "causal-coverage.json"))
    coverage = validate_causal_coverage(ledger, dispositions_doc, coverage_doc, chain)
    validate_systematic_debugging(artifacts_dir)
    artifact_hashes = {name: hash_artifact(artifacts_dir, name) for name in APPROVAL_INPUTS}
    manifest = {
        "schema_version": 1,
        "manifest_type": "approval",
        **_chain_identity(chain),
        "proof_manifest_hash": proof_manifest["semantic_hash"],
        "ledger_root_hash": ledger.get("ledger_root_hash"),
        "ledger_revision_hash": ledger.get("ledger_revision_hash"),
        "source_ids": shape["source_ids"],
        "effective_ids": shape["effective_ids"],
        "artifact_hashes": artifact_hashes,
        "classification": coverage["classification"],
    }
    return finalize_manifest(manifest)


def build_lite_approval_manifest(artifacts_dir: Path) -> dict[str, Any]:
    ledger = load_json(safe_artifact_path(artifacts_dir, "symptoms.json"))
    shape = validate_ledger(ledger, require_sealed=True)
    chain = load_json(safe_artifact_path(artifacts_dir, "bugfix-chain.json"))
    dispositions = load_json(safe_artifact_path(artifacts_dir, "symptom-dispositions.json"))
    coverage = load_json(safe_artifact_path(artifacts_dir, "causal-coverage.json"))
    classification = validate_causal_coverage(ledger, dispositions, coverage, chain)["classification"]
    validate_systematic_debugging(artifacts_dir)
    recorded_classification = load_json(safe_artifact_path(artifacts_dir, "fix-classification.json"))
    if recorded_classification != classification:
        raise ContractError("lite fix classification is stale")
    manifest = {
        "schema_version": 1,
        "manifest_type": "lite-approval",
        **_chain_identity(chain),
        "ledger_root_hash": ledger["ledger_root_hash"],
        "ledger_revision_hash": ledger["ledger_revision_hash"],
        "source_ids": shape["source_ids"],
        "effective_ids": shape["effective_ids"],
        "artifact_hashes": {
            name: hash_artifact(artifacts_dir, name) for name in LITE_APPROVAL_INPUTS
        },
        "classification": classification,
    }
    return finalize_manifest(manifest)


def validate_current_manifest(artifacts_dir: Path, kind: str) -> dict[str, Any]:
    if kind == "proof":
        path = safe_artifact_path(artifacts_dir, "proof-manifest.json")
        recorded = load_json(path)
        current = build_proof_manifest(artifacts_dir)
    elif kind == "approval":
        path = safe_artifact_path(artifacts_dir, "approval-manifest.json")
        recorded = load_json(path)
        current = build_approval_manifest(artifacts_dir)
    elif kind == "lite-approval":
        path = safe_artifact_path(artifacts_dir, "lite-approval-manifest.json")
        recorded = load_json(path)
        current = build_lite_approval_manifest(artifacts_dir)
    else:
        raise ContractError(f"unknown manifest kind: {kind}")
    if recorded.get("semantic_hash") != current.get("semantic_hash"):
        raise ContractError(
            f"stale {kind} manifest recorded={recorded.get('semantic_hash')} current={current.get('semantic_hash')}"
        )
    if recorded.get("artifact_hashes") != current.get("artifact_hashes"):
        raise ContractError(f"stale {kind} manifest artifact hashes")
    return recorded


def validate_external_attestation(artifacts_dir: Path, kind: str, seal_path: Path, role: str) -> dict[str, Any]:
    """Validate a controller-owned seal without pretending it is cryptography.

    U8 wires the private controller/HMAC authority. Until then, this function is
    deliberately strict about source location and file mode, and it only accepts
    an explicit external seal path. An attestation copied into the artifact
    directory is data, not authority.
    """
    ad = artifacts_dir.resolve()
    seal = seal_path.resolve()
    try:
        seal.relative_to(ad)
    except ValueError:
        pass
    else:
        raise ContractError(f"{kind} attestation must be external to artifacts")
    try:
        info = seal_path.lstat()
    except OSError as exc:
        raise ContractError(f"{kind} attestation seal unavailable: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ContractError(f"{kind} attestation seal must be a regular non-symlink file")
    if info.st_mode & 0o077:
        raise ContractError(f"{kind} attestation seal must not be group/world accessible")
    doc = load_json(seal)
    manifest = validate_current_manifest(artifacts_dir, kind)
    expected_role = {"proof": "blind-verifier", "approval": "final-critic"}[kind]
    if role != expected_role:
        raise ContractError(f"{kind} attestation role mismatch expected={expected_role} got={role}")
    checks = {
        "schema_version": 1,
        "authority": "controller",
        "manifest_type": kind,
        "role": expected_role,
        "provider": manifest["provider"],
        "chain_id": manifest["chain_id"],
        "run_id": manifest["run_id"],
        "manifest_hash": manifest["semantic_hash"],
    }
    for key, expected in checks.items():
        if doc.get(key) != expected:
            raise ContractError(f"{kind} attestation {key} mismatch")
    _sha256_hex(doc.get("authority_mac"), f"{kind} attestation authority_mac")
    if doc.get("verdict") not in {"ACCEPT", "PASS", "APPROVED"}:
        raise ContractError(f"{kind} attestation verdict is not approving")
    return doc


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read JSON {path.name}: {exc}") from exc


def fail(message: str) -> NoReturn:
    print(f"BUGFIX_CONTRACT=FAIL {message}")
    raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("validate-ledger")
    p.add_argument("artifacts", type=Path)
    p = sub.add_parser("seal-ledger")
    p.add_argument("artifacts", type=Path)
    p = sub.add_parser("bind-chain-ledger")
    p.add_argument("artifacts", type=Path)
    p = sub.add_parser("normalize-gather-more")
    p.add_argument("artifacts", type=Path)
    p = sub.add_parser("classify")
    p.add_argument("artifacts", type=Path)
    p = sub.add_parser("validate-causal-coverage")
    p.add_argument("--artifacts", type=Path, required=True)
    p = sub.add_parser("write-proof-manifest")
    p.add_argument("--artifacts", type=Path, required=True)
    p = sub.add_parser("write-approval-manifest")
    p.add_argument("--artifacts", type=Path, required=True)
    p = sub.add_parser("write-lite-approval-manifest")
    p.add_argument("--artifacts", type=Path, required=True)
    p = sub.add_parser("validate-current-manifest")
    p.add_argument("--artifacts", type=Path, required=True)
    p.add_argument("--kind", choices=("proof", "approval", "lite-approval"), required=True)
    p = sub.add_parser("validate-smoke-readiness")
    p.add_argument("--artifacts", type=Path, required=True)
    p.add_argument("--allow-open-ticket", action="store_true")
    p = sub.add_parser("validate-attestation")
    p.add_argument("--artifacts", type=Path, required=True)
    p.add_argument("--kind", choices=("proof", "approval"), required=True)
    p.add_argument("--seal", type=Path, required=True)
    p.add_argument("--role", choices=("blind-verifier", "final-critic"), required=True)
    args = parser.parse_args()
    try:
        ad = args.artifacts.resolve()
        ledger_path = safe_artifact_path(ad, "symptoms.json")
        ledger = load_json(ledger_path)
        if args.action == "normalize-gather-more":
            debug = load_json(safe_artifact_path(ad, "debug-phase.json"))
            plan_path = safe_artifact_path(ad, "fix-plan.json")
            plan = load_json(plan_path)
            if debug.get("reproduction_status") == "gather-more":
                if any((plan.get("approach"), plan.get("fix_site"), plan.get("files"))):
                    write_json_atomic(ad / "discarded-conditional-fix-plan.json", plan)
                normalized = {
                    "approach": "", "fix_site": "", "files": [], "risks": [],
                    "alternatives": [], "blocked_reason": "gather-more",
                }
                write_json_atomic(plan_path, normalized)
                hypotheses_path = safe_artifact_path(ad, "hypotheses.json")
                hypotheses = load_json(hypotheses_path)
                proof_path = safe_artifact_path(ad, "proof-assessment.json")
                proof = load_json(proof_path)
                selected = str(proof.get("selected_hypothesis_id", ""))
                selected_number = selected[1:] if selected.startswith("H") else selected
                matching = [item for item in hypotheses if str(item.get("id")) in {selected, selected_number}]
                if len(matching) != 1:
                    raise ContractError("gather-more selected hypothesis does not resolve uniquely")
                selected_item = matching[0]
                for item in hypotheses:
                    if item.get("status") == "open" and item is not selected_item:
                        item["status"] = "queued"
                proof["selected_hypothesis_id"] = str(selected_item["id"])
                write_json_atomic(hypotheses_path, hypotheses)
                write_json_atomic(proof_path, proof)
                print("BUGFIX_GATHER_MORE=NORMALIZED implementation_plan=blocked")
            else:
                print("BUGFIX_GATHER_MORE=UNCHANGED reproduction_status=" + str(debug.get("reproduction_status")))
            return
        if args.action == "write-proof-manifest":
            manifest = build_proof_manifest(ad)
            write_json_atomic(ad / "proof-manifest.json", manifest)
            print(f"BUGFIX_PROOF_MANIFEST=OK hash={manifest['semantic_hash']}")
            return
        if args.action == "write-approval-manifest":
            manifest = build_approval_manifest(ad)
            write_json_atomic(ad / "approval-manifest.json", manifest)
            write_json_atomic(ad / "fix-classification.json", manifest["classification"])
            print(
                "BUGFIX_APPROVAL_MANIFEST=OK "
                f"hash={manifest['semantic_hash']} "
                f"implementation={manifest['classification']['implementation_result']} "
                f"ticket={manifest['classification']['ticket_disposition']}"
            )
            return
        if args.action == "write-lite-approval-manifest":
            manifest = build_lite_approval_manifest(ad)
            write_json_atomic(ad / "lite-approval-manifest.json", manifest)
            print(f"BUGFIX_LITE_APPROVAL_MANIFEST=OK hash={manifest['semantic_hash']}")
            return
        if args.action == "validate-current-manifest":
            manifest = validate_current_manifest(ad, args.kind)
            print(f"BUGFIX_MANIFEST_CURRENT=OK kind={args.kind} hash={manifest['semantic_hash']}")
            return
        if args.action == "validate-smoke-readiness":
            allow = args.allow_open_ticket or (ad / "accept-residuals.txt").is_file()
            result = validate_smoke_readiness(ad, allow_open_ticket=allow)
            print(
                "BUGFIX_SMOKE_READY=OK "
                f"auto_rows={result['auto_rows']} allow_open_ticket={str(allow).lower()}"
            )
            return
        if args.action == "validate-attestation":
            seal = validate_external_attestation(ad, args.kind, args.seal, args.role)
            print(f"BUGFIX_ATTESTATION=OK kind={args.kind} role={seal['role']} hash={seal['manifest_hash']}")
            return
        if args.action == "validate-ledger":
            result = validate_ledger(ledger, artifacts_dir=ad)
            print(f"BUGFIX_LEDGER=OK sources={len(result['source_ids'])} effective={len(result['effective_ids'])} hash={result['ledger_hash']}")
            return
        if args.action == "seal-ledger":
            # Intake authors content, not hashes. The trusted gate derives the
            # revision-1 root/revision identities immediately before sealing.
            if ledger.get("revision") == 1:
                ledger["ledger_root_hash"] = canonical_hash(ledger.get("source_symptoms"))
            ledger["ledger_revision_hash"] = None
            ledger["ledger_revision_hash"] = canonical_hash(ledger)
            temporary_ledger = ledger_path.with_suffix(".tmp")
            temporary_ledger.write_text(json.dumps(ledger, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(temporary_ledger, ledger_path)
            result = validate_ledger(ledger, artifacts_dir=ad)
            seal = {
                "schema_version": 2,
                "ledger_root_hash": ledger["ledger_root_hash"],
                "ledger_revision_hash": ledger["ledger_revision_hash"],
                "source_count": len(result["source_ids"]),
                "effective_count": len(result["effective_ids"]),
            }
            target = ad / "symptoms.seal.json"
            temporary = target.with_suffix(".tmp")
            temporary.write_text(json.dumps(seal, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, target)
            print(f"BUGFIX_LEDGER_SEAL=OK root={seal['ledger_root_hash']} revision={seal['ledger_revision_hash']}")
            return
        if args.action == "bind-chain-ledger":
            result = validate_ledger(ledger, artifacts_dir=ad, require_sealed=True)
            chain_path = safe_artifact_path(ad, "bugfix-chain.json")
            chain = load_json(chain_path)
            existing_ids = chain.get("root_source_ids")
            if existing_ids not in (None, [], result["source_ids"]):
                raise ContractError("bugfix chain root_source_ids already conflict with sealed ledger")
            existing_root = chain.get("ledger_root_hash")
            if existing_root not in (None, ledger["ledger_root_hash"]):
                raise ContractError("bugfix chain ledger_root_hash already conflicts with sealed ledger")
            chain["root_source_ids"] = result["source_ids"]
            chain["ledger_root_hash"] = ledger["ledger_root_hash"]
            chain["ledger_revision_hash"] = ledger["ledger_revision_hash"]
            write_json_atomic(chain_path, chain)
            print(
                "BUGFIX_CHAIN_LEDGER=OK "
                f"sources={','.join(result['source_ids'])} root={ledger['ledger_root_hash']}"
            )
            return
        dispositions = load_json(safe_artifact_path(ad, "symptom-dispositions.json"))
        if args.action == "validate-causal-coverage":
            coverage = load_json(safe_artifact_path(ad, "causal-coverage.json"))
            chain = load_json(safe_artifact_path(ad, "bugfix-chain.json"))
            try:
                result = validate_causal_coverage(ledger, dispositions, coverage, chain)
                validate_systematic_debugging(ad)
            except ContractError as exc:
                print(f"BUGFIX_COVERAGE=FAIL {exc}")
                raise SystemExit(1)
            print(
                "BUGFIX_COVERAGE=OK "
                f"sources={len(result['source_ids'])} effective={len(result['effective_ids'])} "
                f"implementation={result['classification']['implementation_result']}"
            )
            return
        if isinstance(dispositions, dict) and "dispositions" in dispositions:
            dispositions = {x["symptom_id"]: x for x in dispositions["dispositions"]}
        proof = load_json(safe_artifact_path(ad, "proof-assessment.json"))
        result = classify(ledger, dispositions, incidental_mechanism=bool(proof.get("incidental_finding")))
        target = ad / "fix-classification.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, target)
        print(
            "BUGFIX_CLASSIFICATION=OK "
            f"implementation={result['implementation_result']} ticket={result['ticket_disposition']} "
            f"closure={str(result['ticket_closure_allowed']).lower()}"
        )
    except ContractError as exc:
        fail(str(exc))


if __name__ == "__main__":
    main()
