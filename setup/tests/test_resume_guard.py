#!/usr/bin/env python3
"""Tests for setup/resume.sh, the guarded wrapper around `archon workflow resume`.

The defect being guarded: `archon workflow resume <run-id>` validates the id you
name and then throws it away. The executor re-selects the run to resume with

    SELECT * FROM remote_agent_workflow_runs
     WHERE workflow_name = ?1 AND working_path = ?2
       AND (status IN ('failed','paused')
            OR (status = 'running'
                AND (last_activity_at IS NULL
                     OR last_activity_at < datetime('now','-' || ?3 || ' days'))))
     ORDER BY started_at DESC
     LIMIT 1                                   -- ?3 = 1 (day)

so the *newest* resumable run of the (workflow_name, working_path) lane wins.
Note there is no paused-first ordering on this path: `started_at DESC` is the
only ordering, which `test_paused_named_loses_to_newer_failed` pins.

Every test drives resume.sh against a throwaway sqlite db (ARCHON_DB) with a
fake `archon` on PATH that records its argv and bumps whichever run it is told
to resume, so we can assert both the refusal decisions and the post-hoc
verification.
"""
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "resume.sh"

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
  user_message TEXT
);
"""

SHIM = """#!/usr/bin/env python3
import json, os, sqlite3, sys

with open(os.environ["SHIM_LOG"], "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")

target = os.environ.get("FAKE_RESUMES", "")
if not target:
    # Default: behave itself and resume exactly the run named on the argv.
    target = sys.argv[3] if len(sys.argv) > 3 else ""
if target == "none":
    # Simulate archon resuming nothing at all.
    target = ""

if target:
    db = sqlite3.connect(os.environ["ARCHON_DB"])
    db.execute(
        "UPDATE remote_agent_workflow_runs "
        "SET status = 'running', started_at = ?, last_activity_at = ? WHERE id = ?",
        ("2026-08-30 12:00:00", "2026-08-30 12:00:00", target),
    )
    db.commit()
    db.close()

