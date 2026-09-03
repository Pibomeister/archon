#!/usr/bin/env python3
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "bugfix-contract.py"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "rca-minimal"
WORKFLOW = Path(__file__).resolve().parents[2] / "workflows" / "bugfix.yaml"


def write(ad, name, obj):
    (ad / name).write_text(json.dumps(obj), encoding="utf-8")


class SystematicDebuggingContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        shutil.copytree(FIXTURE, self.tmp, dirs_exist_ok=True)

    def run_coverage(self):
        return subprocess.run([
            "python3", str(SCRIPT), "validate-causal-coverage", "--artifacts", str(self.tmp)
        ], capture_output=True, encoding="utf-8")

    def test_happy_path_enforces_systematic_debugging_artifacts(self):
        r = self.run_coverage()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("BUGFIX_COVERAGE=OK", r.stdout)

    def test_rca_prompt_uses_contract_id_shape_and_repository_test_policy(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('"selected_hypothesis_id":1', workflow)
        self.assertIn('write 1, never "H1"', workflow)
        self.assertNotIn('path of the NEW spec file', workflow)
        self.assertIn('Follow repo-policy.json for test placement', workflow)
        self.assertIn('Never add speculative', workflow)
        self.assertIn('the exact intended implementation scope', workflow)
        self.assertIn('format-stable bytes that will be committed', workflow)
        self.assertIn('formatter changed predicted signature', workflow)
        self.assertIn('Do not add a `commands` field', workflow)
        self.assertIn('one stable hermetic owner', workflow)
        self.assertIn('at the highest practical fidelity', workflow)
        self.assertIn('Implement ALL of', workflow)
        self.assertIn('this file freezes here', workflow)
        self.assertIn('Any .int.spec.ts, .e2e.spec.ts, .ai.spec.ts or', workflow)
        self.assertIn('`verify.json` must contain only `test_patterns`', workflow)
        self.assertIn('Configure the skill so no safe_auto/gated_auto fix', workflow)
        self.assertIn('repro test modified before commit', workflow)
        self.assertNotIn('TEST_GROUP=evals bun run test:integration', workflow)
        self.assertIn('search-streaming-contracts.int.spec.ts', workflow)
        self.assertIn('bun run eval:lint-corpus', workflow)
        self.assertIn('cause=missing-reranker-fixture', workflow)
        self.assertIn('never re-record a failing outcome into baseline truth', workflow)
        self.assertIn('eval-quality-differential.sh', workflow)
        self.assertIn('EVAL_QUALITY=ADVISORY pre-existing baseline failure', workflow)
        self.assertIn('surface_equivalence', workflow)
        self.assertIn('Similar types or shared helpers are not ownership evidence', workflow)
        self.assertIn('SMOKE_PRODUCT_FAIL', workflow)
        self.assertIn('SMOKE_HARNESS_DRIFT', workflow)
        self.assertIn('failure_class', workflow)
        self.assertIn("accepted integration/eval profile", workflow)
        self.assertIn("not authority to change a production default", workflow)
        self.assertIn('Write rejection-receipt.json ONLY inside', workflow)
        self.assertIn('never fall back to the repository, current directory, /tmp', workflow)
        self.assertIn("gate-driven distortion", workflow)
        self.assertIn("Never recommend", workflow)
        self.assertIn("Never invent", workflow)
        self.assertIn("mechanism and belongs in causal citations", workflow)
        self.assertIn('SEARCH_V4_COMPANY_MIN_CORPUS="${SEARCH_V4_COMPANY_MIN_CORPUS:-1}"', workflow)
        self.assertIn('--project=chromium --workers=1 --reporter=json', workflow)
        self.assertIn('Write EXACTLY ONE file: kb-capture.md', workflow)
        self.assertIn('external_kb=optional', workflow)
        self.assertIn('validate-smoke-readiness', workflow)
        self.assertIn('ticket_disposition=RESOLVED', workflow)
        self.assertIn('auto product failures have been blocked', workflow)

    def test_runbook_and_operator_skill_keep_external_kb_promotion_optional(self):
        archon = WORKFLOW.parent.parent
        runbook = (archon / "RUNBOOK.md").read_text(encoding="utf-8")
        skill = (archon / "skills/archon-sdlc/SKILL.md").read_text(encoding="utf-8")
        for document in (runbook, skill):
            self.assertIn("run-local", document)
            self.assertIn("kb-capture.md", document)
            self.assertIn("optional operator", document)
        self.assertNotIn("Each lane's `kb-capture` node writes **exactly one** new file to `goodword-kb", runbook)
        self.assertNotIn("Each lane's `kb-capture` writes exactly one file to", skill)
        self.assertNotIn("wiki/change-history/<date>-archon-bugfix", runbook)
        self.assertNotIn("wiki/change-history/<date>-archon-backfill", runbook)
        self.assertNotIn("writes one change-history file", skill)

    def test_multiple_active_hypotheses_fail(self):
        write(self.tmp, "hypotheses.json", [
            {"id": "H1", "hypothesis": "one", "status": "open"},
            {"id": "H2", "hypothesis": "two", "status": "open"},
        ])
        r = self.run_coverage()
        self.assertEqual(r.returncode, 1)
        self.assertIn("exactly one active hypothesis", r.stdout)

    def test_missing_working_comparison_fails(self):
        write(self.tmp, "pattern-comparison.json", {"schema_version": 1, "differences": ["x"]})
        r = self.run_coverage()
        self.assertEqual(r.returncode, 1)
        self.assertIn("missing working comparison", r.stdout)

    def test_runtime_surface_equivalence_is_required(self):
        boundary = json.loads((self.tmp / "boundary-trace.json").read_text())
        boundary.pop("surface_equivalence")
        write(self.tmp, "boundary-trace.json", boundary)
        r = self.run_coverage()
        self.assertEqual(r.returncode, 1)
        self.assertIn("missing surface_equivalence", r.stdout)

    def test_adjacent_test_path_cannot_authorize_implementation(self):
        boundary = json.loads((self.tmp / "boundary-trace.json").read_text())
        boundary["surface_equivalence"]["test_matches_runtime"] = False
        write(self.tmp, "boundary-trace.json", boundary)
        r = self.run_coverage()
        self.assertEqual(r.returncode, 1)
        self.assertIn("test/smoke runtime equivalence", r.stdout)

    def test_self_attested_true_cannot_hide_an_adjacent_test_owner(self):
        boundary = json.loads((self.tmp / "boundary-trace.json").read_text())
        boundary["surface_equivalence"]["test_runtime_owner"] = "src/adjacent-search.service.ts:page"
        write(self.tmp, "boundary-trace.json", boundary)
        r = self.run_coverage()
        self.assertEqual(r.returncode, 1)
        self.assertIn("test runtime owner mismatch", r.stdout)

    def test_self_attested_true_cannot_hide_an_adjacent_smoke_owner(self):
        boundary = json.loads((self.tmp / "boundary-trace.json").read_text())
        boundary["surface_equivalence"]["smoke_runtime_owner"] = "src/adjacent-search.service.ts:page"
        write(self.tmp, "boundary-trace.json", boundary)
        r = self.run_coverage()
        self.assertEqual(r.returncode, 1)
        self.assertIn("smoke runtime owner mismatch", r.stdout)

    def test_surface_equivalence_requires_evidence_for_each_ownership_link(self):
        boundary = json.loads((self.tmp / "boundary-trace.json").read_text())
        boundary["surface_equivalence"]["evidence"] = boundary["surface_equivalence"]["evidence"][:1]
        write(self.tmp, "boundary-trace.json", boundary)
        r = self.run_coverage()
        self.assertEqual(r.returncode, 1)
        self.assertIn("evidence for runtime, test, and smoke ownership", r.stdout)

    def test_ambiguous_reported_surface_blocks_an_implementation_plan(self):
        boundary = json.loads((self.tmp / "boundary-trace.json").read_text())
        boundary["surface_equivalence"]["reported_surface_status"] = "ambiguous"
        boundary["surface_equivalence"]["surface_selection_basis"] = "The report names multiple possible entrypoints."
        write(self.tmp, "boundary-trace.json", boundary)
        r = self.run_coverage()
        self.assertEqual(r.returncode, 1)
        self.assertIn("requires an identified surface", r.stdout)

    def test_ambiguous_surface_with_no_fix_plan_remains_a_valid_open_investigation(self):
        boundary = json.loads((self.tmp / "boundary-trace.json").read_text())
        boundary["surface_equivalence"].update({
            "reported_surface_status": "ambiguous",
            "surface_selection_basis": "The report maps to multiple entrypoints.",
            "runtime_owner": "unselected",
            "test_runtime_owner": "candidate-only",
            "smoke_runtime_owner": "unselected",
            "test_matches_runtime": False,
            "smoke_matches_runtime": False,
        })
        write(self.tmp, "boundary-trace.json", boundary)
        write(self.tmp, "fix-plan.json", {
            "approach": "", "fix_site": "", "files": [], "risks": [], "alternatives": []
        })
        r = self.run_coverage()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_test_fixture_cannot_remove_the_observed_causal_precondition(self):
        boundary = json.loads((self.tmp / "boundary-trace.json").read_text())
        boundary["surface_equivalence"]["reproduction_equivalence"] = {
            "observed_preconditions": ["cardinality below configured threshold"],
            "test_preconditions": ["cardinality padded above configured threshold"],
            "material_differences": [{
                "dimension": "fixture cardinality",
                "observed": "below threshold",
                "test": "above threshold",
                "impact": "changes-causal-boundary",
            }],
            "equivalent": False,
        }
        write(self.tmp, "boundary-trace.json", boundary)
        r = self.run_coverage()
        self.assertEqual(r.returncode, 1)
        self.assertIn("reproduction precondition equivalence", r.stdout)

    def test_fix_plan_requires_valid_mechanism(self):
        proof = json.loads((self.tmp / "proof-assessment.json").read_text())
        proof["mechanism_valid"] = False
        write(self.tmp, "proof-assessment.json", proof)
        r = self.run_coverage()
        self.assertEqual(r.returncode, 1)
        self.assertIn("mechanism_valid=true", r.stdout)

    def test_gather_more_conditional_plan_is_preserved_then_cleared_deterministically(self):
        debug = json.loads((self.tmp / "debug-phase.json").read_text())
        debug["reproduction_status"] = "gather-more"
        write(self.tmp, "debug-phase.json", debug)
        plan = json.loads((self.tmp / "fix-plan.json").read_text())
        write(self.tmp, "fix-plan.json", plan)
        hypotheses = json.loads((self.tmp / "hypotheses.json").read_text())
        hypotheses.append({"id": "H2", "hypothesis": "still plausible", "status": "open"})
        write(self.tmp, "hypotheses.json", hypotheses)
        result = subprocess.run(
            ["python3", str(SCRIPT), "normalize-gather-more", str(self.tmp)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        normalized = json.loads((self.tmp / "fix-plan.json").read_text())
        self.assertEqual(normalized["approach"], "")
        self.assertEqual(normalized["files"], [])
        self.assertTrue((self.tmp / "discarded-conditional-fix-plan.json").is_file())
        normalized_hypotheses = json.loads((self.tmp / "hypotheses.json").read_text())
        self.assertEqual([row["status"] for row in normalized_hypotheses], ["confirmed-by-experiment", "queued"])

    def test_occurrence_unattributed_cannot_claim_fixed_ticket(self):
        proof = json.loads((self.tmp / "proof-assessment.json").read_text())
        proof["occurrence_attributed"] = False
        proof["ticket_resolution_claim"] = "fixed"
        write(self.tmp, "proof-assessment.json", proof)
        r = self.run_coverage()
        self.assertEqual(r.returncode, 1)
        self.assertIn("occurrence-unattributed", r.stdout)

    def test_three_failed_fixes_require_architecture_review(self):
        debug = json.loads((self.tmp / "debug-phase.json").read_text())
        debug["fix_attempt_count"] = 3
        write(self.tmp, "debug-phase.json", debug)
        r = self.run_coverage()
        self.assertEqual(r.returncode, 1)
        self.assertIn("architecture review", r.stdout)

    def test_incomplete_prod_probe_cannot_authorize_occurrence_attribution(self):
        proof = json.loads((self.tmp / "proof-assessment.json").read_text())
        proof["occurrence_evidence_sources"] = ["prod-probes"]
        write(self.tmp, "proof-assessment.json", proof)
        write(self.tmp, "evidence-provenance.json", {
            "schema_version": 2,
            "sources": [{
                "source": "prod-probes", "status": "degraded",
                "completeness": "incomplete", "occurrence_attribution_valid": False,
                "expires_at": "2099-01-01T00:00:00Z",
            }],
        })
        r = self.run_coverage()
        self.assertEqual(r.returncode, 1)
        self.assertIn("incomplete, stale, or invalidated", r.stdout)


if __name__ == "__main__":
    unittest.main()
