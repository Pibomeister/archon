#!/usr/bin/env python3
"""Tests for codex-watchdog.sh: typed exits, wall trip, token trip (kill stubbed
via the WATCHDOG_KILL_CMD test seam), non-running run pass-through."""
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parent.parent
SCRIPT = SETUP / "codex-watchdog.sh"


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
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE remote_agent_workflow_runs (id TEXT, user_message TEXT, status TEXT, started_at TEXT)")
        con.execute("INSERT INTO remote_agent_workflow_runs VALUES ('cafebabe99', '/tmp/spec.md', ?, '2026-08-31 11:00:00')", (status,))
        con.execute("CREATE TABLE remote_agent_workflow_events (workflow_run_id TEXT, created_at TEXT)")
        con.execute("INSERT INTO remote_agent_workflow_events VALUES ('cafebabe99', '2026-08-31 11:00:00')")
        con.execute("INSERT INTO remote_agent_workflow_events VALUES ('cafebabe99', '2026-08-31 12:00:00')")
        con.commit(); con.close()

    def run_wd(self, *args, timeout=30):
        env = dict(os.environ, WATCHDOG_KILL_CMD=self.stub)
        return subprocess.run(["bash", str(SCRIPT), "cafebabe", "--db", str(self.db),
                               "--codex-home", str(self.home), "--interval-s", "1", *args],
                              capture_output=True, encoding="utf-8", env=env, timeout=timeout)


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
        # no events/rollouts consistency problem: point codex-home at a missing
        # dir AND drop the events table so codex-usage fails -> WARN, no kill
        self.make_run("paused")  # paused after first poll so the loop exits
        import sqlite3 as sq
        con = sq.connect(self.db); con.execute("DROP TABLE remote_agent_workflow_events"); con.commit(); con.close()
        r = self.run_wd("--wall-minutes", "60", "--max-total-tokens", "1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.killlog.exists(), "killed on accounting failure")

    def test_missing_wall_budget_fails_typed(self):
        self.make_run("running")
        r = self.run_wd()
        self.assertEqual(r.returncode, 1)
        self.assertIn("WATCHDOG=FAIL --wall-minutes is required", r.stdout)


if __name__ == "__main__":
    unittest.main()
