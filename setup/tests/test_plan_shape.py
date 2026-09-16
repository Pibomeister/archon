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
WITH_INTERFACE = FIXTURES / "plan-with-interface"


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

    def test_browser_policy_digest_mismatch_fails(self):
        (self.ad / "browser-evidence.sha256").write_text("0" * 64 + "\n", encoding="utf-8")
        r = run(self.ad, self.wt, self.spec)
        self.assertEqual(r.returncode, 1)
        self.assertIn("browser-evidence.json/.sha256 missing or malformed", r.stdout)

    def test_integration_spec_pattern_is_rejected_before_approval(self):
        # api's unit jest config ignores .int.spec.ts: gate-tests would exit
        # "No tests found" after implement, where no resume can repair it.
        (self.ad / "verify.json").write_text(json.dumps(
            {"test_patterns": ["apps/api/src/x/foo.int.spec.ts"]}), encoding="utf-8")
        r = run(self.ad, self.wt, self.spec)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("UNIT_PATTERNS=FAIL repo=api pattern=apps/api/src/x/foo.int.spec.ts", r.stdout)

    def test_stem_selecting_only_excluded_tracked_specs_is_rejected(self):
        subprocess.run(["git", "init", "-q", str(self.wt)], check=True)
        for rel in ("src/home-airports.int.spec.ts", "src/other.spec.ts"):
            (self.wt / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.wt / rel).write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.wt), "add", "."], check=True)
        (self.ad / "params.json").write_text(json.dumps(
            {"repo": "api", "worktree": str(self.wt)}), encoding="utf-8")
        (self.ad / "verify.json").write_text(json.dumps({"test_patterns": ["home-airports"]}), encoding="utf-8")
        r = run(self.ad, self.wt, self.spec)
        self.assertIn("UNIT_PATTERNS=FAIL repo=api pattern=home-airports", r.stdout)
        (self.ad / "verify.json").write_text(json.dumps({"test_patterns": ["other"]}), encoding="utf-8")
        self.assertNotIn("UNIT_PATTERNS=FAIL", run(self.ad, self.wt, self.spec).stdout,
                         "control: a pattern selecting a unit spec is accepted")

    def test_joint_stage_patterns_use_their_own_repo_excludes(self):
        (self.ad / "joint-plan.json").write_text(json.dumps({"stages": {
            "goodword-mcp": {"test_patterns": ["tests/tool.e2e.test.ts"]}}}), encoding="utf-8")
        r = run(self.ad, self.wt, self.spec)
        self.assertIn("UNIT_PATTERNS=FAIL repo=goodword-mcp", r.stdout)


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

    def test_line_suffix_on_evidence_file_is_stripped(self):
        # a "path:N-M" citation is a formatting habit (same class as rca-gate's fix)
        prem = json.loads((WITH_PREMISES / "premises-cited.json").read_text(encoding="utf-8"))
        for pr in prem:
            for e in pr["evidence"]:
                e["file"] = e["file"] + ":12-14"
        (self.ad / "premises.json").write_text(json.dumps(prem), encoding="utf-8")
        r = run(self.ad, self.wt, self.spec)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_missing_premises_json_fails(self):
        r = run(self.ad, self.wt, self.spec)  # premises.json never copied in
        self.assertEqual(r.returncode, 1)
        self.assertIn("PLAN_SHAPE=FAIL premises.json missing, empty, or uncited", r.stdout)


class PlanShapeInterfaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ad = self.tmp / "artifacts"
        self.wt = self.tmp / "worktree"
        self.wt.mkdir()
        shutil.copytree(MINIMAL, self.ad)
        shutil.copyfile(WITH_INTERFACE / "spec.md", self.ad / "spec.md")
        self.spec = self.ad / "spec.md"

    def test_pinned_artifact_declared_passes(self):
        shutil.copyfile(WITH_INTERFACE / "joint-plan-foo.json", self.ad / "joint-plan.json")
        r = run(self.ad, self.wt, self.spec)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.strip(), "PLAN_SHAPE=OK")

    def test_undeclared_artifact_fails(self):
        shutil.copyfile(WITH_INTERFACE / "joint-plan-bar.json", self.ad / "joint-plan.json")
        r = run(self.ad, self.wt, self.spec)
        self.assertEqual(r.returncode, 1)
        self.assertIn("PLAN_SHAPE=FAIL interface deviation undeclared: src/api/bar.ts", r.stdout)

    def test_declared_deviation_passes(self):
        shutil.copyfile(WITH_INTERFACE / "joint-plan-bar.json", self.ad / "joint-plan.json")
        with open(self.ad / "plan.md", "a", encoding="utf-8") as f:
            f.write("\ndeviation: src/api/bar.ts\n")
        r = run(self.ad, self.wt, self.spec)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.strip(), "PLAN_SHAPE=OK")

    def test_missing_pinned_decisions_fails(self):
        plan = json.loads((WITH_INTERFACE / "joint-plan-foo.json").read_text(encoding="utf-8"))
        del plan["pinned_decisions"]
        (self.ad / "joint-plan.json").write_text(json.dumps(plan), encoding="utf-8")
        r = run(self.ad, self.wt, self.spec)
        self.assertEqual(r.returncode, 1)
        self.assertIn("PLAN_SHAPE=FAIL pinned_decisions missing", r.stdout)

    def test_pinned_decision_missing_a_field_fails(self):
        plan = json.loads((WITH_INTERFACE / "joint-plan-foo.json").read_text(encoding="utf-8"))
        del plan["pinned_decisions"][0]["rule"]
        (self.ad / "joint-plan.json").write_text(json.dumps(plan), encoding="utf-8")
        r = run(self.ad, self.wt, self.spec)
        self.assertEqual(r.returncode, 1)
        self.assertIn("PLAN_SHAPE=FAIL pinned_decisions missing", r.stdout)

    def test_unpinned_spec_does_not_require_pinned_decisions(self):
        # Negative control: the gate keys on the spec section, not on the
        # joint plan. Drop the section and the same plan passes.
        plan = json.loads((WITH_INTERFACE / "joint-plan-foo.json").read_text(encoding="utf-8"))
        del plan["pinned_decisions"]
        (self.ad / "joint-plan.json").write_text(json.dumps(plan), encoding="utf-8")
        self.spec.write_text("# Spec\n", encoding="utf-8")
        r = run(self.ad, self.wt, self.spec)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.strip(), "PLAN_SHAPE=OK")

    def test_no_joint_plan_json_skips_check(self):
        # joint-plan.json never copied in; the gate only applies when it exists.
        r = run(self.ad, self.wt, self.spec)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.strip(), "PLAN_SHAPE=OK")


if __name__ == "__main__":
    unittest.main()
