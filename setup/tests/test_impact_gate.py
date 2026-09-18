#!/usr/bin/env python3
"""impact.json is an AI-authored envelope. A bash gate must json.load it before
the critic prompt is allowed to treat it as typed evidence."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

SETUP = Path(__file__).resolve().parent.parent
ARCHON = SETUP.parent
sys.path.insert(0, str(SETUP))

from nodes.extract import runnable_body  # noqa: E402


def walk(nodes):
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        yield n
        lg = n.get("loop_group")
        if isinstance(lg, dict):
            yield from walk(lg.get("nodes"))


def node(workflow, nid):
    doc = yaml.safe_load((ARCHON / "workflows" / f"{workflow}.yaml").read_text(encoding="utf-8"))
    return next(n for n in walk(doc.get("nodes")) if n.get("id") == nid)


class ImpactGateHelper(unittest.TestCase):
    def run_gate(self, payload, round_no="1"):
        tmp = Path(tempfile.mkdtemp(prefix="ig-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = tmp / "impact.json"
        if isinstance(payload, str):
            path.write_text(payload, encoding="utf-8")
        else:
            path.write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(SETUP / "impact_gate.py"), str(path), "--round", round_no],
            capture_output=True, text=True,
        )

    def test_gathered_passes(self):
        r = self.run_gate({
            "status": "GATHERED",
            "symbols": [{"name": "Foo.bar", "file": "x.ts", "d1_callers": ["A"], "risk": "LOW"}],
        })
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("IMPACT_GATE=PASS round=1 status=GATHERED symbols=1", r.stdout)

    def test_skipped_requires_empty_symbols(self):
        r = self.run_gate({"status": "SKIPPED", "symbols": [{"name": "x", "file": "f", "d1_callers": [], "risk": "LOW"}]})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("IMPACT_GATE=FAIL", r.stdout + r.stderr)

    def test_malformed_json_fails(self):
        r = self.run_gate("{not json")
        self.assertEqual(r.returncode, 1)
        self.assertIn("IMPACT_GATE=FAIL", r.stdout + r.stderr)

    def test_unknown_status_fails(self):
        r = self.run_gate({"status": "MAYBE", "symbols": []})
        self.assertEqual(r.returncode, 1)
        self.assertIn("IMPACT_GATE=FAIL", r.stdout + r.stderr)


class ImpactGateWiring(unittest.TestCase):
    def test_plan_loop_gate_sits_between_probe_and_critic(self):
        probe = node("full-sdlc-api", "impact-probe")
        gate = node("full-sdlc-api", "impact-gate")
        critic = node("full-sdlc-api", "plan-critic")
        self.assertEqual(probe["depends_on"], ["plan-round-pre"])
        self.assertEqual(gate["depends_on"], ["impact-probe"])
        self.assertEqual(critic["depends_on"], ["impact-gate"])
        self.assertIn("impact_gate.py", gate["bash"])

    def test_rca_loop_gate_sits_between_probe_and_critic(self):
        probe = node("bugfix", "impact-probe")
        gate = node("bugfix", "impact-gate")
        critic = node("bugfix", "rca-critic")
        self.assertEqual(probe["depends_on"], ["rca-round-pre"])
        self.assertEqual(gate["depends_on"], ["impact-probe"])
        self.assertEqual(critic["depends_on"], ["impact-gate"])
        self.assertIn("impact_gate.py", gate["bash"])

    def test_extracted_plan_gate_body_json_loads_round_file(self):
        tmp = Path(tempfile.mkdtemp(prefix="igw-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "plan-round.txt").write_text("1\n")
        rd = tmp / "plan-round-1"
        rd.mkdir()
        (rd / "impact.json").write_text(json.dumps({
            "status": "SKIPPED", "symbols": [],
        }))
        body = runnable_body("full-sdlc-api", "impact-gate")
        p = subprocess.run(["bash", "-c", body], capture_output=True, text=True,
                           env=dict(os.environ, ARTIFACTS_DIR=str(tmp)))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("IMPACT_GATE=PASS", p.stdout)


if __name__ == "__main__":
    unittest.main()
