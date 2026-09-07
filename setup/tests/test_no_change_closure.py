import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "no-change-closure.py"
module_spec = importlib.util.spec_from_file_location("no_change_closure", SCRIPT)
no_change = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(no_change)


class NoChangeClosureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.artifacts = Path(self.temp.name)
        self.product = {"files": [], "sha256": "a" * 64}
        self.write(
            "run-context.json",
            {
                "profile": {
                    "noChangeClosure": {
                        "enabled": True,
                        "verifierIds": ["unit", "lint"],
                    }
                }
            },
        )
        self.write(
            "baseline-verification.json",
            {
                "passed": True,
                "commands": [
                    {"id": "unit", "exitCode": 0},
                    {"id": "lint", "exitCode": 0},
                ],
                "workProduct": self.product,
            },
        )
        self.write(
            "verification.json",
            {
                "passed": True,
                "commands": [
                    {"id": "unit", "exitCode": 0},
                    {"id": "lint", "exitCode": 0},
                ],
                "workProduct": self.product,
            },
        )
        self.write(
            "no-change-claim.json",
            {
                "source": "agent",
                "category": "already-satisfied",
                "summary": "The requested outcome already holds.",
                "workProduct": self.product,
            },
        )

    def write(self, name, value):
        (self.artifacts / name).write_text(json.dumps(value), encoding="utf-8")

    def test_verified_no_change_writes_distinct_lifecycle_intent(self):
        result = no_change.verify(self.artifacts)
        self.assertEqual(result["lifecycleResult"], "fulfilled-no-change")
        self.assertEqual(result["verifierIds"], ["unit", "lint"])
        written = json.loads((self.artifacts / "no-change-closure-intent.json").read_text())
        self.assertEqual(written["lifecycleResult"], "fulfilled-no-change")

    def test_disabled_policy_rejects_claim(self):
        self.write("run-context.json", {"profile": {"noChangeClosure": {"enabled": False, "verifierIds": ["unit"]}}})
        with self.assertRaisesRegex(ValueError, "not enabled"):
            no_change.verify(self.artifacts)

    def test_claim_category_is_not_proof(self):
        self.write("no-change-claim.json", {"source": "agent", "category": "changes-produced"})
        with self.assertRaisesRegex(ValueError, "already-satisfied"):
            no_change.verify(self.artifacts)

    def test_baseline_must_pass_pinned_verifiers(self):
        self.write(
            "baseline-verification.json",
            {
                "passed": True,
                "commands": [{"id": "unit", "exitCode": 0}, {"id": "lint", "exitCode": 1}],
                "workProduct": self.product,
            },
        )
        with self.assertRaisesRegex(ValueError, "baseline verification"):
            no_change.verify(self.artifacts)

    def test_changed_work_product_blocks_no_change_closure(self):
        self.write(
            "verification.json",
            {
                "passed": True,
                "commands": [{"id": "unit", "exitCode": 0}, {"id": "lint", "exitCode": 0}],
                "workProduct": {"files": [{"path": "view.txt", "sha256": "b" * 64}], "sha256": "c" * 64},
            },
        )
        with self.assertRaisesRegex(ValueError, "changed work product"):
            no_change.verify(self.artifacts)

    def test_missing_work_product_proof_blocks_no_change_closure(self):
        self.write("verification.json", {"passed": True, "commands": [{"id": "unit", "exitCode": 0}, {"id": "lint", "exitCode": 0}]})
        with self.assertRaisesRegex(ValueError, "missing work product proof"):
            no_change.verify(self.artifacts)

    def test_stale_round_claim_blocks_no_change_closure(self):
        self.write(
            "verification.json",
            {
                "passed": True,
                "commands": [{"id": "unit", "exitCode": 0}, {"id": "lint", "exitCode": 0}],
                "workProduct": self.product,
                "round": 2,
            },
        )
        self.write(
            "no-change-claim.json",
            {
                "source": "agent",
                "category": "already-satisfied",
                "summary": "The requested outcome already holds.",
                "workProduct": self.product,
                "round": 1,
            },
        )
        with self.assertRaisesRegex(ValueError, "stale"):
            no_change.verify(self.artifacts)


if __name__ == "__main__":
    unittest.main()
