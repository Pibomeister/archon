#!/usr/bin/env python3
"""converge: the cross-repo stop (all lanes) and the gated yield branch (full-sdlc-api only).

The three parent bodies come from the YAML via nodes.extract; the lite overlay
runs as a bare script, in the style of test_lite_converge.py. Both are pointed at
a temp mirror of setup/ so `review-yield.py` (B2) can be stood in for without
writing a stand-in into the repository.

The stand-in prints whatever the fixture put in yield-line.txt: which severities
map to `verdict=DIMINISHING` is review-yield.py's contract and is tested there.
What is tested HERE is the converge branch's own conditions — scope, the opt-in
file, clean tree, no incompletes, 2 <= N < CAP, and the reported verdict.

ARCHON_FEATURE_SCOPE is a process-env variable that the node harness does not
pin, so every case passes it explicitly (D1).
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from nodes.extract import runnable_body

SETUP = Path(__file__).resolve().parent.parent
OVERLAY = SETUP / "lite" / "api" / "review-loop.converge.bash.sh"
ROOT_LITERAL = "/Users/eduardopicazo/Documents/Workspace/Goodword"
LANES = ("full-sdlc-api", "bugfix", "full-sdlc-web")

YIELD_STUB = """#!/usr/bin/env python3
import sys
print(open(sys.argv[1] + "/yield-line.txt", encoding="utf-8").read().strip())
"""
# Stands in for B3's validation so the capture of its message can be asserted.
FIXER_STUB = """#!/usr/bin/env python3
import sys
print("FIXER_BLOCKED: cross_repo entry missing producer_repo", file=sys.stderr)
sys.exit(1)
"""
DIMINISHING = "REVIEW_YIELD=OK verdict=DIMINISHING applied=1 maxsev=P2 reraised=0"
CONTINUE = "REVIEW_YIELD=OK verdict=CONTINUE applied=1 maxsev=P1 reraised=0"


def sh(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, shell=True, check=True, capture_output=True)


class ConvergeYield(unittest.TestCase):
    def mirror(self, tmp, broken_fixer_check=False):
        """A setup/ the node can shell into: symlinks to the real scripts, plus
        stand-ins for the ones this branch's siblings own."""
        m = tmp / ".archon" / "setup"
        m.mkdir(parents=True)
        for p in SETUP.iterdir():
            if p.name not in ("review-yield.py", "check-fixer-result.py"):
                (m / p.name).symlink_to(p)
        (m / "review-yield.py").write_text(YIELD_STUB, encoding="utf-8")
        if broken_fixer_check:
            (m / "check-fixer-result.py").write_text(FIXER_STUB, encoding="utf-8")
        else:
            (m / "check-fixer-result.py").symlink_to(SETUP / "check-fixer-result.py")
        return m

    def build(self, lane, *, n=2, cap=4, applied=None, incomplete=0, cross_repo=None,
              yield_stop=True, yield_line=DIMINISHING, verdict="Ready with fixes",
              moved=True, broken_fixer_check=False):
        tmp = Path(tempfile.mkdtemp(prefix="cy-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        wt = tmp / "wt"
        wt.mkdir()
        sh("git init -q && git config user.email t@t && git config user.name t "
           "&& echo a > a.ts && git add . && git commit -qm base", wt)
        base = subprocess.run("git rev-parse HEAD", cwd=wt, shell=True,
                              capture_output=True, encoding="utf-8").stdout.strip()
        ad = tmp / "ad"
        (ad / f"round-{n}").mkdir(parents=True)
        (ad / "round.txt").write_text(f"{n}\n")
        (ad / "round-cap.txt").write_text(f"{cap}\n")
        (ad / "bootstrap-head.txt").write_text(base + "\n")
        (ad / "red-sha.txt").write_text(base + "\n")
        (ad / "failing-test.json").write_text(json.dumps({"test_file": "a.ts"}))
        (ad / "files-allowlist.json").write_text(json.dumps(["a.ts", "b.ts"]))
        (ad / "params.json").write_text(json.dumps(
            {"spec": "/x.md", "slug": "x", "branch": "archon/x", "worktree": str(wt)}))
        (ad / "yield-line.txt").write_text(yield_line + "\n")
        if yield_stop:
            (ad / "yield-stop.txt").write_text("opt-in\n")
        (ad / f"round-{n}" / "pre-head.txt").write_text(base + "\n")
        (ad / f"round-{n}" / "review-summary.json").write_text(json.dumps({"verdict": verdict}))
        result = {
            "applied": applied if applied is not None else [{"finding": "f1", "action": "a", "severity": "P2"}],
            "failed": [], "advisory": [],
            "incomplete": [{"finding": "i", "action": "retry"} for _ in range(incomplete)],
        }
        if cross_repo is not None:
            result["cross_repo"] = cross_repo
        (ad / f"round-{n}" / "fixer-result.json").write_text(json.dumps(result))
        if n > 1:
            (ad / f"round-{n - 1}").mkdir()
            (ad / f"round-{n - 1}" / "fixer-result.json").write_text(json.dumps(
                {"applied": [{"finding": "f0", "action": "a", "severity": "P2"}],
                 "failed": [], "advisory": [], "incomplete": []}))
        if moved:
            # The subject is load-bearing since the autofix guard landed:
            # converge derives head movement from the commit SUBJECTS in the
            # round window, and only `apply fixer feedback` counts as progress.
            # A placeholder subject now reads as tree drift, which is the point.
            sh("echo b > b.ts && git add . && git commit -qm 'fix(review): apply fixer feedback'", wt)
        m = self.mirror(tmp, broken_fixer_check)
        if lane == "lite":
            body = OVERLAY.read_text(encoding="utf-8").replace(
                ROOT_LITERAL + "/.archon/setup", str(m))
        else:
            body = runnable_body(lane, "converge", root=str(tmp))
        return tmp, ad, body

    def run_converge(self, lane, scope="repositories", ack=None, **kw):
        tmp, ad, body = self.build(lane, **kw)
        if ack is not None:
            (ad / "cross-repo-filed.json").write_text(json.dumps(ack))
        env = dict(os.environ, ARTIFACTS_DIR=str(ad), ARCHON_FEATURE_SCOPE=scope)
        p = subprocess.run(["bash", "-c", body], capture_output=True,
                           encoding="utf-8", env=env, cwd=str(tmp))
        return p, ad

    # ---------------------------------------------------------- yield branch
    def test_yield_converges_and_discloses_the_unreviewed_fix(self):
        p, ad = self.run_converge("full-sdlc-api")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("CONVERGED round=2 (REVIEW_DIMINISHING", p.stdout)
        self.assertIn("<promise>REVIEW_CONVERGED</promise>", p.stdout)
        self.assertIn("LITE_FIXES_UNREVIEWED round=2 1 file(s)", p.stdout)
        disclosure = (ad / "lite-fixes-unreviewed.txt").read_text()
        self.assertIn("applied_findings=1", disclosure)
        self.assertIn("  b.ts", disclosure)

    def test_without_the_opt_in_file_the_round_only_progresses(self):
        p, ad = self.run_converge("full-sdlc-api", yield_stop=False)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("ROUND_PROGRESSED round=2", p.stdout)
        self.assertNotIn("REVIEW_CONVERGED", p.stdout)
        self.assertFalse((ad / "lite-fixes-unreviewed.txt").exists())

    def test_the_cap_keeps_the_last_word(self):
        p, _ = self.run_converge("full-sdlc-api", n=2, cap=2)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("ROUND_CAP_REACHED round=2", p.stdout)
        self.assertNotIn("REVIEW_CONVERGED", p.stdout)

    def test_a_continuing_yield_does_not_converge(self):
        p, _ = self.run_converge(
            "full-sdlc-api", yield_line=CONTINUE,
            applied=[{"finding": "f1", "action": "a", "severity": "P1"},
                     {"finding": "f2", "action": "a"}])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("ROUND_PROGRESSED round=2", p.stdout)
        self.assertNotIn("REVIEW_CONVERGED", p.stdout)

    def test_round_one_is_too_early(self):
        p, _ = self.run_converge("full-sdlc-api", n=1)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("ROUND_PROGRESSED round=1", p.stdout)

    def test_legacy_scope_does_not_converge(self):
        p, _ = self.run_converge("full-sdlc-api", scope="legacy")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("ROUND_PROGRESSED round=2", p.stdout)
        self.assertNotIn("REVIEW_CONVERGED", p.stdout)

    def test_bugfix_lane_has_no_yield_branch(self):
        p, _ = self.run_converge("bugfix", n=2, cap=2)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("ROUND_CAP_REACHED round=2", p.stdout)
        self.assertNotIn("REVIEW_CONVERGED", p.stdout)

    def test_bugfix_lane_has_no_yield_branch_under_a_raised_cap(self):
        # round-cap.txt is operator-editable, so the lane exclusion — not the
        # N < CAP guard — is what keeps the human gate at bugfix's own cap.
        p, _ = self.run_converge("bugfix", n=2, cap=3)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("ROUND_PROGRESSED round=2", p.stdout)
        self.assertNotIn("REVIEW_CONVERGED", p.stdout)

    def test_web_lane_has_no_yield_branch(self):
        p, _ = self.run_converge("full-sdlc-web")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("ROUND_PROGRESSED round=2", p.stdout)
        self.assertNotIn("REVIEW_CONVERGED", p.stdout)

    # ------------------------------------------------------- cross-repo stop
    def test_cross_repo_stops_every_lane_before_the_waiver_ledger(self):
        for lane in LANES + ("lite",):
            with self.subTest(lane=lane):
                p, ad = self.run_converge(
                    lane, n=1, cross_repo=[{"finding": "mcp returns 500", "action": "route",
                                            "producer_repo": "goodword-mcp", "severity": "P1"}])
                self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                self.assertIn("CROSS_REPO_FINDING round=1 count=1 repos=goodword-mcp", p.stdout)
                self.assertNotIn("REVIEW_CONVERGED", p.stdout)
                self.assertEqual(
                    json.loads((ad / "cross-repo-findings.json").read_text())[0]["producer_repo"],
                    "goodword-mcp")
                self.assertFalse((ad / "waivers.md").exists(),
                                 "a cross-repo finding must never reach update-waivers.py")

    # ------------------------------------------- operator acknowledgement
    # A cross_repo entry resolves only by a human recording where it was filed,
    # keyed to that exact finding. The key comes from the operator helper, the
    # same way the operator gets it; the literal pins the digest recipe.
    FINDING = {"finding": "mcp returns 500", "action": "route",
               "producer_repo": "goodword-mcp", "severity": "P1"}
    KEY = "583b294310459c02"

    def converge_with_ack(self, lane, ack):
        # moved=False + Ready to merge: the only possible stop left is cross-repo.
        return self.run_converge(lane, n=1, cross_repo=[self.FINDING], ack=ack,
                                 verdict="Ready to merge", moved=False, yield_stop=False)

    def test_an_acknowledged_cross_repo_finding_converges_every_lane(self):
        for lane in LANES + ("lite",):
            with self.subTest(lane=lane):
                p, ad = self.converge_with_ack(lane, [
                    {"key": self.KEY, "filed": "https://github.com/o/goodword-mcp/issues/7", "by": "operator"}])
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertIn(f"CROSS_REPO_ACKED key={self.KEY} repo=goodword-mcp "
                              "filed=https://github.com/o/goodword-mcp/issues/7", p.stdout)
                self.assertNotIn("CROSS_REPO_FINDING", p.stdout)
                self.assertIn("CONVERGED round=1", p.stdout)

    def test_an_invalid_acknowledgement_still_stops_every_lane(self):
        cases = {
            "wrong key": [{"key": "0000000000000000", "filed": "https://x.test/1", "by": "op"}],
            "missing url": [{"key": self.KEY, "filed": "", "by": "op"}],
            "not a url": [{"key": self.KEY, "filed": "filed it in slack", "by": "op"}],
            "missing by": [{"key": self.KEY, "filed": "https://x.test/1", "by": " "}],
            "not a list": {"key": self.KEY, "filed": "https://x.test/1", "by": "op"},
        }
        for lane in LANES + ("lite",):
            for name, ack in cases.items():
                with self.subTest(lane=lane, case=name):
                    p, ad = self.converge_with_ack(lane, ack)
                    self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                    self.assertIn("CROSS_REPO_FINDING round=1 count=1 repos=goodword-mcp", p.stdout)
                    self.assertNotIn("REVIEW_CONVERGED", p.stdout)
                    self.assertEqual(json.loads((ad / "cross-repo-findings.json").read_text())[0]["key"],
                                     self.KEY)

    def test_the_operator_helper_prints_the_key_the_gate_matches(self):
        p, ad = self.converge_with_ack("full-sdlc-api", None)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        out = subprocess.run(["python3", str(SETUP / "cross-repo-keys.py"), str(ad)],
                             capture_output=True, encoding="utf-8")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn(f"key={self.KEY} severity=P1 repo=goodword-mcp OPEN: mcp returns 500", out.stdout)
        self.assertIn(f'"key": "{self.KEY}"', out.stdout)

    def test_an_empty_cross_repo_partition_is_not_a_stop(self):
        p, ad = self.run_converge("full-sdlc-api", n=1, cross_repo=[])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("CROSS_REPO_FINDING", p.stdout)
        self.assertFalse((ad / "cross-repo-findings.json").exists())

    def test_the_validators_message_reaches_converge_txt(self):
        p, ad = self.run_converge("full-sdlc-api", n=1, broken_fixer_check=True)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("producer_repo", (ad / "round-1" / "converge.txt").read_text())


if __name__ == "__main__":
    unittest.main()
