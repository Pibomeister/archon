#!/usr/bin/env python3
"""A plain `archon workflow resume` does not carry the launcher's
ARCHON_FEATURE_* env (observed 2026-09-13, run ffd88016). Every gate keyed on
that env then read "not a repository run" and took the wrong branch, silently.
trusted-local-candidate.sh:8-13 fixed that for itself by reading params.json;
setup/feature_env.py is the same fallback for the other readers.

Each reader gets a pair: env unset with params.json present takes the
repository branch, and the negative control removes params.json so the reader
falls back to the behaviour it had before this existed. Two readers cannot
import feature_env.py because archon-run.py installs them as standalone private
copies outside the workspace, so their inlined key maps are pinned to
feature_env.KEYS here.

plan-shape.sh is deliberately absent: its only ARCHON_FEATURE_SCOPE read sits in
the `elif` reached exclusively when params.json is ABSENT, so no params.json
fallback can reach it. test_plan_shape_read_is_unreachable_by_fallback pins that
so the reader is not "fixed" with a line that cannot run.
"""
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parents[1]
ARCHON = SETUP.parent
sys.path.insert(0, str(SETUP))
import feature_env  # noqa: E402

GIT_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
CHAIN = "b" * 32
RUN_ID = "a" * 32

REPOSITORY_PARAMS = {
    "schema_version": 2,
    "feature_scope": "repositories",
    "feature_phase": "implement",
    "logical_chain_id": CHAIN,
    "run_id": RUN_ID,
    "repo": "api",
    "repositories": ["api", "goodword-mcp"],
    "slug": "feature-bbbbbbbb-implement-api",
}


