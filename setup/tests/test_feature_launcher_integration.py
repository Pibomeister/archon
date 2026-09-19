#!/usr/bin/env python3
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace


SETUP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SETUP))
spec = importlib.util.spec_from_file_location("feature_chain", SETUP / "feature_chain.py")
assert spec and spec.loader
fc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fc)


def git(repo: Path, *argv: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *argv], capture_output=True, encoding="utf-8")
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class FeatureLauncherIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.control = self.root / "private-control"
        self.codex_home = self.root / "codex-home"
        self.spec = self.root / "specs" / "Bridge Feature.md"
        self.spec.parent.mkdir()
        self.spec.write_text("# Bridge Feature\n", encoding="utf-8")
        self.init_repo("api")
        self.init_repo("goodword-mcp")
        self.host = SimpleNamespace(ROOT=self.root)
        self.host.artifact_dir = lambda row: Path(row["output_root"])
        self.args = Namespace(
            spec=str(self.spec),
            provider="codex",
            control_dir=self.control,
            codex_home=self.codex_home,
            wall_minutes=240,
            max_total_tokens=30_000_000,
            budget_shepherd=False,
            no_watch=True,
        )

    def init_repo(self, name):
        repo = self.root / name
        repo.mkdir(parents=True)
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "test@example.com")
        git(repo, "config", "user.name", "Test")
        (repo / "README.md").write_text(name + "\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "initial")

    @contextmanager
    def chain_env(self, state, *, phase, repo):
        env = {
            "ARCHON_FEATURE_SCOPE": "repositories",
            "ARCHON_FEATURE_CHAIN_ID": state["logical_chain_id"],
            "ARCHON_FEATURE_PROVIDER": "codex",
            "ARCHON_FEATURE_PHASE": phase,
            "ARCHON_FEATURE_REPO": repo,
            "ARCHON_FEATURE_REPOSITORIES": ",".join(state["repositories"]),
            "ARCHON_FEATURE_LANE": "full-sdlc-api-codex",
        }
        previous = {key: os.environ.get(key) for key in env}
        os.environ.update(env)
        try:
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def row(self, run_id, artifacts, lane="full-sdlc-api-codex"):
        artifacts.mkdir(parents=True, exist_ok=True)
        return {
            "id": run_id,
            "workflow_name": lane,
            "user_message": str(self.spec),
            "output_root": str(artifacts),
        }

    def plan(self):
        return {
            "schema": "archon.joint-feature-plan.v1",
            "repositories": ["api", "goodword-mcp"],
            "dependency_order": ["api", "goodword-mcp"],
            "contracts": [{"producer": "api", "consumer": "goodword-mcp", "artifact": "openapi.json", "description": "fixture bridge"}],
            "integration": {"scenarios": [{"name": "api-mcp-local", "uses": ["api", "goodword-mcp"], "commands": [{"repo": "api", "argv": ["fixture"]}], "expected_tests": ["api mcp bridge"]}]},
            "stages": {
                "api": {
                    "depends_on": [],
                    "files_allowlist": ["src/api.ts"],
                    "test_patterns": ["api.spec.ts"],
                    "verification": ["bun run typecheck", "bun run lint", "bun run test -- api.spec.ts"],
                },
                "goodword-mcp": {
                    "depends_on": ["api"],
                    "files_allowlist": ["src/tool.ts"],
                    "test_patterns": ["tool.spec.ts"],
                    "verification": [
                        "mise x node@20 -- pnpm exec tsc --noEmit",
                        "mise x node@20 -- env NODE_OPTIONS=--experimental-vm-modules pnpm exec jest --testPathPatterns tool.spec.ts",
                    ],
                },
            },
        }

    def eval_params(self, params_path: Path) -> dict:
        script = (
            "set -euo pipefail\n"
            "eval \"$(bash \"$1\" \"$2\")\"\n"
            "python3 - \"$SPEC\" \"$SLUG\" \"$BR\" \"$WT\" \"$REPO\" \"$APIPORT\" \"$WEBPORT\" \"$HAS_SMOKE\" \"$ENV_SRC\" \"$HAS_BROWSER\" \"$IMPACT_INDEX\" <<'PY'\n"
            "import json, sys\n"
            "names = ['SPEC','SLUG','BR','WT','REPO','APIPORT','WEBPORT','HAS_SMOKE','ENV_SRC','HAS_BROWSER','IMPACT_INDEX']\n"
            "payload = dict(zip(names, sys.argv[1:]))\n"
            "print(json.dumps(payload, sort_keys=True))\n"
            "PY"
        )
        result = subprocess.run(
            ["bash", "-c", script, "bash", str(SETUP / "params-env.sh"), str(params_path)],
            capture_output=True,
            encoding="utf-8",
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_controller_params_env_and_stage_artifacts_bridge_real_api_and_mcp_repos(self):
        state = fc.make_initial_state(self.host, self.args, ["api", "goodword-mcp"])
        state = fc.write_state(self.control, state)
        fc.budget_init(self.args, state)

        for repo in state["repositories"]:
            expected_slug = "bridge-feature-" + state["logical_chain_id"][:8]
            worktree = Path(state["worktrees"][repo]["worktree"])
            self.assertEqual(self.root / repo / ".worktrees" / expected_slug, worktree)
            self.assertTrue(worktree.is_dir())
            self.assertEqual(state["baselines"]["commits"][repo], git(worktree, "rev-parse", "HEAD"))

        approved = fc.approve_plan(self.control, state["logical_chain_id"], self.plan())
        artifacts = self.root / "artifacts" / "api-stage"
        row = self.row("a" * 32, artifacts)
        with self.chain_env(approved, phase="implement", repo="api"):
            bound = fc.before_dispatch_bind(self.host, self.args, row)

        self.assertIsNotNone(bound)
        latest = fc.read_state(self.control, approved["logical_chain_id"])
        self.assertEqual("api", latest["current_run"]["repo"])
        self.assertEqual([latest["worktrees"]["api"]["worktree"]], latest["current_run"]["write_roots"])

        params = read_json(artifacts / "params.json")
        self.assertEqual("feature-" + approved["logical_chain_id"][:8] + "-implement-api", params["slug"])
        self.assertEqual("api", params["repo"])
        self.assertEqual("api", params["repository"])
        self.assertEqual(["api", "goodword-mcp"], params["repositories"])
        self.assertEqual(["api", "goodword-mcp"], params["repository_scope"])
        self.assertEqual(str(self.spec.resolve()), params["spec"])
        self.assertEqual(approved["worktrees"]["api"]["worktree"], params["worktree"])
        self.assertEqual(approved["worktrees"]["api"]["branch"], params["branch"])
        self.assertEqual(approved["worktrees"]["api"]["baseline"], params["baseline"])
        self.assertEqual({repo: approved["worktrees"][repo]["worktree"] for repo in approved["repositories"]}, params["worktrees_by_repo"])
        self.assertIn("api_port", params)
        self.assertNotIn("web_port", params)

        expanded = self.eval_params(artifacts / "params.json")
        self.assertEqual("api", expanded["REPO"])
        self.assertEqual(params["slug"], expanded["SLUG"])
        self.assertEqual(params["branch"], expanded["BR"])
        self.assertEqual(params["worktree"], expanded["WT"])
        self.assertEqual(str(params["api_port"]), expanded["APIPORT"])
        self.assertEqual("", expanded["WEBPORT"])
        self.assertEqual("1", expanded["HAS_SMOKE"])
        self.assertEqual(".env", expanded["ENV_SRC"])
        self.assertEqual("1", expanded["HAS_BROWSER"])
        self.assertEqual("mono", expanded["IMPACT_INDEX"])

        self.assertEqual({f"{repo}_worktree": approved["worktrees"][repo]["worktree"] for repo in approved["repositories"]},
                         read_json(artifacts / "worktrees.json"))
        self.assertEqual(self.plan(), read_json(artifacts / fc.JOINT_PLAN_ARTIFACT))
        self.assertEqual(["src/api.ts"], read_json(artifacts / "files-allowlist.json"))
        self.assertEqual({"test_patterns": ["api.spec.ts"], "verification": ["bun run typecheck", "bun run lint", "bun run test -- api.spec.ts"]},
                         read_json(artifacts / "verify.json"))
        self.assertEqual(read_json(artifacts / fc.JOINT_PLAN_ARTIFACT), json.loads((artifacts / "plan.md").read_text(encoding="utf-8")))

    def test_claude_dispatch_binds_chain_before_returning_row(self):
        args = Namespace(**dict(vars(self.args), provider="claude", db=self.root / "archon.db"))
        with sqlite3.connect(args.db) as con:
            con.execute("CREATE TABLE remote_agent_workflow_run_node_sessions "
                        "(workflow_run_id TEXT, provider TEXT, node_id TEXT, provider_session_id TEXT)")
            con.execute("CREATE TABLE remote_agent_workflow_events "
                        "(workflow_run_id TEXT, created_at TEXT, event_type TEXT, node_name TEXT, payload TEXT)")
        state = fc.make_initial_state(self.host, args, ["api", "goodword-mcp"])
        state = fc.write_state(self.control, state)
        fc.budget_init(args, state)
        order = []

        def dispatch_feature_phase(host_args, lane, message, env):
            order.append(("dispatch", lane, env["ARCHON_FEATURE_PHASE"], env["ARCHON_FEATURE_PROVIDER"]))
            row = self.row("b" * 32, self.root / "artifacts" / "planning", lane=lane)
            fc.before_dispatch_bind(self.host, host_args, row)
            order.append(("bound", fc.read_state(self.control, state["logical_chain_id"])["current_run"]["run_id"]))
            return row

        self.host.dispatch_feature_phase = dispatch_feature_phase
        dispatched = fc.dispatch_planning(self.host, args, state)

        self.assertEqual(order[0], ("dispatch", "full-sdlc-api", "planning", "claude"))
        self.assertEqual(order[1], ("bound", "b" * 32))
        self.assertEqual(dispatched["row"]["workflow_name"], "full-sdlc-api")
        params = read_json(self.root / "artifacts" / "planning" / "params.json")
        self.assertEqual("planning", params["feature_phase"])
        self.assertEqual(["api", "goodword-mcp"], params["repositories"])
        self.assertTrue((self.root / "artifacts" / "planning" / fc.PLANNING_REQUEST_ARTIFACT).is_file())

    def test_single_repo_mcp_chain_gets_the_smoke_port_its_profile_requires(self):
        # Chain 0712fb0d: a goodword-mcp-only chain got no api_port because the
        # port was keyed on the repo NAME "api", while the lane preflight keys
        # the requirement on the profile's HAS_SMOKE.
        args = Namespace(**dict(vars(self.args), provider="claude", db=self.root / "archon.db"))
        with sqlite3.connect(args.db) as con:
            con.execute("CREATE TABLE remote_agent_workflow_run_node_sessions "
                        "(workflow_run_id TEXT, provider TEXT, node_id TEXT, provider_session_id TEXT)")
            con.execute("CREATE TABLE remote_agent_workflow_events "
                        "(workflow_run_id TEXT, created_at TEXT, event_type TEXT, node_name TEXT, payload TEXT)")
        state = fc.write_state(self.control, fc.make_initial_state(self.host, args, ["goodword-mcp"]))
        fc.budget_init(args, state)

        def dispatch_feature_phase(host_args, lane, message, env):
            row = self.row("c" * 32, self.root / "artifacts" / "mcp-planning", lane=lane)
            fc.before_dispatch_bind(self.host, host_args, row)
            return row

        self.host.dispatch_feature_phase = dispatch_feature_phase
        fc.dispatch_planning(self.host, args, state)
        params_path = self.root / "artifacts" / "mcp-planning" / "params.json"
        params = read_json(params_path)
        self.assertEqual("goodword-mcp", params["repo"])
        self.assertIn("api_port", params)
        expanded = self.eval_params(params_path)
        self.assertEqual("1", expanded["HAS_SMOKE"])
        self.assertEqual(str(params["api_port"]), expanded["APIPORT"])


if __name__ == "__main__":
    unittest.main()
