#!/usr/bin/env python3
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parent.parent
SCRIPT = SETUP / "feature-budget.py"
CHAIN = "a" * 32


def session_meta_line(session_id, parent_thread_id=None):
    payload = {"id": session_id, "session_id": session_id}
    if parent_thread_id is not None:
        payload["source"] = {
            "subagent": {
                "thread_spawn": {
                    "parent_thread_id": parent_thread_id,
                    "depth": 1,
                }
            }
        }
    return json.dumps({"type": "session_meta", "payload": payload})


def token_line(total):
    return json.dumps({
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": max(total - 10, 0),
                    "cached_input_tokens": 0,
                    "output_tokens": min(total, 10),
                    "total_tokens": total,
                }
            },
        },
    })


class FeatureBudget(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.control = self.root / "control"
        self.control.mkdir(mode=0o700)
        self.home = self.root / "codex-home"
        self.sessions = self.home / "sessions/2026/09/11"
        self.sessions.mkdir(parents=True)
        self.db = self.root / "archon.db"
        with sqlite3.connect(self.db) as con:
            con.execute(
                "CREATE TABLE remote_agent_workflow_run_node_sessions "
                "(workflow_run_id TEXT, node_id TEXT, provider TEXT, provider_session_id TEXT)"
            )
            con.execute(
                "CREATE TABLE remote_agent_workflow_events "
                "(workflow_run_id TEXT, event_type TEXT, step_name TEXT, data TEXT, created_at TEXT)"
            )

    def run_budget(self, *args):
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--control-dir",
                str(self.control),
                "--codex-home",
                str(self.home),
                *args,
            ],
            capture_output=True,
            encoding="utf-8",
        )

    def init_budget(self, wall=240, tokens=30_000_000):
        result = self.run_budget(
            "init",
            "--chain-id",
            CHAIN,
            "--wall-minutes",
            str(wall),
            "--max-total-tokens",
            str(tokens),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def write_session(self, name, total, session_id=None, parent_thread_id=None):
        path = self.sessions / name
        lines = []
        if session_id is not None:
            lines.append(session_meta_line(session_id, parent_thread_id))
        lines.append(token_line(total))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def record_session(self, run_id, node_id, session_id):
        with sqlite3.connect(self.db) as con:
            con.execute(
                "INSERT INTO remote_agent_workflow_run_node_sessions VALUES (?,?,?,?)",
                (run_id, node_id, "codex", session_id),
            )

    def record_codex_activity(self, run_id, node_id="planner"):
        with sqlite3.connect(self.db) as con:
            con.execute(
                "INSERT INTO remote_agent_workflow_events "
                "(workflow_run_id, event_type, step_name, data) VALUES (?,?,?,?)",
                (run_id, "tool_called", node_id, json.dumps({"provider": "codex"})),
            )

    def record_workflow_started(self, run_id):
        with sqlite3.connect(self.db) as con:
            con.execute(
                "INSERT INTO remote_agent_workflow_events "
                "(workflow_run_id, event_type, step_name, data) VALUES (?,?,?,?)",
                (
                    run_id,
                    "workflow_started",
                    None,
                    json.dumps({"workflowName": "full-sdlc-api-codex", "provider": "codex"}),
                ),
            )

    def record_stop(self, run_id, event_type, created_at):
        with sqlite3.connect(self.db) as con:
            con.execute(
                "INSERT INTO remote_agent_workflow_events "
                "(workflow_run_id, event_type, step_name, data, created_at) VALUES (?,?,?,?,?)",
                (run_id, event_type, None, "{}", created_at),
            )

    def usage(self, now=1000):
        result = self.run_budget("usage", "--chain-id", CHAIN, "--now", str(now), "--json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_active_intervals_exclude_approval_pauses(self):
        self.init_budget()
        session = self.write_session("planning.jsonl", 100)
        result = self.run_budget(
            "bind-run", "--chain-id", CHAIN, "--run-id", "b" * 32, "--session-file", str(session)
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        for action, now in (
            ("active-start", 100),
            ("active-stop", 160),
            ("active-start", 220),
            ("active-stop", 250),
        ):
            result = self.run_budget(action, "--chain-id", CHAIN, "--run-id", "b" * 32, "--now", str(now))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        summary = self.usage()
        self.assertEqual(summary["active_seconds"], 90)
        self.assertEqual(summary["total_tokens"], 100)

    def test_session_usage_is_deduplicated_across_retries(self):
        self.init_budget()
        shared = self.write_session("shared.jsonl", 300)
        child = self.write_session("child.jsonl", 200)
        for run_id, files in (("c" * 32, [shared]), ("d" * 32, [shared, child])):
            args = ["bind-run", "--chain-id", CHAIN, "--run-id", run_id]
            for item in files:
                args.extend(["--session-file", str(item)])
            result = self.run_budget(*args)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        summary = self.usage()
        self.assertEqual(summary["runs"], 2)
        self.assertEqual(summary["sessions"], 2)
        self.assertEqual(summary["total_tokens"], 500)

    def test_pre_arm_run_binding_allows_no_session_until_ai_records_one(self):
        self.init_budget()
        result = self.run_budget("bind-run", "--chain-id", CHAIN, "--run-id", "e" * 32)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        result = self.run_budget("usage", "--chain-id", CHAIN, "--db", str(self.db), "--require-sessions", "--json")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["sessions"], 0)

    def test_db_recorded_sessions_are_refreshed_and_counted_exactly(self):
        self.init_budget()
        run_id = "e" * 32
        result = self.run_budget("bind-run", "--chain-id", CHAIN, "--run-id", run_id)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        session_id = "01a06ac7-4d10-7710-b293-2f4a0df2d522"
        self.write_session(f"rollout-2026-09-03T22-57-21-{session_id}.jsonl", 123, session_id=session_id)
        self.write_session(f"unrelated-{session_id}.jsonl", 999, session_id="01a06ac7-4d10-7710-b293-2f4a0df2d523")
        self.record_session(run_id, "planner", session_id)

        result = self.run_budget("usage", "--chain-id", CHAIN, "--db", str(self.db), "--json")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["sessions"], 1)
        self.assertEqual(summary["total_tokens"], 123)

    def test_bound_session_survives_stock_node_session_overwrite(self):
        self.init_budget()
        run_id = "e" * 32
        first_id = "01a06ac7-4d10-7710-b293-2f4a0df2d522"
        second_id = "01a06ac7-4d10-7710-b293-2f4a0df2d523"
        self.write_session(f"rollout-2026-09-03T22-57-21-{first_id}.jsonl", 100, session_id=first_id)
        self.write_session(f"rollout-2026-09-03T22-58-00-{second_id}.jsonl", 50, session_id=second_id)
        result = self.run_budget("bind-session", "--chain-id", CHAIN, "--run-id", run_id, "--session-id", first_id)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with sqlite3.connect(self.db) as con:
            con.execute("DROP TABLE remote_agent_workflow_run_node_sessions")
            con.execute(
                "CREATE TABLE remote_agent_workflow_run_node_sessions "
                "(workflow_run_id TEXT, node_id TEXT, provider TEXT, provider_session_id TEXT, "
                "PRIMARY KEY (workflow_run_id, node_id))"
            )
            con.execute(
                "INSERT INTO remote_agent_workflow_run_node_sessions VALUES (?,?,?,?)",
                (run_id, "planner", "codex", first_id),
            )
            con.execute(
                "INSERT OR REPLACE INTO remote_agent_workflow_run_node_sessions VALUES (?,?,?,?)",
                (run_id, "planner", "codex", second_id),
            )

        result = self.run_budget("usage", "--chain-id", CHAIN, "--db", str(self.db), "--json")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["sessions"], 2)
        self.assertEqual(summary["total_tokens"], 150)

    def test_bound_parent_counts_subagent_descendants_by_exact_ancestry(self):
        self.init_budget()
        run_id = "e" * 32
        parent = "01a06ac7-4d10-7710-b293-2f4a0df2d522"
        child = "01a06ac7-4d10-7710-b293-2f4a0df2d523"
        grandchild = "01a06ac7-4d10-7710-b293-2f4a0df2d524"
        other_parent = "01a06ac7-4d10-7710-b293-2f4a0df2d525"
        unrelated_child = "01a06ac7-4d10-7710-b293-2f4a0df2d526"
        self.write_session(f"rollout-2026-09-03T22-57-21-{parent}.jsonl", 100, session_id=parent)
        self.write_session(f"rollout-2026-09-03T22-57-22-{child}.jsonl", 50, session_id=child, parent_thread_id=parent)
        self.write_session(f"rollout-2026-09-03T22-57-23-{grandchild}.jsonl", 25, session_id=grandchild, parent_thread_id=child)
        self.write_session(f"rollout-2026-09-03T22-57-24-{other_parent}.jsonl", 1000, session_id=other_parent)
        self.write_session(
            f"rollout-2026-09-03T22-57-25-{unrelated_child}.jsonl",
            2000,
            session_id=unrelated_child,
            parent_thread_id=other_parent,
        )
        result = self.run_budget("bind-session", "--chain-id", CHAIN, "--run-id", run_id, "--session-id", parent)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        result = self.run_budget("usage", "--chain-id", CHAIN, "--json")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["sessions"], 3)
        self.assertEqual(summary["total_tokens"], 175)

        self.write_session(f"rollout-2026-09-03T22-57-22-{child}.jsonl", 50, session_id=child)
        self.write_session(f"rollout-2026-09-03T22-57-23-{grandchild}.jsonl", 25, session_id=grandchild)
        self.assertEqual(self.usage()["total_tokens"], 175)

    def test_session_discovery_rejects_suffix_file_without_matching_meta(self):
        self.init_budget()
        run_id = "e" * 32
        session_id = "01a06ac7-4d10-7710-b293-2f4a0df2d522"
        result = self.run_budget("bind-run", "--chain-id", CHAIN, "--run-id", run_id)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.write_session(f"rollout-2026-09-03T22-57-21-{session_id}.jsonl", 123, session_id="01a06ac7-4d10-7710-b293-2f4a0df2d523")
        self.record_session(run_id, "planner", session_id)

        result = self.run_budget("usage", "--chain-id", CHAIN, "--db", str(self.db), "--json")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expected one Codex session file", result.stderr)

    def test_db_recorded_missing_session_file_fails_closed(self):
        self.init_budget()
        run_id = "e" * 32
        result = self.run_budget("bind-run", "--chain-id", CHAIN, "--run-id", run_id)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.record_session(run_id, "planner", "missing-session")

        result = self.run_budget("usage", "--chain-id", CHAIN, "--db", str(self.db), "--json")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expected one Codex session file", result.stderr)

    def test_codex_activity_without_session_fails_closed(self):
        self.init_budget()
        run_id = "e" * 32
        result = self.run_budget("bind-run", "--chain-id", CHAIN, "--run-id", run_id)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.record_codex_activity(run_id)

        result = self.run_budget("usage", "--chain-id", CHAIN, "--db", str(self.db), "--require-sessions", "--json")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("has Codex activity but no recorded Codex session", result.stderr)

    def test_workflow_started_codex_metadata_without_session_is_still_pre_ai(self):
        self.init_budget()
        run_id = "e" * 32
        result = self.run_budget("bind-run", "--chain-id", CHAIN, "--run-id", run_id)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.record_workflow_started(run_id)

        result = self.run_budget("usage", "--chain-id", CHAIN, "--db", str(self.db), "--require-sessions", "--json")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["sessions"], 0)

    def test_wrapper_failure_before_provider_start_is_resumable_without_invented_tokens(self):
        self.init_budget()
        run_id = "e" * 32
        self.run_budget("bind-run", "--chain-id", CHAIN, "--run-id", run_id)
        with sqlite3.connect(self.db) as con:
            con.execute("INSERT INTO remote_agent_workflow_events (workflow_run_id,event_type,step_name,data) VALUES (?,?,?,?)",
                        (run_id, "node_started", "planner", json.dumps({"provider": "codex"})))
            con.execute("INSERT INTO remote_agent_workflow_events (workflow_run_id,event_type,step_name,data) VALUES (?,?,?,?)",
                        (run_id, "node_failed", "planner", json.dumps({"error": "CODEX_WRAPPER=FAIL missing control environment"})))
        result = self.run_budget("usage", "--chain-id", CHAIN, "--db", str(self.db), "--require-sessions", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["total_tokens"], 0)

    def test_token_high_water_prevents_session_truncation_replenishing_budget(self):
        self.init_budget()
        session = self.write_session("stable.jsonl", 500)
        result = self.run_budget(
            "bind-run", "--chain-id", CHAIN, "--run-id", "e" * 32, "--session-file", str(session)
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.usage()["total_tokens"], 500)
        session.write_text(token_line(100) + "\n", encoding="utf-8")

        self.assertEqual(self.usage()["total_tokens"], 500)

    def test_token_high_water_is_per_session_not_global_total(self):
        self.init_budget()
        first = self.write_session("first.jsonl", 100)
        result = self.run_budget(
            "bind-run", "--chain-id", CHAIN, "--run-id", "e" * 32, "--session-file", str(first)
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.usage()["total_tokens"], 100)
        first.write_text(token_line(0) + "\n", encoding="utf-8")
        second = self.write_session("second.jsonl", 50)
        result = self.run_budget(
            "bind-run", "--chain-id", CHAIN, "--run-id", "f" * 32, "--session-file", str(second)
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        self.assertEqual(self.usage()["total_tokens"], 150)

    def test_stale_open_interval_recovers_at_run_stop_event_before_retry(self):
        self.init_budget()
        run_id = "e" * 32
        next_run = "f" * 32
        result = self.run_budget("active-start", "--chain-id", CHAIN, "--run-id", run_id, "--now", "100")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.record_stop(run_id, "approval_requested", "1970-01-01 00:02:40")

        result = self.run_budget(
            "active-start", "--chain-id", CHAIN, "--run-id", next_run, "--db", str(self.db), "--now", "220"
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        summary = self.usage(now=280)
        self.assertEqual(summary["active_seconds"], 120)

    def test_session_file_must_stay_under_codex_home(self):
        self.init_budget()
        outside = self.root / "outside.jsonl"
        outside.write_text(token_line(1), encoding="utf-8")

        result = self.run_budget(
            "bind-run", "--chain-id", CHAIN, "--run-id", "f" * 32, "--session-file", str(outside)
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("session file must live under codex home", result.stderr)


if __name__ == "__main__":
    unittest.main()
