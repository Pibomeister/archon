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
from nodes.gitutil import git
from nodes.runner import _isolation_env
from test_node_stress import (deslop_common, deslop_review, deslop_review_gate_fixture,
                              jdump, prerun, run_node)

LANES = (("full-sdlc-api", "api"), ("bugfix", "bugfix"))
BODY = ["deslop-fix-pre", "deslop-fix", "deslop-recheck", "deslop-review", "deslop-review-gate"]


def fix_result(round_no=1):
    return {"round": round_no,
            "fixed": [{"guard": "comments", "file": "src/foo.ts", "line": 2, "action": "deleted"}],
            "not_fixed": []}


def run_fix_pre(tmp, workflow="full-sdlc-api"):
    body = runnable_body(workflow, "deslop-fix-pre")
    env = dict(os.environ, **_isolation_env(tmp), ARTIFACTS_DIR=str(tmp / "artifacts"))
    return subprocess.run(["bash", "-c", body], capture_output=True, encoding="utf-8",
                          env=env, cwd=str(tmp))


def round_one_dirty(workflow, lane, tmp):
    """Round 1 recheck -> DIRTY verdict -> the fixer is owed round 1."""
    deslop_review_gate_fixture(lane, deslop_review("DIRTY", blocking=1))(tmp)
    prerun(workflow, "deslop-review-gate", tmp)
    pre = prerun(workflow, "deslop-fix-pre", tmp)
    assert json.loads(pre.stdout.strip().splitlines()[-1]) == {"fix": "pending", "round": "1"}, pre.stdout


def dirty_then_fixed(workflow, lane, second_review, fixer=None):
    """Round 1 DIRTY -> fixer result -> round 2 recheck, then `second_review`.
    `fixer(tmp)` stands in for the fixer session's side effects."""
    def build(tmp):
        round_one_dirty(workflow, lane, tmp)
        art = tmp / "artifacts"
        jdump(art / "deslop-fix-result.json", fix_result(1))
        if fixer:
            fixer(tmp)
        prerun(workflow, "deslop-recheck", tmp)
        jdump(art / "deslop-review.json", second_review)
    return build


def fixer_then_recheck(workflow, lane, fixer):
    """Round 1 DIRTY, then `fixer(tmp)`; the node under test is the round 2 recheck."""
    def build(tmp):
        round_one_dirty(workflow, lane, tmp)
        jdump(tmp / "artifacts" / "deslop-fix-result.json", fix_result(1))
        fixer(tmp)
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
            base = deslop_common(tmp, lane)
            art = tmp / "artifacts"
            (art / "deslop-fix-pending.txt").write_text("1\n")
            (art / "deslop-round-1").mkdir()
            jdump(art / "deslop-round-1" / "blocking-findings.json", {"round": 1, "findings": [
                {"guard": "comments", "file": "src/foo.ts", "line": 2, "confidence": 75, "evidence": "x"}]})
            (art / "deslop-tree.txt").write_text(f"head={base}\n")
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


class FixerCannotRewriteTheRecord(unittest.TestCase):
    """The fixer runs between two gates and can write any artifact; the gates check."""

    def recheck_after(self, fixer):
        out = {}
        for workflow, lane in LANES:
            out[workflow] = run_node(workflow, "deslop-recheck", fixer_then_recheck(workflow, lane, fixer))
        return out

    def test_writer_declaration_is_frozen(self):
        def declares(tmp):
            path = tmp / "artifacts" / "deslop-result.json"
            doc = json.loads(path.read_text())
            doc["passes"].append({"guard": "comments", "file": "src/foo.ts", "line": 9, "action": "deleted"})
            jdump(path, doc)
        for workflow, r in self.recheck_after(declares).items():
            with self.subTest(workflow=workflow):
                self.assertEqual(r["rc"], 1, r["output"])
                self.assertIn("DESLOP_GATE=FAIL deslop-result.json changed after the writer round=2", r["output"])

    def test_negative_control_an_untouched_declaration_passes(self):
        for workflow, r in self.recheck_after(lambda tmp: None).items():
            with self.subTest(workflow=workflow):
                self.assertEqual(r["rc"], 0, r["output"])
                self.assertIn("DESLOP_FIX_CONSUMED round=2 fixed_round=1", r["output"])
                self.assertIn("DESLOP_GATE=PASS", r["output"])

    def test_fixer_commit_is_caught(self):
        def commits(tmp):
            commit(tmp / "wt", "fixer")
        for workflow, r in self.recheck_after(commits).items():
            with self.subTest(workflow=workflow):
                self.assertEqual(r["rc"], 1, r["output"])
                self.assertIn("DESLOP_GATE=FAIL fixer moved HEAD round=2", r["output"])


