#!/usr/bin/env python3
"""deslop-review-gate: check-slop decides complexity, the reviewer only reports it.

`check-slop.py` already separates the two complexity cases the reviewer cannot
tell apart from the file alone:

  SLOP=FAIL   a function this change created, or raised past the threshold
  SLOP=REPORT a function that was already over the threshold and was not worsened

Only the first is blocking, by design. A reviewer reading the same file sees one
absolute number, files the pre-existing case at confidence 100, and stops the run
for work the mechanical guard itself declined to demand. The gate now keeps that
finding and drops it to confidence 50, printing what it did.

Live, `deslop-recheck` hard-fails on any SLOP=FAIL before this node runs, so the
blocking branch here is a unit-level belt: it pins that the gate reads slop.txt
rather than assuming it is always empty of failures.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from nodes.extract import runnable_body

GUARDS = ("complexity", "tautological_tests", "yagni", "open_closed", "comments")
PREEXISTING = ("SLOP=REPORT complexity fn=renderRow file=src/x.ts value=15\n"
               "SLOP=OK files=1\n")
NEW_OVER = ("SLOP=FAIL complexity file=src/x.ts line=3 fn=newThing value=12 "
            "threshold=10 new=true\nSLOP=FAIL count=1\n")


def sh(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, shell=True, check=True, capture_output=True)


def capture(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True,
                          encoding="utf-8", check=True).stdout.strip()


def review(findings, verdict):
    return json.dumps({
        "verdict": verdict,
        "coverage": {g: {"status": "assessed", "evidence": "read the diff"} for g in GUARDS},
        "findings": findings,
    })


def finding(guard="complexity", file="src/x.ts", line=3, confidence=100):
    return {"guard": guard, "file": file, "line": line,
            "confidence": confidence, "evidence": "cyclomatic complexity 15"}


class DeslopComplexity(unittest.TestCase):
    def run_gate(self, *, slop, findings, verdict="DIRTY"):
        tmp = Path(tempfile.mkdtemp(prefix="dc-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        wt = tmp / "wt"
        (wt / "src").mkdir(parents=True)
        (wt / "src" / "x.ts").write_text("export const a = 1;\n")
        sh("git init -q && git config user.email t@t && git config user.name t "
           "&& git add . && git commit -qm base", wt)
        ad = tmp / "ad"
        rd = ad / "deslop-round-1"
        rd.mkdir(parents=True)
        (ad / "deslop-round.txt").write_text("1\n")
        (ad / "params.json").write_text(json.dumps(
            {"spec": "/x.md", "slug": "x", "branch": "archon/x", "worktree": str(wt)}))
        (rd / "slop.txt").write_text(slop)
        (ad / "deslop-review.json").write_text(review(findings, verdict))
        # The gate's tamper check recomputes head, live-index tree and a
        # throwaway-index tree over the whole worktree. On a clean committed
        # tree all three are knowable up front, so the checkpoint is real rather
        # than stubbed and a reviewer edit would still be caught.
        head = capture("git rev-parse HEAD", wt)
        tree = capture("git write-tree", wt)
        (rd / "checkpoint-tree.txt").write_text(tree + "\n")
        (ad / "deslop-tree.txt").write_text(
            f"head={head}\nindex={tree}\ncheckpoint={tree}\n")
        body = runnable_body("full-sdlc-api", "deslop-review-gate", root=str(tmp))
        # The body shells into <root>/.archon/setup for params-env.sh.
        m = tmp / ".archon" / "setup"
        m.mkdir(parents=True)
        for p in (Path(__file__).resolve().parent.parent).iterdir():
            (m / p.name).symlink_to(p)
        env = dict(os.environ, ARTIFACTS_DIR=str(ad))
        return subprocess.run(["bash", "-c", body], capture_output=True,
                              encoding="utf-8", env=env, cwd=str(tmp))

    def test_a_preexisting_function_does_not_block(self):
        p = self.run_gate(slop=PREEXISTING, findings=[finding()])
        self.assertIn("DESLOP_COMPLEXITY_DOWNGRADED fn=renderRow file=src/x.ts:3 "
                      "confidence=100->50", p.stdout, p.stdout + p.stderr)
        self.assertIn("blocking=0 downgraded=1", p.stdout)
        self.assertIn("DESLOP=CLEAN round=1", p.stdout)
        self.assertIn("<promise>DESLOP_CLEAN</promise>", p.stdout)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_a_new_function_over_the_threshold_still_blocks(self):
        p = self.run_gate(slop=NEW_OVER, findings=[finding()])
        self.assertNotIn("DESLOP_COMPLEXITY_DOWNGRADED", p.stdout)
        self.assertIn("DESLOP_FINDING guard=complexity file=src/x.ts:3 confidence=100",
                      p.stdout, p.stdout + p.stderr)
        self.assertIn("DESLOP=DIRTY round=1 blocking=1", p.stdout)
        self.assertIn("DESLOP_RETRY=PASS round=1 dirty_rounds=1", p.stdout)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_the_downgrade_is_scoped_to_the_complexity_guard(self):
        p = self.run_gate(slop=PREEXISTING, findings=[finding(guard="yagni")])
        self.assertNotIn("DESLOP_COMPLEXITY_DOWNGRADED", p.stdout)
        self.assertIn("DESLOP=DIRTY round=1 blocking=1", p.stdout)
        self.assertIn("DESLOP_RETRY=PASS round=1 dirty_rounds=1", p.stdout)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_the_downgrade_is_scoped_to_the_named_file(self):
        # A SLOP=FAIL for another file is not cover for this one.
        p = self.run_gate(
            slop="SLOP=FAIL complexity file=src/other.ts line=9 fn=z value=14 threshold=10\n",
            findings=[finding()])
        self.assertIn("DESLOP_COMPLEXITY_DOWNGRADED fn=?", p.stdout, p.stdout + p.stderr)
        self.assertIn("DESLOP=CLEAN round=1", p.stdout)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_a_confidence_50_complexity_finding_is_left_alone(self):
        p = self.run_gate(slop=PREEXISTING, findings=[finding(confidence=50)],
                          verdict="CLEAN")
        self.assertNotIn("DESLOP_COMPLEXITY_DOWNGRADED", p.stdout)
        self.assertIn("blocking=0 downgraded=0", p.stdout)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_a_declared_dirty_with_no_downgrade_is_still_inconsistent(self):
        # The pre-existing fail-closed check must survive: DIRTY with nothing
        # blocking and nothing downgraded is still a reviewer contradicting
        # its own findings.
        p = self.run_gate(slop=PREEXISTING, findings=[], verdict="DIRTY")
        self.assertIn("DESLOP_REVIEW=FAIL verdict inconsistent round=1", p.stdout)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)

    def test_a_missing_slop_file_downgrades_rather_than_erroring(self):
        p = self.run_gate(slop="", findings=[finding()])
        self.assertIn("DESLOP_COMPLEXITY_DOWNGRADED", p.stdout, p.stdout + p.stderr)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
