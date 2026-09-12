#!/usr/bin/env python3
import importlib.util
import contextlib
import io
import json
import os
import sqlite3
import subprocess
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SETUP = Path(__file__).resolve().parent.parent
SCRIPT = SETUP / "codex-lite-run.py"

spec = importlib.util.spec_from_file_location("codex_lite_run", SCRIPT)
assert spec and spec.loader
clr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(clr)


class CodexLiteRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "archon.db"
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE remote_agent_workflow_runs "
                    "(id TEXT, workflow_name TEXT, user_message TEXT, status TEXT, output_root TEXT, started_at TEXT, codebase_id TEXT)")
        con.execute("CREATE TABLE remote_agent_codebases "
                    "(id TEXT, name TEXT, default_cwd TEXT, kind TEXT, updated_at TEXT)")
        con.execute("INSERT INTO remote_agent_codebases VALUES (?,?,?,?,?)",
                    ("goodword-codebase", "Goodword", str(clr.ROOT), "repo", "2026-09-10 00:00:00"))
        con.commit(); con.close()

    def add_run(self, run_id, lane="bugfix-lite-codex", status="paused", started="2026-08-31 12:00:00"):
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO remote_agent_workflow_runs VALUES (?,?,?,?,?,?,?)",
                    (run_id, lane, "/tmp/spec.md", status, str(self.root / "out"), started, "goodword-codebase"))
        con.commit(); con.close()

    @staticmethod
    def private_control(run_id, token="valid-token", **overrides):
        data = {
            "run": run_id,
            "control_token_hash": clr.token_digest(token),
            "launcher_pgid": 456,
            "launcher_fingerprint": "launcher-fp",
            "watchdog_pgid": 987,
            "watchdog_fingerprint": "watchdog-fp",
            "wall_minutes": 90,
            "max_total_tokens": 8_000_000,
        }
        data.update(overrides)
        data["authority_mac"] = clr.authority_mac(token, data)
        return data

    def test_resolve_run_requires_unique_prefix(self):
        self.add_run("abcdef0011")
        self.add_run("abcdef0022", started="2026-08-31 12:01:00")
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                clr.resolve_run(self.db, "abcdef00")

    def test_resolve_run_accepts_guarded_full_bugfix_lane(self):
        self.add_run("cafebabe99", lane="bugfix-codex")
        self.assertEqual(clr.resolve_run(self.db, "cafebabe99")["workflow_name"], "bugfix-codex")

    def test_resolve_run_accepts_exact_lite_run(self):
        self.add_run("cafebabe99")
        row = clr.resolve_run(self.db, "cafebabe99")
        self.assertEqual(row["id"], "cafebabe99")

    def test_resolve_run_accepts_stock_uuid_and_prefixes(self):
        run_id = "21786566-8f16-4be3-904a-f89cb2768fe1"
        self.add_run(run_id)

        self.assertEqual(clr.resolve_run(self.db, run_id)["id"], run_id)
        self.assertEqual(clr.resolve_run(self.db, "21786566-8f16")["id"], run_id)

    def test_wait_for_run_id_reads_stock_uuid_from_log(self):
        log = self.root / "archon.log"
        run_id = "21786566-8f16-4be3-904a-f89cb2768fe1"
        log.write_text(json.dumps({"workflowRunId": run_id}) + "\n", encoding="utf-8")

        self.assertEqual(clr.wait_for_run_id(log, os.getpid(), timeout_s=1), run_id)

    def test_codex_artifacts_base_uses_existing_output_root(self):
        run_id = "21786566-8f16-4be3-904a-f89cb2768fe1"
        self.add_run(run_id)
        row = clr.resolve_run(self.db, run_id)

        self.assertEqual(
            clr.codex_artifacts_base(self.db, clr.ROOT, row),
            self.root / "out" / "artifacts" / "runs",
        )

    def test_codex_artifacts_base_derives_repo_local_root_from_codebase_metadata(self):
        self.assertEqual(
            clr.codex_artifacts_base(self.db, clr.ROOT),
            clr.USER_HOME / ".archon" / "workspaces" / "_local" / "Goodword" / "artifacts" / "runs",
        )

    def test_run_command_does_not_use_archon_detach(self):
        old = os.environ.get("ARCHON_BIN")
        os.environ["ARCHON_BIN"] = "/fake/archon"
        self.addCleanup(lambda: os.environ.__setitem__("ARCHON_BIN", old) if old is not None else os.environ.pop("ARCHON_BIN", None))
        cmd = clr.command_for("run", "bugfix-lite-codex\0/tmp/spec.md")
        # archon-run.py detaches the launcher itself, so it must never ask the
        # CLI to detach as well -- that was this test's original point and it
        # still holds.
        self.assertNotIn("--detach", cmd)
        # --branch is what gives the run its own working_path, and the path is
        # the ONLY thing archon locks on: without it every lane serializes and a
        # second launch self-cancels with "Workflow already active on this path"
        # (RUNBOOK 5a). The branch is derived from the spec slug, so two tickets
        # never collide and a relaunch of one ticket deliberately reuses its own.
        self.assertEqual(cmd, ["/fake/archon", "workflow", "run", "bugfix-lite-codex",
                               "--branch", "bugfix-lite-codex-spec", "/tmp/spec.md"])
        self.assertEqual(cmd[-1], "/tmp/spec.md",
                         "the spec must stay the trailing positional: the CLI stores it as "
                         "user_message, and archon-run.py's post-launch guard compares it")

    def test_private_wrapper_forces_workspace_write_on_codex_exec(self):
        real = self.root / "real-codex.sh"
        real.write_text('#!/bin/bash\nprintf "%s\\n" "$*"\n', encoding="utf-8")
        real.chmod(0o755)
        wrapper = self.root / "control/codex-workspace-wrapper.sh"
        wrapper.parent.mkdir(mode=0o700)
        wrapper.write_bytes(clr.WORKSPACE_WRAPPER.read_bytes())
        wrapper.chmod(0o500)
        for repo in ("api", "web-app"):
            (self.root / repo / ".git").mkdir(parents=True)
        env = dict(os.environ, CODEX_REAL_BIN=str(real), CODEX_WORKSPACE_ROOT=str(self.root),
                   CODEX_ARTIFACTS_BASE=str(self.root / "artifacts/runs"))
        result = subprocess.run(
            [str(wrapper), "exec", "--cd", str(self.root / "api"), "--experimental-json"],
            capture_output=True, encoding="utf-8", env=env,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"exec --cd {self.root.resolve()}/api ", result.stdout)
        self.assertIn('default_permissions="archon-worker"', result.stdout)
        self.assertIn('extends=":workspace"', result.stdout)
        self.assertIn('network={enabled=false}', result.stdout)
        self.assertNotIn("danger-full-access", result.stdout)

    def test_private_wrapper_replaces_adapter_sandbox_override(self):
        real = self.root / "real-codex.sh"
        real.write_text('#!/bin/bash\nprintf "%s\\n" "$*"\n', encoding="utf-8")
        real.chmod(0o755)
        for repo in ("api", "web-app"):
            (self.root / repo / ".git").mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, CODEX_REAL_BIN=str(real), CODEX_WORKSPACE_ROOT=str(self.root),
                   CODEX_ARTIFACTS_BASE=str(self.root / "artifacts/runs"))
        result = subprocess.run(
            [str(clr.WORKSPACE_WRAPPER), "exec", "--cd", str(self.root / "api"), "--sandbox", "danger-full-access",
             "--config", "sandbox_workspace_write.network_access=true"],
            capture_output=True, encoding="utf-8", env=env,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"exec --cd {self.root.resolve()}/api ", result.stdout)
        self.assertIn('default_permissions="archon-worker"', result.stdout)
        self.assertIn('extends=":workspace"', result.stdout)
        self.assertIn('network={enabled=false}', result.stdout)
        self.assertNotIn("danger-full-access", result.stdout)

    def test_private_wrapper_forwards_gitnexus_pin_into_the_mcp_server_env(self):
        real = self.root / "real-codex.sh"
        real.write_text('#!/bin/bash\nprintf "%s\\n" "$*"\n', encoding="utf-8")
        real.chmod(0o755)
        for repo in ("api", "web-app"):
            (self.root / repo / ".git").mkdir(parents=True, exist_ok=True)
        index = self.root / "gitnexus-index"
        chain_state = self.root / "chain.json"
        env = dict(os.environ, CODEX_REAL_BIN=str(real), CODEX_WORKSPACE_ROOT=str(self.root),
                   CODEX_ARTIFACTS_BASE=str(self.root / "artifacts/runs"),
                   ARCHON_GITNEXUS_INDEX=str(index), ARCHON_GITNEXUS_COMMIT="c" * 40,
                   ARCHON_BUGFIX_CHAIN_ID="d" * 32, ARCHON_BUGFIX_CHAIN_STATE=str(chain_state))
        result = subprocess.run(
            [str(clr.WORKSPACE_WRAPPER), "exec", "--cd", str(self.root / "api")],
            capture_output=True, encoding="utf-8", env=env,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for key, value in (("ARCHON_GITNEXUS_INDEX", str(index)),
                           ("ARCHON_GITNEXUS_COMMIT", "c" * 40),
                           ("ARCHON_BUGFIX_CHAIN_ID", "d" * 32),
                           ("ARCHON_BUGFIX_CHAIN_STATE", str(chain_state))):
            self.assertIn(f'--config mcp_servers.gitnexus.env.{key}="{value}"', result.stdout)

    def test_private_wrapper_omits_gitnexus_mcp_env_without_a_pin(self):
        real = self.root / "real-codex.sh"
        real.write_text('#!/bin/bash\nprintf "%s\\n" "$*"\n', encoding="utf-8")
        real.chmod(0o755)
        for repo in ("api", "web-app"):
            (self.root / repo / ".git").mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("ARCHON_GITNEXUS_", "ARCHON_BUGFIX_CHAIN_"))}
        env.update(CODEX_REAL_BIN=str(real), CODEX_WORKSPACE_ROOT=str(self.root),
                   CODEX_ARTIFACTS_BASE=str(self.root / "artifacts/runs"))
        result = subprocess.run(
            [str(clr.WORKSPACE_WRAPPER), "exec", "--cd", str(self.root / "api")],
            capture_output=True, encoding="utf-8", env=env,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("mcp_servers.gitnexus.env", result.stdout)

    def test_private_wrapper_adds_only_the_prompt_bound_run_artifacts(self):
        real = self.root / "real-codex.sh"
        real.write_text('#!/bin/bash\nprintf "%s\\n" "$*"\ncat >/dev/null\n', encoding="utf-8")
        real.chmod(0o755)
        for repo in ("api", "web-app"):
            (self.root / repo / ".git").mkdir(parents=True, exist_ok=True)
        base = self.root / "artifacts/runs"
        run_dir = base / ("a" * 32)
        run_dir.mkdir(parents=True)
        (run_dir / "params.json").write_text(json.dumps({"worktree": str(self.root / "api")}))
        env = dict(os.environ, CODEX_REAL_BIN=str(real), CODEX_WORKSPACE_ROOT=str(self.root),
                   CODEX_ARTIFACTS_BASE=str(base))
        result = subprocess.run(
            [str(clr.WORKSPACE_WRAPPER), "exec", "--cd", str(self.root / "api"), "--experimental-json"],
            input=f"Write evidence to {run_dir}/evidence.json", capture_output=True,
            encoding="utf-8", env=env,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"--add-dir {run_dir}", result.stdout)

    def test_private_wrapper_refuses_multiple_run_artifact_roots(self):
        real = self.root / "real-codex.sh"
        real.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        real.chmod(0o755)
        for repo in ("api", "web-app"):
            (self.root / repo / ".git").mkdir(parents=True, exist_ok=True)
        base = self.root / "artifacts/runs"
        env = dict(os.environ, CODEX_REAL_BIN=str(real), CODEX_WORKSPACE_ROOT=str(self.root),
                   CODEX_ARTIFACTS_BASE=str(base))
        result = subprocess.run(
            [str(clr.WORKSPACE_WRAPPER), "exec", "--cd", str(self.root / "api")],
            input=f"{base / ('a' * 32)} {base / ('b' * 32)}",
            capture_output=True, encoding="utf-8", env=env,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("multiple run artifact roots", result.stderr)

    def test_private_wrapper_uses_recorded_worktree_without_api_or_web(self):
        worktree = self.root / "arbitrary-repo/.worktrees/ticket with spaces"
        worktree.mkdir(parents=True)
        (worktree / ".git").write_text("gitdir: /unused/metadata\n")
        base = self.root / "artifacts/runs"
        artifacts = base / "21786566-8f16-4be3-904a-f89cb2768fe1"
        artifacts.mkdir(parents=True)
        (artifacts / "params.json").write_text(json.dumps({"worktree": str(worktree)}))
        real = self.root / "real-codex.py"
        real.write_text("#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
        real.chmod(0o755)
        env = dict(os.environ, CODEX_REAL_BIN=str(real), CODEX_WORKSPACE_ROOT=str(self.root),
                   CODEX_ARTIFACTS_BASE=str(base))
        result = subprocess.run(
            [str(clr.WORKSPACE_WRAPPER), "exec", "--cd", str(self.root),
             "--add-dir", str(self.root.parent)], input=f"Read {artifacts}/params.json",
            capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads(result.stdout)
        self.assertEqual(argv[:5], ["exec", "--cd", str(worktree.resolve()),
                                  "--config", 'default_permissions="archon-worker"'])
        self.assertEqual(argv[-2:], ["--add-dir", str(artifacts)])
        self.assertEqual(argv.count("--add-dir"), 1)
        self.assertIn('extends=":workspace"', argv[6])
        self.assertIn('network={enabled=false}', argv[6])
        resumed = subprocess.run(
            [str(clr.WORKSPACE_WRAPPER), "exec", "--cd", str(self.root), "resume", "thread-id"],
            input="", capture_output=True, text=True,
            env=dict(env, CODEX_RUN_ARTIFACTS=str(artifacts)))
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(json.loads(resumed.stdout)[1:3], ["--cd", str(worktree.resolve())])

    def test_private_wrapper_before_bootstrap_writes_only_run_artifacts(self):
        artifacts = self.root / "artifacts/runs" / ("b" * 32)
        artifacts.mkdir(parents=True)
        (artifacts / "params.json").write_text(json.dumps({
            "worktree": str(self.root / "other-repo/.worktrees/not-created")}))
        real = self.root / "real-codex.py"
        real.write_text("#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
        real.chmod(0o755)
        env = dict(os.environ, CODEX_REAL_BIN=str(real), CODEX_WORKSPACE_ROOT=str(self.root),
                   CODEX_ARTIFACTS_BASE=str(artifacts.parent))
        result = subprocess.run([str(clr.WORKSPACE_WRAPPER), "exec"],
                                input=f"Read {artifacts}/params.json", capture_output=True,
                                text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads(result.stdout)
        self.assertEqual(argv[:5], ["exec", "--cd", str(artifacts.resolve()),
                                  "--config", 'default_permissions="archon-worker"'])
        self.assertEqual(argv[-2:], ["--add-dir", str(artifacts)])
        self.assertEqual(argv.count("--add-dir"), 1)
        self.assertIn('extends=":workspace"', argv[6])
        self.assertIn('network={enabled=false}', argv[6])

        self.assertIn("--skip-git-repo-check", argv)

        (artifacts / "bootstrap-head.txt").write_text("a" * 40)
        result = subprocess.run([str(clr.WORKSPACE_WRAPPER), "exec"],
                                input=f"Read {artifacts}/params.json", capture_output=True,
                                text=True, env=env)
        self.assertEqual(result.returncode, 2)
        self.assertIn("selected worktree missing after bootstrap", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_private_wrapper_rejects_outside_worktree_and_workspace_root(self):
        real = self.root / "real-codex.sh"
        real.write_text("#!/bin/sh\necho SHOULD_NOT_LAUNCH\n")
        real.chmod(0o755)
        env = dict(os.environ, CODEX_REAL_BIN=str(real), CODEX_WORKSPACE_ROOT=str(self.root),
                   CODEX_ARTIFACTS_BASE=str(self.root / "artifacts/runs"))
        for target in (self.root, self.root.parent):
            with self.subTest(target=target):
                result = subprocess.run([str(clr.WORKSPACE_WRAPPER), "exec", "--cd", str(target)],
                                        input="", capture_output=True, text=True, env=env)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("SHOULD_NOT_LAUNCH", result.stdout)
                self.assertIn("CODEX_WRAPPER=FAIL", result.stderr)

    @unittest.skipUnless(sys.platform == "darwin", "native macOS sandbox probe")
    def test_private_wrapper_permissions_deny_controller_read_and_signal(self):
        worktree = self.root / "repo"
        (worktree / ".git").mkdir(parents=True)
        control = self.root / "control"
        control.mkdir(mode=0o700)
        secret = control / "synthetic.txt"
        secret.write_text("SYNTHETIC_ONLY")
        real = self.root / "capture.py"
        real.write_text("#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
        real.chmod(0o755)
        env = dict(os.environ, CODEX_REAL_BIN=str(real), CODEX_WORKSPACE_ROOT=str(self.root),
                   CODEX_ARTIFACTS_BASE=str(self.root / "runs"), ARCHON_CONTROL_DIR=str(control))
        captured = subprocess.run([str(clr.WORKSPACE_WRAPPER), "exec", "--cd", str(worktree)],
                                  input="", text=True, capture_output=True, env=env)
        self.assertEqual(captured.returncode, 0, captured.stderr)
        argv = json.loads(captured.stdout)
        permission_config = argv[6]
        base = ["codex", "sandbox", "-P", "archon-worker", "-c", permission_config,
                "-C", str(worktree), "--"]
        denied = subprocess.run(base + ["/bin/cat", str(secret)], capture_output=True, text=True)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("Operation not permitted", denied.stderr)
        allowed = subprocess.run(base + ["/bin/sh", "-c", "echo LOCAL_OK > allowed.txt"],
                                 capture_output=True, text=True)
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertEqual((worktree / "allowed.txt").read_text(), "LOCAL_OK\n")
        child = subprocess.Popen(["/bin/sleep", "30"])
        try:
            denied = subprocess.run(base + ["/bin/kill", "-TERM", str(child.pid)],
                                    capture_output=True, text=True)
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("Operation not permitted", denied.stderr)
            self.assertIsNone(child.poll())
        finally:
            child.terminate()
            child.wait()

    def test_private_wrapper_rejects_invalid_recorded_worktree(self):
        artifacts = self.root / "artifacts/runs" / ("c" * 32)
        artifacts.mkdir(parents=True)
        escape = self.root / "escape"
        escape.symlink_to(self.root.parent, target_is_directory=True)
        real = self.root / "real-codex.sh"
        real.write_text("#!/bin/sh\necho SHOULD_NOT_LAUNCH\n")
        real.chmod(0o755)
        env = dict(os.environ, CODEX_REAL_BIN=str(real), CODEX_WORKSPACE_ROOT=str(self.root),
                   CODEX_ARTIFACTS_BASE=str(artifacts.parent))
        for payload in ("not json", "{}", json.dumps({"worktree": str(escape)}),
                        json.dumps({"worktree": str(self.root)})):
            with self.subTest(payload=payload):
                (artifacts / "params.json").write_text(payload)
                result = subprocess.run([str(clr.WORKSPACE_WRAPPER), "exec"],
                                        input=f"Read {artifacts}/params.json", capture_output=True,
                                        text=True, env=env)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("SHOULD_NOT_LAUNCH", result.stdout)
                self.assertIn("CODEX_WRAPPER=FAIL", result.stderr)

    def test_detach_reports_exact_process_group(self):
        log = self.root / "detached.log"
        pid, pgid = clr.detached(log, ["bash", "-c", "sleep 30"], dict(os.environ))
        self.addCleanup(lambda: os.killpg(pgid, signal.SIGTERM) if self._pgid_exists(pgid) else None)
        self.assertEqual(os.getpgid(pid), pgid)
        os.killpg(pgid, signal.SIGTERM)

    def test_supervisor_exits_after_a_normally_completed_child(self):
        log = self.root / "completed.log"
        _, pgid = clr.detached(log, ["bash", "-c", "exit 0"], dict(os.environ))
        deadline = __import__("time").time() + 3
        while self._pgid_exists(pgid) and __import__("time").time() < deadline:
            __import__("time").sleep(0.05)
        self.assertFalse(self._pgid_exists(pgid), "supervisor leaked after its child completed")

    def test_terminate_group_escalates_when_descendant_ignores_term(self):
        log = self.root / "term-ignoring.log"
        _, pgid = clr.detached(
            log, ["bash", "-c", "trap '' TERM; while :; do sleep 1; done"], dict(os.environ)
        )
        self.addCleanup(lambda: os.killpg(pgid, signal.SIGKILL) if self._pgid_exists(pgid) else None)
        fingerprint = clr.process_fingerprint(pgid)
        self.assertIsNotNone(fingerprint)
        clr.terminate_group(pgid, wait_s=0.2, expected_fingerprint=fingerprint)
        self.assertFalse(self._pgid_exists(pgid), "TERM-ignoring process group survived KILL escalation")

    def test_supervisor_survives_parent_exit_to_kill_term_ignoring_grandchild(self):
        log = self.root / "term-ignoring-grandchild.log"
        _, pgid = clr.detached(log, [
            "bash", "-c",
            "(trap '' TERM; while :; do sleep 1; done) & trap 'exit 0' TERM; wait",
        ], dict(os.environ))
        self.addCleanup(lambda: os.killpg(pgid, signal.SIGKILL) if self._pgid_exists(pgid) else None)
        fingerprint = clr.process_fingerprint(pgid)
        clr.terminate_group(pgid, wait_s=0.2, expected_fingerprint=fingerprint)
        self.assertFalse(self._pgid_exists(pgid), "orphan grandchild survived group escalation")

    @staticmethod
    def _pgid_exists(pgid):
        try:
            os.killpg(pgid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def test_watchdog_command_uses_exact_run_and_process_group(self):
        arm = self.root / "armed"
        cmd = clr.watchdog_command("cafebabe99", 4321, "launch-fp", 90, 8_000_000,
                                   self.db, self.root / "codex-home", arm)
        self.assertEqual(cmd[2], "cafebabe99")
        self.assertEqual(cmd[cmd.index("--launcher-pgid") + 1], "4321")
        self.assertEqual(cmd[cmd.index("--launcher-fingerprint") + 1], "launch-fp")
        self.assertIn("--await-running", cmd)
        self.assertEqual(cmd[cmd.index("--arm-file") + 1], str(arm))
        self.assertNotIn("pgrep", " ".join(cmd))
        self.assertNotIn("pgrep", (SETUP / "codex-watchdog.sh").read_text(encoding="utf-8"))

    def test_pre_run_id_failure_terminates_launcher_group(self):
        spec_path = self.root / "spec.md"
        spec_path.write_text("spec", encoding="utf-8")
        argv = [str(SCRIPT), "--db", str(self.db), "--codex-home", str(self.root / "codex-home"),
                "--control-dir", str(self.root / "control"),
                "run", "bugfix-lite-codex", str(spec_path)]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(clr, "DEFAULT_CONTROL_DIR", self.root / "control"), \
             mock.patch.object(clr, "ensure_environment"), \
             mock.patch.object(clr, "stage_private_codex_skills"), \
             mock.patch.object(clr, "detached", return_value=(123, 456)), \
             mock.patch.object(clr, "process_fingerprint", return_value="launcher-fp"), \
             mock.patch.object(clr, "wait_for_run_id", side_effect=SystemExit(1)), \
             mock.patch.object(clr, "terminate_group") as terminate, \
             mock.patch.dict(os.environ, {"CODEX_LITE_LOG_DIR": str(self.root / "logs")}):
            with self.assertRaises(SystemExit):
                clr.main()
        terminate.assert_called_once_with(456, expected_fingerprint="launcher-fp")

    def test_pre_arm_failure_terminates_watchdog_and_launcher_groups(self):
        self.add_run("cafebabe99", status="paused")
        control_dir = self.root / "control"
        row = clr.resolve_run(self.db, "cafebabe99")
        clr.secure_write_json(
            clr.control_state_path(row, control_dir), self.private_control(row["id"])
        )
        argv = [str(SCRIPT), "--db", str(self.db), "--codex-home", str(self.root / "codex-home"),
                "--control-dir", str(control_dir),
                "approve", "cafebabe99", "--token", "valid-token"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(clr, "DEFAULT_CONTROL_DIR", control_dir), \
             mock.patch.object(clr, "ensure_environment"), \
             mock.patch.object(clr, "stage_private_codex_skills"), \
             mock.patch.object(clr, "detached", side_effect=[(123, 456), (789, 987)]), \
             mock.patch.object(clr, "process_fingerprint", side_effect=["launcher-fp", "watchdog-fp"]), \
             mock.patch.object(clr, "wait_for_watchdog_arm", side_effect=SystemExit(1)), \
             mock.patch.object(clr, "terminate_group") as terminate, \
             mock.patch.dict(os.environ, {"CODEX_LITE_LOG_DIR": str(self.root / "logs")}):
            with self.assertRaises(SystemExit):
                clr.main()
        self.assertEqual(terminate.call_args_list, [
            mock.call(987, expected_fingerprint="watchdog-fp"),
            mock.call(456, expected_fingerprint="launcher-fp"),
        ])
        restored = clr.read_control_state(row, control_dir)
        self.assertEqual(restored["control_token_hash"], clr.token_digest("valid-token"))
        self.assertEqual(clr.require_control_token(row, control_dir, "valid-token")["run"], row["id"])

    def test_resume_exports_exact_run_artifacts_without_prompt_binding(self):
        self.add_run("cafebabe99", status="failed")
        control_dir = self.root / "control"
        row = clr.resolve_run(self.db, "cafebabe99")
        clr.secure_write_json(
            clr.control_state_path(row, control_dir), self.private_control(row["id"])
        )
        argv = [str(SCRIPT), "--db", str(self.db), "--codex-home", str(self.root / "codex-home"),
                "--control-dir", str(control_dir),
                "resume", "cafebabe99", "--token", "valid-token"]
        captured_envs = []

        def capture_detached(_log, _command, env, supervise=True):
            captured_envs.append(dict(env))
            raise SystemExit(1)

        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(clr, "DEFAULT_CONTROL_DIR", control_dir), \
             mock.patch.object(clr, "ensure_environment"), \
             mock.patch.object(clr, "stage_private_codex_skills"), \
             mock.patch.object(clr, "detached", side_effect=capture_detached), \
             mock.patch.dict(os.environ, {"CODEX_LITE_LOG_DIR": str(self.root / "logs")}):
            with self.assertRaises(SystemExit):
                clr.main()

        self.assertEqual(captured_envs[0]["CODEX_RUN_ARTIFACTS"], str(clr.artifact_dir(row)))

    def test_resume_pre_arm_cleanup_failure_still_restores_prior_control(self):
        self.add_run("cafebabe99", status="failed")
        control_dir = self.root / "control"
        row = clr.resolve_run(self.db, "cafebabe99")
        prior = self.private_control(row["id"], token="old-token")
        clr.secure_write_json(clr.control_state_path(row, control_dir), prior)
        argv = [str(SCRIPT), "--db", str(self.db), "--codex-home", str(self.root / "codex-home"),
                "--control-dir", str(control_dir),
                "resume", "cafebabe99", "--token", "old-token"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(clr, "DEFAULT_CONTROL_DIR", control_dir), \
             mock.patch.object(clr, "ensure_environment"), \
             mock.patch.object(clr, "stage_private_codex_skills"), \
             mock.patch.object(clr, "detached", side_effect=[(123, 456), (789, 987)]), \
             mock.patch.object(clr, "process_fingerprint", side_effect=["launcher-fp", "watchdog-fp"]), \
             mock.patch.object(clr, "wait_for_watchdog_arm", side_effect=SystemExit(1)), \
             mock.patch.object(clr, "terminate_group"), \
             mock.patch.object(clr, "abandon_if_orphaned", side_effect=RuntimeError("sqlite locked")), \
             mock.patch.dict(os.environ, {"CODEX_LITE_LOG_DIR": str(self.root / "logs")}):
            with self.assertRaises(RuntimeError):
                clr.main()

        restored = clr.read_control_state(row, control_dir)
        self.assertEqual(restored["control_token_hash"], clr.token_digest("old-token"))
        self.assertEqual(clr.require_control_token(row, control_dir, "old-token")["run"], row["id"])

    def test_stop_controlled_processes_uses_only_recorded_exact_groups(self):
        self.add_run("cafebabe99", status="running")
        row = clr.resolve_run(self.db, "cafebabe99")
        control_dir = self.root / "control"
        state = clr.control_state_path(row, control_dir)
        clr.secure_write_json(state, self.private_control(row["id"]))
        with mock.patch.object(clr, "process_fingerprint", return_value="watchdog-fp"), \
             mock.patch.object(clr, "terminate_group") as terminate:
            clr.stop_controlled_processes(row, control_dir)
        self.assertEqual(terminate.call_args_list, [
            mock.call(456, expected_fingerprint="launcher-fp"),
            mock.call(987, expected_fingerprint="watchdog-fp"),
        ])

    def test_paused_abandon_never_kills_historical_process_groups(self):
        self.add_run("cafebabe99", status="paused")
        row = clr.resolve_run(self.db, "cafebabe99")
        with mock.patch.object(clr, "terminate_group") as terminate:
            clr.stop_controlled_processes(row, self.root / "control")
        terminate.assert_not_called()

    def test_running_abandon_without_private_control_state_fails_closed(self):
        self.add_run("cafebabe99", status="running")
        row = clr.resolve_run(self.db, "cafebabe99")
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                clr.stop_controlled_processes(row, self.root / "control")

    def test_terminate_group_refuses_a_reused_process_fingerprint(self):
        with mock.patch.object(clr.os, "getpgid", return_value=456), \
             mock.patch.object(clr, "process_fingerprint", return_value="new-process"), \
             mock.patch.object(clr.os, "killpg") as killpg, \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                clr.terminate_group(456, expected_fingerprint="original-process")
        killpg.assert_not_called()

    def test_check_mode_is_available_without_launch(self):
        control_dir = self.root / "control"
        argv = [str(SCRIPT), "--control-dir", str(control_dir), "check"]
        out = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(clr, "DEFAULT_CONTROL_DIR", control_dir), \
             mock.patch.object(clr, "ensure_environment"), \
             mock.patch.object(clr, "stage_private_codex_skills"), \
             contextlib.redirect_stdout(out):
            self.assertIsNone(clr.main())
        self.assertIn("CODEX_LITE_RUN=READY", out.getvalue())

    def test_guarded_run_arms_then_abandon_kills_exact_groups(self):
        fake_archon = self.root / "fake-archon.py"
        fake_archon.write_text("""#!/usr/bin/env python3
import json, os, sqlite3, sys, time
db = os.environ["ARCHON_DB"]
action = sys.argv[2]
if action == "run":
    # Flags may sit between the lane and the message (archon-run.py passes
    # --branch, which is what gives each run its own path lock). Parse the way
    # the real CLI does -- positionals, flags skipped -- or the shim mistakes
    # "--branch" for the spec and the lane/spec guard fails for a fake reason.
    rest = sys.argv[3:]
    positional = []
    i = 0
    while i < len(rest):
        if rest[i] == "--branch":
            i += 2
            continue
        positional.append(rest[i])
        i += 1
    lane, spec = positional[0], positional[1]
    run_id = "a" * 32
    con = sqlite3.connect(db)
    con.execute("INSERT INTO remote_agent_workflow_runs VALUES (?,?,?,?,?,?,?)",
                (run_id, lane, spec, "running", os.environ["FAKE_OUTPUT"],
                 "2026-08-31 12:00:00", "goodword-codebase"))
    con.commit(); con.close()
    os.unlink(os.environ["ARCHON_CODEX_LITE_GUARD_FILE"])
    print(json.dumps({"workflowRunId": run_id}), flush=True)
    time.sleep(60)
elif action == "abandon":
    run_id = sys.argv[3]
    con = sqlite3.connect(db)
    con.execute("UPDATE remote_agent_workflow_runs SET status='cancelled' WHERE id=?", (run_id,))
    con.commit(); con.close()
    print(json.dumps({"status": "cancelled"}))
else:
    raise SystemExit(2)
""", encoding="utf-8")
        fake_archon.chmod(0o755)
        run_id = "a" * 32
        spec_path = self.root / "spec.md"
        spec_path.write_text("spec", encoding="utf-8")
        env = dict(
            os.environ,
            HOME=str(self.root / "home"),
            ARCHON_BIN=str(fake_archon),
            CODEX_LITE_LOG_DIR=str(self.root / "logs"),
            FAKE_OUTPUT=str(self.root / "out"),
        )
        control_dir = self.root / "home/.archon/control/codex-lite"
        base = [str(SCRIPT), "--db", str(self.db),
                "--codex-home", str(self.root / "codex-home"),
                "--control-dir", str(control_dir)]
        out = io.StringIO()
        with mock.patch.object(sys, "argv", [*base, "run", "bugfix-lite-codex", str(spec_path)]), \
             mock.patch.object(clr, "DEFAULT_CONTROL_DIR", control_dir), \
             mock.patch.object(clr, "ensure_environment"), \
             mock.patch.object(clr, "stage_private_codex_skills"), \
             mock.patch.dict(os.environ, env, clear=False), \
             contextlib.redirect_stdout(out):
            self.assertIsNone(clr.main())
        started_stdout = out.getvalue()
        self.assertIn("CODEX_LITE_RUN=STARTED", started_stdout)
        token = started_stdout.split("control_token=", 1)[1].split()[0]
        row = clr.resolve_run(self.db, run_id)
        control = clr.read_control_state(row, control_dir)
        state_path = clr.control_state_path(row, control_dir)
        self.assertEqual(state_path.stat().st_mode & 0o077, 0)
        self.assertEqual(control_dir.stat().st_mode & 0o077, 0)
        self.assertEqual((control_dir / "codex-workspace-wrapper.sh").stat().st_mode & 0o777, 0o500)
        public = json.loads(clr.control_artifact_path(row).read_text(encoding="utf-8"))
        self.assertTrue(control["watchdog_armed"])
        self.assertTrue(Path(control["watchdog_arm_file"]).is_file())
        self.assertNotIn("launcher_pgid", public)
        self.assertNotIn("control_token_hash", public)

        out = io.StringIO()
        with mock.patch.object(sys, "argv", [*base, "abandon", run_id, "--token", token]), \
             mock.patch.object(clr, "DEFAULT_CONTROL_DIR", control_dir), \
             mock.patch.object(clr, "ensure_environment"), \
             mock.patch.object(clr, "stage_private_codex_skills"), \
             mock.patch.dict(os.environ, env, clear=False), \
             contextlib.redirect_stdout(out):
            self.assertIsNone(clr.main())
        self.assertIn("CODEX_LITE_RUN=ABANDONED", out.getvalue())
        self.assertEqual(clr.status_for_run(self.db, run_id), "cancelled")
        for pgid in (control["watchdog_pgid"], control["launcher_pgid"]):
            self.assertFalse(self._pgid_exists(pgid), f"process group {pgid} survived abandon")

    def test_ai_writable_artifact_cannot_change_kill_authority(self):
        self.add_run("cafebabe99", status="running")
        row = clr.resolve_run(self.db, "cafebabe99")
        control_dir = self.root / "control"
        clr.secure_write_json(
            clr.control_state_path(row, control_dir), self.private_control(row["id"])
        )
        clr.control_artifact_path(row).write_text(json.dumps({
            "run": row["id"], "launcher_pgid": 111, "watchdog_pgid": 222,
        }), encoding="utf-8")
        with mock.patch.object(clr, "process_fingerprint", return_value="watchdog-fp"), \
             mock.patch.object(clr, "terminate_group") as terminate:
            clr.stop_controlled_processes(row, control_dir)
        self.assertEqual(terminate.call_args_list, [
            mock.call(456, expected_fingerprint="launcher-fp"),
            mock.call(987, expected_fingerprint="watchdog-fp"),
        ])

    def test_reused_watcher_pid_cannot_block_launcher_abandon(self):
        self.add_run("cafebabe99", status="running")
        row = clr.resolve_run(self.db, "cafebabe99")
        control_dir = self.root / "control"
        clr.secure_write_json(
            clr.control_state_path(row, control_dir),
            self.private_control(row["id"], watchdog_fingerprint="old-watchdog-fp"),
        )
        output = io.StringIO()
        with mock.patch.object(clr, "process_fingerprint", return_value="reused-process"), \
             mock.patch.object(clr, "process_exists", return_value=True), \
             mock.patch.object(clr, "terminate_group") as terminate, \
             contextlib.redirect_stdout(output):
            clr.stop_controlled_processes(row, control_dir)
        terminate.assert_called_once_with(456, expected_fingerprint="launcher-fp")
        self.assertIn("watcher PGID 987 was reused", output.getvalue())

    def test_control_token_is_required_and_bound_to_run(self):
        self.add_run("cafebabe99", status="paused")
        row = clr.resolve_run(self.db, "cafebabe99")
        control_dir = self.root / "control"
        clr.secure_write_json(
            clr.control_state_path(row, control_dir),
            self.private_control(row["id"], token="right-token"),
        )
        for token in (None, clr.CONTROL_TOKEN_PLACEHOLDER, "wrong-token"):
            with self.subTest(token=token), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    clr.require_control_token(row, control_dir, token)
        self.assertEqual(clr.require_control_token(row, control_dir, "right-token")["run"], row["id"])

    def test_human_token_rejects_tampered_private_signal_authority(self):
        self.add_run("cafebabe99", status="paused")
        row = clr.resolve_run(self.db, "cafebabe99")
        control_dir = self.root / "control"
        state = self.private_control(row["id"], token="human-token")
        state["launcher_pgid"] = 111  # attacker cannot recompute MAC without the human token
        clr.secure_write_json(clr.control_state_path(row, control_dir), state)
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                clr.require_control_token(row, control_dir, "human-token")

    def test_operator_docs_use_guarded_lite_codex_launcher(self):
        runbook = (SETUP.parent / "RUNBOOK.md").read_text(encoding="utf-8")
        skill = (SETUP.parent / "skills/archon-sdlc/SKILL.md").read_text(encoding="utf-8")
        for text in (runbook, skill):
            self.assertIn("archon-run.py", text)
            self.assertNotIn("archon workflow run bugfix-lite-codex", text)
            self.assertNotIn("archon workflow run full-sdlc-api-lite-codex", text)


if __name__ == "__main__":
    unittest.main()
