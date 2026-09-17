#!/usr/bin/env python3
"""deslop-verify: a DIRTY verdict below the cap is a retry, not a failed run.

archon 0.10.1 fails a loop_group the moment any body node fails ("Loop-group
node 'deslop-verify' failed at iteration 1"), so a gate that exits 1 to mean
"not clean yet" ends the loop before iteration 2 can run. Run 180f2453 lost an
otherwise green stage to one task-history comment that way. The body is now

  deslop-fix-pre -> deslop-fix (when a fix is pending) -> deslop-recheck
    -> deslop-review -> deslop-review-gate

and the gate exits 0 without the promise on a first DIRTY verdict, leaving
deslop-fix-pending.txt for the next iteration's fixer. Only the second DIRTY
verdict (DESLOP_ROUND_CAP), mechanical failures and validator errors stop.
Every body is extracted live from the workflow YAML.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from nodes.extract import WORKFLOWS, runnable_body
from nodes.runner import _isolation_env
from test_node_stress import (deslop_common, deslop_review, deslop_review_gate_fixture,
                              jdump, prerun, run_node)

LANES = (("full-sdlc-api", "api"), ("bugfix", "bugfix"))
BODY = ["deslop-fix-pre", "deslop-fix", "deslop-recheck", "deslop-review", "deslop-review-gate"]


def fix_result(round_no=1):
    return {"round": round_no,
            "fixed": [{"guard": "comments", "file": "src/foo.ts", "line": 2, "action": "deleted"}],
            "not_fixed": []}


def run_fix_pre(tmp):
    body = runnable_body("full-sdlc-api", "deslop-fix-pre")
    env = dict(os.environ, **_isolation_env(tmp), ARTIFACTS_DIR=str(tmp / "artifacts"))
    return subprocess.run(["bash", "-c", body], capture_output=True, encoding="utf-8",
                          env=env, cwd=str(tmp))


def dirty_then_fixed(workflow, lane, second_review):
    """Round 1 DIRTY -> fixer result -> round 2 recheck, then `second_review`."""
    def build(tmp):
        deslop_review_gate_fixture(lane, deslop_review("DIRTY", blocking=1))(tmp)
        prerun(workflow, "deslop-review-gate", tmp)
        art = tmp / "artifacts"
        pre = prerun(workflow, "deslop-fix-pre", tmp)
        assert json.loads(pre.stdout.strip().splitlines()[-1]) == {"fix": "pending", "round": "1"}, pre.stdout
        jdump(art / "deslop-fix-result.json", fix_result(1))
        prerun(workflow, "deslop-recheck", tmp)
        jdump(art / "deslop-review.json", second_review)
    return build


class DirtyRetriesBelowTheCap(unittest.TestCase):
    def test_first_dirty_verdict_continues_the_loop(self):
        for workflow, lane in LANES:
            with self.subTest(workflow=workflow):
                r = run_node(workflow, "deslop-review-gate",
                             deslop_review_gate_fixture(lane, deslop_review("DIRTY", blocking=1)))
                self.assertEqual(r["rc"], 0, r["output"])
                self.assertIn("DESLOP=DIRTY round=1 blocking=1", r["output"])
                self.assertIn("DESLOP_RETRY=PASS round=1 dirty_rounds=1", r["output"])
                self.assertNotIn("<promise>DESLOP_CLEAN</promise>", r["output"])
                self.assertEqual(r["files"]["deslop-fix-pending.txt"].strip(), "1")
                self.assertEqual(r["files"]["deslop-dirty.txt"].strip(), "1")
                blocking = json.loads(r["files"]["deslop-round-1/blocking-findings.json"])
                self.assertEqual(blocking, {"round": 1, "findings": [
                    {"guard": "comments", "file": "src/foo.ts", "line": 2,
                     "confidence": 75, "evidence": "narrating comment"}]})

    def test_second_dirty_verdict_is_the_cap(self):
        for workflow, lane in LANES:
            with self.subTest(workflow=workflow):
                r = run_node(workflow, "deslop-review-gate",
                             dirty_then_fixed(workflow, lane, deslop_review("DIRTY", blocking=1)))
                self.assertEqual(r["rc"], 1, r["output"])
                self.assertIn("DESLOP_ROUND_CAP round=2 dirty_rounds=2", r["output"])
                self.assertNotIn("DESLOP_RETRY", r["output"])
                # No marker at the cap: a resume after a hand fix must not buy
                # another automatic fixer pass.
                self.assertNotIn("deslop-fix-pending.txt", r["files"])

    def test_clean_after_the_fix_completes_the_loop(self):
        for workflow, lane in LANES:
            with self.subTest(workflow=workflow):
                r = run_node(workflow, "deslop-review-gate",
                             dirty_then_fixed(workflow, lane, deslop_review()))
                self.assertEqual(r["rc"], 0, r["output"])
                self.assertIn("DESLOP=CLEAN round=2", r["output"])
                self.assertIn("<promise>DESLOP_CLEAN</promise>", r["output"])
                self.assertEqual(json.loads(r["files"]["deslop-round-2/fix-result.json"]), fix_result(1))
                self.assertNotIn("deslop-fix-pending.txt", r["files"])

    def test_junk_dirty_counter_still_cannot_switch_the_cap_off(self):
        for workflow, lane in LANES:
            def build(tmp, lane=lane):
                deslop_review_gate_fixture(lane, deslop_review("DIRTY", blocking=1))(tmp)
                (tmp / "artifacts" / "deslop-dirty.txt").write_text("2x")
            with self.subTest(workflow=workflow):
                r = run_node(workflow, "deslop-review-gate", build)
                self.assertEqual(r["rc"], 0, r["output"])
                self.assertIn("DESLOP_RETRY=PASS round=1 dirty_rounds=1", r["output"])


class RecheckConsumesTheMarker(unittest.TestCase):
    def pending(self, lane, result=True):
        def build(tmp):
            deslop_common(tmp, lane)
            art = tmp / "artifacts"
            (art / "deslop-fix-pending.txt").write_text("1\n")
            if result:
                jdump(art / "deslop-fix-result.json", fix_result(1))
        return build

    def test_mechanical_failure_after_a_fix_still_fails_immediately(self):
        for workflow, lane in LANES:
            with self.subTest(workflow=workflow):
                r = run_node(workflow, "deslop-recheck", self.pending(lane),
                             env={"SHIM_RC_BUN_TYPECHECK": "1"})
                self.assertEqual(r["rc"], 1, r["output"])
                self.assertIn("DESLOP_GATE=FAIL typecheck round=1", r["output"])

    def test_a_fixer_that_wrote_nothing_keeps_the_marker(self):
        for workflow, lane in LANES:
            with self.subTest(workflow=workflow):
                r = run_node(workflow, "deslop-recheck", self.pending(lane, result=False))
                self.assertEqual(r["rc"], 1, r["output"])
                self.assertIn("DESLOP_GATE=FAIL no deslop-fix-result.json round=1", r["output"])
                self.assertEqual(r["files"]["deslop-fix-pending.txt"].strip(), "1")

    def test_a_result_for_another_round_is_rejected(self):
        for workflow, lane in LANES:
            def build(tmp, lane=lane):
                self.pending(lane)(tmp)
                jdump(tmp / "artifacts" / "deslop-fix-result.json", fix_result(7))
            with self.subTest(workflow=workflow):
                r = run_node(workflow, "deslop-recheck", build)
                self.assertEqual(r["rc"], 1, r["output"])
                self.assertIn("DESLOP_GATE=FAIL deslop-fix-result.json unparseable round=1", r["output"])


class FixPre(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fixpre-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "artifacts").mkdir()

    def test_no_marker_skips_the_fixer(self):
        p = run_fix_pre(self.tmp)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(p.stdout.strip(), '{"fix":"none"}')

    def test_marker_runs_the_fixer_and_clears_a_stale_result(self):
        art = self.tmp / "artifacts"
        (art / "deslop-fix-pending.txt").write_text("3\n")
        (art / "deslop-round-3").mkdir()
        jdump(art / "deslop-round-3" / "blocking-findings.json", {"round": 3, "findings": []})
        jdump(art / "deslop-fix-result.json", fix_result(3))
        p = run_fix_pre(self.tmp)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(p.stdout.strip(), '{"fix":"pending","round":"3"}')
        self.assertFalse((art / "deslop-fix-result.json").exists())

    def test_junk_marker_fails_closed(self):
        (self.tmp / "artifacts" / "deslop-fix-pending.txt").write_text("1/../x")
        p = run_fix_pre(self.tmp)
        self.assertEqual(p.returncode, 1)
        self.assertIn("DESLOP_FIX=FAIL deslop-fix-pending.txt is not an integer: [1/../x]", p.stdout)

    def test_marker_without_findings_fails_closed(self):
        (self.tmp / "artifacts" / "deslop-fix-pending.txt").write_text("2")
        p = run_fix_pre(self.tmp)
        self.assertEqual(p.returncode, 1)
        self.assertIn("DESLOP_FIX=FAIL no blocking-findings.json for round=2", p.stdout)


class LoopShape(unittest.TestCase):
    """The engine half of the contract, pinned in every lane that carries the group."""

    def test_body_wiring(self):
        for workflow in ("full-sdlc-api", "full-sdlc-api-codex", "bugfix", "bugfix-codex"):
            doc = yaml.safe_load((WORKFLOWS / f"{workflow}.yaml").read_text(encoding="utf-8"))
            group = next(n for n in doc["nodes"] if n["id"] == "deslop-verify")["loop_group"]
            nodes = {n["id"]: n for n in group["nodes"]}
            with self.subTest(workflow=workflow):
                self.assertEqual([n["id"] for n in group["nodes"]], BODY)
                self.assertEqual(group["until"], "DESLOP_CLEAN")
                # Two iterations = one writer-fix retry, matching the DIRTY cap of 2.
                self.assertEqual(group["max_iterations"], 2)
                self.assertEqual(nodes["deslop-fix"]["depends_on"], ["deslop-fix-pre"])
                self.assertEqual(nodes["deslop-fix"]["when"], "$deslop-fix-pre.output.fix == 'pending'")
                self.assertEqual(nodes["deslop-recheck"]["depends_on"], ["deslop-fix-pre", "deslop-fix"])
                self.assertEqual(nodes["deslop-recheck"]["trigger_rule"], "none_failed_min_one_success")


if __name__ == "__main__":
    unittest.main()
