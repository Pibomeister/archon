#!/usr/bin/env python3
"""full-sdlc-api plan-loop: accept-by-hand at the round cap (plan-accept.txt),
the mirror of rca-plan-accept.txt. Uses the plan-minimal fixture, which passes
plan-shape.sh. Only the cap branch of plan-converge is under test: the run is
staged so the verdict is REVISE with a moved plan."""
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
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "plan-minimal"


def converge_bash():
    doc = yaml.safe_load((ARCHON / "workflows" / "full-sdlc-api.yaml").read_text(encoding="utf-8"))
    loop = next(n for n in doc["nodes"] if n["id"] == "plan-loop")
    return next(b for b in loop["loop_group"]["nodes"] if b["id"] == "plan-converge")["bash"]


class PlanAccept(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ad = self.tmp / "ad"
        shutil.copytree(FIXTURE, self.ad)
        self.wt = self.tmp / "wt"
        shutil.copytree(FIXTURE, self.wt)  # premise evidence files, if any, resolve here
        (self.ad / "params.json").write_text(json.dumps({"spec": str(self.ad / "spec.md"), "slug": "x", "branch": "archon/x", "worktree": str(self.wt)}))
        (self.ad / "plan-round.txt").write_text("1\n")
        (self.ad / "plan-round-cap.txt").write_text("1\n")
        self.rd = self.ad / "plan-round-1"
        self.rd.mkdir()
        (self.rd / "pre-plan.md").write_text((self.ad / "plan.md").read_text() + "\n<!-- pre -->\n")
        for f in ("files-allowlist.json", "verify.json", "reader-audit.json"):
            shutil.copy(self.ad / f, self.rd / f"pre-{f}")
        (self.rd / "revision.json").write_text(json.dumps({"applied": [{"id": 1}], "declined": []}))

    def critique(self, findings):
        (self.rd / "critique.json").write_text(json.dumps({"verdict": "REVISE", "findings": findings}))

    def go(self):
        script = converge_bash().replace("/Users/eduardopicazo/Documents/Workspace/Goodword/.archon/setup", str(SETUP))
        return subprocess.run(["bash", "-c", script], capture_output=True, encoding="utf-8", env={**os.environ, "ARTIFACTS_DIR": str(self.ad)})

    def test_cap_without_accept_stops(self):
        self.critique([{"severity": "P1", "kind": "gap", "confidence": 100}])
        r = self.go()
        self.assertNotIn("<promise>PLAN_CONVERGED</promise>", r.stdout)
        self.assertIn("PLAN_ROUND_CAP round=1 cap=1", r.stdout + r.stderr)

    def test_accept_with_only_p1_converges(self):
        self.critique([{"severity": "P1", "kind": "gap", "confidence": 100}])
        (self.ad / "plan-accept.txt").write_text("edy: accepted\n")
        r = self.go()
        self.assertIn("<promise>PLAN_CONVERGED</promise>", r.stdout, r.stdout + r.stderr)
        self.assertIn("human accepted at cap: edy:", r.stdout)

    def test_accept_never_waives_a_p0(self):
        self.critique([{"severity": "P0", "kind": "regression", "confidence": 90}])
        (self.ad / "plan-accept.txt").write_text("edy: force\n")
        r = self.go()
        self.assertNotIn("<promise>PLAN_CONVERGED</promise>", r.stdout)
        self.assertIn("P0=1 open", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
