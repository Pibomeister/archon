#!/usr/bin/env python3
import contextlib
import importlib.util
import io
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SETUP = Path(__file__).resolve().parent.parent
SCRIPT = SETUP / "archon-run.py"
spec = importlib.util.spec_from_file_location("archon_run", SCRIPT)
assert spec and spec.loader
ar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ar)


class FakeFeatureChainError(ValueError):
    pass


class FakeRepositoryController:
    FeatureChainError = FakeFeatureChainError

    def __init__(self, *, fail_before_control: bool = False, amended_tokens: int | None = None):
        self.fail_before_control = fail_before_control
        self.amended_tokens = amended_tokens
        self.calls = []
        self.state = {
            "logical_chain_id": "a" * 32,
            "provider": "codex",
            "scope": "repositories",
            "repositories": ["api", "goodword-mcp"],
        }

    def read_state(self, _control_dir, chain_id):
        if chain_id != self.state["logical_chain_id"]:
            raise self.FeatureChainError("wrong chain")
        self.calls.append(("read_state", chain_id))
        return dict(self.state)

    def bind_run_control_payload(self, _control_dir, state, repo, row, phase):
        self.calls.append(("bind_run_control_payload", row["id"], phase, repo))
        return {
            "logical_chain_id": state["logical_chain_id"],
            "provider": state["provider"],
            "lane": row["workflow_name"],
            "scope": "repositories",
            "repo": repo,
            "phase": phase,
            "run_id": row["id"],
            "write_roots": ["/tmp/feature-write-root"],
        }

    def before_dispatch_bind(self, _host, _args, row):
        self.calls.append(("before_dispatch_bind", row["id"]))
        return dict(self.state)

    def restore_control(self, _host, row, _control_dir, control):
        self.calls.append(("restore_control", row["id"]))
        feature = control["feature_chain"]
        os.environ.update({
            "ARCHON_FEATURE_SCOPE": "repositories",
            "ARCHON_FEATURE_CHAIN_ID": feature["logical_chain_id"],
            "ARCHON_FEATURE_PROVIDER": feature["provider"],
            "ARCHON_FEATURE_PHASE": feature["phase"],
            "ARCHON_FEATURE_REPO": feature["repo"],
        })
        return dict(os.environ)

    def prearm_failure(self, _host, _args, row, reason):
        self.calls.append(("prearm_failure", row["id"], reason))
        return dict(self.state)

    def before_control(self, _host, _args, row, _control):
        self.calls.append(("before_control", row["id"]))
        if self.fail_before_control:
            raise self.FeatureChainError("repository-list feature control already in progress")
        if self.amended_tokens is not None:
            _args.max_total_tokens = self.amended_tokens
        return dict(self.state)

    def control_failed(self, _host, _args, row):
        self.calls.append(("control_failed", row["id"]))
        return dict(self.state)

    def after_control(self, _host, _args, row):
        self.calls.append(("after_control", row["id"]))
        return dict(self.state)


class FeaturePrearmRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "archon.db"
        self.control_dir = self.root / "control"
        self.codex_home = self.root / "codex-home"
        self.codex_home.mkdir(mode=0o700)
        codex_config = self.codex_home / "config.toml"
        codex_config.write_text(
            'model = "gpt-5.6-sol"\nmodel_reasoning_effort = "medium"\n',
            encoding="utf-8",
        )
        codex_config.chmod(0o600)
        self.registry = self.root / "registry.json"
        self.spec = self.root / "spec.md"
        self.spec.write_text("# Feature\n", encoding="utf-8")
        with sqlite3.connect(self.db) as con:
            con.execute(
                "CREATE TABLE remote_agent_workflow_runs "
                "(id TEXT, workflow_name TEXT, user_message TEXT, status TEXT, output_root TEXT, started_at TEXT, codebase_id TEXT)"
            )
            con.execute(
                "CREATE TABLE remote_agent_codebases "
                "(id TEXT, name TEXT, default_cwd TEXT, kind TEXT, updated_at TEXT)"
            )
            con.execute(
                "INSERT INTO remote_agent_codebases VALUES (?,?,?,?,?)",
                ("goodword-codebase", "Goodword", str(ar.ROOT), "repo", "2026-09-11 12:00:00"),
            )
        self.chain_env = {
            "ARCHON_FEATURE_SCOPE": "repositories",
            "ARCHON_FEATURE_CHAIN_ID": "a" * 32,
            "ARCHON_FEATURE_PROVIDER": "codex",
            "ARCHON_FEATURE_PHASE": "planning",
            "ARCHON_FEATURE_REPO": "joint",
            "ARCHON_FEATURE_REPOSITORIES": "api,goodword-mcp",
            "ARCHON_FEATURE_LANE": "full-sdlc-api-codex",
            "CODEX_LITE_LOG_DIR": str(self.root / "logs"),
        }

    def add_run(self, run_id="cafebabe99", *, status="running", lane="full-sdlc-api-codex"):
        output_root = self.root / "out" / run_id
        output_root.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db) as con:
            con.execute(
                "INSERT INTO remote_agent_workflow_runs VALUES (?,?,?,?,?,?,?)",
                (
                    run_id,
                    lane,
                    str(self.spec.resolve()),
                    status,
                    str(output_root),
                    "2026-09-11 12:00:00",
                    "goodword-codebase",
                ),
            )
        return {
            "id": run_id,
            "workflow_name": lane,
            "user_message": str(self.spec.resolve()),
            "status": status,
            "output_root": str(output_root),
        }

    def argv(self, *tail):
        return [
            str(SCRIPT),
            "--db", str(self.db),
            "--codex-home", str(self.codex_home),
            "--registry", str(self.registry),
            "--control-dir", str(self.control_dir),
            *tail,
        ]

    def private_control(self, row, token="prior-token"):
        data = {
            "action": "resume",
            "run": row["id"],
            "control_token_hash": ar.token_digest(token),
            "launcher_pid": 111,
            "launcher_pgid": 456,
            "launcher_fingerprint": "old-launcher-fp",
            "watchdog_pid": 222,
            "watchdog_pgid": 987,
            "watchdog_fingerprint": "old-watchdog-fp",
            "watchdog_armed": True,
            "watchdog_arm_file": str(self.root / "old.armed"),
            "wall_minutes": 240,
            "max_total_tokens": 30_000_000,
            "workflow_log": str(self.root / "old-workflow.log"),
            "watchdog_log": str(self.root / "old-watchdog.log"),
            "feature_chain": {
                "logical_chain_id": self.chain_env["ARCHON_FEATURE_CHAIN_ID"],
                "provider": "codex",
                "lane": row["workflow_name"],
                "scope": "repositories",
                "repo": "api",
                "phase": "implement",
                "run_id": row["id"],
                "write_roots": ["/tmp/feature-write-root"],
            },
        }
        data["authority_mac"] = ar.authority_mac(token, data)
        return data

    @contextlib.contextmanager
    def patched_runtime(self, argv, controller, *, wait_error=None, terminate_error=None):
        wait_error = wait_error or SystemExit(1)
        patches = [
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(ar, "DEFAULT_CONTROL_DIR", self.control_dir),
            mock.patch.object(ar, "ensure_environment"),
            mock.patch.object(ar, "stage_private_codex_skills"),
            mock.patch.object(ar, "install_private_codex_wrapper", return_value=self.root / "codex-wrapper"),
            mock.patch.object(ar, "detached", side_effect=[(123, 456), (789, 987)]),
            mock.patch.object(ar, "process_fingerprint", side_effect=["launcher-fp", "watchdog-fp"]),
            mock.patch.object(ar, "wait_for_run_id", return_value="cafebabe99"),
            mock.patch.object(ar, "wait_for_watchdog_arm", side_effect=wait_error),
            mock.patch.object(ar, "feature_repository_controller", return_value=controller),
            mock.patch.object(ar, "abandon_if_orphaned"),
            mock.patch.dict(os.environ, self.chain_env, clear=False),
        ]
        if terminate_error is None:
            patches.append(mock.patch.object(ar, "terminate_group"))
        else:
            patches.append(mock.patch.object(ar, "terminate_group", side_effect=terminate_error))
        started = [p.start() for p in patches]
        try:
            yield {
                "abandon_if_orphaned": started[-3],
                "terminate_group": started[-1],
            }
        finally:
            for p in reversed(patches):
                p.stop()

    def recoverable_token_from(self, stdout: str) -> str:
        match = re.search(r"CODEX_LITE_RUN=RECOVERABLE[^\n]*control_token=([^\s]+)", stdout)
        self.assertIsNotNone(match, stdout)
        return match.group(1)

    def test_initial_repository_prearm_failure_preserves_run_and_emits_usable_token(self):
        row = self.add_run(status="running")
        controller = FakeRepositoryController()
        stdout = io.StringIO()
        argv = self.argv("run", "full-sdlc-api-codex", str(self.spec.resolve()))
        with self.patched_runtime(argv, controller) as patched:
            with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit):
                ar.main()

        self.assertIn(("before_dispatch_bind", row["id"]), controller.calls)
        self.assertTrue(any(call[0] == "prearm_failure" and call[1] == row["id"] for call in controller.calls))
        patched["abandon_if_orphaned"].assert_not_called()
        token = self.recoverable_token_from(stdout.getvalue())
        control = ar.require_control_token(row, self.control_dir, token)
        self.assertEqual(row["id"], control["run"])
        self.assertEqual("repositories", control["feature_chain"]["scope"])
        self.assertEqual("planning", control["feature_chain"]["phase"])

    def test_repository_continuation_prearm_failure_restores_prior_token(self):
        row = self.add_run(status="failed")
        prior = self.private_control(row, "prior-token")
        ar.secure_write_json(ar.control_state_path(row, self.control_dir), prior)
        controller = FakeRepositoryController()
        stdout = io.StringIO()
        argv = self.argv("resume", row["id"], "--token", "prior-token")
        with self.patched_runtime(argv, controller) as patched:
            with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit):
                ar.main()

        self.assertIn(("before_control", row["id"]), controller.calls)
        self.assertTrue(any(call[0] == "prearm_failure" and call[1] == row["id"] for call in controller.calls))
        self.assertIn(("control_failed", row["id"]), controller.calls)
        self.assertNotIn("CODEX_LITE_RUN=RECOVERABLE", stdout.getvalue())
        restored = ar.read_control_state(row, self.control_dir)
        self.assertEqual(ar.token_digest("prior-token"), restored["control_token_hash"])
        self.assertEqual(row["id"], ar.require_control_token(row, self.control_dir, "prior-token")["run"])

    def test_repository_continuation_prearm_failure_restores_locked_amended_allowance(self):
        row = self.add_run(status="failed")
        prior = self.private_control(row, "prior-token")
        self.assertEqual(30_000_000, prior["max_total_tokens"])
        ar.secure_write_json(ar.control_state_path(row, self.control_dir), prior)
        controller = FakeRepositoryController(amended_tokens=100_000_000)
        stdout = io.StringIO()
        argv = self.argv("resume", row["id"], "--token", "prior-token")
        with self.patched_runtime(argv, controller):
            with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit):
                ar.main()

        self.assertIn(("before_control", row["id"]), controller.calls)
        restored = ar.require_control_token(row, self.control_dir, "prior-token")
        self.assertEqual(100_000_000, restored["max_total_tokens"])
        self.assertEqual(240, restored["wall_minutes"])
        self.assertNotIn("CODEX_LITE_RUN=RECOVERABLE", stdout.getvalue())

    def test_duplicate_before_control_failure_does_not_cleanup_or_overwrite_authority(self):
        row = self.add_run(status="failed")
        prior = self.private_control(row, "prior-token")
        ar.secure_write_json(ar.control_state_path(row, self.control_dir), prior)
        controller = FakeRepositoryController(fail_before_control=True)
        stdout = io.StringIO()
        argv = self.argv("resume", row["id"], "--token", "prior-token")
        with self.patched_runtime(argv, controller) as patched:
            with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit):
                ar.main()

        self.assertIn(("before_control", row["id"]), controller.calls)
        self.assertFalse(any(call[0] == "prearm_failure" for call in controller.calls))
        self.assertFalse(any(call[0] == "control_failed" for call in controller.calls))
        patched["abandon_if_orphaned"].assert_not_called()
        self.assertEqual(prior, ar.read_control_state(row, self.control_dir))
        self.assertEqual(row["id"], ar.require_control_token(row, self.control_dir, "prior-token")["run"])
        self.assertNotIn("CODEX_LITE_RUN=RECOVERABLE", stdout.getvalue())

    def test_repository_prearm_failure_does_not_claim_recovery_when_containment_fails(self):
        row = self.add_run(status="running")
        controller = FakeRepositoryController()
        stdout = io.StringIO()
        stderr = io.StringIO()
        argv = self.argv("run", "full-sdlc-api-codex", str(self.spec.resolve()))
        with self.patched_runtime(argv, controller, terminate_error=RuntimeError("kill failed")) as patched:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
                ar.main()

        self.assertIn(("before_dispatch_bind", row["id"]), controller.calls)
        self.assertFalse(any(call[0] == "prearm_failure" for call in controller.calls))
        patched["abandon_if_orphaned"].assert_not_called()
        self.assertNotIn("CODEX_LITE_RUN=RECOVERABLE", stdout.getvalue())
        self.assertIn("CODEX_LITE_RUN=CLEANUP_FAIL", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
