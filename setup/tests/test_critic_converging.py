#!/usr/bin/env python3
"""A round cap stops churn, not convergence.

Two full RCA runs on one ticket ended at `RCA_PLAN_ROUND_CAP round=3 cap=3`.
Only one of them was churning. Run 6b9c088a's critic closed findings every
round -- 6 findings with 2 blocking, then 5 with 2, then 2 with 1 -- and was one
revision from ACCEPT when the counter spent the run. Run 57309e15's went the
other way, ending with more blocking findings than it started with.

Those two runs are the fixtures for the discriminator: it must say yes to the
first and no to the second, and it must measure BLOCKING findings rather than
total ones, because trading two P1s for one P1 while dropping three P3s looks
like progress by count and is not."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ARCHON = Path(__file__).resolve().parent.parent.parent
SCRIPT = ARCHON / "setup" / "critic-converging.py"


def walk(nodes):
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        yield n
        for key in ("loop_group", "body"):
            v = n.get(key)
            if isinstance(v, dict):
                yield from walk(v.get("nodes"))
            elif isinstance(v, list):
                yield from walk(v)


def node(lane, nid):
    doc = yaml.safe_load((ARCHON / "workflows" / f"{lane}.yaml").read_text(encoding="utf-8"))
    got = [n["bash"] for n in walk(doc.get("nodes")) if n.get("id") == nid]
    assert len(got) == 1, f"{lane}/{nid}"
    return got[0]


def f(sev, conf=100):
    return {"severity": sev, "confidence": conf, "kind": "gap"}


class Converging(unittest.TestCase):
    def setUp(self):
        self.ad = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.ad, ignore_errors=True)

    def round(self, n, findings):
        d = self.ad / f"rca-round-{n}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "critique.json").write_text(json.dumps({"verdict": "REVISE", "findings": findings}),
                                         encoding="utf-8")

    def ask(self, n):
        r = subprocess.run([sys.executable, str(SCRIPT), str(self.ad), str(n), "rca-round-"],
                           capture_output=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_falling_blocking_count_is_converging(self):
        self.round(2, [f("P1"), f("P1")])
        self.round(3, [f("P1"), f("P2")])
        self.assertEqual(self.ask(3), "yes")

    def test_rising_blocking_count_is_not(self):
        self.round(2, [f("P1"), f("P1")])
        self.round(3, [f("P1"), f("P1"), f("P1"), f("P1")])
        self.assertEqual(self.ask(3), "no")

    def test_a_flat_blocking_count_is_not(self):
        self.round(2, [f("P1"), f("P1")])
        self.round(3, [f("P1"), f("P1")])
        self.assertEqual(self.ask(3), "no")

    def test_dropping_only_advisories_is_not_progress(self):
        # The trap: total findings fall while the blocking ones do not move.
        self.round(2, [f("P1"), f("P1"), f("P3"), f("P3"), f("P3")])
        self.round(3, [f("P1"), f("P1")])
        self.assertEqual(self.ask(3), "no")

    def test_low_confidence_findings_do_not_block(self):
        self.round(2, [f("P1", 100), f("P1", 100)])
        self.round(3, [f("P1", 100), f("P1", 40)])
        self.assertEqual(self.ask(3), "yes")

    def test_an_uncoercible_confidence_counts_as_blocking(self):
        # Never let malformed output read as progress.
        self.round(2, [f("P1"), f("P1")])
        self.round(3, [f("P1"), {"severity": "P1", "confidence": None, "kind": "gap"}])
        self.assertEqual(self.ask(3), "no")

    def test_zero_blocking_is_accepts_job_not_this_one(self):
        self.round(2, [f("P1"), f("P1")])
        self.round(3, [f("P2")])
        self.assertEqual(self.ask(3), "no")

    def test_unreadable_or_first_round_is_no(self):
        self.round(1, [f("P1")])
        self.assertEqual(self.ask(1), "no")
        self.assertEqual(self.ask(2), "no")
        self.assertEqual(self.ask("junk"), "no")


class WiredIntoBothNodes(unittest.TestCase):
    def test_converge_grants_at_most_one_extra_round(self):
        bash = node("bugfix", "rca-converge")
        self.assertIn("critic-converging.py", bash, "converge never asks whether it is converging")
        self.assertIn("RCA_PLAN_ROUND_EXTENDED", bash)
        self.assertIn('[ ! -s "$EXT" ]', bash, "the extension is not bounded to once per run")

    def test_round_pre_honours_the_extension(self):
        bash = node("bugfix", "rca-round-pre")
        self.assertIn("rca-round-extended.txt", bash,
                      "the pre-check would refuse the round converge just bought")

    def test_the_extension_cannot_waive_a_blocking_finding(self):
        # It buys a round; it never turns REVISE into ACCEPT. Both guards that
        # enforce that must still be present.
        bash = node("bugfix", "rca-converge")
        self.assertIn('[ "$V" = ACCEPT ] && [ "$BLOCKING" != 0 ]', bash)
        self.assertIn("a P0 is never waivable", bash)


if __name__ == "__main__":
    unittest.main()
