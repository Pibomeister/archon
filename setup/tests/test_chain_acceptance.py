#!/usr/bin/env python3
"""chain-acceptance.py: PARTIAL for a residual is not PARTIAL for a blocker.

The live gate accepts PASS, or PARTIAL naming only residuals.json dispositions.
Both outcomes print the same `ACCEPTANCE=PARTIAL failures=<n>` line, so the
distinction has to be in the output, not in the reader's head: residual_only
says whether every failure named is a P2/P3 the run deliberately carried out.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ARCHON = Path(__file__).resolve().parent.parent.parent
CHECKER = ARCHON / "setup" / "chain-acceptance.py"


class AcceptanceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.control = self.root / "control"
        self.control.mkdir()
        self.artifacts = self.root / "api-run"
        (self.artifacts / "round-1").mkdir(parents=True)
        self.write_round(1, {"applied": [], "failed": [], "advisory": []})
        (self.artifacts / "round-1" / "converge.txt").write_text(
            "ROUND=1 verdict=[Ready to merge]\nCONVERGED round=1\n", encoding="utf-8")
        (self.artifacts / "smoke-result.txt").write_text("SMOKE=PASS api\n", encoding="utf-8")
        self.state = {
            "status": "locally_verified",
            "candidate_handoffs": {"api": {"artifacts": str(self.artifacts)}},
            "integration": {"integration_evidence": {"status": "passed"}},
            "approved_plan": {
                "acceptance_criteria": [{"id": "AC1", "text": "works"}],
                "integration": {"scenarios": [{"name": "s", "covers": ["AC1"]}]},
            },
        }

    def write_round(self, n, result):
        target = self.artifacts / f"round-{n}"
        target.mkdir(parents=True, exist_ok=True)
        (target / "fixer-result.json").write_text(json.dumps(result), encoding="utf-8")

    def check(self):
        (self.control / "chain-1.json").write_text(json.dumps(self.state), encoding="utf-8")
        env = dict(os.environ, ARCHON_CHAIN_CONTROL_DIR=str(self.control))
        return subprocess.run([sys.executable, str(CHECKER), "chain-1"],
                              capture_output=True, encoding="utf-8", env=env)

    def test_a_clean_chain_passes(self):
        proc = self.check()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("ACCEPTANCE=PASS", proc.stdout)

    def test_a_pin_conflict_partition_fails_clause_three(self):
        self.write_round(1, {"applied": [], "failed": [], "advisory": [],
                             "pin_conflict": [{"finding": "f", "action": "a",
                                               "symbol": "GroupService.shareGroup"}]})
        proc = self.check()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("FAIL api: clause3 round-1 pin_conflict non-empty", proc.stdout)
        self.assertIn("residual_only=false", proc.stdout)

    def test_the_other_blocking_partitions_still_fail(self):
        for key in ("failed", "incomplete", "cross_repo"):
            with self.subTest(partition=key):
                self.write_round(1, {"applied": [], "failed": [], "advisory": [],
                                     key: [{"finding": "f", "action": "a"}]})
                proc = self.check()
                self.assertEqual(proc.returncode, 1)
                self.assertIn(f"clause3 round-1 {key} non-empty", proc.stdout)

    def test_a_residual_disposition_is_partial_and_tolerated(self):
        (self.artifacts / "residuals.json").write_text(
            json.dumps([{"id": "a", "severity": "P3", "state": "deferred"},
                        {"id": "b", "severity": "P2", "state": "pinned"}]),
            encoding="utf-8")
        proc = self.check()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("clause3 residuals.json disposition deferred=1", proc.stdout)
        self.assertIn("clause3 residuals.json disposition pinned=1", proc.stdout)
        self.assertIn("ACCEPTANCE=PARTIAL failures=2 residual_only=true", proc.stdout)

    def test_a_residual_beside_a_real_failure_is_not_tolerated(self):
        (self.artifacts / "residuals.json").write_text(
            json.dumps([{"id": "a", "severity": "P3", "state": "deferred"}]), encoding="utf-8")
        (self.artifacts / "accept-residuals.txt").write_text("human\n", encoding="utf-8")
        proc = self.check()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("residual_only=false", proc.stdout)

    def test_a_human_bypass_fails_clause_one(self):
        (self.artifacts / "yield-stop.txt").write_text("stop\n", encoding="utf-8")
        proc = self.check()
        self.assertIn("clause1 yield-stop.txt present", proc.stdout)

    def test_a_converge_that_is_not_clean_fails_clause_four(self):
        (self.artifacts / "round-1" / "converge.txt").write_text(
            "CONVERGED round=1 (human accepted residuals)\n", encoding="utf-8")
        proc = self.check()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("clause4 final converge not clean", proc.stdout)

    def test_an_uncovered_criterion_fails_clause_six(self):
        self.state["approved_plan"]["acceptance_criteria"].append({"id": "AC2", "text": "x"})
        proc = self.check()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("clause6 uncovered criteria: ['AC2']", proc.stdout)


if __name__ == "__main__":
    unittest.main()
