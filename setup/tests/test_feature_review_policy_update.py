#!/usr/bin/env python3
import importlib.util
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml


SETUP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SETUP))

ar_spec = importlib.util.spec_from_file_location("archon_run", SETUP / "archon-run.py")
assert ar_spec and ar_spec.loader
ar = importlib.util.module_from_spec(ar_spec)
ar_spec.loader.exec_module(ar)

fc_spec = importlib.util.spec_from_file_location("feature_chain", SETUP / "feature_chain.py")
assert fc_spec and fc_spec.loader
fc = importlib.util.module_from_spec(fc_spec)
fc_spec.loader.exec_module(fc)

runtime_spec = importlib.util.spec_from_file_location("review_delta_runtime", SETUP / "review_delta_runtime.py")
assert runtime_spec and runtime_spec.loader
runtime = importlib.util.module_from_spec(runtime_spec)
runtime_spec.loader.exec_module(runtime)

CHAIN = "e" * 32
RUN = "f" * 32


class FeatureReviewPolicyUpdate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.control = self.root / "control"
        self.control.mkdir(mode=0o700)
        self.db = self.root / "archon.db"
        self.spec = self.root / "feature.md"
        self.spec.write_text("# Feature\n", encoding="utf-8")
        self.output_root = self.root / "out"
        self.artifacts = self.output_root / "artifacts" / "runs" / RUN
        self.artifacts.mkdir(parents=True)
        self.workflow_source = self.make_workflow_source()
        self.row = {
            "id": RUN,
            "workflow_name": "full-sdlc-api-codex",
            "user_message": str(self.spec),
            "status": "failed",
            "output_root": str(self.output_root),
        }
        self.args = Namespace(
            control_dir=self.control,
            db=self.db,
            token="operator-token",
            policy="risk-delta-v1",
            expected_captured_source_digest=self.workflow_source["digest"],
            qualification_packet=None,
            reason="Authorized ENG-3866 risk-delta pilot",
            action="feature-review-policy-update",
        )
        self.host = SimpleNamespace(
            require_control_token=ar.require_control_token,
            read_control_state=ar.read_control_state,
            secure_write_json=ar.secure_write_json,
            control_state_path=ar.control_state_path,
            run_row_by_id=ar.run_row_by_id,
        )
        with sqlite3.connect(self.db) as con:
            con.execute(
                "CREATE TABLE remote_agent_workflow_runs "
                "(id TEXT, workflow_name TEXT, user_message TEXT, status TEXT, output_root TEXT, started_at TEXT, metadata TEXT)"
            )
            con.execute(
                "INSERT INTO remote_agent_workflow_runs VALUES (?,?,?,?,?,?,?)",
                (
                    RUN,
                    self.row["workflow_name"],
                    self.row["user_message"],
                    "failed",
                    str(self.output_root),
                    "2026-09-14 00:00:00",
                    json.dumps({"workflow_source": self.workflow_source}, sort_keys=True),
                ),
            )
        groups = mock.patch.object(fc.os, "killpg", side_effect=ProcessLookupError)
        groups.start()
        self.addCleanup(groups.stop)
        self.write_state()
        self.write_control()

    def make_workflow_source(self):
        root = self.artifacts / "workflow-source"
        (root / "project/.archon/workflows").mkdir(parents=True)
        (root / "project/.archon/workflows/full-sdlc-api-codex.yaml").write_text(
            yaml.safe_dump({
                "name": "full-sdlc-api-codex",
                "provider": "codex",
                "nodes": [
                    {"id": "preflight", "bash": "true"},
                    {"id": "review-loop", "loop_group": {"nodes": [
                        {"id": "round-pre", "bash": "true"},
                        {"id": "review", "prompt": "full review"},
                        {"id": "review-gate", "bash": "true"},
                        {"id": "commit-fixes", "bash": "true"},
                        {"id": "fixer", "prompt": "fix"},
                        {"id": "commit-fixer", "bash": "true"},
                        {"id": "converge", "bash": "true"},
                    ]}},
                    {"id": "ship", "bash": "true"},
                ],
            }, sort_keys=False),
            encoding="utf-8",
        )
        (root / "bundled/commands").mkdir(parents=True)
        (root / "bundled/commands/review.md").write_text("review\n", encoding="utf-8")
        files = [path for path in sorted(root.rglob("*")) if path.is_file()]
        digest = hashlib.sha256()
        for path in files:
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii"))
            digest.update(b"\n")
        source = {
            "version": 1,
            "root": str(root),
            "origin": str(self.root),
            "captured_at": "2026-09-14T00:00:00.000Z",
            "digest": digest.hexdigest(),
            "file_count": len(files),
            "byte_count": sum(path.stat().st_size for path in files),
            "workflow_name": "full-sdlc-api-codex",
        }
        manifest = {**source, "engine_version": "0.10.1", "scopes": ["project", "bundled"], "source_config": {"load_default_workflows": True, "load_default_commands": True}}
        (root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return source

    def write_state(self):
        state = {
            "schema_version": 2,
            "kind": "archon-feature-chain",
            "logical_chain_id": CHAIN,
            "chain_secret": "s" * 48,
            "provider": "codex",
            "scope": "repositories",
            "repositories": ["api", "goodword-mcp"],
            "spec": str(self.spec),
            "spec_sha256": fc.file_digest(self.spec),
            "approval": {"digest": "frozen", "source_artifacts": {"root": str(self.artifacts), "files": {}}},
            "approved_plan": {"plan": "frozen"},
            "stages": {"api": {"status": "pending"}, "goodword-mcp": {"status": "pending"}},
            "baselines": {"commits": {"api": "a" * 40, "goodword-mcp": "b" * 40}},
            "worktrees": {
                "api": {"worktree": "/tmp/api", "baseline": "a" * 40},
                "goodword-mcp": {"worktree": "/tmp/mcp", "baseline": "b" * 40},
            },
            "child_runs": {},
            "current_run": {
                "phase": "planning",
                "run_id": RUN,
                "artifacts_dir": str(self.artifacts),
                "write_roots": [str(self.artifacts)],
            },
            "pending_control": None,
            "dispatch_reservation": None,
            "budget": {"wall_minutes": 240, "max_total_tokens": 30_000_000, "ledger": "feature-budget.py"},
            "model": {"name": "gpt-5.6-sol", "reasoning_effort": "medium"},
            "consumed_resources": {"tokens": 350_000_000},
            "created_at": "2026-09-14T00:00:00Z",
            "updated_at": "2026-09-14T00:00:00Z",
        }
        fc.write_state(self.control, state)

    def write_control(self, token="operator-token", provider="codex", scope="repositories"):
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
                "provider": provider,
                "scope": scope,
                "phase": "planning",
                "repo": None,
                "run_id": RUN,
                "write_roots": [str(self.artifacts)],
            },
        }
        control["authority_mac"] = ar.authority_mac(token, control)
        ar.secure_write_json(ar.control_state_path(self.row, self.control), control)

    def state(self):
        return fc.read_state(self.control, CHAIN)

    def qualification_packet(self):
        state = self.state()
        policy = state["review_policy"]
        blockers = sorted({
            "malformed-claim-disclosure", "recipient-resurrection",
            "reconciliation-audit-id-reuse", "missing-iam-permission",
            "incorrect-index-types", "http-contract-mismatch",
        })
        snapshot = self.retain("repair.patch", "fixture patch")
        raw = self.retain("review.txt", "fixture review")
        packet = {"schema": "archon.review-qualification.v1", "matched_repairs": []}
        for index, blocker in enumerate(blockers):
            pair = {
                "id": blocker,
                "snapshot": snapshot,
                "required_responsibilities": ["correctness"],
                "known_blockers": [blocker],
            }
            for side, amounts in (("historical", (100, 80, 10, 10)), ("risk_delta", (40, 20, 10, 10))):
                usage = {
                    "accounting_unit": "provider-total-tokens-cached-input-counted-once",
                    "gross_tokens": amounts[0],
                    "cached_input_tokens": amounts[1],
                    "uncached_input_tokens": amounts[2],
                    "output_tokens": amounts[3],
                    "active_ms": 1,
                    "repeated_reads": 0,
                    "new_validated_defects": 0,
                    "reopened_findings": 0,
                    "sample_id": f"{index}-{side}",
                }
                usage["controller_mac"] = fc.hmac_sha256(state["chain_secret"], usage)
                pair[side] = {
                    "model": "gpt-5.6-sol",
                    "reasoning_effort": "medium",
                    "status": "completed",
                    "independent": True,
                    "raw_review": raw,
                    "snapshot_sha256": snapshot["sha256"],
                    "responsibilities": ["correctness"],
                    "usage": self.retain(f"{index}-{side}.json", json.dumps(usage)),
                    **({"detected_blockers": [blocker]} if side == "risk_delta" else {}),
                }
            packet["matched_repairs"].append(pair)
        negative = {"status": "passed", "checks": ["malformed-output-blocks", "wrong-model-blocks"]}
        negative["controller_mac"] = fc.hmac_sha256(state["chain_secret"], negative)
        negative_ref = self.retain("negative-tests.json", json.dumps(negative, sort_keys=True))
        packet["controller_evidence"] = {
            "predecessor_captured_source_digest": policy["predecessor_captured_source_digest"],
            "effective_workflow_source_digest": policy["effective_workflow_source_digest"],
            "helper_workflow_digests_sha256": policy["helper_workflow_digests"]["sha256"],
            "offline_negative_tests": negative_ref,
        }
        path = self.root / "qualification.json"
        path.write_text(json.dumps(packet, sort_keys=True), encoding="utf-8")
        return path

    def policy_amendment_id(self, digests=None, previous_policy=None):
        payload = {
            "kind": "feature-review-policy-update",
            "logical_chain_id": CHAIN,
            "run_id": RUN,
            "policy": self.args.policy,
            "predecessor_captured_source_digest": self.args.expected_captured_source_digest,
            "captured_workflow_source": fc.read_expected_predecessor_source_metadata(
                self.db,
                RUN,
                self.args.expected_captured_source_digest,
                self.row["workflow_name"],
            ),
            "helper_workflow_digests": digests or fc.current_review_policy_digests(self.row),
            "reason": self.args.reason,
        }
        if previous_policy is not None:
            payload["previous_review_policy"] = previous_policy
        return fc.digest(payload)

    def changed_policy_digests(self):
        digests = json.loads(json.dumps(fc.current_review_policy_digests(self.row)))
        digests["helpers"]["review_delta_runtime.py"] = "9" * 64
        digests["sha256"] = fc.digest({"helpers": digests["helpers"], "workflows": digests["workflows"]})
        return digests

    def retain(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return {"path": name, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}

    def run_metadata(self):
        with sqlite3.connect(self.db) as con:
            row = con.execute("SELECT metadata FROM remote_agent_workflow_runs WHERE id=?", (RUN,)).fetchone()
        return json.loads(row[0])

    def test_guarded_update_records_unqualified_policy_and_preserves_chain_state(self):
        before = self.state()

        result = fc.review_policy_update_command(self.host, self.args, self.row)
        state = self.state()

        self.assertEqual(CHAIN, result["chain"])
        self.assertEqual("risk-delta-v1", state["review_policy"]["policy"])
        self.assertEqual("unqualified", state["review_policy"]["qualification_status"])
        self.assertEqual(self.workflow_source["digest"], state["review_policy"]["predecessor_captured_source_digest"])
        self.assertEqual(fc.read_workflow_source_metadata(self.db, RUN), state["review_policy"]["captured_workflow_source"])
        self.assertIn("effective_workflow_source", state["review_policy"])
        effective = state["review_policy"]["effective_workflow_source"]
        self.assertNotEqual(self.workflow_source["digest"], effective["digest"])
        self.assertEqual(self.workflow_source, self.run_metadata()["workflow_source"])
        effective_doc = yaml.safe_load((Path(effective["root"]) / "project/.archon/workflows/full-sdlc-api-codex.yaml").read_text())
        loop_nodes = next(node for node in effective_doc["nodes"] if node["id"] == "review-loop")["loop_group"]["nodes"]
        self.assertEqual("delta-verify-before", loop_nodes[0]["id"])
        self.assertEqual("delta-review-1", loop_nodes[2]["id"])
        self.assertEqual(fc.current_review_policy_digests(self.row), state["review_policy"]["helper_workflow_digests"])
        self.assertEqual(before["approval"], state["approval"])
        self.assertEqual(before["approved_plan"], state["approved_plan"])
        self.assertEqual(before["budget"], state["budget"])
        self.assertEqual(before["model"], state["model"])
        self.assertEqual(before["consumed_resources"], state["consumed_resources"])
        self.assertEqual(before["current_run"], state["current_run"])
        self.assertEqual(1, len(state["review_policy_amendments"]))

        repeat = fc.review_policy_update_command(self.host, self.args, self.row)
        self.assertTrue(repeat["already_applied"])
        self.assertEqual(1, len(self.state()["review_policy_amendments"]))

    def test_effective_source_materialization_recovers_manifestless_partial_root(self):
        state = self.state()
        amendment_id = self.policy_amendment_id()
        effective_root = fc.effective_source_path(state, amendment_id)
        shutil.copytree(
            Path(self.workflow_source["root"]),
            effective_root,
            ignore=lambda _directory, names: {"manifest.json"} & set(names),
        )
        workflow = effective_root / "project/.archon/workflows/full-sdlc-api-codex.yaml"
        transformed = fc.review_delta_workflow.transform(yaml.safe_load(workflow.read_text()), SETUP)
        workflow.write_text(yaml.safe_dump(transformed, sort_keys=False), encoding="utf-8")
        self.assertFalse((effective_root / "manifest.json").exists())

        result = fc.review_policy_update_command(self.host, self.args, self.row)
        state = self.state()
        effective = state["review_policy"]["effective_workflow_source"]

        self.assertFalse(result["already_applied"])
        self.assertEqual(str(effective_root), effective["root"])
        self.assertTrue((effective_root / "manifest.json").is_file())
        self.assertEqual(effective, fc.verify_captured_workflow_source(effective, self.row["workflow_name"]))
        effective_doc = yaml.safe_load(workflow.read_text())
        loop_nodes = next(node for node in effective_doc["nodes"] if node["id"] == "review-loop")["loop_group"]["nodes"]
        self.assertEqual(1, sum(1 for node in loop_nodes if node["id"] == "delta-review-1"))

    def test_review_policy_helper_digests_pin_delta_runtime(self):
        digests = fc.current_review_policy_digests(self.row)

        self.assertIn("review_delta_runtime.py", digests["helpers"])

    def test_qualification_packet_activates_effective_source_in_run_metadata(self):
        fc.review_policy_update_command(self.host, self.args, self.row)
        self.args.qualification_packet = self.qualification_packet()

        result = fc.review_policy_update_command(self.host, self.args, self.row)
        state = self.state()
        metadata = self.run_metadata()

        self.assertFalse(result["already_applied"])
        self.assertEqual("qualified", state["review_policy"]["qualification_status"])
        self.assertEqual(state["review_policy"]["effective_workflow_source"], metadata["workflow_source"])
        self.assertEqual(state["review_policy"]["captured_workflow_source"], metadata["workflow_source_predecessor"])
        self.assertEqual("risk-delta-v1", metadata["workflow_source_policy_amendment"]["policy"])
        self.assertFalse((self.artifacts / "review-authority.json").exists())
        review_state = json.loads((self.artifacts / "review-state.json").read_text())
        self.assertEqual("risk-delta-review-state-seed", review_state["kind"])
        self.assertEqual("qualified", review_state["policy"]["qualification_status"])
        previous = dict(os.environ)
        os.environ["ARCHON_CONTROL_DIR"] = str(self.control)
        os.environ["ARCHON_FEATURE_CHAIN_ID"] = CHAIN
        try:
            runtime.verify_authority(self.artifacts, review_state)
        finally:
            os.environ.clear()
            os.environ.update(previous)
        self.assertEqual([], review_state["author_session_ids"])
        self.assertEqual({}, review_state["reviewer_provenance"])

        fc.require_review_policy_integrity(state, self.row, self.db)

    def test_activation_retry_recovers_after_db_switch_before_state_write(self):
        fc.review_policy_update_command(self.host, self.args, self.row)
        self.args.qualification_packet = self.qualification_packet()
        original = fc.write_state
        calls = {"count": 0}

        def flaky(control_dir, state):
            policy = state.get("review_policy") if isinstance(state, dict) else None
            if (isinstance(policy, dict)
                    and policy.get("qualification_status") == "qualified"
                    and state.get("review_policy_activation") is None
                    and calls["count"] == 0):
                calls["count"] += 1
                raise RuntimeError("interrupted after workflow source activation")
            return original(control_dir, state)

        with mock.patch.object(fc, "write_state", side_effect=flaky):
            with self.assertRaisesRegex(RuntimeError, "workflow source activation"):
                fc.review_policy_update_command(self.host, self.args, self.row)

        metadata = self.run_metadata()
        pending = self.state()
        self.assertEqual("in_progress", pending["review_policy_activation"]["status"])
        self.assertEqual(pending["review_policy"]["effective_workflow_source"], metadata["workflow_source"])
        self.assertEqual(pending["review_policy"]["captured_workflow_source"], metadata["workflow_source_predecessor"])

        result = fc.review_policy_update_command(self.host, self.args, self.row)
        state = self.state()
        self.assertFalse(result["already_applied"])
        self.assertIsNone(state["review_policy_activation"])
        self.assertEqual("qualified", state["review_policy"]["qualification_status"])

    def test_unqualified_policy_can_be_re_registered_after_source_update(self):
        fc.review_policy_update_command(self.host, self.args, self.row)
        first = self.state()["review_policy"]
        changed_digests = self.changed_policy_digests()

        with mock.patch.object(fc, "current_review_policy_digests", return_value=changed_digests):
            result = fc.review_policy_update_command(self.host, self.args, self.row)

        state = self.state()
        policy = state["review_policy"]
        self.assertFalse(result["already_applied"])
        self.assertEqual("unqualified", policy["qualification_status"])
        self.assertEqual(changed_digests, policy["helper_workflow_digests"])
        self.assertEqual(first["amendment_id"], policy["previous_review_policy"]["amendment_id"])
        self.assertEqual(first["effective_workflow_source_digest"], policy["previous_review_policy"]["effective_workflow_source_digest"])
        self.assertEqual(2, len(state["review_policy_amendments"]))
        self.assertEqual(first["amendment_id"], state["review_policy_amendments"][1]["previous_review_policy"]["amendment_id"])

        with mock.patch.object(fc, "current_review_policy_digests", return_value=changed_digests):
            repeat = fc.review_policy_update_command(self.host, self.args, self.row)
        self.assertTrue(repeat["already_applied"])
        self.assertEqual(2, len(self.state()["review_policy_amendments"]))

    def test_qualified_policy_cannot_be_replaced_by_source_update(self):
        fc.review_policy_update_command(self.host, self.args, self.row)
        self.args.qualification_packet = self.qualification_packet()
        fc.review_policy_update_command(self.host, self.args, self.row)
        changed_digests = self.changed_policy_digests()
        self.args.qualification_packet = None

        with mock.patch.object(fc, "current_review_policy_digests", return_value=changed_digests):
            with self.assertRaisesRegex(fc.FeatureChainError, "qualified feature review policy cannot be replaced"):
                fc.review_policy_update_command(self.host, self.args, self.row)

    def test_controller_seed_can_prepare_without_future_reviewer_sessions(self):
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test User"], check=True)
        (repo / "README.md").write_text("seed\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "seed"], check=True, capture_output=True)
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()

        fc.review_policy_update_command(self.host, self.args, self.row)
        self.args.qualification_packet = self.qualification_packet()
        fc.review_policy_update_command(self.host, self.args, self.row)
        state = self.state()
        stage_artifacts = self.root / "stage-artifacts"
        stage_artifacts.mkdir()
        state["current_run"] = {**state["current_run"], "phase": "implement", "repo": "api", "run_id": RUN, "artifacts_dir": str(stage_artifacts)}
        state["worktrees"]["api"] = {"worktree": str(repo), "baseline": head}
        state["baselines"]["commits"]["api"] = head
        state["stages"]["api"]["plan"] = {
            "depends_on": [],
            "files_allowlist": ["README.md"],
            "test_patterns": ["README.md"],
            "verification": [{"command": "true"}],
        }
        fc.write_review_state_seed(stage_artifacts, state, state["review_policy"], self.row, [])

        review_state_path = stage_artifacts / "review-state.json"
        review_state = json.loads(review_state_path.read_text())
        self.assertEqual(head, review_state["baseline_base"])
        self.assertEqual({}, review_state["reviewer_provenance"])
        self.assertEqual([], review_state["author_session_ids"])

        fc.write_state(self.control, state)
        previous = dict(os.environ)
        os.environ["ARCHON_CONTROL_DIR"] = str(self.control)
        os.environ["ARCHON_FEATURE_CHAIN_ID"] = CHAIN
        try:
            result = runtime.prepare_review(stage_artifacts, repo, review_state_path, None)
        finally:
            os.environ.clear()
            os.environ.update(previous)
        self.assertTrue(all(result[f"review_{index}"] == "no" for index in range(1, 10)))

        review_state["coverage_records"] = [{"status": "complete", "kept": True}]
        (stage_artifacts / "review-state.json").write_text(json.dumps(review_state), encoding="utf-8")
        fc.write_review_state_seed(stage_artifacts, state, state["review_policy"], self.row, [])
        retained = json.loads((stage_artifacts / "review-state.json").read_text())
        self.assertEqual([{"status": "complete", "kept": True}], retained["coverage_records"])

    def test_reseed_for_new_policy_preserves_verified_mutable_review_ledger(self):
        fc.review_policy_update_command(self.host, self.args, self.row)
        state = self.state()
        stage_artifacts = self.root / "ledger-stage-artifacts"
        stage_artifacts.mkdir()
        state["current_run"] = {**state["current_run"], "phase": "implement", "repo": "api", "run_id": RUN, "artifacts_dir": str(stage_artifacts)}
        fc.write_review_state_seed(stage_artifacts, state, state["review_policy"], self.row, [])
        review_state_path = stage_artifacts / "review-state.json"
        old_seed = json.loads(review_state_path.read_text())
        old_seed["coverage_records"] = [{"id": "coverage-1"}]
        old_seed["findings"] = [{"id": "finding-1", "status": "open"}]
        old_seed["segment_requirements"] = [{"id": "segment-1"}]
        old_seed["receipts"] = [{"command": "unit"}]
        old_seed["controller_mac"] = fc.hmac_sha256(state["chain_secret"], runtime.controller_seed_body(old_seed))
        review_state_path.write_text(json.dumps(old_seed, sort_keys=True), encoding="utf-8")

        changed_digests = self.changed_policy_digests()
        with mock.patch.object(fc, "current_review_policy_digests", return_value=changed_digests):
            fc.review_policy_update_command(self.host, self.args, self.row)
        new_state = self.state()
        new_state["current_run"] = state["current_run"]
        fc.write_review_state_seed(stage_artifacts, new_state, new_state["review_policy"], self.row, [])

        reseeded = json.loads(review_state_path.read_text())
        self.assertEqual(new_state["review_policy"]["amendment_id"], reseeded["policy"]["amendment_id"])
        self.assertEqual([{"id": "coverage-1"}], reseeded["coverage_records"])
        self.assertEqual([{"id": "finding-1", "status": "open"}], reseeded["findings"])
        self.assertEqual([{"id": "segment-1"}], reseeded["segment_requirements"])
        self.assertEqual([{"command": "unit"}], reseeded["receipts"])
        self.assertEqual(new_state["review_policy"]["effective_workflow_source_digest"], reseeded["protected_inputs"]["captured_source_digest"])
        fc.verify_review_state_seed_for_refresh(new_state, reseeded)

    def test_controller_seed_and_refresh_default_to_conservative_specialist_risks(self):
        fc.review_policy_update_command(self.host, self.args, self.row)
        self.args.qualification_packet = self.qualification_packet()
        fc.review_policy_update_command(self.host, self.args, self.row)
        state = self.state()
        stage_artifacts = self.root / "risk-stage-artifacts"
        stage_artifacts.mkdir()
        state["current_run"] = {**state["current_run"], "phase": "implement", "repo": "api", "run_id": RUN, "artifacts_dir": str(stage_artifacts)}
        state["stages"]["api"]["plan"] = {
            "depends_on": [],
            "files_allowlist": ["claim.ts"],
            "test_patterns": ["claim.test.ts"],
            "verification": [{"id": "unit", "argv": ["true"]}],
        }
        fc.write_review_state_seed(stage_artifacts, state, state["review_policy"], self.row, [])
        seed_path = stage_artifacts / "review-state.json"
        seed = json.loads(seed_path.read_text(encoding="utf-8"))
        self.assertEqual(["authorization", "transactions", "query_volume", "cli_behavior"], seed["risk_areas"])
        self.assertFalse(seed["cross_store"])
        self.assertFalse(seed["impact_evidence_available"])
        self.assertTrue(seed["impact_tooling_missing"])

        seed["risk_areas"] = []
        seed["impact_evidence_available"] = True
        seed["protected_inputs"]["scope_inputs_digest"] = runtime.digest_state_items(seed, runtime.SCOPE_AUTHORITY_KEYS)
        seed["controller_mac"] = fc.hmac_sha256(state["chain_secret"], runtime.controller_seed_body(seed))
        seed_path.write_text(json.dumps(seed, sort_keys=True), encoding="utf-8")
        fc.write_state(self.control, state)

        refreshed = fc.refresh_review_state(self.control, CHAIN, stage_artifacts)
        self.assertEqual(["authorization", "transactions", "query_volume", "cli_behavior"], refreshed["risk_areas"])
        self.assertFalse(refreshed["impact_evidence_available"])
        previous = dict(os.environ)
        os.environ["ARCHON_CONTROL_DIR"] = str(self.control)
        os.environ["ARCHON_FEATURE_CHAIN_ID"] = CHAIN
        try:
            runtime.verify_authority(stage_artifacts, refreshed)
        finally:
            os.environ.clear()
            os.environ.update(previous)

    def test_qualification_requires_controller_owned_usage(self):
        fc.review_policy_update_command(self.host, self.args, self.row)
        packet = self.qualification_packet()
        data = json.loads(packet.read_text())
        usage_ref = data["matched_repairs"][0]["risk_delta"]["usage"]
        usage_path = self.root / usage_ref["path"]
        usage = json.loads(usage_path.read_text())
        usage["gross_tokens"] += 1
        usage_path.write_text(json.dumps(usage), encoding="utf-8")
        usage_ref["sha256"] = hashlib.sha256(usage_path.read_bytes()).hexdigest()
        packet.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        self.args.qualification_packet = packet

        with self.assertRaisesRegex(fc.FeatureChainError, "controller"):
            fc.review_policy_update_command(self.host, self.args, self.row)

    def test_unqualified_policy_blocks_dispatch_and_resume_control(self):
        fc.review_policy_update_command(self.host, self.args, self.row)

        with self.assertRaisesRegex(fc.FeatureChainError, "not qualified"):
            fc.dispatch_lane(self.host, Namespace(control_dir=self.control), "lane", self.spec, {
                "ARCHON_FEATURE_SCOPE": "repositories",
                "ARCHON_FEATURE_CHAIN_ID": CHAIN,
            })

        control = ar.require_control_token(self.row, self.control, "operator-token")
        with self.assertRaisesRegex(fc.FeatureChainError, "not qualified"):
            fc.before_control(self.host, Namespace(**{**vars(self.args), "action": "resume"}), self.row, control)

        abandoned = fc.before_control(self.host, Namespace(**{**vars(self.args), "action": "abandon"}), self.row, control)
        self.assertEqual("abandon", abandoned["pending_control"]["action"])

    def test_rejects_bad_token_active_run_wrong_provider_and_digest_mismatch(self):
        self.args.token = "wrong-token"
        with self.assertRaises(SystemExit):
            fc.review_policy_update_command(self.host, self.args, self.row)
        self.args.token = "operator-token"

        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE remote_agent_workflow_runs SET status='running' WHERE id=?", (RUN,))
        with self.assertRaisesRegex(fc.FeatureChainError, "stopped"):
            fc.review_policy_update_command(self.host, self.args, self.row)
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE remote_agent_workflow_runs SET status='failed' WHERE id=?", (RUN,))

        self.write_control(provider="claude")
        with self.assertRaisesRegex(fc.FeatureChainError, "Codex"):
            fc.review_policy_update_command(self.host, self.args, self.row)
        self.write_control()

        self.args.expected_captured_source_digest = "2" * 64
        with self.assertRaisesRegex(fc.FeatureChainError, "expected captured source digest"):
            fc.review_policy_update_command(self.host, self.args, self.row)

    def test_requires_persisted_workflow_source_metadata_and_verified_manifest(self):
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE remote_agent_workflow_runs SET metadata=? WHERE id=?", (json.dumps({}), RUN))
        with self.assertRaisesRegex(fc.FeatureChainError, "workflow source metadata is missing"):
            fc.review_policy_update_command(self.host, self.args, self.row)

        with sqlite3.connect(self.db) as con:
            con.execute(
                "UPDATE remote_agent_workflow_runs SET metadata=? WHERE id=?",
                (json.dumps({"workflow_source": self.workflow_source}, sort_keys=True), RUN),
            )
        (Path(self.workflow_source["root"]) / "project/.archon/workflows/full-sdlc-api-codex.yaml").write_text("nodes: [drift]\n", encoding="utf-8")
        with self.assertRaisesRegex(fc.FeatureChainError, "digest does not match"):
            fc.review_policy_update_command(self.host, self.args, self.row)

    def test_rejects_competing_controls_amendments_dispatch_and_live_processes(self):
        state = self.state()
        state["pending_control"] = {"owner_pid": fc.os.getpid(), "owner_fingerprint": fc.process_fingerprint()}
        fc.write_state(self.control, state)
        with self.assertRaisesRegex(fc.FeatureChainError, "control already in progress"):
            fc.review_policy_update_command(self.host, self.args, self.row)

        state = self.state()
        state["pending_control"] = None
        state["dispatch_reservation"] = {"phase": "planning"}
        fc.write_state(self.control, state)
        with self.assertRaisesRegex(fc.FeatureChainError, "dispatch already in progress"):
            fc.review_policy_update_command(self.host, self.args, self.row)

        state = self.state()
        state["dispatch_reservation"] = None
        state["budget_amendment"] = {"status": "in_progress"}
        fc.write_state(self.control, state)
        with self.assertRaisesRegex(fc.FeatureChainError, "budget amendment"):
            fc.review_policy_update_command(self.host, self.args, self.row)

        state = self.state()
        state["budget_amendment"] = None
        state["scope_amendment"] = {"status": "in_progress"}
        fc.write_state(self.control, state)
        with self.assertRaisesRegex(fc.FeatureChainError, "scope amendment"):
            fc.review_policy_update_command(self.host, self.args, self.row)

        state = self.state()
        state["scope_amendment"] = None
        fc.write_state(self.control, state)
        with mock.patch.object(fc.os, "killpg", return_value=None):
            with self.assertRaisesRegex(fc.FeatureChainError, "launcher process group is live"):
                fc.review_policy_update_command(self.host, self.args, self.row)

    def test_incomplete_journal_blocks_dispatch_and_retries_same_request(self):
        original = fc.write_state
        calls = {"count": 0}

        def flaky(control_dir, state):
            result = original(control_dir, state)
            if isinstance(state.get("review_policy_amendment"), dict) and state["review_policy_amendment"].get("status") == "in_progress":
                calls["count"] += 1
                if calls["count"] == 1:
                    raise RuntimeError("interrupted after in-progress review policy write")
            return result

        with mock.patch.object(fc, "write_state", side_effect=flaky):
            with self.assertRaisesRegex(RuntimeError, "in-progress"):
                fc.review_policy_update_command(self.host, self.args, self.row)

        pending = self.state()
        self.assertEqual("in_progress", pending["review_policy_amendment"]["status"])
        with self.assertRaisesRegex(fc.FeatureChainError, "review policy amendment"):
            fc.dispatch_lane(self.host, Namespace(control_dir=self.control), "lane", self.spec, {
                "ARCHON_FEATURE_SCOPE": "repositories",
                "ARCHON_FEATURE_CHAIN_ID": CHAIN,
            })

        result = fc.review_policy_update_command(self.host, self.args, self.row)
        state = self.state()
        self.assertFalse(result["already_applied"])
        self.assertIsNone(state["review_policy_amendment"])
        self.assertEqual(1, len(state["review_policy_amendments"]))

    def test_policy_drift_blocks_later_execution(self):
        fc.review_policy_update_command(self.host, self.args, self.row)
        self.args.qualification_packet = self.qualification_packet()
        fc.review_policy_update_command(self.host, self.args, self.row)
        state = self.state()
        state["review_policy"]["helper_workflow_digests"] = {
            **state["review_policy"]["helper_workflow_digests"],
            "sha256": "9" * 64,
        }
        fc.write_state(self.control, state)

        with self.assertRaisesRegex(fc.FeatureChainError, "digest drifted"):
            fc.dispatch_lane(self.host, Namespace(control_dir=self.control), "lane", self.spec, {
                "ARCHON_FEATURE_SCOPE": "repositories",
                "ARCHON_FEATURE_CHAIN_ID": CHAIN,
            })

    def test_effective_source_drift_blocks_later_execution(self):
        fc.review_policy_update_command(self.host, self.args, self.row)
        self.args.qualification_packet = self.qualification_packet()
        fc.review_policy_update_command(self.host, self.args, self.row)
        state = self.state()
        effective = state["review_policy"]["effective_workflow_source"]
        workflow = Path(effective["root"]) / "project/.archon/workflows/full-sdlc-api-codex.yaml"
        workflow.write_text(workflow.read_text() + "\n# drift\n", encoding="utf-8")

        with self.assertRaisesRegex(fc.FeatureChainError, "digest does not match"):
            fc.require_review_policy_integrity(state, self.row, self.db)

    def test_dispatch_source_check_allows_new_capture_root_with_same_digest_and_lineage(self):
        fc.review_policy_update_command(self.host, self.args, self.row)
        self.args.qualification_packet = self.qualification_packet()
        fc.review_policy_update_command(self.host, self.args, self.row)
        state = self.state()
        effective = state["review_policy"]["effective_workflow_source"]
        new_root = self.artifacts / "new-run-workflow-source"
        shutil.copytree(Path(effective["root"]), new_root)
        source = {**effective, "root": str(new_root)}
        new_run = "a" * 32
        with sqlite3.connect(self.db) as con:
            con.execute(
                "INSERT INTO remote_agent_workflow_runs VALUES (?,?,?,?,?,?,?)",
                (
                    new_run,
                    self.row["workflow_name"],
                    self.row["user_message"],
                    "running",
                    str(self.output_root),
                    "2026-09-14 00:01:00",
                    json.dumps({"workflow_source": source}, sort_keys=True),
                ),
            )

        fc.verify_dispatch_workflow_source(self.db, new_run, state)

    def test_guarded_run_command_uses_effective_workflow_source_env(self):
        with mock.patch.dict(fc.os.environ, {"ARCHON_EFFECTIVE_WORKFLOW_SOURCE_ROOT": "/tmp/effective-source"}):
            command = ar.command_for("run", "full-sdlc-api-codex\0" + str(self.spec))

        self.assertIn("--workflow-source", command)
        self.assertEqual("/tmp/effective-source", command[command.index("--workflow-source") + 1])

    def test_parser_registers_feature_review_policy_update(self):
        parsed = ar.parser().parse_args([
            "feature-review-policy-update",
            RUN,
            "--token",
            "t",
            "--policy",
            "risk-delta-v1",
            "--expected-captured-source-digest",
            self.workflow_source["digest"],
            "--qualification-packet",
            str(self.root / "qualification.json"),
            "--reason",
            "authorized",
        ])

        self.assertEqual("feature-review-policy-update", parsed.action)
        self.assertEqual("risk-delta-v1", parsed.policy)
        self.assertEqual(self.workflow_source["digest"], parsed.expected_captured_source_digest)
        self.assertEqual(self.root / "qualification.json", parsed.qualification_packet)


if __name__ == "__main__":
    unittest.main()
