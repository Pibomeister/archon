#!/usr/bin/env python3
"""plan-snapshot: name a backgrounded planner, and re-read operator edits on resume.

Run f07acb10's planner backgrounded its Explore agents, polled them with
ScheduleWakeup/ListAgents and ended its turn; the node was recorded completed
and plan-snapshot said only "no plan.md". The shipped node body now prints
NODE_BACKGROUNDED_NO_OUTPUT from the node's recorded tool calls.

A resume re-executes only the failed bash node, so an operator's fix to an
artifact must be read from disk by that same body on its second execution --
exercised here by running the extracted body twice around an edit.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from nodes.extract import runnable_body

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "plan-minimal"
RUN_ID = "0123abcd-0000-4000-8000-000000000000"


class PlanSnapshotRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ad = self.tmp / RUN_ID
        shutil.copytree(FIXTURE, self.ad)
        self.wt = self.tmp / "wt"
        self.wt.mkdir()
        (self.ad / "params.json").write_text(json.dumps({
            "spec": str(self.ad / "spec.md"), "slug": "s", "branch": "b",
            "repo": "api", "worktree": str(self.wt)}), encoding="utf-8")
        self.db = self.tmp / "archon.db"
        with sqlite3.connect(self.db) as con:
            con.execute("CREATE TABLE remote_agent_workflow_events (workflow_run_id TEXT, "
                        "event_type TEXT, step_name TEXT, data TEXT)")

    def record(self, node, tool, tool_input=None):
        with sqlite3.connect(self.db) as con:
            con.execute("INSERT INTO remote_agent_workflow_events VALUES (?,?,?,?)",
                        (RUN_ID, "tool_called", node,
                         json.dumps({"tool_name": tool, "tool_input": tool_input or {}})))

    def run_node(self):
        env = dict(os.environ, ARTIFACTS_DIR=str(self.ad), ARCHON_DB=str(self.db))
        return subprocess.run(["bash", "-c", runnable_body("full-sdlc-api", "plan-snapshot")],
                              capture_output=True, encoding="utf-8", env=env)

    def test_backgrounded_planner_is_named_not_just_missing_plan(self):
        (self.ad / "plan.md").unlink()
        self.record("ralplan", "Agent", {"description": "probe"})
        self.record("ralplan", "ScheduleWakeup", {"delaySeconds": 300})
        r = self.run_node()
        self.assertEqual(1, r.returncode, r.stdout)
        self.assertIn("NODE_BACKGROUNDED_NO_OUTPUT node=ralplan", r.stdout)
        self.assertIn("evidence=ScheduleWakeup", r.stdout)
        self.assertIn("SNAPSHOT=FAIL no plan.md", r.stdout)

    def test_missing_plan_without_background_evidence_stays_plain(self):
        (self.ad / "plan.md").unlink()
        self.record("ralplan", "Agent", {"description": "foreground probe"})
        self.record("kb-recon", "ScheduleWakeup", {})
        r = self.run_node()
        self.assertIn("NODE_NO_OUTPUT node=ralplan", r.stdout)
        self.assertNotIn("NODE_BACKGROUNDED_NO_OUTPUT", r.stdout)

    def test_run_in_background_input_counts_as_evidence(self):
        (self.ad / "plan.md").unlink()
        self.record("ralplan", "Bash", {"command": "sleep 9", "run_in_background": True})
        self.assertIn("evidence=Bash(run_in_background)", self.run_node().stdout)

    def test_resume_rereads_an_operator_fixed_artifact(self):
        (self.ad / "verify.json").write_text(json.dumps(
            {"test_patterns": ["apps/api/src/x/foo.int.spec.ts"]}), encoding="utf-8")
        first = self.run_node()
        self.assertEqual(1, first.returncode, first.stdout)
        self.assertIn("UNIT_PATTERNS=FAIL", first.stdout)
        (self.ad / "verify.json").write_text(json.dumps(
            {"test_patterns": ["apps/api/src/x/foo.spec.ts"]}), encoding="utf-8")
        second = self.run_node()
        self.assertEqual(0, second.returncode, second.stdout + second.stderr)
        self.assertIn("SNAPSHOT=OK", second.stdout)


class ClaudeBackgroundTasksDisabled(unittest.TestCase):
    """The prevention half: archon loads <cwd>/.archon/.env for run, approve and
    resume from the Goodword root; resume.sh may run from anywhere, so it sets
    the variable itself; the installer must ship the file."""

    ARCHON = Path(__file__).resolve().parents[2]

    def test_env_file_disables_background_tasks(self):
        lines = (self.ARCHON / ".env").read_text(encoding="utf-8").splitlines()
        self.assertIn("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1", lines)

    def test_package_ships_it_and_resume_sets_it(self):
        self.assertIn("\n  .env\n", (self.ARCHON / "setup/package.sh").read_text(encoding="utf-8"))
        self.assertIn("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1 archon workflow resume",
                      (self.ARCHON / "setup/resume.sh").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
