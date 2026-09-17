#!/usr/bin/env python3
"""candidate_env.py and jest-count.py against real temporary git repositories.

The layout is the one that failed chain 42b42a13: the root clone owns
node_modules and the env files, the chain's source worktree lives under
<clone>/.worktrees/ with neither, and the candidate is a detached worktree
outside the clone where Node's ancestor lookup cannot reach the clone.
"""
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SETUP))
import candidate_env as ce  # noqa: E402

GIT_ENV = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")


def git(*args):
    return subprocess.run(["git", *args], env=GIT_ENV, check=True, capture_output=True, text=True).stdout.strip()


class CandidateEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.clone = self.root / "api"
        self.clone.mkdir()
        git("-C", str(self.clone), "init", "-q")
        git("-C", str(self.clone), "config", "user.email", "f@example.com")
        git("-C", str(self.clone), "config", "user.name", "F")
        (self.clone / ".gitignore").write_text("node_modules\n.env\n.env.*\n.worktrees\n")
        (self.clone / "package.json").write_text('{"name":"api"}\n')
        (self.clone / "bun.lock").write_text("lock-v1\n")
        git("-C", str(self.clone), "add", ".")
        git("-C", str(self.clone), "commit", "-qm", "base")
        (self.clone / "node_modules").mkdir()
        (self.clone / "node_modules" / "marker").write_text("root")
        (self.clone / ".env").write_text("ENV=root\n")
        (self.clone / ".env.e2e").write_text("E2E=root\n")
        self.source = self.clone / ".worktrees" / "chain"
        git("-C", str(self.clone), "worktree", "add", "-q", "-b", "stage", str(self.source))
        self.target = self.root / "artifacts" / "joint-integration-worktrees" / "api"
        self.target.parent.mkdir(parents=True)
        git("-C", str(self.source), "worktree", "add", "-q", "--detach", str(self.target), "HEAD")

    def shim_bun(self, script):
        bin_dir = self.root / "bin"
        bin_dir.mkdir(exist_ok=True)
        shim = bin_dir / "bun"
        shim.write_text("#!/bin/sh\n" + script)
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
        path = os.environ["PATH"]
        os.environ["PATH"] = f"{bin_dir}:{path}"
        self.addCleanup(os.environ.__setitem__, "PATH", path)

    def test_resolves_from_the_root_clone_when_the_chain_worktree_has_nothing(self):
        result = ce.prepare("api", self.target, self.source)
        self.assertEqual({"node_modules": f"linked:{self.clone / 'node_modules'}"}, result["deps"])
        self.assertEqual(f"linked:{self.clone / '.env.e2e'}", result["env_files"][".env.e2e"])
        self.assertEqual("root", (self.target / "node_modules" / "marker").read_text())
        self.assertEqual("E2E=root\n", (self.target / ".env.e2e").read_text())
        self.assertEqual("", git("-C", str(self.target), "status", "--porcelain"))

    def test_the_source_worktree_wins_over_the_clone(self):
        (self.source / "node_modules").mkdir()
        (self.source / ".env.e2e").write_text("E2E=stage\n")
        result = ce.prepare("api", self.target, self.source)
        self.assertEqual(f"linked:{self.source / 'node_modules'}", result["deps"]["node_modules"])
        self.assertEqual("E2E=stage\n", (self.target / ".env.e2e").read_text())

    def test_present_entries_are_left_alone(self):
        (self.target / "node_modules").mkdir()
        (self.target / ".env").write_text("ENV=mine\n")
        result = ce.prepare("api", self.target, self.source)
        self.assertEqual("present", result["deps"]["node_modules"])
        self.assertEqual("present", result["env_files"][".env"])
        self.assertFalse((self.target / "node_modules").is_symlink())

    def test_missing_env_file_fails_naming_every_place_searched(self):
        (self.clone / ".env.e2e").unlink()
        with self.assertRaises(ce.CandidateEnvError) as ctx:
            ce.prepare("api", self.target, self.source)
        message = str(ctx.exception)
        self.assertIn("repo=api .env.e2e: not found in", message)
        self.assertIn(str(self.source), message)
        self.assertIn(str(self.clone), message)

    def test_changed_lockfile_installs_instead_of_linking_stale_dependencies(self):
        (self.target / "bun.lock").write_text("lock-v2\n")
        self.shim_bun('mkdir node_modules && echo "$@" > node_modules/installed-with\n')
        result = ce.prepare("api", self.target, self.source)
        self.assertEqual("installed", result["deps"]["node_modules"])
        self.assertFalse((self.target / "node_modules").is_symlink())
        self.assertEqual("install --frozen-lockfile\n", (self.target / "node_modules" / "installed-with").read_text())

    def test_matching_manifests_never_run_the_install(self):
        self.shim_bun("touch install-ran; exit 1\n")
        ce.prepare("api", self.target, self.source)
        self.assertFalse((self.target / "install-ran").exists())
        self.assertTrue((self.target / "node_modules").is_symlink())

    def test_failed_install_is_a_typed_failure(self):
        (self.target / "package.json").write_text('{"name":"api","dependencies":{"x":"1"}}\n')
        self.shim_bun('echo "lockfile had changes" >&2; exit 3\n')
        with self.assertRaises(ce.CandidateEnvError) as ctx:
            ce.prepare("api", self.target, self.source)
        self.assertIn("rc=3", str(ctx.exception))
        self.assertIn("lockfile had changes", str(ctx.exception))

    def test_cli_prints_typed_lines(self):
        ok = subprocess.run([sys.executable, str(SETUP / "candidate_env.py"), "api", str(self.target), str(self.source)],
                            capture_output=True, text=True)
        self.assertEqual(0, ok.returncode, ok.stdout + ok.stderr)
        self.assertTrue(ok.stdout.startswith("CANDIDATE_ENV=PASS repo=api "))
        bad = subprocess.run([sys.executable, str(SETUP / "candidate_env.py"), "nope", str(self.target), str(self.source)],
                             capture_output=True, text=True)
        self.assertEqual(1, bad.returncode)
        self.assertIn("CANDIDATE_ENV=FAIL unknown repo nope", bad.stdout)


class JestCount(unittest.TestCase):
    def run_count(self, passed, failed, rc=0):
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / "fake-jest.py"
            fake.write_text(
                "import json, sys\n"
                "out = sys.argv[sys.argv.index('--outputFile') + 1]\n"
                f"json.dump({{'numPassedTests': {passed}, 'numFailedTests': {failed}}}, open(out, 'w'))\n"
                f"sys.exit({rc})\n"
            )
            return subprocess.run([sys.executable, str(SETUP / "jest-count.py"), sys.executable, str(fake)],
                                  capture_output=True, text=True)

    def test_prints_the_passed_case_count(self):
        result = self.run_count(4, 0)
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn("ARCHON_INTEGRATION_TESTS=4", result.stdout)

    def test_a_failed_case_fails_even_when_jest_exits_zero(self):
        result = self.run_count(3, 1)
        self.assertEqual(1, result.returncode)
        self.assertIn("JEST_COUNT=FAIL passed=3 failed=1", result.stdout)

    def test_zero_passed_cannot_pass(self):
        self.assertEqual(1, self.run_count(0, 0).returncode)

    def test_command_exit_code_is_preserved(self):
        result = self.run_count(2, 0, rc=7)
        self.assertEqual(7, result.returncode)
        self.assertIn("ARCHON_INTEGRATION_TESTS=2", result.stdout)


if __name__ == "__main__":
    unittest.main()