FIXED_FOO = ("export function foo(x: number): number {\n  return x + 1 + k4 + k5 + k6 + k7 + k8 + k9 + k10;\n}\n"
             + "".join(f"const k{i} = 0;\n" for i in range(4, 11)))


def commit(wt, message):
    # Pinned dates: the node output is compared across stress runs.
    env = dict(os.environ, GIT_AUTHOR_DATE="2026-01-01T00:00:00Z", GIT_COMMITTER_DATE="2026-01-01T00:00:00Z")
    subprocess.run(["git", "-C", str(wt), "-c", "user.name=f", "-c", "user.email=f@x",
                    "commit", "-qam", message], check=True, env=env, capture_output=True)


def beyond_guards(fixer):
    """bugfix: red-sha holds the repro, HEAD is the fix commit on top of it, and the
    deslop pass edited line 2 (a guard) and line 10 (beyond the five guards)."""
    def build(tmp):
        deslop_common(tmp, "bugfix")
        wt, art = tmp / "wt", tmp / "artifacts"
        (wt / "src" / "foo.ts").write_text(FIXED_FOO)
        commit(wt, "fix")
        desloped = FIXED_FOO.replace("x + 1", "x + 2").replace("k10 = 0", "k10 = 1")
        (wt / "src" / "foo.ts").write_text(desloped)
        prerun("bugfix", "deslop-recheck", tmp)
        review = deslop_review("DIRTY", blocking=1)
        review["findings"][0].update(guard="beyond_five_guards", line=10, confidence=100)
        jdump(art / "deslop-review.json", review)
        prerun("bugfix", "deslop-review-gate", tmp)
        prerun("bugfix", "deslop-fix-pre", tmp)
        jdump(art / "deslop-fix-result.json", {"round": 1, "fixed": [
            {"guard": "beyond_five_guards", "file": "src/foo.ts", "line": 10, "action": "reverted"}],
            "not_fixed": []})
        fixer(wt, desloped)
    return build


class BeyondFiveGuardsRevertsToHead(unittest.TestCase):
    def recheck(self, fixer):
        return run_node("bugfix", "deslop-recheck", beyond_guards(fixer))

    def test_region_restored_to_head_passes(self):
        r = self.recheck(lambda wt, d: (wt / "src" / "foo.ts").write_text(d.replace("k10 = 1", "k10 = 0")))
        self.assertEqual(r["rc"], 0, r["output"])
        self.assertIn("DESLOP_GATE=PASS", r["output"])

    def test_restoring_red_sha_content_is_caught(self):
        # The reviewer's P2b case: red-sha predates the fix, so this drops lines 4-10 of it.
        r = self.recheck(lambda wt, d: (wt / "src" / "foo.ts").write_text(
            git(wt, "show", "HEAD~1:src/foo.ts").stdout.replace("x + 1", "x + 2")))
        self.assertEqual(r["rc"], 1, r["output"])
        self.assertIn("DESLOP_GATE=FAIL beyond_five_guards edit not reverted to HEAD file=src/foo.ts:10 round=2",
                      r["output"])

    def test_a_fixer_that_only_claimed_the_revert_is_caught(self):
        r = self.recheck(lambda wt, d: None)
        self.assertEqual(r["rc"], 1, r["output"])
        self.assertIn("DESLOP_GATE=FAIL beyond_five_guards edit not reverted to HEAD file=src/foo.ts:10 round=2",
                      r["output"])


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
