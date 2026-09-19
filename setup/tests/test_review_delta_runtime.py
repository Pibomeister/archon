#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SETUP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SETUP))

import review_delta_runtime as runtime
import review_policy as policy
import feature_chain as fc
import review_session
import review_verification


def run_git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


class RiskDeltaRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        run_git(self.repo, "init", "-q")
        run_git(self.repo, "config", "user.email", "test@example.com")
        run_git(self.repo, "config", "user.name", "Test")
        self.source = self.repo / "claim.txt"
        self.source.write_text("base\n", encoding="utf-8")
        run_git(self.repo, "add", ".")
        run_git(self.repo, "commit", "-qm", "base")
        self.base = run_git(self.repo, "rev-parse", "HEAD")
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.state_path = self.artifacts / "review-state.json"
        self.control = self.root / "control"
        self.chain_id = "a" * 24
        self.run_id = "1234abcd"
        self.chain_secret = "s" * 32

    def commit(self, text: str, message: str) -> str:
        self.source.write_text(text, encoding="utf-8")
        run_git(self.repo, "add", ".")
        run_git(self.repo, "commit", "-qm", message)
        return run_git(self.repo, "rev-parse", "HEAD")

    def write_state(self, **overrides):
        state = {
            "schema_version": 1,
            "kind": "risk-delta-review-state-seed",
            "logical_chain_id": self.chain_id,
            "run_id": self.run_id,
            "policy": {"name": policy.POLICY, "qualification_status": "qualified"},
            "repo": "api",
            "baseline_base": self.base,
            "author_id": "fixer-1",
            "captured_source_digest": "a" * 64,
            "impact_evidence_available": True,
            "required_receipts": ["unit"],
            "receipt_expectations": {
                "unit": {
                    "source_tree_digest": "b" * 64,
                    "dependencies_digest": "c" * 64,
                    "configuration_digest": "d" * 64,
                    "environment_fingerprint": "e" * 64,
                    "retained_output_digest": "f" * 64,
                }
            },
            "receipts": [],
            "coverage_records": [],
            "findings": [],
            "trusted_coverage_provenance": [],
            "author_session_ids": [{"session_id": "author-session"}],
        }
        state.update(overrides)
        trusted_coverage = state.get("trusted_coverage_provenance")
        if overrides.get("coverage_records") or overrides.get("segment_requirements"):
            trusted_coverage = {
                "coverage_records": overrides.get("coverage_records", []),
                "segment_requirements": overrides.get("segment_requirements", []),
            }
            state["trusted_coverage_provenance"] = trusted_coverage
        protected = {
            "repo": state["repo"],
            "baseline_base": state["baseline_base"],
            "author_id": state["author_id"],
            "captured_source_digest": state["captured_source_digest"],
            "required_checks_digest": runtime.digest_state_items(state, ["required_receipts", "receipt_expectations"]),
            "scope_inputs_digest": runtime.digest_state_items(state, runtime.SCOPE_AUTHORITY_KEYS),
            "coverage_provenance_digest": policy.digest(trusted_coverage),
        }
        state["protected_inputs"] = protected
        state["controller_mac"] = fc.hmac_sha256(self.chain_secret, runtime.controller_seed_body(state))
        write_json(self.state_path, state)
        fc.write_state(self.control, {
            "schema_version": 2,
            "logical_chain_id": self.chain_id,
            "chain_secret": self.chain_secret,
            "provider": "codex",
            "repositories": ["api"],
            "review_policy": {"qualification_status": "qualified"},
            "current_run": {
                "run_id": self.run_id,
                "repo": "api",
                "artifacts_dir": str(self.artifacts),
            },
            "worktrees": {"api": {"worktree": str(self.repo), "baseline": self.base}},
            "stages": {"api": {"plan": {"verification": [{"id": "unit", "argv": ["true"]}]}}},
        })
        os.environ["ARCHON_CONTROL_DIR"] = str(self.control)
        os.environ["ARCHON_FEATURE_CHAIN_ID"] = self.chain_id

    def response_for(self, slot: int, reviewer: str):
        current = json.loads((self.artifacts / "current-review.json").read_text(encoding="utf-8"))
        assignment = next(item for item in current["assignments"] if item["slot"] == slot)
        session_id = reviewer.replace("_", "-")
        review_session.bind(self.control, self.chain_id, self.run_id, session_id, slot, "reviewer", policy.REQUIRED_MODEL, policy.REQUIRED_REASONING_EFFORT)
        response = {
            "reviewer_id": session_id,
            "model": policy.REQUIRED_MODEL,
            "reasoning_effort": policy.REQUIRED_REASONING_EFFORT,
            "status": "complete",
            "raw_output": f"{reviewer} covered {assignment['responsibility']}",
            "findings": [],
        }
        write_json(Path(current["round_dir"]) / assignment["response_file"], response)

    def write_required_converge_gates(self):
        current = json.loads((self.artifacts / "current-review.json").read_text(encoding="utf-8"))
        head = run_git(self.repo, "rev-parse", "HEAD")
        (Path(current["round_dir"]) / "pre-head.txt").write_text(f"{head}\n", encoding="utf-8")
        write_json(Path(current["round_dir"]) / "fixer-result.json", {
            "applied": [],
            "failed": [],
            "advisory": [],
            "incomplete": [],
            "cross_repo": [],
        })
        write_json(self.artifacts / "files-allowlist.json", ["claim.txt"])
        (self.artifacts / "bootstrap-head.txt").write_text(f"{self.base}\n", encoding="utf-8")

    def test_legacy_completed_review_requires_import_instead_of_full_diff_restart(self):
        self.commit("repaired\n", "repair")
        self.write_state()
        previous = self.artifacts / "round-6"
        previous.mkdir()
        write_json(previous / "review-summary.json", {"verdict": "Not ready", "degraded": False})
        with self.assertRaisesRegex(ValueError, "HISTORICAL_COVERAGE_IMPORT_REQUIRED"):
            runtime.prepare_review(self.artifacts, self.repo, self.state_path, None)
        self.assertFalse((self.artifacts / "current-review.json").exists())

    def successful_receipt(self):
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        state["receipts"] = [{
            "policy": policy.POLICY,
            "schema_version": policy.SCHEMA_VERSION,
            "candidate": {"repo": "api", "base": self.base, "head": run_git(self.repo, "rev-parse", "HEAD")},
            "command": "unit",
            "exit_status": 0,
            "source_tree_digest": "b" * 64,
            "dependencies_digest": "c" * 64,
            "configuration_digest": "d" * 64,
            "environment_fingerprint": "e" * 64,
            "retained_output_digest": "f" * 64,
        }]
        write_json(self.state_path, state)

    def private_successful_receipt(self):
        output = self.artifacts / "review-verification" / "unit.log"
        output.parent.mkdir(exist_ok=True)
        output.write_text("ok\n", encoding="utf-8")
        candidate = {"repo": "api", "base": self.base, "head": run_git(self.repo, "rev-parse", "HEAD")}
        argv = ["true"]
        expected = review_verification.fingerprint(self.repo, argv)
        receipt = {
            "policy": policy.POLICY,
            "schema_version": policy.SCHEMA_VERSION,
            "candidate": candidate,
            "command": "unit",
            "argv": argv,
            **expected,
            "exit_status": 0,
            "retained_output": str(output),
            "retained_output_digest": fc.file_digest(output),
        }
        key = policy.digest({"candidate": candidate, "command": "unit", "argv": argv, **expected})
        root = fc._ensure_private_dir(self.control / "review-verification", "review verification directory") / self.run_id
        root.mkdir(mode=0o700)
        record = {"receipt": receipt}
        record["receipt_mac"] = fc.hmac_sha256(self.chain_secret, record)
        fc._secure_write(root / f"{key}.json", record)

    def coverage(self, base: str, head: str, coverage_id: str, responsibility: str, reviewer: str):
        return {
            "policy": policy.POLICY,
            "schema_version": policy.SCHEMA_VERSION,
            "candidate": {"repo": "api", "base": base, "head": head},
            "coverage_id": coverage_id,
            "responsibilities": [responsibility],
            "source_digest": "a" * 64,
            "context_digest": "b" * 64,
            "reviewer_id": reviewer,
            "model": policy.REQUIRED_MODEL,
            "reasoning_effort": policy.REQUIRED_REASONING_EFFORT,
            "status": "complete",
        }

    def test_repair_delta_uses_latest_completed_review_head_not_original_base(self):
        first = self.commit("fix one\n", "fix one")
        self.write_state()
        result = runtime.prepare_review(self.artifacts, self.repo, self.state_path, None)
        self.assertEqual({"review_1": "yes", "review_2": "yes"}, {key: result[key] for key in ("review_1", "review_2")})
        self.response_for(1, "reviewer-correctness")
        self.response_for(2, "reviewer-tests")
        completed = runtime.complete_reviews(self.artifacts, self.state_path)
        self.assertEqual("complete", completed["status"])

        second = self.commit("fix two\n", "fix two")
        result = runtime.prepare_review(self.artifacts, self.repo, self.state_path, None)
        current = json.loads((self.artifacts / "current-review.json").read_text(encoding="utf-8"))
        self.assertEqual({"repo": "api", "base": first, "head": second}, current["candidate"])
        self.assertIn("-fix one\n+fix two", Path(current["diff_path"]).read_text(encoding="utf-8"))
        self.assertNotIn("-base", Path(current["diff_path"]).read_text(encoding="utf-8"))
        self.assertEqual("yes", result["review_1"])
        self.response_for(1, "reviewer-delta")
        runtime.complete_reviews(self.artifacts, self.state_path)
        self.successful_receipt()
        self.write_required_converge_gates()
        self.assertEqual("converged", runtime.converge(self.artifacts, self.repo, self.state_path)["status"])

    def test_cross_repo_finding_blocks_convergence_until_a_human_records_it_filed(self):
        self.commit("fix one\n", "fix one")
        self.write_state()
        runtime.prepare_review(self.artifacts, self.repo, self.state_path, None)
        self.response_for(1, "reviewer-correctness")
        self.response_for(2, "reviewer-tests")
        runtime.complete_reviews(self.artifacts, self.state_path)
        self.successful_receipt()
        self.write_required_converge_gates()
        current = json.loads((self.artifacts / "current-review.json").read_text(encoding="utf-8"))
        write_json(Path(current["round_dir"]) / "fixer-result.json", {
            "applied": [], "failed": [], "advisory": [], "incomplete": [],
            "cross_repo": [{"finding": "mcp drops errorMessage", "action": "route",
                            "producer_repo": "goodword-mcp", "severity": "P2"}],
        })
        with self.assertRaisesRegex(policy.ReviewPolicyError, "CROSS_REPO_FINDING count=1 repos=goodword-mcp"):
            runtime.converge(self.artifacts, self.repo, self.state_path)
        write_json(self.artifacts / "cross-repo-filed.json",
                   [{"key": "65609a9208165071", "filed": "https://github.com/o/goodword-mcp/issues/12", "by": "edy"}])
        self.assertEqual("converged", runtime.converge(self.artifacts, self.repo, self.state_path)["status"])

    def test_interrupted_checkpoint_reuses_completed_slot_and_attempts_remain_separate(self):
        self.commit("fix risky\n", "fix risky")
        self.write_state(risk_areas=["authorization"])
        runtime.prepare_review(self.artifacts, self.repo, self.state_path, None)
        self.response_for(1, "reviewer-delta")
        partial = runtime.complete_reviews(self.artifacts, self.state_path, slot=1)
        self.assertEqual([2, 3], partial["pending"])
        checkpoint = next((self.artifacts / "review-checkpoints").glob("*.slot-1.coverage.json"))
        before = checkpoint.read_text(encoding="utf-8")

        reused = runtime.prepare_review(self.artifacts, self.repo, self.state_path, None)
        self.assertTrue(reused["reused"])
        self.assertEqual("no", reused["review_1"])
        self.assertEqual("yes", reused["review_2"])
        self.assertEqual("yes", reused["review_3"])
        self.response_for(2, "reviewer-security")
        self.response_for(3, "reviewer-standards")
        completed = runtime.complete_reviews(self.artifacts, self.state_path)
        self.assertEqual("complete", completed["status"])
        self.assertEqual(before, checkpoint.read_text(encoding="utf-8"))

    def test_unreviewed_head_blocks_convergence(self):
        self.commit("reviewed\n", "reviewed")
        self.write_state()
        runtime.prepare_review(self.artifacts, self.repo, self.state_path, None)
        self.response_for(1, "reviewer-correctness")
        self.response_for(2, "reviewer-tests")
        runtime.complete_reviews(self.artifacts, self.state_path)
        self.commit("unreviewed\n", "unreviewed")
        self.successful_receipt()
        self.write_required_converge_gates()

        with self.assertRaisesRegex(policy.ReviewPolicyError, "end at the final candidate head"):
            runtime.converge(self.artifacts, self.repo, self.state_path)

    def test_risk_change_refreshes_specialist_coverage(self):
        self.commit("secure repair\n", "secure repair")
        self.write_state(risk_areas=["authorization"])

        result = runtime.prepare_review(self.artifacts, self.repo, self.state_path, None)
        current = json.loads((self.artifacts / "current-review.json").read_text(encoding="utf-8"))
        responsibilities = [assignment["responsibility"] for assignment in current["assignments"]]
        self.assertEqual(["correctness_contracts", "testing_maintainability_standards", "security"], responsibilities)
        self.assertEqual("yes", result["review_3"])

    def test_missing_impact_evidence_defaults_to_caller_reader_trace(self):
        self.commit("unknown impact\n", "unknown impact")
        self.write_state(impact_evidence_available=False)

        runtime.prepare_review(self.artifacts, self.repo, self.state_path, None)
        current = json.loads((self.artifacts / "current-review.json").read_text(encoding="utf-8"))
        responsibilities = [assignment["responsibility"] for assignment in current["assignments"]]
        self.assertIn("caller_reader_trace", responsibilities)
        self.assertTrue(all(Path(item["response_file"]).is_absolute() for item in current["assignments"]))
        self.assertIn("checkpoint_command", current["assignments"][0])

    def test_unbounded_impact_requires_explicit_recovery_decision(self):
        self.commit("unbounded impact\n", "unbounded impact")
        self.write_state(impact_unbounded=True)

        with self.assertRaisesRegex(policy.ReviewPolicyError, "full-candidate recovery"):
            runtime.prepare_review(self.artifacts, self.repo, self.state_path, None)

    def test_missing_reviewer_provenance_blocks_prepare(self):
        self.commit("needs controller\n", "needs controller")
        self.write_state()
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        state["protected_inputs"]["scope_inputs_digest"] = "0" * 64
        state["controller_mac"] = fc.hmac_sha256(self.chain_secret, runtime.controller_seed_body(state))
        write_json(self.state_path, state)

        with self.assertRaisesRegex(policy.ReviewPolicyError, "protected input mismatch"):
            runtime.prepare_review(self.artifacts, self.repo, self.state_path, None)

    def test_response_provenance_must_match_assignment(self):
        self.commit("wrong provenance\n", "wrong provenance")
        self.write_state()
        runtime.prepare_review(self.artifacts, self.repo, self.state_path, None)
        current = json.loads((self.artifacts / "current-review.json").read_text(encoding="utf-8"))
        assignment = current["assignments"][0]
        review_session.bind(self.control, self.chain_id, self.run_id, "real-session", 1, "reviewer", policy.REQUIRED_MODEL, policy.REQUIRED_REASONING_EFFORT)
        write_json(Path(assignment["response_file"]), {
            "reviewer_id": "reviewer-correctness",
            "model": policy.REQUIRED_MODEL,
            "reasoning_effort": policy.REQUIRED_REASONING_EFFORT,
            "status": "complete",
            "raw_output": "mismatched provenance",
            "findings": [],
        })

        with self.assertRaisesRegex(policy.ReviewPolicyError, "session provenance"):
            runtime.complete_reviews(self.artifacts, self.state_path, slot=1)

    def test_convergence_requires_actual_gate_artifacts(self):
        self.commit("reviewed\n", "reviewed")
        self.write_state()
        runtime.prepare_review(self.artifacts, self.repo, self.state_path, None)
        self.response_for(1, "reviewer-correctness")
        self.response_for(2, "reviewer-tests")
        runtime.complete_reviews(self.artifacts, self.state_path)
        self.successful_receipt()

        with self.assertRaisesRegex(policy.ReviewPolicyError, "missing fixer-result"):
            runtime.converge(self.artifacts, self.repo, self.state_path)

    def test_convergence_imports_private_verification_receipts(self):
        self.commit("reviewed\n", "reviewed")
        self.write_state(receipt_expectations={})
        runtime.prepare_review(self.artifacts, self.repo, self.state_path, None)
        self.response_for(1, "reviewer-correctness")
        self.response_for(2, "reviewer-tests")
        runtime.complete_reviews(self.artifacts, self.state_path)
        self.write_required_converge_gates()
        self.private_successful_receipt()

        result = runtime.converge(self.artifacts, self.repo, self.state_path)

        self.assertEqual("converged", result["status"])
        refreshed = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(["unit"], [receipt["command"] for receipt in refreshed["receipts"]])
        self.assertIn("unit", refreshed["receipt_expectations"])
        runtime.verify_authority(self.artifacts, refreshed)

    def test_missing_required_receipt_is_typed_verification_stop(self):
        self.commit("reviewed\n", "reviewed")
        self.write_state()
        runtime.prepare_review(self.artifacts, self.repo, self.state_path, None)
        self.response_for(1, "reviewer-correctness")
        self.response_for(2, "reviewer-tests")
        runtime.complete_reviews(self.artifacts, self.state_path)
        self.write_required_converge_gates()

        proc = subprocess.run(
            [
                sys.executable,
                str(SETUP / "review_delta_runtime.py"),
                "converge",
                "--artifacts",
                str(self.artifacts),
                "--worktree",
                str(self.repo),
            ],
            capture_output=True,
            text=True,
            env={**os.environ, "ARCHON_CONTROL_DIR": str(self.control), "ARCHON_FEATURE_CHAIN_ID": self.chain_id},
        )
        self.assertEqual(1, proc.returncode)
        self.assertIn(runtime.VERIFICATION_REQUIRED, proc.stdout)

    def test_historical_full_baseline_with_open_findings_becomes_delta_base(self):
        reviewed = self.commit("reviewed baseline\n", "reviewed baseline")
        current = self.commit("three file repair\n", "repair after baseline")
        open_finding = {
            "finding_id": "F-open",
            "violated_invariant": "claim disclosure stays scoped",
            "affected_paths": ["claim.txt"],
            "severity": "P1",
            "evidence": "historical reviewer left this open",
            "owning_repository": "api",
            "status": "open",
        }
        self.write_state(
            coverage_records=[
                self.coverage(self.base, reviewed, "historical-r6", "correctness_contracts", "r6-correctness"),
                self.coverage(self.base, reviewed, "historical-r6", "testing_maintainability_standards", "r6-standards"),
            ],
            segment_requirements=[{
                "coverage_id": "historical-r6",
                "type": "initial",
                "candidate": {"repo": "api", "base": self.base, "head": reviewed},
                "author_id": "historical-implementer",
                "source_digest": "a" * 64,
                "context_digest": "b" * 64,
            }],
            findings=[open_finding],
        )

        runtime.prepare_review(self.artifacts, self.repo, self.state_path, None)
        prepared = json.loads((self.artifacts / "current-review.json").read_text(encoding="utf-8"))
        self.assertEqual({"repo": "api", "base": reviewed, "head": current}, prepared["candidate"])
        self.assertEqual(["independent_repair_review"], [item["responsibility"] for item in prepared["assignments"]])


if __name__ == "__main__":
    unittest.main()
