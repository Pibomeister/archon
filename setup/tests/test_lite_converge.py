#!/usr/bin/env python3
"""The lite convergence contract (full-sdlc-api-lite review-loop/converge overlay).

Runs the overlay as a bare script against a throwaway git worktree and faked
round artifacts, so each branch of the contract is exercised:

  Ready with fixes + HEAD moved  -> REVIEW_CONVERGED + lite-fixes-unreviewed.txt
  Ready to merge  + HEAD static  -> REVIEW_CONVERGED, no unreviewed file
  Not ready       + HEAD moved   -> ROUND_CAP_REACHED round=1, exit 1 (cap 1)
  Not ready + accept-residuals   -> REVIEW_CONVERGED (human act)
  fixer-result blocked           -> FIXER_BLOCKED, exit 1
  incomplete items, no accept    -> ROUND_CAP_REACHED, exit 1
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parent.parent
OVERLAY = SETUP / "lite" / "api" / "review-loop.converge.bash.sh"
ROOT_LITERAL = "/Users/eduardopicazo/Documents/Workspace/Goodword"


def sh(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, shell=True, check=True, capture_output=True)


class LiteConverge(unittest.TestCase):
    def setUp(self):
        if not OVERLAY.is_file():
            self.skipTest("overlay not built yet")
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.wt = self.tmp / "wt"
        self.wt.mkdir()
        sh("git init -q && git config user.email t@t && git config user.name t && echo a > a.ts && git add . && git commit -qm base", self.wt)
        self.base = subprocess.run("git rev-parse HEAD", cwd=self.wt, shell=True, capture_output=True, encoding="utf-8").stdout.strip()
        self.ad = self.tmp / "ad"
        (self.ad / "round-1").mkdir(parents=True)
        (self.ad / "round.txt").write_text("1\n")
        (self.ad / "round-cap.txt").write_text("1\n")
        (self.ad / "bootstrap-head.txt").write_text(self.base + "\n")
        (self.ad / "files-allowlist.json").write_text(json.dumps(["a.ts", "b.ts"]))
        (self.ad / "params.json").write_text(json.dumps({"spec": "/x.md", "slug": "x", "branch": "archon/x", "worktree": str(self.wt)}))
        (self.ad / "round-1" / "pre-head.txt").write_text(self.base + "\n")
        self.fixer({"applied": [{"finding": "f1"}], "failed": [], "advisory": [], "incomplete": []})
        # the overlay hardcodes the Goodword root for its helper scripts; point
        # those at the real setup dir by rewriting the literal to a temp mirror
        script = OVERLAY.read_text(encoding="utf-8").replace(ROOT_LITERAL + "/.archon/setup", str(SETUP))
        self.script = self.tmp / "converge.sh"
        self.script.write_text(script, encoding="utf-8")

    def fixer(self, obj):
        (self.ad / "round-1" / "fixer-result.json").write_text(json.dumps(obj))

    def verdict(self, v):
        (self.ad / "round-1" / "review-summary.json").write_text(json.dumps({"verdict": v}))

    def land_fix(self):
        sh("echo b > b.ts && git add . && git commit -qm fix", self.wt)

    def go(self):
        return subprocess.run(["bash", str(self.script)], capture_output=True, encoding="utf-8",
                              env={**os.environ, "ARTIFACTS_DIR": str(self.ad)})

    def test_ready_with_fixes_converges_and_records_unreviewed(self):
        self.verdict("Ready with fixes")
        self.land_fix()
        r = self.go()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("<promise>REVIEW_CONVERGED</promise>", r.stdout)
        self.assertIn("LITE_FIXES_UNREVIEWED round=1 1 file(s)", r.stdout)
        f = (self.ad / "lite-fixes-unreviewed.txt").read_text()
        self.assertIn("applied_findings=1", f)
        self.assertIn("  b.ts", f)

    def test_ready_static_head_converges_without_file(self):
        self.verdict("Ready to merge")
        r = self.go()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("<promise>REVIEW_CONVERGED</promise>", r.stdout)
        self.assertFalse((self.ad / "lite-fixes-unreviewed.txt").exists())

    def test_not_ready_with_fixes_hits_cap(self):
        self.verdict("Not ready")
        self.land_fix()
        r = self.go()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("ROUND_CAP_REACHED round=1", r.stdout)
        self.assertNotIn("REVIEW_CONVERGED", r.stdout)

    def test_not_ready_accept_residuals_is_human_override(self):
        self.verdict("Not ready")
        self.land_fix()
        (self.ad / "accept-residuals.txt").write_text("accepted by human\n")
        r = self.go()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("human accepted residuals", r.stdout)

    def test_not_ready_no_progress(self):
        self.verdict("Not ready")
        r = self.go()
        self.assertEqual(r.returncode, 1)
        self.assertIn("NO_PROGRESS round=1", r.stdout)

    def test_fixer_blocked(self):
        self.verdict("Ready with fixes")
        self.fixer({"applied": [], "failed": [{"finding": "x"}], "advisory": []})
        r = self.go()
        self.assertEqual(r.returncode, 1)
        self.assertIn("FIXER_BLOCKED round=1", r.stdout)

    def test_incomplete_items_hit_cap(self):
        self.verdict("Ready with fixes")
        self.fixer({"applied": [], "failed": [], "advisory": [], "incomplete": [{"finding": "x", "why": "y"}]})
        self.land_fix()
        r = self.go()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("ROUND_CAP_REACHED round=1 (fixer left 1 incomplete", r.stdout)

    def test_scope_breach_stops(self):
        self.verdict("Ready with fixes")
        sh("echo z > z.ts && git add . && git commit -qm oob", self.wt)
        r = self.go()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertNotIn("REVIEW_CONVERGED", r.stdout)


if __name__ == "__main__":
    unittest.main()
