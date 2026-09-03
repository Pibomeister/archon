#!/usr/bin/env python3
"""bugfix rca-plan-loop/rca-converge: the accept-by-hand path at the round cap.

`rca-plan-accept.txt` is a human act (mirror of accept-residuals.txt). At the
cap with a REVISE verdict the loop converges when the last critique has no P0
at >=75 confidence, and still stops when a P0 remains. The converge bash is
extracted from the shipped bugfix.yaml (bugfix-lite drops the loop) and run
against a synthetic artifacts dir built on the rca-minimal fixture, which
passes rca-shape.sh."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

SETUP = Path(__file__).resolve().parent.parent
ARCHON = SETUP.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "rca-minimal"
WORKFLOWS = [ARCHON / "workflows" / "bugfix.yaml"]  # bugfix-lite drops rca-plan-loop by design
IMM = ["rca.md", "causal-chain.json", "hypotheses.json", "residuals.json", "probe.json", "repo.json"]
MUT = ["fix-plan.json", "files-allowlist.json", "verify.json", "failing-test.json"]


def converge_bash(workflow):
    doc = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    loop = next(n for n in doc["nodes"] if n["id"] == "rca-plan-loop")
    return next(b for b in loop["loop_group"]["nodes"] if b["id"] == "rca-converge")["bash"]


class AcceptByHand(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ad = self.tmp / "ad"
        shutil.copytree(FIXTURE, self.ad)
        # diagnosis files the fixture lacks, plus durable anchors + pre snapshots
        (self.ad / "rca.md").write_text("## Observation\nx\n")
        (self.ad / "causal-chain.json").write_text(json.dumps({"links": [{"index": 1, "cause": "a"}, {"index": 2, "cause": "b", "fixable": True, "fix_site": "x:1"}]}))
        (self.ad / "hypotheses.json").write_text(json.dumps([{"id": 1, "status": "open"}]))
        proof = json.loads((self.ad / "proof-assessment.json").read_text())
        proof["selected_hypothesis_id"] = "1"
        (self.ad / "proof-assessment.json").write_text(json.dumps(proof))
        for f in IMM:
            shutil.copy(self.ad / f, self.ad / f"imm-{f}")
        shutil.copy(self.ad / "rca.md", self.ad / "rca.pre.md")
        shutil.copy(self.ad / "causal-chain.json", self.ad / "causal-chain.pre.json")
        (self.ad / "rca-round.txt").write_text("1\n")
        (self.ad / "rca-round-cap.txt").write_text("1\n")
        self.rd = self.ad / "rca-round-1"
        self.rd.mkdir()
        for f in MUT:
            shutil.copy(self.ad / f, self.rd / f"pre-{f}")
        # the round moved the plan (so this is not NO_PROGRESS)
        fp = json.loads((self.ad / "fix-plan.json").read_text())
        fp["approach"] = fp.get("approach", "") + " revised"
        (self.ad / "fix-plan.json").write_text(json.dumps(fp))
        (self.rd / "revision.json").write_text(json.dumps({"applied": [{"id": 1}], "declined": [], "skipped": None}))

    def critique(self, findings):
        (self.rd / "critique.json").write_text(json.dumps({"verdict": "REVISE", "findings": findings}))

    def run_converge(self, workflow):
        script = converge_bash(workflow).replace("/Users/eduardopicazo/Documents/Workspace/Goodword/.archon/setup", str(SETUP))
        return subprocess.run(["bash", "-c", script], capture_output=True, encoding="utf-8",
                              env={**os.environ, "ARTIFACTS_DIR": str(self.ad)})

    def test_cap_without_accept_stops(self):
        self.critique([{"severity": "P1", "kind": "gap", "confidence": 100}])
        for wf in WORKFLOWS:
            r = self.run_converge(wf)
            self.assertEqual(r.returncode, 1, wf.name + r.stdout + r.stderr)
            self.assertIn("RCA_PLAN_ROUND_CAP round=1 cap=1", r.stdout)
            self.assertNotIn("RCA_PLAN_CONVERGED", r.stdout)

    def test_accept_with_only_p1_converges(self):
        self.critique([{"severity": "P1", "kind": "gap", "confidence": 100}, {"severity": "P2", "kind": "scope", "confidence": 90}])
        (self.ad / "rca-plan-accept.txt").write_text("edy: residual P1s are test-scope; accepted 2026-08-29\n")
        for wf in WORKFLOWS:
            r = self.run_converge(wf)
            self.assertEqual(r.returncode, 0, wf.name + r.stdout + r.stderr)
            self.assertIn("<promise>RCA_PLAN_CONVERGED</promise>", r.stdout)
            self.assertIn("human accepted at cap: edy:", r.stdout)

    def test_accept_never_waives_a_p0(self):
        self.critique([{"severity": "P0", "kind": "regression", "confidence": 100}])
        (self.ad / "rca-plan-accept.txt").write_text("edy: trying to force it\n")
        for wf in WORKFLOWS:
            r = self.run_converge(wf)
            self.assertEqual(r.returncode, 1, wf.name + r.stdout)
            self.assertIn("P0=1 open", r.stdout)
            self.assertNotIn("RCA_PLAN_CONVERGED", r.stdout)

    def test_low_confidence_p0_is_not_blocking(self):
        self.critique([{"severity": "P0", "kind": "gap", "confidence": 40}])
        (self.ad / "rca-plan-accept.txt").write_text("edy: accepted\n")
        for wf in WORKFLOWS:
            r = self.run_converge(wf)
            self.assertEqual(r.returncode, 0, wf.name + r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
