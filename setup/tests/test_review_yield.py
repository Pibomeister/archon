#!/usr/bin/env python3
"""What the yield measurement must not get wrong.

Converge reads this line out of a `$(...)` capture, so every failure mode here
is silent: a traceback yields an empty line and disables the reader, and a
missing previous round read as "zero findings applied" buys a convergence the
run never earned. Both are absent-input cases, not malformed-input cases --
round-reclaim.sh can decrement the durable counter, and the waiver ledger does
not exist at all on a first round with no advisory.

The second thing pinned here is the subject of the numbers: `applied`, `maxsev`
and `reraised` are the CURRENT round's, so a fixture whose previous round has a
different count and a worse severity must still report the current round's.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ARCHON = Path(__file__).resolve().parent.parent.parent
SCRIPT = ARCHON / "setup" / "review-yield.py"


def f(finding, severity="P2"):
    e = {"finding": finding, "action": "fixed it"}
    if severity is not None:
        e["severity"] = severity
    return e


# (name, previous round applied, current round applied, previous incomplete,
#  current incomplete, expected fields)
CASES = [
    ("one P2 each round converges", [f("a")], [f("b")], [], [],
     "verdict=DIMINISHING applied=1 maxsev=P2"),
    ("nothing applied either round converges", [], [], [], [],
     "verdict=DIMINISHING applied=0 maxsev=P2"),
    ("numbers are the current round's, not the pair's",
     [f("a", "P0"), f("b", "P0"), f("c", "P0")], [f("d")], [], [],
     "verdict=CONTINUE applied=1 maxsev=P2"),
    ("two fixes this round is still yielding", [f("a")], [f("b"), f("c")], [], [],
     "verdict=CONTINUE applied=2 maxsev=P2"),
    ("two fixes last round is still yielding", [f("a"), f("b")], [f("c")], [], [],
     "verdict=CONTINUE applied=1 maxsev=P2"),
    ("a P1 this round is not polishing", [f("a")], [f("b", "P1")], [], [],
     "verdict=CONTINUE applied=1 maxsev=P1"),
    ("a P1 last round is not polishing", [f("a", "P1")], [f("b")], [], [],
     "verdict=CONTINUE applied=1 maxsev=P2"),
    ("missing severity reads as P0", [f("a", None)], [f("b", None)], [], [],
     "verdict=CONTINUE applied=1 maxsev=P0"),
    ("unparseable severity reads as P0", [f("a")], [f("b", "high")], [], [],
     "verdict=CONTINUE applied=1 maxsev=P0"),
    ("incomplete work this round blocks", [f("a")], [f("b")], [], ["budget"],
     "verdict=CONTINUE applied=1 maxsev=P2"),
    ("incomplete work last round blocks", [f("a")], [f("b")], ["budget"], [],
     "verdict=CONTINUE applied=1 maxsev=P2"),
]


class ReviewYield(unittest.TestCase):
    def setUp(self):
        self.ad = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.ad, ignore_errors=True)

    def write_round(self, n, applied, incomplete=(), key="applied"):
        d = self.ad / f"round-{n}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "fixer-result.json").write_text(
            json.dumps({key: applied, "failed": [], "advisory": [],
                        "incomplete": list(incomplete)}), encoding="utf-8")

    def write_ledger(self, findings):
        (self.ad / "waivers.json").write_text(
            json.dumps({"schema": "archon.waiver-ledger.v1",
                        "entries": [{"key": "", "finding": x, "rationale": "no",
                                     "round": 1} for x in findings]}),
            encoding="utf-8")

    def ask(self, n):
        r = subprocess.run([sys.executable, str(SCRIPT), str(self.ad), str(n)],
                           capture_output=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        out = r.stdout.splitlines()
        self.assertEqual(len(out), 1, r.stdout)
        self.assertTrue(out[0].startswith("REVIEW_YIELD=OK "), out[0])
        return out[0]

    def test_table(self):
        for name, prev, cur, prev_inc, cur_inc, expected in CASES:
            with self.subTest(name):
                shutil.rmtree(self.ad, ignore_errors=True)
                self.ad.mkdir(parents=True, exist_ok=True)
                self.write_round(1, prev, prev_inc)
                self.write_round(2, cur, cur_inc)
                self.assertEqual(self.ask(2), f"REVIEW_YIELD=OK {expected} reraised=0")

    def test_round_1_can_never_converge(self):
        self.write_round(1, [f("a")])
        self.assertIn("verdict=CONTINUE applied=1 maxsev=P2", self.ask(1))

    def test_absent_previous_round_does_not_buy_a_convergence(self):
        self.write_round(2, [f("b")])
        self.assertEqual(self.ask(2),
                         "REVIEW_YIELD=OK verdict=CONTINUE applied=1 maxsev=P2 reraised=0")

    def test_empty_previous_result_does_not_buy_a_convergence(self):
        (self.ad / "round-1").mkdir(parents=True)
        (self.ad / "round-1" / "fixer-result.json").write_text("", encoding="utf-8")
        self.write_round(2, [f("b")])
        self.assertIn("verdict=CONTINUE", self.ask(2))

    def test_previous_result_without_an_applied_key_does_not_buy_a_convergence(self):
        self.write_round(1, [f("a")], key="fixes")
        self.write_round(2, [f("b")])
        self.assertIn("verdict=CONTINUE", self.ask(2))

    def test_unreadable_current_result_reports_continue_and_exits_zero(self):
        self.write_round(1, [f("a")])
        (self.ad / "round-2").mkdir(parents=True)
        (self.ad / "round-2" / "fixer-result.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(self.ask(2),
                         "REVIEW_YIELD=OK verdict=CONTINUE applied=0 maxsev=P0 reraised=0")

    def test_absent_current_result_reports_continue_and_exits_zero(self):
        self.write_round(1, [f("a")])
        self.assertIn("verdict=CONTINUE applied=0 maxsev=P0", self.ask(2))

    def test_absent_ledger_is_an_empty_ledger(self):
        self.write_round(1, [f("a")])
        self.write_round(2, [f("b")])
        self.assertFalse((self.ad / "waivers.json").exists())
        self.assertIn("reraised=0", self.ask(2))

    def test_unreadable_ledger_is_an_empty_ledger(self):
        self.write_round(1, [f("a")])
        self.write_round(2, [f("b")])
        (self.ad / "waivers.json").write_text("{not json", encoding="utf-8")
        self.assertIn("reraised=0", self.ask(2))

    def test_reraised_counts_a_waived_finding_across_normalization(self):
        self.write_round(1, [f("a")])
        self.write_round(2, [f("Fix  the Thing.")])
        self.write_ledger(["fix the thing"])
        self.assertIn("reraised=1", self.ask(2))

    def test_reraised_counts_entries_not_distinct_keys(self):
        self.write_round(1, [f("a")])
        self.write_round(2, [f("Fix the thing"), f("FIX THE THING!")])
        self.write_ledger(["fix the thing"])
        self.assertIn("applied=2 maxsev=P2 reraised=2", self.ask(2))

    def test_an_unwaived_finding_is_not_reraised(self):
        self.write_round(1, [f("a")])
        self.write_round(2, [f("something else entirely")])
        self.write_ledger(["fix the thing"])
        self.assertIn("reraised=0", self.ask(2))

    def test_reraised_is_reported_on_a_converging_pair(self):
        self.write_round(1, [f("fix the thing")])
        self.write_round(2, [f("Fix the thing")])
        self.write_ledger(["fix the thing"])
        self.assertEqual(self.ask(2),
                         "REVIEW_YIELD=OK verdict=DIMINISHING applied=1 maxsev=P2 reraised=1")

    def test_a_non_integer_round_reports_continue_and_exits_zero(self):
        self.assertIn("verdict=CONTINUE", self.ask("N"))


if __name__ == "__main__":
    unittest.main()