def clean_env(**overrides):
    """The launcher's chain env, gone -- which is exactly the resume case."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("ARCHON_FEATURE_")}
    env.update(GIT_ENV)
    env.update(overrides)
    return env


class FeatureEnvHelperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.artifacts = Path(self.tmp.name)
        (self.artifacts / "params.json").write_text(json.dumps(REPOSITORY_PARAMS))

    def test_params_json_supplies_every_key(self):
        self.assertEqual(
            feature_env.feature_env_all(self.artifacts),
            {
                "ARCHON_FEATURE_SCOPE": "repositories",
                "ARCHON_FEATURE_PHASE": "implement",
                "ARCHON_FEATURE_CHAIN_ID": CHAIN,
                "ARCHON_FEATURE_RUN_ID": RUN_ID,
            },
        )

    def test_env_wins_over_params_json(self):
        os.environ["ARCHON_FEATURE_PHASE"] = "planning"
        self.addCleanup(os.environ.pop, "ARCHON_FEATURE_PHASE", None)
        self.assertEqual(feature_env.feature_env("ARCHON_FEATURE_PHASE", artifacts=self.artifacts), "planning")

    def test_absent_params_json_returns_the_caller_default(self):
        (self.artifacts / "params.json").unlink()
        self.assertIsNone(feature_env.feature_env("ARCHON_FEATURE_SCOPE", artifacts=self.artifacts))
        self.assertEqual(feature_env.feature_env_all(self.artifacts), {})

    def test_malformed_params_json_is_not_a_traceback(self):
        (self.artifacts / "params.json").write_text("{not json")
        self.assertIsNone(feature_env.feature_env("ARCHON_FEATURE_SCOPE", artifacts=self.artifacts))

    def test_shell_form_emits_quoted_exports(self):
        result = subprocess.run(
            ["python3", str(SETUP / "feature_env.py"), str(self.artifacts)],
            capture_output=True, text=True, env=clean_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("export ARCHON_FEATURE_SCOPE=repositories", result.stdout)
        self.assertIn(f"export ARCHON_FEATURE_CHAIN_ID={CHAIN}", result.stdout)

    def test_sourcing_feature_env_sh_exports_into_the_caller(self):
        script = (
            f'. "{SETUP}/feature-env.sh" "{self.artifacts}"\n'
            'echo "SCOPE=${ARCHON_FEATURE_SCOPE-}"\n'
        )
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=clean_env())
        self.assertEqual(result.stdout.strip(), "SCOPE=repositories", result.stderr)

    def test_sourcing_without_params_json_leaves_the_env_unset(self):
        (self.artifacts / "params.json").unlink()
        script = (
            'set -u\n'
            f'. "{SETUP}/feature-env.sh" "{self.artifacts}"\n'
            'echo "SCOPE=${ARCHON_FEATURE_SCOPE-}"\n'
        )
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=clean_env())
        self.assertEqual(result.stdout.strip(), "SCOPE=", result.stderr)


class PrivateCopyKeyMapTest(unittest.TestCase):
    """archon-run.py copies these two out of the workspace, so they carry their
    own key map. A copy that drifts reads the wrong params.json field and the
    guard silently stops guarding."""

    def test_spawn_guard_key_map_matches_feature_env(self):
        text = (SETUP / "codex-spawn-guard.py").read_text()
        match = re.search(r"PARAMS_KEYS = (\{[^}]*\})", text)
        self.assertIsNotNone(match, "codex-spawn-guard.py lost its inline key map")
        keys = eval(match.group(1))  # noqa: S307 - a literal dict from our own file
        self.assertTrue(keys)
        for name, key in keys.items():
            self.assertEqual(feature_env.KEYS[name], key)

    def test_wrapper_key_map_matches_feature_env(self):
        text = (SETUP / "codex-workspace-wrapper.sh").read_text()
        match = re.search(r"PARAMS_KEYS = (\{.*?\})", text, re.S)
        self.assertIsNotNone(match, "codex-workspace-wrapper.sh lost its inline key map")
        self.assertEqual(eval(match.group(1)), feature_env.KEYS)  # noqa: S307


class CheckScopeFallbackTest(unittest.TestCase):
    """check-scope.py --stage --quarantine refuses to auto-expand the allowlist
    on a repository-list stage. Reading the scope from params.json is what makes
    that refusal survive a resume."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.worktree = self.root / "wt"
        self.worktree.mkdir()
        (self.worktree / "src").mkdir()
        self.env = clean_env()
        for cmd in (["init", "-q"], ["config", "user.name", "Fixture"],
                    ["config", "user.email", "fixture@example.com"]):
            subprocess.run(["git", "-C", str(self.worktree), *cmd], env=self.env, check=True)
        (self.worktree / "src" / "commit-import.service.ts").write_text("export const a = 1;\n")
        subprocess.run(["git", "-C", str(self.worktree), "add", "-A"], env=self.env, check=True)
        subprocess.run(["git", "-C", str(self.worktree), "commit", "-qm", "base"], env=self.env, check=True)
        (self.artifacts / "files-allowlist.json").write_text(
            json.dumps(["src/commit-import.service.ts"]))
        # The stray a repo rule mandates: a new sibling sharing the stem.
        (self.worktree / "src" / "commit-import.util.ts").write_text("export const b = 2;\n")

    def run_guard(self):
        return subprocess.run(
            ["python3", str(SETUP / "check-scope.py"),
             str(self.artifacts / "files-allowlist.json"), str(self.worktree), "HEAD",
             "--stage", "--quarantine", str(self.artifacts)],
            capture_output=True, text=True, env=self.env)

    def test_params_json_makes_the_repository_refusal_survive_a_resume(self):
        (self.artifacts / "params.json").write_text(json.dumps(REPOSITORY_PARAMS))
        result = self.run_guard()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("COMMIT_SCOPE=FAIL nothing staged (repository-list stages require",
                      result.stdout)

    def test_negative_control_without_params_json_it_adopts_as_before(self):
        result = self.run_guard()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("COMMIT_SCOPE=ADOPTED file=src/commit-import.util.ts", result.stdout)


class ResolveParamsFallbackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.spec = self.root / "eng-0000-fixture.md"
        self.spec.write_text("# Spec\n")

    def run_resolve(self, root=None):
        return subprocess.run(
            ["bash", str(SETUP / "resolve-params.sh"), str(root or ARCHON.parent),
             str(self.spec), str(self.artifacts)],
            capture_output=True, text=True,
            env=clean_env(ARCHON_PARAMS_WAIT_SECONDS="0"))

    def test_params_json_restores_the_repository_early_return(self):
        (self.artifacts / "params.json").write_text(json.dumps(REPOSITORY_PARAMS))
        result = self.run_resolve()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PARAMS=OK adopted repository feature params phase=implement", result.stdout)

    def test_negative_control_without_params_json_it_derives_a_single_repo_run(self):
        # Pre-existing behaviour with neither env nor params.json: a fresh api
        # binding, not a typed stop. Recorded so the fallback's effect is
        # visible as a difference and not as the only observed outcome.
        result = self.run_resolve()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PARAMS=OK", result.stdout)
        self.assertIn("repo=api (new)", result.stdout)
        self.assertNotIn("repository feature params", result.stdout)


class SpawnGuardFallbackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.artifacts = Path(self.tmp.name)
        self.payload = json.dumps({
            "hook_event_name": "PreToolUse",
            "tool_name": "spawn_agent",
            "tool_input": {"model": "gpt-5.6-sol", "reasoning_effort": "high"},
        })

    def run_guard(self):
        return subprocess.run(
            ["python3", str(SETUP / "codex-spawn-guard.py")],
            input=self.payload, capture_output=True, text=True,
            env=clean_env(CODEX_RUN_ARTIFACTS=str(self.artifacts),
                          ARCHON_CODEX_PINNED_MODEL="gpt-5.6-sol",
                          ARCHON_CODEX_PINNED_REASONING_EFFORT="medium"))

    def test_params_json_keeps_the_guard_armed_after_a_resume(self):
        (self.artifacts / "params.json").write_text(json.dumps(REPOSITORY_PARAMS))
        result = self.run_guard()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"decision":"block"', result.stdout)
        self.assertIn("reasoning_effort=medium", result.stdout)

    def test_negative_control_without_params_json_the_guard_stands_down(self):
        result = self.run_guard()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")


