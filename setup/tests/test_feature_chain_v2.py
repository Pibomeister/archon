#!/usr/bin/env python3
import contextlib
import datetime
import importlib.util
import io
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SETUP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SETUP))
spec = importlib.util.spec_from_file_location("feature_chain", SETUP / "feature_chain.py")
assert spec and spec.loader
fc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fc)


class RecordingStream(io.StringIO):
    def __init__(self):
        super().__init__()
        self.flushes = []

    def flush(self):
        self.flushes.append(self.getvalue())
        super().flush()


def git(repo: Path, *argv: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *argv], capture_output=True, encoding="utf-8")
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


class FeatureChainV2(unittest.TestCase):
    def setUp(self):
        env_patch = mock.patch.dict(fc.os.environ, {}, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.control = self.root / "private-control"
        self.spec = self.root / "feature.md"
        self.spec.write_text("# Feature\n", encoding="utf-8")
        for repo in ("api", "goodword-mcp", "web-app"):
            self.init_repo(repo)
        self.host = SimpleNamespace(ROOT=self.root)
        self.host.calls = []
        def invoke(args, lane, message):
            run_id = f"{len(self.host.calls) + 1:032x}"
            row = {
                "id": run_id,
                "workflow_name": lane,
                "user_message": str(message),
                "output_root": str(self.root / "artifacts" / run_id),
            }
            Path(row["output_root"]).mkdir(parents=True)
            if fc.os.environ.get("ARCHON_FEATURE_SCOPE") == "repositories":
                fc.before_dispatch_bind(self.host, args, row)
            self.host.calls.append((lane, str(message), dict(fc.os.environ), row))
            return row
        self.host.invoke_codex_lane = invoke
        self.host.artifact_dir = lambda row: Path(row["output_root"])
        self.args = Namespace(
            spec=str(self.spec),
            provider="codex",
            control_dir=self.control,
            db=self.root / "archon.db",
            codex_home=self.root / "codex-home",
            wall_minutes=240,
            max_total_tokens=30_000_000,
            budget_shepherd=False,
            no_watch=True,
        )
        with sqlite3.connect(self.args.db) as con:
            con.execute(
                "CREATE TABLE remote_agent_workflow_run_node_sessions "
                "(workflow_run_id TEXT, provider TEXT, node_id TEXT, provider_session_id TEXT)"
            )
            con.execute(
                "CREATE TABLE remote_agent_workflow_events "
                "(workflow_run_id TEXT, created_at TEXT, event_type TEXT, node_name TEXT, payload TEXT)"
            )

    def init_repo(self, name):
        repo = self.root / name
        repo.mkdir(parents=True)
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "test@example.com")
        git(repo, "config", "user.name", "Test")
        git(repo, "remote", "add", "origin", f"git@github.com:Owner/{name}.git")
        (repo / "README.md").write_text(name + "\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "initial")

    def plan(self, deps=None):
        deps = deps or {"api": [], "goodword-mcp": ["api"]}
        return {
            "schema": "archon.joint-feature-plan.v1",
            "repositories": ["api", "goodword-mcp"],
            "dependency_order": ["api", "goodword-mcp"] if not deps["api"] else ["goodword-mcp", "api"],
            "contracts": [{"producer": "api", "consumer": "goodword-mcp", "artifact": "openapi.json", "description": "fixture"}],
            "integration": {"scenarios": [{"name": "local-api-mcp", "uses": ["api", "goodword-mcp"], "commands": [{"repo": "api", "argv": ["fixture"]}], "expected_tests": ["local api mcp"]}]},
            "stages": {
                "api": {
                    "depends_on": deps["api"],
                    "files_allowlist": ["src/api.ts"],
                    "test_patterns": ["api.spec.ts"],
                    "verification": ["bun test"],
                },
                "goodword-mcp": {
                    "depends_on": deps["goodword-mcp"],
                    "files_allowlist": ["src/tool.ts"],
                    "test_patterns": ["tool.spec.ts"],
                    "verification": ["pnpm test"],
                },
            },
        }

    def row(self, run_id, lane="full-sdlc-api-codex"):
        return {"id": run_id, "workflow_name": lane, "user_message": str(self.spec), "output_root": str(self.root / f"out-{run_id[:4]}")}

    def result_artifacts(self, row, repo, head=None):
        ad = Path(row["output_root"])
        ad.mkdir(parents=True)
        payload = {
            "outcome": "CHANGED",
            "head": head or git(self.root / repo, "rev-parse", "HEAD"),
            "verification_evidence": [{"command": "unit", "status": "passed", "log": "verify.log", "tests_passed": 1}],
            "interface_artifacts": [{"path": "contract.json", "source_path": "openapi.json", "sha256": "ca3d163bab055381827226140568f3bef7eaac187cebd76878e0b63e9e442356"}],
        }
        (ad / "verify.log").write_text("Tests: 1 passed\n", encoding="utf-8")
        (ad / "contract.json").write_text("{}\n", encoding="utf-8")
        worktree = next((self.root / repo / ".worktrees").iterdir())
        (ad / "params.json").write_text(json.dumps({"worktree": str(worktree)}), encoding="utf-8")
        (ad / "verify.json").write_text(json.dumps({"test_patterns": self.plan()["stages"][repo]["test_patterns"]}), encoding="utf-8")
        (ad / "feature-result.json").write_text(json.dumps(payload), encoding="utf-8")
        return ad

    def test_launch_prepares_repository_keyed_state_and_private_worktrees(self):
        launched = fc.launch(self.host, self.args, ["api", "goodword-mcp"])
        state = fc.read_state(self.control, launched["state"]["logical_chain_id"])

        self.assertEqual(state["schema_version"], 2)
        self.assertEqual(state["scope"], "repositories")
        self.assertEqual(state["executable_plan_contract"], 1)
        self.assertEqual(state["repositories"], ["api", "goodword-mcp"])
        self.assertEqual(self.host.calls[0][0], "full-sdlc-api-codex")
        self.assertEqual(state["current_run"]["phase"], "planning")
        self.assertTrue((Path(state["current_run"]["artifacts_dir"]) / fc.PLANNING_REQUEST_ARTIFACT).is_file())
        for repo in state["repositories"]:
            self.assertTrue(Path(state["worktrees"][repo]["worktree"]).is_dir())
            self.assertEqual(state["stages"][repo]["status"], "pending")

    def test_approval_binds_exact_plan_and_topological_order(self):
        state = fc.launch(self.host, self.args, ["api", "goodword-mcp"])["state"]
        approved = fc.approve_plan(self.control, state["logical_chain_id"], self.plan())

        self.assertEqual(approved["dependency_order"], ["api", "goodword-mcp"])
        self.assertEqual(fc.next_stage(approved), "api")
        approved["approved_plan"]["contracts"][0]["artifact"] = "other.json"
        with self.assertRaisesRegex(fc.FeatureChainError, "approval"):
            fc.verify_approval(approved)

    def test_plan_rejects_cycles_and_outside_scope_dependencies(self):
        state = fc.launch(self.host, self.args, ["api", "goodword-mcp"])["state"]
        with self.assertRaisesRegex(fc.FeatureChainError, "cycle"):
            fc.approve_plan(self.control, state["logical_chain_id"], self.plan({"api": ["goodword-mcp"], "goodword-mcp": ["api"]}))
        outside = self.plan({"api": [], "goodword-mcp": ["web-app"]})
        with self.assertRaisesRegex(fc.FeatureChainError, "outside-scope"):
            fc.approve_plan(self.control, state["logical_chain_id"], outside)

    def test_new_chain_rejects_shell_integration_commands(self):
        state = fc.launch(self.host, self.args, ["api", "goodword-mcp"])["state"]
        plan = self.plan()
        plan["integration"]["scenarios"][0]["commands"] = ["legacy shell"]

        with self.assertRaisesRegex(fc.FeatureChainError, r"must be \{repo, argv\}"):
            fc.approve_plan(self.control, state["logical_chain_id"], plan)

    def test_planning_artifact_params_cannot_downgrade_executable_contract(self):
        launched = fc.launch(self.host, self.args, ["api", "goodword-mcp"])
        state = launched["state"]
        row = launched["row"]
        artifacts = Path(row["output_root"])
        plan = self.plan()
        plan["integration"]["scenarios"][0]["commands"] = ["legacy shell"]
        (artifacts / fc.JOINT_PLAN_ARTIFACT).write_text(json.dumps(plan), encoding="utf-8")
        (artifacts / "params.json").write_text(json.dumps({"repositories": state["repositories"]}), encoding="utf-8")
        control = fc.bind_run_control_payload(self.control, state, None, row, "planning")

        with self.assertRaisesRegex(fc.FeatureChainError, r"must be \{repo, argv\}"):
            fc.before_control(self.host, Namespace(**vars(self.args), action="approve", token="token"), row, {"feature_chain": control})

    def test_legacy_state_allows_shell_integration_commands_without_contract_flag(self):
        state = fc.launch(self.host, self.args, ["api", "goodword-mcp"])["state"]
        state.pop("executable_plan_contract", None)
        fc.write_state(self.control, state)
        plan = self.plan()
        plan["integration"]["scenarios"][0]["commands"] = ["legacy shell"]

        approved = fc.approve_plan(self.control, state["logical_chain_id"], plan)

        self.assertNotIn("executable_plan_contract", approved)
        self.assertEqual(approved["approval"]["plan_digest"], fc.digest(plan))
        fc.verify_approval(approved)

    def test_dispatch_status_flushes_control_authority_before_followup_output(self):
        state = {"logical_chain_id": "chain-1", "repositories": ["api", "goodword-mcp"]}
        row = {
            "id": "1234567890abcdef",
            "workflow_name": "full-sdlc-api-codex",
            "_control_line": "CODEX_LITE_RUN=STARTED run=12345678 control_token=operator",
        }
        stream = RecordingStream()

        with contextlib.redirect_stdout(stream):
            fc.emit_dispatch_status(state, row, "implement", "api")

        self.assertIn("CODEX_LITE_RUN=STARTED", stream.getvalue())
        self.assertIn("ARCHON_FEATURE_REPOSITORY_CHAIN=DISPATCHED", stream.getvalue())
        self.assertGreaterEqual(len(stream.flushes), 2)
        self.assertIn("control_token=operator", stream.flushes[0])
        self.assertIn("ARCHON_FEATURE_REPOSITORY_CHAIN=DISPATCHED", stream.flushes[-1])

    def test_controller_consumes_list_stage_plan_without_rewriting_approved_plan(self):
        state = fc.launch(self.host, self.args, ["api", "goodword-mcp"])["state"]
        plan = self.plan()
        plan["stages"] = [
            dict({"repo": repo}, **body)
            for repo, body in plan["stages"].items()
        ]

        approved = fc.approve_plan(self.control, state["logical_chain_id"], plan)

        self.assertIsInstance(approved["approved_plan"]["stages"], list)
        self.assertEqual(approved["approval"]["plan_digest"], fc.digest(plan))
        self.assertEqual(approved["stages"]["api"]["plan"]["files_allowlist"], ["src/api.ts"])
        self.assertEqual(approved["stages"]["goodword-mcp"]["plan"]["depends_on"], ["api"])

    def test_bind_phase_records_wrapper_write_authority(self):
        state = fc.launch(self.host, self.args, ["api", "goodword-mcp"])["state"]
        planning_row = self.row("a" * 32)
        artifacts = self.root / "planning-artifacts"
        planned = fc.bind_phase_run(self.control, state["logical_chain_id"], phase="planning", row=planning_row, artifacts_dir=artifacts)
        self.assertEqual(planned["current_run"]["write_roots"], [str(artifacts)])
        fc.assert_write_allowed(self.control, state["logical_chain_id"], artifacts / "plan.json")
        with self.assertRaises(fc.FeatureChainError):
            fc.assert_write_allowed(self.control, state["logical_chain_id"], self.root / "api" / "src.ts")

        approved = fc.approve_plan(self.control, state["logical_chain_id"], self.plan())
        api_row = self.row("b" * 32)
        running = fc.bind_phase_run(self.control, approved["logical_chain_id"], phase="implement", repo="api", row=api_row)
        api_worktree = Path(running["worktrees"]["api"]["worktree"])
        fc.assert_write_allowed(self.control, running["logical_chain_id"], api_worktree / "src.ts")
        with self.assertRaises(fc.FeatureChainError):
            fc.assert_write_allowed(self.control, running["logical_chain_id"], Path(running["worktrees"]["goodword-mcp"]["worktree"]) / "src.ts")

    def test_advance_records_candidate_and_honors_dependency_order(self):
        launched = fc.launch(self.host, self.args, ["api", "goodword-mcp"])
        state = fc.approve_plan(self.control, launched["state"]["logical_chain_id"], self.plan())
        api_row = self.row("c" * 32)
        fc.bind_phase_run(self.control, state["logical_chain_id"], phase="implement", repo="api", row=api_row)
        artifacts = self.result_artifacts(api_row, "api")

        advanced = fc.advance(self.host, self.args, dict(api_row, artifacts=str(artifacts)), {
            "state": "terminal",
            "status": "completed",
            "artifacts": str(artifacts),
            "feature_chain": {"logical_chain_id": state["logical_chain_id"], "repo": "api"},
        })

        self.assertEqual(advanced["candidate"]["repo"], "api")
        self.assertEqual(advanced["next_repo"], "goodword-mcp")
        latest = fc.read_state(self.control, state["logical_chain_id"])
        self.assertEqual(latest["stages"]["api"]["status"], "verified")

    def test_restore_and_before_control_reject_stale_run(self):
        launched = fc.launch(self.host, self.args, ["api", "goodword-mcp"])
        state = fc.approve_plan(self.control, launched["state"]["logical_chain_id"], self.plan())
        row = self.row("d" * 32)
        running = fc.bind_phase_run(self.control, state["logical_chain_id"], phase="implement", repo="api", row=row)
        control = fc.bind_run_control_payload(self.control, running, "api", row, "implement")

        env = fc.restore_control(self.host, row, self.control, {"feature_chain": control})
        self.assertEqual(env["ARCHON_FEATURE_SCOPE"], "repositories")
        self.assertEqual(env["ARCHON_FEATURE_REPO"], "api")
        self.assertEqual(fc.before_control(self.host, self.args, row, {"feature_chain": control})["logical_chain_id"], state["logical_chain_id"])
        stale = dict(row, id="e" * 32)
        with self.assertRaisesRegex(fc.FeatureChainError, "stale"):
            fc.before_control(self.host, self.args, stale, {"feature_chain": control})

    def test_integration_requires_verified_candidates_and_passing_tests(self):
        launched = fc.launch(self.host, self.args, ["api", "goodword-mcp"])
        state = fc.approve_plan(self.control, launched["state"]["logical_chain_id"], self.plan())
        for run_id, repo in (("1" * 32, "api"), ("2" * 32, "goodword-mcp")):
            row = self.row(run_id)
            fc.bind_phase_run(self.control, state["logical_chain_id"], phase="implement", repo=repo, row=row)
            artifacts = self.result_artifacts(row, repo)
            state = fc.advance(self.host, self.args, dict(row, artifacts=str(artifacts)), {
                "state": "terminal",
                "status": "completed",
                "artifacts": str(artifacts),
                "feature_chain": {"logical_chain_id": state["logical_chain_id"], "repo": repo},
            })["state"]

        with self.assertRaisesRegex(fc.FeatureChainError, "did not pass"):
            fc.finalize_integration(self.control, state["logical_chain_id"], {"status": "failed", "tests": ["fixture"]})
        latest = fc.read_state(self.control, state["logical_chain_id"])
        evidence = {
            "status": "passed",
            "tests": [{"name": "fixture"}],
            "counters": {"tests_passed": 1},
            "plan_digest": latest["approval"]["plan_digest"],
            "approved_plan_digest": latest["approval"]["plan_digest"],
            "candidate_heads": {repo: latest["candidate_handoffs"][repo]["candidate_head"] for repo in latest["repositories"]},
        }
        final = fc.finalize_integration(self.control, state["logical_chain_id"], evidence)

        self.assertEqual(final["status"], "locally_verified")
        self.assertEqual(final["integration"]["publication"], "held")

    def locally_verified_chain(self, api_changed=True, finalize_artifacts=None, finalize=True):
        launched = fc.launch(self.host, self.args, ["api", "goodword-mcp"])
        state = fc.approve_plan(self.control, launched["state"]["logical_chain_id"], self.plan())
        for run_id, repo in (("1" * 32, "api"), ("2" * 32, "goodword-mcp")):
            worktree = next((self.root / repo / ".worktrees").iterdir())
            if repo == "goodword-mcp" or api_changed:
                (worktree / "src").mkdir(exist_ok=True)
                (worktree / "src" / ("api.ts" if repo == "api" else "tool.ts")).write_text("x\n", encoding="utf-8")
                git(worktree, "add", "."); git(worktree, "commit", "-qm", f"feat({repo}): change")
            row = self.row(run_id)
            fc.bind_phase_run(self.control, state["logical_chain_id"], phase="implement", repo=repo, row=row)
            artifacts = self.result_artifacts(row, repo, head=git(worktree, "rev-parse", "HEAD"))
            (artifacts / "commit-msg.txt").write_text(f"feat({repo}): change\n\nbody\n", encoding="utf-8")
            state = fc.advance(self.host, self.args, dict(row, artifacts=str(artifacts)), {
                "state": "terminal", "status": "completed", "artifacts": str(artifacts),
                "feature_chain": {"logical_chain_id": state["logical_chain_id"], "repo": repo},
            })["state"]
        latest = fc.read_state(self.control, state["logical_chain_id"])
        if not finalize:
            return latest
        evidence = {
            "status": "passed", "tests": [{"name": "fixture"}], "counters": {"tests_passed": 1, "scenarios": 1},
            "commands": [{"scenario": "local api mcp"}],
            "plan_digest": latest["approval"]["plan_digest"], "approved_plan_digest": latest["approval"]["plan_digest"],
            "candidate_heads": {repo: latest["candidate_handoffs"][repo]["candidate_head"] for repo in latest["repositories"]},
        }
        state = fc.finalize_integration(self.control, state["logical_chain_id"], evidence, finalize_artifacts)
        integration_dir = self.root / "integration-artifacts"
        integration_dir.mkdir()
        with fc.chain_lock(self.control, state["logical_chain_id"]):
            state = fc.read_state(self.control, state["logical_chain_id"])
            state["current_run"] = {"phase": "integration", "run_id": "3" * 32, "artifacts_dir": str(integration_dir)}
            state = fc.write_state(self.control, state)
        return state

    def fake_gh(self, open_prs=None, fail_on=None):
        """Recording fake for subprocess.run: answers git push / gh pr list|create|view|edit."""
        calls, prs = [], dict(open_prs or {})
        bodies = {}
        counter = {"n": 100}

        def run(argv, capture_output=True, encoding="utf-8"):
            calls.append(list(argv))
            if fail_on and fail_on(argv):
                return SimpleNamespace(returncode=1, stdout="", stderr="injected failure")
            if argv[0] == "git" and "push" in argv:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            slug, branch = argv[argv.index("--repo") + 1], None
            if "--head" in argv:
                branch = argv[argv.index("--head") + 1]
            if argv[:3] == ["gh", "pr", "list"]:
                found = [dict(url=u, headRefOid=h, isDraft=True) for (s, b), (u, h) in prs.items() if s == slug and b == branch]
                return SimpleNamespace(returncode=0, stdout=json.dumps(found), stderr="")
            if argv[:3] == ["gh", "pr", "create"]:
                counter["n"] += 1
                url = f"https://github.com/{slug}/pull/{counter['n']}"
                prs[(slug, branch)] = (url, "created")
                bodies[url] = Path(argv[argv.index("--body-file") + 1]).read_text(encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout=url + "\n", stderr="")
            if argv[:3] == ["gh", "pr", "view"]:
                return SimpleNamespace(returncode=0, stdout=bodies.get(argv[3], ""), stderr="")
            if argv[:3] == ["gh", "pr", "edit"]:
                bodies[argv[3]] = Path(argv[argv.index("--body-file") + 1]).read_text(encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected command {argv}")

        run.calls, run.prs, run.bodies = calls, prs, bodies
        return run

    def test_reopen_resets_the_stage_and_its_consumers_and_redispatches(self):
        state = self.locally_verified_chain(finalize=False)
        self.assertEqual(state["current_run"]["phase"], "integration")
        api_head = state["candidate_handoffs"]["api"]["candidate_head"]
        with mock.patch("builtins.print"):
            out = fc.reopen(self.host, self.args, state["logical_chain_id"], "api", "integration failed: non-owner 403 path")
        latest = fc.read_state(self.control, state["logical_chain_id"])
        self.assertEqual(latest["stages"]["api"]["status"], "running")
        self.assertEqual(latest["stages"]["goodword-mcp"]["status"], "pending")
        self.assertNotIn("api", latest["candidate_handoffs"])
        self.assertNotIn("goodword-mcp", latest["candidate_handoffs"])
        self.assertIsNone(latest["integration"])
        self.assertEqual(latest["reopens"][0]["affected"], ["api", "goodword-mcp"])
        self.assertIsInstance(latest["reopens"][0]["stopped_run_id"], str)
        self.assertEqual(latest["reopens"][0]["previous_heads"]["api"], api_head)
        lane, _message, env, _row = self.host.calls[-1]
        self.assertEqual(env["ARCHON_FEATURE_REPO"], "api")
        self.assertEqual(env["ARCHON_FEATURE_PHASE"], "implement")
        self.assertEqual(out["row"]["id"], latest["current_run"]["run_id"])

    def test_reopen_of_a_leaf_stage_leaves_the_producer_verified(self):
        state = self.locally_verified_chain(finalize=False)
        with mock.patch("builtins.print"):
            fc.reopen(self.host, self.args, state["logical_chain_id"], "goodword-mcp", "fix the e2e non-owner case")
        latest = fc.read_state(self.control, state["logical_chain_id"])
        self.assertEqual(latest["stages"]["api"]["status"], "verified")
        self.assertEqual(latest["stages"]["goodword-mcp"]["status"], "running")
        self.assertIn("api", latest["candidate_handoffs"])

    def test_reopen_of_the_producer_is_allowed_after_a_stopped_consumer_run_but_not_a_foreign_one(self):
        state = self.locally_verified_chain(finalize=False)
        chain_id = state["logical_chain_id"]
        with fc.chain_lock(self.control, chain_id):
            latest = fc.read_state(self.control, chain_id)
            latest["current_run"] = {"phase": "implement", "repo": "goodword-mcp", "run_id": "4" * 32}
            fc.write_state(self.control, latest)
        with mock.patch("builtins.print"):
            fc.reopen(self.host, self.args, chain_id, "api", "cross-repo finding from the consumer")
        self.assertEqual(fc.read_state(self.control, chain_id)["stages"]["api"]["status"], "running")

    def test_reopen_refuses_a_foreign_stage_run_and_a_live_run(self):
        state = self.locally_verified_chain(finalize=False)
        chain_id = state["logical_chain_id"]
        with fc.chain_lock(self.control, chain_id):
            latest = fc.read_state(self.control, chain_id)
            latest["current_run"] = {"phase": "implement", "repo": "api", "run_id": "5" * 32}
            fc.write_state(self.control, latest)
        with self.assertRaisesRegex(fc.FeatureChainError, "would abandon the api stage run"):
            fc.reopen(self.host, self.args, chain_id, "goodword-mcp", "x")
        self.host.run_row_by_id = lambda db, run_id: {"status": "running"}
        with fc.chain_lock(self.control, chain_id):
            latest = fc.read_state(self.control, chain_id)
            latest["current_run"] = {"phase": "integration", "run_id": "6" * 32}
            fc.write_state(self.control, latest)
        with self.assertRaisesRegex(fc.FeatureChainError, "still running"):
            fc.reopen(self.host, self.args, chain_id, "goodword-mcp", "x")

    def test_reopen_refuses_verified_chains_pending_stages_and_empty_reasons(self):
        state = self.locally_verified_chain()
        with self.assertRaisesRegex(fc.FeatureChainError, "locally verified"):
            fc.reopen(self.host, self.args, state["logical_chain_id"], "api", "x")
        launched = fc.launch(self.host, self.args, ["api", "goodword-mcp"])
        fc.approve_plan(self.control, launched["state"]["logical_chain_id"], self.plan())
        with self.assertRaisesRegex(fc.FeatureChainError, "not verified"):
            fc.reopen(self.host, self.args, launched["state"]["logical_chain_id"], "api", "x")
        with self.assertRaisesRegex(fc.FeatureChainError, "requires a reason"):
            fc.reopen(self.host, self.args, launched["state"]["logical_chain_id"], "api", " ")

    def test_reopen_verify_only_puts_the_previous_head_in_the_stage_params(self):
        state = self.locally_verified_chain(finalize=False)
        api_head = state["candidate_handoffs"]["api"]["candidate_head"]
        with mock.patch("builtins.print"):
            fc.reopen(self.host, self.args, state["logical_chain_id"], "api", "verify the hand fix", verify_only=True)
        params = json.loads((Path(self.host.calls[-1][3]["output_root"]) / "params.json").read_text(encoding="utf-8"))
        self.assertEqual(params["feature_verify_only"], "yes")
        self.assertEqual(params["feature_previous_head"], api_head)
        latest = fc.read_state(self.control, state["logical_chain_id"])
        self.assertTrue(latest["reopens"][0]["verify_only"])
        self.assertEqual(latest["stages"]["api"]["verify_only_head"], api_head)
        self.assertNotIn("verify_only_head", latest["stages"]["goodword-mcp"])

    def test_reopen_without_verify_only_records_neither_param(self):
        state = self.locally_verified_chain(finalize=False)
        with mock.patch("builtins.print"):
            fc.reopen(self.host, self.args, state["logical_chain_id"], "api", "re-implement the non-owner path")
        params = json.loads((Path(self.host.calls[-1][3]["output_root"]) / "params.json").read_text(encoding="utf-8"))
        self.assertNotIn("feature_verify_only", params)
        self.assertNotIn("feature_previous_head", params)
        self.assertFalse(fc.read_state(self.control, state["logical_chain_id"])["reopens"][0]["verify_only"])

    def test_reopen_binds_the_reason_and_failure_evidence_into_the_stage_run(self):
        state = self.locally_verified_chain(finalize=False)
        api_head = state["candidate_handoffs"]["api"]["candidate_head"]
        integration_dir = Path(state["current_run"]["artifacts_dir"])
        (integration_dir / "joint-integration-result.json").write_text("{}\n", encoding="utf-8")
        (integration_dir / "joint-integration-1.log").write_text("got 500\n", encoding="utf-8")
        with mock.patch("builtins.print"):
            fc.reopen(self.host, self.args, state["logical_chain_id"], "api", "window=bogus returns 500; validate it")
        artifacts = Path(self.host.calls[-1][3]["output_root"])
        params = json.loads((artifacts / "params.json").read_text(encoding="utf-8"))
        context_path = artifacts / "reopen-context.json"
        self.assertEqual(params["feature_reopen_context_sha256"], fc.file_digest(context_path))
        context = json.loads(context_path.read_text(encoding="utf-8"))
        self.assertEqual(context["reason"], "window=bogus returns 500; validate it")
        self.assertEqual(context["previous_head"], api_head)
        self.assertEqual(context["stopped_run_id"], state["current_run"]["run_id"])
        self.assertEqual(context["evidence"], [str(integration_dir / "joint-integration-result.json"),
                                               str(integration_dir / "joint-integration-1.log")])
        latest = fc.read_state(self.control, state["logical_chain_id"])
        self.assertEqual(latest["reopens"][0]["evidence"], context["evidence"])
        self.assertNotIn("reopen_context", latest["stages"]["goodword-mcp"])

    def test_verify_only_reopen_dispatches_no_reopen_context(self):
        state = self.locally_verified_chain(finalize=False)
        with mock.patch("builtins.print"):
            fc.reopen(self.host, self.args, state["logical_chain_id"], "api", "verify the hand fix", verify_only=True)
        artifacts = Path(self.host.calls[-1][3]["output_root"])
        self.assertFalse((artifacts / "reopen-context.json").exists())
        self.assertNotIn("feature_reopen_context_sha256", json.loads((artifacts / "params.json").read_text(encoding="utf-8")))
        self.assertNotIn("reopen_context", fc.read_state(self.control, state["logical_chain_id"])["stages"]["api"])

    def test_a_tampered_reopen_context_refuses_dispatch_artifacts(self):
        state = self.locally_verified_chain(finalize=False)
        with mock.patch("builtins.print"):
            fc.reopen(self.host, self.args, state["logical_chain_id"], "api", "x")
        latest = fc.read_state(self.control, state["logical_chain_id"])
        latest["stages"]["api"]["reopen_context"]["content_text"] += " "
        with self.assertRaisesRegex(fc.FeatureChainError, "reopen context artifact does not match"):
            fc.write_phase_artifacts(self.root / "tamper", latest, "implement", "api", {"id": "7" * 32})

    def test_a_reopened_stage_whose_implement_run_stopped_can_be_reopened_again(self):
        state = self.locally_verified_chain(finalize=False)
        chain_id = state["logical_chain_id"]
        api_head = state["candidate_handoffs"]["api"]["candidate_head"]
        integration_dir = Path(state["current_run"]["artifacts_dir"])
        (integration_dir / "joint-integration-1.log").write_text("got 500\n", encoding="utf-8")
        with fc.chain_lock(self.control, chain_id):
            latest = fc.read_state(self.control, chain_id)
            latest.setdefault("phase_runs", []).append(dict(latest["current_run"]))
            fc.write_state(self.control, latest)
        with mock.patch("builtins.print"):
            fc.reopen(self.host, self.args, chain_id, "api", "first reason")
        stopped = fc.read_state(self.control, chain_id)["current_run"]
        self.assertEqual(stopped["phase"], "implement")
        with mock.patch("builtins.print"):
            fc.reopen(self.host, self.args, chain_id, "api", "second reason")
        latest = fc.read_state(self.control, chain_id)
        self.assertEqual(len(latest["reopens"]), 2)
        self.assertEqual(latest["reopens"][1]["previous_heads"]["api"], api_head)
        self.assertEqual(latest["reopens"][1]["stopped_run_id"], stopped["run_id"])
        context = json.loads((Path(self.host.calls[-1][3]["output_root"]) / "reopen-context.json").read_text(encoding="utf-8"))
        self.assertEqual((context["reason"], context["previous_head"]), ("second reason", api_head))
        # The stopped reopen run has no integration evidence; the first reopen's does.
        self.assertEqual(context["evidence"], [str(integration_dir / "joint-integration-1.log")])
        self.assertNotEqual(latest["current_run"]["run_id"], stopped["run_id"])

    def reopened_api_run(self):
        state = self.locally_verified_chain(finalize=False)
        chain_id = state["logical_chain_id"]
        integration_dir = Path(state["current_run"]["artifacts_dir"])
        (integration_dir / "joint-integration-1.log").write_text("got 500\n", encoding="utf-8")
        with mock.patch("builtins.print"):
            fc.reopen(self.host, self.args, chain_id, "api", "first reason")
        return chain_id, state["candidate_handoffs"]["api"]["candidate_head"], integration_dir

    def test_reopen_again_carries_the_original_failure_evidence_through_every_repeat(self):
        chain_id, api_head, integration_dir = self.reopened_api_run()
        for reason in ("second reason", "third reason"):
            with mock.patch("builtins.print"):
                fc.reopen(self.host, self.args, chain_id, "api", reason)
        latest = fc.read_state(self.control, chain_id)
        self.assertEqual(latest["reopens"][2]["evidence"], [str(integration_dir / "joint-integration-1.log")])
        self.assertEqual(latest["reopens"][2]["previous_heads"]["api"], api_head)

    def test_reopen_again_refuses_when_a_later_reopen_owns_the_stage_or_the_run_is_not_stopped(self):
        chain_id, _head, _dir = self.reopened_api_run()
        with fc.chain_lock(self.control, chain_id):
            latest = fc.read_state(self.control, chain_id)
            latest["reopens"].append({"repo": "upstream", "affected": ["upstream", "api"], "reopened_at": fc.now()})
            fc.write_state(self.control, latest)
        with self.assertRaisesRegex(fc.FeatureChainError, "not verified"):
            fc.reopen(self.host, self.args, chain_id, "api", "x")
        with fc.chain_lock(self.control, chain_id):
            latest = fc.read_state(self.control, chain_id)
            latest["reopens"].pop()
            fc.write_state(self.control, latest)
        self.host.run_row_by_id = lambda db, run_id: {"status": "paused"}
        with self.assertRaisesRegex(fc.FeatureChainError, "failed, cancelled, or completed without feature-result.json, got paused"):
            fc.reopen(self.host, self.args, chain_id, "api", "x")
        # Negative control: the same chain with a failed run is admitted.
        self.host.run_row_by_id = lambda db, run_id: {"status": "failed"}
        with mock.patch("builtins.print"):
            fc.reopen(self.host, self.args, chain_id, "api", "x")

    def test_reopen_again_admits_a_completed_run_that_wrote_no_feature_result(self):
        # Run e21573ca: a verify-only stage completed with every gate skipped and no
        # feature-result.json; feature-advance could not verify it, and reopen refused it.
        chain_id, api_head, _dir = self.reopened_api_run()
        stopped = Path(fc.read_state(self.control, chain_id)["current_run"]["artifacts_dir"])
        self.host.run_row_by_id = lambda db, run_id: {"status": "completed"}
        # Negative control: a completed run that did write its result is not stopped.
        (stopped / "feature-result.json").write_text('{"outcome": "CHANGED"}', encoding="utf-8")
        with self.assertRaisesRegex(fc.FeatureChainError, "got completed"):
            fc.reopen(self.host, self.args, chain_id, "api", "x", verify_only=True)
        (stopped / "feature-result.json").unlink()
        with mock.patch("builtins.print"):
            fc.reopen(self.host, self.args, chain_id, "api", "x", verify_only=True)
        latest = fc.read_state(self.control, chain_id)
        self.assertEqual(latest["reopens"][-1]["stopped_run_artifacts"], str(stopped))
        self.assertEqual(latest["stages"]["api"]["verify_only_head"], api_head)

    def test_a_verify_only_stage_whose_candidate_tree_is_unchanged_does_not_verify(self):
        state = self.locally_verified_chain(finalize=False)
        chain_id = state["logical_chain_id"]
        api_head = state["candidate_handoffs"]["api"]["candidate_head"]
        with mock.patch("builtins.print"):
            fc.reopen(self.host, self.args, chain_id, "api", "verify the hand fix", verify_only=True)
        latest = fc.read_state(self.control, chain_id)
        artifacts = self.root / "verify-only-no-change"
        artifacts.mkdir()
        (artifacts / "feature-result.json").write_text(json.dumps({"outcome": "NO_CHANGE", "head": api_head}), encoding="utf-8")
        row = {"id": latest["current_run"]["run_id"]}
        with self.assertRaisesRegex(fc.FeatureChainError, "reopen produced no change"):
            fc.candidate_from_artifacts("api", row, artifacts, latest)
        # Negative control: without verify_only_head the same candidate passes the check.
        latest["stages"]["api"].pop("verify_only_head")
        with self.assertRaisesRegex(fc.FeatureChainError, "params.json"):
            fc.candidate_from_artifacts("api", row, artifacts, latest)

    def test_a_reopened_stage_whose_candidate_tree_is_unchanged_does_not_verify(self):
        chain_id, api_head, _dir = self.reopened_api_run()
        latest = fc.read_state(self.control, chain_id)
        artifacts = self.root / "no-change-artifacts"
        artifacts.mkdir()
        (artifacts / "feature-result.json").write_text(json.dumps({"outcome": "NO_CHANGE", "head": api_head}), encoding="utf-8")
        row = {"id": latest["current_run"]["run_id"]}
        with self.assertRaisesRegex(fc.FeatureChainError, "reopen produced no change"):
            fc.candidate_from_artifacts("api", row, artifacts, latest)
        # Negative control: without the reopen context the same candidate gets past
        # the check (and stops later, on the params.json this fixture never wrote).
        latest["stages"]["api"].pop("reopen_context")
        with self.assertRaisesRegex(fc.FeatureChainError, "params.json"):
            fc.candidate_from_artifacts("api", row, artifacts, latest)

    def test_dispatch_binds_the_contract_symbols_digest_in_params(self):
        chain_id, _head, _dir = self.reopened_api_run()
        artifacts = Path(self.host.calls[-1][3]["output_root"])
        params = json.loads((artifacts / "params.json").read_text(encoding="utf-8"))
        self.assertEqual(params["feature_contract_symbols_sha256"], fc.file_digest(artifacts / "contract-symbols.json"))

    def test_an_implementing_stage_that_was_never_reopened_is_not_reopenable(self):
        # Negative control for the re-reopen admission: the prior reopen record is
        # what makes a stopped implement run reopenable, not the run alone.
        state = self.locally_verified_chain(finalize=False)
        chain_id = state["logical_chain_id"]
        with fc.chain_lock(self.control, chain_id):
            latest = fc.read_state(self.control, chain_id)
            latest["stages"]["api"]["status"] = "running"
            latest["current_run"] = {"phase": "implement", "repo": "api", "run_id": "8" * 32}
            fc.write_state(self.control, latest)
        with self.assertRaisesRegex(fc.FeatureChainError, "not verified"):
            fc.reopen(self.host, self.args, chain_id, "api", "x")

    def test_stage_dispatch_writes_the_approved_contract_symbols(self):
        state = self.locally_verified_chain(finalize=False)
        api = self.root / "contract-api"
        fc.write_phase_artifacts(api, state, "implement", "api", {"id": "7" * 32})
        doc = json.loads((api / "contract-symbols.json").read_text(encoding="utf-8"))
        self.assertEqual((doc["repo"], doc["plan_digest"]), ("api", state["approval"]["plan_digest"]))
        self.assertEqual(doc["contracts"], [{"artifact": "openapi.json", "symbols": []}])

    def test_contract_symbols_come_from_the_producer_contracts_plan_lines_and_owned_pins(self):
        plan = self.plan()
        plan["contracts"][0]["artifact"] = "src/dto.ts"
        plan["contracts"][0]["description"] = "BriefingResponseDto shape (meetings, optional travelCandidates) the tool mirrors"
        plan["pinned_decisions"] = [
            {"symbol": "resolveBriefingWindow", "file": "src/api.ts", "rule": "r"},
            {"symbol": "getBriefing", "file": "src/tool.ts", "rule": "r"},
        ]
        plan_md = ("- `src/dto.ts` — contract: `{ window, travelCandidates?: TravelCandidatesDto }` see `apps/x/y.ts:3`\n"
                   "- unrelated line naming `OtherDto`\n")
        state = {"approved_plan": plan, "approval": {"plan_digest": "d"}}
        self.assertEqual(fc.contract_symbols(state, "api", plan_md)["contracts"], [
            {"artifact": "src/api.ts", "symbols": ["resolveBriefingWindow"]},
            {"artifact": "src/dto.ts", "symbols": ["BriefingResponseDto", "TravelCandidatesDto", "travelCandidates", "window"]},
        ])
        # Negative control: the consumer produces no contract and gets only its own pin.
        self.assertEqual(fc.contract_symbols(state, "goodword-mcp", plan_md)["contracts"],
                         [{"artifact": "src/tool.ts", "symbols": ["getBriefing"]}])

    def test_reopen_takes_verify_only_from_the_cli_flag_on_args(self):
        state = self.locally_verified_chain(finalize=False)
        api_head = state["candidate_handoffs"]["api"]["candidate_head"]
        with mock.patch("builtins.print"):
            fc.reopen(self.host, Namespace(**vars(self.args), verify_only=True), state["logical_chain_id"], "api", "x")
        params = json.loads((Path(self.host.calls[-1][3]["output_root"]) / "params.json").read_text(encoding="utf-8"))
        self.assertEqual(params["feature_previous_head"], api_head)

    def timing_db(self, rows):
        db = self.root / f"timing-{len(list(self.root.glob('timing-*.db')))}.db"
        with sqlite3.connect(db) as con:
            con.execute(
                "CREATE TABLE remote_agent_workflow_runs "
                "(id TEXT, workflow_name TEXT, status TEXT, started_at TEXT, completed_at TEXT)"
            )
            con.executemany(
                "INSERT INTO remote_agent_workflow_runs (id, started_at, completed_at) VALUES (?, ?, ?)", rows)
        return db

    def timing_state(self, chain_id="f" * 32):
        return {
            "logical_chain_id": chain_id,
            "repositories": ["api", "goodword-mcp"],
            "phase_runs": [
                {"phase": "planning", "run_id": "a" * 32},
                {"phase": "implement", "repo": "api", "run_id": "b" * 32},
                {"phase": "implement", "repo": "goodword-mcp", "run_id": "c" * 32},
                {"phase": "integration", "run_id": "d" * 32},
            ],
        }

    def test_chain_timing_sums_each_phase_and_leaves_unfinished_runs_null(self):
        db = self.timing_db([
            ("a" * 32, "2026-09-14 10:00:00", "2026-09-14 10:10:00"),
            ("b" * 32, "2026-09-14 10:10:00", "2026-09-14 10:40:00"),
            ("c" * 32, "2026-09-14 10:40:00", "2026-09-14 11:00:00"),
            ("d" * 32, "2026-09-14 11:00:00", None),
        ])

        timing = fc.chain_timing(self.timing_state(), db)

        self.assertEqual(timing["planning_s"], 600)
        self.assertEqual(timing["stages"], {"api": 1800, "goodword-mcp": 1200})
        self.assertIsNone(timing["integration_s"])
        self.assertEqual(timing["wall_s"], 3600)
        self.assertRegex(timing["updated_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_chain_timing_sums_resume_segments_and_excludes_the_gate_wait(self):
        """Live 2026-09-16 (chain 3460c074): the runs table reported api=1482 s for a
        stage resumed five times, and a planning row whose started_at was the approve."""
        db = self.timing_db([
            ("a" * 32, "2026-09-14 12:00:00", "2026-09-14 12:00:01"),
            ("b" * 32, "2026-09-14 13:00:00", "2026-09-14 13:10:00"),
            ("c" * 32, None, None),
            ("d" * 32, None, None),
        ])
        with sqlite3.connect(db) as con:
            con.execute(
                "CREATE TABLE remote_agent_workflow_events "
                "(workflow_run_id TEXT, event_type TEXT, created_at TEXT)"
            )
            con.executemany("INSERT INTO remote_agent_workflow_events VALUES (?, ?, ?)", [
                ("a" * 32, "workflow_started", "2026-09-14 10:00:00"),
                ("a" * 32, "approval_requested", "2026-09-14 10:20:00"),   # gate wait 10:20 -> 12:00 is not work
                ("a" * 32, "workflow_started", "2026-09-14 12:00:00"),
                ("a" * 32, "workflow_completed", "2026-09-14 12:00:01"),
                ("b" * 32, "workflow_started", "2026-09-14 12:10:00"),
                ("b" * 32, "workflow_failed", "2026-09-14 12:40:00"),
                ("b" * 32, "workflow_started", "2026-09-14 13:00:00"),
                ("b" * 32, "workflow_completed", "2026-09-14 13:10:00"),
            ])

        timing = fc.chain_timing(self.timing_state(), db)

        self.assertEqual(timing["planning_s"], 1201)
        self.assertEqual(timing["stages"], {"api": 2400, "goodword-mcp": None})
        self.assertEqual(timing["wall_s"], 3 * 3600 + 600)

    def test_chain_timing_is_all_null_when_the_run_table_is_unreadable(self):
        timing = fc.chain_timing(self.timing_state(), self.root / "missing.db")

        self.assertIsNone(timing["wall_s"])
        self.assertIsNone(timing["planning_s"])
        self.assertIsNone(timing["integration_s"])
        self.assertEqual(timing["stages"], {"api": None, "goodword-mcp": None})

    def record_timing(self, wall_seconds, chain_id):
        start = datetime.datetime(2026, 9, 14, 10, 0, 0)
        end = start + datetime.timedelta(seconds=wall_seconds)
        db = self.timing_db([
            ("a" * 32, start.strftime("%Y-%m-%d %H:%M:%S"), start.strftime("%Y-%m-%d %H:%M:%S")),
            ("d" * 32, start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")),
        ])
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            fc.record_chain_timing(Namespace(db=db, control_dir=self.control), self.timing_state(chain_id))
        return stream.getvalue()

    def test_chain_timing_prints_the_typed_line_and_flags_only_a_wall_over_the_cap(self):
        at_cap = self.record_timing(10800, "a" * 32)
        over_cap = self.record_timing(10801, "b" * 32)

        self.assertIn("CHAIN_TIMING wall=10800 planning=0 stages=api:null,goodword-mcp:null integration=10800\n", at_cap)
        self.assertNotIn("CHAIN_BUDGET=EXCEEDED wall=", at_cap)
        self.assertIn("CHAIN_TIMING wall=10801 planning=0 stages=api:null,goodword-mcp:null integration=10801\n", over_cap)
        self.assertIn("CHAIN_BUDGET=EXCEEDED wall=10801 cap=10800\n", over_cap)
        written = json.loads(fc.timing_path(self.control, "b" * 32).read_text(encoding="utf-8"))
        self.assertEqual(written["wall_s"], 10801)

    def test_the_active_budget_is_separate_from_the_wall_budget(self):
        """Item 9: the 2-hour claim is about active minutes.

        Wall includes every human gate, so a chain that sat at a plan-gate
        overnight blows the wall cap having done an hour of work, and a chain
        that burned three hours of agent time inside a two-hour window does not
        blow it at all. The active sum is the one the claim is about, and it is
        capped separately.
        """
        over = self.record_timing(10801, "c" * 32)

        self.assertIn("CHAIN_ACTIVE active=10801 cap=7200 wall=10801\n", over)
        self.assertIn("CHAIN_BUDGET=EXCEEDED active=10801 cap=7200 wall=10801\n", over)

    def test_the_active_cap_is_overridable(self):
        with mock.patch.dict(fc.os.environ, {"ARCHON_CHAIN_BUDGET_S": "20000"}):
            output = self.record_timing(10801, "e" * 32)
        self.assertIn("CHAIN_ACTIVE active=10801 cap=20000", output)
        self.assertNotIn("CHAIN_BUDGET=EXCEEDED active=", output)

    def test_a_junk_active_cap_falls_back_to_the_default(self):
        for junk in ("", "0", "-1", "two hours"):
            with self.subTest(value=junk):
                with mock.patch.dict(fc.os.environ, {"ARCHON_CHAIN_BUDGET_S": junk}):
                    self.assertEqual(fc.active_budget_seconds(), 7200)

    def test_advance_writes_chain_timing_on_every_stage_transition(self):
        state = self.locally_verified_chain(finalize=False)

        timing = json.loads(fc.timing_path(self.control, state["logical_chain_id"]).read_text(encoding="utf-8"))

        self.assertEqual(set(timing),
                         {"wall_s", "planning_s", "stages", "integration_s", "activity", "updated_at"})
        self.assertEqual(timing["stages"], {"api": None, "goodword-mcp": None})

    def activity_state(self, entries_by_round):
        """A chain whose api stage has round dirs carrying an activity log."""
        artifacts = self.root / "activity-artifacts"
        for n, entries in entries_by_round.items():
            round_dir = artifacts / f"round-{n}"
            round_dir.mkdir(parents=True)
            (round_dir / "activity.jsonl").write_text(
                "".join(json.dumps({"round": n, **e}) + "\n" for e in entries), encoding="utf-8")
        state = self.timing_state()
        for item in state["phase_runs"]:
            if item.get("repo") == "api":
                item["artifacts_dir"] = str(artifacts)
        return state, artifacts

    def test_round_telemetry_counts_invocations_reuses_and_rounds(self):
        _, artifacts = self.activity_state({
            1: [{"kind": "review", "decision": "run", "id": "id1"},
                {"kind": "review", "decision": "done", "id": "id1"},
                {"kind": "fixer", "decision": "run", "tree": "t1"},
                {"kind": "fixer", "decision": "done", "tree": "t1"}],
            2: [{"kind": "review", "decision": "run", "id": "id2"},
                {"kind": "review", "decision": "done", "id": "id2"},
                {"kind": "review", "decision": "reuse", "id": "id2"},
                {"kind": "fixer", "decision": "reuse-committed", "tree": "t2"}],
        })

        counts = fc.round_telemetry(artifacts)

        self.assertEqual(counts, {"rounds": 2, "review_invocations": 2, "review_reused": 1,
                                  "review_duplicates": 0, "fixer_invocations": 1,
                                  "fixer_duplicates": 0})

    def test_a_run_after_a_done_for_the_same_id_is_a_duplicate(self):
        """The property the whole of item 1 exists to hold.

        Counted by this reader from the log, not asserted by round-state.py: the
        component whose refusal to duplicate is under measurement cannot also be
        the one certifying it.
        """
        _, artifacts = self.activity_state({
            1: [{"kind": "review", "decision": "run", "id": "id1"},
                {"kind": "review", "decision": "done", "id": "id1"},
                {"kind": "review", "decision": "run", "id": "id1"},
                {"kind": "fixer", "decision": "done", "tree": "t1"},
                {"kind": "fixer", "decision": "run", "tree": "t1"}],
        })

        counts = fc.round_telemetry(artifacts)

        self.assertEqual(counts["review_duplicates"], 1)
        self.assertEqual(counts["fixer_duplicates"], 1)

    def test_a_new_round_repeating_an_activity_is_not_a_duplicate(self):
        """A progressed decision opens a round that asks for a fresh review."""
        _, artifacts = self.activity_state({
            1: [{"kind": "review", "decision": "run", "id": "id1"},
                {"kind": "review", "decision": "done", "id": "id1"}],
            2: [{"kind": "review", "decision": "run", "id": "id1"}],
        })

        self.assertEqual(fc.round_telemetry(artifacts)["review_duplicates"], 0)

    def test_an_interrupted_activity_rerun_is_not_a_duplicate(self):
        """No `done` was ever recorded, so the second run finishes the first."""
        _, artifacts = self.activity_state({
            1: [{"kind": "review", "decision": "run", "id": "id1"},
                {"kind": "review", "decision": "run", "id": "id1"},
                {"kind": "review", "decision": "done", "id": "id1"}],
        })

        counts = fc.round_telemetry(artifacts)
        self.assertEqual(counts["review_invocations"], 2)
        self.assertEqual(counts["review_duplicates"], 0)

    def test_a_stage_with_no_activity_log_counts_its_rounds_and_nothing_else(self):
        artifacts = self.root / "bare-artifacts"
        (artifacts / "round-1").mkdir(parents=True)
        (artifacts / "round-2").mkdir(parents=True)

        counts = fc.round_telemetry(artifacts)

        self.assertEqual(counts["rounds"], 2)
        self.assertEqual(counts["review_invocations"], 0)

    def test_a_missing_artifacts_directory_is_all_zero(self):
        self.assertEqual(fc.round_telemetry(self.root / "gone"),
                         {key: 0 for key in fc.ROUND_TELEMETRY_KEYS})

    def test_chain_timing_prints_the_review_counts_and_stores_them(self):
        state, _ = self.activity_state({
            1: [{"kind": "review", "decision": "run", "id": "id1"},
                {"kind": "review", "decision": "done", "id": "id1"},
                {"kind": "review", "decision": "reuse", "id": "id1"},
                {"kind": "fixer", "decision": "run", "tree": "t1"}],
        })
        db = self.timing_db([("a" * 32, "2026-09-14 10:00:00", "2026-09-14 10:10:00")])
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            timing = fc.record_chain_timing(Namespace(db=db, control_dir=self.control), state)

        self.assertIn("CHAIN_TIMING reviews=1/1 rounds=1 review_duplicates=0 "
                      "fixers=1 fixer_duplicates=0\n", stream.getvalue())
        self.assertEqual(timing["activity"]["api"]["review_invocations"], 1)

    def test_publish_opens_draft_prs_in_dependency_order_and_cross_links(self):
        state = self.locally_verified_chain()
        run = self.fake_gh()
        with mock.patch("builtins.print"):
            out = fc.publish(self.host, self.args, state["logical_chain_id"], run=run)
        creates = [c for c in run.calls if c[:3] == ["gh", "pr", "create"]]
        self.assertEqual([c[c.index("--head") + 1].rsplit("-", 1)[1] for c in creates], ["api", "mcp"])
        for c in creates:
            self.assertIn("--draft", c); self.assertEqual(c[c.index("--base") + 1], "main"); self.assertNotIn("--label", c)
        self.assertEqual(creates[0][creates[0].index("--title") + 1], "feat(api): change [archon]")
        api_url, mcp_url = out["publications"]["api"]["pr_url"], out["publications"]["goodword-mcp"]["pr_url"]
        self.assertIn(api_url, run.bodies[mcp_url])
        self.assertIn(mcp_url, run.bodies[api_url])
        self.assertIn("Depends on: api", run.bodies[mcp_url])
        self.assertEqual(out["edited"], ["api"])
        record = json.loads((self.root / "integration-artifacts" / fc.PUBLICATION_ARTIFACT).read_text(encoding="utf-8"))
        latest = fc.read_state(self.control, state["logical_chain_id"])
        fc.verify_publication_record(latest, record)
        self.assertEqual(latest["publication"], record)
        self.assertEqual(latest["integration"]["publication"], "held")
        fc.verify_receipt(latest)

    def test_publication_body_lists_only_acknowledged_cross_repo_findings(self):
        state = self.locally_verified_chain()
        artifacts = Path(state["candidate_handoffs"]["api"]["artifacts"])
        (artifacts / "round-1").mkdir(exist_ok=True)
        filed = {"finding": "mcp drops errorMessage", "action": "route", "producer_repo": "goodword-mcp", "severity": "P2"}
        unfiled = {"finding": "mcp retries forever", "action": "route", "producer_repo": "goodword-mcp", "severity": "P1"}
        (artifacts / "round-1" / "fixer-result.json").write_text(json.dumps(
            {"applied": [], "failed": [], "advisory": [], "cross_repo": [filed, unfiled]}), encoding="utf-8")
        body = fc.publication_body(state, "api", {})
        self.assertNotIn("Cross-repo findings filed", body)
        # sha256("goodword-mcp\nmcp drops errorMessage")[:16], computed outside the helper.
        (artifacts / "cross-repo-filed.json").write_text(json.dumps(
            [{"key": "65609a9208165071", "filed": "https://github.com/o/goodword-mcp/issues/12", "by": "edy"}]), encoding="utf-8")
        body = fc.publication_body(state, "api", {})
        self.assertIn("## Cross-repo findings filed\n\n- [P2] goodword-mcp: mcp drops errorMessage "
                      "(filed: https://github.com/o/goodword-mcp/issues/12, by edy, key `65609a9208165071`)", body)
        self.assertNotIn("mcp retries forever", body)

    def test_publish_no_change_repo_skips_push_and_pr(self):
        state = self.locally_verified_chain(api_changed=False)
        run = self.fake_gh()
        with mock.patch("builtins.print"):
            out = fc.publish(self.host, self.args, state["logical_chain_id"], run=run)
        self.assertEqual(out["publications"]["api"]["outcome"], "NO_CHANGE")
        self.assertIsNone(out["publications"]["api"]["pr_url"])
        self.assertFalse([c for c in run.calls if "push" in c and "-api" in c[-2]])
        self.assertEqual(len([c for c in run.calls if c[:3] == ["gh", "pr", "create"]]), 1)
        self.assertIn("no change, no PR", run.bodies[out["publications"]["goodword-mcp"]["pr_url"]])

    def test_publish_adopts_open_pr_on_exact_head_and_refuses_mismatch(self):
        state = self.locally_verified_chain()
        api_meta = state["worktrees"]["api"]
        head = state["candidate_handoffs"]["api"]["candidate_head"]
        run = self.fake_gh(open_prs={("Owner/api", api_meta["branch"]): ("https://github.com/Owner/api/pull/7", head)})
        with mock.patch("builtins.print"):
            out = fc.publish(self.host, self.args, state["logical_chain_id"], run=run)
        self.assertEqual(out["publications"]["api"]["pr_url"], "https://github.com/Owner/api/pull/7")
        self.assertEqual(len([c for c in run.calls if c[:3] == ["gh", "pr", "create"]]), 1)

        stale = fc.read_state(self.control, state["logical_chain_id"])
        with fc.chain_lock(self.control, state["logical_chain_id"]):
            stale.pop("publication"); fc.write_state(self.control, stale)
        run = self.fake_gh(open_prs={("Owner/api", api_meta["branch"]): ("https://github.com/Owner/api/pull/8", "f" * 40)})
        with self.assertRaisesRegex(fc.FeatureChainError, "pr head mismatch repo=api"):
            fc.publish(self.host, self.args, state["logical_chain_id"], run=run)
        self.assertFalse([c for c in run.calls if c[:3] == ["gh", "pr", "create"]])
        self.assertIsNone(fc.read_state(self.control, state["logical_chain_id"]).get("publication"))

    def test_publish_refuses_unverified_dirty_or_drifted_chains_without_pushing(self):
        state = self.locally_verified_chain()
        run = self.fake_gh()
        with fc.chain_lock(self.control, state["logical_chain_id"]):
            fc.write_state(self.control, dict(fc.read_state(self.control, state["logical_chain_id"]), status="implementing"))
        with self.assertRaisesRegex(fc.FeatureChainError, "not locally_verified"):
            fc.publish(self.host, self.args, state["logical_chain_id"], run=run)
        with fc.chain_lock(self.control, state["logical_chain_id"]):
            fc.write_state(self.control, dict(fc.read_state(self.control, state["logical_chain_id"]), status="locally_verified"))
        self.assertEqual(run.calls, [])

        worktree = Path(state["worktrees"]["api"]["worktree"])
        (worktree / "dirty.txt").write_text("x\n", encoding="utf-8")
        with self.assertRaisesRegex(fc.FeatureChainError, "dirty"):
            fc.publish(self.host, self.args, state["logical_chain_id"], run=run)
        (worktree / "dirty.txt").unlink()
        git(worktree, "commit", "-q", "--allow-empty", "-m", "drift")
        with self.assertRaisesRegex(fc.FeatureChainError, "drifted"):
            fc.publish(self.host, self.args, state["logical_chain_id"], run=run)
        self.assertEqual(run.calls, [])

    def test_publish_resumes_after_partial_failure_by_adopting_first_pr(self):
        state = self.locally_verified_chain()
        mcp_branch = state["worktrees"]["goodword-mcp"]["branch"]
        run = self.fake_gh(fail_on=lambda argv: argv[:3] == ["gh", "pr", "create"] and argv[argv.index("--head") + 1] == mcp_branch)
        with self.assertRaisesRegex(fc.FeatureChainError, "gh pr create goodword-mcp failed"):
            fc.publish(self.host, self.args, state["logical_chain_id"], run=run)
        self.assertIsNone(fc.read_state(self.control, state["logical_chain_id"]).get("publication"))
        api_url = next(iter(run.prs.values()))[0]

        head = state["candidate_handoffs"]["api"]["candidate_head"]
        run2 = self.fake_gh(open_prs={("Owner/api", state["worktrees"]["api"]["branch"]): (api_url, head)})
        run2.bodies[api_url] = run.bodies[api_url]
        with mock.patch("builtins.print"):
            out = fc.publish(self.host, self.args, state["logical_chain_id"], run=run2)
        self.assertEqual(out["publications"]["api"]["pr_url"], api_url)
        self.assertEqual(len([c for c in run2.calls if c[:3] == ["gh", "pr", "create"]]), 1)
        self.assertIn(out["publications"]["goodword-mcp"]["pr_url"], run2.bodies[api_url])

        run3 = self.fake_gh(open_prs={(k[0], k[1]): v for k, v in run2.prs.items()})
        run3.bodies.update(run2.bodies)
        with mock.patch("builtins.print"):
            again = fc.publish(self.host, self.args, state["logical_chain_id"], run=run3)
        self.assertEqual(again["publications"], out["publications"])
        self.assertEqual([c for c in run3.calls if c[0] == "git" or c[:3] in (["gh", "pr", "create"], ["gh", "pr", "edit"])], [])

    def clear_current_run(self, chain_id):
        with fc.chain_lock(self.control, chain_id):
            state = fc.read_state(self.control, chain_id)
            state["current_run"] = None
            fc.write_state(self.control, state)

    def test_finalize_through_wrapper_records_integration_artifacts(self):
        artifacts = self.root / "wrapper-artifacts"
        state = self.locally_verified_chain(finalize_artifacts=artifacts)
        self.assertEqual(state["integration_artifacts"], str(artifacts))

    def test_publish_falls_back_to_integration_artifacts_when_current_run_is_cleared(self):
        artifacts = self.root / "wrapper-artifacts"
        state = self.locally_verified_chain(finalize_artifacts=artifacts)
        self.clear_current_run(state["logical_chain_id"])
        run = self.fake_gh()
        with mock.patch("builtins.print"):
            out = fc.publish(self.host, self.args, state["logical_chain_id"], run=run)
        self.assertEqual(out["record_path"], str(artifacts / fc.PUBLICATION_ARTIFACT))
        self.assertEqual(json.loads((artifacts / fc.PUBLICATION_ARTIFACT).read_text(encoding="utf-8")), out["record"])

    def test_publish_without_any_artifacts_directory_raises(self):
        state = self.locally_verified_chain()
        self.clear_current_run(state["logical_chain_id"])
        self.assertNotIn("integration_artifacts", fc.read_state(self.control, state["logical_chain_id"]))
        run = self.fake_gh()
        with self.assertRaisesRegex(fc.FeatureChainError, "no integration artifacts directory for the publication record"):
            fc.publish(self.host, self.args, state["logical_chain_id"], run=run)
        self.assertEqual(run.calls, [], "must fail before any push or PR call")

    def test_before_control_claim_rejects_duplicate_resume_until_released(self):
        launched = fc.launch(self.host, self.args, ["api", "goodword-mcp"])
        state = fc.approve_plan(self.control, launched["state"]["logical_chain_id"], self.plan())
        row = self.row("f" * 32)
        running = fc.bind_phase_run(self.control, state["logical_chain_id"], phase="implement", repo="api", row=row)
        control = fc.bind_run_control_payload(self.control, running, "api", row, "implement")
        args = Namespace(**vars(self.args), action="resume", token="token")

        fc.before_control(self.host, args, row, {"feature_chain": control})
        with self.assertRaisesRegex(fc.FeatureChainError, "control already in progress"):
            fc.before_control(self.host, args, row, {"feature_chain": control})
        with fc.host_env(fc, {"ARCHON_FEATURE_CHAIN_ID": state["logical_chain_id"]}):
            fc.after_control(self.host, args, row)
        fc.before_control(self.host, args, row, {"feature_chain": control})

    def test_before_control_recovers_dead_claim_but_never_steals_live_claim(self):
        launched = fc.launch(self.host, self.args, ["api", "goodword-mcp"])
        state = fc.approve_plan(self.control, launched["state"]["logical_chain_id"], self.plan())
        row = self.row("7" * 32)
        running = fc.bind_phase_run(self.control, state["logical_chain_id"], phase="implement", repo="api", row=row)
        control = fc.bind_run_control_payload(self.control, running, "api", row, "implement")
        args = Namespace(**vars(self.args), action="resume", token="token")

        running["pending_control"] = {
            "action": "resume",
            "run_id": row["id"],
            "owner_pid": 99999999,
            "owner_fingerprint": "definitely-dead",
            "claimed_at": fc.now(),
        }
        fc.write_state(self.control, running)
        recovered = fc.before_control(self.host, args, row, {"feature_chain": control})
        self.assertEqual(recovered["pending_control"]["run_id"], row["id"])

        recovered["pending_control"] = {
            "action": "resume",
            "run_id": row["id"],
            "owner_pid": fc.os.getpid(),
            "owner_fingerprint": fc.process_fingerprint(),
            "claimed_at": fc.now(),
        }
        fc.write_state(self.control, recovered)
        with self.assertRaisesRegex(fc.FeatureChainError, "control already in progress"):
            fc.before_control(self.host, args, row, {"feature_chain": control})

    def test_before_control_revalidates_token_under_chain_lock(self):
        launched = fc.launch(self.host, self.args, ["api", "goodword-mcp"])
        state = fc.approve_plan(self.control, launched["state"]["logical_chain_id"], self.plan())
        row = self.row("8" * 32)
        running = fc.bind_phase_run(self.control, state["logical_chain_id"], phase="implement", repo="api", row=row)
        control = fc.bind_run_control_payload(self.control, running, "api", row, "implement")
        calls = []
        def require_control_token(row_arg, control_dir, token):
            calls.append((row_arg["id"], token))
            if token != "fresh":
                raise fc.FeatureChainError("stale token")
        self.host.require_control_token = require_control_token

        with self.assertRaisesRegex(fc.FeatureChainError, "stale token"):
            fc.before_control(self.host, Namespace(**vars(self.args), action="resume", token="old"), row, {"feature_chain": control})
        self.assertIsNone(fc.read_state(self.control, state["logical_chain_id"])["pending_control"])
        fc.before_control(self.host, Namespace(**vars(self.args), action="resume", token="fresh"), row, {"feature_chain": control})
        self.assertEqual(calls, [(row["id"], "old"), (row["id"], "fresh")])

    def test_before_control_refreshes_stale_args_from_locked_chain_budget(self):
        launched = fc.launch(self.host, self.args, ["api", "goodword-mcp"])
        row = launched["row"]
        state = fc.read_state(self.control, launched["state"]["logical_chain_id"])
        state["budget"]["wall_minutes"] = 240
        state["budget"]["max_total_tokens"] = 100_000_000
        state = fc.write_state(self.control, state)
        control = fc.bind_run_control_payload(self.control, state, None, row, "planning")
        args = Namespace(**vars(self.args), action="resume", token="token")
        args.max_total_tokens = 30_000_000

        def budget_require_remaining(_args, _chain_id):
            self.assertEqual(100_000_000, _args.max_total_tokens)
            self.assertEqual(240, _args.wall_minutes)
            return {"max_total_tokens": 100_000_000}

        with mock.patch.object(fc, "budget_require_remaining", side_effect=budget_require_remaining):
            fc.before_control(self.host, args, row, {"feature_chain": control})

        self.assertEqual(100_000_000, args.max_total_tokens)
        self.assertEqual(240, args.wall_minutes)

    def test_failed_approval_validation_does_not_leave_pending_claim(self):
        launched = fc.launch(self.host, self.args, ["api", "goodword-mcp"])
        state = launched["state"]
        row = launched["row"]
        artifacts = Path(row["output_root"])
        (artifacts / fc.JOINT_PLAN_ARTIFACT).write_text(json.dumps({"schema": "wrong"}), encoding="utf-8")
        control = fc.bind_run_control_payload(self.control, state, None, row, "planning")
        args = Namespace(**vars(self.args), action="approve", token="token")

        with self.assertRaises(fc.FeatureChainError):
            fc.before_control(self.host, args, row, {"feature_chain": control})
        self.assertIsNone(fc.read_state(self.control, state["logical_chain_id"])["pending_control"])

    def test_dispatch_reservation_retries_same_next_stage_after_invoke_error(self):
        launched = fc.launch(self.host, self.args, ["api", "goodword-mcp"])
        state = fc.approve_plan(self.control, launched["state"]["logical_chain_id"], self.plan())
        row = self.row("9" * 32)
        fc.bind_phase_run(self.control, state["logical_chain_id"], phase="implement", repo="api", row=row)
        artifacts = self.result_artifacts(row, "api")
        original_calls = len(self.host.calls)
        def fail_dispatch(args, lane, message):
            raise fc.FeatureChainError("dispatch failed")
        self.host.invoke_codex_lane = fail_dispatch
        payload = {
            "state": "terminal",
            "status": "completed",
            "artifacts": str(artifacts),
            "feature_chain": {"logical_chain_id": state["logical_chain_id"], "repo": "api"},
        }

        with self.assertRaisesRegex(fc.FeatureChainError, "dispatch failed"):
            fc.advance(self.host, self.args, dict(row, artifacts=str(artifacts)), payload)
        after_failure = fc.read_state(self.control, state["logical_chain_id"])
        self.assertEqual(after_failure["stages"]["api"]["status"], "verified")
        self.assertEqual(after_failure["dispatch_reservation"]["status"], "failed")

        def retry_dispatch(args, lane, message):
            retry_row = self.row("aa" * 16, lane)
            Path(retry_row["output_root"]).mkdir(parents=True)
            fc.before_dispatch_bind(self.host, args, retry_row)
            self.host.calls.append((lane, str(message), dict(fc.os.environ), retry_row))
            return retry_row
        self.host.invoke_codex_lane = retry_dispatch
        retry = fc.advance(self.host, self.args, dict(row, artifacts=str(artifacts)), payload)
        latest = fc.read_state(self.control, state["logical_chain_id"])
        self.assertEqual(retry["next_repo"], "goodword-mcp")
        self.assertEqual(latest["current_run"]["repo"], "goodword-mcp")
        self.assertEqual(len(self.host.calls), original_calls + 1)

    def test_dead_owner_dispatch_reservation_is_reclaimed_once(self):
        launched = fc.launch(self.host, self.args, ["api", "goodword-mcp"])
        state = fc.approve_plan(self.control, launched["state"]["logical_chain_id"], self.plan())
        row = self.row("b1" * 16)
        fc.bind_phase_run(self.control, state["logical_chain_id"], phase="implement", repo="api", row=row)
        artifacts = self.result_artifacts(row, "api")
        def fail_dispatch(args, lane, message):
            raise fc.FeatureChainError("dispatch failed")
        self.host.invoke_codex_lane = fail_dispatch
        payload = {
            "state": "terminal",
            "status": "completed",
            "artifacts": str(artifacts),
            "feature_chain": {"logical_chain_id": state["logical_chain_id"], "repo": "api"},
        }
        with self.assertRaisesRegex(fc.FeatureChainError, "dispatch failed"):
            fc.advance(self.host, self.args, dict(row, artifacts=str(artifacts)), payload)
        reserved = fc.read_state(self.control, state["logical_chain_id"])
        reserved["dispatch_reservation"].update({
            "status": "dispatching",
            "owner_pid": 99999999,
            "owner_fingerprint": "definitely-dead",
        })
        fc.write_state(self.control, reserved)

        def retry_dispatch(args, lane, message):
            retry_row = self.row("b2" * 16, lane)
            Path(retry_row["output_root"]).mkdir(parents=True)
            fc.before_dispatch_bind(self.host, args, retry_row)
            self.host.calls.append((lane, str(message), dict(fc.os.environ), retry_row))
            return retry_row
        self.host.invoke_codex_lane = retry_dispatch
        retry = fc.advance(self.host, self.args, dict(row, artifacts=str(artifacts)), payload)

        latest = fc.read_state(self.control, state["logical_chain_id"])
        self.assertEqual(retry["next_repo"], "goodword-mcp")
        self.assertEqual(latest["current_run"]["run_id"], "b2" * 16)

    def test_exhausted_budget_blocks_dispatch_without_invoking_child_or_resetting_state(self):
        launched = fc.launch(self.host, self.args, ["api", "goodword-mcp"])
        state = fc.approve_plan(self.control, launched["state"]["logical_chain_id"], self.plan())
        row = self.row("c1" * 16)
        fc.bind_phase_run(self.control, state["logical_chain_id"], phase="implement", repo="api", row=row)
        artifacts = self.result_artifacts(row, "api")
        original_calls = len(self.host.calls)
        self.host.invoke_codex_lane = lambda args, lane, message: self.fail("budget-exhausted dispatch must not invoke")
        payload = {
            "state": "terminal",
            "status": "completed",
            "artifacts": str(artifacts),
            "feature_chain": {"logical_chain_id": state["logical_chain_id"], "repo": "api"},
        }

        with mock.patch.object(fc, "budget_require_remaining", side_effect=fc.FeatureChainError("feature shared token budget is exhausted")):
            with self.assertRaisesRegex(fc.FeatureChainError, "token budget"):
                fc.advance(self.host, self.args, dict(row, artifacts=str(artifacts)), payload)

        latest = fc.read_state(self.control, state["logical_chain_id"])
        self.assertEqual(len(self.host.calls), original_calls)
        self.assertEqual(latest["stages"]["api"]["status"], "verified")
        self.assertEqual(latest["dispatch_reservation"]["status"], "failed")
        self.assertEqual(latest["dispatch_reservation"]["repo"], "goodword-mcp")
        self.assertIsNone(latest.get("current_run"))

    def test_exhausted_budget_blocks_resume_control_without_pending_claim(self):
        launched = fc.launch(self.host, self.args, ["api", "goodword-mcp"])
        state = fc.approve_plan(self.control, launched["state"]["logical_chain_id"], self.plan())
        row = self.row("c2" * 16)
        running = fc.bind_phase_run(self.control, state["logical_chain_id"], phase="implement", repo="api", row=row)
        control = fc.bind_run_control_payload(self.control, running, "api", row, "implement")
        args = Namespace(**vars(self.args), action="resume", token="token")

        with mock.patch.object(fc, "budget_require_remaining", side_effect=fc.FeatureChainError("feature shared wall budget is exhausted")):
            with self.assertRaisesRegex(fc.FeatureChainError, "wall budget"):
                fc.before_control(self.host, args, row, {"feature_chain": control})

        self.assertIsNone(fc.read_state(self.control, state["logical_chain_id"])["pending_control"])

    def test_prepare_worktrees_rolls_back_only_new_clean_worktrees_on_partial_failure(self):
        chain_id = "1234567890abcdef1234567890abcdef"
        slug = "feature"
        baselines = fc.capture_baselines(self.host, ["api", "goodword-mcp"])
        existing = self.root / "goodword-mcp" / ".worktrees" / f"{slug}-{chain_id[:8]}"
        existing.mkdir(parents=True)

        with self.assertRaisesRegex(fc.FeatureChainError, "already exists"):
            fc.prepare_worktrees(self.host, chain_id, ["api", "goodword-mcp"], baselines, slug)

        api_worktree = self.root / "api" / ".worktrees" / f"{slug}-{chain_id[:8]}"
        self.assertFalse(api_worktree.exists())
        self.assertTrue(existing.exists())
        branch = f"archon/{slug}-{chain_id[:8]}-api"
        self.assertEqual(git(self.root / "api", "branch", "--list", branch), "")

    def test_failed_stage_preserves_current_run_for_resume(self):
        launched = fc.launch(self.host, self.args, ["api", "goodword-mcp"])
        state = fc.approve_plan(self.control, launched["state"]["logical_chain_id"], self.plan())
        row = self.row("a1" * 16)
        fc.bind_phase_run(self.control, state["logical_chain_id"], phase="implement", repo="api", row=row)

        advanced = fc.advance(self.host, self.args, row, {
            "state": "terminal",
            "status": "failed",
            "feature_chain": {"logical_chain_id": state["logical_chain_id"], "repo": "api"},
        })

        latest = fc.read_state(self.control, state["logical_chain_id"])
        self.assertEqual(advanced["repo"], "api")
        self.assertEqual(latest["stages"]["api"]["status"], "failed")
        self.assertEqual(latest["current_run"]["run_id"], row["id"])

    def test_approval_rejects_spec_and_planning_artifact_drift(self):
        launched = fc.launch(self.host, self.args, ["api", "goodword-mcp"])
        state = launched["state"]
        row = launched["row"]
        artifacts = Path(row["output_root"])
        (artifacts / fc.JOINT_PLAN_ARTIFACT).write_text(json.dumps(self.plan()), encoding="utf-8")
        (artifacts / "plan.md").write_text("approved plan\n", encoding="utf-8")
        control = fc.bind_run_control_payload(self.control, state, None, row, "planning")
        approved = fc.before_control(self.host, Namespace(**vars(self.args), action="approve", token="token"), row, {"feature_chain": control})

        self.spec.write_text("# Mutated\n", encoding="utf-8")
        with self.assertRaisesRegex(fc.FeatureChainError, "spec bytes"):
            fc.verify_approval(fc.read_state(self.control, approved["logical_chain_id"]))

        self.spec.write_text("# Feature\n", encoding="utf-8")
        (artifacts / "plan.md").write_text("mutated\n", encoding="utf-8")
        with self.assertRaisesRegex(fc.FeatureChainError, "planning artifact"):
            fc.verify_approval(fc.read_state(self.control, approved["logical_chain_id"]))

    def test_prearm_failure_marks_child_failed_without_clearing_chain_authority(self):
        with sqlite3.connect(self.args.db) as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS remote_agent_workflow_runs "
                "(id TEXT, workflow_name TEXT, user_message TEXT, status TEXT, output_root TEXT, completed_at TEXT)"
            )
        launched = fc.launch(self.host, self.args, ["api", "goodword-mcp"])
        row = launched["row"]
        with sqlite3.connect(self.args.db) as con:
            con.execute(
                "INSERT INTO remote_agent_workflow_runs VALUES (?,?,?,?,?,NULL)",
                (row["id"], row["workflow_name"], row["user_message"], "running", row["output_root"]),
            )
        with fc.host_env(fc, {"ARCHON_FEATURE_CHAIN_ID": launched["state"]["logical_chain_id"]}):
            recovered = fc.prearm_failure(self.host, self.args, row, "watchdog failed before arm")

        self.assertEqual(recovered["current_run"]["run_id"], row["id"])
        self.assertEqual(recovered["last_prearm_failure"]["reason"], "watchdog failed before arm")
        with sqlite3.connect(self.args.db) as con:
            status = con.execute("SELECT status FROM remote_agent_workflow_runs WHERE id = ?", (row["id"],)).fetchone()[0]
            event = con.execute("SELECT event_type FROM remote_agent_workflow_events WHERE workflow_run_id = ?", (row["id"],)).fetchone()[0]
        self.assertEqual(status, "failed")
        self.assertEqual(event, "workflow_failed")

    def test_prearm_event_matches_stock_required_sqlite_columns(self):
        db = self.root / "stock-archon.db"

        with sqlite3.connect(db) as con:
            con.execute("CREATE TABLE remote_agent_workflow_runs (id TEXT PRIMARY KEY, status TEXT)")
            con.execute("INSERT INTO remote_agent_workflow_runs VALUES ('abcd1234', 'running')")
            con.execute("CREATE TABLE remote_agent_workflow_events (id TEXT PRIMARY KEY NOT NULL, workflow_run_id TEXT NOT NULL, event_order INTEGER NOT NULL, event_type TEXT NOT NULL, step_name TEXT, step_index INTEGER, data TEXT NOT NULL, created_at TEXT NOT NULL)")
        fc.mark_run_failed_for_prearm(db, {"id": "abcd1234"}, "arm failure")
        with sqlite3.connect(db) as con:
            self.assertEqual(con.execute("SELECT status FROM remote_agent_workflow_runs").fetchone()[0], "failed")
            event = con.execute("SELECT event_order, event_type, data FROM remote_agent_workflow_events").fetchone()
        self.assertEqual(event[:2], (1, "workflow_failed"))
        self.assertEqual(json.loads(event[2])["reason"], "arm failure")

    def test_candidate_evidence_requires_real_logs_and_positive_tests(self):
        artifacts = self.root / "proof"
        artifacts.mkdir()
        (artifacts / "verify.log").write_text("Tests: 1 passed\n", encoding="utf-8")
        for evidence, reason in (
            ([{"status": "passed", "tests_passed": 1}], "no log"),
            ([{"status": "passed", "log": "missing.log", "tests_passed": 1}], "missing"),
            ([{"status": "passed", "log": "verify.log", "tests_passed": 0}], "zero tests"),
            ([{"status": "skipped", "log": "verify.log", "tests_passed": 1}], "did not pass"),
        ):
            with self.subTest(reason=reason), self.assertRaisesRegex(fc.FeatureChainError, reason):
                fc.require_verification_evidence(artifacts, "api", evidence)

    def test_interface_artifact_drift_is_rejected(self):
        artifacts = self.root / "proof"
        artifacts.mkdir()
        (artifacts / "contract.json").write_text('{"changed":true}\n', encoding="utf-8")
        with self.assertRaisesRegex(fc.FeatureChainError, "digest changed"):
            fc.verify_interface_artifacts(artifacts, [{"path": "contract.json", "sha256": "ca3d163bab055381827226140568f3bef7eaac187cebd76878e0b63e9e442356"}])

    def test_terminal_unapproved_planning_restarts_without_new_chain_or_budget(self):
        launched = fc.launch(self.host, self.args, ["api", "goodword-mcp"])
        state, row = launched["state"], dict(launched["row"], status="completed")
        legacy_attempt = {"run_id": "f" * 32, "spec_sha256": "legacy-spec"}
        state["planning_attempts"] = [legacy_attempt]
        state = fc.write_state(self.control, state)
        artifacts = Path(row["output_root"])
        (artifacts / fc.JOINT_PLAN_ARTIFACT).write_text(json.dumps(self.plan()), encoding="utf-8")
        (artifacts / "plan.md").write_text("initial plan\n", encoding="utf-8")
        (artifacts / "docreview.diff").write_text("--- old\n+++ new\n", encoding="utf-8")
        round_dir = artifacts / "plan-round-4"
        round_dir.mkdir()
        (round_dir / "critique.json").write_text('{"verdict":"REJECT"}\n', encoding="utf-8")
        (round_dir / "revision.json").write_text('{"action":"REVISE"}\n', encoding="utf-8")
        control = {"feature_chain": fc.bind_run_control_payload(self.control, state, None, row, "planning")}
        restarted = fc.restart_planning(self.host, self.args, row, control)
        self.assertEqual(restarted["state"]["logical_chain_id"], state["logical_chain_id"])
        self.assertEqual(restarted["state"]["budget"], state["budget"])
        self.assertEqual(restarted["state"]["worktrees"], state["worktrees"])
        self.assertEqual(restarted["state"]["planning_generation"], 1)
        self.assertIsNone(restarted["state"]["approval"])
        self.assertIsNone(restarted["state"]["approved_plan"])
        self.assertEqual(len(restarted["state"]["planning_attempts"]), 2)
        self.assertEqual(restarted["state"]["planning_attempts"][0], legacy_attempt)
        attempt = restarted["state"]["planning_attempts"][1]
        self.assertEqual(attempt["run_id"], row["id"])
        self.assertEqual(attempt["files"][fc.JOINT_PLAN_ARTIFACT]["content_text"], json.dumps(self.plan()))
        self.assertEqual(attempt["files"]["plan-round-4/critique.json"]["content_text"], '{"verdict":"REJECT"}\n')
        new_artifacts = Path(restarted["row"]["output_root"])
        prior = json.loads((new_artifacts / fc.PRIOR_PLANNING_EVIDENCE_ARTIFACT).read_text(encoding="utf-8"))
        request = json.loads((new_artifacts / fc.PLANNING_REQUEST_ARTIFACT).read_text(encoding="utf-8"))
        self.assertEqual(prior["attempts"][0], legacy_attempt)
        self.assertEqual(prior["attempts"][1]["files"]["plan-round-4/revision.json"]["content_text"], '{"action":"REVISE"}\n')
        self.assertEqual(request["prior_planning_evidence"]["artifact"], fc.PRIOR_PLANNING_EVIDENCE_ARTIFACT)
        self.assertEqual(request["prior_planning_evidence"]["sha256"], fc.file_digest(new_artifacts / fc.PRIOR_PLANNING_EVIDENCE_ARTIFACT))
        self.assertEqual(request["prior_planning_evidence"]["attempts"][0], legacy_attempt)
        self.assertEqual(request["prior_planning_evidence"]["attempts"][1]["evidence_sha256"], attempt["evidence_sha256"])
        self.assertNotEqual(restarted["row"]["id"], row["id"])
        with self.assertRaisesRegex(fc.FeatureChainError, "stale"):
            fc.restart_planning(self.host, self.args, row, control)

    def test_replan_dispatch_retry_does_not_duplicate_attempt_or_budget_generation(self):
        launched = fc.launch(self.host, self.args, ["api", "goodword-mcp"])
        state, row = launched["state"], dict(launched["row"], status="failed")
        artifacts = Path(row["output_root"])
        (artifacts / fc.JOINT_PLAN_ARTIFACT).write_text(json.dumps(self.plan()), encoding="utf-8")
        control = {"feature_chain": fc.bind_run_control_payload(self.control, state, None, row, "planning")}
        original_budget = state["budget"]
        original_invoke = self.host.invoke_codex_lane
        self.host.invoke_codex_lane = mock.Mock(side_effect=fc.FeatureChainError("pre-arm dispatch failed"))

        with self.assertRaisesRegex(fc.FeatureChainError, "pre-arm dispatch failed"):
            fc.restart_planning(self.host, self.args, row, control)

        failed = fc.read_state(self.control, state["logical_chain_id"])
        self.assertEqual(failed["planning_generation"], 1)
        self.assertEqual(len(failed["planning_attempts"]), 1)
        self.assertEqual(failed["budget"], original_budget)
        self.assertEqual(failed["dispatch_reservation"]["status"], "failed")
        self.assertIsNone(failed.get("current_run"))

        self.host.invoke_codex_lane = original_invoke
        retried = fc.restart_planning(self.host, self.args, row, control)

        self.assertEqual(retried["state"]["planning_generation"], 1)
        self.assertEqual(len(retried["state"]["planning_attempts"]), 1)
        self.assertEqual(retried["state"]["budget"], original_budget)

    def claude_args(self):
        return Namespace(**dict(vars(self.args), provider="claude"))

    def test_claude_state_and_lane_defaults(self):
        launched = fc.launch(self.host, self.claude_args(), ["api", "goodword-mcp"])
        state = fc.read_state(self.control, launched["state"]["logical_chain_id"])
        self.assertEqual(state["provider"], "claude")
        self.assertEqual(self.host.calls[0][0], "full-sdlc-api")
        self.assertEqual(self.host.calls[0][2]["ARCHON_FEATURE_PROVIDER"], "claude")
        self.assertEqual(fc.planning_lane(self.args, state), "full-sdlc-api")
        self.assertEqual(fc.repository_lane(self.args, state, "goodword-mcp"), "full-sdlc-api")
        self.assertEqual(fc.repository_lane(self.args, state, "web-app"), "full-sdlc-web")
        self.assertEqual(fc.integration_lane(self.args, state), "full-sdlc-api")
        codex_state = dict(state, provider="codex")
        self.assertEqual(fc.planning_lane(self.args, codex_state), "full-sdlc-api-codex")
        self.assertEqual(fc.repository_lane(self.args, codex_state, "web-app"), "full-sdlc-web-codex")

    def test_unknown_provider_is_rejected_before_worktrees(self):
        with self.assertRaisesRegex(fc.FeatureChainError, "unsupported for provider=gemini"):
            fc.make_initial_state(self.host, Namespace(**dict(vars(self.args), provider="gemini")), ["api", "goodword-mcp"])
        self.assertFalse((self.root / "api" / ".worktrees").exists())

    def test_advance_unguarded_seals_completed_planning_from_artifacts_then_dispatches(self):
        args = self.claude_args()
        launched = fc.launch(self.host, args, ["api", "goodword-mcp"])
        state, row = launched["state"], launched["row"]
        artifacts = Path(row["output_root"])
        (artifacts / fc.JOINT_PLAN_ARTIFACT).write_text(json.dumps(self.plan()), encoding="utf-8")
        (artifacts / "plan.md").write_text("approved plan\n", encoding="utf-8")
        result = {"state": "terminal", "status": "completed", "artifacts": str(artifacts),
                  "feature_chain": {"logical_chain_id": state["logical_chain_id"], "phase": "planning"}}

        advanced = fc.advance_unguarded(self.host, args, row, result)

        sealed = fc.read_state(self.control, state["logical_chain_id"])
        self.assertEqual(sealed["approval"]["source_artifacts"]["root"], str(artifacts))
        self.assertIn("plan.md", sealed["approval"]["source_artifacts"]["files"])
        self.assertEqual(sealed["dependency_order"], ["api", "goodword-mcp"])
        self.assertEqual(advanced["next_repo"], "api")
        self.assertEqual(self.host.calls[-1][0], "full-sdlc-api")
        self.assertEqual(self.host.calls[-1][2]["ARCHON_FEATURE_PHASE"], "implement")
        self.assertEqual(self.host.calls[-1][2]["ARCHON_FEATURE_REPO"], "api")
        self.assertEqual(sealed["current_run"]["repo"], "api")

    def test_claude_replan_without_token_restarts_planning_on_the_same_chain(self):
        # Run f07acb10: a Claude planning node was recorded completed with no
        # plan.md. Resume never re-runs a completed AI node and a Claude launch
        # has no control token, so feature-replan was unreachable.
        args = self.claude_args()
        launched = fc.launch(self.host, args, ["api", "goodword-mcp"])
        state, row = launched["state"], dict(launched["row"], status="failed")
        restarted = fc.restart_planning_unguarded(self.host, args, row, state["logical_chain_id"])
        self.assertEqual(restarted["state"]["logical_chain_id"], state["logical_chain_id"])
        self.assertEqual(restarted["state"]["worktrees"], state["worktrees"])
        self.assertEqual(restarted["state"]["planning_generation"], 1)
        self.assertNotEqual(restarted["row"]["id"], row["id"])
        self.assertEqual(self.host.calls[-1][0], "full-sdlc-api")
        self.assertEqual(self.host.calls[-1][2]["ARCHON_FEATURE_PHASE"], "planning")
        with self.assertRaisesRegex(fc.FeatureChainError, "stale"):
            fc.restart_planning_unguarded(self.host, args, row, state["logical_chain_id"])

    def test_replan_guidance_is_hashed_into_state_and_handed_to_the_planner(self):
        args = self.claude_args()
        launched = fc.launch(self.host, args, ["api", "goodword-mcp"])
        state, row = launched["state"], dict(launched["row"], status="failed")
        guidance = self.root / "guidance.md"
        guidance.write_text("The week window must look ahead, not back.\n", encoding="utf-8")
        expected_sha = hashlib.sha256(guidance.read_bytes()).hexdigest()
        restarted = fc.restart_planning_unguarded(
            self.host, Namespace(**dict(vars(args), guidance_file=str(guidance))), row, state["logical_chain_id"])
        sealed = fc.read_state(self.control, state["logical_chain_id"])
        self.assertEqual(expected_sha, sealed["operator_guidance"]["sha256"])
        self.assertEqual(1, sealed["operator_guidance"]["planning_generation"])
        artifacts = Path(restarted["row"]["output_root"])
        self.assertEqual(guidance.read_bytes(), (artifacts / fc.OPERATOR_GUIDANCE_ARTIFACT).read_bytes())
        request = json.loads((artifacts / fc.PLANNING_REQUEST_ARTIFACT).read_text(encoding="utf-8"))
        self.assertEqual(expected_sha, request["operator_guidance"]["sha256"])
        self.assertEqual(fc.OPERATOR_GUIDANCE_ARTIFACT, request["operator_guidance"]["artifact"])
        # A later replan without the flag keeps steering by the same guidance.
        guidance.write_text("edited after the fact\n", encoding="utf-8")
        again = fc.restart_planning_unguarded(self.host, args, dict(restarted["row"], status="failed"),
                                              state["logical_chain_id"])
        carried = Path(again["row"]["output_root"]) / fc.OPERATOR_GUIDANCE_ARTIFACT
        self.assertEqual("The week window must look ahead, not back.\n", carried.read_text(encoding="utf-8"))
        self.assertIn(fc.OPERATOR_GUIDANCE_ARTIFACT, fc.PLANNING_SUPPORT_ARTIFACTS)

    def test_replan_without_guidance_writes_none(self):
        args = self.claude_args()
        launched = fc.launch(self.host, args, ["api", "goodword-mcp"])
        state, row = launched["state"], dict(launched["row"], status="failed")
        restarted = fc.restart_planning_unguarded(self.host, args, row, state["logical_chain_id"])
        artifacts = Path(restarted["row"]["output_root"])
        self.assertFalse((artifacts / fc.OPERATOR_GUIDANCE_ARTIFACT).exists())
        request = json.loads((artifacts / fc.PLANNING_REQUEST_ARTIFACT).read_text(encoding="utf-8"))
        self.assertIsNone(request["operator_guidance"])

    def test_unusable_guidance_file_refuses_before_touching_the_chain(self):
        args = self.claude_args()
        launched = fc.launch(self.host, args, ["api", "goodword-mcp"])
        state, row = launched["state"], dict(launched["row"], status="failed")
        empty = self.root / "empty.md"
        empty.write_text("  \n", encoding="utf-8")
        calls = len(self.host.calls)
        for path, message in ((empty, "empty"), (self.root / "missing.md", "unreadable")):
            with self.subTest(path=path.name), self.assertRaisesRegex(fc.FeatureChainError, message):
                fc.restart_planning_unguarded(
                    self.host, Namespace(**dict(vars(args), guidance_file=str(path))), row, state["logical_chain_id"])
        after = fc.read_state(self.control, state["logical_chain_id"])
        self.assertEqual(0, after.get("planning_generation", 0))
        self.assertNotIn("operator_guidance", after)
        self.assertEqual(calls, len(self.host.calls))

    def test_claude_replan_refuses_codex_chains_and_approved_work(self):
        launched = fc.launch(self.host, self.args, ["api", "goodword-mcp"])
        row = dict(launched["row"], status="failed")
        with self.assertRaisesRegex(fc.FeatureChainError, "require --token"):
            fc.restart_planning_unguarded(self.host, self.args, row, launched["state"]["logical_chain_id"])
        args = self.claude_args()
        launched = fc.launch(self.host, args, ["api", "goodword-mcp"])
        state, row = launched["state"], launched["row"]
        artifacts = Path(row["output_root"])
        (artifacts / fc.JOINT_PLAN_ARTIFACT).write_text(json.dumps(self.plan()), encoding="utf-8")
        fc.advance_unguarded(self.host, args, row, {
            "state": "terminal", "status": "completed", "artifacts": str(artifacts),
            "feature_chain": {"logical_chain_id": state["logical_chain_id"], "phase": "planning"}})
        with self.assertRaisesRegex(fc.FeatureChainError, "planning run|stale|approved"):
            fc.restart_planning_unguarded(self.host, args, dict(row, status="completed"), state["logical_chain_id"])

    def test_advance_unguarded_rejects_non_terminal_planning_and_seals_nothing(self):
        args = self.claude_args()
        launched = fc.launch(self.host, args, ["api", "goodword-mcp"])
        state, row = launched["state"], launched["row"]
        artifacts = Path(row["output_root"])
        (artifacts / fc.JOINT_PLAN_ARTIFACT).write_text(json.dumps(self.plan()), encoding="utf-8")
        calls_before = len(self.host.calls)
        for result in ({"state": "gate", "status": "paused"}, {"state": "terminal", "status": "failed"}):
            with self.subTest(result=result), self.assertRaisesRegex(fc.FeatureChainError, "must complete"):
                fc.advance_unguarded(self.host, args, row, dict(
                    result, feature_chain={"logical_chain_id": state["logical_chain_id"], "phase": "planning"}))
        self.assertIsNone(fc.read_state(self.control, state["logical_chain_id"])["approval"])
        self.assertEqual(len(self.host.calls), calls_before)

    def test_budget_usage_requires_sessions_only_for_codex(self):
        seen = {}
        def fake_run_budget(args, *argv):
            seen[args.provider] = argv
            return "{}"
        with mock.patch.object(fc, "run_budget", fake_run_budget):
            for provider in ("codex", "claude"):
                args = Namespace(**dict(vars(self.args), provider=provider))
                state = fc.write_state(self.control, fc.make_initial_state(self.host, args, ["api", "goodword-mcp"]))
                fc.budget_usage(args, state["logical_chain_id"])
        self.assertIn("--require-sessions", seen["codex"])
        self.assertNotIn("--require-sessions", seen["claude"])

    def test_advance_unguarded_refuses_codex_chains(self):
        launched = fc.launch(self.host, self.args, ["api", "goodword-mcp"])
        state, row = launched["state"], launched["row"]
        with self.assertRaisesRegex(fc.FeatureChainError, "only for claude"):
            fc.advance_unguarded(self.host, self.args, row, {
                "state": "terminal", "status": "completed",
                "feature_chain": {"logical_chain_id": state["logical_chain_id"], "phase": "planning"}})


if __name__ == "__main__":
    unittest.main()
