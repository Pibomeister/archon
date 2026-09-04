#!/usr/bin/env python3
"""Candidate coverage must not be a copying task.

change-context.py listed up to fifteen prior-work candidates and the RCA was
told to "write change-context-assessment.json with exactly one row for every
candidate". Reproducing fifteen ids by hand is a task that silently loses one:
run da65d1b3's RCA returned 14 of 15 and the run died at rca-gate with
`CHANGE_CONTEXT=FAIL candidate coverage mismatch missing=['web-app#1203']`.
That was a dropped row, not a judgement -- the analysis was complete, the
transcription was not.

build() now seeds the file with one blank row per candidate, so the RCA fills
rows in rather than reproducing them. A row cannot be dropped, and a row left
blank fails validate by NAME."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ARCHON = Path(__file__).resolve().parent.parent.parent
SCRIPT = ARCHON / "setup" / "change-context.py"


class ChangeContextSeed(unittest.TestCase):
    def setUp(self):
        self.ad = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.ad, ignore_errors=True)
        (self.ad / "evidence").mkdir()
        (self.ad / "bug-report-normalized.md").write_text(
            "Mesh import created notes with the wrong content for contacts\n", encoding="utf-8")
        self.prs("open-prs-api.json", [
            {"number": 2303, "title": "fix note import content mapping",
             "files": [{"path": "libs/business-logic/src/lib/user-import/commit-import.service.ts"}]},
            {"number": 2334, "title": "contacts import notes content",
             "files": [{"path": "libs/business-logic/src/lib/user-import/commit-import.util.ts"}]},
        ])

    def prs(self, name, rows):
        (self.ad / "evidence" / name).write_text(json.dumps(rows), encoding="utf-8")

    def run_cc(self, verb):
        return subprocess.run([sys.executable, str(SCRIPT), verb, "--artifacts", str(self.ad)],
                              capture_output=True, encoding="utf-8")

    def seeded(self):
        return json.loads((self.ad / "change-context-assessment.json").read_text())["assessments"]

    def candidates(self):
        return [c["id"] for c in json.loads((self.ad / "change-context.json").read_text())["candidates"]]

    def test_build_seeds_one_blank_row_per_candidate(self):
        r = self.run_cc("build")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        ids = self.candidates()
        self.assertTrue(ids, "fixture produced no candidates; the probe is vacuous")
        self.assertEqual([row["id"] for row in self.seeded()], ids)
        self.assertTrue(all(row["decision"] == "" for row in self.seeded()))

    def test_an_unfilled_row_fails_by_name(self):
        self.run_cc("build")
        target = self.seeded()[0]["id"]
        r = self.run_cc("validate")
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"unfilled assessment for {target}", r.stdout + r.stderr)
        self.assertIn("seeded for you", r.stdout + r.stderr)

    def test_a_filled_seed_passes(self):
        self.run_cc("build")
        rows = self.seeded()
        for row in rows:
            row.update(decision="unrelated", evidence="read the PR diff", reason="different subsystem")
        (self.ad / "change-context-assessment.json").write_text(
            json.dumps({"schema_version": 1, "assessments": rows}), encoding="utf-8")
        r = self.run_cc("validate")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("CHANGE_CONTEXT_ASSESSMENT=PASS", r.stdout)

    def test_a_dropped_row_is_still_caught(self):
        # The seed makes dropping unlikely, not impossible: a model that
        # rewrites the file wholesale can still lose one, and the coverage
        # check must keep catching that. This is run da65d1b3's exact failure.
        self.run_cc("build")
        rows = self.seeded()
        self.assertGreaterEqual(len(rows), 2, "need two candidates to drop one")
        dropped = rows.pop()["id"]
        for row in rows:
            row.update(decision="unrelated", evidence="e", reason="r")
        (self.ad / "change-context-assessment.json").write_text(
            json.dumps({"schema_version": 1, "assessments": rows}), encoding="utf-8")
        r = self.run_cc("validate")
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"missing=['{dropped}']", r.stdout + r.stderr)

    def test_build_never_overwrites_existing_work(self):
        # A resume re-runs build. Wiping a filled assessment would throw away
        # the RCA's analysis and put the run back where it started.
        self.run_cc("build")
        rows = self.seeded()
        for row in rows:
            row.update(decision="solves", evidence="kept", reason="kept")
        (self.ad / "change-context-assessment.json").write_text(
            json.dumps({"schema_version": 1, "assessments": rows}), encoding="utf-8")
        self.run_cc("build")
        self.assertTrue(all(row["evidence"] == "kept" for row in self.seeded()))


if __name__ == "__main__":
    unittest.main()
