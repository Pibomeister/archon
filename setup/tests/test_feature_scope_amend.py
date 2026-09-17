#!/usr/bin/env python3
import contextlib
import importlib.util
import io
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

ar_spec = importlib.util.spec_from_file_location("archon_run", SETUP / "archon-run.py")
assert ar_spec and ar_spec.loader
ar = importlib.util.module_from_spec(ar_spec)
ar_spec.loader.exec_module(ar)

fc_spec = importlib.util.spec_from_file_location("feature_chain", SETUP / "feature_chain.py")
assert fc_spec and fc_spec.loader
fc = importlib.util.module_from_spec(fc_spec)
fc_spec.loader.exec_module(fc)

CHAIN = "c" * 32
RUN = "d" * 32


def git(repo: Path, *argv: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *argv], capture_output=True, encoding="utf-8")
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


class FeatureScopeAmend(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.control = self.root / "control"
        self.control.mkdir(mode=0o700)
        self.db = self.root / "archon.db"
        self.spec = self.root / "feature.md"
        self.spec.write_text("# Feature\n", encoding="utf-8")
        self.output_root = self.root / "out"
        self.artifacts = self.output_root / "artifacts" / "runs" / RUN
        self.artifacts.mkdir(parents=True)
        self.planning = self.root / ".archon" / "runs" / CHAIN / "planning"
        self.planning.mkdir(parents=True)
        self.host = SimpleNamespace(
            ROOT=self.root,
            require_control_token=ar.require_control_token,
            read_control_state=ar.read_control_state,
            secure_write_json=ar.secure_write_json,
            control_state_path=ar.control_state_path,
            run_row_by_id=ar.run_row_by_id,
        )
        self.args = Namespace(
            control_dir=self.control,
            db=self.db,
            token="operator-token",
            add_file="apps/enrichment-service/template.yaml",
            reason="Allow verified condition-check permission template",
            action="feature-scope-amend",
        )
        self.row = {
            "id": RUN,
            "workflow_name": "full-sdlc-api-codex",
            "user_message": str(self.spec),
            "status": "failed",
            "output_root": str(self.output_root),
        }
        for repo in ("api", "goodword-mcp"):
            self.init_repo(repo)
        self.add_api_file("src/api.ts", "export const ok = true;\n")
        self.add_api_file("apps/enrichment-service/template.yaml", "Resources: {}\n")
        self.add_mcp_file("src/tool.ts", "export const tool = true;\n")
        self.prepare_db("failed")
        self.prepare_state()
        self.write_control()
        groups = mock.patch.object(fc.os, "killpg", side_effect=ProcessLookupError)
        groups.start()
        self.addCleanup(groups.stop)

    def init_repo(self, name: str) -> None:
        repo = self.root / name
        repo.mkdir(parents=True)
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "test@example.com")
        git(repo, "config", "user.name", "Test")
        (repo / "README.md").write_text(name + "\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "initial")

    def add_api_file(self, rel: str, text: str) -> None:
        path = self.root / "api" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        git(self.root / "api", "add", rel)
        git(self.root / "api", "commit", "-qm", f"add {rel}")

    def add_mcp_file(self, rel: str, text: str) -> None:
        path = self.root / "goodword-mcp" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        git(self.root / "goodword-mcp", "add", rel)
        git(self.root / "goodword-mcp", "commit", "-qm", f"add {rel}")

    def plan(self):
        return {
            "schema": "archon.joint-feature-plan.v1",
            "repositories": ["api", "goodword-mcp"],
            "dependency_order": ["api", "goodword-mcp"],
            "contracts": [{"producer": "api", "consumer": "goodword-mcp", "artifact": "openapi.json", "description": "fixture"}],
            "integration": {"scenarios": [{"name": "local-api-mcp", "uses": ["api", "goodword-mcp"], "commands": [{"repo": "api", "argv": ["fixture"]}], "expected_tests": ["local api mcp"]}]},
            "stages": {
                "api": {"depends_on": [], "files_allowlist": ["src/api.ts"], "test_patterns": ["api.spec.ts"], "verification": ["bun test"]},
                "goodword-mcp": {"depends_on": ["api"], "files_allowlist": ["src/tool.ts"], "test_patterns": ["tool.spec.ts"], "verification": ["pnpm test"]},
            },
        }

    def prepare_db(self, status: str) -> None:
        with sqlite3.connect(self.db) as con:
            con.execute("CREATE TABLE remote_agent_workflow_runs (id TEXT, workflow_name TEXT, user_message TEXT, status TEXT, output_root TEXT, started_at TEXT)")
            con.execute("INSERT INTO remote_agent_workflow_runs VALUES (?,?,?,?,?,?)", (RUN, self.row["workflow_name"], self.row["user_message"], status, str(self.output_root), "2026-09-13 00:00:00"))

    def prepare_state(self) -> None:
        args = Namespace(spec=str(self.spec), provider=getattr(self, "provider", "codex"), control_dir=self.control, wall_minutes=240, max_total_tokens=30_000_000)
        state = fc.make_initial_state(self.host, args, ["api", "goodword-mcp"])
        state["logical_chain_id"] = CHAIN
        state["chain_secret"] = "s" * 48
        state["spec_sha256"] = fc.file_digest(self.spec)
        state["created_at"] = "2026-09-13T00:00:00Z"
        plan = self.plan()
        (self.planning / fc.JOINT_PLAN_ARTIFACT).write_text(json.dumps(plan, sort_keys=True), encoding="utf-8")
        (self.planning / "plan.md").write_text("# Approved plan\n", encoding="utf-8")
        source = {"root": str(self.planning), "files": {fc.JOINT_PLAN_ARTIFACT: fc.file_digest(self.planning / fc.JOINT_PLAN_ARTIFACT), "plan.md": fc.file_digest(self.planning / "plan.md")}}
        state = fc.approve_plan_unlocked(self.control, state, plan, source)
        state["current_run"] = {"phase": "implement", "repo": "api", "run_id": RUN, "artifacts_dir": str(self.artifacts), "write_roots": [state["worktrees"]["api"]["worktree"]]}
        state["child_runs"] = {"api": dict(state["current_run"])}
        state["stages"]["api"]["status"] = "running"
        fc.write_state(self.control, state)
        (self.artifacts / fc.JOINT_PLAN_ARTIFACT).write_text(json.dumps(state["approved_plan"]), encoding="utf-8")
        (self.artifacts / "files-allowlist.json").write_text(json.dumps(["src/api.ts"]), encoding="utf-8")
        (self.artifacts / "candidate-inputs.json").write_text("{}\n", encoding="utf-8")
        (self.artifacts / "candidate-revisions.json").write_text(json.dumps({
            "schema": "archon.joint-candidates.v1",
            "plan_digest": state["approval"]["plan_digest"],
            "approved_plan_digest": state["approval"]["plan_digest"],
            "candidate_heads": {},
            "repositories": {},
        }), encoding="utf-8")
        (self.artifacts / "plan.md").write_text("# Approved plan\n", encoding="utf-8")

    def write_control(self, token: str = "operator-token") -> None:
        control = {
            "action": "resume",
            "run": RUN,
            "control_token_hash": ar.token_digest(token),
            "launcher_pgid": 111,
            "launcher_fingerprint": "launcher",
            "watchdog_pgid": 222,
            "watchdog_fingerprint": "watchdog",
            "wall_minutes": 240,
            "max_total_tokens": 30_000_000,
            "feature_chain": {
                "logical_chain_id": CHAIN,
                "provider": "codex",
                "scope": "repositories",
                "phase": "implement",
                "repo": "api",
                "run_id": RUN,
                "write_roots": [str(self.root / "api")],
            },
        }
        control["authority_mac"] = ar.authority_mac(token, control)
        ar.secure_write_json(ar.control_state_path(self.row, self.control), control)

    def state(self):
        return fc.read_state(self.control, CHAIN)

    def assert_retry_after_failed_amendment(self):
        pending = self.state()
        self.assertEqual("in_progress", pending["scope_amendment"]["status"])
        result = fc.scope_amend_command(self.host, self.args, self.row)
        state = self.state()
        self.assertFalse(result["already_applied"])
        self.assertIsNone(state.get("scope_amendment"))
        self.assertEqual(1, len(state["scope_amendments"]))
        self.assertEqual(1, len(state["approval_history"]))
        fc.verify_approval(state)

    def test_guarded_scope_amend_adds_only_current_repo_allowlist_and_resigns(self):
        before = self.state()
        old_approval = dict(before["approval"])
        result = fc.scope_amend_command(self.host, self.args, self.row)
        state = self.state()

        self.assertEqual(CHAIN, result["chain"])
        self.assertEqual("api", result["repo"])
        self.assertIn(self.args.add_file, state["stages"]["api"]["plan"]["files_allowlist"])
        self.assertNotIn(self.args.add_file, state["stages"]["goodword-mcp"]["plan"]["files_allowlist"])
        self.assertEqual(before["current_run"], state["current_run"])
        self.assertEqual(before["worktrees"], state["worktrees"])
        self.assertEqual(before["budget"], state["budget"])
        self.assertEqual([old_approval], state["approval_history"])
        self.assertNotEqual(old_approval["approval_digest"], state["approval"]["approval_digest"])
        fc.verify_approval(state)
        amend_root = Path(state["approval"]["source_artifacts"]["root"])
        self.assertTrue((amend_root / "original-joint-plan.json").is_file())
        self.assertIn("Guarded scope amendment", (amend_root / "plan.md").read_text(encoding="utf-8"))
        self.assertIn("Guarded scope amendment approval packet", (amend_root / "approval-packet-notice.md").read_text(encoding="utf-8"))
        self.assertEqual(["src/api.ts", self.args.add_file], json.loads((self.artifacts / "files-allowlist.json").read_text(encoding="utf-8")))
        revisions = json.loads((self.artifacts / "candidate-revisions.json").read_text(encoding="utf-8"))
        self.assertEqual(state["approval"]["plan_digest"], revisions["plan_digest"])
        self.assertEqual(state["approval"]["plan_digest"], revisions["approved_plan_digest"])
        self.assertEqual({}, revisions["candidate_heads"])
        self.assertEqual({}, revisions["repositories"])

    def test_duplicate_retry_is_idempotent(self):
        first = fc.scope_amend_command(self.host, self.args, self.row)
        second = fc.scope_amend_command(self.host, self.args, self.row)
        state = self.state()

        self.assertTrue(second["already_applied"])
        self.assertEqual(first["amendment_id"], second["amendment_id"])
        self.assertEqual(1, len(state["scope_amendments"]))

    def test_retries_matching_incomplete_journal(self):
        state = self.state()
        add_file = fc.validate_scope_add_file(state, "api", self.args.add_file)
        amendment_id = fc.digest(fc.scope_amendment_payload(CHAIN, RUN, "api", add_file, self.args.reason))
        state["scope_amendment"] = {"kind": "feature-scope-amend", "logical_chain_id": CHAIN, "run_id": RUN, "repo": "api", "add_file": add_file, "reason": self.args.reason, "amendment_id": amendment_id, "status": "in_progress", "started_at": "2026-09-13T00:00:00Z"}
        fc.write_state(self.control, state)

        result = fc.scope_amend_command(self.host, self.args, self.row)

        self.assertEqual(amendment_id, result["amendment_id"])
        self.assertIsNone(self.state().get("scope_amendment"))

    def test_stale_active_and_concurrent_controls_fail_closed(self):
        self.args.token = "wrong"
        with self.assertRaises(SystemExit):
            fc.scope_amend_command(self.host, self.args, self.row)
        self.args.token = "operator-token"
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE remote_agent_workflow_runs SET status='running' WHERE id=?", (RUN,))
        with self.assertRaisesRegex(fc.FeatureChainError, "stopped"):
            fc.scope_amend_command(self.host, self.args, self.row)
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE remote_agent_workflow_runs SET status='failed' WHERE id=?", (RUN,))
        state = self.state(); state["pending_control"] = {"owner_pid": fc.os.getpid(), "owner_fingerprint": fc.process_fingerprint()}; fc.write_state(self.control, state)
        with self.assertRaisesRegex(fc.FeatureChainError, "control"):
            fc.scope_amend_command(self.host, self.args, self.row)
        state = self.state(); state["pending_control"] = None; state["dispatch_reservation"] = {"phase": "implement"}; fc.write_state(self.control, state)
        with self.assertRaisesRegex(fc.FeatureChainError, "dispatch"):
            fc.scope_amend_command(self.host, self.args, self.row)
        state = self.state(); state["dispatch_reservation"] = None; state["budget_amendment"] = {"status": "in_progress"}; fc.write_state(self.control, state)
        with self.assertRaisesRegex(fc.FeatureChainError, "budget amendment"):
            fc.scope_amend_command(self.host, self.args, self.row)

    def test_rejects_stale_current_run_and_live_control_processes(self):
        state = self.state(); state["current_run"]["run_id"] = "e" * 32; fc.write_state(self.control, state)
        with self.assertRaisesRegex(fc.FeatureChainError, "stale"):
            fc.scope_amend_command(self.host, self.args, self.row)
        state["current_run"]["run_id"] = RUN; fc.write_state(self.control, state)
        with mock.patch.object(fc.os, "killpg", return_value=None):
            with self.assertRaisesRegex(fc.FeatureChainError, "process group is live"):
                fc.scope_amend_command(self.host, self.args, self.row)

    def test_rejects_verified_handoff_integration_publication_and_immutable_contract_changes(self):
        for key, value, message in (
            ("candidate_handoffs", {"api": {"candidate_head": "0" * 40}}, "handoffs"),
            ("integration", {"status": "locally_verified"}, "integration"),
            ("publication", {"publication_digest": "x"}, "published"),
        ):
            state = self.state(); state[key] = value; fc.write_state(self.control, state)
            with self.assertRaisesRegex(fc.FeatureChainError, message):
                fc.scope_amend_command(self.host, self.args, self.row)
            state = self.state(); state[key] = {} if key == "candidate_handoffs" else None; state.pop("status", None); fc.write_state(self.control, state)
        state = self.state(); state["approved_plan"]["contracts"][0]["artifact"] = "changed.json"; fc.write_state(self.control, state)
        with self.assertRaisesRegex(fc.FeatureChainError, "approval"):
            fc.scope_amend_command(self.host, self.args, self.row)

    def test_path_must_be_safe_tracked_current_repo_file_and_not_symlink(self):
        untracked = Path(self.state()["worktrees"]["api"]["worktree"]) / "untracked.txt"
        untracked.write_text("x\n", encoding="utf-8")
        for path, message in (("../goodword-mcp/src/tool.ts", "relative"), ("untracked.txt", "tracked"), ("src/missing.ts", "unavailable")):
            self.args.add_file = path
            with self.assertRaisesRegex(fc.FeatureChainError, message):
                fc.scope_amend_command(self.host, self.args, self.row)
        target = Path(self.state()["worktrees"]["api"]["worktree"]) / "src/api.ts"
        link = Path(self.state()["worktrees"]["api"]["worktree"]) / "apps/enrichment-service/link.yaml"
        link.symlink_to(target)
        git(Path(self.state()["worktrees"]["api"]["worktree"]), "add", "apps/enrichment-service/link.yaml")
        self.args.add_file = "apps/enrichment-service/link.yaml"
        with self.assertRaisesRegex(fc.FeatureChainError, "non-symlink"):
            fc.scope_amend_command(self.host, self.args, self.row)

    def test_incomplete_scope_amendment_blocks_dispatch(self):
        state = self.state()
        state["scope_amendment"] = {"status": "in_progress", "amendment_id": "x"}
        fc.write_state(self.control, state)

        with self.assertRaisesRegex(fc.FeatureChainError, "scope amendment"):
            fc.dispatch_lane(self.host, Namespace(control_dir=self.control), "lane", self.spec, {"ARCHON_FEATURE_SCOPE": "repositories", "ARCHON_FEATURE_CHAIN_ID": CHAIN})

    def test_missing_current_artifacts_fails_without_applied_receipt(self):
        (self.artifacts / "candidate-revisions.json").unlink()

        with self.assertRaisesRegex(fc.FeatureChainError, "candidate-revisions"):
            fc.scope_amend_command(self.host, self.args, self.row)

        state = self.state()
        self.assertIsNone(state.get("scope_amendment"))
        self.assertNotIn("scope_amendments", state)

    def test_symlink_scope_amendment_directory_is_refused_before_journal(self):
        target = self.root / "outside-amendments"
        target.mkdir()
        (self.planning / "scope-amendments").symlink_to(target)

        with self.assertRaisesRegex(fc.FeatureChainError, "symlink"):
            fc.scope_amend_command(self.host, self.args, self.row)

        self.assertIsNone(self.state().get("scope_amendment"))

    def test_pending_scope_amendment_blocks_resume_before_control_but_not_abandon(self):
        state = self.state()
        state["scope_amendment"] = {"status": "in_progress", "amendment_id": "x"}
        fc.write_state(self.control, state)
        control = ar.require_control_token(self.row, self.control, "operator-token")

        with self.assertRaisesRegex(fc.FeatureChainError, "scope amendment"):
            fc.before_control(self.host, Namespace(**{**vars(self.args), "action": "resume"}), self.row, control)

        abandoned = fc.before_control(self.host, Namespace(**{**vars(self.args), "action": "abandon"}), self.row, control)
        self.assertEqual("abandon", abandoned["pending_control"]["action"])

    def assert_crash_after_stage_json_write_retries(self, name):
        original = fc.write_json_atomic
        calls = {"raised": False}
        def flaky(path, payload):
            result = original(path, payload)
            if Path(path).parent == self.artifacts and Path(path).name == name and not calls["raised"]:
                calls["raised"] = True
                raise RuntimeError(f"crash after {name}")
            return result
        with mock.patch.object(fc, "write_json_atomic", side_effect=flaky):
            with self.assertRaisesRegex(RuntimeError, name):
                fc.scope_amend_command(self.host, self.args, self.row)
        self.assert_retry_after_failed_amendment()

    def test_crash_after_joint_plan_stage_write_retries_without_duplicate_history(self):
        self.assert_crash_after_stage_json_write_retries(fc.JOINT_PLAN_ARTIFACT)

    def test_crash_after_allowlist_stage_write_retries_without_duplicate_history(self):
        self.assert_crash_after_stage_json_write_retries("files-allowlist.json")

    def test_crash_after_candidate_revisions_stage_write_retries_without_duplicate_history(self):
        self.assert_crash_after_stage_json_write_retries("candidate-revisions.json")

    def test_crash_on_final_state_write_retries_without_nesting_or_duplicate_history(self):
        original = fc.write_state
        calls = {"count": 0}
        def flaky(control_dir, state):
            if isinstance(state.get("scope_amendment"), dict) and state["scope_amendment"].get("status") == "in_progress":
                calls["count"] += 1
            elif state.get("scope_amendment") is None and state.get("scope_amendments"):
                raise RuntimeError("crash on final state")
            return original(control_dir, state)

        with mock.patch.object(fc, "write_state", side_effect=flaky):
            with self.assertRaisesRegex(RuntimeError, "final state"):
                fc.scope_amend_command(self.host, self.args, self.row)
        packet_root = self.planning / "scope-amendments"
        roots_before = sorted(p.name for p in packet_root.iterdir())
        self.assert_retry_after_failed_amendment()
        roots_after = sorted(p.name for p in packet_root.iterdir())
        self.assertEqual(roots_before, roots_after)

    def test_parser_registers_feature_scope_amend(self):
        parsed = ar.parser().parse_args(["feature-scope-amend", RUN, "--token", "t", "--add-file", "src/api.ts", "--reason", "r"])

        self.assertEqual("feature-scope-amend", parsed.action)
        self.assertEqual("src/api.ts", parsed.add_file)


class ClaudeChainScopeAmend(unittest.TestCase):
    """feature-scope-amend --chain through ar.main(): Claude launches have no control token."""

    def setUp(self):
        self.fx = FeatureScopeAmend("test_parser_registers_feature_scope_amend")
        self.fx.provider = "claude"
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        ar.control_state_path(self.fx.row, self.fx.control).unlink()
        self.set_run("full-sdlc-api", "failed")

    def set_run(self, workflow: str, status: str) -> None:
        with sqlite3.connect(self.fx.db) as con:
            con.execute("UPDATE remote_agent_workflow_runs SET workflow_name=?, status=? WHERE id=?", (workflow, status, RUN))

    def set_provider(self, provider: str) -> None:
        state = self.fx.state()
        state["provider"] = provider
        fc.write_state(self.fx.control, state)

    def main(self, *auth: str) -> str:
        argv = ["archon-run.py", "--db", str(self.fx.db), "--control-dir", str(self.fx.control),
                "feature-scope-amend", RUN, *auth, "--add-file", self.fx.args.add_file, "--reason", self.fx.args.reason]
        out = io.StringIO()
        with mock.patch("sys.argv", argv), mock.patch.object(ar, "validate_control_location"), \
             contextlib.redirect_stdout(out):
            ar.main()
        return out.getvalue()

    def assert_refused(self, *auth: str, message: str) -> None:
        before = self.fx.state()
        out = io.StringIO()
        with mock.patch.object(ar, "fail", side_effect=SystemExit) as failed, contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit):
                self.main(*auth)
        self.assertIn(message, failed.call_args.args[0])
        after = self.fx.state()
        self.assertNotIn("scope_amendments", after)
        self.assertEqual(before["approval"], after["approval"])

    def test_claude_chain_amend_adds_file_and_refreshes_bound_artifacts(self):
        before = self.fx.state()
        out = self.main("--chain", CHAIN)
        state = self.fx.state()

        self.assertIn("ARCHON_FEATURE_SCOPE_AMEND=APPLIED", out)
        self.assertEqual(["src/api.ts", self.fx.args.add_file], state["stages"]["api"]["plan"]["files_allowlist"])
        self.assertEqual(["src/tool.ts"], state["stages"]["goodword-mcp"]["plan"]["files_allowlist"])
        self.assertEqual([before["approval"]], state["approval_history"])
        fc.verify_approval(state)
        artifacts = self.fx.artifacts
        self.assertEqual(["src/api.ts", self.fx.args.add_file], json.loads((artifacts / "files-allowlist.json").read_text()))
        revisions = json.loads((artifacts / "candidate-revisions.json").read_text())
        self.assertEqual(state["approval"]["plan_digest"], revisions["approved_plan_digest"])
        self.assertNotEqual(before["approval"]["plan_digest"], revisions["approved_plan_digest"])
        self.assertIn(self.fx.args.add_file, json.loads((artifacts / fc.JOINT_PLAN_ARTIFACT).read_text())["stages"]["api"]["files_allowlist"])
        self.assertIn("ARCHON_FEATURE_SCOPE_AMEND=UNCHANGED", self.main("--chain", CHAIN))

    def test_codex_chain_is_refused_without_its_token(self):
        self.set_provider("codex")
        self.assert_refused("--chain", CHAIN, message="codex chains require --token")
        self.set_run("full-sdlc-api-codex", "failed")
        self.fx.write_control()
        self.assert_refused(message="requires --token")

    def test_claude_lane_run_requires_the_chain_flag(self):
        self.assert_refused(message="not a guarded Codex lane")

    def test_live_run_and_live_claims_are_refused(self):
        self.set_run("full-sdlc-api", "running")
        self.assert_refused("--chain", CHAIN, message="requires a stopped run")
        self.set_run("full-sdlc-api", "failed")
        state = self.fx.state()
        state["pending_control"] = {"owner_pid": fc.os.getpid(), "owner_fingerprint": fc.process_fingerprint()}
        fc.write_state(self.fx.control, state)
        self.assert_refused("--chain", CHAIN, message="control already in progress")
        state = self.fx.state()
        state["pending_control"] = None
        state["dispatch_reservation"] = {"phase": "implement"}
        fc.write_state(self.fx.control, state)
        self.assert_refused("--chain", CHAIN, message="dispatch already in progress")

    def test_incomplete_budget_amendment_and_stale_run_are_refused(self):
        state = self.fx.state()
        state["budget_amendment"] = {"status": "in_progress"}
        fc.write_state(self.fx.control, state)
        self.assert_refused("--chain", CHAIN, message="budget amendment is incomplete")
        state = self.fx.state()
        state["budget_amendment"] = None
        state["current_run"]["run_id"] = "e" * 32
        fc.write_state(self.fx.control, state)
        self.assert_refused("--chain", CHAIN, message="stale")


