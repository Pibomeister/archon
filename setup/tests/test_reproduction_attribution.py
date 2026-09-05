#!/usr/bin/env python3
"""Diagnostic: which symptoms are class-hardening-only ONLY because the proof
arrived after the disposition?

`fixed` requires the reported occurrence to be attributed. For a data bug that
is a production row, and probe-run plus rca-reassess can supply one while the
RCA is still open. For a deterministic behaviour bug the occurrence IS the
behaviour and the proof is the RED test -- which is written long after the
disposition that would need it. Measured on run 127a883f: symptom-dispositions
at 18:50:23, debug-phase reproduction_status "class-only" at 18:50:30,
red-sha.txt at 19:31:47. Forty-one minutes.

This script only REPORTS. Nothing in any workflow calls it with --apply, and
whether reproduction should count as attribution is a decision about what a run
may claim, not a thing to widen quietly -- the RCA prompt's own words are "do
not reach for `fixed` to make the run look complete"."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ARCHON = Path(__file__).resolve().parent.parent.parent
SCRIPT = ARCHON / "setup" / "reproduction-attribution.py"
SIG = "Unable to find an element with the text: 21."


class ReproductionAttribution(unittest.TestCase):
    def setUp(self):
        self.ad = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.ad, ignore_errors=True)
        self.write_all()

    def write_all(self, disposition="class-hardening-only", red_test="spec.tsx :: numbering",
                  visible=True, signature=SIG, committed=SIG, green=True):
        j = lambda n, o: (self.ad / n).write_text(json.dumps(o), encoding="utf-8")
        j("symptom-dispositions.json", {"schema_version": 2, "dispositions": [
            {"symptom_id": "E1", "disposition": disposition, "repo": "web-app"}]})
        j("causal-coverage.json", {"schema_version": 2, "coverage": [
            {"symptom_id": "E1", "red_test": red_test,
             "counterfactual_user_visible": visible, "occurrence_attributed": False}]})
        j("failing-test.json", {"predicted_failure_signature": signature})
        j("green.json", {"green": green})
        j("debug-phase.json", {"reproduction_status": "class-only"})
        (self.ad / "red-committed-out.txt").write_text(f"FAIL\n{committed}\n", encoding="utf-8")

    def run_it(self, apply=False):
        cmd = [sys.executable, str(SCRIPT), "--artifacts", str(self.ad)]
        if apply:
            cmd.append("--apply")
        r = subprocess.run(cmd, capture_output=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_reports_an_eligible_symptom(self):
        self.assertIn("WOULD_UPGRADE symptoms=E1", self.run_it())

    def test_it_changes_nothing_without_apply(self):
        before = (self.ad / "symptom-dispositions.json").read_text()
        self.run_it()
        self.assertEqual((self.ad / "symptom-dispositions.json").read_text(), before)

    def test_a_signature_the_committed_repro_did_not_produce_is_not_proof(self):
        self.write_all(committed="some other failure")
        self.assertIn("NOOP", self.run_it())

    def test_a_symptom_with_no_red_test_is_not_eligible(self):
        self.write_all(red_test="")
        self.assertIn("NOOP", self.run_it())

    def test_a_symptom_the_user_cannot_see_is_not_eligible(self):
        self.write_all(visible=False)
        self.assertIn("NOOP", self.run_it())

    def test_a_red_that_never_went_green_is_not_eligible(self):
        self.write_all(green=False)
        self.assertIn("NOOP", self.run_it())

    def test_it_never_touches_other_dispositions(self):
        for d in ("by-design", "separate-ticket", "product-semantics", "unresolved", "fixed"):
            with self.subTest(d=d):
                self.write_all(disposition=d)
                self.assertIn("NOOP", self.run_it())

    def test_no_workflow_invokes_it_with_apply(self):
        # The whole point: this is a diagnostic until someone decides that
        # reproduction counts as attribution. If a lane ever calls it with
        # --apply, that decision has been made and should be visible here.
        for y in sorted((ARCHON / "workflows").glob("*.yaml")):
            text = y.read_text(encoding="utf-8")
            if "reproduction-attribution.py" in text:
                self.assertNotIn("--apply", text.split("reproduction-attribution.py")[1][:200], y.name)


if __name__ == "__main__":
    unittest.main()
