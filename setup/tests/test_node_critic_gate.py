"""plan-critic-gate / rca-critic-gate must reject a non-integer round counter
before using it to build a path. Their four sibling nodes (plan-round-pre,
plan-converge, rca-round-pre, rca-converge) already guard `case "$N" in
''|*[!0-9]*)`; these two did not, so a path-shaped counter such as
`1/../plan-round-1` resolved to a real directory and the gate printed a
PASS-class `CRITIQUE round=1/../plan-round-1 ...` line with rc 0 (found by the
round-2 architect review, 2026-08-29)."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from nodes.extract import runnable_body

LANES = {"full-sdlc-api": ("plan-critic-gate", "plan-round"), "bugfix": ("rca-critic-gate", "rca-round")}
CRITIQUE = {"verdict": "ACCEPT", "findings": [], "scope_disputes": []}


def run_gate(workflow, counter):
    node, prefix = LANES[workflow]
    art = Path(tempfile.mkdtemp(prefix="cg-"))
    (art / f"{prefix}.txt").write_text(counter)
    rd = art / f"{prefix}-1"
    rd.mkdir()
    (rd / "critique.json").write_text(json.dumps(CRITIQUE))
    body = runnable_body(workflow, node)
    p = subprocess.run(["bash", "-c", body], capture_output=True, text=True,
                       env=dict(os.environ, ARTIFACTS_DIR=str(art)))
    shutil.rmtree(art, ignore_errors=True)
    return p


class CriticGateRoundGuard(unittest.TestCase):
    def check(self, workflow):
        _, prefix = LANES[workflow]
        for bad in ("1/../%s-1" % prefix, "one", "", " 1"):
            p = run_gate(workflow, bad)
            self.assertEqual(p.returncode, 1, (bad, p.stdout, p.stderr))
            self.assertIn(f"CRITIC_GATE=FAIL {prefix}.txt is not an integer: [{bad}]", p.stdout + p.stderr, bad)
            self.assertNotIn("CRITIQUE round=", p.stdout, bad)
        good = run_gate(workflow, "1")
        self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
        self.assertIn("CRITIQUE round=1 ", good.stdout + good.stderr)

    def test_full_sdlc_api(self):
        self.check("full-sdlc-api")

    def test_bugfix(self):
        self.check("bugfix")


if __name__ == "__main__":
    unittest.main()
