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


def claude_assistant(message_id, input_tokens, cache_creation, cache_read, output_tokens, tool_id=None, tool_name="Read"):
    content = []
    if tool_id is not None:
        content.append({"type": "tool_use", "id": tool_id, "name": tool_name, "input": {}})
    return json.dumps({
        "type": "assistant",
        "message": {
            "id": message_id,
            "model": "claude-opus-5",
            "content": content,
            "usage": {
                "input_tokens": input_tokens,
                "cache_creation_input_tokens": cache_creation,
                "cache_read_input_tokens": cache_read,
                "output_tokens": output_tokens,
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

    def record_provider_event(self, run_id="e" * 32, event_id="evt-1", tokens=None, model="claude-opus-5[1m]"):
        data = {
            "tokens": tokens or {"input": 964_761, "output": 12_765, "cacheRead": 840_703, "cacheWrite": 123_768},
            "model_usage": {"resolved": model},
        }
        with sqlite3.connect(self.db) as con:
            try:
                con.execute("ALTER TABLE remote_agent_workflow_events ADD COLUMN id TEXT")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc):
                    raise
            con.execute(
                "INSERT INTO remote_agent_workflow_events "
                "(id, workflow_run_id, event_type, step_name, data, created_at) VALUES (?,?,?,?,?,?)",
                (event_id, run_id, "node_completed", "plan-gate:on_reject", json.dumps(data), "2026-09-13 00:56:30"),
            )

    def record_tool_owner(self, run_id="e" * 32, tool_id="toolu_01"):
        with sqlite3.connect(self.db) as con:
            con.execute(
                "INSERT INTO remote_agent_workflow_events "
                "(workflow_run_id, event_type, step_name, data) VALUES (?,?,?,?)",
                (run_id, "tool_called", "plan-gate:on_reject", json.dumps({"tool_call_id": tool_id})),
            )

    def write_claude_transcript(self, lines=None):
        path = self.root / "claude.jsonl"
        if lines is None:
            lines = [
                claude_assistant("msg-1", 100, 50_000, 400_000, 5_000, "toolu_01"),
                claude_assistant("msg-1", 100, 50_000, 400_000, 5_000, "toolu_01"),
                claude_assistant("msg-2", 190, 73_768, 440_703, 7_765),
            ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

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

    def test_amend_limit_increases_cap_without_replenishing_usage_or_time(self):
        self.init_budget(tokens=30_000_000)
        session = self.write_session("used.jsonl", 30_154_003)
        run_id = "e" * 32
        for action, now in (("active-start", "100"), ("active-stop", "5131")):
            result = self.run_budget(action, "--chain-id", CHAIN, "--run-id", run_id, "--now", now)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.run_budget(
            "bind-run", "--chain-id", CHAIN, "--run-id", run_id, "--session-file", str(session)
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        result = self.run_budget(
            "amend-limit",
            "--chain-id", CHAIN,
            "--total-tokens", "100000000",
            "--amendment-id", "f" * 64,
            "--reason", "Authorized ENG-3866 retry",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        summary = self.usage(now=6000)
        self.assertEqual(100_000_000, summary["max_total_tokens"])
        self.assertEqual(30_154_003, summary["total_tokens"])
        self.assertEqual(5_031, summary["active_seconds"])

    def test_amend_limit_can_increase_active_minutes_without_replenishing_usage_or_time(self):
        self.init_budget(tokens=100_000_000)
        session = self.write_session("used.jsonl", 30_154_003)
        run_id = "e" * 32
        for action, now in (("active-start", "100"), ("active-stop", "5131")):
            result = self.run_budget(action, "--chain-id", CHAIN, "--run-id", run_id, "--now", now)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.run_budget(
            "bind-run", "--chain-id", CHAIN, "--run-id", run_id, "--session-file", str(session)
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        result = self.run_budget(
            "amend-limit",
            "--chain-id", CHAIN,
            "--total-tokens", "100000000",
            "--total-active-minutes", "480",
            "--amendment-id", "a" * 64,
            "--reason", "Authorized extended retry",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        summary = self.usage(now=6000)
        self.assertEqual(100_000_000, summary["max_total_tokens"])
        self.assertEqual(28_800, summary["wall_seconds"])
        self.assertEqual(30_154_003, summary["total_tokens"])
        self.assertEqual(5_031, summary["active_seconds"])
        self.assertFalse(summary["wall_exhausted"])
        self.assertFalse(summary["tokens_exhausted"])
        state = json.loads((self.control / "feature-budgets" / f"{CHAIN}.json").read_text())
        self.assertEqual(28_800, state["last_usage"]["wall_seconds"])
        self.assertEqual(5_031, state["last_usage"]["active_seconds"])

    def test_amend_limit_retries_pending_journal_idempotently(self):
        self.init_budget(tokens=30_000_000)
        state_path = self.control / "feature-budgets" / f"{CHAIN}.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["pending_amendment"] = {
            "amendment_id": "e" * 64,
            "from_total_tokens": 30_000_000,
            "total_tokens": 100_000_000,
            "reason": "Authorized retry",
            "started_at": "2026-09-12T00:00:00Z",
        }
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

        blocked = self.run_budget("active-start", "--chain-id", CHAIN, "--run-id", "d" * 32, "--now", "100")
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("amendment is incomplete", blocked.stderr)

        second = self.run_budget(
            "amend-limit", "--chain-id", CHAIN, "--total-tokens", "100000000",
            "--amendment-id", "e" * 64, "--reason", "Authorized retry",
        )
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        third = self.run_budget(
            "amend-limit", "--chain-id", CHAIN, "--total-tokens", "100000000",
            "--amendment-id", "e" * 64, "--reason", "Authorized retry",
        )
        self.assertEqual(third.returncode, 0, third.stdout + third.stderr)
        self.assertIn("existing=true", third.stdout)
        self.assertEqual(100_000_000, self.usage()["max_total_tokens"])

    def test_amend_limit_rejects_live_execution_and_decrease(self):
        self.init_budget(tokens=30_000_000)
        result = self.run_budget("active-start", "--chain-id", CHAIN, "--run-id", "e" * 32, "--now", "100")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        active = self.run_budget(
            "amend-limit", "--chain-id", CHAIN, "--total-tokens", "100000000",
            "--amendment-id", "d" * 64, "--reason", "active",
        )
        self.assertNotEqual(active.returncode, 0)
        self.assertIn("active interval is open", active.stderr)
        result = self.run_budget("active-stop", "--chain-id", CHAIN, "--run-id", "e" * 32, "--now", "120")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        decrease = self.run_budget(
            "amend-limit", "--chain-id", CHAIN, "--total-tokens", "10000000",
            "--amendment-id", "c" * 64, "--reason", "decrease",
        )
        self.assertNotEqual(decrease.returncode, 0)
        self.assertIn("may only increase", decrease.stderr)

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

    def test_provider_usage_receipt_adds_exact_incident_total_once(self):
        self.init_budget(tokens=2_000_000)
        run_id = "e" * 32
        self.run_budget("bind-run", "--chain-id", CHAIN, "--run-id", run_id)
        self.record_provider_event(run_id)
        self.record_tool_owner(run_id, "toolu_01")
        transcript = self.write_claude_transcript()

        result = self.run_budget(
            "account-provider-usage", "--chain-id", CHAIN, "--run-id", run_id,
            "--db", str(self.db), "--event-id", "evt-1", "--transcript", str(transcript),
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        summary = self.usage()
        self.assertEqual(977_526, summary["total_tokens"])
        self.assertEqual(1, summary["provider_receipts"])
        self.assertEqual(964_761, summary["input_tokens"])
        self.assertEqual(840_703, summary["cached_input_tokens"])
        self.assertEqual(12_765, summary["output_tokens"])
        self.assertEqual(summary["total_tokens"], summary["input_tokens"] + summary["output_tokens"])
        state = json.loads((self.control / "feature-budgets" / f"{CHAIN}.json").read_text(encoding="utf-8"))
        receipt = state["provider_usage_receipts"][0]
        self.assertEqual(290, receipt["provider_usage"]["uncached_input_tokens"])
        self.assertEqual(123_768, receipt["provider_usage"]["cache_creation_input_tokens"])
        self.assertEqual(840_703, receipt["provider_usage"]["cache_read_input_tokens"])
        self.assertEqual(964_761, receipt["provider_usage"]["inclusive_input_tokens"])
        self.assertEqual(840_703, receipt["usage"]["cached_input_tokens"])
        self.assertEqual(receipt["usage"]["total_tokens"], receipt["usage"]["input_tokens"] + receipt["usage"]["output_tokens"])
        self.assertEqual(0, state["token_high_water"]["total_tokens"])

        repeated = self.run_budget(
            "account-provider-usage", "--chain-id", CHAIN, "--run-id", run_id,
            "--db", str(self.db), "--event-id", "evt-1", "--transcript", str(transcript),
        )
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertIn("existing=true", repeated.stdout)
        self.assertEqual(977_526, self.usage()["total_tokens"])
        state = json.loads((self.control / "feature-budgets" / f"{CHAIN}.json").read_text(encoding="utf-8"))
        self.assertEqual(0, state["token_high_water"]["total_tokens"])

    def test_provider_usage_rejects_conflicting_or_unowned_evidence(self):
        self.init_budget(tokens=2_000_000)
        run_id = "e" * 32
        self.run_budget("bind-run", "--chain-id", CHAIN, "--run-id", run_id)
        self.record_provider_event(run_id)
        transcript = self.write_claude_transcript()

        unowned = self.run_budget(
            "account-provider-usage", "--chain-id", CHAIN, "--run-id", run_id,
            "--db", str(self.db), "--event-id", "evt-1", "--transcript", str(transcript),
        )
        self.assertNotEqual(unowned.returncode, 0)
        self.assertIn("not owned by the registered run", unowned.stderr)

        self.record_tool_owner(run_id, "toolu_01")
        ok = self.run_budget(
            "account-provider-usage", "--chain-id", CHAIN, "--run-id", run_id,
            "--db", str(self.db), "--event-id", "evt-1", "--transcript", str(transcript),
        )
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        conflict = self.write_claude_transcript([
            claude_assistant("msg-x", 290, 123_768, 840_703, 12_765),
        ])
        result = self.run_budget(
            "account-provider-usage", "--chain-id", CHAIN, "--run-id", run_id,
            "--db", str(self.db), "--event-id", "evt-1", "--transcript", str(conflict),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("different evidence", result.stderr)

    def test_provider_usage_rejects_malformed_negative_mismatch_and_wrong_run(self):
        self.init_budget(tokens=2_000_000)
        run_id = "e" * 32
        self.run_budget("bind-run", "--chain-id", CHAIN, "--run-id", run_id)
        self.record_provider_event("f" * 32)
        transcript = self.write_claude_transcript()

        wrong_run = self.run_budget(
            "account-provider-usage", "--chain-id", CHAIN, "--run-id", run_id,
            "--db", str(self.db), "--event-id", "evt-1", "--transcript", str(transcript),
        )
        self.assertNotEqual(wrong_run.returncode, 0)
        self.assertIn("does not belong", wrong_run.stderr)

        with sqlite3.connect(self.db) as con:
            con.execute("DELETE FROM remote_agent_workflow_events")
        self.record_provider_event(run_id, tokens={"input": 1, "output": 1, "cacheRead": 2, "cacheWrite": 0})
        negative = self.run_budget(
            "account-provider-usage", "--chain-id", CHAIN, "--run-id", run_id,
            "--db", str(self.db), "--event-id", "evt-1", "--transcript", str(transcript),
        )
        self.assertNotEqual(negative.returncode, 0)
        self.assertIn("cache tokens exceed", negative.stderr)

        malformed = self.root / "bad.jsonl"
        malformed.write_text("{not-json}\n", encoding="utf-8")
        with sqlite3.connect(self.db) as con:
            con.execute("DELETE FROM remote_agent_workflow_events")
        self.record_provider_event(run_id)
        bad = self.run_budget(
            "account-provider-usage", "--chain-id", CHAIN, "--run-id", run_id,
            "--db", str(self.db), "--event-id", "evt-1", "--transcript", str(malformed),
        )
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("malformed JSONL", bad.stderr)

        mismatch = self.write_claude_transcript([claude_assistant("msg-1", 1, 2, 3, 4)])
        bad_total = self.run_budget(
            "account-provider-usage", "--chain-id", CHAIN, "--run-id", run_id,
            "--db", str(self.db), "--event-id", "evt-1", "--transcript", str(mismatch),
        )
        self.assertNotEqual(bad_total.returncode, 0)
        self.assertIn("usage does not match", bad_total.stderr)

    def test_provider_usage_preserves_old_aggregate_highwater_without_folding_receipts_into_it(self):
        self.init_budget(tokens=1_000)
        run_id = "e" * 32
        session = self.write_session("used.jsonl", 50)
        self.run_budget("bind-run", "--chain-id", CHAIN, "--run-id", run_id, "--session-file", str(session))
        state_path = self.control / "feature-budgets" / f"{CHAIN}.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["token_high_water"] = {"input_tokens": 90, "cached_input_tokens": 0, "output_tokens": 10, "total_tokens": 100}
        state["provider_usage_receipts"] = [{
            "kind": "provider-usage-receipt",
            "run_id": run_id,
            "provider": "claude",
            "model": "claude-opus-5",
            "event_id": "evt-low",
            "event_sha256": "a" * 64,
            "transcript_sha256": "b" * 64,
            "provider_usage": {
                "uncached_input_tokens": 7,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "inclusive_input_tokens": 7,
                "output_tokens": 3,
                "total_tokens": 10,
            },
            "usage": {
                "input_tokens": 7,
                "cached_input_tokens": 0,
                "output_tokens": 3,
                "total_tokens": 10,
            },
        }]
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

        summary = self.usage()
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(110, summary["total_tokens"])
        self.assertEqual(100, state["token_high_water"]["total_tokens"])
        self.assertEqual({"input_tokens": 90, "cached_input_tokens": 0, "output_tokens": 10, "total_tokens": 100},
                         state["token_high_water"])
        self.assertEqual(110, self.usage()["total_tokens"])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(100, state["token_high_water"]["total_tokens"])


if __name__ == "__main__":
    unittest.main()
