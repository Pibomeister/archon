#!/usr/bin/env python3
"""A lockfile follows its manifest into scope (setup/lockfile_scope.py).

An implementer that adds a dependency changes package.json and the lockfile;
plans list package.json. The lockfile was an ordinary path to the scope scans
(a breach) and an --exclude to the commit nodes (never staged, tree left dirty),
so the stage could not converge. These pin the profile-driven rule at both
readers: check-scope.py (every lane gate) and feature_chain.py (the chain's
candidate check)."""
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

SETUP = Path(__file__).resolve().parents[1]
ARCHON = SETUP.parent
SCRIPT = SETUP / "check-scope.py"
sys.path.insert(0, str(SETUP))
import lockfile_scope  # noqa: E402

LOCKFILE = {"api": "bun.lock", "goodword-mcp": "pnpm-lock.yaml", "web-app": "pnpm-lock.yaml"}


class ProfilesDeclareTheirLockfile(unittest.TestCase):
    def test_each_repo_profile_names_its_package_manager_lockfile(self):
        self.assertEqual(["bun.lock", "bun.lockb"], lockfile_scope.profile("api")["lockfiles"])
        self.assertEqual(["pnpm-lock.yaml"], lockfile_scope.profile("goodword-mcp")["lockfiles"])
        self.assertEqual(["pnpm-lock.yaml"], lockfile_scope.profile("web-app")["lockfiles"])

    def test_only_the_unfrozen_install_declares_lockfile_drift(self):
        # The drift flag must track the install command, or a frozen repo would
        # tolerate lockfile-only drift the install can never produce.
        for repo in ("api", "goodword-mcp", "web-app"):
            prof = lockfile_scope.profile(repo)
            unfrozen = "--no-frozen-lockfile" in prof["install"]
            self.assertEqual(unfrozen, bool(prof["lockfile_install_drift"]), repo)


