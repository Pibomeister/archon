import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import feature_chain as fc
import review_verification as rv


class ReviewVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        def git(*args):
            return subprocess.run(["git", "-C", str(self.repo), *args], check=True, text=True, capture_output=True).stdout.strip()
        git("init", "-q")
        git("config", "user.name", "Test")
        git("config", "user.email", "test@example.com")
        (self.repo / "fixture.txt").write_text("candidate")
        git("add", ".")
        git("commit", "-qm", "fixture")
        head = git("rev-parse", "HEAD")
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.control = self.root / "control"
        self.chain_id, self.run_id = "a" * 32, "b" * 32
        self.calls = self.root / "calls.txt"
        code = "from pathlib import Path;p=Path(" + repr(str(self.calls)) + ");p.write_text(p.read_text()+'x' if p.exists() else 'x');print('PASS')"
        self.command = {"argv": [sys.executable, "-c", code]}
        self.state = {"schema_version": 2, "logical_chain_id": self.chain_id, "chain_secret": "s" * 64,
                      "current_run": {"run_id": self.run_id, "repo": "api", "artifacts_dir": str(self.artifacts)},
                      "worktrees": {"api": {"worktree": str(self.repo), "baseline": head}},
                      "stages": {"api": {"plan": {"verification": [self.command]}}}}
        fc.write_state(self.control, self.state)
        patch = mock.patch.dict(os.environ, {"ARCHON_CONTROL_DIR": str(self.control), "ARCHON_FEATURE_CHAIN_ID": self.chain_id})
        patch.start()
        self.addCleanup(patch.stop)

    def test_same_exact_candidate_command_and_environment_executes_once(self):
        first = rv.verify(self.artifacts)
        second = rv.verify(self.artifacts)
        self.assertEqual(self.calls.read_text(), "x")
        self.assertEqual(first[0]["retained_output"], second[0]["retained_output"])
        self.assertEqual(Path(first[0]["retained_output"]).read_text(), "PASS\n")
        self.assertEqual(first[0]["exit_status"], 0)

    def test_environment_change_invalidates_receipt_and_retains_prior_output(self):
        first = rv.verify(self.artifacts)
        with mock.patch.dict(os.environ, {"REVIEW_TEST_FEATURE_FLAG": "changed"}):
            second = rv.verify(self.artifacts)
        self.assertEqual(self.calls.read_text(), "xx")
        self.assertNotEqual(first[0]["environment_fingerprint"], second[0]["environment_fingerprint"])
        self.assertTrue(Path(first[0]["retained_output"]).exists())

    def test_dependency_content_changes_invalidate_fingerprint(self):
        modules = self.root / "modules"
        modules.mkdir()
        package = modules / "implementation.js"
        package.write_text("old")
        old = rv.files_digest([modules])
        package.write_text("new")
        self.assertNotEqual(old, rv.files_digest([modules]))

    def test_dependency_symlink_selection_is_part_of_fingerprint(self):
        modules = self.root / "modules"
        for version in ["v1", "v2"]:
            package = modules / ".store" / version
            package.mkdir(parents=True)
            (package / "index.js").write_text(version)
        link = modules / "selected"
        link.symlink_to(modules / ".store/v1", target_is_directory=True)
        old = rv.files_digest([modules])
        link.unlink()
        link.symlink_to(modules / ".store/v2", target_is_directory=True)
        self.assertNotEqual(old, rv.files_digest([modules]))

    def test_failed_command_retains_evidence_and_does_not_become_success_cache(self):
        self.state["stages"]["api"]["plan"]["verification"] = [{"argv": [sys.executable, "-c", "print('failure');raise SystemExit(3)"]}]
        fc.write_state(self.control, self.state)
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, "VERIFICATION_REQUIRED"):
                rv.verify(self.artifacts)
        receipts = list((self.control / "review-verification" / self.run_id).glob("*.failed.*.json"))
        self.assertEqual(len(receipts), 2)
        self.assertEqual(json.loads(receipts[0].read_text())["receipt"]["exit_status"], 3)


if __name__ == "__main__":
    unittest.main()