sys.exit(int(os.environ.get("FAKE_RC", "0")))
"""

LANE = "/tmp/lane-alpha"

# sqlite's datetime('now') is UTC, so the stale-'running' comparisons must be too.
def utc_ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S")


class ResumeGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

        self.db = root / "archon.db"
        con = sqlite3.connect(self.db)
        con.executescript(SCHEMA)
        con.commit()
        con.close()

        self.bin = root / "bin"
        self.bin.mkdir()
        shim = self.bin / "archon"
        shim.write_text(SHIM)
        shim.chmod(0o755)
        self.shim_log = root / "shim.log"

    # ---- helpers -------------------------------------------------------
    def add(self, run_id, status, started_at, *, workflow="bugfix", path=LANE, last=None):
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO remote_agent_workflow_runs "
            "(id, workflow_name, status, started_at, last_activity_at, working_path, user_message) "
            "VALUES (?,?,?,?,?,?,?)",
            (run_id, workflow, status, started_at, last or started_at, path, "msg"),
        )
        con.commit()
        con.close()

    def run_guard(self, arg, *extra, fake_resumes=None, fake_rc=None):
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}{os.pathsep}{env['PATH']}"
        env["ARCHON_DB"] = str(self.db)
        env["SHIM_LOG"] = str(self.shim_log)
        if fake_resumes is not None:
            env["FAKE_RESUMES"] = fake_resumes
        if fake_rc is not None:
            env["FAKE_RC"] = fake_rc
        return subprocess.run(
            ["bash", str(SCRIPT), arg, *extra],
            capture_output=True, encoding="utf-8", env=env,
        )

    def shim_calls(self):
        if not self.shim_log.exists():
            return []
        return [json.loads(line) for line in self.shim_log.read_text().splitlines() if line.strip()]

    def status_of(self, run_id):
        con = sqlite3.connect(self.db)
        row = con.execute(
            "SELECT status FROM remote_agent_workflow_runs WHERE id = ?", (run_id,)
        ).fetchone()
        con.close()
        return row[0]

    # ---- (a) named run is older than another failed run of the same lane
    def test_older_named_run_refused_with_would_resume(self):
        named = "aaaaaaaa" + "0" * 24
        newer = "bbbbbbbb" + "0" * 24
        self.add(named, "failed", "2026-08-29 21:58:39")
        self.add(newer, "failed", "2026-08-29 23:21:52")

        res = self.run_guard(named[:8])

        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("RESUME=REFUSED", res.stdout)
        self.assertIn("would_resume=bbbbbbbb", res.stdout)
        self.assertIn("named=aaaaaaaa", res.stdout)
        self.assertIn("reason=newer-resumable-run-of-lane", res.stdout)
        self.assertIn(f"archon workflow abandon {newer}", res.stdout)
        self.assertEqual(self.shim_calls(), [], "archon must not run on a refusal")

    # ---- (b) paused does NOT sort first on the CLI resume path
    def test_paused_named_loses_to_newer_failed(self):
        """jV0 orders by started_at DESC only -- the paused-first CASE lives in
        findResumableRunByParentConversation, which the CLI resume path never
        reaches (parent_conversation_id is NULL for every CLI run)."""
        named = "cccccccc" + "0" * 24
        newer = "dddddddd" + "0" * 24
        self.add(named, "paused", "2026-08-29 10:00:00")
        self.add(newer, "failed", "2026-08-29 11:00:00")

        res = self.run_guard(named[:8])

        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("would_resume=dddddddd", res.stdout)
        self.assertIn("named=cccccccc", res.stdout)
        self.assertEqual(self.shim_calls(), [])

    def test_paused_named_allowed_when_it_is_newest(self):
        named = "cccccccc" + "0" * 24
        older = "dddddddd" + "0" * 24
        self.add(named, "paused", "2026-08-29 11:00:00")
        self.add(older, "failed", "2026-08-29 10:00:00")

        res = self.run_guard(named[:8])

        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("RESUME=OK run=cccccccc", res.stdout)

    # ---- (c) sole resumable run -> OK, archon invoked with the FULL id
    def test_sole_resumable_run_ok_and_full_id_passed(self):
        named = "eeeeeeee" + "0" * 24
        self.add(named, "failed", "2026-08-29 21:58:39")
        self.add("ffffffff" + "0" * 24, "completed", "2026-08-29 23:00:00")

        res = self.run_guard(named[:8], "--verbose")

        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("RESUME=OK run=eeeeeeee", res.stdout)
        self.assertEqual(
            self.shim_calls(),
            [["workflow", "resume", named, "--verbose"]],
            "archon must be called with the full id and the passthrough args",
        )

    # ---- (d) archon moved a different run of the lane
    def test_wrong_run_moved_is_reported(self):
        named = "11111111" + "0" * 24
        other = "22222222" + "0" * 24
        self.add(named, "failed", "2026-08-29 23:00:00")
        self.add(other, "failed", "2026-08-29 21:00:00")

        res = self.run_guard(named[:8], fake_resumes=other)

        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("RESUME=WRONG_RUN", res.stdout)
        self.assertIn("named=11111111", res.stdout)
        self.assertIn("moved=22222222", res.stdout)

    def test_no_run_moved_is_reported(self):
        named = "11111111" + "0" * 24
        self.add(named, "failed", "2026-08-29 23:00:00")

        res = self.run_guard(named[:8], fake_resumes="none")

        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("RESUME=WRONG_RUN", res.stdout)
        self.assertIn("moved=none", res.stdout)

    # ---- (e) ambiguous prefix
    def test_ambiguous_prefix_refused(self):
        self.add("abcd1111" + "0" * 24, "failed", "2026-08-29 10:00:00")
        self.add("abcd2222" + "0" * 24, "failed", "2026-08-29 11:00:00")

        res = self.run_guard("abcd")

        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("RESUME=REFUSED ambiguous", res.stdout)
        self.assertEqual(self.shim_calls(), [])

    def test_unknown_prefix_refused(self):
        self.add("abcd1111" + "0" * 24, "failed", "2026-08-29 10:00:00")

        res = self.run_guard("9999")

        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("RESUME=REFUSED", res.stdout)
        self.assertIn("not-found", res.stdout)

    # ---- (f) non-resumable status
    def test_completed_run_refused_with_status(self):
        named = "33333333" + "0" * 24
        self.add(named, "completed", "2026-08-29 10:00:00")

        res = self.run_guard(named[:8])

        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("RESUME=REFUSED status=completed", res.stdout)
        self.assertEqual(self.shim_calls(), [])

    def test_cancelled_run_refused_with_status(self):
        named = "44444444" + "0" * 24
        self.add(named, "cancelled", "2026-08-29 10:00:00")

        res = self.run_guard(named[:8])

        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("RESUME=REFUSED status=cancelled", res.stdout)

    # ---- lane scoping: a different workflow / path must not block
    def test_other_lane_newer_run_does_not_block(self):
        named = "55555555" + "0" * 24
        self.add(named, "failed", "2026-08-29 10:00:00")
        self.add("66666666" + "0" * 24, "failed", "2026-08-29 23:00:00", workflow="full-sdlc-api")
        self.add("77777777" + "0" * 24, "failed", "2026-08-29 23:30:00", path="/tmp/lane-beta")

        res = self.run_guard(named[:8])

        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("RESUME=OK run=55555555", res.stdout)

    # ---- the stale-'running' arm of the resumable predicate
    def test_stale_running_run_blocks(self):
        named = "88888888" + "0" * 24
        stale = "99999999" + "0" * 24
        self.add(named, "failed", "2026-08-29 10:00:00")
        self.add(stale, "running", "2026-08-29 23:00:00", last="2020-01-01 00:00:00")

        res = self.run_guard(named[:8])

        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("would_resume=99999999", res.stdout)

    def test_fresh_running_run_does_not_block(self):
        named = "88888888" + "0" * 24
        fresh = "99999999" + "0" * 24
        self.add(named, "failed", "2026-08-29 10:00:00")
        # Newest of the lane by started_at, but active an hour ago -> not resumable.
        self.add(fresh, "running", "2126-08-29 23:00:00", last=utc_ago(hours=1))

        res = self.run_guard(named[:8])

        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("RESUME=OK run=88888888", res.stdout)

    def test_running_idle_two_days_blocks(self):
        """Pins the stale-'running' grace period at ONE day, not three.

        In the bundle the clause reads `Dw$(Y, 3)` -> `nowMinusDays(3)` ->
        `datetime('now','-' || $3 || ' days')`. The 3 is the SQL *placeholder
        index*, not a day count; $3 is bound to `Fw$`, and `Fw$ = 1`. A run idle
        for two days is therefore resumable and will hijack the resume.
        """
        named = "88888888" + "0" * 24
        idle = "99999999" + "0" * 24
        self.add(named, "failed", "2026-08-29 10:00:00")
        self.add(idle, "running", "2126-08-29 23:00:00", last=utc_ago(days=2))

        res = self.run_guard(named[:8])

        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("would_resume=99999999", res.stdout)
        self.assertEqual(self.shim_calls(), [])

    def test_null_last_activity_running_blocks(self):
        named = "88888888" + "0" * 24
        orphan = "99999999" + "0" * 24
        self.add(named, "failed", "2026-08-29 10:00:00")
        self.add(orphan, "running", "2126-08-29 23:00:00", last="")
        con = sqlite3.connect(self.db)
        con.execute("UPDATE remote_agent_workflow_runs SET last_activity_at = NULL WHERE id = ?", (orphan,))
        con.commit()
        con.close()

        res = self.run_guard(named[:8])

        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("would_resume=99999999", res.stdout)

    # ---- a started_at tie makes the CLI's pick undefined
    def test_started_at_tie_refused(self):
        named = "aabbccdd" + "0" * 24
        twin = "bbccddee" + "0" * 24
        self.add(named, "failed", "2026-08-29 10:00:00")
        self.add(twin, "failed", "2026-08-29 10:00:00")

        res = self.run_guard(named[:8])

        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("RESUME=REFUSED", res.stdout)
        self.assertIn("reason=started-at-tie", res.stdout)
        self.assertEqual(self.shim_calls(), [])

    # ---- exit code passthrough on a legitimate re-failure
    def test_archon_exit_code_is_propagated(self):
        named = "cafe0001" + "0" * 24
        self.add(named, "failed", "2026-08-29 10:00:00")

        res = self.run_guard(named[:8], fake_rc="3")

        self.assertEqual(res.returncode, 3, res.stdout + res.stderr)
        self.assertIn("RESUME=OK run=cafe0001", res.stdout)
        self.assertIn("archon_rc=3", res.stdout)

    def test_bad_id_format_refused(self):
        res = self.run_guard("../../etc/passwd")
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("RESUME=REFUSED", res.stdout)
        self.assertIn("bad-id-format", res.stdout)


if __name__ == "__main__":
    unittest.main()
