#!/usr/bin/env python3
"""What the full/delta decision must not get wrong.

Every failure here is a reviewer reading the wrong thing: too little (a delta
round that should have been full, so a behaviour change goes unreviewed) or the
wrong base (a sha the reviewer's `git diff` cannot resolve). The expensive
direction is safe and the cheap direction is not, so every case that is not
provably cosmetic has to land on `full`.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ARCHON = Path(__file__).resolve().parents[2]
SCRIPT = ARCHON / "setup" / "review-mode.py"
PREV_HEAD = "6f56dbce0152ccaa97a958d08f4e61b6e9872985"


def f(finding, severity="P2"):
    e = {"finding": finding, "action": "fixed it"}
    if severity is not None:
        e["severity"] = severity
    return e


class ReviewMode(unittest.TestCase):
    def setUp(self):
        self.ad = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.ad, ignore_errors=True)

    def write_round(self, n, applied, head=PREV_HEAD, result=None):
        d = self.ad / f"round-{n}"
        d.mkdir(parents=True, exist_ok=True)
        body = result if result is not None else json.dumps(
            {"applied": applied, "failed": [], "advisory": [], "incomplete": []})
        (d / "fixer-result.json").write_text(body, encoding="utf-8")
        if head is not None:
            (d / "pre-head.txt").write_text(head + "\n", encoding="utf-8")

    def ask(self, n):
        r = subprocess.run([sys.executable, str(SCRIPT), str(self.ad), str(n)],
                           capture_output=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        out = r.stdout.splitlines()
        self.assertEqual(len(out), 1, r.stdout)
        return out[0]

    def assertDecision(self, n, mode, base):
        self.assertEqual(self.ask(n), f"REVIEW_MODE={mode} base={base}")
        rd = self.ad / f"round-{n}"
        self.assertEqual((rd / "review-mode.txt").read_text(encoding="utf-8"), mode + "\n")
        self.assertEqual((rd / "review-base.txt").read_text(encoding="utf-8"), base + "\n")

    def test_round_1_has_no_predecessor_to_diff_against(self):
        self.assertDecision(1, "full", "origin/main")

    def test_a_p1_fix_last_round_forces_a_full_re_read(self):
        self.write_round(1, [f("a"), f("b", "P1")])
        self.assertDecision(2, "full", "origin/main")

    def test_a_p0_fix_last_round_forces_a_full_re_read(self):
        self.write_round(1, [f("a", "P0")])
        self.assertDecision(2, "full", "origin/main")

    def test_an_unreadable_previous_round_is_not_evidence_of_a_cosmetic_one(self):
        for name, setup in (
            ("no round directory", lambda: None),
            ("no fixer result", lambda: (self.ad / "round-1").mkdir(parents=True)),
            ("invalid json", lambda: self.write_round(1, [], result="{not json")),
            ("no applied key", lambda: self.write_round(
                1, [], result=json.dumps({"fixes": []}))),
            ("no pre-head", lambda: self.write_round(1, [f("a")], head=None)),
            ("empty pre-head", lambda: self.write_round(1, [f("a")], head="")),
        ):
            with self.subTest(name):
                shutil.rmtree(self.ad, ignore_errors=True)
                self.ad.mkdir(parents=True, exist_ok=True)
                setup()
                self.assertDecision(2, "full", "origin/main")

    def test_a_cosmetic_previous_round_reviews_only_what_it_changed(self):
        self.write_round(1, [f("a"), f("b", "P3")])
        self.assertDecision(2, "delta", PREV_HEAD)

    def test_a_previous_round_that_applied_nothing_is_cosmetic(self):
        self.write_round(1, [])
        self.assertDecision(2, "delta", PREV_HEAD)

    def test_a_missing_severity_reads_as_p0(self):
        # Pre-2026-09-13 fixer results carry no severity at all; half the runs
        # on this machine are that shape. Guessing P2 for them would hand a
        # delta review to a round that may well have applied a P0.
        self.write_round(1, [f("a", None)])
        self.assertDecision(2, "full", "origin/main")

    def test_an_unparseable_severity_reads_as_p0(self):
        self.write_round(1, [f("a", "high")])
        self.assertDecision(2, "full", "origin/main")

    def test_a_non_integer_round_is_full(self):
        r = subprocess.run([sys.executable, str(SCRIPT), str(self.ad), "N"],
                           capture_output=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "REVIEW_MODE=full base=origin/main\n")

    def test_no_artifacts_dir_writes_nothing_into_the_working_directory(self):
        r = subprocess.run([sys.executable, str(SCRIPT)], cwd=self.ad,
                           capture_output=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "REVIEW_MODE=full base=origin/main\n")
        self.assertEqual(list(self.ad.iterdir()), [])

    def test_the_delta_decision_survives_a_real_fixer_result(self):
        # The synthetic fixtures above are this script's own shape. Pin it to a
        # result an actual lane wrote: round 3 of the api candidate in chain
        # 6a3a3b06, which applied one P1 and one P2 and therefore cannot delta.
        real = (Path.home() / ".archon/workspaces/_local/Goodword/artifacts/runs"
                / "2d65e873-01f9-425c-b6a8-1cfd767a42f8" / "round-3" / "fixer-result.json")
        if not real.is_file():
            self.skipTest(f"no local run artifacts at {real}")
        self.write_round(1, [], result=real.read_text(encoding="utf-8"))
        self.assertDecision(2, "full", "origin/main")


if __name__ == "__main__":
    unittest.main()
