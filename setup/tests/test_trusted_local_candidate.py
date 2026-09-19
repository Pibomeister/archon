#!/usr/bin/env python3
from __future__ import annotations
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parents[1]
HELPER = SETUP / "trusted-local-candidate.sh"
WRITER = SETUP / "write-local-candidate.py"


def run(cmd, **kw):
    result = subprocess.run(cmd, text=True, capture_output=True, **kw)
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return result


def write_json(path: Path, value: dict | list):
    path.write_text(json.dumps(value), encoding="utf-8")


class TrustedLocalCandidateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.spec = self.root / "spec.md"
        self.spec.write_text("# Feature\n", encoding="utf-8")
        self.repo = self.root / "api"
        self.repo.mkdir()
        run(["git", "-C", str(self.repo), "init", "-q"])
        run(["git", "-C", str(self.repo), "config", "user.email", "test@example.com"])
        run(["git", "-C", str(self.repo), "config", "user.name", "Test"])
        (self.repo / "src").mkdir()
        (self.repo / "src/api.ts").write_text("export const x = 1;\n", encoding="utf-8")
        (self.repo / "contract.json").write_text('{"ok":true}\n', encoding="utf-8")
        run(["git", "-C", str(self.repo), "add", "."])
        run(["git", "-C", str(self.repo), "commit", "-qm", "initial"])
        self.head = run(["git", "-C", str(self.repo), "rev-parse", "HEAD"]).stdout.strip()
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        write_json(self.artifacts / "params.json", {
            "spec": str(self.spec),
            "slug": "trusted-local-candidate",
            "repo": "api",
            "worktree": str(self.repo),
            "branch": "main",
            "feature_scope": "repositories",
            "feature_phase": "implement",
        })
        write_json(self.artifacts / "files-allowlist.json", ["src/api.ts", "contract.json"])
        write_json(self.artifacts / "verify.json", {"test_patterns": ["api.spec.ts"]})
        write_json(self.artifacts / "feature-result.json", {"outcome": "CHANGED"})
        write_json(self.artifacts / "joint-plan.json", {
            "contracts": [{"producer": "api", "consumer": "goodword-mcp", "artifact": "contract.json", "description": "fixture"}]
        })
        (self.artifacts / "bootstrap-head.txt").write_text(self.head + "\n", encoding="utf-8")
        (self.artifacts / "smoke-result.txt").write_text("SMOKE=PASS api-docs-json=200 guarded-control=401\n", encoding="utf-8")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.write_fake_bun()
        self.env = dict(os.environ)
        self.env["PATH"] = str(self.bin) + os.pathsep + self.env.get("PATH", "")
        self.env["ARCHON_FEATURE_SCOPE"] = "repositories"
        self.env["ARCHON_FEATURE_PHASE"] = "implement"

    def write_fake_bun(self, mode="pass"):
        script = f'''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys
mode={mode!r}
args=sys.argv[1:]
if args[:2] == ["run", "typecheck"]:
    sys.exit(1 if mode == "typecheck_fail" else 0)
if args[:2] == ["run", "lint"]:
    sys.exit(0)
if args[:2] == ["run", "export-interface"]:
    if mode == "export_dirty":
        pathlib.Path("src/api.ts").write_text("export const x = 2;\\n", encoding="utf-8")
    if mode == "export_commit":
        pathlib.Path("src/api.ts").write_text("export const x = 3;\\n", encoding="utf-8")
        subprocess.run(["git", "add", "src/api.ts"], check=True)
        subprocess.run(["git", "commit", "-qm", "export mutation"], check=True)
    if mode != "export_missing":
        out=pathlib.Path(os.environ["ARCHON_INTERFACE_OUTPUT_DIR"]) / "contract.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text('{{"exported":true}}\\n', encoding="utf-8")
    sys.exit(0)
if args[:3] == ["run", "test", "--"]:
    if "--outputFile" in args and mode != "no_report":
        out=pathlib.Path(args[args.index("--outputFile")+1])
        if mode == "zero":
            payload={{"success": True, "numPassedTests": 0, "numFailedTests": 0, "numFailedTestSuites": 0}}
        elif mode == "skipped_shape":
            payload={{"success": True, "numTotalTests": 1, "numPendingTests": 1, "numFailedTests": 0, "numFailedTestSuites": 0}}
        else:
            payload={{"success": True, "numPassedTests": 2, "numFailedTests": 0, "numFailedTestSuites": 0}}
        out.write_text(json.dumps(payload), encoding="utf-8")
    sys.exit(0)
sys.exit(2)
'''
        path = self.bin / "bun"
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)

    def run_helper(self):
        return subprocess.run(["bash", str(HELPER), str(self.artifacts)], text=True, capture_output=True, env=self.env)

    def test_resume_without_chain_env_reads_scope_and_phase_from_params(self):
        # `archon workflow resume` drops the launcher's ARCHON_FEATURE_* env; params.json is the durable copy.
        self.env.pop("ARCHON_FEATURE_SCOPE")
        self.env.pop("ARCHON_FEATURE_PHASE")
        result = self.run_helper()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn("missing repository feature scope", result.stdout)

    def test_success_writes_actual_unit_count_and_interface_hash(self):
        result = self.run_helper()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        payload = json.loads((self.artifacts / "feature-result.json").read_text(encoding="utf-8"))
        self.assertEqual(2, payload["verification"]["tests_passed"])
        self.assertEqual(2, [x for x in payload["verification_evidence"] if x["name"] == "unit"][0]["tests_passed"])
        self.assertNotIn("tests_passed", [x for x in payload["verification_evidence"] if x["name"] == "typecheck"][0])
        self.assertEqual("passed", [x for x in payload["verification_evidence"] if x["name"] == "smoke"][0]["status"])
        row = payload["interface_artifacts"][0]
        self.assertEqual("interface/contract.json", row["path"])
        self.assertTrue((self.artifacts / row["path"]).is_file())
        self.assertEqual("worktree", row["source_kind"])

    def set_verify_only(self, previous_head):
        params = json.loads((self.artifacts / "params.json").read_text(encoding="utf-8"))
        params["feature_verify_only"] = "yes"
        params["feature_previous_head"] = previous_head
        write_json(self.artifacts / "params.json", params)

    def test_verify_only_reopen_stops_when_the_hand_fix_never_landed(self):
        self.set_verify_only(self.head)
        result = self.run_helper()
        self.assertEqual(1, result.returncode)
        self.assertIn(f"REOPEN=NOOP head={self.head}", result.stdout)
        self.assertNotIn("LOCAL_CANDIDATE=PASS", result.stdout)

    def test_verify_only_reopen_proceeds_once_the_hand_fix_is_committed(self):
        (self.repo / "src/api.ts").write_text("export const x = 2;\n", encoding="utf-8")
        run(["git", "-C", str(self.repo), "commit", "-qam", "fix: hand patch"])
        self.set_verify_only(self.head)
        result = self.run_helper()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn("REOPEN=NOOP", result.stdout)
        self.assertIn("LOCAL_CANDIDATE=PASS", result.stdout)

    def test_exported_interface_artifact_is_copied_from_declared_output_dir(self):
        write_json(self.artifacts / "joint-plan.json", {
            "contracts": [{
                "producer": "api",
                "consumer": "goodword-mcp",
                "artifact": "contract.json",
                "description": "fixture",
                "export": {"argv": ["bun", "run", "export-interface"]},
            }]
        })
        result = self.run_helper()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        payload = json.loads((self.artifacts / "feature-result.json").read_text(encoding="utf-8"))
        row = payload["interface_artifacts"][0]
        copied = self.artifacts / row["path"]
        self.assertEqual("export", row["source_kind"])
        self.assertEqual("contract.json", row["source_path"])
        self.assertEqual('{"exported":true}\n', copied.read_text(encoding="utf-8"))
        self.assertEqual(row["sha256"], __import__("hashlib").sha256(copied.read_bytes()).hexdigest())

    def test_export_missing_artifact_fails_before_candidate_receipt(self):
        write_json(self.artifacts / "joint-plan.json", {
            "contracts": [{
                "producer": "api",
                "consumer": "goodword-mcp",
                "artifact": "contract.json",
                "description": "fixture",
                "export": {"argv": ["bun", "run", "export-interface"]},
            }]
        })
        self.write_fake_bun("export_missing")
        result = self.run_helper()
        self.assertEqual(1, result.returncode)
        self.assertIn("interface export did not produce declared artifact", result.stdout + result.stderr)

    def test_stale_export_output_cannot_satisfy_missing_retry(self):
        write_json(self.artifacts / "joint-plan.json", {
            "contracts": [{
                "producer": "api",
                "consumer": "goodword-mcp",
                "artifact": "contract.json",
                "description": "fixture",
                "export": {"argv": ["bun", "run", "export-interface"]},
            }]
        })
        stale = self.artifacts / "interface-export" / "contract.json"
        stale.parent.mkdir(parents=True)
        stale.write_text('{"stale":true}\n', encoding="utf-8")
        self.write_fake_bun("export_missing")
        result = self.run_helper()
        self.assertEqual(1, result.returncode)
        self.assertIn("interface export did not produce declared artifact", result.stdout + result.stderr)

    def test_export_that_mutates_source_worktree_is_rejected(self):
        write_json(self.artifacts / "joint-plan.json", {
            "contracts": [{
                "producer": "api",
                "consumer": "goodword-mcp",
                "artifact": "contract.json",
                "description": "fixture",
                "export": {"argv": ["bun", "run", "export-interface"]},
            }]
        })
        self.write_fake_bun("export_dirty")
        result = self.run_helper()
        self.assertEqual(1, result.returncode)
        self.assertIn("LOCAL_CANDIDATE=FAIL dirty worktree", result.stdout + result.stderr)

    def test_export_that_changes_head_but_leaves_worktree_clean_is_rejected(self):
        write_json(self.artifacts / "joint-plan.json", {
            "contracts": [{
                "producer": "api",
                "consumer": "goodword-mcp",
                "artifact": "contract.json",
                "description": "fixture",
                "export": {"argv": ["bun", "run", "export-interface"]},
            }]
        })
        self.write_fake_bun("export_commit")
        result = self.run_helper()
        self.assertEqual(1, result.returncode)
        self.assertIn("LOCAL_CANDIDATE=FAIL candidate head changed during export", result.stdout + result.stderr)

    def test_export_argv_with_nul_is_rejected_before_subprocess(self):
        self.prepare_writer_inputs()
        write_json(self.artifacts / "joint-plan.json", {
            "contracts": [{
                "producer": "api",
                "consumer": "goodword-mcp",
                "artifact": "contract.json",
                "description": "fixture",
                "export": {"argv": ["bun", "bad\u0000arg"]},
            }]
        })
        result = subprocess.run([
            "python3", str(WRITER), "--run-interface-exports",
            str(self.artifacts), "api", str(self.repo), str(self.artifacts / "interface-export"),
        ], text=True, capture_output=True)
        self.assertEqual(1, result.returncode)
        self.assertIn("must not contain NUL bytes", result.stdout + result.stderr)

    def test_export_output_dir_must_stay_under_artifacts(self):
        self.prepare_writer_inputs()
        write_json(self.artifacts / "joint-plan.json", {
            "contracts": [{
                "producer": "api",
                "consumer": "goodword-mcp",
                "artifact": "contract.json",
                "description": "fixture",
                "export": {"argv": ["bun", "run", "export-interface"]},
            }]
        })
        result = subprocess.run([
            "python3", str(WRITER), "--run-interface-exports",
            str(self.artifacts), "api", str(self.repo), str(self.root / "outside-export"),
        ], text=True, capture_output=True)
        self.assertEqual(1, result.returncode)
        self.assertIn("interface export output escapes allowed root", result.stdout + result.stderr)

    def test_typecheck_failure_stops_before_later_success(self):
        self.write_fake_bun("typecheck_fail")
        result = self.run_helper()
        self.assertEqual(1, result.returncode)
        self.assertFalse((self.artifacts / "local-candidate-test-1.json").exists())

    def test_stale_report_and_no_new_report_fail(self):
        write_json(self.artifacts / "local-candidate-test-1.json", {"success": True, "numPassedTests": 9, "numFailedTests": 0, "numFailedTestSuites": 0})
        self.write_fake_bun("no_report")
        result = self.run_helper()
        self.assertEqual(1, result.returncode)
        self.assertIn("test report count", result.stdout + result.stderr)

    def test_zero_and_all_skipped_reports_fail(self):
        self.write_fake_bun("zero")
        result = self.run_helper()
        self.assertEqual(1, result.returncode)
        self.assertIn("zero passed tests", result.stdout + result.stderr)
        self.write_fake_bun("skipped_shape")
        result = self.run_helper()
        self.assertEqual(1, result.returncode)
        self.assertIn("missing integer numPassedTests", result.stdout + result.stderr)
        self.prepare_writer_inputs()
        write_json(self.artifacts / "local-candidate-test-1.json", {"success": True, "numPassedTests": 2, "numFailedTests": 0, "numFailedTestSuites": 1})
        result = self.run_writer()
        self.assertEqual(1, result.returncode)
        self.assertIn("test report has failures", result.stdout + result.stderr)

    def test_failed_or_skipped_smoke_cannot_pass_api(self):
        (self.artifacts / "smoke-result.txt").write_text("SMOKE=FAIL api-docs-json code=000\n", encoding="utf-8")
        result = self.run_helper()
        self.assertEqual(1, result.returncode)
        self.assertIn("smoke result is not acceptable", result.stdout + result.stderr)
        (self.artifacts / "smoke-result.txt").write_text("SMOKE=NOT_APPLICABLE (api declares no boot smoke)\n", encoding="utf-8")
        result = self.run_helper()
        self.assertEqual(1, result.returncode)
        self.assertIn("smoke result is not acceptable", result.stdout + result.stderr)

    def test_boolean_report_counter_is_not_a_test_count(self):
        self.prepare_writer_inputs()
        write_json(self.artifacts / "local-candidate-test-1.json", {
            "success": True, "numPassedTests": True, "numFailedTests": 0, "numFailedTestSuites": 0,
        })
        result = self.run_writer()
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing integer numPassedTests", result.stdout)
    def prepare_writer_inputs(self):
        (self.artifacts / "local-candidate-patterns.txt").write_text("api.spec.ts\n", encoding="utf-8")
        write_json(self.artifacts / "local-candidate-test-1.json", {"success": True, "numPassedTests": 2, "numFailedTests": 0, "numFailedTestSuites": 0})

    def run_writer(self):
        return subprocess.run(["python3", str(WRITER), str(self.artifacts), "api", self.head, self.head], text=True, capture_output=True)

    def test_missing_declared_interface_and_symlink_escape_fail(self):
        self.prepare_writer_inputs()
        (self.repo / "contract.json").unlink()
        result = self.run_writer()
        self.assertEqual(1, result.returncode)
        self.assertIn("declared interface artifact missing", result.stdout + result.stderr)
        outside = self.root / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        (self.repo / "contract.json").symlink_to(outside)
        result = self.run_writer()
        self.assertEqual(1, result.returncode)
        self.assertIn("interface source escapes", result.stdout + result.stderr)

    def seed_review_commits(self, subject="feat(api): candidate change"):
        for index in range(4):
            (self.repo / "src/api.ts").write_text(f"export const x = {index + 2};\n", encoding="utf-8")
            run(["git", "-C", str(self.repo), "add", "src/api.ts"])
            run(["git", "-C", str(self.repo), "commit", "-qm", "feat" if index == 0 else f"fix(review): {index}"])
        (self.artifacts / "commit-msg.txt").write_text(f"{subject}\n\nBody line.\n", encoding="utf-8")
        return subject

    def git(self, *args):
        return run(["git", "-C", str(self.repo), *args]).stdout.strip()

    def test_squash_collapses_review_commits_into_one(self):
        subject = self.seed_review_commits()
        tree_before = self.git("rev-parse", "HEAD^{tree}")
        result = self.run_helper()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("CANDIDATE_SQUASH=OK commits=1 head=", result.stdout)
        self.assertEqual("1", self.git("rev-list", "--count", f"{self.head}..HEAD"))
        self.assertEqual(tree_before, self.git("rev-parse", "HEAD^{tree}"))
        self.assertEqual(subject, self.git("log", "-1", "--format=%s"))

    def test_second_run_leaves_head_byte_identical(self):
        self.seed_review_commits()
        first = self.run_helper()
        self.assertEqual(0, first.returncode, first.stdout + first.stderr)
        squashed = self.git("rev-parse", "HEAD")
        second = self.run_helper()
        self.assertEqual(0, second.returncode, second.stdout + second.stderr)
        self.assertIn("CANDIDATE_SQUASH=SKIP already squashed", second.stdout)
        self.assertEqual(squashed, self.git("rev-parse", "HEAD"))

    def test_missing_commit_message_leaves_history_intact(self):
        self.seed_review_commits()
        (self.artifacts / "commit-msg.txt").unlink()
        result = self.run_helper()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("CANDIDATE_SQUASH=SKIP no commit-msg.txt", result.stdout)
        self.assertEqual("4", self.git("rev-list", "--count", f"{self.head}..HEAD"))

    def test_squash_that_changes_the_tree_is_rejected(self):
        self.seed_review_commits()
        hook = self.repo / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\necho smuggled > smuggled.txt\ngit add smuggled.txt\n", encoding="utf-8")
        hook.chmod(0o755)
        result = self.run_helper()
        self.assertEqual(1, result.returncode)
        self.assertIn("CANDIDATE_SQUASH=FAIL tree changed", result.stdout)

    def test_mcp_smoke_not_applicable_is_recorded_not_passed(self):
        write_json(self.artifacts / "params.json", {
            "spec": str(self.spec),
            "slug": "trusted-local-candidate",
            "repo": "goodword-mcp",
            "worktree": str(self.repo),
            "branch": "main",
            "feature_scope": "repositories",
            "feature_phase": "implement",
        })
        write_json(self.artifacts / "joint-plan.json", {"contracts": []})
        (self.artifacts / "smoke-result.txt").write_text("SMOKE=NOT_APPLICABLE (goodword-mcp declares no boot smoke)\n", encoding="utf-8")
        self.prepare_writer_inputs()
        result = subprocess.run(["python3", str(WRITER), str(self.artifacts), "goodword-mcp", self.head, self.head], text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        payload = json.loads((self.artifacts / "feature-result.json").read_text(encoding="utf-8"))
        self.assertEqual("not_applicable", [x for x in payload["verification_evidence"] if x["name"] == "smoke"][0]["status"])


if __name__ == "__main__":
    unittest.main()