class FeaturePinAmend(FeatureScopeAmend):
    """feature-pin-amend: the recovery scope-amend cannot express.

    feature-scope-amend is allowlist-only, so a PIN_BREACH on a pin the spec got
    wrong had no route but re-planning the stage. This is the same operation
    shape -- same refusal rule, same approval path, same audit row -- over the
    pinned decision's allowed_change instead of the allowlist.

    It inherits scope-amend's suite deliberately: the guards are the contract,
    and a copy of them would drift from the original the first time either moved.
    """

    PIN = "shareGroup"

    def plan(self):
        body = super().plan()
        body["pinned_decisions"] = [{
            "symbol": self.PIN,
            "file": "src/api.ts",
            "rule": "No other change to `POST /group/share`.",
            "spec_line": 12,
            "allowed_change": "none",
        }]
        return body

    def setUp(self):
        super().setUp()
        self.args.symbol = self.PIN
        self.args.allowed_change = "the managed-group sentence only"
        self.args.action = "feature-pin-amend"

    def amend(self):
        return fc.pin_amend_command(self.host, self.args, self.row)

    def pin_entry(self, state):
        return fc.plan_pin_entries(state["approved_plan"], "api")[0]

    def test_the_pin_is_rewritten_and_the_approval_is_resigned(self):
        before = self.state()
        old_approval = dict(before["approval"])
        self.assertEqual("none", self.pin_entry(before)["allowed_change"])

        result = self.amend()
        state = self.state()

        self.assertEqual((CHAIN, "api", self.PIN), (result["chain"], result["repo"], result["symbol"]))
        self.assertEqual(self.args.allowed_change, self.pin_entry(state)["allowed_change"])
        self.assertEqual([old_approval], state["approval_history"])
        self.assertNotEqual(old_approval["approval_digest"], state["approval"]["approval_digest"])
        fc.verify_approval(state)

    def test_the_plan_digest_moves_so_the_completed_review_is_invalidated(self):
        """The review identity includes the plan digest; that is the whole point."""
        before = self.state()["approval"]["plan_digest"]
        self.amend()
        self.assertNotEqual(before, self.state()["approval"]["plan_digest"])

    def test_the_stage_artifacts_are_refreshed_with_the_new_plan(self):
        self.amend()
        state = self.state()
        staged = json.loads((self.artifacts / fc.JOINT_PLAN_ARTIFACT).read_text(encoding="utf-8"))
        self.assertEqual(self.args.allowed_change,
                         fc.plan_pin_entries(staged, "api")[0]["allowed_change"])
        revisions = json.loads((self.artifacts / "candidate-revisions.json").read_text(encoding="utf-8"))
        self.assertEqual(state["approval"]["plan_digest"], revisions["plan_digest"])
        self.assertEqual(state["approval"]["plan_digest"], revisions["approved_plan_digest"])

    def test_the_approval_packet_records_the_amendment(self):
        self.amend()
        amend_root = Path(self.state()["approval"]["source_artifacts"]["root"])
        notice = json.loads((amend_root / "pin-amendment.json").read_text(encoding="utf-8"))
        self.assertEqual((self.PIN, "api"), (notice["symbol"], notice["repo"]))
        self.assertEqual(self.args.allowed_change, notice["allowed_change"])
        self.assertTrue((amend_root / "original-joint-plan.json").is_file())
        self.assertIn("Guarded pin amendment", (amend_root / "plan.md").read_text(encoding="utf-8"))

    def test_the_amendment_is_idempotent(self):
        first = self.amend()
        second = self.amend()
        state = self.state()
        self.assertTrue(second["already_applied"])
        self.assertEqual(first["amendment_id"], second["amendment_id"])
        self.assertEqual(1, len(state["pin_amendments"]))

    def test_a_matching_incomplete_journal_is_retried(self):
        state = self.state()
        amendment_id = fc.digest(fc.pin_amendment_payload(
            CHAIN, RUN, "api", self.PIN, self.args.allowed_change, self.args.reason))
        state["pin_amendment"] = {
            **fc.pin_amendment_payload(CHAIN, RUN, "api", self.PIN,
                                       self.args.allowed_change, self.args.reason),
            "amendment_id": amendment_id, "status": "in_progress",
            "started_at": "2026-09-13T00:00:00Z"}
        fc.write_state(self.control, state)

        result = self.amend()

        self.assertEqual(amendment_id, result["amendment_id"])
        self.assertIsNone(self.state().get("pin_amendment"))

    def test_a_verified_candidate_handoff_refuses_the_amendment(self):
        state = self.state()
        state["candidate_handoffs"] = {"api": {"candidate_head": "a" * 40}}
        fc.write_state(self.control, state)
        with self.assertRaisesRegex(fc.FeatureChainError, "feature-pin-amend cannot modify"):
            self.amend()

    def test_a_locally_verified_chain_refuses_the_amendment(self):
        state = self.state()
        state["status"] = "locally_verified"
        fc.write_state(self.control, state)
        with self.assertRaisesRegex(fc.FeatureChainError, "after integration"):
            self.amend()

    def test_a_symbol_the_plan_does_not_pin_is_refused(self):
        self.args.symbol = "noSuchSymbol"
        with self.assertRaisesRegex(fc.FeatureChainError, "no pinned decision for symbol"):
            self.amend()

    def test_an_amendment_that_changes_nothing_is_refused(self):
        self.args.allowed_change = "none"
        with self.assertRaisesRegex(fc.FeatureChainError, "already allows"):
            self.amend()

    def test_an_empty_allowed_change_is_refused(self):
        """`none` is the pin; an empty string records nothing at all."""
        self.args.allowed_change = "   "
        with self.assertRaisesRegex(fc.FeatureChainError, "requires --allowed-change"):
            self.amend()

    def test_a_reason_is_required(self):
        self.args.reason = ""
        with self.assertRaisesRegex(fc.FeatureChainError, "requires a reason"):
            self.amend()

    def test_an_incomplete_pin_amendment_blocks_the_next_dispatch(self):
        state = self.state()
        state["pin_amendment"] = {"status": "in_progress"}
        with self.assertRaisesRegex(fc.FeatureChainError, "pin amendment is incomplete"):
            fc.require_no_incomplete_pin_amendment(state)

    def test_the_cli_parses_the_amendment_flags(self):
        parsed = ar.parser().parse_args([
            "feature-pin-amend", RUN, "--token", "t", "--symbol", self.PIN,
            "--allowed-change", "the managed-group sentence only", "--reason", "spec was wrong"])
        self.assertEqual("feature-pin-amend", parsed.action)
        self.assertEqual(self.PIN, parsed.symbol)
        self.assertEqual("the managed-group sentence only", parsed.allowed_change)

    # The inherited scope-amend cases exercise fc.scope_amend_command, which this
    # subclass's plan() does not change; running them twice buys nothing.
    def test_guarded_scope_amend_adds_only_current_repo_allowlist_and_resigns(self):
        self.skipTest("covered by FeatureScopeAmend")

    def test_duplicate_retry_is_idempotent(self):
        self.skipTest("covered by FeatureScopeAmend")

    def test_retries_matching_incomplete_journal(self):
        self.skipTest("covered by FeatureScopeAmend")


if __name__ == "__main__":
    unittest.main()
