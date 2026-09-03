#!/usr/bin/env python3
"""ENG-3060 characterization fixtures for the next bugfix contract.

These tests lock the incident-level failure mode before U1 introduces the
contract helper. They intentionally distinguish two things:

* Fixture invariants and the current v1 RCA shape gap are executable today.
* Future causal-coverage behavior is specified through setup/bugfix-contract.py
  and fails with a clear "missing helper" message until U1 lands.
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "bugfix-eng3060"
RCA_SHAPE = ROOT / "rca-shape.sh"
BUGFIX_CONTRACT = ROOT / "bugfix-contract.py"


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def source_ids(stage):
    symptoms = load_json(FIXTURES / stage / "symptoms.json")
    return [row["id"] for row in symptoms["source_symptoms"]]


class Eng3060FixtureInvariantsTest(unittest.TestCase):
    def test_report_fixture_names_all_three_original_symptoms(self):
        report = (FIXTURES / "report.md").read_text(encoding="utf-8")
        self.assertIn("Granola", report)
        self.assertIn("Sahiba", report)
        self.assertIn("Outputs", report)

    def test_initial_conflict_fixture_keeps_all_three_source_symptoms_active(self):
        self.assertEqual(source_ids("initial-conflict"), ["S1", "S2", "S3"])
        dispositions = load_json(FIXTURES / "initial-conflict" / "symptom-dispositions.json")
        self.assertEqual(
            {row["symptom_id"]: row["disposition"] for row in dispositions["dispositions"]},
            {"E1": "unresolved", "E2": "unresolved", "E3": "unresolved"},
        )

    def test_class_hardening_fixture_preserves_granola_as_open_symptom(self):
        self.assertEqual(source_ids("class-hardening"), ["S1", "S2", "S3"])
        dispositions = load_json(FIXTURES / "class-hardening" / "symptom-dispositions.json")
        by_id = {row["symptom_id"]: row["disposition"] for row in dispositions["dispositions"]}
        self.assertEqual(by_id["E1"], "class-hardening-only")

    def test_rca_shape_rejects_narrowed_v1_artifacts_without_symptom_contract(self):
        """The v2 shape gate no longer accepts residual-only symptom scope."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            for name in (
                "repo.json",
                "failing-test.json",
                "fix-plan.json",
                "probe.json",
                "residuals.json",
                "verify.json",
                "files-allowlist.json",
            ):
                shutil.copy2(FIXTURES / "class-hardening" / name, tmp / name)
            result = subprocess.run(
                ["bash", str(RCA_SHAPE), str(tmp)],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("RCA_SHAPE=FAIL", result.stdout)
        self.assertIn("symptoms.json", result.stdout)


class Eng3060FutureContractTest(unittest.TestCase):
    def run_contract(self, stage):
        if not BUGFIX_CONTRACT.exists():
            self.fail(
                "missing expected U1 helper: setup/bugfix-contract.py "
                "(needed to validate ENG-3060 causal coverage)"
            )
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        work = Path(td.name) / stage
        shutil.copytree(FIXTURES / stage, work)
        return subprocess.run(
            ["python3", str(BUGFIX_CONTRACT), "classify", str(work)],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def test_fully_occurrence_covered_fixture_can_claim_full_fix(self):
        result = self.run_contract("full-fix")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("BUGFIX_CLASSIFICATION=OK", result.stdout)
        self.assertIn("implementation=FULL_FIX", result.stdout)
        self.assertIn("ticket=RESOLVED", result.stdout)
        self.assertIn("closure=true", result.stdout)

    def test_h1_conflict_retires_hypothesis_but_keeps_all_symptoms_active(self):
        result = self.run_contract("initial-conflict")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("BUGFIX_CLASSIFICATION=OK", result.stdout)

    def test_h3_class_only_proof_cannot_close_granola_symptom(self):
        result = self.run_contract("class-hardening")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("BUGFIX_CLASSIFICATION=OK", result.stdout)
        self.assertIn("implementation=PARTIAL_FIX", result.stdout)
        self.assertIn("ticket=OPEN", result.stdout)
        self.assertIn("closure=false", result.stdout)

    def test_narrowed_successor_without_parent_lineage_fails_typed(self):
        if not BUGFIX_CONTRACT.exists():
            self.fail("missing expected U1 helper: setup/bugfix-contract.py")
        result = subprocess.run(
            [
                "python3",
                str(BUGFIX_CONTRACT),
                "validate-causal-coverage",
                "--artifacts",
                str(FIXTURES / "narrowed-successor"),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if result.returncode == 2 and "invalid choice: 'validate-causal-coverage'" in result.stderr:
            self.fail(
                "missing expected causal-coverage command: validate-causal-coverage "
                "must reject narrowed successors that drop S1 without parent lineage"
            )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("BUGFIX_COVERAGE=FAIL", result.stdout)
        self.assertIn("missing parent lineage", result.stdout)
        self.assertIn("dropped source symptoms: S1", result.stdout)


if __name__ == "__main__":
    unittest.main()
