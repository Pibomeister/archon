#!/usr/bin/env python3
"""Tests for plan-shape.sh against fixtures/plan-minimal/ and
fixtures/plan-with-premises/ (the latter exercises the premise-citation
cited() check). Negative controls copy a fixture into a temp dir and mutate
one file, so the fixtures stay the single source of truth for "valid"."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "plan-shape.sh"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
MINIMAL = FIXTURES / "plan-minimal"
WITH_PREMISES = FIXTURES / "plan-with-premises"


def run(ad, wt, spec):
    return subprocess.run(["bash", str(SCRIPT), str(ad), str(wt), str(spec)], capture_output=True, encoding="utf-8")


class PlanShapeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ad = self.tmp / "artifacts"
        self.wt = self.tmp / "worktree"
        self.wt.mkdir()
        shutil.copytree(MINIMAL, self.ad)
        self.spec = self.ad / "spec.md"

    def test_minimal_valid_artifacts_pass_no_premises(self):
        r = run(self.ad, self.wt, self.spec)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.strip(), "PLAN_SHAPE=OK")

    def test_missing_plan_md_fails(self):
        (self.ad / "plan.md").unlink()
        r = run(self.ad, self.wt, self.spec)
        self.assertEqual(r.returncode, 1)
        self.assertIn("PLAN_SHAPE=FAIL no plan.md", r.stdout)

    def test_missing_heading_fails(self):
        text = (self.ad / "plan.md").read_text(encoding="utf-8")
        (self.ad / "plan.md").write_text(text.replace("## Verification\n", ""), encoding="utf-8")
        r = run(self.ad, self.wt, self.spec)
        self.assertEqual(r.returncode, 1)
        self.assertIn("PLAN_SHAPE=FAIL missing ## Verification", r.stdout)

    def test_empty_verify_json_fails(self):
        (self.ad / "verify.json").write_text(json.dumps({"test_patterns": []}), encoding="utf-8")
        r = run(self.ad, self.wt, self.spec)
        self.assertEqual(r.returncode, 1)
        self.assertIn("verify.json missing or empty", r.stdout)

    def test_empty_allowlist_fails(self):
        (self.ad / "files-allowlist.json").write_text("[]", encoding="utf-8")
        r = run(self.ad, self.wt, self.spec)
        self.assertEqual(r.returncode, 1)
        self.assertIn("files-allowlist.json missing or empty", r.stdout)

    def test_malformed_reader_audit_fails(self):
        (self.ad / "reader-audit.json").write_text(json.dumps({"nope": []}), encoding="utf-8")
        r = run(self.ad, self.wt, self.spec)
        self.assertEqual(r.returncode, 1)
        self.assertIn("reader-audit.json missing or malformed", r.stdout)


class PlanShapeWithPremisesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ad = self.tmp / "artifacts"
        shutil.copytree(MINIMAL, self.ad)
        # plan-with-premises only overrides spec.md and premises.json; reuse
        # the rest of the minimal fixture (verify.json etc).
        shutil.copyfile(WITH_PREMISES / "spec.md", self.ad / "spec.md")
        self.wt = self.tmp / "worktree"
        shutil.copytree(WITH_PREMISES / "worktree", self.wt)
        self.spec = self.ad / "spec.md"

    def test_cited_premises_pass(self):
        shutil.copyfile(WITH_PREMISES / "premises-cited.json", self.ad / "premises.json")
        r = run(self.ad, self.wt, self.spec)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_uncited_premise_quote_fails(self):
        shutil.copyfile(WITH_PREMISES / "premises-uncited.json", self.ad / "premises.json")
        r = run(self.ad, self.wt, self.spec)
        self.assertEqual(r.returncode, 1)
        self.assertIn("PLAN_SHAPE=FAIL premises.json missing, empty, or uncited", r.stdout)

    def test_missing_premises_json_fails(self):
        r = run(self.ad, self.wt, self.spec)  # premises.json never copied in
        self.assertEqual(r.returncode, 1)
        self.assertIn("PLAN_SHAPE=FAIL premises.json missing, empty, or uncited", r.stdout)


if __name__ == "__main__":
    unittest.main()
