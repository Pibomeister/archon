#!/usr/bin/env python3
import hashlib
import importlib.util
import sys
import unittest
from pathlib import Path


SETUP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SETUP))

rp_spec = importlib.util.spec_from_file_location("review_policy", SETUP / "review_policy.py")
assert rp_spec and rp_spec.loader
rp = importlib.util.module_from_spec(rp_spec)
rp_spec.loader.exec_module(rp)


BASE = "a" * 40
HEAD = "b" * 40
MID = "c" * 40
FIX = "4" * 40
SHA = "d" * 64


def candidate():
    return {"repo": "api", "base": BASE, "head": HEAD}


def coverage(*responsibilities, reviewer_id="reviewer-1", cand=None, coverage_id="delta"):
    return {
        "policy": rp.POLICY,
        "schema_version": rp.SCHEMA_VERSION,
        "candidate": cand or candidate(),
        "coverage_id": coverage_id,
        "responsibilities": list(responsibilities),
        "source_digest": SHA,
        "context_digest": "e" * 64,
        "reviewer_id": reviewer_id,
        "model": "gpt-5.6-sol",
        "reasoning_effort": "medium",
        "status": "complete",
    }


def receipt(command="bun test claims.spec.ts", exit_status=0):
    return {
        "policy": rp.POLICY,
        "schema_version": rp.SCHEMA_VERSION,
        "candidate": candidate(),
        "command": command,
        "exit_status": exit_status,
        "source_tree_digest": SHA,
        "dependencies_digest": "e" * 64,
        "configuration_digest": "f" * 64,
        "environment_fingerprint": "1" * 64,
        "retained_output_digest": "2" * 64,
    }


def convergence_plan(**change):
    payload = {"mode": "delta", "candidate": candidate()}
    payload.update(change)
    plan = rp.required_review_plan(payload)
    plan["required_receipts"] = ["bun test claims.spec.ts"]
    plan["receipt_expectations"] = {
        "bun test claims.spec.ts": {
            "source_tree_digest": SHA,
            "dependencies_digest": "e" * 64,
            "configuration_digest": "f" * 64,
            "environment_fingerprint": "1" * 64,
            "retained_output_digest": "2" * 64,
        }
    }
    plan["segment_requirements"] = [{
        "coverage_id": "baseline",
        "type": "initial",
        "candidate": candidate(),
        "author_id": "baseline-author",
        "source_digest": SHA,
        "context_digest": "e" * 64,
    }]
    return plan


def delta_plan(**change):
    payload = {"mode": "delta", "candidate": candidate()}
    payload.update(change)
    plan = rp.required_review_plan(payload)
    plan["required_receipts"] = ["bun test claims.spec.ts"]
    plan["receipt_expectations"] = {
        "bun test claims.spec.ts": {
            "source_tree_digest": SHA,
            "dependencies_digest": "e" * 64,
            "configuration_digest": "f" * 64,
            "environment_fingerprint": "1" * 64,
            "retained_output_digest": "2" * 64,
        }
    }
    plan["segment_requirements"] = [{
        "coverage_id": "baseline",
        "type": "initial",
        "candidate": {"repo": "api", "base": BASE, "head": MID},
        "author_id": "baseline-author",
        "source_digest": SHA,
        "context_digest": "e" * 64,
    }, {
        "coverage_id": "delta",
        "type": "delta",
        "candidate": {"repo": "api", "base": MID, "head": HEAD},
        "author_id": "fixer-1",
        "risk_areas": payload.get("risk_areas", []),
        "cross_store": payload.get("cross_store", False),
        "impact_tooling_missing": payload.get("impact_tooling_missing", False),
        "source_digest": SHA,
        "context_digest": "e" * 64,
    }]
    return plan


def closed_finding(finding_id="F-1"):
    return {
        "finding_id": finding_id,
        "violated_invariant": "claims must stay scoped to the requester",
        "affected_paths": ["src/claims.ts"],
        "severity": "P1",
        "evidence": "reviewer found malformed-claim disclosure",
        "owning_repository": "api",
        "status": "closed",
        "repair_commit": FIX,
        "repair_author_id": "fixer-1",
        "regression_evidence": {
            "failure": "malformed claim disclosed another recipient",
            "executed": True,
            "receipt": {"command": "bun test claims.spec.ts", "exit_status": 0},
        },
        "independent_closure": {"reviewer_id": "reviewer-2", "status": "closed", "repair_commit": FIX},
    }