class CheckScopeLockfileRule(unittest.TestCase):
    def make(self, repo, with_params=True):
        self.wt = Path(tempfile.mkdtemp())
        self.art = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.wt, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.art, ignore_errors=True)
        self.lock = LOCKFILE[repo]
        run = lambda c: subprocess.run(c, cwd=self.wt, shell=True, check=True, capture_output=True)
        run("git init -q && git config user.email t@t && git config user.name t")
        (self.wt / "src").mkdir()
        (self.wt / "src/tool.ts").write_text("export const a = 1;\n")
        (self.wt / "package.json").write_text('{"dependencies": {}}\n')
        (self.wt / self.lock).write_text("lock: 1\n")
        run("git add -A && git commit -qm base")
        self.base = subprocess.run(["git", "-C", str(self.wt), "rev-parse", "HEAD"],
                                   capture_output=True, encoding="utf-8").stdout.strip()
        self.allow = self.art / "files-allowlist.json"
        self.allow.write_text(json.dumps(["src/tool.ts", "package.json"]))
        if with_params:
            (self.art / "params.json").write_text(json.dumps({"repo": repo}))

    def add_dependency(self):
        (self.wt / "src/tool.ts").write_text("export const a = 2;\n")
        (self.wt / "package.json").write_text('{"dependencies": {"zod": "3"}}\n')
        (self.wt / self.lock).write_text("lock: 2\n")

    def scan(self, base=None, *extra):
        return subprocess.run(["python3", str(SCRIPT), str(self.allow), str(self.wt), base or self.base, *extra],
                              capture_output=True, encoding="utf-8")

    def stage(self, *extra):
        return subprocess.run(["python3", str(SCRIPT), str(self.allow), str(self.wt), "HEAD", "--stage",
                               "--quarantine", str(self.art), "--exclude", "pnpm-lock.yaml", *extra],
                              capture_output=True, encoding="utf-8")

    def staged(self):
        out = subprocess.run(["git", "-C", str(self.wt), "diff", "--cached", "--name-only"],
                             capture_output=True, encoding="utf-8").stdout
        return sorted(p for p in out.split() if p)

    def commit(self, msg="c"):
        subprocess.run(["git", "-C", str(self.wt), "commit", "-qm", msg], check=True, capture_output=True)

    def test_dependency_added_with_lockfile_is_clean_and_staged_for_every_repo(self):
        for repo in ("api", "goodword-mcp", "web-app"):
            with self.subTest(repo=repo):
                self.make(repo)
                self.add_dependency()
                r = self.scan()
                self.assertEqual(0, r.returncode, r.stdout + r.stderr)
                r = self.stage()
                self.assertEqual(0, r.returncode, r.stdout + r.stderr)
                self.assertIn(f"COMMIT_SCOPE=LOCKFILE file={self.lock}", r.stdout)
                self.assertEqual(sorted(["package.json", "src/tool.ts", self.lock]), self.staged())
                self.commit()
                self.assertEqual("", subprocess.run(["git", "-C", str(self.wt), "status", "--porcelain"],
                                                    capture_output=True, encoding="utf-8").stdout)
                r = self.scan()
                self.assertEqual(0, r.returncode, "committed lockfile must stay in scope at converge: " + r.stdout)

    def test_lockfile_only_drift_is_a_breach_in_frozen_install_repos(self):
        for repo in ("api", "goodword-mcp"):
            with self.subTest(repo=repo):
                self.make(repo)
                (self.wt / self.lock).write_text("lock: drift\n")
                r = self.scan()
                self.assertEqual(1, r.returncode, r.stdout)
                self.assertIn(f"SCOPE_BREACH file={self.lock} (lockfile changed without an in-scope package.json change)",
                              r.stdout)
                # --exclude pnpm-lock.yaml (every commit node passes it) no longer rescues it.
                r = self.stage()
                self.assertEqual(1, r.returncode, r.stdout)
                self.assertIn(f"COMMIT_SCOPE=STRAY file={self.lock}", r.stdout)
                self.assertEqual([], self.staged())
                self.assertTrue((self.wt / self.lock).exists(), "a lockfile is never quarantined")

    def test_web_app_uncommitted_install_drift_is_tolerated_but_never_staged(self):
        self.make("web-app")
        (self.wt / "src/tool.ts").write_text("export const a = 3;\n")
        (self.wt / self.lock).write_text("lock: install drift\n")
        self.assertEqual(0, self.scan().returncode)
        r = self.stage()
        self.assertEqual(0, r.returncode, r.stdout)
        self.assertEqual(["src/tool.ts"], self.staged())

    def test_web_app_committed_lockfile_only_change_is_a_breach(self):
        self.make("web-app")
        (self.wt / self.lock).write_text("lock: committed\n")
        subprocess.run(["git", "-C", str(self.wt), "add", self.lock], check=True)
        self.commit()
        r = self.scan()
        self.assertEqual(1, r.returncode, r.stdout)
        self.assertIn(f"SCOPE_BREACH file={self.lock}", r.stdout)

    def test_another_package_managers_lockfile_is_an_ordinary_path(self):
        # api's lockfile is bun.lock; a pnpm-lock.yaml there follows no manifest.
        self.make("api")
        self.add_dependency()
        (self.wt / "pnpm-lock.yaml").write_text("stray: 1\n")
        r = self.scan()
        self.assertEqual(1, r.returncode, r.stdout)
        self.assertIn("SCOPE_BREACH file=pnpm-lock.yaml\n", r.stdout)

    def test_negative_control_without_a_known_repo_the_lockfile_is_not_admitted(self):
        # Remove the only input the rule reads: the same dependency add breaches.
        self.make("goodword-mcp", with_params=False)
        self.add_dependency()
        r = self.scan()
        self.assertEqual(1, r.returncode, r.stdout)
        self.assertIn("SCOPE_BREACH file=pnpm-lock.yaml", r.stdout)

    def test_negative_control_manifest_outside_the_allowlist_does_not_admit_the_lockfile(self):
        self.make("goodword-mcp")
        self.allow.write_text(json.dumps(["src/tool.ts"]))
        self.add_dependency()
        r = self.scan()
        self.assertEqual(1, r.returncode, r.stdout)
        self.assertIn("SCOPE_BREACH file=package.json", r.stdout)
        self.assertIn("SCOPE_BREACH file=pnpm-lock.yaml", r.stdout)


class JudgeNegativeControls(unittest.TestCase):
    def test_drift_tolerance_is_the_profile_flag_not_the_repo_name(self):
        prof = dict(lockfile_scope.profile("web-app"))
        self.assertEqual((set(), {"pnpm-lock.yaml"}),
                         lockfile_scope.judge(prof, {"a.ts"}, set(), {"pnpm-lock.yaml"}))
        prof["lockfile_install_drift"] = ""
        self.assertEqual((set(), set()), lockfile_scope.judge(prof, {"a.ts"}, set(), {"pnpm-lock.yaml"}))


