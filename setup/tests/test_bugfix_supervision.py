#!/usr/bin/env python3
import contextlib
import importlib.util
import io
import json
import hashlib
import sqlite3
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

SETUP = Path(__file__).resolve().parent.parent
SCRIPT = SETUP / "archon-run.py"
spec = importlib.util.spec_from_file_location("archon_run", SCRIPT)
assert spec and spec.loader
ar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ar)


class BugfixSupervision(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "archon.db"
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE remote_agent_workflow_runs (id TEXT, workflow_name TEXT, user_message TEXT, status TEXT, output_root TEXT, started_at TEXT)")
        con.execute("CREATE TABLE remote_agent_workflow_events (workflow_run_id TEXT, created_at TEXT, node_name TEXT, payload TEXT)")
        con.execute("INSERT INTO remote_agent_workflow_runs VALUES (?,?,?,?,?,?)", ("a" * 32, "bugfix-codex", "/tmp/report.md", "paused", str(self.root / "out"), "2026-09-01 12:00:00"))
        con.execute("INSERT INTO remote_agent_workflow_events VALUES (?,?,?,?)", ("a" * 32, "2026-09-01 12:01:00", "rca-approval", json.dumps({"control_token": "secret"})))
        con.commit(); con.close()

    def test_supervise_exact_run_stops_on_named_gate(self):
        result = ar.supervise_exact_run(self.db, "a" * 32, 1, 0.01)
        self.assertEqual(result["state"], "gate")
        self.assertEqual(result["gate"], "rca-approval")
        self.assertEqual(result["run"], "a" * 32)

    def test_supervision_handoff_surfaces_packet_scope_and_rejection_reason(self):
        row = ar.run_row_by_id(self.db, "a" * 32)
        ad = ar.artifact_dir(row)
        ad.mkdir(parents=True)
        (ad / "rca-review.html").write_text("packet")
        (ad / "bugfix-chain.json").write_text(json.dumps({"logical_chain_id": "chain-1"}))
        (ad / "fix-classification.json").write_text(json.dumps({
            "implementation_result": "PARTIAL_FIX",
            "ticket_disposition": "OPEN",
            "ticket_closure_allowed": False,
            "open_effective_ids": ["E2"],
        }))

        result = ar.supervise_exact_run(self.db, "a" * 32, 1, 0.01)

        self.assertEqual(result["packet"], str(ad / "rca-review.html"))
        self.assertEqual(result["chain"], "chain-1")
        self.assertEqual(result["classification"], "PARTIAL_FIX")
        self.assertEqual(result["open_symptoms"], "E2")
        self.assertEqual(result["recommended_action"], "reject-or-human-residual-decision")

    def test_product_smoke_failure_dominates_handoff_recommendation(self):
        row = ar.run_row_by_id(self.db, "a" * 32)
        ad = ar.artifact_dir(row)
        ad.mkdir(parents=True)
        (ad / "fix-classification.json").write_text(json.dumps({
            "implementation_result": "FULL_FIX",
            "ticket_disposition": "RESOLVED",
            "ticket_closure_allowed": True,
            "open_effective_ids": [],
        }))
        (ad / "smoke-matrix.json").write_text(json.dumps({"rows": [{
            "id": "visible-result", "kind": "auto", "result": "fail",
            "failure_class": "product", "observed": "wrong result",
        }]}))

        result = ar.supervise_exact_run(self.db, "a" * 32, 1, 0.01)

        self.assertEqual(result["product_failures"], "visible-result")
        self.assertEqual(result["recommended_action"], "reject-product-failure")

    def test_rejection_releases_the_previous_paused_launcher_group(self):
        row = ar.run_row_by_id(self.db, "a" * 32)
        previous = {"launcher_pgid": 1234, "launcher_fingerprint": "owned"}
        with mock.patch.object(ar, "process_fingerprint", return_value="owned"), \
             mock.patch.object(ar, "terminate_group") as terminate:
            ar.cleanup_after_rejection(row, previous, self.db)
        terminate.assert_called_once_with(1234, expected_fingerprint="owned")

    def test_smoke_rejection_removes_only_the_known_disposable_web_worktree(self):
        row = ar.run_row_by_id(self.db, "a" * 32)
        con = sqlite3.connect(self.db)
        con.execute("DELETE FROM remote_agent_workflow_events")
        con.execute(
            "INSERT INTO remote_agent_workflow_events VALUES (?,?,?,?)",
            ("a" * 32, "2026-09-01 12:02:00", "smoke-approval", "{}"),
        )
        con.commit()
        con.close()
        fake_root = self.root / "goodword"
        worktree = fake_root / "web-app" / ".worktrees" / "bugfix-smoke"
        worktree.mkdir(parents=True)
        ad = ar.artifact_dir(row)
        (ad / "smoke-stack").mkdir(parents=True)
        (ad / "smoke-stack" / "web-dir.txt").write_text(str(worktree))
        completed = mock.Mock(returncode=0, stderr="")
        with mock.patch.object(ar, "ROOT", fake_root), \
             mock.patch.object(ar.subprocess, "run", return_value=completed) as run:
            ar.cleanup_after_rejection(row, None, self.db)
        self.assertEqual(run.call_args_list[0].args[0][-2:], ["--force", str(worktree.resolve())])

    def test_baseline_capture_resolves_origin_main_commits_not_checkout_head(self):
        completed = mock.Mock(returncode=0, stdout="a" * 40 + "\n", stderr="")
        with mock.patch.object(ar.subprocess, "run", return_value=completed) as run:
            baseline = ar.capture_bugfix_baseline(self.root)
        self.assertEqual(baseline["commits"]["api"], "a" * 40)
        rev_calls = [call for call in run.call_args_list if "rev-parse" in call.args[0]]
        self.assertEqual(len(rev_calls), 2)
        for call in rev_calls:
            self.assertEqual(call.args[0][-2:], ["--verify", "origin/main^{commit}"])

    def test_failed_fix_seal_captures_tracked_and_untracked_allowlisted_files(self):
        repo = self.root / "repo"
        repo.mkdir()
        subprocess = ar.subprocess
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        (repo / "tracked.ts").write_text("before\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
        baseline = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                                  capture_output=True, text=True).stdout.strip()
        (repo / "tracked.ts").write_text("after\n")
        (repo / "new-test.ts").write_text("new red test\n")
        row = {"id": "9" * 32, "workflow_name": "bugfix", "output_root": str(self.root / "out")}
        ad = ar.artifact_dir(row)
        (ad / "attempt-1").mkdir(parents=True)
        (ad / "attempt-1" / "green.json").write_text('{"green":false,"reason":"red"}')
        (ad / "params.json").write_text(json.dumps({"worktree": str(repo), "repo": "api", "branch": "archon/test"}))
        (ad / "files-allowlist.json").write_text(json.dumps(["tracked.ts", "new-test.ts"]))
        state = {"baseline": {"commits": {"api": baseline}}}
        ar.seal_failed_fix_evidence(row, 1, state)
        patch = (ad / "failed-patch.diff").read_text()
        self.assertIn("-before", patch)
        self.assertIn("+after", patch)
        untracked = json.loads((ad / "failed-untracked.json").read_text())
        self.assertIn("new-test.ts", untracked)
        self.assertEqual(untracked["new-test.ts"]["sha256"], hashlib.sha256(b"new red test\n").hexdigest())

    def test_supervise_command_writes_redacted_handoff_without_token(self):
        handoff = self.root / "handoff.json"
        args = Namespace(db=self.db, run_id="a" * 32, timeout_seconds=1, interval_s=0.01, handoff_file=handoff)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            ar.supervise_command(args)
        self.assertIn("ARCHON_SUPERVISE=GATE", out.getvalue())
        data = handoff.read_text(encoding="utf-8")
        self.assertIn("operator-held-not-persisted", data)
        self.assertNotIn("secret", data)

    def test_default_adaptive_bugfix_invokes_supervision_for_real_running_row(self):
        report = self.root / "report.md"
        report.write_text("thin", encoding="utf-8")
        args = Namespace(report=str(report), provider="claude", db=self.db, codex_home=self.root / "home", registry=self.root / "registry", control_dir=self.root / "control", no_watch=False, watch_timeout_seconds=1)
        row = {"id": "a" * 32, "workflow_name": "bugfix", "user_message": str(report), "status": "running", "output_root": str(self.root / "out")}
        baseline = {"commits": {"api": "a"*40, "web-app": "b"*40}, "gitnexus": {"commit": "a"*40, "index_path": "/tmp/i"}, "sha256": "c"*64}
        with mock.patch.object(ar, "capture_bugfix_baseline", return_value=baseline),              mock.patch.object(ar, "static_bugfix_route", return_value=("full", "static-missing-repro")),              mock.patch.object(ar, "run_claude_lane", return_value=row),              mock.patch.object(ar, "status_for_run", return_value="running"),              mock.patch.object(ar, "supervise_exact_run", return_value={"state": "gate", "run": "a"*32, "status": "paused", "gate": "rca-approval"}) as sup,              contextlib.redirect_stdout(io.StringIO()):
            ar.adaptive_bugfix(args)
        sup.assert_called_once()

    def test_conflict_supervision_launches_one_same_provider_successor(self):
        report = self.root / "report.md"
        report.write_text("thin", encoding="utf-8")
        args = Namespace(report=str(report), provider="codex", db=self.db,
                         codex_home=self.root / "home", registry=self.root / "registry",
                         control_dir=self.root / "control", no_watch=False,
                         watch_timeout_seconds=1)
        first = {"id": "b" * 32, "workflow_name": "bugfix-codex", "user_message": str(report), "status": "running", "output_root": str(self.root / "out")}
        second = {"id": "c" * 32, "workflow_name": "bugfix-codex", "user_message": str(report), "status": "running", "output_root": str(self.root / "out"), "_control_line": "CODEX_LITE_RUN=STARTED run=cccccccc token=successor-token"}
        baseline = {"commits": {"api": "a"*40, "web-app": "b"*40}, "gitnexus": {"commit": "a"*40, "index_path": "/tmp/i"}, "sha256": "c"*64}
        launched = []
        def launch(_args, lane, _report):
            launched.append(lane)
            return first if len(launched) == 1 else second
        calls = 0
        def supervise(_db, run_id, _timeout, _interval):
            nonlocal calls
            calls += 1
            if calls == 1:
                ad = ar.artifact_dir(first)
                receipt = json.loads((ad / "bugfix-chain.json").read_text())
                source = [{"id": "S1"}]
                root_hash = hashlib.sha256(json.dumps(source, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                ledger = {"source_symptoms": source, "ledger_root_hash": root_hash, "ledger_revision_hash": "d" * 64}
                (ad / "symptoms.json").write_text(json.dumps(ledger))
                receipt.update({"root_source_ids": ["S1"], "ledger_root_hash": root_hash, "ledger_revision_hash": "d" * 64})
                (ad / "bugfix-chain.json").write_text(json.dumps(receipt))
                (ad / "proof-recovery.json").write_text(json.dumps({"state": "RECOVERY_SUCCESSOR_REQUIRED"}))
                return {"state": "terminal", "run": run_id, "status": "failed", "lane": "bugfix-codex"}
            return {"state": "gate", "run": run_id, "status": "paused", "lane": "bugfix-codex", "gate": "rca-approval"}
        with mock.patch.object(ar, "capture_bugfix_baseline", return_value=baseline), \
             mock.patch.object(ar, "static_bugfix_route", return_value=("full", "static-missing-repro")), \
             mock.patch.object(ar, "invoke_codex_lane", side_effect=launch), \
             mock.patch.object(ar, "status_for_run", return_value="running"), \
             mock.patch.object(ar, "supervise_exact_run", side_effect=supervise), \
             mock.patch.object(ar, "seal_failed_fix_evidence"), \
             mock.patch.object(ar, "reset_failed_worktree"), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            ar.adaptive_bugfix(args)
        self.assertEqual(launched, ["bugfix-codex", "bugfix-codex"])
        chain = json.loads((ar.artifact_dir(second) / "bugfix-chain.json").read_text())
        self.assertEqual(chain["provider"], "codex")
        self.assertEqual(chain["parent_run_id"], first["id"])
        self.assertEqual(chain["root_source_ids"], ["S1"])
        self.assertEqual(chain["counters"]["successors"], 1)
        self.assertIn("token=successor-token", out.getvalue())

    def test_three_failed_fix_runs_create_two_successors_then_trip_architecture_breaker(self):
        report = self.root / "report.md"
        report.write_text("thin", encoding="utf-8")
        args = Namespace(report=str(report), provider="codex", db=self.db,
                         codex_home=self.root / "home", registry=self.root / "registry",
                         control_dir=self.root / "control", no_watch=False,
                         watch_timeout_seconds=1, chain_id=None, continuation_seed=None)
        rows = [
            {"id": char * 32, "workflow_name": "bugfix-codex", "user_message": str(report),
             "status": "running", "output_root": str(self.root / "out")}
            for char in ("d", "e", "f")
        ]
        baseline = {"commits": {"api": "a"*40, "web-app": "b"*40},
                    "gitnexus": {"commit": "a"*40, "index_path": "/tmp/i"}, "sha256": "c"*64}
        launched = []
        def launch(_args, lane, _report):
            launched.append(lane)
            return rows[len(launched) - 1]
        def supervise(_db, run_id, _timeout, _interval):
            row = next(item for item in rows if item["id"] == run_id)
            ad = ar.artifact_dir(row)
            receipt = json.loads((ad / "bugfix-chain.json").read_text())
            source = [{"id": "S1"}]
            root_hash = hashlib.sha256(json.dumps(source, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            (ad / "symptoms.json").write_text(json.dumps({"source_symptoms": source,
                "ledger_root_hash": root_hash, "ledger_revision_hash": "d" * 64}))
            receipt.update({"root_source_ids": ["S1"], "ledger_root_hash": root_hash,
                            "ledger_revision_hash": "d" * 64})
            (ad / "bugfix-chain.json").write_text(json.dumps(receipt))
            (ad / "proof-recovery.json").write_text(json.dumps({"state": "CONVERGED"}))
            attempt = ad / "attempt-1"
            attempt.mkdir()
            (attempt / "green.json").write_text(json.dumps({"green": False, "reason": "still red"}))
            (attempt / "converge.txt").write_text("FIX_ATTEMPT_FAILED attempt=1\n")
            return {"state": "terminal", "run": run_id, "status": "failed", "lane": "bugfix-codex"}
        with mock.patch.object(ar, "capture_bugfix_baseline", return_value=baseline), \
             mock.patch.object(ar, "static_bugfix_route", return_value=("full", "static-missing-repro")), \
             mock.patch.object(ar, "invoke_codex_lane", side_effect=launch), \
             mock.patch.object(ar, "status_for_run", return_value="running"), \
             mock.patch.object(ar, "supervise_exact_run", side_effect=supervise), \
             mock.patch.object(ar, "seal_failed_fix_evidence"), \
             mock.patch.object(ar, "reset_failed_worktree"), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            ar.adaptive_bugfix(args)
        self.assertEqual(launched, ["bugfix-codex"] * 3)
        self.assertIn("ARCHITECTURE_SUSPECT", out.getvalue())
        final_receipt = json.loads((ar.artifact_dir(rows[-1]) / "bugfix-chain.json").read_text())
        state = ar.read_chain_state(args.control_dir, final_receipt["logical_chain_id"])
        self.assertEqual(state["counters"]["causal_fix_failures"], 3)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            ar.create_continuation_seed(args.control_dir, state, rows[-1]["id"], "failed-fix-investigation")


if __name__ == "__main__":
    unittest.main()
