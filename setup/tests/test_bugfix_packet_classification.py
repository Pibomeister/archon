#!/usr/bin/env python3
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parent.parent
SCRIPT = SETUP / "bugfix-contract.py"
FIXTURE = SETUP / "tests" / "fixtures" / "rca-minimal"


def write(path: Path, value):
    if isinstance(value, str):
        path.write_text(value, encoding="utf-8")
    else:
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


class BugfixPacketClassificationTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.ad = Path(self.td.name) / "artifacts"
        shutil.copytree(FIXTURE, self.ad)
        write(self.ad / "rca.md", "## Observation\noff-by-one on last page\nConfidence: High\n")
        write(self.ad / "causal-chain.json", {"links": [{"index": 1, "cause": "off-by-one on last page", "evidence": {"source": "report", "file": "bug-report.md", "quote": "off-by-one on last page"}, "fixable": True, "fix_site": "src/foo.ts:42"}]})
        write(self.ad / "evidence-manifest.json", {"sources": {"report": {"status": "gathered", "file": "bug-report.md"}}})
        r = self.run_contract("write-proof-manifest", "--artifacts", str(self.ad))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def run_contract(self, *args):
        return subprocess.run(["python3", str(SCRIPT), *args], cwd=SETUP.parent, text=True, capture_output=True)

    def test_full_fix_packet_classification_is_resolved_and_closable(self):
        r = self.run_contract("write-approval-manifest", "--artifacts", str(self.ad))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        c = json.loads((self.ad / "fix-classification.json").read_text())
        self.assertEqual(c["implementation_result"], "FULL_FIX")
        self.assertEqual(c["ticket_disposition"], "RESOLVED")
        self.assertEqual(c["approval_scope"], "ship-covered-symptoms")
        self.assertTrue(c["ticket_closure_allowed"])

    def test_product_semantics_plus_fixed_symptom_stays_partial_and_open(self):
        symptoms = json.loads((self.ad / "symptoms.json").read_text())
        symptoms["effective_symptoms"][0]["relation"] = "split"
        symptoms["effective_symptoms"].append({"id": "E2", "source_ids": ["S1"], "relation": "split", "claim": "Granola icon is shown", "expected_behavior": "no Granola icon", "actual_behavior": "Granola icon is shown"})
        symptoms["ledger_revision_hash"] = None
        import hashlib
        symptoms["ledger_revision_hash"] = hashlib.sha256(json.dumps(symptoms, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        write(self.ad / "symptoms.json", symptoms)
        chain = json.loads((self.ad / "bugfix-chain.json").read_text())
        chain["ledger_revision_hash"] = symptoms["ledger_revision_hash"]
        write(self.ad / "bugfix-chain.json", chain)
        chain_assessment = json.loads((self.ad / "chain-assessment.json").read_text())
        chain_assessment["active_symptom_ids"] = ["E1", "E2"]
        write(self.ad / "chain-assessment.json", chain_assessment)
        recovery = json.loads((self.ad / "proof-recovery.json").read_text())
        recovery["active_symptom_ids"] = ["E1", "E2"]
        write(self.ad / "proof-recovery.json", recovery)
        write(self.ad / "symptom-dispositions.json", {"schema_version": 2, "dispositions": [{"symptom_id": "E1", "disposition": "fixed"}, {"symptom_id": "E2", "disposition": "product-semantics"}]})
        write(self.ad / "causal-coverage.json", {"schema_version": 2, "coverage": [
            {"symptom_id": "E1", "cause_id": "H1", "occurrence_attributed": True, "planned_diff": ["src/foo.ts"], "red_test": "src/__tests__/foo.spec.ts::does the thing", "counterfactual_user_visible": True},
            {"symptom_id": "E2", "cause_id": None, "occurrence_attributed": False, "planned_diff": [], "red_test": None, "counterfactual_user_visible": False},
        ]})
        self.assertEqual(self.run_contract("write-proof-manifest", "--artifacts", str(self.ad)).returncode, 0)
        r = self.run_contract("write-approval-manifest", "--artifacts", str(self.ad))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        c = json.loads((self.ad / "fix-classification.json").read_text())
        self.assertEqual(c["implementation_result"], "PARTIAL_FIX")
        self.assertEqual(c["ticket_disposition"], "PRODUCT_DECISION_NEEDED")
        self.assertEqual(c["approval_scope"], "ship-covered-symptoms")
        self.assertFalse(c["ticket_closure_allowed"])
        self.assertIn("E2", c["open_effective_ids"])

    def test_fixed_symptom_requires_occurrence_and_counterfactual_coverage(self):
        coverage = json.loads((self.ad / "causal-coverage.json").read_text())
        coverage["coverage"][0]["counterfactual_user_visible"] = False
        write(self.ad / "causal-coverage.json", coverage)
        r = self.run_contract("write-approval-manifest", "--artifacts", str(self.ad))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("lacks positive user-visible counterfactual", r.stdout)

    def test_approval_manifest_invalidates_on_plan_mutation(self):
        self.assertEqual(self.run_contract("write-approval-manifest", "--artifacts", str(self.ad)).returncode, 0)
        plan = json.loads((self.ad / "fix-plan.json").read_text())
        plan["files"].append("src/extra.ts")
        write(self.ad / "fix-plan.json", plan)
        r = self.run_contract("validate-current-manifest", "--artifacts", str(self.ad), "--kind", "approval")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("stale approval manifest", r.stdout)

    def test_lite_approval_manifest_invalidates_on_gate_edit(self):
        self.assertEqual(
            self.run_contract("write-approval-manifest", "--artifacts", str(self.ad)).returncode,
            0,
        )
        r = self.run_contract("write-lite-approval-manifest", "--artifacts", str(self.ad))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        plan = json.loads((self.ad / "fix-plan.json").read_text())
        plan["approach"] = "human edited after attestation"
        write(self.ad / "fix-plan.json", plan)
        r = self.run_contract(
            "validate-current-manifest", "--artifacts", str(self.ad), "--kind", "lite-approval"
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("stale lite-approval manifest", r.stdout)


if __name__ == "__main__":
    unittest.main()
