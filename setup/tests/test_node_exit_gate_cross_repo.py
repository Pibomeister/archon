#!/usr/bin/env python3
"""exit-gate: the cross-repo belt runs unconditionally, ahead of every bypass.

converge's stop can be skipped by a resume that re-enters after it, and the
accept-residuals branch skips the fixer re-check outright — so a human accepting
residuals would otherwise accept a cross-repo finding, which is the one outcome
the partition exists to prevent. Both lanes that declare exit-gate are covered;
full-sdlc-web declares none, so its cross-repo stop lives only at converge.
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
LANES = ("full-sdlc-api", "bugfix")


def sh(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, shell=True, check=True, capture_output=True)


class ExitGateCrossRepo(unittest.TestCase):
    def run_gate(self, lane, cross_repo, ack=None):
        tmp = Path(tempfile.mkdtemp(prefix="eg-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        mirror = tmp / ".archon" / "setup"
        mirror.mkdir(parents=True)
        for p in SETUP.iterdir():
            (mirror / p.name).symlink_to(p)
        wt = tmp / "wt"
        wt.mkdir()
        sh("git init -q && git config user.email t@t && git config user.name t "
           "&& echo a > a.ts && git add . && git commit -qm base", wt)
        base = subprocess.run("git rev-parse HEAD", cwd=wt, shell=True,
                              capture_output=True, encoding="utf-8").stdout.strip()
        ad = tmp / "ad"
        (ad / "round-1").mkdir(parents=True)
        (ad / "round.txt").write_text("1\n")
        (ad / "repo.txt").write_text("api\n")
        (ad / "red-sha.txt").write_text(base + "\n")
        (ad / "bootstrap-head.txt").write_text(base + "\n")
        (ad / "failing-test.json").write_text(json.dumps({"test_file": "a.ts"}))
        (ad / "files-allowlist.json").write_text(json.dumps(["a.ts"]))
        (ad / "params.json").write_text(json.dumps(
            {"spec": "/x.md", "slug": "x", "branch": "archon/x", "worktree": str(wt)}))
        # The human bypass: it skips the verdict and the fixer re-check, so the
        # belt has to sit outside it.
        (ad / "accept-residuals.txt").write_text("accepted by test\n")
        (ad / "round-1" / "fixer-result.json").write_text(json.dumps(
            {"applied": [], "failed": [], "advisory": [], "incomplete": [],
             "cross_repo": cross_repo}))
        if ack is not None:
            (ad / "cross-repo-filed.json").write_text(json.dumps(ack))
        # full-sdlc-api's exit-gate authorizes through round-state.py before it
        # reaches anything an operator can waive, so the round has to look gated
        # for the cross-repo belt to be what these cases are measuring. Seeded
        # from review-input.json's own id rather than a literal, so the identity
        # formula stays in one place.
        if lane == "full-sdlc-api":
            (ad / "plan.md").write_text("# plan\n")
            env0 = {**os.environ, "ARTIFACTS_DIR": str(ad)}
            subprocess.run(["python3", str(SETUP / "round-state.py"), "pre", str(ad)],
                           capture_output=True, env=env0)
            rd = ad / f"round-{(ad / 'round.txt').read_text().strip()}"
            rec = json.loads((rd / "review-input.json").read_text())
            (rd / "gate.txt").write_text("PASS\n")
            (rd / "review.ok").write_text(json.dumps(
                {"gen": rec["attempt"], "id": rec["id"], "guard": "PASS"}))
            (rd / "fixer.ok").write_text(json.dumps(
                {"attempt": rec["attempt"], "review_id": rec["id"],
                 "review_gen": rec["attempt"],
                 "head": subprocess.run("git rev-parse HEAD", cwd=wt, shell=True,
                                        capture_output=True, encoding="utf-8").stdout.strip(),
                 "tree": "", "committed": False}))
            (rd / "fixer-result.json").write_text(json.dumps(
                {"applied": [], "failed": [], "advisory": [], "incomplete": [],
                 "cross_repo": cross_repo}))
        body = runnable_body(lane, "exit-gate", root=str(tmp))
        return subprocess.run(["bash", "-c", body], capture_output=True, encoding="utf-8",
                              env=dict(os.environ, ARTIFACTS_DIR=str(ad)), cwd=str(tmp))

    def test_accepted_residuals_do_not_accept_a_cross_repo_finding(self):
        for lane in LANES:
            with self.subTest(lane=lane):
                p = self.run_gate(lane, [{"finding": "mcp 500", "action": "route",
                                          "producer_repo": "goodword-mcp", "severity": "P1"}])
                self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                self.assertIn("EXIT_GATE=FAIL CROSS_REPO_FINDING round=1 count=1", p.stdout)
                self.assertNotIn("EXIT_GATE=PASS", p.stdout)

    # Digest of "goodword-mcp\nmcp 500", computed independently of the helper.
    FINDING = {"finding": "mcp 500", "action": "route", "producer_repo": "goodword-mcp", "severity": "P1"}
    KEY = "8d56a400c5576e44"
    NEXTCHECK = {"full-sdlc-api": "accept-residuals present", "bugfix": "REPO_POLICY=SNAPSHOT"}

    def test_an_acknowledged_finding_reaches_the_rest_of_the_gate(self):
        for lane in LANES:
            with self.subTest(lane=lane):
                p = self.run_gate(lane, [self.FINDING], ack=[
                    {"key": self.KEY, "filed": "https://github.com/o/goodword-mcp/pull/9", "by": "operator"}])
                self.assertIn(f"CROSS_REPO_ACKED key={self.KEY}", p.stdout)
                self.assertNotIn("CROSS_REPO_FINDING", p.stdout)
                self.assertIn(self.NEXTCHECK[lane], p.stdout)

    def test_a_wrong_key_or_missing_url_still_fails(self):
        for lane in LANES:
            for ack in ([{"key": "ffffffffffffffff", "filed": "https://x.test/1", "by": "op"}],
                        [{"key": self.KEY, "by": "op"}]):
                with self.subTest(lane=lane, ack=ack):
                    p = self.run_gate(lane, [self.FINDING], ack=ack)
                    self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                    self.assertIn("EXIT_GATE=FAIL CROSS_REPO_FINDING round=1 count=1 repos=goodword-mcp", p.stdout)

    def test_an_empty_partition_reaches_the_rest_of_the_gate(self):
        # The check the belt hands off to differs per lane: bugfix runs the
        # repository policy first, full-sdlc-api goes straight to the bypass.
        nextcheck = {"full-sdlc-api": "accept-residuals present",
                     "bugfix": "REPO_POLICY=SNAPSHOT"}
        for lane in LANES:
            with self.subTest(lane=lane):
                p = self.run_gate(lane, [])
                self.assertNotIn("CROSS_REPO_FINDING", p.stdout)
                self.assertIn(nextcheck[lane], p.stdout)


if __name__ == "__main__":
    unittest.main()
