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

resumes = [t for t in target.split(",") if t]
inserts = [i for i in os.environ.get("FAKE_INSERTS", "").split(",") if i]

if resumes or inserts:
    db = sqlite3.connect(os.environ["ARCHON_DB"])
    for run_id in resumes:
        db.execute(
            "UPDATE remote_agent_workflow_runs "
            "SET status = 'running', started_at = ?, last_activity_at = ? WHERE id = ?",
            ("2026-08-30 12:00:00", "2026-08-30 12:00:00", run_id),
        )
    for run_id in inserts:
        # Another operator starting a run in the same lane while we resume.
        db.execute(
            "INSERT INTO remote_agent_workflow_runs "
            "(id, workflow_name, status, started_at, last_activity_at, working_path, user_message) "
            "VALUES (?,?,?,?,?,?,?)",
            (run_id, "bugfix", "cancelled", "2026-08-30 11:00:00",
             "2026-08-30 11:00:00", os.environ["FAKE_LANE"], "someone else"),
        )
    db.commit()
    db.close()

if os.environ.get("FAKE_CORRUPT_AFTER"):
    # The db becomes unreadable *after* archon ran -- the post-verification query
    # must still produce a typed line and still report archon_rc.
    with open(os.environ["ARCHON_DB"], "wb") as fh:
        fh.write(b"this is definitely not a sqlite database")

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

    def break_tool(self, name):
        """Shadow a coreutil the guard uses outside qq, so it fails unexpectedly."""
        tool = self.bin / name
        tool.write_text("#!/bin/sh\nexit 1\n")
        tool.chmod(0o755)

    def run_guard(self, arg, *extra, fake_resumes=None, fake_rc=None,
                  fake_inserts=None, corrupt_after=False, archon_db=None):
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}{os.pathsep}{env['PATH']}"
        env["ARCHON_DB"] = str(archon_db or self.db)
        env["SHIM_LOG"] = str(self.shim_log)
        env["FAKE_LANE"] = LANE
        if fake_resumes is not None:
            env["FAKE_RESUMES"] = fake_resumes
        if fake_rc is not None:
            env["FAKE_RC"] = fake_rc
        if fake_inserts is not None:
            env["FAKE_INSERTS"] = fake_inserts
        if corrupt_after:
            env["FAKE_CORRUPT_AFTER"] = "1"
        return subprocess.run(
            ["bash", str(SCRIPT), arg, *extra],
            capture_output=True, encoding="utf-8", env=env,
        )

    def verdict_lines(self, res):
        return [ln for ln in res.stdout.splitlines() if ln.startswith("RESUME=")]

    def sole_verdict(self, res):
        lines = self.verdict_lines(res)
        self.assertEqual(len(lines), 1,
                         f"expected exactly one RESUME= line, got {lines!r}\n"
                         f"stdout={res.stdout!r} stderr={res.stderr!r}")
        return lines[0]

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
    # ---- P2-2: RESUME=OK is reserved for archon rc 0; rc != 0 is RESUME=RAN
    def test_named_run_moved_but_archon_failed_is_ran_not_ok(self):
        named = "cafe0001" + "0" * 24
        self.add(named, "failed", "2026-08-29 10:00:00")

        res = self.run_guard(named[:8], fake_rc="3")

        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        line = self.sole_verdict(res)
        self.assertTrue(line.startswith("RESUME=RAN run=cafe0001"), line)
        self.assertIn("archon_rc=3", line)
        self.assertNotIn("RESUME=OK", res.stdout)

    def test_clean_run_is_ok_with_rc_zero(self):
        named = "cafe0002" + "0" * 24
        self.add(named, "failed", "2026-08-29 10:00:00")

        res = self.run_guard(named[:8])

        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.sole_verdict(res), "RESUME=OK run=cafe0002 archon_rc=0")

    # ---- P2-1: nothing ran at all is NOT_EXECUTED, not WRONG_RUN
    def test_archon_failed_and_nothing_moved_is_not_executed(self):
        named = "cafe0003" + "0" * 24
        self.add(named, "failed", "2026-08-29 10:00:00")

        res = self.run_guard(named[:8], fake_resumes="none", fake_rc="2")

        self.assertNotEqual(res.returncode, 0, res.stdout + res.stderr)
        line = self.sole_verdict(res)
        self.assertTrue(line.startswith("RESUME=NOT_EXECUTED named=cafe0003"), line)
        self.assertIn("archon_rc=2", line)
        self.assertNotIn("WRONG_RUN", res.stdout)

    def test_archon_succeeded_but_nothing_moved_is_wrong_run(self):
        """rc 0 with no row touched is still a silent no-op -- keep WRONG_RUN."""
        named = "cafe0004" + "0" * 24
        self.add(named, "failed", "2026-08-29 10:00:00")

        res = self.run_guard(named[:8], fake_resumes="none")

        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        line = self.sole_verdict(res)
        self.assertTrue(line.startswith("RESUME=WRONG_RUN named=cafe0004 moved=none"), line)

    # ---- P1-1: a row that APPEARED during the resume is not a moved row
    def test_appeared_row_is_reported_not_treated_as_moved(self):
        """The CLI cannot create a run on resume (Dn1 only UPDATEs), so a row that
        shows up mid-resume belongs to another operator. Real precedent: c66c7cc0
        was created 23:25:27, inside 607fa834's resume window (23:21:52-23:30:26).
        """
        named = "beef0001" + "0" * 24
        intruder = "beef0002" + "0" * 24
        self.add(named, "failed", "2026-08-29 10:00:00")

        res = self.run_guard(named[:8], fake_inserts=intruder)

        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        line = self.sole_verdict(res)
        self.assertTrue(line.startswith("RESUME=OK run=beef0001"), line)
        self.assertIn("appeared=beef0002", line)
        self.assertNotIn("WRONG_RUN", res.stdout)

    def test_appeared_rows_do_not_mask_a_real_wrong_run(self):
        named = "beef0003" + "0" * 24
        other = "beef0004" + "0" * 24
        intruder = "beef0005" + "0" * 24
        self.add(named, "failed", "2026-08-29 23:00:00")
        self.add(other, "failed", "2026-08-29 21:00:00")

        res = self.run_guard(named[:8], fake_resumes=other, fake_inserts=intruder)

        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        line = self.sole_verdict(res)
        self.assertTrue(line.startswith("RESUME=WRONG_RUN named=beef0003"), line)
        self.assertIn("moved=beef0004", line)
        self.assertIn("appeared=beef0005", line)

    # ---- P3: every wrongly-moved run is listed, not just the last one
    def test_all_wrongly_moved_runs_are_listed(self):
        named = "d0d00001" + "0" * 24
        a = "d0d00002" + "0" * 24
        b = "d0d00003" + "0" * 24
        self.add(named, "failed", "2026-08-29 23:00:00")
        self.add(a, "failed", "2026-08-29 21:00:00")
        self.add(b, "failed", "2026-08-29 20:00:00")

        res = self.run_guard(named[:8], fake_resumes=f"{a},{b}")

        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        line = self.sole_verdict(res)
        self.assertIn("moved=d0d00002,d0d00003", line)

    # ---- P3: a newline in working_path must not garble the verdict line
    def test_newline_in_working_path_still_refuses_cleanly(self):
        weird = "/tmp/lane\nbroken"
        named = "ace00001" + "0" * 24
        newer = "ace00002" + "0" * 24
        self.add(named, "failed", "2026-08-29 10:00:00", path=weird)
        self.add(newer, "failed", "2026-08-29 11:00:00", path=weird)

        res = self.run_guard(named[:8])

        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        line = self.sole_verdict(res)
        self.assertTrue(line.startswith("RESUME=REFUSED"), line)
        self.assertIn("would_resume=ace00002", line)
        self.assertIn("named=ace00001", line)
        self.assertEqual(self.shim_calls(), [])

    def test_newline_in_working_path_lane_is_not_truncated(self):
        """The truncated path '/tmp/lane' must not be used as the lane: a run
        living at the truncated path must not be able to block or unblock."""
        weird = "/tmp/lane\nbroken"
        named = "ace00003" + "0" * 24
        decoy = "ace00004" + "0" * 24
        self.add(named, "failed", "2026-08-29 10:00:00", path=weird)
        self.add(decoy, "failed", "2026-08-29 23:00:00", path="/tmp/lane")

        res = self.run_guard(named[:8])

        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        line = self.sole_verdict(res)
        self.assertTrue(line.startswith("RESUME=OK run=ace00003"), line)

    def test_newline_in_working_path_diagnostic_is_sanitized(self):
        """The indented lane diagnostic must not smuggle extra lines into stdout."""
        weird = "/tmp/lane\nbroken"
        named = "ace00005" + "0" * 24
        newer = "ace00006" + "0" * 24
        self.add(named, "failed", "2026-08-29 10:00:00", path=weird)
        self.add(newer, "failed", "2026-08-29 11:00:00", path=weird)

        res = self.run_guard(named[:8])

        lines = res.stdout.splitlines()
        self.assertEqual(len(lines), 3, f"stray lines from the raw path: {lines!r}")
        self.assertTrue(lines[0].startswith("RESUME=REFUSED"), lines)
        self.assertIn("/tmp/lane?broken", lines[1])
        self.assertNotIn("broken", lines[0])

    def test_newline_in_db_path_keeps_one_line_verdict(self):
        missing = Path(self.tmp.name) / "no\nsuch.db"

        res = self.run_guard("abcd1234", archon_db=str(missing))

        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        line = self.sole_verdict(res)
        self.assertIn("reason=no-db", line)
        self.assertIn("no?such.db", line)
        self.assertEqual(len(res.stdout.splitlines()), 1, res.stdout)

    # ---- P1-2: no silent exits -- every failure path prints a typed line
    def test_unreadable_db_before_archon_is_typed_error(self):
        junk = Path(self.tmp.name) / "not-a-db"
        junk.write_text("plain text, definitely not sqlite\n")
        named = "fade0001" + "0" * 24
        self.add(named, "failed", "2026-08-29 10:00:00")

        res = self.run_guard(named[:8], archon_db=str(junk))

        self.assertNotEqual(res.returncode, 0, res.stdout + res.stderr)
        line = self.sole_verdict(res)
        self.assertTrue(line.startswith("RESUME=ERROR"), line)
        self.assertIn("stage=resolve", line)
        self.assertEqual(self.shim_calls(), [], "archon must not run after a resolve error")

    def test_db_failure_after_archon_is_typed_error_with_archon_rc(self):
        named = "fade0002" + "0" * 24
        self.add(named, "failed", "2026-08-29 10:00:00")

        res = self.run_guard(named[:8], corrupt_after=True, fake_rc="0")

        self.assertNotEqual(res.returncode, 0, res.stdout + res.stderr)
        line = self.sole_verdict(res)
        self.assertTrue(line.startswith("RESUME=ERROR"), line)
        self.assertIn("stage=post", line)
        self.assertIn("archon_rc=0", line)
        self.assertEqual(len(self.shim_calls()), 1, "archon did run")

    def test_post_archon_error_exit_code_is_distinct_from_archons(self):
        named = "fade0003" + "0" * 24
        self.add(named, "failed", "2026-08-29 10:00:00")

        res = self.run_guard(named[:8], corrupt_after=True, fake_rc="90")

        line = self.sole_verdict(res)
        self.assertTrue(line.startswith("RESUME=ERROR"), line)
        self.assertIn("archon_rc=90", line)
        self.assertNotEqual(res.returncode, 90,
                            "guard error must be distinguishable from archon's own rc")
        self.assertNotEqual(res.returncode, 0, res.stdout + res.stderr)

    def test_unexpected_failure_still_prints_a_typed_line(self):
        """The EXIT-trap backstop: N_MATCHES uses awk in a command substitution
        that qq does not wrap, so a failing awk would exit silently under set -e."""
        named = "fade0004" + "0" * 24
        self.add(named, "failed", "2026-08-29 10:00:00")
        self.break_tool("awk")

        res = self.run_guard(named[:8])

        self.assertNotEqual(res.returncode, 0, res.stdout + res.stderr)
        line = self.sole_verdict(res)
        self.assertTrue(line.startswith("RESUME=ERROR"), line)
        self.assertIn("reason=unexpected-exit", line)
        self.assertEqual(self.shim_calls(), [], "archon must not run after an internal error")

    # ---- P2-3: stdin stays closed
    def test_stdin_is_closed_for_archon(self):
        self.assertIn("</dev/null", SCRIPT.read_text())
        self.assertIn("archon workflow approve", SCRIPT.read_text(),
                      "the header must say why stdin is closed")

    def test_bad_id_format_refused(self):
        res = self.run_guard("../../etc/passwd")
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("RESUME=REFUSED", res.stdout)
        self.assertIn("bad-id-format", res.stdout)


if __name__ == "__main__":
    unittest.main()
