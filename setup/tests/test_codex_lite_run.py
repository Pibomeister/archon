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
                    "(id TEXT, workflow_name TEXT, user_message TEXT, status TEXT, output_root TEXT, started_at TEXT)")
        con.commit(); con.close()

    def add_run(self, run_id, lane="bugfix-lite-codex", status="paused", started="2026-08-31 12:00:00"):
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO remote_agent_workflow_runs VALUES (?,?,?,?,?,?)",
                    (run_id, lane, "/tmp/spec.md", status, str(self.root / "out"), started))
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

    def test_run_command_does_not_use_archon_detach(self):
        old = os.environ.get("ARCHON_BIN")
        os.environ["ARCHON_BIN"] = "/fake/archon"
        self.addCleanup(lambda: os.environ.__setitem__("ARCHON_BIN", old) if old is not None else os.environ.pop("ARCHON_BIN", None))
        cmd = clr.command_for("run", "bugfix-lite-codex\0/tmp/spec.md")
        self.assertEqual(cmd, ["/fake/archon", "workflow", "run", "bugfix-lite-codex", "/tmp/spec.md"])
        self.assertNotIn("--detach", cmd)

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
            [str(wrapper), "exec", "--experimental-json"],
            capture_output=True, encoding="utf-8", env=env,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            f"exec --sandbox workspace-write --cd {self.root}/api --add-dir {self.root}/web-app "
            "--config sandbox_workspace_write.network_access=false --experimental-json",
        )

    def test_private_wrapper_replaces_adapter_sandbox_override(self):
        real = self.root / "real-codex.sh"
        real.write_text('#!/bin/bash\nprintf "%s\\n" "$*"\n', encoding="utf-8")
        real.chmod(0o755)
        for repo in ("api", "web-app"):
            (self.root / repo / ".git").mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, CODEX_REAL_BIN=str(real), CODEX_WORKSPACE_ROOT=str(self.root),
                   CODEX_ARTIFACTS_BASE=str(self.root / "artifacts/runs"))
        result = subprocess.run(
            [str(clr.WORKSPACE_WRAPPER), "exec", "--sandbox", "danger-full-access",
             "--config", "sandbox_workspace_write.network_access=true"],
            capture_output=True, encoding="utf-8", env=env,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            f"exec --sandbox workspace-write --cd {self.root}/api --add-dir {self.root}/web-app "
            "--config sandbox_workspace_write.network_access=false",
        )

    def test_private_wrapper_adds_only_the_prompt_bound_run_artifacts(self):
        real = self.root / "real-codex.sh"
        real.write_text('#!/bin/bash\nprintf "%s\\n" "$*"\ncat >/dev/null\n', encoding="utf-8")
        real.chmod(0o755)
        for repo in ("api", "web-app"):
            (self.root / repo / ".git").mkdir(parents=True, exist_ok=True)
        base = self.root / "artifacts/runs"
        run_dir = base / ("a" * 32)
        run_dir.mkdir(parents=True)
        env = dict(os.environ, CODEX_REAL_BIN=str(real), CODEX_WORKSPACE_ROOT=str(self.root),
                   CODEX_ARTIFACTS_BASE=str(base))
        result = subprocess.run(
            [str(clr.WORKSPACE_WRAPPER), "exec", "--experimental-json"],
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
            [str(clr.WORKSPACE_WRAPPER), "exec"],
            input=f"{base / ('a' * 32)} {base / ('b' * 32)}",
            capture_output=True, encoding="utf-8", env=env,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("multiple run artifact roots", result.stderr)

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
             mock.patch.object(clr, "ensure_environment"), \
             mock.patch.object(clr, "detached", return_value=(123, 456)), \
             mock.patch.object(clr, "process_fingerprint", return_value="launcher-fp"), \
             mock.patch.object(clr, "wait_for_run_id", side_effect=SystemExit(1)), \
             mock.patch.object(clr, "terminate_group") as terminate, \
             mock.patch.dict(os.environ, {"CODEX_LITE_LOG_DIR": str(self.root / "logs"),
                                          "CODEX_LITE_SKIP_ENV_CHECKS": "1"}):
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
             mock.patch.object(clr, "ensure_environment"), \
             mock.patch.object(clr, "detached", side_effect=[(123, 456), (789, 987)]), \
             mock.patch.object(clr, "process_fingerprint", side_effect=["launcher-fp", "watchdog-fp"]), \
             mock.patch.object(clr, "wait_for_watchdog_arm", side_effect=SystemExit(1)), \
             mock.patch.object(clr, "terminate_group") as terminate, \
             mock.patch.dict(os.environ, {"CODEX_LITE_LOG_DIR": str(self.root / "logs"),
                                          "CODEX_LITE_SKIP_ENV_CHECKS": "1"}):
            with self.assertRaises(SystemExit):
                clr.main()
        self.assertEqual(terminate.call_args_list, [
            mock.call(987, expected_fingerprint="watchdog-fp"),
            mock.call(456, expected_fingerprint="launcher-fp"),
        ])
        restored = clr.read_control_state(row, control_dir)
        self.assertEqual(restored["control_token_hash"], clr.token_digest("valid-token"))
        self.assertEqual(clr.require_control_token(row, control_dir, "valid-token")["run"], row["id"])

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
        env = dict(os.environ, CODEX_LITE_SKIP_ENV_CHECKS="1")
        r = subprocess.run([sys.executable, str(SCRIPT), "--control-dir",
                            str(self.root / "control"), "check"],
                           capture_output=True, encoding="utf-8", env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("CODEX_LITE_RUN=READY", r.stdout)

    def test_guarded_run_arms_then_abandon_kills_exact_groups(self):
        fake_archon = self.root / "fake-archon.py"
        fake_archon.write_text("""#!/usr/bin/env python3
import json, os, sqlite3, sys, time
db = os.environ["ARCHON_DB"]
action = sys.argv[2]
if action == "run":
    lane, spec = sys.argv[3:5]
    run_id = "a" * 32
    con = sqlite3.connect(db)
    con.execute("INSERT INTO remote_agent_workflow_runs VALUES (?,?,?,?,?,?)",
                (run_id, lane, spec, "running", os.environ["FAKE_OUTPUT"], "2026-08-31 12:00:00"))
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
            CODEX_LITE_SKIP_ENV_CHECKS="1",
            CODEX_LITE_LOG_DIR=str(self.root / "logs"),
            FAKE_OUTPUT=str(self.root / "out"),
        )
        control_dir = self.root / "home/.archon/control/codex-lite"
        base = [sys.executable, str(SCRIPT), "--db", str(self.db),
                "--codex-home", str(self.root / "codex-home"),
                "--control-dir", str(control_dir)]
        started = subprocess.run(
            [*base, "run", "bugfix-lite-codex", str(spec_path)],
            capture_output=True, encoding="utf-8", env=env, timeout=20,
        )
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        self.assertIn("CODEX_LITE_RUN=STARTED", started.stdout)
        token = started.stdout.split("control_token=", 1)[1].split()[0]
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

        abandoned = subprocess.run(
            [*base, "abandon", run_id, "--token", token],
            capture_output=True, encoding="utf-8", env=env, timeout=20,
        )
        self.assertEqual(abandoned.returncode, 0, abandoned.stdout + abandoned.stderr)
        self.assertIn("CODEX_LITE_RUN=ABANDONED", abandoned.stdout)
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
