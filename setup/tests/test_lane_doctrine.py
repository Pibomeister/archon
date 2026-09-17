#!/usr/bin/env python3
"""lane-doctrine.py locks the prompt text every lane shares.

The claude<->codex axis is already one graph: the twins are generated and
CODEX_DRIFT fails the suite on divergence. The full<->lite axis is not -- the
lite overlays replace a parent prompt wholesale, so a parent edit can silently
miss the lite lane. That is an observed defect, not a hypothetical: the review
persona-sizing fix needed patching at five sites and bugfix-lite took the yaml
half without the prompt half. These tests are that gap's guard."""
import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ARCHON = Path(__file__).resolve().parent.parent.parent
SCRIPT = ARCHON / "setup" / "lane-doctrine.py"

_spec = importlib.util.spec_from_file_location("lane_doctrine", SCRIPT)
ld = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ld)


class LaneDoctrine(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.wf = self.tmp / "workflows"
        self.wf.mkdir()
        for lane in ld.LANES:
            shutil.copy(ARCHON / "workflows" / f"{lane}.yaml", self.wf / f"{lane}.yaml")

    def drop(self, lane, line):
        p = self.wf / f"{lane}.yaml"
        text = p.read_text(encoding="utf-8")
        self.assertIn(line, text, f"{lane}: locked line not present verbatim to drop")
        p.write_text(text.replace(line, ""), encoding="utf-8")

    def a_locked_line(self, node="fixer"):
        locked = ld.shared(str(ARCHON / "workflows"))
        self.assertIn(node, locked, f"locked nodes must include the {node} prompt")
        return locked[node][0]

    def test_shipped_lanes_are_locked(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "check"], capture_output=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("LANE_DOCTRINE=OK", r.stdout)

    def test_the_lock_is_not_vacuous(self):
        # A probe that locks nothing passes every mutation. The floor is well
        # under today's count (21 nodes / 591 lines) so ordinary prompt edits
        # do not trip it, but an empty or near-empty lock does.
        locked = ld.shared(str(ARCHON / "workflows"))
        self.assertGreaterEqual(len(locked), 10, "too few shared prompt nodes locked")
        self.assertGreaterEqual(sum(len(v) for v in locked.values()), 200)
        for node in ("fixer", "prbody"):
            self.assertIn(node, locked, f"{node} appears in 5 lanes and must be locked")

    def test_one_lane_dropping_a_shared_line_is_drift(self):
        # The exact shape of the observed bug: the fix lands everywhere but one.
        line = self.a_locked_line()
        self.drop("bugfix-lite", line)
        ok, msg = ld.check(str(self.wf))
        self.assertFalse(ok, msg)
        self.assertIn("LANE_DOCTRINE=FAIL", msg)
        self.assertIn("bugfix-lite", msg)
        self.assertIn("node fixer", msg)

    def test_every_lane_dropping_it_is_a_relock_not_drift(self):
        # The boundary: removing doctrine everywhere is a deliberate act that
        # `update` records. Only a PARTIAL removal is the silent-divergence bug.
        line = self.a_locked_line()
        for lane in ld.LANES:
            p = self.wf / f"{lane}.yaml"
            p.write_text(p.read_text(encoding="utf-8").replace(line, ""), encoding="utf-8")
        ok, msg = ld.check(str(self.wf))
        self.assertTrue(ok, msg)

    def test_review_is_deliberately_absent_from_the_lock(self):
        # It used to be locked across all five lanes. full-sdlc-api and its two
        # derivatives now embed a measured mode prompt from setup/prompts
        # (embed-prompts.py), so the three v2 lanes and the two v1 lanes share
        # no substantive review line and `shared()` drops the node. That is the
        # intended end state, not drift: what stops a mode swap from quietly
        # deleting the reviewer's execution contract is test_prompt_embedding,
        # which pins the mode-independent preamble, not this lock.
        locked = ld.shared(str(ARCHON / "workflows"))
        self.assertNotIn("review", locked)

    def test_update_rewrites_the_lock_deterministically(self):
        first = ld.shared(str(ARCHON / "workflows"))
        second = ld.shared(str(ARCHON / "workflows"))
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
