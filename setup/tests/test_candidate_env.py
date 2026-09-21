#!/usr/bin/env python3
"""candidate_env.py and jest-count.py against real temporary git repositories.

The layout is the one that failed chain 42b42a13: the root clone owns
node_modules and the env files, the chain's source worktree lives under
<clone>/.worktrees/ with neither, and the candidate is a detached worktree
outside the clone where Node's ancestor lookup cannot reach the clone.
"""
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SETUP))
import candidate_env as ce  # noqa: E402
from nodes.extract import runnable_body  # noqa: E402

GIT_ENV = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")


def git(*args):
    return subprocess.run(["git", *args], env=GIT_ENV, check=True, capture_output=True, text=True).stdout.strip()


class Fixture(unittest.TestCase):
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


class CandidateEnv(Fixture):
    def test_resolves_from_the_root_clone_when_the_chain_worktree_has_nothing(self):
        result = ce.prepare("api", self.target, self.source)
        self.assertEqual({"node_modules": f"linked:{self.clone / 'node_modules'}"}, result["deps"])
        self.assertEqual(f"linked:{self.clone / '.env.e2e'}", result["env_files"][".env.e2e"])
        self.assertEqual("root", (self.target / "node_modules" / "marker").read_text())
        self.assertEqual("E2E=root\n", (self.target / ".env.e2e").read_text())
        self.assertEqual("", git("-C", str(self.target), "status", "--porcelain"))

    def test_the_source_worktree_wins_deps_but_clone_wins_env(self):
        (self.source / "node_modules").mkdir()
        (self.source / "node_modules" / "marker").write_text("stage")
        (self.source / ".env.e2e").write_text("E2E=stage\n")
        result = ce.prepare("api", self.target, self.source)
        self.assertEqual(f"linked:{self.source / 'node_modules'}", result["deps"]["node_modules"])
        self.assertEqual("E2E=root\n", (self.target / ".env.e2e").read_text())

    def test_present_dep_is_left_alone_but_stale_env_is_replaced(self):
        (self.target / "node_modules").mkdir()
        (self.target / ".env").write_text("ENV=stale\n")
        result = ce.prepare("api", self.target, self.source)
        self.assertEqual("present", result["deps"]["node_modules"])
        self.assertFalse((self.target / "node_modules").is_symlink())
        self.assertEqual("ENV=root\n", (self.target / ".env").read_text())

    def test_missing_env_file_fails_naming_every_place_searched(self):
        (self.clone / ".env.e2e").unlink()
        with self.assertRaises(ce.CandidateEnvError) as ctx:
            ce.prepare("api", self.target, self.source)
        message = str(ctx.exception)
        self.assertIn("repo=api .env.e2e: not found in", message)
        self.assertIn(str(self.source), message)
        self.assertIn(str(self.clone), message)

    def test_a_dangling_link_in_the_target_is_a_typed_failure(self):
        (self.target / ".env.e2e").symlink_to(self.root / "moved-away")
        with self.assertRaisesRegex(ce.CandidateEnvError, r"repo=api \.env\.e2e: .* is a dangling symlink"):
            ce.prepare("api", self.target, self.source)

    def test_relative_paths_still_produce_resolving_links(self):
        (self.source / ".env.e2e").write_text("E2E=stage\n")
        cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, cwd)
        ce.prepare("api", self.target.relative_to(self.root), self.source.relative_to(self.root))
        self.assertEqual("E2E=root\n", (self.target / ".env.e2e").read_text())

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


class StageBootstrap(Fixture):
    """full-sdlc-api bootstrap: a chain stage worktree gets the profile's runtime
    files too. Runs the SHIPPED block (extracted, not transcribed)."""

    def block(self):
        body = runnable_body("full-sdlc-api", "bootstrap")
        match = re.search(r'^( *)if \[ "\$\{ARCHON_FEATURE_SCOPE-\}" = repositories \]; then\n(?:.*\n)*?\1fi\n', body, re.M)
        blocks = [m for m in [match] if m and "candidate_env.py" in m.group(0)]
        self.assertEqual(1, len(blocks), "stage runtime-env block not found in bootstrap")
        preamble = []
        for line in body.splitlines():
            if line.startswith("export ARCHON_LAYER=") or line.startswith("export PROJECT_ROOT="):
                preamble.append(line)
            elif preamble:
                break
        return "set -euo pipefail\n" + ("\n".join(preamble) + "\n" if preamble else "") + blocks[0].group(0)

    def run_block(self, scope):
        env = dict(os.environ, REPO="api", WT=str(self.source))
        env.pop("ARCHON_FEATURE_SCOPE", None)
        if scope:
            env["ARCHON_FEATURE_SCOPE"] = scope
        return subprocess.run(["bash", "-c", self.block()], capture_output=True, text=True, env=env)

    def test_repository_stage_links_the_e2e_env_file(self):
        result = self.run_block("repositories")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("E2E=root\n", (self.source / ".env.e2e").read_text())

    def test_unresolvable_stage_environment_fails_bootstrap(self):
        (self.clone / ".env.e2e").unlink()
        result = self.run_block("repositories")
        self.assertEqual(1, result.returncode)
        self.assertIn("CANDIDATE_ENV=FAIL repo=api .env.e2e", result.stdout)
        self.assertIn("BOOTSTRAP=FAIL stage runtime environment unresolved", result.stdout)

    def test_legacy_runs_are_untouched(self):
        result = self.run_block(None)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertFalse((self.source / ".env.e2e").exists())


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

    def test_a_suite_that_failed_to_load_cannot_pass(self):
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / "fake-jest.py"
            fake.write_text(
                "import json, sys\n"
                "out = sys.argv[sys.argv.index('--outputFile') + 1]\n"
                "json.dump({'numPassedTests': 5, 'numFailedTests': 0, 'numRuntimeErrorTestSuites': 1,"
                " 'success': False}, open(out, 'w'))\n"
            )
            result = subprocess.run([sys.executable, str(SETUP / "jest-count.py"), sys.executable, str(fake)],
                                    capture_output=True, text=True)
        self.assertEqual(1, result.returncode)
        self.assertIn("JEST_COUNT=FAIL passed=5", result.stdout)

    def test_zero_passed_cannot_pass(self):
        self.assertEqual(1, self.run_count(0, 0).returncode)

    def test_command_exit_code_is_preserved(self):
        result = self.run_count(2, 0, rc=7)
        self.assertEqual(7, result.returncode)
        self.assertIn("ARCHON_INTEGRATION_TESTS=2", result.stdout)


if __name__ == "__main__":
    unittest.main()
