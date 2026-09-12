#!/usr/bin/env python3
"""Tests for codex-watchdog.sh: typed exits, wall trip, token trip (kill stubbed
via the WATCHDOG_KILL_CMD test seam), non-running run pass-through."""
import json
import os
import signal
import select
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parent.parent
SCRIPT = SETUP / "codex-watchdog.sh"
FEATURE_BUDGET = SETUP / "feature-budget.py"
FEATURE_CHAIN = "a" * 32


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.db = self.tmp / "archon.db"
        self.home = self.tmp / "home"
        (self.home / "sessions/2026/08/31").mkdir(parents=True)
        self.killlog = self.tmp / "killed.txt"
        stub = self.tmp / "killstub.sh"
        stub.write_text(f"#!/bin/bash\necho \"$1\" >> {self.killlog}\n", encoding="utf-8")
        stub.chmod(0o755)
        self.stub = str(stub)

    def make_run(self, status="running"):
        self.make_runs([("cafebabe99", status, "2026-08-31 11:00:00")])

    def make_runs(self, rows):
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE remote_agent_workflow_runs (id TEXT, user_message TEXT, status TEXT, started_at TEXT)")
        con.execute("CREATE TABLE remote_agent_workflow_events (workflow_run_id TEXT, created_at TEXT)")
        for run_id, status, started_at in rows:
            con.execute("INSERT INTO remote_agent_workflow_runs VALUES (?, '/tmp/spec.md', ?, ?)",
                        (run_id, status, started_at))
            con.execute("INSERT INTO remote_agent_workflow_events VALUES (?, '2026-08-31 11:00:00')", (run_id,))
            con.execute("INSERT INTO remote_agent_workflow_events VALUES (?, '2026-08-31 12:00:00')", (run_id,))
        con.commit(); con.close()

    def make_session_table(self):
        con = sqlite3.connect(self.db)
        con.execute(
            "CREATE TABLE remote_agent_workflow_run_node_sessions "
            "(workflow_run_id TEXT, node_id TEXT, provider TEXT, provider_session_id TEXT)"
        )
        con.commit(); con.close()

    def record_session(self, run_id, node_id, session_id):
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO remote_agent_workflow_run_node_sessions VALUES (?,?,?,?)",
            (run_id, node_id, "codex", session_id),
        )
        con.commit(); con.close()

    def run_wd(self, *args, prefix="cafebabe", timeout=30):
        env = dict(os.environ, WATCHDOG_KILL_CMD=self.stub)
        return subprocess.run(["bash", str(SCRIPT), prefix, "--db", str(self.db),
                               "--codex-home", str(self.home), "--interval-s", "1", *args],
                              capture_output=True, encoding="utf-8", env=env, timeout=timeout)

    def run_budget(self, *args):
        return subprocess.run(
            [
                sys.executable,
                str(FEATURE_BUDGET),
                "--control-dir",
                str(self.tmp / "control"),
                "--codex-home",
                str(self.home),
                *args,
            ],
            capture_output=True,
            encoding="utf-8",
        )

    def init_feature_budget(self):
        control = self.tmp / "control"
        control.mkdir(mode=0o700, exist_ok=True)
        r = self.run_budget(
            "init", "--chain-id", FEATURE_CHAIN, "--wall-minutes", "1", "--max-total-tokens", "5000"
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return control

    def launcher(self):
        proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.addCleanup(lambda: proc.kill() if proc.poll() is None else None)
        fingerprint = subprocess.check_output(
            ["ps", "-o", "lstart=", "-o", "command=", "-p", str(proc.pid)],
            encoding="utf-8",
        ).strip()
        return proc, fingerprint

    def assert_pid_gone(self, pid):
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        self.fail(f"process still alive after watchdog containment: {pid}")


class Watchdog(Base):
    def test_non_running_run_exits_typed(self):
        self.make_run("paused")
        r = self.run_wd("--wall-minutes", "60")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("WATCHDOG=RUN_PAUSED", r.stdout)
        self.assertFalse(self.killlog.exists(), "killed a non-running run")

    def test_wall_trip_kills_and_types(self):
        self.make_run("running")
        r = self.run_wd("--wall-minutes", "0")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("WATCHDOG=TRIPPED reason=wall run=cafebabe", r.stdout)
        self.assertEqual(self.killlog.read_text().strip(), "cafebabe99")

    def test_token_trip(self):
        self.make_run("running")
        line = json.dumps({"timestamp": "2026-08-31T11:10:00.000Z", "type": "event_msg",
                           "payload": {"type": "token_count", "info": {"total_token_usage": {
                               "input_tokens": 5000, "cached_input_tokens": 0,
                               "output_tokens": 100, "total_tokens": 5100}}}})
        p = self.home / "sessions/2026/08/31/a.jsonl"
        p.write_text(line + "\n", encoding="utf-8")
        import datetime
        ts = datetime.datetime.fromisoformat("2026-08-31T11:10:00+00:00").timestamp()
        os.utime(p, (ts, ts))
        r = self.run_wd("--wall-minutes", "60", "--max-total-tokens", "5000")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("WATCHDOG=TRIPPED reason=tokens run=cafebabe tokens=5100 cap=5000", r.stdout)
        self.assertEqual(self.killlog.read_text().strip(), "cafebabe99")

    def test_unknown_run_fails_typed(self):
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE remote_agent_workflow_runs (id TEXT, user_message TEXT, status TEXT, started_at TEXT)")
        con.commit(); con.close()
        r = self.run_wd("--wall-minutes", "60")
        self.assertEqual(r.returncode, 1)
        self.assertIn("WATCHDOG=FAIL no run matching", r.stdout)

    def test_accounting_failure_warns_not_kills(self):
        # Drop the events table so codex-usage fails while the run is active.
        # Then pause the run after the watchdog has emitted its one-time warning.
        self.make_run("running")
        import sqlite3 as sq
        con = sq.connect(self.db); con.execute("DROP TABLE remote_agent_workflow_events"); con.commit(); con.close()
        env = dict(os.environ, WATCHDOG_KILL_CMD=self.stub)
        p = subprocess.Popen(["bash", str(SCRIPT), "cafebabe", "--db", str(self.db),
                              "--codex-home", str(self.home), "--interval-s", "1",
                              "--wall-minutes", "60", "--max-total-tokens", "1"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             encoding="utf-8", env=env)
        readable, _, _ = select.select([p.stdout], [], [], 10)
        if not readable:
            p.terminate()
            out, err = p.communicate(timeout=5)
            self.fail(f"watchdog did not emit accounting warning: {out}{err}")
        warning = p.stdout.readline()
        self.assertIn("WATCHDOG=WARN token accounting unavailable", warning)
        con = sq.connect(self.db); con.execute("UPDATE remote_agent_workflow_runs SET status='paused'"); con.commit(); con.close()
        out, err = p.communicate(timeout=5)
        out = warning + out
        self.assertEqual(p.returncode, 0, out + err)
        self.assertIn("WATCHDOG=WARN token accounting unavailable", out)
        self.assertIn("WATCHDOG=RUN_PAUSED", out)
        self.assertFalse(self.killlog.exists(), "killed on accounting failure")

    def test_missing_wall_budget_fails_typed(self):
        self.make_run("running")
        r = self.run_wd()
        self.assertEqual(r.returncode, 1)
        self.assertIn("WATCHDOG=FAIL --wall-minutes is required", r.stdout)

    def test_ambiguous_valid_prefix_refuses(self):
        self.make_runs([
            ("abcdef0011", "paused", "2026-08-31 11:00:00"),
            ("abcdef0022", "paused", "2026-08-31 12:00:00"),
        ])
        r = self.run_wd("--wall-minutes", "60", prefix="abcdef00")
        self.assertEqual(r.returncode, 1)
        self.assertIn("WATCHDOG=FAIL ambiguous prefix=abcdef00 matches=2", r.stdout)
        self.assertFalse(self.killlog.exists())

    def test_invalid_sql_like_prefix_refuses(self):
        self.make_run("paused")
        r = self.run_wd("--wall-minutes", "60", prefix="cafebabe%' OR 1=1 --")
        self.assertEqual(r.returncode, 1)
        self.assertIn("WATCHDOG=FAIL bad-id-format", r.stdout)
        self.assertFalse(self.killlog.exists())

    def test_full_id_selects_exact_run(self):
        self.make_runs([
            ("cafebabe99", "paused", "2026-08-31 11:00:00"),
            ("cafebabeee", "running", "2026-08-31 12:00:00"),
        ])
        r = self.run_wd("--wall-minutes", "60", prefix="cafebabe99")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("WATCHDOG=RUN_PAUSED", r.stdout)

    def test_stock_uuid_prefix_selects_exact_run(self):
        run_id = "21786566-8f16-4be3-904a-f89cb2768fe1"
        self.make_runs([
            (run_id, "paused", "2026-08-31 11:00:00"),
            ("31786566-8f16-4be3-904a-f89cb2768fe1", "running", "2026-08-31 12:00:00"),
        ])
        r = self.run_wd("--wall-minutes", "60", prefix="21786566-8f16")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("WATCHDOG=RUN_PAUSED", r.stdout)

    def test_await_running_does_not_mistake_cached_pause_for_completion(self):
        self.make_run("paused")
        launcher = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.addCleanup(lambda: launcher.kill() if launcher.poll() is None else None)
        arm = self.tmp / "watchdog.armed"
        env = dict(os.environ, WATCHDOG_KILL_CMD=self.stub)
        fingerprint = subprocess.check_output(
            ["ps", "-o", "lstart=", "-o", "command=", "-p", str(launcher.pid)],
            encoding="utf-8",
        ).strip()
        wd = subprocess.Popen([
            "bash", str(SCRIPT), "cafebabe", "--db", str(self.db),
            "--codex-home", str(self.home), "--interval-s", "1",
            "--wall-minutes", "60", "--launcher-pgid", str(launcher.pid),
            "--launcher-fingerprint", fingerprint,
            "--await-running", "--arm-file", str(arm),
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", env=env)
        self.addCleanup(lambda: wd.kill() if wd.poll() is None else None)
        time.sleep(0.3)
        self.assertIsNone(wd.poll(), "watchdog exited on the pre-command paused status")
        self.assertFalse(arm.exists())
        con = sqlite3.connect(self.db)
        con.execute("UPDATE remote_agent_workflow_runs SET status='running'")
        con.commit(); con.close()
        deadline = time.time() + 5
        while not arm.exists() and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(arm.exists(), "watchdog did not publish its arming handshake")
        con = sqlite3.connect(self.db)
        con.execute("UPDATE remote_agent_workflow_runs SET status='paused'")
        con.commit(); con.close()
        out, err = wd.communicate(timeout=5)
        self.assertEqual(wd.returncode, 0, out + err)
        self.assertIn("WATCHDOG=ARMED run=cafebabe", out)
        self.assertIn("WATCHDOG=RUN_PAUSED", out)
        launcher.terminate()
        launcher.wait(timeout=5)

    def test_changed_launcher_fingerprint_refuses_to_arm_or_signal(self):
        self.make_run("paused")
        launcher = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.addCleanup(lambda: launcher.kill() if launcher.poll() is None else None)
        arm = self.tmp / "watchdog.armed"
        env = dict(os.environ, WATCHDOG_KILL_CMD=self.stub)
        r = subprocess.run([
            "bash", str(SCRIPT), "cafebabe", "--db", str(self.db),
            "--codex-home", str(self.home), "--interval-s", "1",
            "--wall-minutes", "60", "--launcher-pgid", str(launcher.pid),
            "--launcher-fingerprint", "different process at this pid",
            "--await-running", "--arm-file", str(arm),
        ], capture_output=True, encoding="utf-8", env=env, timeout=5)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("WATCHDOG=FAIL launcher exited before running", r.stdout)
        self.assertFalse(arm.exists())
        self.assertFalse(self.killlog.exists())
        launcher.terminate(); launcher.wait(timeout=5)

    def test_feature_chain_accounting_failure_trips_closed(self):
        self.make_run("running")
        self.make_session_table()
        self.record_session("cafebabe99", "planner", "missing-session")
        control = self.init_feature_budget()
        r = self.run_budget("bind-run", "--chain-id", FEATURE_CHAIN, "--run-id", "cafebabe99")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        launcher, fingerprint = self.launcher()
        arm = self.tmp / "feature.armed"

        r = self.run_wd(
            "--wall-minutes", "60",
            "--max-total-tokens", "5000",
            "--launcher-pgid", str(launcher.pid),
            "--launcher-fingerprint", fingerprint,
            "--await-running",
            "--arm-file", str(arm),
            "--feature-chain-id", FEATURE_CHAIN,
            "--control-dir", str(control),
            timeout=10,
        )

        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("WATCHDOG=FAIL shared budget interval could not be started", r.stdout)
        self.assertFalse(arm.exists())
        launcher.terminate(); launcher.wait(timeout=5)

    def test_feature_chain_shared_wall_budget_trips_from_prior_interval(self):
        self.make_run("running")
        self.make_session_table()
        self.record_session("cafebabe99", "planner", "feature")
        control = self.init_feature_budget()
        session_id = "feature"
        session = self.home / "sessions/2026/08/31/rollout-2026-08-31T00-00-00-feature.jsonl"
        session.write_text("".join([
            json.dumps({"type": "session_meta", "payload": {"id": session_id, "session_id": session_id}}) + "\n",
            json.dumps({
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1, "total_tokens": 2}},
                },
            }) + "\n",
        ]), encoding="utf-8")
        for action, now in (("active-start", "100"), ("active-stop", "160")):
            r = self.run_budget(action, "--chain-id", FEATURE_CHAIN, "--run-id", "cafebabe99", "--now", now)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        launcher, fingerprint = self.launcher()
        arm = self.tmp / "feature-wall.armed"

        r = self.run_wd(
            "--wall-minutes", "60",
            "--launcher-pgid", str(launcher.pid),
            "--launcher-fingerprint", fingerprint,
            "--await-running",
            "--arm-file", str(arm),
            "--feature-chain-id", FEATURE_CHAIN,
            "--control-dir", str(control),
            timeout=10,
        )

        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("WATCHDOG=FAIL shared budget interval could not be started", r.stdout)
        self.assertFalse(arm.exists())
        launcher.terminate(); launcher.wait(timeout=5)

    def test_feature_chain_trip_kills_registered_integration_group(self):
        self.make_run("running")
        self.make_session_table()
        session_id = "registered-session"
        self.record_session("cafebabe99", "planner", session_id)
        session = self.home / "sessions/2026/08/31/rollout-2026-08-31T00-00-00-registered-session.jsonl"
        session.write_text("".join([
            json.dumps({"type": "session_meta", "payload": {"id": session_id, "session_id": session_id}}) + "\n",
            json.dumps({
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1, "total_tokens": 2}},
                },
            }) + "\n",
        ]), encoding="utf-8")
        control = self.init_feature_budget()
        r = self.run_budget("bind-run", "--chain-id", FEATURE_CHAIN, "--run-id", "cafebabe99")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        group = subprocess.Popen([
            "bash", "-c",
            "python3 - <<'PY' &\n"
            "import os, pathlib, time\n"
            f"pathlib.Path({str(self.tmp / 'registered-child.pid')!r}).write_text(str(os.getpid()))\n"
            "time.sleep(30)\n"
            "PY\n"
            "wait",
        ], start_new_session=True)
        self.addCleanup(lambda: os.killpg(group.pid, signal.SIGKILL) if group.poll() is None else None)
        fingerprint = subprocess.check_output(
            ["ps", "-o", "lstart=", "-o", "command=", "-p", str(group.pid)],
            encoding="utf-8",
        ).strip()
        r = self.run_budget(
            "register-group", "--chain-id", FEATURE_CHAIN, "--run-id", "cafebabe99",
            "--pgid", str(group.pid), "--fingerprint", fingerprint, "--label", "integration:test",
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        child_file = self.tmp / "registered-child.pid"
        deadline = time.time() + 5
        while not child_file.exists() and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(child_file.exists(), "fixture child did not start")
        child_pid = int(child_file.read_text(encoding="utf-8"))
        launcher, launcher_fingerprint = self.launcher()
        arm = self.tmp / "feature-registered.armed"

        env = dict(os.environ, WATCHDOG_KILL_CMD=self.stub)
        wd = subprocess.Popen([
            "bash", str(SCRIPT), "cafebabe", "--db", str(self.db),
            "--codex-home", str(self.home), "--interval-s", "1",
            "--wall-minutes", "60",
            "--max-total-tokens", "5000",
            "--launcher-pgid", str(launcher.pid),
            "--launcher-fingerprint", launcher_fingerprint,
            "--await-running",
            "--arm-file", str(arm),
            "--feature-chain-id", FEATURE_CHAIN,
            "--control-dir", str(control),
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", env=env)
        self.addCleanup(lambda: wd.kill() if wd.poll() is None else None)
        deadline = time.time() + 5
        while not arm.exists() and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(arm.exists(), "watchdog did not arm before token trip")
        session.write_text("".join([
            json.dumps({"type": "session_meta", "payload": {"id": session_id, "session_id": session_id}}) + "\n",
            json.dumps({
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"input_tokens": 5000, "cached_input_tokens": 0, "output_tokens": 1, "total_tokens": 5001}},
                },
            }) + "\n",
        ]), encoding="utf-8")
        out, err = wd.communicate(timeout=10)

        self.assertEqual(wd.returncode, 2, out + err)
        self.assertIn("WATCHDOG=TRIPPED reason=shared-tokens", out)
        self.assertIn("integration:test", out)
        self.assert_pid_gone(child_pid)
        launcher.terminate(); launcher.wait(timeout=5)

    def test_feature_budget_active_failure_does_not_publish_arm_file(self):
        self.make_run("running")
        self.make_session_table()
        control = self.init_feature_budget()
        r = self.run_budget("bind-run", "--chain-id", FEATURE_CHAIN, "--run-id", "cafebabe99")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = self.run_budget("active-start", "--chain-id", FEATURE_CHAIN, "--run-id", "b" * 32, "--now", "100")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        launcher, fingerprint = self.launcher()
        arm = self.tmp / "feature-busy.armed"

        r = self.run_wd(
            "--wall-minutes", "60",
            "--max-total-tokens", "5000",
            "--launcher-pgid", str(launcher.pid),
            "--launcher-fingerprint", fingerprint,
            "--await-running",
            "--arm-file", str(arm),
            "--feature-chain-id", FEATURE_CHAIN,
            "--control-dir", str(control),
            timeout=10,
        )

        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("WATCHDOG=FAIL shared budget interval could not be started", r.stdout)
        self.assertFalse(arm.exists())
        self.assertFalse(self.killlog.exists())
        launcher.terminate(); launcher.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
