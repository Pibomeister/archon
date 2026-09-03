#!/usr/bin/env python3
"""Tests for codex-usage.py: window selection from the run row, final-cumulative
summing across session rollouts, typed output."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parent.parent
SCRIPT = SETUP / "codex-usage.py"


def tc_line(total, last, pct=None):
    payload = {"type": "token_count", "info": {"total_token_usage": total, "last_token_usage": last}}
    if pct is not None:
        payload["rate_limits"] = {"primary": {"used_percent": pct}}
    return json.dumps({"timestamp": "2026-08-31T12:00:00.000Z", "type": "event_msg", "payload": payload})


def usage(i, c, o):
    return {"input_tokens": i, "cached_input_tokens": c, "cache_write_input_tokens": 0,
            "output_tokens": o, "reasoning_output_tokens": 0, "total_tokens": i + o}


class CodexUsage(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.db = self.tmp / "archon.db"
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE remote_agent_workflow_runs (id TEXT, started_at TEXT, completed_at TEXT, last_activity_at TEXT)")
        con.execute("INSERT INTO remote_agent_workflow_runs VALUES ('feedbeef1234', '2026-08-31 12:00:00', '2026-08-31 12:30:00', NULL)")
        # events span a WIDER window than the (resume-rewritten) runs row - the
        # script must use the events, or pre-resume sessions are under-counted
        con.execute("CREATE TABLE remote_agent_workflow_events (workflow_run_id TEXT, created_at TEXT)")
        con.execute("INSERT INTO remote_agent_workflow_events VALUES ('feedbeef1234', '2026-08-31 11:00:00')")
        con.execute("INSERT INTO remote_agent_workflow_events VALUES ('feedbeef1234', '2026-08-31 12:30:00')")
        con.commit(); con.close()
        self.home = self.tmp / "home"
        self.sess = self.home / "sessions/2026/08/31"
        self.sess.mkdir(parents=True)

    def write_rollout(self, name, events, mtime="2026-08-31T11:30:00"):
        p = self.sess / name
        p.write_text("\n".join(events) + "\n", encoding="utf-8")
        import datetime
        ts = datetime.datetime.fromisoformat(mtime + "+00:00").timestamp()
        os.utime(p, (ts, ts))
        return p

    def run_script(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), "feedbeef",
                               "--codex-home", str(self.home), "--db", str(self.db), *args],
                              capture_output=True, encoding="utf-8")

    def test_sums_final_cumulative_per_session(self):
        # two token_count events in one session: only the FINAL cumulative counts
        self.write_rollout("a.jsonl", [tc_line(usage(100, 50, 10), usage(100, 50, 10)),
                                       tc_line(usage(300, 200, 30), usage(200, 150, 20), pct=12.5)])
        self.write_rollout("b.jsonl", [tc_line(usage(1000, 0, 5), usage(1000, 0, 5))])
        r = self.run_script("--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        d = json.loads(r.stdout)
        self.assertEqual(d["sessions"], 2)
        self.assertEqual(d["input_tokens"], 1300)
        self.assertEqual(d["output_tokens"], 35)
        self.assertEqual(d["total_tokens"], 1335)
        self.assertEqual(d["rate_used_pct"], 12.5)

    def test_outside_window_excluded(self):
        self.write_rollout("late.jsonl", [tc_line(usage(999, 0, 9), usage(999, 0, 9))], mtime="2026-08-31T14:00:00")
        r = self.run_script("--json")
        d = json.loads(r.stdout)
        self.assertEqual(d["sessions"], 0)
        self.assertEqual(d["total_tokens"], 0)

    def test_typed_line(self):
        self.write_rollout("a.jsonl", [tc_line(usage(10, 0, 1), usage(10, 0, 1))])
        r = self.run_script()
        self.assertIn("CODEX_USAGE run=feedbeef sessions=1 input=10", r.stdout)

    def test_unknown_run_fails_typed(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "deadbeef", "--codex-home", str(self.home), "--db", str(self.db)],
                           capture_output=True, encoding="utf-8")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("CODEX_USAGE=FAIL no run matching", r.stdout + r.stderr)

    def test_ambiguous_prefix_fails_typed(self):
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO remote_agent_workflow_runs VALUES ('feedbeef5678', '2026-08-31 12:01:00', NULL, NULL)")
        con.commit(); con.close()
        r = self.run_script("--json")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("CODEX_USAGE=FAIL ambiguous prefix", r.stdout + r.stderr)

    def test_invalid_prefix_fails_before_query(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "feedbeef%' OR 1=1 --",
                            "--codex-home", str(self.home), "--db", str(self.db)],
                           capture_output=True, encoding="utf-8")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("CODEX_USAGE=FAIL bad-id-format", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
