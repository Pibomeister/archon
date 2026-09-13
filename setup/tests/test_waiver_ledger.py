#!/usr/bin/env python3
"""Tests for update-waivers.py's waivers.json ledger: one json entry per
advisory, normalized-key dedup, idempotent re-runs, a mixed-vintage waivers.md
written under the pre-json raw-prefix rule, and a finding carrying an internal
newline (which today's script would write as a multi-line heading)."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "update-waivers.py"

# waivers.md exactly as the pre-B1 script wrote it: finding only .strip()ed, so
# an internal newline lands in the heading verbatim.
LEGACY_MD = (
    "# Waiver ledger\n"
    "Advisory findings the fixer declined with rationale. Reviewers must not\n"
    "re-raise a waived finding as actionable without specific new evidence.\n"
    "\n## [round 1] Fix the Thing.\n\nOut of scope for this change.\n"
    "\n## [round 1] Split the loader\nacross two modules.\n\nOut of scope for this change.\n"
)
LEGACY_SINGLE = "Fix the Thing."
LEGACY_MULTI = "Split the loader\nacross two modules."


class WaiverLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ledger = self.tmp / "waivers.md"
        self.json_path = self.tmp / "waivers.json"

    def run_round(self, n, *findings):
        rd = self.tmp / f"round-{n}"
        rd.mkdir(exist_ok=True)
        result = rd / "fixer-result.json"
        result.write_text(
            json.dumps({"advisory": [{"finding": f, "action": "Out of scope."} for f in findings]}),
            encoding="utf-8",
        )
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), str(result), str(self.ledger)],
            capture_output=True, encoding="utf-8",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def entries(self):
        return json.loads(self.json_path.read_text(encoding="utf-8"))["entries"]

    def headings(self):
        return [l for l in self.ledger.read_text(encoding="utf-8").splitlines() if l.startswith("## ")]

    # (a) one json entry per advisory
    def test_one_json_entry_per_advisory(self):
        out = self.run_round(1, "Fix the Thing.", "Rename the helper.")
        self.assertEqual(out, "WAIVERS_ADDED=2 TOTAL_ADVISORY=2 LEDGER_KEYS=2")
        doc = json.loads(self.json_path.read_text(encoding="utf-8"))
        self.assertEqual(doc["schema"], "archon.waiver-ledger.v1")
        self.assertEqual([e["finding"] for e in doc["entries"]], ["Fix the Thing.", "Rename the helper."])
        self.assertEqual([e["round"] for e in doc["entries"]], ["1", "1"])
        self.assertEqual(doc["entries"][0]["key"], "fix the thing")
        self.assertEqual(doc["entries"][0]["rationale"], "Out of scope.")
        self.assertEqual(len(self.headings()), 2)

    # (b) punctuation/case variants collapse to one entry
    def test_normalized_key_collapses_variants(self):
        self.run_round(1, "Fix the Thing.")
        out = self.run_round(2, "fix the thing")
        self.assertEqual(out, "WAIVERS_ADDED=0 TOTAL_ADVISORY=1 LEDGER_KEYS=1")
        self.assertEqual(len(self.entries()), 1)
        self.assertEqual(len(self.headings()), 1)

    # (c) re-running on the same result adds nothing
    def test_rerun_same_result_adds_nothing(self):
        self.run_round(1, "Fix the Thing.")
        before = self.ledger.read_text(encoding="utf-8")
        out = self.run_round(1, "Fix the Thing.")
        self.assertEqual(out, "WAIVERS_ADDED=0 TOTAL_ADVISORY=1 LEDGER_KEYS=1")
        self.assertEqual(self.ledger.read_text(encoding="utf-8"), before)
        self.assertEqual(len(self.entries()), 1)

    # (d) mixed vintage: md written by the pre-json script, no waivers.json
    def test_mixed_vintage_ledger(self):
        self.ledger.write_text(LEGACY_MD, encoding="utf-8")
        self.assertFalse(self.json_path.exists())
        out = self.run_round(2, LEGACY_SINGLE, LEGACY_MULTI)
        self.assertEqual(out, "WAIVERS_ADDED=0 TOTAL_ADVISORY=2 LEDGER_KEYS=2")
        self.assertEqual(len(self.headings()), 2)
        self.assertEqual(len(self.entries()), 2)

    # (e) a finding with an internal newline writes a single-line heading
    def test_multiline_finding_single_line_heading(self):
        self.run_round(1, LEGACY_MULTI)
        self.assertEqual(self.headings(), ["## [round 1] Split the loader across two modules."])
        # a resumed round of a chain whose waivers.json predates B1 must still dedup
        self.json_path.unlink()
        out = self.run_round(2, LEGACY_MULTI)
        self.assertEqual(out, "WAIVERS_ADDED=0 TOTAL_ADVISORY=1 LEDGER_KEYS=1")
        self.assertEqual(len(self.headings()), 1)
        self.assertEqual(len(self.entries()), 1)


if __name__ == "__main__":
    unittest.main()
