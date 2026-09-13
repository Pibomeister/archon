import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


WRAPPER = Path(__file__).resolve().parent.parent / "codex-workspace-wrapper.sh"


class FeatureWrapper(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.control = self.root / "control"
        self.control.mkdir(mode=0o700)
        (self.control / "feature-chains-v2").mkdir(mode=0o700)
        self.run_id = "a" * 32
        self.chain_id = "b" * 32
        self.artifacts = self.root / "artifacts" / self.run_id
        self.artifacts.mkdir(parents=True)
        self.worktree = self.workspace / "api" / ".worktrees" / "candidate"
        (self.worktree / ".git").mkdir(parents=True)
        self.other = self.workspace / "goodword-mcp"
        (self.other / ".git").mkdir(parents=True)
        self.real = self.root / "fake-codex"
        self.real.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        self.real.chmod(0o700)
        self.recorder = WRAPPER.parent / "feature-budget.py"
        self.state = {
            "schema_version": 2, "logical_chain_id": self.chain_id,
            "chain_secret": "private-" * 8, "approval": {"approved": True},
            "worktrees": {"api": {"worktree": str(self.worktree)}},
            "current_run": {"run_id": self.run_id, "phase": "planning", "repo": "api",
                            "write_roots": [str(self.worktree)]},
        }

    def seal(self):
        body = {k: v for k, v in self.state.items() if k != "state_mac"}
        self.state["state_mac"] = hmac.new(
            self.state["chain_secret"].encode(),
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256,
        ).hexdigest()
        path = self.control / "feature-chains-v2" / (self.chain_id + ".json")
        path.write_text(json.dumps(self.state))
        path.chmod(0o600)
        return path

    def invoke(self, *args):
        return subprocess.run(
            ["bash", str(WRAPPER), "exec", "--cd", str(self.other), *args],
            input=f"Write artifacts to {self.artifacts}", text=True, capture_output=True,
            env=dict(os.environ, CODEX_REAL_BIN=str(self.real),
                     CODEX_WORKSPACE_ROOT=str(self.workspace), CODEX_ARTIFACTS_BASE=str(self.artifacts.parent),
                     ARCHON_CONTROL_DIR=str(self.control), ARCHON_FEATURE_CHAIN_ID=self.chain_id,
                     ARCHON_FEATURE_BUDGET_SCRIPT=str(self.recorder), CODEX_HOME=str(self.root / "codex-home"),
                     ARCHON_FEATURE_PHASE=self.state["current_run"]["phase"],
                     ARCHON_FEATURE_SCOPE="repositories"),
        )

    def test_planner_cannot_gain_product_write_root_from_params(self):
        self.seal()
        (self.artifacts / "params.json").write_text(json.dumps({"worktree": str(self.other)}))
        result = self.invoke("--add-dir", str(self.other))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--cd\n" + str(self.artifacts), result.stdout)
        self.assertNotIn(str(self.other), result.stdout)

    def test_prior_planning_evidence_is_read_only_for_planner(self):
        self.seal()
        result = self.invoke()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(json.dumps(str(self.artifacts / "prior-planning-evidence.json")) + '= "read"', result.stdout)
        self.assertIn(json.dumps(str(self.artifacts / "AGENTS.md")) + '= "read"', result.stdout)
        self.assertIn(json.dumps(str(self.artifacts / "budget-forecast.json")) + '= "read"', result.stdout)

    def test_executor_uses_private_worktree_even_if_params_drift(self):
        self.state["current_run"]["phase"] = "implement"
        self.seal()
        (self.artifacts / "params.json").write_text(json.dumps({"worktree": str(self.other)}))
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--cd\n" + str(self.worktree), result.stdout)
        self.assertNotIn(str(self.other), result.stdout)

    def test_stale_run_cannot_reuse_new_stage_authority(self):
        self.state["current_run"]["run_id"] = "c" * 32
        self.seal()
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match artifacts", result.stderr)

    def test_modified_authority_cannot_launch(self):
        path = self.seal()
        self.state["current_run"]["phase"] = "implement"
        path.write_text(json.dumps(self.state))
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MAC mismatch", result.stderr)

    def test_execution_without_joint_approval_fails(self):
        self.state["current_run"]["phase"] = "implement"
        self.state["approval"] = None
        self.seal()
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no joint approval", result.stderr)

    def test_short_config_cannot_expand_permissions(self):
        self.seal()
        result = self.invoke("-c", 'permissions={evil={extends=":danger-full-access"}}')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("evil", result.stdout)

    def test_dotted_config_cannot_expand_permissions(self):
        self.seal()
        result = self.invoke("-c", 'permissions.archon-worker.filesystem.evil="write"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("evil", result.stdout)

    def test_repository_feature_exec_starts_fresh_without_resume_selector(self):
        self.seal()
        result = self.invoke("--model", "gpt-5.6-sol", "--config", "model_reasoning_effort=\"medium\"")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--model\ngpt-5.6-sol", result.stdout)
        self.assertIn("--config\nmodel_reasoning_effort=\"medium\"", result.stdout)
        self.assertNotIn("\nresume\n", result.stdout)

    def test_repository_feature_resume_selector_is_removed_but_model_flags_remain(self):
        self.seal()
        prior = "01a09692-7455-7b03-9e1f-d232497c3f68"
        result = self.invoke("--model", "gpt-5.6-sol", "--config", "model_reasoning_effort=\"medium\"", "resume", prior)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--model\ngpt-5.6-sol", result.stdout)
        self.assertIn("--config\nmodel_reasoning_effort=\"medium\"", result.stdout)
        self.assertNotIn("\nresume\n", result.stdout)
        self.assertNotIn(prior, result.stdout)

    def test_repository_feature_option_values_named_resume_are_preserved(self):
        self.seal()
        result = self.invoke("--model", "resume", "--config", "resume")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--model\nresume", result.stdout)
        self.assertIn("--config\nresume", result.stdout)

    def test_repository_feature_unknown_resume_selector_forms_fail_closed(self):
        self.seal()
        prior = "01a09692-7455-7b03-9e1f-d232497c3f68"
        cases = [
            ("--resume", prior),
            ("--resume=" + prior,),
            ("resume",),
            ("resume", "--last"),
            ("resume", "not-a-uuid"),
            ("fork", prior),
            ("--last",),
            ("--all",),
            (prior,),
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                result = self.invoke(*argv)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("CODEX_WRAPPER=FAIL", result.stderr)

    def test_records_every_started_session_before_forwarding_event(self):
        self.seal()
        common = [sys.executable, str(self.recorder), "--control-dir", str(self.control)]
        subprocess.run([*common, "init", "--chain-id", self.chain_id], check=True, capture_output=True)
        subprocess.run([*common, "bind-run", "--chain-id", self.chain_id, "--run-id", self.run_id],
                       check=True, capture_output=True)
        session = "01999999-1111-7222-8333-444444444444"
        self.real.write_text("#!/bin/sh\nprintf '%s\\n' '" + json.dumps({"type": "thread.started", "thread_id": session}) + "'\n")
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"thread.started"', result.stdout)
        ledger = json.loads((self.control / "feature-budgets" / (self.chain_id + ".json")).read_text())
        self.assertEqual(ledger["runs"][0]["session_ids"], [session])

    def test_resume_conversion_records_new_thread_not_prior_selector(self):
        self.seal()
        common = [sys.executable, str(self.recorder), "--control-dir", str(self.control)]
        subprocess.run([*common, "init", "--chain-id", self.chain_id], check=True, capture_output=True)
        subprocess.run([*common, "bind-run", "--chain-id", self.chain_id, "--run-id", self.run_id],
                       check=True, capture_output=True)
        prior = "01a09692-7455-7b03-9e1f-d232497c3f68"
        fresh = "01999999-1111-7222-8333-444444444444"
        self.real.write_text("#!/bin/sh\nprintf '%s\\n' '" + json.dumps({"type": "thread.started", "thread_id": fresh}) + "'\n")
        result = self.invoke("--model", "gpt-5.6-sol", "resume", prior)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(prior, result.stdout)
        ledger = json.loads((self.control / "feature-budgets" / (self.chain_id + ".json")).read_text())
        self.assertEqual(ledger["runs"][0]["session_ids"], [fresh])

    def test_session_recording_failure_stops_provider_output(self):
        self.seal()
        self.recorder = self.root / "failed-recorder.py"
        self.recorder.write_text("raise SystemExit(1)\n")
        self.real.write_text("#!/bin/sh\nprintf '%s\\n' '{\"type\":\"thread.started\",\"thread_id\":\"example\"}' '{\"type\":\"turn.completed\"}'\n")
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("session registration failed", result.stderr)
        self.assertNotIn("turn.completed", result.stdout)

    def test_sandbox_freezes_routing_and_approved_artifacts(self):
        self.seal()
        (self.artifacts / "params.json").write_text("{}\n")
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        config = next(line for line in result.stdout.splitlines() if line.startswith("permissions="))
        base = ["codex", "sandbox", "-P", "archon-worker", "-c", config,
                "-C", str(self.artifacts), "--", "/bin/sh", "-c"]
        denied = subprocess.run([*base, "echo changed > params.json"], capture_output=True, text=True)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("Operation not permitted", denied.stderr)
        self.assertEqual((self.artifacts / "params.json").read_text(), "{}\n")
        allowed = subprocess.run([*base, "echo plan > plan.md"], capture_output=True, text=True)
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.state["current_run"]["phase"] = "implement"
        self.seal()
        result = self.invoke()
        config = next(line for line in result.stdout.splitlines() if line.startswith("permissions="))
        base[5] = config
        denied = subprocess.run([*base, "echo changed > plan.md"], capture_output=True, text=True)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("Operation not permitted", denied.stderr)
        self.assertEqual((self.artifacts / "plan.md").read_text(), "plan\n")


if __name__ == "__main__":
    unittest.main()
