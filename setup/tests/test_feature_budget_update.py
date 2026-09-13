#!/usr/bin/env python3
import importlib.util
import json
import contextlib
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SETUP = Path(__file__).resolve().parent.parent
CHAIN = "a" * 32
RUN = "b" * 32

sys.path.insert(0, str(SETUP))

ar_spec = importlib.util.spec_from_file_location("archon_run", SETUP / "archon-run.py")
assert ar_spec and ar_spec.loader
ar = importlib.util.module_from_spec(ar_spec)
ar_spec.loader.exec_module(ar)

fc_spec = importlib.util.spec_from_file_location("feature_chain", SETUP / "feature_chain.py")
assert fc_spec and fc_spec.loader
fc = importlib.util.module_from_spec(fc_spec)
fc_spec.loader.exec_module(fc)

budget_spec = importlib.util.spec_from_file_location("feature_budget", SETUP / "feature-budget.py")
budget = importlib.util.module_from_spec(budget_spec)
budget_spec.loader.exec_module(budget)


def token_line(total):
    return json.dumps({
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": total - 3,
                    "cached_input_tokens": 0,
                    "output_tokens": 3,
                    "total_tokens": total,
                }
            },
        },
    })


class FeatureBudgetUpdate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.control = self.root / "control"
        self.control.mkdir(mode=0o700)
        self.codex_home = self.root / "codex-home"
        (self.codex_home / "sessions/2026/09/12").mkdir(parents=True)
        self.db = self.root / "archon.db"
        self.output_root = self.root / "out"
        self.artifacts = self.output_root / "artifacts" / "runs" / RUN
        self.artifacts.mkdir(parents=True)
        self.row = {
            "id": RUN,
            "workflow_name": "full-sdlc-api-codex",
            "user_message": str(self.root / "spec.md"),
            "status": "failed",
            "output_root": str(self.output_root),
        }
        (self.root / "spec.md").write_text("# Feature\n", encoding="utf-8")
        self.args = Namespace(
            control_dir=self.control,
            codex_home=self.codex_home,
            db=self.db,
            token="operator-token",
            total_tokens=100_000_000,
            total_active_minutes=None,
            enable_shepherd=True,
            reason="Authorized ENG-3866 retry",
            action="feature-budget-update",
        )
        self.transcript = self.root / "claude.jsonl"
        self.transcript.write_text("{}\n", encoding="utf-8")
        with sqlite3.connect(self.db) as con:
            con.execute(
                "CREATE TABLE remote_agent_workflow_runs "
                "(id TEXT, workflow_name TEXT, user_message TEXT, status TEXT, output_root TEXT, started_at TEXT)"
            )
            con.execute(
                "CREATE TABLE remote_agent_workflow_run_node_sessions "
                "(workflow_run_id TEXT, node_id TEXT, provider TEXT, provider_session_id TEXT)"
            )
            con.execute(
                "CREATE TABLE remote_agent_workflow_events "
                "(workflow_run_id TEXT, event_type TEXT, step_name TEXT, data TEXT, created_at TEXT)"
            )
            con.execute(
                "INSERT INTO remote_agent_workflow_runs VALUES (?,?,?,?,?,?)",
                (RUN, self.row["workflow_name"], self.row["user_message"], "failed", str(self.output_root), "2026-09-12 00:00:00"),
            )
        self.host = SimpleNamespace(
            require_control_token=ar.require_control_token,
            read_control_state=ar.read_control_state,
            secure_write_json=ar.secure_write_json,
            control_state_path=ar.control_state_path,
            run_row_by_id=ar.run_row_by_id,
            process_fingerprint=lambda _pgid: None,
        )
        self.real_killpg = fc.os.killpg
        groups = mock.patch.object(fc.os, "killpg", side_effect=ProcessLookupError)
        groups.start()
        self.addCleanup(groups.stop)
        self.write_state()
        self.init_budget()
        self.write_control()

    def write_state(self):
        state = {
            "schema_version": 2,
            "kind": "archon-feature-chain",
            "logical_chain_id": CHAIN,
            "chain_secret": "s" * 48,
            "provider": "codex",
            "scope": "repositories",
            "repositories": ["api", "goodword-mcp"],
            "spec": str(self.root / "spec.md"),
            "spec_sha256": "0" * 64,
            "approval": {"digest": "frozen"},
            "approved_plan": {"plan": "frozen"},
            "stages": {"api": {"status": "pending"}, "goodword-mcp": {"status": "pending"}},
            "worktrees": {"api": {"worktree": "/tmp/api"}, "goodword-mcp": {"worktree": "/tmp/mcp"}},
            "child_runs": {},
            "current_run": {"phase": "planning", "run_id": RUN, "artifacts_dir": str(self.artifacts), "write_roots": [str(self.artifacts)]},
            "pending_control": None,
            "budget": {"wall_minutes": 240, "max_total_tokens": 30_000_000, "ledger": "feature-budget.py"},
            "created_at": "2026-09-12T00:00:00Z",
            "updated_at": "2026-09-12T00:00:00Z",
        }
        fc.write_state(self.control, state)

    def init_budget(self):
        result = subprocess.run(
            [
                sys.executable, str(SETUP / "feature-budget.py"),
                "--control-dir", str(self.control), "--codex-home", str(self.codex_home),
                "init", "--chain-id", CHAIN, "--wall-minutes", "240", "--max-total-tokens", "30000000",
            ],
            capture_output=True, encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def write_control(self, token="operator-token"):
        control = {
            "action": "resume",
            "run": RUN,
            "control_token_hash": ar.token_digest(token),
            "launcher_pgid": 111,
            "launcher_fingerprint": "launcher",
            "watchdog_pgid": 222,
            "watchdog_fingerprint": "watchdog",
            "wall_minutes": 240,
            "max_total_tokens": 30_000_000,
            "feature_chain": {
                "logical_chain_id": CHAIN,
                "provider": "codex",
                "scope": "repositories",
                "phase": "planning",
                "repo": None,
                "run_id": RUN,
                "write_roots": [str(self.artifacts)],
            },
        }
        control["authority_mac"] = ar.authority_mac(token, control)
        ar.secure_write_json(ar.control_state_path(self.row, self.control), control)
        (self.artifacts / "codex-lite-control.json").write_text(
            json.dumps({"run": RUN, "max_total_tokens": 30_000_000}) + "\n",
            encoding="utf-8",
        )

    def ledger_usage(self):
        result = subprocess.run(
            [
                sys.executable, str(SETUP / "feature-budget.py"),
                "--control-dir", str(self.control), "--codex-home", str(self.codex_home),
                "usage", "--chain-id", CHAIN, "--now", "6000", "--json",
            ],
            capture_output=True, encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def legacy_token_only_amendment_id(self):
        return fc.digest({
            "kind": "feature-budget-update",
            "logical_chain_id": CHAIN,
            "run_id": RUN,
            "total_tokens": self.args.total_tokens,
            "enable_shepherd": bool(self.args.enable_shepherd),
            "reason": self.args.reason,
        })

    def seed_consumed_budget(self):
        for action, now in (("active-start", "100"), ("active-stop", "5131")):
            subprocess.run(
                [
                    sys.executable, str(SETUP / "feature-budget.py"), "--control-dir", str(self.control),
                    "--codex-home", str(self.codex_home), action, "--chain-id", CHAIN,
                    "--run-id", RUN, "--now", now,
                ],
                check=True,
            )
        session = self.codex_home / "sessions/2026/09/12/planning.jsonl"
        session.write_text(token_line(30_154_003) + "\n", encoding="utf-8")
        subprocess.run(
            [
                sys.executable, str(SETUP / "feature-budget.py"), "--control-dir", str(self.control),
                "--codex-home", str(self.codex_home), "bind-run", "--chain-id", CHAIN,
                "--run-id", RUN, "--session-file", str(session),
            ],
            check=True,
        )

    def test_guarded_update_preserves_usage_time_state_and_token(self):
        self.seed_consumed_budget()
        result = fc.budget_update_command(self.host, self.args, self.row)
        state = fc.read_state(self.control, CHAIN)
        usage = self.ledger_usage()

        self.assertEqual(CHAIN, result["chain"])
        self.assertEqual(100_000_000, state["budget"]["max_total_tokens"])
        self.assertEqual(1, state["budget_shepherd_version"])
        self.assertEqual({"digest": "frozen"}, state["approval"])
        self.assertEqual(RUN, state["current_run"]["run_id"])
        self.assertEqual(100_000_000, usage["max_total_tokens"])
        self.assertEqual(30_154_003, usage["total_tokens"])
        self.assertEqual(5_031, usage["active_seconds"])
        self.assertEqual(100_000_000, ar.require_control_token(self.row, self.control, "operator-token")["max_total_tokens"])
        self.assertEqual(100_000_000, json.loads((self.artifacts / "codex-lite-control.json").read_text())["max_total_tokens"])

        fc.budget_update_command(self.host, self.args, self.row)
        repeated = fc.read_state(self.control, CHAIN)
        self.assertEqual(1, len(repeated["budget_amendments"]))

        with mock.patch.object(ar, "feature_repository_controller", return_value=fc):
            checkpoint = ar.supervisor_feature_shepherd_checkpoint(self.args, self.db, self.row, None)
        self.assertIsInstance(checkpoint, str)
        forecast = json.loads((self.artifacts / "budget-forecast.json").read_text())
        self.assertEqual(30_154_003, forecast["used_tokens"])
        self.assertEqual(69_845_997, forecast["remaining_allowance"])

    def test_token_only_retry_accepts_legacy_pending_amendment_id(self):
        self.seed_consumed_budget()
        legacy_id = self.legacy_token_only_amendment_id()
        state = fc.read_state(self.control, CHAIN)
        state["budget_amendment"] = {
            "amendment_id": legacy_id,
            "run_id": RUN,
            "from_total_tokens": 30_000_000,
            "from_wall_minutes": 240,
            "total_tokens": 100_000_000,
            "reason": self.args.reason,
            "enable_shepherd": True,
            "status": "in_progress",
            "started_at": "2026-09-12T00:00:00Z",
        }
        fc.write_state(self.control, state)

        result = fc.budget_update_command(self.host, self.args, self.row)
        state = fc.read_state(self.control, CHAIN)
        usage = self.ledger_usage()
        control = ar.require_control_token(self.row, self.control, "operator-token")

        self.assertEqual(legacy_id, result["amendment_id"])
        self.assertEqual(legacy_id, state["budget_amendments"][0]["amendment_id"])
        self.assertEqual(100_000_000, usage["max_total_tokens"])
        self.assertEqual(14_400, usage["wall_seconds"])
        self.assertEqual(30_154_003, usage["total_tokens"])
        self.assertEqual(5_031, usage["active_seconds"])
        self.assertEqual(100_000_000, control["max_total_tokens"])

    def test_token_only_retry_accepts_legacy_applied_amendment_id(self):
        self.seed_consumed_budget()
        legacy_id = self.legacy_token_only_amendment_id()
        budget.command_amend_limit(Namespace(
            control_dir=self.control,
            chain_id=CHAIN,
            total_tokens=100_000_000,
            total_active_minutes=None,
            amendment_id=legacy_id,
            reason=self.args.reason,
        ))
        state = fc.read_state(self.control, CHAIN)
        state["budget"]["max_total_tokens"] = 100_000_000
        state.setdefault("budget_amendments", []).append({
            "amendment_id": legacy_id,
            "run_id": RUN,
            "from_total_tokens": 30_000_000,
            "from_wall_minutes": 240,
            "total_tokens": 100_000_000,
            "reason": self.args.reason,
            "enable_shepherd": True,
            "status": "applied",
            "started_at": "2026-09-12T00:00:00Z",
            "applied_at": "2026-09-12T00:00:01Z",
        })
        fc.write_state(self.control, state)

        result = fc.budget_update_command(self.host, self.args, self.row)
        state = fc.read_state(self.control, CHAIN)
        usage = self.ledger_usage()
        control = ar.require_control_token(self.row, self.control, "operator-token")

        self.assertEqual(legacy_id, result["amendment_id"])
        self.assertEqual(1, len(state["budget_amendments"]))
        self.assertEqual(legacy_id, state["budget_amendments"][0]["amendment_id"])
        self.assertEqual(100_000_000, usage["max_total_tokens"])
        self.assertEqual(14_400, usage["wall_seconds"])
        self.assertEqual(30_154_003, usage["total_tokens"])
        self.assertEqual(5_031, usage["active_seconds"])
        self.assertEqual(100_000_000, control["max_total_tokens"])

    def test_guarded_update_can_extend_active_minutes_without_token_increase(self):
        self.seed_consumed_budget()
        self.args.total_tokens = 30_000_000
        self.args.total_active_minutes = 480

        result = fc.budget_update_command(self.host, self.args, self.row)
        state = fc.read_state(self.control, CHAIN)
        usage = self.ledger_usage()
        control = ar.require_control_token(self.row, self.control, "operator-token")
        public = json.loads((self.artifacts / "codex-lite-control.json").read_text())

        self.assertEqual(CHAIN, result["chain"])
        self.assertEqual(30_000_000, state["budget"]["max_total_tokens"])
        self.assertEqual(480, state["budget"]["wall_minutes"])
        self.assertEqual(30_000_000, usage["max_total_tokens"])
        self.assertEqual(28_800, usage["wall_seconds"])
        self.assertEqual(30_154_003, usage["total_tokens"])
        self.assertEqual(5_031, usage["active_seconds"])
        self.assertEqual(480, control["wall_minutes"])
        self.assertEqual(480, public["wall_minutes"])

    def test_update_rejects_stale_token_active_run_and_competing_control(self):
        self.args.token = "wrong"
        with self.assertRaises(SystemExit):
            fc.budget_update_command(self.host, self.args, self.row)
        self.args.token = "operator-token"
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE remote_agent_workflow_runs SET status = 'running' WHERE id = ?", (RUN,))
        active = dict(self.row, status="failed")
        with self.assertRaisesRegex(fc.FeatureChainError, "stopped run"):
            fc.budget_update_command(self.host, self.args, active)
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE remote_agent_workflow_runs SET status = 'failed' WHERE id = ?", (RUN,))
        state = fc.read_state(self.control, CHAIN)
        state["pending_control"] = {"owner_pid": 1, "owner_fingerprint": "still-live"}
        fc.write_state(self.control, state)
        with mock.patch.object(fc, "process_claim_alive", return_value=True):
            with self.assertRaisesRegex(fc.FeatureChainError, "control already in progress"):
                fc.budget_update_command(self.host, self.args, self.row)

    def test_update_rejects_live_control_process_and_dispatch_reservation(self):
        with mock.patch.object(fc.os, "killpg", return_value=None):
            with self.assertRaisesRegex(fc.FeatureChainError, "launcher process group is live"):
                fc.budget_update_command(self.host, self.args, self.row)
        state = fc.read_state(self.control, CHAIN)
        state["dispatch_reservation"] = {"owner_pid": 1, "owner_fingerprint": "still-live", "phase": "planning"}
        fc.write_state(self.control, state)
        with mock.patch.object(fc, "process_claim_alive", return_value=True):
            with self.assertRaisesRegex(fc.FeatureChainError, "dispatch already in progress"):
                fc.budget_update_command(self.host, self.args, self.row)

    def test_incomplete_chain_journal_blocks_dispatch_and_retries(self):
        with mock.patch.object(fc, "budget_amend_allowance", side_effect=fc.FeatureChainError("boom")):
            with self.assertRaisesRegex(fc.FeatureChainError, "boom"):
                fc.budget_update_command(self.host, self.args, self.row)
        state = fc.read_state(self.control, CHAIN)
        self.assertEqual("in_progress", state["budget_amendment"]["status"])
        with self.assertRaisesRegex(fc.FeatureChainError, "amendment is incomplete"):
            fc.shepherd_unlocked(self.args, state)

        fc.budget_update_command(self.host, self.args, self.row)
        state = fc.read_state(self.control, CHAIN)
        self.assertIsNone(state["budget_amendment"])
        self.assertEqual(100_000_000, state["budget"]["max_total_tokens"])

    def test_retry_after_every_persisted_update_preserves_operator_authority(self):
        ledger_path = self.control / "feature-budgets" / f"{CHAIN}.json"

        def amend_in_process(args, chain_id, total, total_active_minutes, amendment_id, reason):
            budget.command_amend_limit(Namespace(control_dir=args.control_dir, chain_id=chain_id,
                                                 total_tokens=total, total_active_minutes=total_active_minutes,
                                                 amendment_id=amendment_id, reason=reason))

        for crash_after in range(1, 7):
            with self.subTest(persisted_write=crash_after):
                self.write_state()
                self.write_control()
                ledger_path.unlink()
                self.init_budget()
                self.seed_consumed_budget()
                self.args.total_active_minutes = 480
                before_ledger = budget.read_budget(ledger_path)
                writes = 0

                def crash_wrapper(original):
                    def write(*args, **kwargs):
                        nonlocal writes
                        result = original(*args, **kwargs)
                        writes += 1
                        if writes == crash_after:
                            raise RuntimeError("interrupted after durable write")
                        return result
                    return write

                with contextlib.ExitStack() as stack:
                    stack.enter_context(mock.patch.object(fc, "budget_amend_allowance", side_effect=amend_in_process))
                    for owner, name in ((fc, "write_state"), (budget, "write_budget"),
                                        (self.host, "secure_write_json"), (fc, "write_json_atomic")):
                        stack.enter_context(mock.patch.object(owner, name, side_effect=crash_wrapper(getattr(owner, name))))
                    with self.assertRaisesRegex(RuntimeError, "interrupted after durable write"):
                        fc.budget_update_command(self.host, self.args, self.row)
                self.assertEqual(crash_after, writes)
                ar.require_control_token(self.row, self.control, "operator-token")
                interrupted = fc.read_state(self.control, CHAIN)
                if crash_after < 6:
                    with self.assertRaisesRegex(fc.FeatureChainError, "amendment is incomplete"):
                        fc.require_no_incomplete_amendment(interrupted)
                fc.budget_update_command(self.host, self.args, self.row)
                state = fc.read_state(self.control, CHAIN)
                ledger = budget.read_budget(ledger_path)
                self.assertEqual(100_000_000, state["budget"]["max_total_tokens"])
                self.assertEqual(480, state["budget"]["wall_minutes"])
                self.assertEqual(100_000_000, ledger["max_total_tokens"])
                self.assertEqual(28_800, ledger["wall_seconds"])
                for key in ("runs", "active_intervals", "token_high_water", "session_token_high_water"):
                    self.assertEqual(before_ledger.get(key), ledger.get(key), key)
                self.assertEqual(30_154_003, self.ledger_usage()["total_tokens"])
                self.assertEqual(5_031, self.ledger_usage()["active_seconds"])
                self.assertEqual(1, len(state["budget_amendments"]))
                self.assertEqual(1, len(ledger["amendments"]))
                control = ar.require_control_token(self.row, self.control, "operator-token")
                self.assertEqual(100_000_000, control["max_total_tokens"])
                self.assertEqual(480, control["wall_minutes"])
                self.args.total_active_minutes = None

    def test_concurrent_identical_requests_apply_once(self):
        control = ar.read_control_state(self.row, self.control)
        control.update(launcher_pgid=None, watchdog_pgid=None)
        control["authority_mac"] = ar.authority_mac("operator-token", control)
        ar.secure_write_json(ar.control_state_path(self.row, self.control), control)
        script = """
import importlib.util, json, sys
from argparse import Namespace
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import feature_chain
spec = importlib.util.spec_from_file_location('launcher', Path(sys.argv[1]) / 'archon-run.py')
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)
args = Namespace(**json.loads(sys.argv[2]))
args.control_dir = Path(args.control_dir)
args.codex_home = Path(args.codex_home)
args.db = Path(args.db)
row = host.run_row_by_id(args.db, sys.argv[3])
feature_chain.budget_update_command(host, args, row)
"""
        arguments = json.dumps(vars(self.args), default=str)
        processes = [subprocess.Popen([sys.executable, "-c", script, str(SETUP), arguments, RUN],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(0, process.returncode, stdout + stderr)
        state = fc.read_state(self.control, CHAIN)
        self.assertEqual(1, len(state["budget_amendments"]))
        self.assertEqual(100_000_000, state["budget"]["max_total_tokens"])

    def test_incomplete_request_cannot_change_shepherd_option(self):
        with mock.patch.object(fc, "budget_amend_allowance", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError):
                fc.budget_update_command(self.host, self.args, self.row)
        self.args.enable_shepherd = False
        with self.assertRaisesRegex(fc.FeatureChainError, "different budget amendment"):
            fc.budget_update_command(self.host, self.args, self.row)

    def test_orphaned_group_member_blocks_amendment_after_leader_exits(self):
        script = "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)"
        leader = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
        try:
            leader.wait(timeout=10)
            self.assertIsNone(ar.process_fingerprint(leader.pid))
            with mock.patch.object(fc.os, "killpg", side_effect=self.real_killpg):
                with self.assertRaisesRegex(fc.FeatureChainError, "launcher process group is live"):
                    fc.require_no_live_control_processes({"launcher_pgid": leader.pid})
        finally:
            try:
                self.real_killpg(leader.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_account_provider_usage_is_guarded_and_delegates_to_private_ledger(self):
        self.seed_consumed_budget()
        args = Namespace(
            control_dir=self.control,
            codex_home=self.codex_home,
            db=self.db,
            token="operator-token",
            event_id="d2f8f63a-2b7d-4023-9209-d56851b28e02",
            transcript=self.transcript,
        )
        with mock.patch.object(fc, "budget_account_provider_usage") as account:
            result = fc.account_provider_usage_command(self.host, args, self.row)

        self.assertEqual(CHAIN, result["chain"])
        self.assertEqual(RUN, result["run_id"])
        account.assert_called_once_with(args, CHAIN, RUN, args.event_id, self.transcript)

    def test_account_provider_usage_rejects_stale_token_and_active_run(self):
        args = Namespace(
            control_dir=self.control,
            codex_home=self.codex_home,
            db=self.db,
            token="wrong-token",
            event_id="evt",
            transcript=self.transcript,
        )
        with self.assertRaises(SystemExit):
            fc.account_provider_usage_command(self.host, args, self.row)

        args.token = "operator-token"
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE remote_agent_workflow_runs SET status = 'running' WHERE id = ?", (RUN,))
        with self.assertRaisesRegex(fc.FeatureChainError, "requires a stopped run"):
            fc.account_provider_usage_command(self.host, args, self.row)


if __name__ == "__main__":
    unittest.main()
