#!/usr/bin/env python3
"""converge: what counts as head movement, and the re-raise qualifier.

Two changes share this fixture because both read the same round window.

1. A REVIEW session that edits the worktree gets its edits committed by
   `commit-fixes` as "fix(review): apply autofix feedback". Nobody reviewed
   those hunks, so counting them as head movement let an unreviewed edit buy the
   loop another round. `converge` now derives MOVED from the commit SUBJECTS in
   `<pre-head>..HEAD`: `apply fixer feedback` is progress, an autofix-only
   window is not, and any other subject is drift and a human stop.

2. `REVIEW_RERAISE` — two rounds that applied nothing while the reviewer
   re-raised what the ledger already waived — qualifies the SAME opt-in
   `yield-stop.txt` branch as `verdict=DIMINISHING`, never a branch of its own.

`review-yield.py` is stood in for (as in test_node_converge_yield.py) so the
yield line is fixture input: which severities map to DIMINISHING is that
script's own contract and is tested there.

ARCHON_FEATURE_SCOPE is process env the node harness does not pin, so every
case passes it explicitly.
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

YIELD_STUB = """#!/usr/bin/env python3
import sys
print(open(sys.argv[1] + "/yield-line.txt", encoding="utf-8").read().strip())
"""
CONTINUE = "REVIEW_YIELD=OK verdict=CONTINUE applied=0 maxsev=P2 reraised=0"
FIXER_SUBJECT = "fix(review): apply fixer feedback"
AUTOFIX_SUBJECT = "fix(review): apply autofix feedback"


def sh(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, shell=True, check=True, capture_output=True)


class ConvergeAutofix(unittest.TestCase):
    def mirror(self, tmp):
        """A setup/ the node can shell into: the real scripts, with
        review-yield.py replaced by a fixture-driven stand-in."""
        m = tmp / ".archon" / "setup"
        m.mkdir(parents=True)
        for p in SETUP.iterdir():
            if p.name != "review-yield.py":
                (m / p.name).symlink_to(p)
        (m / "review-yield.py").write_text(YIELD_STUB, encoding="utf-8")
        return m

    def build(self, *, n=2, cap=4, subjects=(), verdict="Ready with fixes",
              applied=None, advisory=None, incomplete=0, prev_applied=None,
              yield_stop=True, yield_line=CONTINUE):
        tmp = Path(tempfile.mkdtemp(prefix="ca-"))
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
        (ad / "files-allowlist.json").write_text(
            json.dumps(["a.ts"] + [f"b{i}.ts" for i in range(4)]))
        (ad / "params.json").write_text(json.dumps(
            {"spec": "/x.md", "slug": "x", "branch": "archon/x", "worktree": str(wt)}))
        (ad / "yield-line.txt").write_text(yield_line + "\n")
        if yield_stop:
            (ad / "yield-stop.txt").write_text("opt-in\n")
        (ad / f"round-{n}" / "pre-head.txt").write_text(base + "\n")
        (ad / f"round-{n}" / "review-summary.json").write_text(json.dumps({"verdict": verdict}))
        (ad / f"round-{n}" / "fixer-result.json").write_text(json.dumps({
            "applied": applied if applied is not None else [],
            "failed": [],
            "advisory": advisory if advisory is not None else [],
            "incomplete": [{"finding": "i", "action": "retry"} for _ in range(incomplete)],
        }))
        if n > 1:
            (ad / f"round-{n - 1}").mkdir()
            (ad / f"round-{n - 1}" / "fixer-result.json").write_text(json.dumps(
                {"applied": prev_applied if prev_applied is not None else [],
                 "failed": [], "advisory": [], "incomplete": []}))
        for i, subject in enumerate(subjects):
            sh(f"echo x{i} > b{i}.ts && git add . && git commit -qm {subject!r}", wt)
        body = runnable_body("full-sdlc-api", "converge", root=str(tmp))
        body = body.replace(str(tmp / ".archon" / "setup"), str(self.mirror(tmp)))
        return ad, body, tmp

    def run_converge(self, scope="repositories", **kw):
        ad, body, tmp = self.build(**kw)
        env = dict(os.environ, ARTIFACTS_DIR=str(ad), ARCHON_FEATURE_SCOPE=scope)
        p = subprocess.run(["bash", "-c", body], capture_output=True,
                           encoding="utf-8", env=env, cwd=str(tmp))
        return p, ad

    # ------------------------------------------------------- head movement
    def test_an_autofix_only_round_converges_on_a_ready_verdict(self):
        p, ad = self.run_converge(subjects=[AUTOFIX_SUBJECT])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("REVIEW_AUTOFIX_ONLY round=2 files=1", p.stdout)
        self.assertIn("head_moved=NO", p.stdout)
        self.assertIn("CONVERGED round=2", p.stdout)
        self.assertIn("<promise>REVIEW_CONVERGED</promise>", p.stdout)
        disclosure = (ad / "review-autofix-unreviewed.txt").read_text()
        self.assertIn("pre_round_head=", disclosure)
        self.assertIn("  b0.ts", disclosure)

    def test_a_fixer_commit_is_progress_and_costs_a_round(self):
        p, ad = self.run_converge(subjects=[FIXER_SUBJECT])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("head_moved=YES", p.stdout)
        self.assertIn("ROUND_PROGRESSED round=2", p.stdout)
        self.assertNotIn("REVIEW_AUTOFIX_ONLY", p.stdout)
        self.assertFalse((ad / "review-autofix-unreviewed.txt").exists())

    def test_a_fixer_commit_beside_an_autofix_commit_is_still_progress(self):
        p, _ = self.run_converge(subjects=[AUTOFIX_SUBJECT, FIXER_SUBJECT])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("ROUND_PROGRESSED round=2", p.stdout)
        self.assertNotIn("REVIEW_AUTOFIX_ONLY", p.stdout)

    def test_a_foreign_subject_stops_the_run(self):
        p, ad = self.run_converge(subjects=[AUTOFIX_SUBJECT, "chore: hand edit"])
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("REVIEW_TREE_DRIFT round=2", p.stdout)
        self.assertIn("unexpected subject: chore: hand edit", p.stdout)
        self.assertNotIn("REVIEW_CONVERGED", p.stdout)
        self.assertFalse((ad / "review-autofix-unreviewed.txt").exists())

    def test_an_unmoved_head_never_reaches_the_subject_filter(self):
        p, _ = self.run_converge(subjects=[])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("head_moved=NO", p.stdout)
        self.assertNotIn("REVIEW_AUTOFIX_ONLY", p.stdout)
        self.assertIn("CONVERGED round=2", p.stdout)

    # ----------------------------------------------------------- re-raise
    def _reraise_kwargs(self, **over):
        """Two rounds that applied nothing; this round handled 2 advisory
        findings and review-yield says both were already in the ledger."""
        kw = dict(
            subjects=[FIXER_SUBJECT],
            applied=[],
            prev_applied=[],
            advisory=[{"finding": "f1", "action": "Waived: pre-existing", "severity": "P2"},
                      {"finding": "f2", "action": "Waived: out of scope", "severity": "P3"}],
            yield_line="REVIEW_YIELD=OK verdict=CONTINUE applied=0 maxsev=P2 "
                       "reraised=1 reraised_advisory=1",
        )
        kw.update(over)
        return kw

    def test_a_re_raising_pair_of_rounds_qualifies_the_yield_branch(self):
        p, ad = self.run_converge(**self._reraise_kwargs())
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("REVIEW_RERAISE round=2 reraised=2/2", p.stdout)
        self.assertIn("CONVERGED round=2 (REVIEW_DIMINISHING", p.stdout)
        self.assertIn("<promise>REVIEW_CONVERGED</promise>", p.stdout)
        self.assertTrue((ad / "lite-fixes-unreviewed.txt").exists())

    def test_below_half_re_raised_does_not_qualify(self):
        p, _ = self.run_converge(**self._reraise_kwargs(
            advisory=[{"finding": f"f{i}", "action": "Waived: x", "severity": "P3"}
                      for i in range(5)]))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("REVIEW_RERAISE", p.stdout)
        self.assertIn("ROUND_PROGRESSED round=2", p.stdout)

    def test_a_previous_round_that_applied_something_does_not_qualify(self):
        p, _ = self.run_converge(**self._reraise_kwargs(
            prev_applied=[{"finding": "p1", "action": "fixed", "severity": "P2"}]))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("REVIEW_RERAISE", p.stdout)
        self.assertIn("ROUND_PROGRESSED round=2", p.stdout)

    def test_a_missing_reraised_advisory_field_reads_as_zero(self):
        # An older review-yield.py prints no reraised_advisory. Over three
        # handled findings, reraised=1 alone is under half and the qualifier
        # stays off; the field's absence is a 0, never a parse error.
        three = [{"finding": f"f{i}", "action": "Waived: x", "severity": "P3"}
                 for i in range(3)]
        p, _ = self.run_converge(**self._reraise_kwargs(
            advisory=three,
            yield_line="REVIEW_YIELD=OK verdict=CONTINUE applied=0 maxsev=P2 reraised=1"))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("REVIEW_RERAISE", p.stdout)
        self.assertIn("ROUND_PROGRESSED round=2", p.stdout)

    def test_the_two_reraise_counts_are_summed(self):
        # Same three findings: the advisory half of the count is what carries
        # the pair over the threshold, so it is read, not ignored.
        three = [{"finding": f"f{i}", "action": "Waived: x", "severity": "P3"}
                 for i in range(3)]
        p, _ = self.run_converge(**self._reraise_kwargs(
            advisory=three,
            yield_line="REVIEW_YIELD=OK verdict=CONTINUE applied=0 maxsev=P2 "
                       "reraised=1 reraised_advisory=1"))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("REVIEW_RERAISE round=2 reraised=2/3", p.stdout)
        self.assertIn("<promise>REVIEW_CONVERGED</promise>", p.stdout)

    def test_a_round_that_handled_nothing_never_qualifies(self):
        # 0 of 0 re-raised is a quiet round, not re-litigation. Without the
        # HANDLED > 0 guard this would qualify on arithmetic alone.
        p, _ = self.run_converge(**self._reraise_kwargs(advisory=[]))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("REVIEW_RERAISE", p.stdout)
        self.assertIn("ROUND_PROGRESSED round=2", p.stdout)

    def test_the_legacy_scope_gets_no_re_raise_convergence(self):
        p, _ = self.run_converge(scope="legacy", **self._reraise_kwargs())
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("REVIEW_RERAISE round=2", p.stdout)
        self.assertIn("ROUND_PROGRESSED round=2", p.stdout)
        self.assertNotIn("REVIEW_CONVERGED", p.stdout)

    def test_without_the_opt_in_file_the_re_raise_line_is_only_a_report(self):
        p, _ = self.run_converge(**self._reraise_kwargs(yield_stop=False))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("REVIEW_RERAISE round=2", p.stdout)
        self.assertIn("ROUND_PROGRESSED round=2", p.stdout)
        self.assertNotIn("REVIEW_CONVERGED", p.stdout)


if __name__ == "__main__":
    unittest.main()