class ChainCandidateLockfile(unittest.TestCase):
    """feature_chain.candidate_from_artifacts re-checks the committed diff against
    the approved allowlist; the lockfile staged with package.json must pass."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location("feature_chain_lock", SETUP / "feature_chain.py")
        self.fc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.fc)
        self.wt = Path(tempfile.mkdtemp())
        self.art = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.wt, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.art, ignore_errors=True)
        run = lambda c: subprocess.run(c, cwd=self.wt, shell=True, check=True, capture_output=True)
        run("git init -q && git config user.email t@t && git config user.name t")
        (self.wt / "package.json").write_text("{}\n")
        (self.wt / "pnpm-lock.yaml").write_text("lock: 1\n")
        run("git add -A && git commit -qm base")
        self.base = self.head()
        self.run_ = run

    def head(self):
        return subprocess.run(["git", "-C", str(self.wt), "rev-parse", "HEAD"],
                              capture_output=True, encoding="utf-8").stdout.strip()

    def candidate(self):
        head = self.head()
        (self.art / "verify.log").write_text("Tests: 1 passed\n")
        (self.art / "params.json").write_text(json.dumps({"worktree": str(self.wt)}))
        (self.art / "verify.json").write_text(json.dumps({"test_patterns": ["tool"]}))
        (self.art / "feature-result.json").write_text(json.dumps({
            "outcome": "CHANGED", "head": head,
            "verification_evidence": [{"status": "passed", "log": "verify.log", "tests_passed": 1}]}))
        state = {
            "worktrees": {"goodword-mcp": {"worktree": str(self.wt), "baseline": self.base}},
            "stages": {"goodword-mcp": {"plan": {"files_allowlist": ["package.json"], "test_patterns": ["tool"]}}},
            "approved_plan": {"contracts": []},
        }
        return self.fc.candidate_from_artifacts("goodword-mcp", {"id": "r"}, self.art, state)

    def test_committed_dependency_with_lockfile_is_a_valid_candidate(self):
        (self.wt / "package.json").write_text('{"dependencies": {"zod": "3"}}\n')
        (self.wt / "pnpm-lock.yaml").write_text("lock: 2\n")
        self.run_("git add -A && git commit -qm dep")
        self.assertEqual("goodword-mcp", self.candidate()["repo"])

    def test_negative_control_the_rule_is_what_admits_the_lockfile(self):
        (self.wt / "package.json").write_text('{"dependencies": {"zod": "3"}}\n')
        (self.wt / "pnpm-lock.yaml").write_text("lock: 2\n")
        self.run_("git add -A && git commit -qm dep")
        with mock.patch.object(self.fc.lockfile_scope, "judge", return_value=(set(), set())), \
                self.assertRaisesRegex(self.fc.FeatureChainError, "outside approved allowlist: pnpm-lock.yaml"):
            self.candidate()

    def test_committed_lockfile_only_change_is_outside_the_allowlist(self):
        (self.wt / "pnpm-lock.yaml").write_text("lock: drift\n")
        self.run_("git add -A && git commit -qm drift")
        with self.assertRaisesRegex(self.fc.FeatureChainError, "outside approved allowlist: pnpm-lock.yaml"):
            self.candidate()


class LaneNoChangeDetectionFollowsProfiles(unittest.TestCase):
    """gate-tests/commit-impl decide NO_CHANGE with a hardcoded porcelain filter.
    Treating pnpm-lock.yaml as noise is right only in a lane whose repos declare
    install drift; elsewhere it turns lockfile-only drift into NO_CHANGE and skips
    the scope gate."""

    def content_status_lines(self, lane):
        doc = yaml.safe_load((ARCHON / "workflows" / f"{lane}.yaml").read_text(encoding="utf-8"))
        bodies = [n.get("bash") or "" for n in doc["nodes"] if isinstance(n, dict)]
        return [l for b in bodies for l in b.splitlines() if "CONTENT_STATUS=$(" in l]

    def test_lane_lockfile_noise_matches_the_routed_repo_profiles(self):
        spec = importlib.util.spec_from_file_location("feature_chain_routes", SETUP / "feature_chain.py")
        fc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fc)
        lanes = {}
        for routes in fc.DEFAULT_REPOSITORY_LANES.values():
            for repo, lane in routes.items():
                lanes.setdefault(lane.removesuffix("-codex"), set()).add(repo)
        self.assertEqual({"full-sdlc-api": {"api", "goodword-mcp"}, "full-sdlc-web": {"web-app"}}, lanes)
        for lane, repos in lanes.items():
            lines = self.content_status_lines(lane)
            self.assertGreaterEqual(len(lines), 2, f"{lane}: probe found no CONTENT_STATUS lines")
            drift = any(lockfile_scope.profile(r)["lockfile_install_drift"] for r in repos)
            for line in lines:
                self.assertEqual(drift, "pnpm-lock" in line, f"{lane}: {line.strip()}")


if __name__ == "__main__":
    unittest.main()