class RiskDeltaReviewPolicy(unittest.TestCase):
    def test_required_plan_batches_all_responsibilities_without_dropping_specialists(self):
        plan = rp.required_review_plan({
            "mode": "delta",
            "candidate": candidate(),
            "production_changed": True,
            "cross_store": True,
            "impact_tooling_missing": True,
            "public_contract_changed": True,
            "risk_areas": [
                "authorization",
                "transactions",
                "query_volume",
                "cli_behavior",
            ],
        })

        self.assertEqual("affected-subsystem", plan["scope"])
        self.assertEqual(
            [
                "independent_repair_review",
                "security",
                "data_reliability",
                "performance",
                "cli",
                "caller_reader_trace",
            ],
            plan["required_responsibilities"],
        )
        self.assertEqual(plan["required_responsibilities"], [item for batch in plan["review_batches"] for item in batch])
        self.assertTrue(all(len(batch) <= 3 for batch in plan["review_batches"]))

    def test_unbounded_impact_expands_to_full_candidate(self):
        plan = rp.required_review_plan({
            "mode": "delta",
            "candidate": candidate(),
            "impact_unbounded": True,
        })

        self.assertEqual("full-candidate", plan["scope"])

    def test_initial_review_is_full_candidate_scope(self):
        plan = rp.required_review_plan({"mode": "initial", "candidate": candidate()})

        self.assertEqual("full-candidate", plan["scope"])

    def test_coverage_requires_exact_candidate_and_independent_reviewer(self):
        with self.assertRaisesRegex(rp.ReviewPolicyError, "exact candidate"):
            rp.validate_coverage_record(
                coverage("independent_repair_review", cand={"repo": "api", "base": BASE, "head": "f" * 40}),
                candidate(),
                "author-1",
            )
        with self.assertRaisesRegex(rp.ReviewPolicyError, "different from the author"):
            rp.validate_coverage_record(coverage("independent_repair_review", reviewer_id="author-1"), candidate(), "author-1")

    def test_findings_ledger_requires_ids_regression_and_independent_closure(self):
        with self.assertRaisesRegex(rp.ReviewPolicyError, "duplicate finding id"):
            rp.validate_findings_ledger([closed_finding("F-1"), closed_finding("F-1")])

        finding = closed_finding()
        finding.pop("regression_evidence")
        with self.assertRaisesRegex(rp.ReviewPolicyError, "regression evidence"):
            rp.validate_findings_ledger([finding])

        finding = closed_finding()
        finding["regression_evidence"] = {}
        with self.assertRaisesRegex(rp.ReviewPolicyError, "regression evidence is empty"):
            rp.validate_findings_ledger([finding])

        finding = closed_finding()
        finding.pop("repair_author_id")
        with self.assertRaisesRegex(rp.ReviewPolicyError, "repair author"):
            rp.validate_findings_ledger([finding])

        finding = closed_finding()
        finding["independent_closure"]["reviewer_id"] = "fixer-1"
        with self.assertRaisesRegex(rp.ReviewPolicyError, "not independent"):
            rp.validate_findings_ledger([finding])

        finding = closed_finding()
        finding["severity"] = "typo"
        with self.assertRaisesRegex(rp.ReviewPolicyError, "invalid severity"):
            rp.validate_findings_ledger([finding])

        finding = closed_finding()
        finding["independent_closure"]["repair_commit"] = "5" * 40
        with self.assertRaisesRegex(rp.ReviewPolicyError, "repair commit"):
            rp.validate_findings_ledger([finding])

        finding = closed_finding()
        finding["regression_evidence"]["executed"] = False
        with self.assertRaisesRegex(rp.ReviewPolicyError, "must be executed"):
            rp.validate_findings_ledger([finding])

        finding = closed_finding()
        finding["status"] = "open"
        finding["mandatory"] = False
        self.assertEqual([finding], rp.validate_findings_ledger([finding]))

    def test_unresolved_mandatory_findings_block_convergence(self):
        plan = convergence_plan()
        open_finding = closed_finding()
        open_finding["status"] = "open"

        with self.assertRaisesRegex(rp.ReviewPolicyError, "unresolved mandatory findings: F-1"):
            rp.validate_convergence(
                candidate(),
                plan,
                [
                    coverage("correctness_contracts", reviewer_id="reviewer-0", coverage_id="baseline"),
                    coverage("testing_maintainability_standards", reviewer_id="reviewer-2", coverage_id="baseline"),
                ],
                [open_finding],
                [receipt()],
            )

    def test_malformed_normalization_retains_raw_output_and_fails_closed_after_one_retry(self):
        raw = "P1: malformed reviewer text"
        source = {"raw_output": raw, "malformed": True, "blocker_ids": ["B-1"]}
        normalized = {
            "source_raw_output_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            "findings": [{
                "finding_id": "F-1",
                "source_blocker_id": "B-1",
                "violated_invariant": "claims are scoped",
                "affected_paths": ["src/claims.ts"],
                "severity": "P1",
                "evidence": "raw reviewer blocker",
                "owning_repository": "api",
                "status": "open",
            }],
        }

        with self.assertRaisesRegex(rp.ReviewPolicyError, "requires one format-only repair"):
            rp.validate_normalized_blockers(source, normalized, 0)
        self.assertEqual(normalized["findings"], rp.validate_normalized_blockers(source, normalized, 1))
        with self.assertRaisesRegex(rp.ReviewPolicyError, "retry limit"):
            rp.validate_normalized_blockers(source, normalized, 2)

    def test_normalized_blockers_must_account_for_every_source_blocker(self):
        raw = "B-1 and B-2"
        source = {"raw_output": raw, "malformed": False, "blocker_ids": ["B-1", "B-2"]}
        normalized = {
            "source_raw_output_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            "findings": [{
                "finding_id": "F-1",
                "source_blocker_id": "B-1",
                "violated_invariant": "claims are scoped",
                "affected_paths": ["src/claims.ts"],
                "severity": "P1",
                "evidence": "raw reviewer blocker",
                "owning_repository": "api",
                "status": "open",
            }],
        }

        with self.assertRaisesRegex(rp.ReviewPolicyError, "dropped source blockers: B-2"):
            rp.validate_normalized_blockers(source, normalized, 1)

        normalized["findings"][0]["status"] = "advisory"
        source["blocker_ids"] = ["B-1"]
        with self.assertRaisesRegex(rp.ReviewPolicyError, "cannot be advisory"):
            rp.validate_normalized_blockers(source, normalized, 1)

        normalized["findings"][0]["status"] = "bogus"
        with self.assertRaisesRegex(rp.ReviewPolicyError, "invalid status"):
            rp.validate_normalized_blockers(source, normalized, 1)

        normalized["findings"][0].pop("finding_id")
        with self.assertRaisesRegex(rp.ReviewPolicyError, "finding id"):
            rp.validate_normalized_blockers(source, normalized, 1)

    def test_receipt_reuse_requires_matching_exact_fingerprints(self):
        test_receipt = receipt()
        rp.validate_verification_receipt(test_receipt, candidate(), {
            "command": "bun test claims.spec.ts",
            "source_tree_digest": SHA,
            "dependencies_digest": "e" * 64,
            "configuration_digest": "f" * 64,
            "environment_fingerprint": "1" * 64,
            "retained_output_digest": "2" * 64,
        })
        with self.assertRaisesRegex(rp.ReviewPolicyError, "dependencies_digest is stale"):
            rp.validate_verification_receipt(test_receipt, candidate(), {
                "command": "bun test claims.spec.ts",
                "source_tree_digest": SHA,
                "dependencies_digest": "3" * 64,
                "configuration_digest": "f" * 64,
                "environment_fingerprint": "1" * 64,
                "retained_output_digest": "2" * 64,
            })
        with self.assertRaisesRegex(rp.ReviewPolicyError, "did not pass"):
            rp.validate_verification_receipt(receipt(exit_status=1), candidate(), {
                "command": "bun test claims.spec.ts",
                "source_tree_digest": SHA,
                "dependencies_digest": "e" * 64,
                "configuration_digest": "f" * 64,
                "environment_fingerprint": "1" * 64,
                "retained_output_digest": "2" * 64,
                "require_success": True,
            })
        with self.assertRaisesRegex(rp.ReviewPolicyError, "expected source_tree_digest is required"):
            rp.validate_verification_receipt(test_receipt, candidate(), {"command": "bun test claims.spec.ts"})

    def test_repair_packet_reports_missing_authority_and_requires_real_regression(self):
        packet = {
            "production_files": ["src/claims.ts"],
            "test_files": ["test/claims.spec.ts"],
            "contract_files": ["openapi.json"],
            "configuration_files": ["template.yaml"],
            "behavioral_repair": True,
            "regression": {"path": "test/claims.spec.ts", "exercises_failure": True},
        }
        missing = rp.validate_repair_packet(packet, {"files": ["src/claims.ts", "test/claims.spec.ts", "openapi.json"]})
        self.assertEqual(["template.yaml"], missing)

        packet["regression"]["exercises_failure"] = False
        with self.assertRaisesRegex(rp.ReviewPolicyError, "must exercise the defect"):
            rp.validate_repair_packet(packet, {"files": ["src/claims.ts", "test/claims.spec.ts", "openapi.json", "template.yaml"]})

    def test_convergence_requires_complete_cumulative_coverage_and_rejects_yield_stop(self):
        plan = delta_plan(risk_areas=["authorization"])
        baseline_records = [
            coverage("correctness_contracts", reviewer_id="reviewer-0", cand={"repo": "api", "base": BASE, "head": MID}, coverage_id="baseline"),
            coverage("testing_maintainability_standards", reviewer_id="reviewer-2", cand={"repo": "api", "base": BASE, "head": MID}, coverage_id="baseline"),
        ]

        with self.assertRaisesRegex(rp.ReviewPolicyError, "missing review coverage: security"):
            rp.validate_convergence(
                candidate(),
                plan,
                [*baseline_records, coverage("independent_repair_review", cand={"repo": "api", "base": MID, "head": HEAD}, reviewer_id="reviewer-1")],
                [closed_finding()],
                [receipt()],
            )

        plan["yield_stop"] = True
        with self.assertRaisesRegex(rp.ReviewPolicyError, "yield-stop"):
            rp.validate_convergence(
                candidate(),
                plan,
                [
                    *baseline_records,
                    coverage("independent_repair_review", cand={"repo": "api", "base": MID, "head": HEAD}, reviewer_id="reviewer-1"),
                    coverage("security", cand={"repo": "api", "base": MID, "head": HEAD}, reviewer_id="reviewer-2"),
                ],
                [closed_finding()],
                [receipt()],
            )

    def test_convergence_accepts_closed_findings_and_full_coverage(self):
        plan = delta_plan(risk_areas=["authorization"])

        result = rp.validate_convergence(
            candidate(),
            plan,
            [
                coverage("correctness_contracts", reviewer_id="reviewer-0", cand={"repo": "api", "base": BASE, "head": MID}, coverage_id="baseline"),
                coverage("testing_maintainability_standards", reviewer_id="reviewer-2", cand={"repo": "api", "base": BASE, "head": MID}, coverage_id="baseline"),
                coverage("independent_repair_review", cand={"repo": "api", "base": MID, "head": HEAD}, reviewer_id="reviewer-1"),
                coverage("security", cand={"repo": "api", "base": MID, "head": HEAD}, reviewer_id="reviewer-3"),
            ],
            [closed_finding()],
            [receipt()],
        )

        self.assertTrue(result["converged"])
        self.assertEqual(
            ["correctness_contracts", "independent_repair_review", "security", "testing_maintainability_standards"],
            result["covered_responsibilities"],
        )

    def test_convergence_requires_baseline_to_head_chain_expected_digests_and_allowed_model(self):
        final_candidate = {"repo": "api", "base": BASE, "head": HEAD}
        plan = delta_plan()

        broken_plan = delta_plan()
        broken_plan["segment_requirements"] = broken_plan["segment_requirements"][1:]
        with self.assertRaisesRegex(rp.ReviewPolicyError, "first segment"):
            rp.validate_convergence(
                final_candidate,
                broken_plan,
                [coverage("independent_repair_review", cand={"repo": "api", "base": MID, "head": HEAD})],
                [closed_finding()],
                [receipt()],
            )

        with self.assertRaisesRegex(rp.ReviewPolicyError, "missing required coverage records: baseline"):
            rp.validate_convergence(final_candidate, plan, [coverage("independent_repair_review")], [closed_finding()], [receipt()])

        bad_model = coverage("independent_repair_review", coverage_id="delta")
        bad_model["model"] = "wrong-model"
        with self.assertRaisesRegex(rp.ReviewPolicyError, "model must be gpt-5.6-sol"):
            rp.validate_convergence(
                final_candidate,
                delta_plan(),
                [
                    coverage("correctness_contracts", reviewer_id="reviewer-0", cand={"repo": "api", "base": BASE, "head": MID}, coverage_id="baseline"),
                    coverage("testing_maintainability_standards", reviewer_id="reviewer-2", cand={"repo": "api", "base": BASE, "head": MID}, coverage_id="baseline"),
                    {**bad_model, "candidate": {"repo": "api", "base": MID, "head": HEAD}},
                ],
                [closed_finding()],
                [receipt()],
            )

        bad_reasoning = coverage("independent_repair_review", coverage_id="delta")
        bad_reasoning["reasoning_effort"] = "high"
        with self.assertRaisesRegex(rp.ReviewPolicyError, "reasoning effort"):
            rp.validate_convergence(
                final_candidate,
                delta_plan(),
                [
                    coverage("correctness_contracts", reviewer_id="reviewer-0", cand={"repo": "api", "base": BASE, "head": MID}, coverage_id="baseline"),
                    coverage("testing_maintainability_standards", reviewer_id="reviewer-2", cand={"repo": "api", "base": BASE, "head": MID}, coverage_id="baseline"),
                    {**bad_reasoning, "candidate": {"repo": "api", "base": MID, "head": HEAD}},
                ],
                [closed_finding()],
                [receipt()],
            )

        bad_digest = coverage("independent_repair_review", coverage_id="delta")
        bad_digest["context_digest"] = "9" * 64
        with self.assertRaisesRegex(rp.ReviewPolicyError, "context digest"):
            rp.validate_convergence(
                final_candidate,
                delta_plan(),
                [
                    coverage("correctness_contracts", reviewer_id="reviewer-0", cand={"repo": "api", "base": BASE, "head": MID}, coverage_id="baseline"),
                    coverage("testing_maintainability_standards", reviewer_id="reviewer-2", cand={"repo": "api", "base": BASE, "head": MID}, coverage_id="baseline"),
                    {**bad_digest, "candidate": {"repo": "api", "base": MID, "head": HEAD}},
                ],
                [closed_finding()],
                [receipt()],
            )

    def test_convergence_supports_cumulative_baseline_and_delta_records(self):
        final_candidate = {"repo": "api", "base": BASE, "head": HEAD}
        plan = delta_plan()
        baseline_record = coverage(
            "correctness_contracts",
            reviewer_id="reviewer-0",
            cand={"repo": "api", "base": BASE, "head": MID},
            coverage_id="baseline",
        )
        baseline_testing_record = coverage(
            "testing_maintainability_standards",
            reviewer_id="reviewer-2",
            cand={"repo": "api", "base": BASE, "head": MID},
            coverage_id="baseline",
        )
        delta_record = coverage(
            "independent_repair_review",
            reviewer_id="reviewer-1",
            cand={"repo": "api", "base": MID, "head": HEAD},
            coverage_id="delta",
        )

        result = rp.validate_convergence(final_candidate, plan, [baseline_record, baseline_testing_record, delta_record], [closed_finding()], [receipt()])

        self.assertTrue(result["converged"])

    def test_segment_requirements_prevent_cross_segment_responsibility_union(self):
        final_candidate = {"repo": "api", "base": BASE, "head": HEAD}
        plan = delta_plan(risk_areas=["authorization"])
        misplaced_security = coverage("security", coverage_id="baseline", cand={"repo": "api", "base": BASE, "head": MID}, reviewer_id="reviewer-3")

        with self.assertRaisesRegex(rp.ReviewPolicyError, "segment delta missing review coverage: security"):
            rp.validate_convergence(
                final_candidate,
                plan,
                [
                    coverage("correctness_contracts", reviewer_id="reviewer-0", cand={"repo": "api", "base": BASE, "head": MID}, coverage_id="baseline"),
                    coverage("testing_maintainability_standards", reviewer_id="reviewer-2", cand={"repo": "api", "base": BASE, "head": MID}, coverage_id="baseline"),
                    coverage("independent_repair_review", cand={"repo": "api", "base": MID, "head": HEAD}),
                    misplaced_security,
                ],
                [closed_finding()],
                [receipt()],
            )

    def test_extra_full_span_coverage_cannot_bypass_declared_segments(self):
        plan = delta_plan(risk_areas=["authorization"])
        bypass_record = coverage(
            "independent_repair_review",
            "security",
            coverage_id="bypass",
            cand={"repo": "api", "base": BASE, "head": HEAD},
            reviewer_id="reviewer-x",
        )

        with self.assertRaisesRegex(rp.ReviewPolicyError, "unexpected coverage records: bypass"):
            rp.validate_convergence(
                candidate(),
                plan,
                [
                    coverage("correctness_contracts", reviewer_id="reviewer-0", cand={"repo": "api", "base": BASE, "head": MID}, coverage_id="baseline"),
                    coverage("testing_maintainability_standards", reviewer_id="reviewer-2", cand={"repo": "api", "base": BASE, "head": MID}, coverage_id="baseline"),
                    coverage("independent_repair_review", cand={"repo": "api", "base": MID, "head": HEAD}),
                    bypass_record,
                ],
                [closed_finding()],
                [receipt()],
            )

    def test_initial_baseline_requires_separate_reviewers(self):
        final_candidate = {"repo": "api", "base": BASE, "head": HEAD}
        plan = convergence_plan()
        plan["segment_requirements"] = [{
            "coverage_id": "baseline",
            "type": "initial",
            "candidate": final_candidate,
            "author_id": "baseline-author",
            "source_digest": SHA,
            "context_digest": "e" * 64,
        }]

        with self.assertRaisesRegex(rp.ReviewPolicyError, "separate reviewers"):
            rp.validate_convergence(
                final_candidate,
                plan,
                [coverage("correctness_contracts", "testing_maintainability_standards", coverage_id="baseline")],
                [closed_finding()],
                [receipt()],
            )

    def test_convergence_receipts_require_expected_fingerprints(self):
        plan = convergence_plan()
        stale = receipt()
        stale["environment_fingerprint"] = "9" * 64

        with self.assertRaisesRegex(rp.ReviewPolicyError, "environment_fingerprint is stale"):
            rp.validate_convergence(
                candidate(),
                plan,
                [
                    coverage("correctness_contracts", reviewer_id="reviewer-0", coverage_id="baseline"),
                    coverage("testing_maintainability_standards", reviewer_id="reviewer-2", coverage_id="baseline"),
                ],
                [closed_finding()],
                [stale],
            )

    def test_two_no_progress_attempts_on_same_finding_trigger_bounded_diagnosis(self):
        self.assertEqual(
            {"decision": "bounded_diagnosis", "finding_id": "F-1"},
            rp.no_progress_decision([
                {"finding_id": "F-1", "progress": False},
                {"finding_id": "F-1", "progress": False},
            ]),
        )
        self.assertEqual(
            {"decision": "continue", "finding_id": "F-1"},
            rp.no_progress_decision([
                {"finding_id": "F-1", "progress": False},
                {"finding_id": "F-1", "progress": True},
            ]),
        )
        self.assertEqual(
            {"decision": "bounded_diagnosis", "finding_id": "F-1"},
            rp.no_progress_decision([
                {"finding_id": "F-1", "progress": False},
                {"finding_id": "F-2", "progress": True},
                {"finding_id": "F-1", "progress": False},
            ]),
        )


if __name__ == "__main__":
    unittest.main()
