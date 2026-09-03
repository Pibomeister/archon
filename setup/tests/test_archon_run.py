#!/usr/bin/env python3
import contextlib
import importlib.util
import io
import inspect
import json
import sqlite3
import subprocess
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
        with sqlite3.connect(self.db) as con:
            con.execute(
                "CREATE TABLE remote_agent_workflow_runs "
                "(id TEXT, workflow_name TEXT, user_message TEXT, status TEXT, output_root TEXT, started_at TEXT)"
            )

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
        with sqlite3.connect(self.db) as con:
            con.execute(
                "CREATE TABLE remote_agent_workflow_runs "
                "(id TEXT, workflow_name TEXT, user_message TEXT, status TEXT, output_root TEXT, started_at TEXT)"
            )
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


class FeatureFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.control_dir = self.root / "control"
        self.db = self.root / "archon.db"
        with sqlite3.connect(self.db) as con:
            con.execute(
                "CREATE TABLE remote_agent_workflow_runs "
                "(id TEXT, workflow_name TEXT, user_message TEXT, status TEXT, output_root TEXT, started_at TEXT)"
            )
        self.spec = self.root / "feature.md"
        self.spec.write_text("# Feature\n", encoding="utf-8")
        self.baseline = {"commits": {"api": "a" * 40, "web-app": "b" * 40}}
        self.baseline["sha256"] = ar.hashlib.sha256(ar._canonical_json_bytes(self.baseline)).hexdigest()
        for key in ("ARCHON_FEATURE_CHAIN_ID", "ARCHON_FEATURE_PROVIDER", "ARCHON_FEATURE_LANE", "ARCHON_FEATURE_HANDOFF"):
            ar.os.environ.pop(key, None)

    def test_feature_lanes_are_provider_neutral_and_full_codex_guarded(self):
        self.assertEqual(ar.FEATURE_LANES["claude"], {"api": "full-sdlc-api", "web": "full-sdlc-web"})
        self.assertEqual(ar.FEATURE_LANES["codex"], {"api": "full-sdlc-api-codex", "web": "full-sdlc-web-codex"})
        self.assertEqual(ar.CODEX_LANES["full-sdlc-api-codex"], (240, 30_000_000))
        self.assertEqual(ar.CODEX_LANES["full-sdlc-web-codex"], (240, 30_000_000))

    def test_public_feature_handoff_detects_tampering_and_spec_drift(self):
        payload = ar.signed_public_payload({
            "schema_version": 1,
            "kind": "archon-feature-api-handoff",
            "logical_chain_id": "c" * 32,
            "provider": "codex",
            "spec": str(self.spec),
            "spec_sha256": ar.hashlib.sha256(self.spec.read_bytes()).hexdigest(),
            "api_run_id": "d" * 32,
            "api_lane": "full-sdlc-api-codex",
            "api_worktree": str(self.root / "api"),
            "api_branch": "archon/feature",
            "api_head_sha": "e" * 40,
            "api_pr_url": "https://github.com/GoodwordTeam/api/pull/1",
            "baseline": self.baseline,
            "shared_plan_sha256": "f" * 64,
        })
        path = self.root / "handoff.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(ar.verify_public_handoff(path, "codex", self.spec)["api_run_id"], "d" * 32)
        payload["provider"] = "claude"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            ar.verify_public_handoff(path, "codex", self.spec)


    def signed_private_handoff(self, state, api_artifacts):
        payload = ar.signed_public_payload({
            "schema_version": 1,
            "kind": "archon-feature-api-handoff",
            "logical_chain_id": state["logical_chain_id"],
            "provider": state["provider"],
            "spec": state["spec"],
            "spec_sha256": state["spec_sha256"],
            "api_run_id": "d" * 32,
            "api_lane": ar.FEATURE_LANES[state["provider"]]["api"],
            "api_worktree": str(self.root / "api" / ".worktrees" / "feature"),
            "api_branch": "archon/feature",
            "api_head_sha": "e" * 40,
            "api_pr_url": "https://github.com/GoodwordTeam/api/pull/1",
            "api_artifacts": str(api_artifacts),
            "baseline": self.baseline,
            "shared_plan_sha256": ar.hashlib.sha256((api_artifacts / "plan.md").read_bytes()).hexdigest(),
            "files_allowlist_sha256": ar.hashlib.sha256((api_artifacts / "files-allowlist.json").read_bytes()).hexdigest(),
            "web_files_allowlist_sha256": ar.hashlib.sha256((api_artifacts / "web-files-allowlist.json").read_bytes()).hexdigest(),
            "verify_sha256": ar.hashlib.sha256((api_artifacts / "verify.json").read_bytes()).hexdigest(),
            "created_at": "2026-09-02T00:00:00Z",
        })
        payload["handoff_mac"] = ar.feature_handoff_mac(state["chain_secret"], payload)
        state.update({
            "api_run_id": payload["api_run_id"],
            "api_head_sha": payload["api_head_sha"],
            "api_pr_url": payload["api_pr_url"],
            "api_handoff_sha256": payload["handoff_sha256"],
            "api_handoff_mac": payload["handoff_mac"],
        })
        ar.write_feature_chain(self.control_dir, state)
        path = self.root / "handoff.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path, payload

    def api_artifacts(self):
        ad = self.root / "api-artifacts"
        ad.mkdir(exist_ok=True)
        (ad / "plan.md").write_text("# Shared cross-repository plan\n", encoding="utf-8")
        (ad / "files-allowlist.json").write_text('["src/foo.ts"]\n', encoding="utf-8")
        (ad / "web-files-allowlist.json").write_text('["app/routes/feature.tsx"]\n', encoding="utf-8")
        (ad / "verify.json").write_text('{"ok": true}\n', encoding="utf-8")
        return ad

    def test_feature_handoff_requires_private_mac_and_api_artifact_lineage(self):
        state = ar.start_feature_chain(self.control_dir, "codex", self.spec, self.baseline)
        handoff, payload = self.signed_private_handoff(state, self.api_artifacts())
        artifacts = self.root / "web-artifacts"

        verified = ar.verify_feature_handoff(
            self.control_dir, handoff, "codex", "full-sdlc-web-codex", artifacts
        )

        self.assertEqual(verified["api_run_id"], "d" * 32)
        integrity = json.loads((artifacts / "handoff-integrity.json").read_text())
        self.assertTrue(all(integrity["checks"].values()))

        payload["api_head_sha"] = "f" * 40
        payload = ar.signed_public_payload(payload)
        payload["handoff_mac"] = ar.feature_handoff_mac(state["chain_secret"], payload)
        handoff.write_text(json.dumps(payload), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            ar.verify_feature_handoff(self.control_dir, handoff, "codex", "full-sdlc-web-codex")

    def test_feature_handoff_rejects_provider_lane_and_chain_environment_drift(self):
        state = ar.start_feature_chain(self.control_dir, "codex", self.spec, self.baseline)
        handoff, _ = self.signed_private_handoff(state, self.api_artifacts())

        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            ar.verify_feature_handoff(self.control_dir, handoff, "claude", "full-sdlc-web")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            ar.verify_feature_handoff(self.control_dir, handoff, "codex", "full-sdlc-web")
        payload = json.loads(handoff.read_text(encoding="utf-8"))
        payload["api_lane"] = "full-sdlc-api"
        payload = ar.signed_public_payload(payload)
        payload["handoff_mac"] = ar.feature_handoff_mac(state["chain_secret"], payload)
        handoff.write_text(json.dumps(payload), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            ar.verify_feature_handoff(self.control_dir, handoff, "codex", "full-sdlc-web-codex")
        fresh, _ = self.signed_private_handoff(state, self.api_artifacts())
        with mock.patch.dict(ar.os.environ, {"ARCHON_FEATURE_CHAIN_ID": "a" * 32}, clear=False), \
             contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            ar.verify_feature_handoff(self.control_dir, fresh, "codex", "full-sdlc-web-codex")


    def test_feature_control_records_restore_web_resume_environment(self):
        state = ar.start_feature_chain(self.control_dir, "codex", self.spec, self.baseline)
        handoff, _ = self.signed_private_handoff(state, self.api_artifacts())
        row = {"id": "a" * 32, "workflow_name": "full-sdlc-web-codex", "user_message": str(handoff),
               "status": "failed", "output_root": str(self.root / "out")}
        with mock.patch.dict(ar.os.environ, {
            "ARCHON_FEATURE_CHAIN_ID": state["logical_chain_id"],
            "ARCHON_FEATURE_PROVIDER": "codex",
            "ARCHON_FEATURE_LANE": "full-sdlc-web-codex",
        }, clear=False):
            ar.write_control_records(row, self.control_dir, "token", "run", 1, 1, "fp",
                                     self.root / "workflow.log", self.root / "watchdog.log",
                                     2, 2, "wfp", self.root / "arm", True, 240, 30_000_000)
        control = ar.read_control_state(row, self.control_dir)
        for key in ("ARCHON_FEATURE_CHAIN_ID", "ARCHON_FEATURE_PROVIDER", "ARCHON_FEATURE_LANE", "ARCHON_FEATURE_HANDOFF"):
            ar.os.environ.pop(key, None)

        try:
            ar.restore_feature_control_env(row, self.control_dir, control)
            self.assertEqual(ar.os.environ["ARCHON_FEATURE_CHAIN_ID"], state["logical_chain_id"])
            self.assertEqual(ar.os.environ["ARCHON_FEATURE_PROVIDER"], "codex")
            self.assertEqual(ar.os.environ["ARCHON_FEATURE_LANE"], "full-sdlc-web-codex")
            self.assertEqual(ar.os.environ["ARCHON_FEATURE_HANDOFF"], str(handoff))
        finally:
            for key in ("ARCHON_FEATURE_CHAIN_ID", "ARCHON_FEATURE_PROVIDER", "ARCHON_FEATURE_LANE", "ARCHON_FEATURE_HANDOFF"):
                ar.os.environ.pop(key, None)


    def init_git_worktree(self, name):
        wt = self.root / name
        wt.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(wt), "init"], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "-C", str(wt), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(wt), "config", "user.name", "Test"], check=True)
        (wt / "file.txt").write_text(name + "\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", "."], check=True)
        subprocess.run(["git", "-C", str(wt), "commit", "-m", "init"], check=True, stdout=subprocess.DEVNULL)
        return wt, subprocess.check_output(["git", "-C", str(wt), "rev-parse", "HEAD"], text=True).strip()

    def completed_row(self, run_id, lane, label, pr_url):
        wt, head = self.init_git_worktree(label)
        row = {"id": run_id, "workflow_name": lane, "user_message": str(self.spec),
               "status": "completed", "output_root": str(self.root / "out")}
        ad = ar.artifact_dir(row)
        ad.mkdir(parents=True, exist_ok=True)
        (ad / "params.json").write_text(json.dumps({"worktree": str(wt), "branch": f"archon/{label}"}), encoding="utf-8")
        (ad / "worktrees.json").write_text(json.dumps({f"{label}_worktree": str(wt)}), encoding="utf-8")
        (ad / "pr-url.txt").write_text(pr_url + "\n", encoding="utf-8")
        with sqlite3.connect(self.db) as con:
            con.execute(
                "INSERT INTO remote_agent_workflow_runs VALUES (?,?,?,?,?,?)",
                (
                    row["id"],
                    row["workflow_name"],
                    row["user_message"],
                    row["status"],
                    row["output_root"],
                    "2026-09-03",
                ),
            )
        return row, head

    def update_run(self, run_id, *, status=None, user_message=None):
        with sqlite3.connect(self.db) as con:
            if status is not None:
                con.execute(
                    "UPDATE remote_agent_workflow_runs SET status = ? WHERE id = ?",
                    (status, run_id),
                )
            if user_message is not None:
                con.execute(
                    "UPDATE remote_agent_workflow_runs SET user_message = ? WHERE id = ?",
                    (user_message, run_id),
                )

    def test_feature_receipt_requires_completed_web_with_pr_and_verifies_tamper(self):
        state = ar.start_feature_chain(self.control_dir, "codex", self.spec, self.baseline)
        api, api_head = self.completed_row("1" * 32, "full-sdlc-api-codex", "api", "https://github.com/GoodwordTeam/api/pull/1")
        web, web_head = self.completed_row("2" * 32, "full-sdlc-web-codex", "web", "https://github.com/GoodwordTeam/web-app/pull/2")
        state.update({
            "api_run_id": api["id"],
            "api_head_sha": api_head,
            "api_pr_url": "https://github.com/GoodwordTeam/api/pull/1",
            "api_handoff_sha256": "a" * 64,
        })
        state = ar.write_feature_chain(self.control_dir, state)

        receipt = ar.write_chain_receipt(self.control_dir, state, api, web, self.root / "handoff.json", self.db)
        verified = ar.verify_feature_chain_receipt(self.control_dir, receipt)

        self.assertEqual(verified["api_run_id"], api["id"])
        self.assertEqual(verified["web_run_id"], web["id"])
        self.assertEqual(verified["api_pr_url"], "https://github.com/GoodwordTeam/api/pull/1")
        self.assertEqual(verified["web_pr_url"], "https://github.com/GoodwordTeam/web-app/pull/2")
        self.assertEqual(verified["api_head_sha"], api_head)
        self.assertEqual(verified["web_head_sha"], web_head)
        tampered = json.loads(receipt.read_text(encoding="utf-8"))
        tampered["web_pr_url"] = "https://evil.example/pr"
        receipt.write_text(json.dumps(tampered), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            ar.verify_feature_chain_receipt(self.control_dir, receipt)

    def test_feature_receipt_refuses_failed_or_prless_web(self):
        state = ar.start_feature_chain(self.control_dir, "codex", self.spec, self.baseline)
        api, api_head = self.completed_row("3" * 32, "full-sdlc-api-codex", "api", "https://github.com/GoodwordTeam/api/pull/3")
        web, _ = self.completed_row("4" * 32, "full-sdlc-web-codex", "web", "https://github.com/GoodwordTeam/web-app/pull/4")
        state.update({"api_run_id": api["id"], "api_head_sha": api_head, "api_pr_url": "https://github.com/GoodwordTeam/api/pull/3"})
        state = ar.write_feature_chain(self.control_dir, state)
        self.update_run(web["id"], status="failed")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            ar.write_chain_receipt(self.control_dir, state, api, web, self.root / "handoff.json", self.db)
        self.update_run(web["id"], status="completed")
        (ar.artifact_dir(web) / "pr-url.txt").unlink()
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            ar.write_chain_receipt(self.control_dir, state, api, web, self.root / "handoff.json", self.db)

    def feature_args(self):
        return Namespace(spec=str(self.spec), provider="codex", scope="fullstack", db=self.root / "archon.db",
                         codex_home=self.root / "home", registry=self.root / "registry", control_dir=self.control_dir,
                         no_watch=False, watch_timeout_seconds=1)

    def test_adaptive_feature_finalizes_receipt_only_after_terminal_web_success(self):
        chain = "b" * 32
        state = {"logical_chain_id": chain, "provider": "codex", "spec": str(self.spec),
                 "spec_sha256": ar.sha256_file(self.spec), "baseline": self.baseline, "chain_secret": "secret"}
        api = {"id": "5" * 32, "workflow_name": "full-sdlc-api-codex", "output_root": str(self.root / "out")}
        web = {"id": "6" * 32, "workflow_name": "full-sdlc-web-codex", "output_root": str(self.root / "out")}
        handoff = self.root / "handoff.json"
        handoff.write_text("{}", encoding="utf-8")
        with mock.patch.object(ar, "capture_feature_baseline", return_value=self.baseline), \
             mock.patch.object(ar, "start_feature_chain", return_value=state.copy()), \
             mock.patch.object(ar, "write_feature_chain", side_effect=lambda _c, st: st), \
             mock.patch.object(ar, "run_feature_lane", side_effect=[api, web]), \
             mock.patch.object(ar, "supervise_exact_run", side_effect=[{"state": "terminal", "status": "completed", "run": api["id"]}, {"state": "terminal", "status": "completed", "run": web["id"]}]), \
             mock.patch.object(ar, "api_handoff_from_run", return_value=handoff), \
             mock.patch.object(ar, "verify_public_handoff", return_value={"logical_chain_id": chain}), \
             mock.patch.object(ar, "run_row_by_id", return_value=dict(web, status="completed")), \
             mock.patch.object(ar, "write_chain_receipt", return_value=self.root / "receipt.json") as write_receipt, \
             mock.patch.object(ar, "verify_feature_chain_receipt"), \
             contextlib.redirect_stdout(io.StringIO()):
            ar.adaptive_feature(self.feature_args())
        write_receipt.assert_called_once()

    def test_adaptive_feature_does_not_finalize_receipt_for_paused_or_failed_web(self):
        for web_result in ({"state": "gate", "status": "paused", "run": "8" * 32}, {"state": "terminal", "status": "failed", "run": "8" * 32}):
            chain = "c" * 32
            state = {"logical_chain_id": chain, "provider": "codex", "spec": str(self.spec),
                     "spec_sha256": ar.sha256_file(self.spec), "baseline": self.baseline, "chain_secret": "secret"}
            api = {"id": "7" * 32, "workflow_name": "full-sdlc-api-codex", "output_root": str(self.root / "out")}
            web = {"id": "8" * 32, "workflow_name": "full-sdlc-web-codex", "output_root": str(self.root / "out")}
            handoff = self.root / f"handoff-{web_result['status']}.json"
            handoff.write_text("{}", encoding="utf-8")
            with self.subTest(web_status=web_result["status"]), \
                 mock.patch.object(ar, "capture_feature_baseline", return_value=self.baseline), \
                 mock.patch.object(ar, "start_feature_chain", return_value=state.copy()), \
                 mock.patch.object(ar, "write_feature_chain", side_effect=lambda _c, st: st), \
                 mock.patch.object(ar, "run_feature_lane", side_effect=[api, web]), \
                 mock.patch.object(ar, "supervise_exact_run", side_effect=[{"state": "terminal", "status": "completed", "run": api["id"]}, web_result]), \
                 mock.patch.object(ar, "api_handoff_from_run", return_value=handoff), \
                 mock.patch.object(ar, "verify_public_handoff", return_value={"logical_chain_id": chain}), \
                 mock.patch.object(ar, "write_chain_receipt") as write_receipt, \
                 contextlib.redirect_stdout(io.StringIO()):
                ar.adaptive_feature(self.feature_args())
            write_receipt.assert_not_called()


    def test_supervise_command_refreshes_running_web_row_before_receipt_finalization(self):
        state = ar.start_feature_chain(self.control_dir, "codex", self.spec, self.baseline)
        api, api_head = self.completed_row("9" * 32, "full-sdlc-api-codex", "api", "https://github.com/GoodwordTeam/api/pull/9")
        web, _ = self.completed_row("a" * 32, "full-sdlc-web-codex", "web", "https://github.com/GoodwordTeam/web-app/pull/10")
        state.update({
            "api_run_id": api["id"],
            "api_head_sha": api_head,
            "api_pr_url": "https://github.com/GoodwordTeam/api/pull/9",
            "api_handoff_sha256": "b" * 64,
            "web_run_id": web["id"],
        })
        state = ar.write_feature_chain(self.control_dir, state)
        handoff = self.root / "handoff-supervise.json"
        handoff.write_text("{}", encoding="utf-8")
        self.update_run(web["id"], status="running", user_message=str(handoff))
        web_running = dict(web, status="running", user_message=str(handoff))
        with mock.patch.dict(ar.os.environ, {
            "ARCHON_FEATURE_CHAIN_ID": state["logical_chain_id"],
            "ARCHON_FEATURE_PROVIDER": "codex",
            "ARCHON_FEATURE_LANE": "full-sdlc-web-codex",
        }, clear=False):
            ar.write_control_records(web_running, self.control_dir, "token", "run", 1, 1, "fp",
                                     self.root / "workflow.log", self.root / "watchdog.log",
                                     2, 2, "wfp", self.root / "arm", True, 240, 30_000_000)
        for key in ("ARCHON_DB", "ARCHON_FEATURE_CHAIN_ID", "ARCHON_FEATURE_PROVIDER", "ARCHON_FEATURE_LANE", "ARCHON_FEATURE_HANDOFF"):
            ar.os.environ.pop(key, None)

        def complete(_db, run_id, _timeout, _interval):
            self.update_run(run_id, status="completed")
            return {"state": "terminal", "status": "completed", "run": run_id, "lane": "full-sdlc-web-codex"}

        args = Namespace(db=self.db, run_id=web["id"], timeout_seconds=1, interval_s=0.01,
                         handoff_file=None, control_dir=self.control_dir)
        with mock.patch.object(ar, "supervise_exact_run", side_effect=complete), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            ar.supervise_command(args)

        receipt = ar.artifact_dir(web) / "feature-chain-receipt.json"
        self.assertTrue(receipt.is_file())
        self.assertIn("feature_receipt=", out.getvalue())
        verified = ar.verify_feature_chain_receipt(self.control_dir, receipt)
        self.assertEqual(verified["web_run_id"], web["id"])

    def test_feature_chain_private_state_is_provider_bound_and_mac_checked(self):
        state = ar.start_feature_chain(self.control_dir, "codex", self.spec, self.baseline)
        current = ar.read_feature_chain(self.control_dir, state["logical_chain_id"])
        self.assertEqual(current["provider"], "codex")
        path = ar.feature_state_path(self.control_dir, state["logical_chain_id"])
        data = json.loads(path.read_text(encoding="utf-8"))
        data["provider"] = "claude"
        path.write_text(json.dumps(data), encoding="utf-8")
        path.chmod(0o600)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            ar.read_feature_chain(self.control_dir, state["logical_chain_id"])


if __name__ == "__main__":
    unittest.main()
