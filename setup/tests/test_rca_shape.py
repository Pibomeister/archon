#!/usr/bin/env python3
"""Tests for rca-shape.sh against the fixtures/rca-minimal/ artifact dir.
Each negative control copies the fixture into a temp dir and mutates one
file, so the fixture stays the single source of truth for "valid"."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "rca-shape.sh"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "rca-minimal"


def write(ad, name, obj):
    (ad / name).write_text(json.dumps(obj), encoding="utf-8")


def minimal_artifacts(ad):
    shutil.copytree(FIXTURE, ad, dirs_exist_ok=True)


def run(ad):
    return subprocess.run(["bash", str(SCRIPT), str(ad)], capture_output=True, encoding="utf-8")


class RcaShapeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_minimal_valid_artifacts_pass(self):
        minimal_artifacts(self.tmp)
        r = run(self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("RCA_SHAPE=OK repo=api kind=unit", r.stdout)

    def test_probe_none_reason_required_when_empty(self):
        minimal_artifacts(self.tmp)
        write(self.tmp, "probe.json", {"probes": []})  # no none_reason
        r = run(self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("empty probes requires none_reason", r.stdout)

    def test_missing_repo_json_fails(self):
        minimal_artifacts(self.tmp)
        (self.tmp / "repo.json").unlink()
        r = run(self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("RCA_SHAPE=FAIL", r.stdout)

    def test_cross_repo_both_fails(self):
        minimal_artifacts(self.tmp)
        write(self.tmp, "repo.json", {"repo": "both"})
        r = run(self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("RCA_SHAPE=FAIL CROSS_REPO_BUG", r.stdout)

    def test_failing_test_repo_mismatch_fails(self):
        minimal_artifacts(self.tmp)
        d = json.loads((self.tmp / "failing-test.json").read_text())
        d["repo"] = "web-app"
        write(self.tmp, "failing-test.json", d)
        r = run(self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("failing-test repo != repo.json repo", r.stdout)

    def test_generic_signature_fails(self):
        minimal_artifacts(self.tmp)
        d = json.loads((self.tmp / "failing-test.json").read_text())
        d["predicted_failure_signature"] = "Error"
        write(self.tmp, "failing-test.json", d)
        r = run(self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("signature too generic", r.stdout)

    def test_probe_sql_write_keyword_rejected(self):
        minimal_artifacts(self.tmp)
        write(self.tmp, "probe.json", {"probes": [
            {"id": "p1", "question": "how many?", "sql": "SELECT * FROM foo; DROP TABLE bar"}
        ]})
        r = run(self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("write/DDL keyword rejected", r.stdout)

    def test_residual_bad_disposition_fails(self):
        minimal_artifacts(self.tmp)
        write(self.tmp, "residuals.json", {"residuals": [
            {"symptom": "x", "disposition": "maybe", "citation": "src/foo.ts:1"}
        ]})
        r = run(self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("disposition out of enum", r.stdout)

    def test_test_file_not_in_allowlist_fails(self):
        minimal_artifacts(self.tmp)
        write(self.tmp, "files-allowlist.json", ["src/foo.ts"])  # missing test_file
        r = run(self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("files-allowlist must include test_file", r.stdout)

    def test_verify_patterns_reject_non_unit_specs(self):
        minimal_artifacts(self.tmp)
        write(self.tmp, "verify.json", {"test_patterns": [
            "src/__tests__/foo.spec.ts",
            "apps/api-e2e/src/eval/search-eval.int.spec.ts",
        ]})
        r = run(self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("test_patterns must be unit specs", r.stdout)

    def test_fix_plan_files_not_subset_of_allowlist_fails(self):
        minimal_artifacts(self.tmp)
        d = json.loads((self.tmp / "fix-plan.json").read_text())
        d["files"] = ["src/foo.ts", "src/__tests__/foo.spec.ts", "src/sneaky.ts"]
        write(self.tmp, "fix-plan.json", d)
        r = run(self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("fix-plan.files not subset of files-allowlist", r.stdout)

    def test_ambiguous_no_plan_investigation_stops_with_typed_open_state(self):
        minimal_artifacts(self.tmp)
        boundary = json.loads((self.tmp / "boundary-trace.json").read_text())
        boundary["surface_equivalence"].update({
            "reported_surface_status": "ambiguous",
            "runtime_owner": "unselected",
            "test_runtime_owner": "candidate-only",
            "smoke_runtime_owner": "unselected",
            "test_matches_runtime": False,
            "smoke_matches_runtime": False,
        })
        write(self.tmp, "boundary-trace.json", boundary)
        write(self.tmp, "fix-plan.json", {
            "approach": "", "fix_site": "", "files": [], "risks": [], "alternatives": []
        })
        r = run(self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn(
            "RCA_INVESTIGATION_REQUIRED reason=surface-ambiguous ticket=open no_implementation=true",
            r.stdout,
        )

    def test_writes_repo_txt_on_pass(self):
        # rca-gate (bugfix.yaml:721) writes repo.txt after validation and 9
        # downstream nodes `cat` it; rca-shape.sh must be a true drop-in.
        minimal_artifacts(self.tmp)
        self.assertFalse((self.tmp / "repo.txt").exists())
        r = run(self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.tmp / "repo.txt").read_text(encoding="utf-8"), "api\n")

    def test_repo_txt_write_is_idempotent(self):
        minimal_artifacts(self.tmp)
        (self.tmp / "repo.txt").write_text("stale\n", encoding="utf-8")
        r = run(self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.tmp / "repo.txt").read_text(encoding="utf-8"), "api\n")

    def test_integration_kind_reemits_rca_note(self):
        minimal_artifacts(self.tmp)
        d = json.loads((self.tmp / "failing-test.json").read_text())
        d["kind"] = "integration"
        d["integration_note"] = "owns ports 54322/8001"
        write(self.tmp, "failing-test.json", d)
        r = run(self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(
            "RCA_NOTE=integration mutex: the repro will own ports 54322/8001 machine-globally",
            r.stdout,
        )
        self.assertIn("RCA_SHAPE=OK repo=api kind=integration", r.stdout)

    def test_integration_kind_without_note_fails(self):
        minimal_artifacts(self.tmp)
        d = json.loads((self.tmp / "failing-test.json").read_text())
        d["kind"] = "integration"
        write(self.tmp, "failing-test.json", d)
        r = run(self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("kind=integration requires integration_note", r.stdout)

    def test_allowlist_files_wrapper_shape_normalized(self):
        minimal_artifacts(self.tmp)
        write(self.tmp, "files-allowlist.json", {"files": ["src/foo.ts", "src/__tests__/foo.spec.ts"]})
        r = run(self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        normalized = json.loads((self.tmp / "files-allowlist.json").read_text())
        self.assertEqual(normalized, ["src/foo.ts", "src/__tests__/foo.spec.ts"])


if __name__ == "__main__":
    unittest.main()
