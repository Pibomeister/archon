#!/usr/bin/env python3
import unittest
from pathlib import Path
import yaml

ARCHON = Path(__file__).resolve().parents[2]


class IntakeSymptomContractTest(unittest.TestCase):
    def test_full_and_lite_intake_require_v2_immutable_ledger(self):
        full = yaml.safe_load((ARCHON / "workflows/bugfix.yaml").read_text())
        intake = next(n for n in full["nodes"] if n["id"] == "intake")["prompt"]
        gate = next(n for n in full["nodes"] if n["id"] == "intake-gate")["bash"]
        lite = (ARCHON / "setup/lite/bugfix/intake.prompt.md").read_text()
        lite_gate = (ARCHON / "setup/lite/bugfix/intake-gate.bash.sh").read_text()
        for prompt in (intake, lite):
            for token in ("symptoms.json", "source_symptoms", "effective_symptoms", "byte_span", "expected_behavior", "actual_behavior"):
                self.assertIn(token, prompt)
            self.assertIn("At least one symptom", prompt)
        for body in (gate, lite_gate):
            self.assertIn("bugfix-contract.py", body)
            self.assertIn("seal-ledger", body)

    def test_rca_consumes_ledger_and_emits_proof_axes(self):
        full = yaml.safe_load((ARCHON / "workflows/bugfix.yaml").read_text())
        rca = next(n for n in full["nodes"] if n["id"] == "rca")["prompt"]
        for token in ("symptoms.json", "symptom-dispositions.json", "causal-coverage.json", "proof-assessment.json", "mechanism_valid", "occurrence_attributed", "debug-phase.json", "boundary-trace.json", "pattern-comparison.json"):
            self.assertIn(token, rca)


if __name__ == "__main__":
    unittest.main()
