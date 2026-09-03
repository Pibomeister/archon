#!/usr/bin/env python3
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "run-repro.sh"


class RunReproTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.worktree = self.tmp / "worktree"
        self.artifacts = self.tmp / "artifacts"
        self.repo.mkdir()
        self.artifacts.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Test"], check=True)
        (self.repo / "README").write_text("test\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "README"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "initial"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "-q", str(self.worktree)], check=True)

    def test_integration_repro_sources_main_checkout_env(self):
        (self.repo / ".env.e2e").write_text("ARCHON_REPRO_SENTINEL=loaded\n", encoding="utf-8")
        (self.artifacts / "failing-test.json").write_text(json.dumps({
            "repo": "api",
            "kind": "integration",
            "command": 'test "$ARCHON_REPRO_SENTINEL" = loaded && echo ENV_LOADED',
        }), encoding="utf-8")
        output = self.artifacts / "out.txt"
        result = subprocess.run(
            ["bash", str(SCRIPT), str(self.worktree), str(self.artifacts), str(output)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ENV_LOADED", output.read_text(encoding="utf-8"))

    def test_integration_repro_fails_typed_without_env(self):
        (self.artifacts / "failing-test.json").write_text(json.dumps({
            "repo": "api", "kind": "integration", "command": "true",
        }), encoding="utf-8")
        output = self.artifacts / "out.txt"
        result = subprocess.run(
            ["bash", str(SCRIPT), str(self.worktree), str(self.artifacts), str(output)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 97)
        self.assertIn("REPRO=FAIL missing integration env", result.stdout)


if __name__ == "__main__":
    unittest.main()
