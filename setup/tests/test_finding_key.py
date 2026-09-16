#!/usr/bin/env python3
"""One finding, restated, must key to one ledger entry.

A reviewer that re-raises a waived finding does not restate it verbatim: it
appends the round's provenance (" -- re-raised by project-standards this
round") and cites whatever file:line the code is at this round. Under a plain
normalization each restatement minted a fresh waiver, so the ledger grew a new
"unwaived" entry every round and `reraised` counted none of them -- the metric
that is supposed to catch a reviewer re-litigating a scope decision read zero
precisely when it was happening.

The real ledger this is measured against is chain 6a3a3b06's api candidate,
where 22 headings cover far fewer distinct findings.
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SETUP))
from finding_key import key_of  # noqa: E402

REAL_LEDGER = (Path.home() / ".archon/workspaces/_local/Goodword/artifacts/runs"
               / "2d65e873-01f9-425c-b6a8-1cfd767a42f8" / "waivers.md")
HEADING = re.compile(r"^## (?:\[round ([^\]]*)\] )?(.*)$")

BASE = "Service method name diverges from controller's convention"
# The same finding as the api lane's reviewer actually wrote it in rounds 1 and 3.
ROUND_1 = BASE + " (group.service.ts:979, getShareLink vs getGroupShareLink)"
ROUND_3 = (BASE + " (group.service.ts:972, getShareLink vs getGroupShareLink)"
           " -- re-raised by maintainability this round")


class KeyOf(unittest.TestCase):
    def test_the_provenance_suffix_is_not_part_of_the_finding(self):
        self.assertEqual(key_of("Guard is missing -- re-raised by adversarial this round"),
                         key_of("Guard is missing"))

    def test_an_em_dash_suffix_is_cut_too(self):
        self.assertEqual(key_of("Guard is missing — re-surfaced this round"),
                         key_of("Guard is missing"))

    def test_a_trailing_file_line_is_not_part_of_the_finding(self):
        self.assertEqual(key_of("Guard is missing (group.service.ts:979)"),
                         key_of("Guard is missing"))

    def test_a_trailing_file_line_carries_its_whole_parenthetical(self):
        self.assertEqual(key_of(ROUND_1), key_of(BASE))
        self.assertEqual(key_of(ROUND_1), key_of(ROUND_3))

    def test_a_double_hyphen_inside_a_word_is_not_a_suffix(self):
        # Only " -- " with spaces around it is provenance; a bare -- is text.
        self.assertNotEqual(key_of("Guard--missing in the loader"), key_of("Guard"))

    def test_a_parenthetical_that_is_not_a_location_survives(self):
        self.assertNotEqual(key_of("Multi-line comment (ponytail note)"),
                            key_of("Multi-line comment"))

    def test_normalization_still_matches_update_waivers_historic_keys(self):
        # waivers.json entries written before this module existed must keep
        # matching, or every in-flight chain's ledger silently resets.
        self.assertEqual(key_of("Fix the Thing."), "fix the thing")
        self.assertEqual(key_of("Split the loader\nacross  two\tmodules."),
                         "split the loader across two modules")

    def test_the_key_is_truncated(self):
        self.assertEqual(len(key_of("word " * 60)), 80)

    def test_the_real_ledger_collapses(self):
        if not REAL_LEDGER.is_file():
            self.skipTest(f"no local run artifacts at {REAL_LEDGER}")
        findings = [HEADING.match(l).group(2)
                    for l in REAL_LEDGER.read_text(encoding="utf-8").splitlines()
                    if l.startswith("## ")]
        self.assertEqual(len(findings), 22)
        self.assertLess(len({key_of(f) for f in findings}), len(findings))


class LedgerDedup(unittest.TestCase):
    """Proven through update-waivers.py itself, not through key_of alone: the
    ledger is what the review prompt reads, so the entry count is the claim."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ledger = self.tmp / "waivers.md"

    def run_round(self, n, *findings):
        rd = self.tmp / f"round-{n}"
        rd.mkdir(exist_ok=True)
        result = rd / "fixer-result.json"
        result.write_text(json.dumps(
            {"advisory": [{"finding": f, "action": "Waived: out of scope."} for f in findings]}),
            encoding="utf-8")
        p = subprocess.run([sys.executable, str(SETUP / "update-waivers.py"),
                            str(result), str(self.ledger)],
                           capture_output=True, encoding="utf-8")
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.strip()

    def entries(self):
        return json.loads((self.tmp / "waivers.json").read_text(encoding="utf-8"))["entries"]

    def test_a_re_raised_finding_does_not_mint_a_second_waiver(self):
        self.run_round(1, ROUND_1)
        self.assertEqual(self.run_round(3, ROUND_3),
                         "WAIVERS_ADDED=0 TOTAL_ADVISORY=1 LEDGER_KEYS=1")
        self.assertEqual(len(self.entries()), 1)

    def test_the_first_statement_of_the_finding_is_what_is_kept(self):
        self.run_round(1, ROUND_1)
        self.run_round(3, ROUND_3)
        self.assertEqual(self.entries()[0]["finding"], ROUND_1)
        self.assertEqual(self.entries()[0]["round"], "1")

    def test_two_genuinely_different_findings_still_get_two_entries(self):
        # Negative control for the dedup: a key that collapses everything would
        # pass the test above forever.
        self.run_round(1, ROUND_1)
        self.assertEqual(self.run_round(2, "Scan runs on every read (repo.ts:304)"),
                         "WAIVERS_ADDED=1 TOTAL_ADVISORY=1 LEDGER_KEYS=2")
        self.assertEqual(len(self.entries()), 2)


if __name__ == "__main__":
    unittest.main()
