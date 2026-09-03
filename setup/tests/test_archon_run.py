#!/usr/bin/env python3
import contextlib
import importlib.util
import io
import inspect
import json
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


class AdaptiveBugfix(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "archon.db"
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE remote_agent_workflow_runs "
                    "(id TEXT, workflow_name TEXT, user_message TEXT, status TEXT, output_root TEXT, started_at TEXT)")
        con.commit(); con.close()

    def report(self, body):
        p = self.root / f"report-{len(list(self.root.glob('report-*')))}.md"
        p.write_text(body, encoding="utf-8")
        return p

    def row(self, run_id, lane, status="running"):
        return {"id": run_id, "workflow_name": lane, "user_message": "/report.md",
                "status": status, "output_root": str(self.root / "out")}

    def args(self, report, provider):
        return Namespace(report=str(report), provider=provider, db=self.db,
                         codex_home=self.root / "home", registry=self.root / "registry",
                         control_dir=self.root / "control")

    def test_static_prefilter_accepts_exact_engineering_ready_repro(self):
        p = self.report("""# Bug\nRepository: api\n## Repro\n```bash\nbun run test -- widgets.spec.ts\n```\nObserved: expected 2, received 1\n""")
        self.assertEqual(ar.static_bugfix_route(p), ("lite", "static-lite-candidate"))

    def test_guarded_controls_do_not_require_aws_cli_or_sso(self):
        source = inspect.getsource(ar.ensure_environment)
        self.assertNotIn('"aws"', source)
        self.assertNotIn("AWS SSO", source)

    def test_gitnexus_missing_mcp_degrades_without_blocking_controls(self):
        home = self.root / "home"
        home.mkdir()
        (home / "config.toml").write_text('sandbox_mode = "workspace-write"\n', encoding="utf-8")

        result = ar.assess_gitnexus_environment(self.root, home, self.root / "missing-registry")

        self.assertEqual(result, {"status": "UNAVAILABLE", "reason": "mcp-not-configured"})

    def test_gitnexus_missing_registry_degrades_without_blocking_controls(self):
        home = self.root / "home"
        home.mkdir()
        dispatcher = (ar.SETUP / "gitnexus-mcp-dispatch.py").resolve()
        (home / "config.toml").write_text(
            '[mcp_servers.gitnexus]\ncommand = "python3"\nargs = ["' + str(dispatcher) + '"]\n'
            'sandbox_mode = "workspace-write"\n',
            encoding="utf-8",
        )

        result = ar.assess_gitnexus_environment(self.root, home, self.root / "missing-registry")

        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertTrue(result["reason"].startswith("registry-unreadable"))

    def test_gitnexus_configured_mcp_must_preserve_pinned_dispatcher(self):
        home = self.root / "home"
        home.mkdir()
        (home / "config.toml").write_text(
            '[mcp_servers.gitnexus]\ncommand = "python3"\nargs = ["/tmp/untrusted.py"]\n',
            encoding="utf-8",
        )

        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            ar.assess_gitnexus_environment(self.root, home, self.root / "registry")

    def test_gate_controls_do_not_require_free_ports_owned_by_the_paused_run(self):
        source = inspect.getsource(ar.main)
        self.assertIn('check_ports=args.action == "run"', source)

    def test_rejection_is_not_blocked_by_launch_environment_health(self):
        source = inspect.getsource(ar.main)
        self.assertIn('args.action not in {"abandon", "reject"}', source)
        reject_branch = source[source.index('elif args.action == "reject"'):]
        reject_branch = reject_branch[:reject_branch.index("\n    else:")]
        self.assertNotIn("ensure_environment", reject_branch)

    def test_supervision_reads_the_real_archon_approval_step_name(self):
        event = {
            "event_type": "approval_requested",
            "step_name": "smoke-approval",
            "data": '{"message":"waiting"}',
        }

        self.assertEqual(ar.gate_name_from_event(event), "smoke-approval")

    def test_supervision_keeps_compatibility_with_nested_gate_names(self):
        event = {"data": '{"step_name":"rca-approval"}'}

        self.assertEqual(ar.gate_name_from_event(event), "rca-approval")

    def test_supervision_prefers_the_approval_event_when_timestamps_collide(self):
        run_id = "a" * 32
        con = sqlite3.connect(self.db)
        con.execute(
            "INSERT INTO remote_agent_workflow_runs VALUES (?,?,?,?,?,?)",
            (run_id, "bugfix-codex", "/report.md", "paused", str(self.root / "out"), "2026-09-02"),
        )
        con.execute(
            "CREATE TABLE remote_agent_workflow_events "
            "(workflow_run_id TEXT, event_order INTEGER, event_type TEXT, step_name TEXT, data TEXT, created_at TEXT)"
        )
        timestamp = "2026-09-02 18:30:13"
        con.execute(
            "INSERT INTO remote_agent_workflow_events VALUES (?,?,?,?,?,?)",
            (run_id, 10, "node_completed", "smoke-matrix-render", "{}", timestamp),
        )
        con.execute(
            "INSERT INTO remote_agent_workflow_events VALUES (?,?,?,?,?,?)",
            (run_id, 11, "approval_requested", "smoke-approval", "{}", timestamp),
        )
        con.commit()
        con.close()

        result = ar.supervise_exact_run(self.db, run_id, 0)

        self.assertEqual(result["gate"], "smoke-approval")

    def test_static_prefilter_routes_thin_unknown_and_unsafe_reports_full(self):
        cases = [
            ("# Bug\nSomething is broken\n", "static-missing-single-repository"),
            ("Repository: api\n## Repro\n```bash\nrm -rf /\n```\nObserved boom\n", "static-repro-command-not-allowed"),
            ("Repository: api\n## Repro\n```bash\nbun run test -- a; rm -rf /\n```\nObserved boom\n", "static-repro-command-unsafe"),
            ("Repository: api\n## Repro\n```bash\nbun run test -- a\nbun run test -- b\n```\nObserved boom\n", "static-repro-command-count"),
            ("Repository: api\nRepository: web-app\n", "static-missing-single-repository"),
        ]
        for body, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(ar.static_bugfix_route(self.report(body)), ("full", reason))

    def test_typed_full_envelope_requires_failed_status(self):
        row = self.row("a" * 32, "bugfix-lite", "running")
        ad = ar.artifact_dir(row); ad.mkdir(parents=True)
        (ad / "envelope-pre.txt").write_text("ROUTE=FULL reason=triage\n", encoding="utf-8")
        with mock.patch.object(ar, "status_for_run", side_effect=["running", "cancelled"]), \
             mock.patch.object(ar.time, "sleep"):
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                ar.wait_for_pre_envelope(row, self.db, 1)

    def test_malformed_envelope_never_falls_back(self):
        row = self.row("b" * 32, "bugfix-lite", "failed")
        ad = ar.artifact_dir(row); ad.mkdir(parents=True)
        (ad / "envelope-pre.txt").write_text("ROUTE=FULL reason=malformed\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            ar.wait_for_pre_envelope(row, self.db, 1)

    def test_codex_lite_full_fallback_never_crosses_provider(self):
        report = self.report("Repository: api\n## Repro\n```\nbun run test -- a\n```\nObserved fail\n")
        lite = self.row("1" * 32, "bugfix-lite-codex", "failed")
        full = self.row("2" * 32, "bugfix-codex", "running")
        launched = []
        def launch(_args, lane, _report):
            launched.append(lane)
            return lite if "lite" in lane else full
        out = io.StringIO()
        with mock.patch.object(ar, "invoke_codex_lane", side_effect=launch), \
             mock.patch.object(ar, "wait_for_pre_envelope", return_value=("FULL", "ROUTE=FULL reason=triage")), \
             mock.patch.object(ar, "adopt_run_ledger", side_effect=lambda _c, state, _r: state), \
             mock.patch.object(ar, "prepare_continuation_bundle"), \
             mock.patch.object(ar, "write_routing_receipt") as receipt, \
             contextlib.redirect_stdout(out):
            ar.adaptive_bugfix(self.args(report, "codex"))
        self.assertEqual(launched, ["bugfix-lite-codex", "bugfix-codex"])
        data = receipt.call_args.args[1]
        self.assertEqual(data["discarded_lite_run_id"], lite["id"])
        self.assertEqual(data["active_run_id"], full["id"])
        self.assertIn("provider=codex lane=bugfix-codex", out.getvalue())

    def test_static_full_skips_lite_for_both_providers(self):
        report = self.report("# thin ticket\n")
        for provider, lane in (("claude", "bugfix"), ("codex", "bugfix-codex")):
            launched = []
            row = self.row(provider[0] * 32, lane)
            target = "invoke_codex_lane" if provider == "codex" else "run_claude_lane"
            def launch(*args):
                launched.append(args[1] if provider == "codex" else args[0])
                return row
            with self.subTest(provider=provider), mock.patch.object(ar, target, side_effect=launch), \
                 mock.patch.object(ar, "write_routing_receipt"), contextlib.redirect_stdout(io.StringIO()):
                ar.adaptive_bugfix(self.args(report, provider))
            self.assertEqual(launched, [lane])


class BugfixChainLineage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "archon.db"
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE remote_agent_workflow_runs "
                    "(id TEXT, workflow_name TEXT, user_message TEXT, status TEXT, output_root TEXT, started_at TEXT)")
        con.commit(); con.close()
        self.control_dir = self.root / "control"
        self.report = self.root / "report.md"
        self.report.write_text("Repository: api\n## Repro\n```bash\nbun run test -- a\n```\nObserved fail\n", encoding="utf-8")
        self.baseline = {"commits": {"api": "a" * 40, "web-app": "b" * 40},
                         "gitnexus": {"repo": "api", "index_path": str(self.root / "api-index"), "commit": "a" * 40}}
        self.baseline["sha256"] = ar.hashlib.sha256(ar._canonical_json_bytes(self.baseline)).hexdigest()

    def row(self, run_id, lane="bugfix-lite-codex", status="running"):
        return {"id": run_id, "workflow_name": lane, "user_message": str(self.report),
                "status": status, "output_root": str(self.root / "out")}

    def test_chain_receipt_preserves_provider_report_sequence_and_baseline(self):
        state = ar.start_bugfix_chain(self.control_dir, "codex", self.report, self.baseline)
        lite = self.row("1" * 32)
        state = ar.record_chain_run(self.control_dir, state, lite, "static-lite-candidate", None)
        full = self.row("2" * 32, "bugfix-codex")
        state, seed = ar.create_continuation_seed(self.control_dir, state, lite["id"], "lite-envelope-full")
        state = ar.consume_continuation_seed(self.control_dir, state["logical_chain_id"], seed["nonce"], "codex")
        state = ar.record_chain_run(self.control_dir, state, full, "lite-envelope-full", lite["id"])
        receipt = json.loads((ar.artifact_dir(full) / "bugfix-chain.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["provider"], "codex")
        self.assertEqual(receipt["root_run_id"], lite["id"])
        self.assertEqual(receipt["parent_run_id"], lite["id"])
        self.assertEqual(receipt["sequence"], 1)
        self.assertEqual(receipt["baseline"]["commits"], self.baseline["commits"])
        self.assertEqual(receipt["counters"]["successors"], 1)
        private_path = ar.chain_state_path(self.control_dir, state["logical_chain_id"])
        self.assertEqual(private_path.stat().st_mode & 0o077, 0)

    def test_continuation_seed_is_single_use_and_provider_bound(self):
        state = ar.start_bugfix_chain(self.control_dir, "claude", self.report, self.baseline)
        row = self.row("3" * 32, "bugfix", "failed")
        state = ar.record_chain_run(self.control_dir, state, row, "chain-conflict", None)
        state, seed = ar.create_continuation_seed(self.control_dir, state, row["id"], "proof-successor")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            ar.consume_continuation_seed(self.control_dir, state["logical_chain_id"], seed["nonce"], "codex")
        ar.consume_continuation_seed(self.control_dir, state["logical_chain_id"], seed["nonce"], "claude")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            ar.consume_continuation_seed(self.control_dir, state["logical_chain_id"], seed["nonce"], "claude")

    def test_third_ordinary_recovery_successor_is_rejected_chain_wide(self):
        state = ar.start_bugfix_chain(self.control_dir, "claude", self.report, self.baseline)
        current = self.row("a" * 32, "bugfix", "failed")
        state = ar.record_chain_run(self.control_dir, state, current, "initial", None)
        for index, char in enumerate(("b", "c"), start=1):
            state, seed = ar.create_continuation_seed(
                self.control_dir, state, current["id"], "proof-conflict-recovery"
            )
            state = ar.consume_continuation_seed(
                self.control_dir, state["logical_chain_id"], seed["nonce"], "claude"
            )
            nxt = self.row(char * 32, "bugfix", "failed")
            state = ar.record_chain_run(
                self.control_dir, state, nxt, "proof-conflict-recovery", current["id"]
            )
            current = nxt
        self.assertEqual(state["counters"]["recovery_successors"], 2)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            ar.create_continuation_seed(
                self.control_dir, state, current["id"], "human-rejection"
            )

    def test_chain_state_mac_detects_private_tampering(self):
        state = ar.start_bugfix_chain(self.control_dir, "codex", self.report, self.baseline)
        path = ar.chain_state_path(self.control_dir, state["logical_chain_id"])
        data = json.loads(path.read_text(encoding="utf-8"))
        data["provider"] = "claude"
        path.write_text(json.dumps(data), encoding="utf-8")
        path.chmod(0o600)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            ar.read_chain_state(self.control_dir, state["logical_chain_id"])

    def test_adaptive_bugfix_receipt_contains_logical_chain_and_can_skip_watch(self):
        row = self.row("4" * 32, "bugfix-codex")
        args = Namespace(report=str(self.report), provider="codex", db=self.db,
                         codex_home=self.root / "home", registry=self.root / "registry",
                         control_dir=self.control_dir, no_watch=True, watch_timeout_seconds=1)
        with mock.patch.object(ar, "capture_bugfix_baseline", return_value=self.baseline), \
             mock.patch.object(ar, "static_bugfix_route", return_value=("full", "static-missing-repro")), \
             mock.patch.object(ar, "invoke_codex_lane", return_value=row), \
             mock.patch.object(ar, "status_for_run", return_value="running"), \
             contextlib.redirect_stdout(io.StringIO()):
            ar.adaptive_bugfix(args)
        receipt = json.loads((ar.artifact_dir(row) / "bugfix-routing-receipt.json").read_text(encoding="utf-8"))
        chain = json.loads((ar.artifact_dir(row) / "bugfix-chain.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["logical_chain_id"], chain["logical_chain_id"])
        self.assertEqual(receipt["baseline_commits"], self.baseline["commits"])
        self.assertEqual(chain["sequence"], 0)

    def test_guarded_continuation_consumes_seed_without_new_chain_or_provider_change(self):
        state = ar.start_bugfix_chain(self.control_dir, "codex", self.report, self.baseline)
        parent = self.row("5" * 32, "bugfix-codex", "failed")
        state = ar.record_chain_run(self.control_dir, state, parent, "initial", None)
        state, seed = ar.create_continuation_seed(
            self.control_dir, state, parent["id"], "manual-proof-recovery"
        )
        successor = self.row("6" * 32, "bugfix-codex")
        args = Namespace(
            report=str(self.report), provider="codex", db=self.db,
            codex_home=self.root / "home", registry=self.root / "registry",
            control_dir=self.control_dir, no_watch=True, watch_timeout_seconds=1,
            chain_id=state["logical_chain_id"], continuation_seed=seed["nonce"],
        )
        with mock.patch.object(ar, "capture_bugfix_baseline") as capture, \
             mock.patch.object(ar, "invoke_codex_lane", return_value=successor), \
             mock.patch.object(ar, "status_for_run", return_value="running"), \
             contextlib.redirect_stdout(io.StringIO()):
            ar.adaptive_bugfix(args)
        capture.assert_not_called()
        receipt = json.loads((ar.artifact_dir(successor) / "bugfix-chain.json").read_text())
        self.assertEqual(receipt["provider"], "codex")
        self.assertEqual(receipt["parent_run_id"], parent["id"])
        self.assertEqual(receipt["logical_chain_id"], state["logical_chain_id"])

    def test_continuation_bundle_imports_exact_parent_ledger_and_contradiction(self):
        state = ar.start_bugfix_chain(self.control_dir, "codex", self.report, self.baseline)
        parent = self.row("7" * 32, "bugfix-codex", "failed")
        state = ar.record_chain_run(self.control_dir, state, parent, "initial", None)
        ad = ar.artifact_dir(parent)
        source = [{"id": "S1", "claim": "Granola visible", "source_quote": "Granola visible"}]
        root_hash = ar.hashlib.sha256(ar._canonical_json_bytes(source)).hexdigest()
        ledger = {"schema_version": 2, "revision": 1, "previous_revision_hash": None,
                  "ledger_root_hash": root_hash, "ledger_revision_hash": "e" * 64,
                  "source_symptoms": source,
                  "effective_symptoms": [{"id": "E1", "source_ids": ["S1"], "claim": "Granola visible"}]}
        (ad / "symptoms.json").write_text(json.dumps(ledger))
        (ad / "proof-recovery.json").write_text(json.dumps({"state": "RECOVERY_SUCCESSOR_REQUIRED"}))
        (ad / "chain-verify.json").write_text(json.dumps({"comparison": {"verdict": "conflict"}}))
        receipt = json.loads((ad / "bugfix-chain.json").read_text())
        receipt.update({"root_source_ids": ["S1"], "ledger_root_hash": root_hash,
                        "ledger_revision_hash": ledger["ledger_revision_hash"]})
        (ad / "bugfix-chain.json").write_text(json.dumps(receipt))
        state = ar.adopt_run_ledger(self.control_dir, state, parent)
        state, seed = ar.create_continuation_seed(self.control_dir, state, parent["id"], "proof-conflict-recovery")
        state = ar.consume_continuation_seed(self.control_dir, state["logical_chain_id"], seed["nonce"], "codex")
        ar.prepare_continuation_bundle(self.control_dir, state, parent, seed)
        child = self.row("8" * 32, "bugfix-codex")
        state = ar.record_chain_run(self.control_dir, state, child, "proof-conflict-recovery", parent["id"])
        child_ad = ar.artifact_dir(child)
        with mock.patch.dict(ar.os.environ, {
            "ARCHON_BUGFIX_CHAIN_ID": state["logical_chain_id"],
            "ARCHON_BUGFIX_CONTINUATION_SEED": seed["nonce"],
        }, clear=False), contextlib.redirect_stdout(io.StringIO()):
            ar.import_continuation_bundle(Namespace(
                control_dir=self.control_dir, artifacts=child_ad, finalize_ledger=True
            ))
        imported = json.loads((child_ad / "symptoms.json").read_text())
        self.assertEqual(imported["source_symptoms"], source)
        self.assertEqual(imported["ledger_root_hash"], root_hash)
        self.assertEqual(imported["revision"], 2)
        inherited = json.loads((child_ad / "continuation" / "chain-verify.json").read_text())
        self.assertEqual(inherited["comparison"]["verdict"], "conflict")

    def test_architecture_receipt_unblocks_only_the_protected_fourth_attempt(self):
        state = ar.start_bugfix_chain(self.control_dir, "codex", self.report, self.baseline)
        parent = self.row("9" * 32, "bugfix-codex", "failed")
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO remote_agent_workflow_runs VALUES (?,?,?,?,?,?)",
                    (parent["id"], parent["workflow_name"], parent["user_message"],
                     parent["status"], parent["output_root"], "2026-09-01"))
        con.commit(); con.close()
        state = ar.record_chain_run(self.control_dir, state, parent, "failed-fix-investigation", None)
        ad = ar.artifact_dir(parent)
        source = [{"id": "S1"}]
        root_hash = ar.hashlib.sha256(ar._canonical_json_bytes(source)).hexdigest()
        (ad / "symptoms.json").write_text(json.dumps({"source_symptoms": source,
            "ledger_root_hash": root_hash, "ledger_revision_hash": "e" * 64}))
        (ad / "proof-recovery.json").write_text('{"state":"CONVERGED"}')
        receipt = json.loads((ad / "bugfix-chain.json").read_text())
        receipt.update({"root_source_ids": ["S1"], "ledger_root_hash": root_hash,
                        "ledger_revision_hash": "e" * 64})
        (ad / "bugfix-chain.json").write_text(json.dumps(receipt))
        state = ar.adopt_run_ledger(self.control_dir, state, parent)
        state["counters"]["causal_fix_failures"] = 3
        state = ar.write_chain_state(self.control_dir, state)
        state, token = ar.issue_architecture_challenge(self.control_dir, state, parent["id"])
        args = Namespace(db=self.db, run_id=parent["id"], chain_id=state["logical_chain_id"],
                         control_dir=self.control_dir, provider="codex", reviewed_by="human@example.com",
                         reason="architecture reviewed", token=token)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            ar.approve_architecture_successor(Namespace(**{**vars(args), "token": "wrong"}))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            ar.approve_architecture_successor(args)
        current = ar.read_chain_state(self.control_dir, state["logical_chain_id"])
        self.assertTrue(current["architecture_review_receipt"]["approved"])
        self.assertIn("bugfix --provider codex", out.getvalue())
        self.assertIn("--continuation-seed", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            ar.approve_architecture_successor(args)

    def test_watchdog_command_includes_chain_id_when_available(self):
        cmd = ar.watchdog_command("cafebabe99", 4321, "fp", 90, 1, self.db, self.root / "home", self.root / "arm", "chain123")
        self.assertEqual(cmd[cmd.index("--chain-id") + 1], "chain123")


if __name__ == "__main__":
    unittest.main()