class JointIntegrationFallbackTest(unittest.TestCase):
    """run-joint-integration.py hands ARCHON_FEATURE_* to every approved
    command; without the chain id those commands run unregistered with the
    feature budget, so a runaway cannot be terminated by chain."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = clean_env()
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.commits = {name: self.init_repo(name) for name in ("api", "goodword-mcp")}
        self.write_plan()
        self.write_candidates()

    def init_repo(self, name):
        repo = self.root / name
        repo.mkdir()
        for cmd in (["init", "-q"], ["config", "user.name", "Fixture"],
                    ["config", "user.email", "fixture@example.com"]):
            subprocess.run(["git", "-C", str(repo), *cmd], env=self.env, check=True)
        (repo / "candidate.txt").write_text(name + "\n")
        subprocess.run(["git", "-C", str(repo), "add", "candidate.txt"], env=self.env, check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "candidate"], env=self.env, check=True)
        return subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], env=self.env, text=True).strip()

    def write_plan(self):
        plan = {
            "schema": "archon.joint-feature-plan.v1",
            "repositories": ["api", "goodword-mcp"],
            "dependency_order": ["api", "goodword-mcp"],
            "stages": {
                "api": {"depends_on": [], "files_allowlist": ["candidate.txt"],
                        "test_patterns": ["candidate"], "verification": ["fixture"]},
                "goodword-mcp": {"depends_on": ["api"], "files_allowlist": ["candidate.txt"],
                                 "test_patterns": ["candidate"], "verification": ["fixture"]},
            },
            "integration": {"scenarios": [{
                "name": "fixture",
                "uses": ["api", "goodword-mcp"],
                "commands": ['echo "CHAIN=${ARCHON_FEATURE_CHAIN_ID-unset}"; '
                             'echo "ARCHON_INTEGRATION_TESTS=1"'],
                "expected_tests": ["fixture contract"],
            }]},
        }
        self.plan_digest = hashlib.sha256(
            json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        (self.artifacts / "joint-plan.json").write_text(json.dumps(plan, indent=2, sort_keys=True))

    def write_candidates(self):
        (self.artifacts / "candidate-revisions.json").write_text(json.dumps({
            "schema": "archon.joint-candidates.v1",
            "plan_digest": self.plan_digest,
            "approved_plan_digest": self.plan_digest,
            "candidate_heads": dict(self.commits),
            "repositories": {
                name: {"source_worktree": str(self.root / name), "commit": commit}
                for name, commit in self.commits.items()
            },
        }, indent=2, sort_keys=True))

    def run_runner(self):
        result = subprocess.run(
            ["python3", str(SETUP / "run-joint-integration.py"), "--artifacts", str(self.artifacts)],
            capture_output=True, text=True, env=self.env)
        log = self.artifacts / "joint-integration-1.log"
        return result, log.read_text() if log.exists() else ""

    def test_params_json_restores_the_chain_id_for_approved_commands(self):
        (self.artifacts / "params.json").write_text(json.dumps(REPOSITORY_PARAMS))
        result, log = self.run_runner()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"CHAIN={CHAIN}", log)

    def test_negative_control_without_params_json_the_chain_id_is_unset(self):
        result, log = self.run_runner()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("CHAIN=unset", log)


class CodexWrapperFallbackTest(unittest.TestCase):
    """The wrapper refuses native multi-agent inside a guarded repository chain.
    That refusal is keyed on ARCHON_FEATURE_SCOPE, which a plain resume drops."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.artifacts = self.root / "artifacts" / RUN_ID
        self.artifacts.mkdir(parents=True)
        self.real = self.root / "fake-codex"
        self.real.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        self.real.chmod(0o700)

    def invoke(self):
        return subprocess.run(
            ["bash", str(SETUP / "codex-workspace-wrapper.sh"), "exec",
             "--enable", "multi_agent"],
            input=f"Write artifacts to {self.artifacts}", text=True, capture_output=True,
            cwd=str(self.workspace),
            env=clean_env(CODEX_REAL_BIN=str(self.real),
                          CODEX_WORKSPACE_ROOT=str(self.workspace),
                          CODEX_ARTIFACTS_BASE=str(self.artifacts.parent)))

    def test_params_json_keeps_the_multi_agent_refusal_after_a_resume(self):
        (self.artifacts / "params.json").write_text(json.dumps(REPOSITORY_PARAMS))
        result = self.invoke()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("native multi-agent cannot be enabled inside a guarded repository chain",
                      result.stderr)

    def test_negative_control_without_params_json_the_refusal_does_not_fire(self):
        result = self.invoke()
        self.assertNotIn("native multi-agent cannot be enabled", result.stderr)


class PlanShapeUnreachableReadTest(unittest.TestCase):
    def test_plan_shape_read_is_unreachable_by_fallback(self):
        text = (SETUP / "plan-shape.sh").read_text()
        match = re.search(
            r'if \[ -f "\$AD/params\.json" \]; then.*?elif \[ "\$\{ARCHON_FEATURE_SCOPE-\}" = repositories \]',
            text, re.S)
        self.assertIsNotNone(
            match,
            "plan-shape.sh's scope read moved; if it is now reachable with params.json "
            "present, source setup/feature-env.sh there and delete this test")


class SetupScriptsDiscoverableTest(unittest.TestCase):
    def test_check_scope_imports_feature_env_from_its_own_directory(self):
        # check-scope.py is always invoked as an absolute script path, which puts
        # setup/ on sys.path[0]. Pin that: an invocation from elsewhere must work.
        spec = importlib.util.spec_from_file_location("check_scope_probe", SETUP / "check-scope.py")
        self.assertIsNotNone(spec)
        result = subprocess.run(
            ["python3", str(SETUP / "check-scope.py")],
            capture_output=True, text=True, cwd="/", env=clean_env())
        self.assertNotIn("ModuleNotFoundError", result.stderr)


if __name__ == "__main__":
    unittest.main()
