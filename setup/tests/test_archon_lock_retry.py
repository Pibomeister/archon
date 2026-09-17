#!/usr/bin/env python3
"""Tests for setup/archon-lock-retry.sh and its callers (resume.sh, archon-run.py).

archon 0.10.1 fails run-state controls with "database is locked" when several
workflows write ~/.archon/archon.db: its SQLite adapter reads the run row inside
a deferred BEGIN and then UPDATEs it, and that read->write upgrade in WAL mode
returns SQLITE_BUSY immediately, bypassing busy_timeout. Observed on 2026-09-16:
`archon workflow reject` recorded the rejection and then failed to resume, and
`setup/resume.sh 48d8e1e3` returned RESUME=NOT_EXECUTED twice.

Every test drives the real scripts against a throwaway sqlite db with a fake
`archon` on PATH that fails with the lock message a configured number of times.
"""
import importlib.util
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parent.parent
RESUME = SETUP / "resume.sh"
RETRY = SETUP / "archon-lock-retry.sh"
LANE = "/tmp/lane-lock"
RUN = "4848e1e3" + "0" * 24
OTHER = "0the0000" + "0" * 24

SCHEMA = """
CREATE TABLE remote_agent_workflow_runs (
  id TEXT PRIMARY KEY,
  workflow_name TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT,
  last_activity_at TEXT,
  working_path TEXT,
  codebase_id TEXT,
  parent_conversation_id TEXT,
  user_message TEXT,
  metadata TEXT DEFAULT '{}'
);
"""

# FAKE_LOCKED: how many calls fail with the lock message ("forever" = all).
# FAKE_TOUCH_NAMED=1: the failing call also changes the named run's metadata.
# FAKE_ERROR: the non-lock error message to fail with instead.
# FAKE_APPROVE_RECORDED=1: `approve` records the decision, then its resume
#   hits the lock (the reject/approve incident shape).
# Every call bumps OTHER's last_activity_at, as a concurrent run would.
SHIM = r"""#!/usr/bin/env python3
import json, os, sqlite3, sys
log = os.environ["SHIM_LOG"]
with open(log, "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\n")
calls = sum(1 for _ in open(log))
verb, run = sys.argv[2], sys.argv[3]
db = sqlite3.connect(os.environ["ARCHON_DB"])
db.execute("UPDATE remote_agent_workflow_runs SET last_activity_at = datetime('now', ?) WHERE id = ?",
           (f"+{calls} seconds", os.environ["OTHER_RUN"]))
db.commit()
locked = os.environ.get("FAKE_LOCKED", "0")
if verb == "approve" and os.environ.get("FAKE_APPROVE_RECORDED"):
    db.execute("UPDATE remote_agent_workflow_runs SET metadata = '{\"approval\":\"approved\"}' WHERE id = ?", (run,))
    db.commit()
    sys.stderr.write("Error: Approved but failed to resume workflow 'bugfix': Cannot resume workflow "
                     "'bugfix': failed to load prior run state — Failed to resume workflow run: "
                     "database is locked\n")
    sys.exit(1)
if os.environ.get("FAKE_ERROR"):
    sys.stderr.write(os.environ["FAKE_ERROR"] + "\n")
    sys.exit(1)
if locked == "forever" or calls <= int(locked):
    if os.environ.get("FAKE_TOUCH_NAMED"):
        db.execute("UPDATE remote_agent_workflow_runs SET metadata = ? WHERE id = ?", (f'{{"n":{calls}}}', run))
        db.commit()
    sys.stderr.write("Error: Cannot resume workflow 'bugfix': failed to load prior run state — "
                     "Failed to resume workflow run: database is locked\n")
    sys.exit(1)
db.execute("UPDATE remote_agent_workflow_runs SET status = 'running', started_at = ?, last_activity_at = ? "
           "WHERE id = ?", ("2026-09-16 12:00:00", "2026-09-16 12:00:00", run))
db.commit()
print(f"Resuming workflow: bugfix ({verb})")
"""


class LockRetryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db = root / "archon.db"
        con = sqlite3.connect(self.db)
        con.executescript(SCHEMA)
        for run_id, status, started, lane in ((RUN, "paused", "2026-09-16 10:00:00", LANE),
                                              (OTHER, "running", "2026-09-16 09:00:00", "/tmp/lane-other")):
            con.execute(
                "INSERT INTO remote_agent_workflow_runs "
                "(id, workflow_name, status, started_at, last_activity_at, working_path, user_message) "
                "VALUES (?,?,?,?,?,?,?)",
                (run_id, "bugfix", status, started, started, lane, "msg"),
            )
        con.commit()
        con.close()
        self.bin = root / "bin"
        self.bin.mkdir()
        (self.bin / "archon").write_text(SHIM)
        (self.bin / "archon").chmod(0o755)
        self.log = root / "shim.log"

    def env(self, **fake):
        env = dict(os.environ)
        env.update(PATH=f"{self.bin}{os.pathsep}{env['PATH']}", ARCHON_DB=str(self.db),
                   SHIM_LOG=str(self.log), OTHER_RUN=OTHER, ARCHON_LOCK_RETRY_ATTEMPTS="4",
                   ARCHON_LOCK_RETRY_MIN_MS="0", ARCHON_LOCK_RETRY_MAX_MS="0",
                   ARCHON_CONTROL_DIR=str(Path(self.tmp.name) / "control"))
        env.update(fake)
        return env

    def resume(self, **fake):
        return subprocess.run(["bash", str(RESUME), RUN[:8]], capture_output=True,
                              encoding="utf-8", env=self.env(**fake), timeout=60)

    def calls(self):
        return [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.exists() else []

    def verdict(self, res):
        lines = [x for x in res.stdout.splitlines() if x.startswith("RESUME=")]
        self.assertEqual(len(lines), 1, res.stdout + res.stderr)
        return lines[0]

    def retry_lines(self, res):
        return [x for x in res.stdout.splitlines() if x.startswith("RESUME_DB_LOCKED ")]

    def test_locked_then_succeeds_is_ok_with_one_retry_line(self):
        res = self.resume(FAKE_LOCKED="1")
        self.assertEqual(self.verdict(res), "RESUME=OK run=4848e1e3 archon_rc=0", res.stdout + res.stderr)
        self.assertEqual(res.returncode, 0)
        self.assertEqual(len(self.retry_lines(res)), 1, res.stdout)
        self.assertRegex(self.retry_lines(res)[0], r"^RESUME_DB_LOCKED attempt=1 of=4 sleep_ms=0 run=4848e1e3$")
        self.assertEqual(len(self.calls()), 2)
        # archon's own stderr still reaches stderr, its stdout still reaches stdout.
        self.assertIn("database is locked", res.stderr)
        self.assertNotIn("database is locked", res.stdout)
        self.assertIn("Resuming workflow: bugfix", res.stdout)

    def test_locked_forever_is_not_executed_after_the_attempt_cap(self):
        res = self.resume(FAKE_LOCKED="forever")
        self.assertEqual(self.verdict(res), "RESUME=NOT_EXECUTED named=4848e1e3 archon_rc=1")
        self.assertEqual(res.returncode, 1)
        self.assertEqual(len(self.calls()), 4)
        self.assertEqual([x.split()[1] for x in self.retry_lines(res)],
                         ["attempt=1", "attempt=2", "attempt=3"])

    def test_non_lock_failure_is_not_retried(self):
        res = self.resume(FAKE_ERROR="Error: Cannot resume: the prior run has no completed nodes")
        self.assertEqual(self.verdict(res), "RESUME=NOT_EXECUTED named=4848e1e3 archon_rc=1")
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.retry_lines(res), [])

    def test_named_row_changed_during_locked_failure_is_not_retried(self):
        res = self.resume(FAKE_LOCKED="forever", FAKE_TOUCH_NAMED="1")
        self.assertEqual(len(self.calls()), 1, res.stdout + res.stderr)
        self.assertEqual(self.retry_lines(res), [])
        # Verdict is what it was before retries existed. resume.sh's own snapshot
        # ignores metadata, so a metadata-only change still reads as NOT_EXECUTED.
        self.assertEqual(self.verdict(res), "RESUME=NOT_EXECUTED named=4848e1e3 archon_rc=1")

    def test_other_runs_heartbeats_do_not_block_the_retry(self):
        # The shim bumps a concurrent run's last_activity_at on every call; only
        # the named row decides a retry.
        res = self.resume(FAKE_LOCKED="2")
        self.assertEqual(self.verdict(res), "RESUME=OK run=4848e1e3 archon_rc=0", res.stdout + res.stderr)
        self.assertEqual(len(self.retry_lines(res)), 2)

    def test_recorded_approval_whose_resume_locked_continues_with_resume(self):
        res = subprocess.run(
            ["bash", str(RETRY), "APPROVE", RUN, "--", "archon", "workflow", "approve", RUN],
            capture_output=True, encoding="utf-8", timeout=60,
            env=self.env(FAKE_APPROVE_RECORDED="1"),
        )
        verbs = [c[1] for c in self.calls()]
        self.assertEqual(verbs, ["approve", "resume"], res.stdout + res.stderr)
        self.assertIn("APPROVE_DB_LOCKED recorded=yes continue=resume.sh run=4848e1e3", res.stdout)
        self.assertEqual(self.verdict(res), "RESUME=OK run=4848e1e3 archon_rc=0")
        self.assertEqual(res.returncode, 0)


class ControlCommandsRouteThroughRetryTest(unittest.TestCase):
    def test_archon_run_approve_and_reject_use_the_retry_wrapper(self):
        spec = importlib.util.spec_from_file_location("archon_run_lock", SETUP / "archon-run.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.command_for("approve", RUN)[:5],
                         ["bash", str(SETUP / "archon-lock-retry.sh"), "APPROVE", RUN, "--"])
        self.assertEqual(mod.command_for("reject", RUN, "why")[-5:],
                         ["archon", "workflow", "reject", RUN, "why"])
        self.assertEqual(mod.command_for("reject", RUN, "why")[2], "REJECT")
        self.assertEqual(mod.command_for("resume", RUN), ["bash", str(SETUP / "resume.sh"), RUN])

    def test_package_ships_the_retry_wrapper(self):
        # resume.sh, gate-approve.sh and archon-run.py all exec it by path.
        self.assertIn("\n  setup/archon-lock-retry.sh\n", (SETUP / "package.sh").read_text(encoding="utf-8"))

    def test_gate_approve_uses_the_retry_wrapper(self):
        body = (SETUP / "gate-approve.sh").read_text(encoding="utf-8")
        self.assertIn('archon-lock-retry.sh" GATE_APPROVE "$RUN_ID" --', body)


if __name__ == "__main__":
    unittest.main()
