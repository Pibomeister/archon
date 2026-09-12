#!/usr/bin/env python3
import json
import os
from unittest import mock
import sys
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SETUP))
import control_contract


SECRET = "controller-secret-" * 3


class ApprovalSnapshotTest(unittest.TestCase):
    def write_artifact(self, root: Path, name: str, value: str = "ok") -> None:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")

    def capture(self, root: Path, out: Path, allowlist=None, provider="codex"):
        if allowlist is None:
            allowlist = ["plan.json", "nested/evidence.txt"]
        return control_contract.capture_approval_snapshot(
            out,
            artifacts_dir=root,
            allowlist=allowlist,
            run_id="run-123",
            gate_id="plan-gate",
            native_pause_identity={"pause_id": "native-1", "node": "plan-gate"},
            provider=provider,
            context_digests={"workflow": "a" * 64, "policy": "b" * 64},
            controller_secret=SECRET,
        )

    def test_snapshot_captures_private_authoritative_bytes_and_decision_replays(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "artifacts"
            self.write_artifact(root, "plan.json", '{"plan":true}\n')
            self.write_artifact(root, "nested/evidence.txt", "observed")
            snapshot_path = Path(td) / "private" / "snapshot.json"
            snapshot = self.capture(root, snapshot_path)

            self.assertEqual(snapshot_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(snapshot["files"][0]["content_b64"], "eyJwbGFuIjp0cnVlfQo=")
            self.assertEqual(snapshot["files"][0]["path"], "plan.json")
            self.assertEqual(snapshot["files"][0]["size"], 14)

            decision_path = Path(td) / "private" / "decision.json"
            decision = control_contract.record_operator_decision(
                decision_path,
                snapshot_path=snapshot_path,
                expected_snapshot_digest=snapshot["snapshot_digest"],
                decision="approved",
                operator_id="operator@example.com",
                controller_secret=SECRET,
                artifacts_dir=root,
            )
            replay = control_contract.validate_operator_decision(
                decision_path,
                snapshot_path=snapshot_path,
                expected_snapshot_digest=snapshot["snapshot_digest"],
                controller_secret=SECRET,
                artifacts_dir=root,
            )
            self.assertEqual(replay["decision"], "approved")
            self.assertEqual(decision["snapshot_digest"], snapshot["snapshot_digest"])

    def test_both_providers_require_the_external_key_and_preserve_rejection(self):
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as td:
                root = Path(td) / "artifacts"
                self.write_artifact(root, "plan.json", "ok")
                snapshot_path = Path(td) / "snapshot.json"
                snapshot = self.capture(root, snapshot_path, ["plan.json"], provider)
                self.assertEqual(snapshot["provider"], provider)
                self.assertEqual(snapshot["files"][0]["content_b64"], "b2s=")
                with self.assertRaisesRegex(control_contract.ControlContractError, "MAC"):
                    control_contract.verify_approval_snapshot(snapshot_path, controller_secret="wrong-key" * 8)
                decision_path = Path(td) / "decision.json"
                control_contract.record_operator_decision(
                    decision_path, decision="rejected", operator_id="fixture-operator",
                    snapshot_path=snapshot_path,
                    expected_snapshot_digest=snapshot["snapshot_digest"],
                    controller_secret=SECRET, artifacts_dir=root,
                )
                result = control_contract.validate_operator_decision(
                    decision_path, snapshot_path=snapshot_path,
                    expected_snapshot_digest=snapshot["snapshot_digest"],
                    controller_secret=SECRET, artifacts_dir=root,
                )
                self.assertEqual(result["decision"], "rejected")
                self.assertEqual(result["gate_id"], "plan-gate")
                self.assertEqual(result["run_id"], "run-123")
                before = decision_path.read_bytes()
                control_contract.record_operator_decision(
                    decision_path, decision="rejected", operator_id="fixture-operator",
                    snapshot_path=snapshot_path,
                    expected_snapshot_digest=snapshot["snapshot_digest"],
                    controller_secret=SECRET, artifacts_dir=root,
                )
                self.assertEqual(decision_path.read_bytes(), before)

    def test_concurrent_identical_decisions_replay_without_overwriting(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "artifacts"
            self.write_artifact(root, "plan.json", "ok")
            snapshot_path = Path(td) / "snapshot.json"
            snapshot = self.capture(root, snapshot_path, ["plan.json"])
            decision_path = Path(td) / "decision.json"
            create = control_contract.secure_create_json

            def competing_create(path, data):
                create(path, data)
                create(path, data)

            with mock.patch.object(control_contract, "secure_create_json", side_effect=competing_create):
                result = control_contract.record_operator_decision(
                    decision_path, snapshot_path=snapshot_path,
                    expected_snapshot_digest=snapshot["snapshot_digest"],
                    decision="approved", operator_id="fixture-operator", controller_secret=SECRET,
                )
            self.assertEqual(result["decision"], "approved")
            self.assertEqual(result["operator_id"], "fixture-operator")

    def test_malformed_decision_is_a_contract_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "artifacts"
            self.write_artifact(root, "plan.json", "ok")
            snapshot_path = Path(td) / "snapshot.json"
            snapshot = self.capture(root, snapshot_path, ["plan.json"])
            with self.assertRaises(control_contract.ControlContractError):
                control_contract.record_operator_decision(
                    Path(td) / "decision.json", snapshot_path=snapshot_path,
                    expected_snapshot_digest=snapshot["snapshot_digest"], decision=json.loads("[]"),
                    operator_id="fixture-operator", controller_secret=SECRET,
                )

    def test_capture_limits_cannot_exceed_verifier_limits(self):
        for limits in (
            {"max_file_bytes": control_contract.MAX_APPROVAL_SNAPSHOT_FILE_BYTES + 1},
            {"max_total_bytes": control_contract.MAX_APPROVAL_SNAPSHOT_TOTAL_BYTES + 1},
            {"max_file_bytes": True},
            {"max_total_bytes": 0},
        ):
            with self.subTest(limits=limits), tempfile.TemporaryDirectory() as td:
                root = Path(td) / "artifacts"
                self.write_artifact(root, "plan.json", "ok")
                with self.assertRaisesRegex(control_contract.ControlContractError, "limit"):
                    control_contract.capture_approval_snapshot(
                        Path(td) / "snapshot.json", artifacts_dir=root,
                        allowlist=["plan.json"], run_id="run-1", gate_id="gate-1",
                        native_pause_identity={"event_id": "event-1"}, provider="claude",
                        context_digests={"workflow": "a" * 64}, controller_secret=SECRET,
                        **limits,
                    )

    def test_private_write_failure_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "state.json"
            with mock.patch.object(control_contract.os, "fsync", side_effect=OSError("disk error")):
                with self.assertRaisesRegex(OSError, "disk error"):
                    control_contract.secure_create_json(output, {"state": "new"})
            self.assertEqual(list(Path(td).iterdir()), [])

    def test_capture_never_overwrites_an_existing_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "artifacts"
            self.write_artifact(root, "plan.json", "original")
            output = Path(td) / "snapshot.json"
            self.capture(root, output, ["plan.json"])
            before = output.read_bytes()
            self.write_artifact(root, "plan.json", "replacement")
            with self.assertRaisesRegex(control_contract.ControlContractError, "already exists"):
                self.capture(root, output, ["plan.json"])
            self.assertEqual(output.read_bytes(), before)
            self.assertEqual(sorted(p.name for p in Path(td).iterdir()), ["artifacts", "snapshot.json"])

    def test_nonregular_artifacts_are_opened_without_blocking(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "artifacts"
            root.mkdir()
            os.mkfifo(root / "pipe")
            real_open = os.open

            def checked_open(path, flags, *args, **kwargs):
                if path == "pipe":
                    self.assertTrue(flags & os.O_NONBLOCK, "FIFO open must not block before type validation")
                return real_open(path, flags, *args, **kwargs)

            with mock.patch.object(control_contract.os, "open", side_effect=checked_open):
                with self.assertRaisesRegex(control_contract.ControlContractError, "regular file"):
                    self.capture(root, Path(td) / "snapshot.json", ["pipe"])

    def test_malformed_mac_is_a_contract_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "artifacts"
            self.write_artifact(root, "plan.json")
            snapshot = self.capture(root, Path(td) / "snapshot.json", ["plan.json"])
            for mac in (None, [], 42, "not-a-mac"):
                with self.subTest(mac=mac), self.assertRaises(control_contract.ControlContractError):
                    control_contract.verify_approval_snapshot({**snapshot, "authority_mac": mac}, controller_secret=SECRET)

    def test_boolean_schema_is_rejected_even_with_valid_seal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "artifacts"
            self.write_artifact(root, "plan.json")
            snapshot = self.capture(root, Path(td) / "snapshot.json", ["plan.json"])
            body = {k: v for k, v in snapshot.items() if k not in {"snapshot_digest", "authority_mac"}}
            body["schema_version"] = True
            malformed = control_contract._seal(body, "snapshot_digest", SECRET)
            with self.assertRaisesRegex(control_contract.ControlContractError, "schema"):
                control_contract.verify_approval_snapshot(malformed, controller_secret=SECRET)

    def test_record_decision_rejects_drift_and_snapshot_digest_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "artifacts"
            self.write_artifact(root, "plan.json", "before")
            snapshot_path = Path(td) / "private" / "snapshot.json"
            snapshot = self.capture(root, snapshot_path, ["plan.json"])
            self.write_artifact(root, "plan.json", "after")

            with self.assertRaisesRegex(control_contract.ControlContractError, "state drift"):
                control_contract.record_operator_decision(
                    Path(td) / "private" / "decision.json",
                    snapshot_path=snapshot_path,
                    expected_snapshot_digest=snapshot["snapshot_digest"],
                    decision="approved",
                    operator_id="operator@example.com",
                    controller_secret=SECRET,
                    artifacts_dir=root,
                )
            with self.assertRaisesRegex(control_contract.ControlContractError, "snapshot digest"):
                control_contract.record_operator_decision(
                    Path(td) / "private" / "decision-2.json",
                    snapshot_path=snapshot_path,
                    expected_snapshot_digest="0" * 64,
                    decision="approved",
                    operator_id="operator@example.com",
                    controller_secret=SECRET,
                )

    def test_rejects_unsafe_or_malformed_allowlist_entries(self):
        cases = [
            [],
            ["plan.json", "plan.json"],
            ["../plan.json"],
            ["/tmp/plan.json"],
            ["nested/../plan.json"],
            ["missing.json"],
        ]
        for allowlist in cases:
            with self.subTest(allowlist=allowlist), tempfile.TemporaryDirectory() as td:
                root = Path(td) / "artifacts"
                self.write_artifact(root, "plan.json", "ok")
                with self.assertRaises(control_contract.ControlContractError):
                    self.capture(root, Path(td) / "private" / "snapshot.json", allowlist)

    def test_rejects_malformed_context_and_unknown_provider(self):
        cases = [
            {"provider": "gemini", "pause": {"pause_id": "native-1"}, "context": {"workflow": "a" * 64}},
            {"provider": "codex", "pause": {}, "context": {"workflow": "a" * 64}},
            {"provider": "codex", "pause": {"pause_id": "native-1"}, "context": {"workflow": "bad"}},
        ]
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as td:
                root = Path(td) / "artifacts"
                self.write_artifact(root, "plan.json", "ok")
                with self.assertRaises(control_contract.ControlContractError):
                    control_contract.capture_approval_snapshot(
                        Path(td) / "private" / "snapshot.json",
                        artifacts_dir=root,
                        allowlist=["plan.json"],
                        run_id="run-123",
                        gate_id="plan-gate",
                        native_pause_identity=case["pause"],
                        provider=case["provider"],
                        context_digests=case["context"],
                        controller_secret=SECRET,
                    )

    def test_rejects_symlink_hardlink_and_oversize_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "artifacts"
            self.write_artifact(root, "target.txt", "ok")
            (root / "link.txt").symlink_to(root / "target.txt")
            with self.assertRaisesRegex(control_contract.ControlContractError, "symlink"):
                self.capture(root, Path(td) / "private" / "snapshot.json", ["link.txt"])

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "artifacts"
            self.write_artifact(root, "target.txt", "ok")
            (root / "hard.txt").hardlink_to(root / "target.txt")
            with self.assertRaisesRegex(control_contract.ControlContractError, "hardlink"):
                self.capture(root, Path(td) / "private" / "snapshot.json", ["hard.txt"])

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "artifacts"
            self.write_artifact(root, "large.txt", "abcdef")
            with self.assertRaisesRegex(control_contract.ControlContractError, "too large"):
                control_contract.capture_approval_snapshot(
                    Path(td) / "private" / "snapshot.json",
                    artifacts_dir=root,
                    allowlist=["large.txt"],
                    run_id="run-123",
                    gate_id="plan-gate",
                    native_pause_identity={"pause_id": "native-1"},
                    provider="codex",
                    context_digests={"workflow": "a" * 64},
                    controller_secret=SECRET,
                    max_file_bytes=5,
                )

    def test_rejects_tampered_snapshot_and_conflicting_duplicate_decision(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "artifacts"
            self.write_artifact(root, "plan.json", "ok")
            snapshot_path = Path(td) / "private" / "snapshot.json"
            snapshot = self.capture(root, snapshot_path, ["plan.json"])
            saved = json.loads(snapshot_path.read_text(encoding="utf-8"))
            saved["files"][0]["sha256"] = "0" * 64
            snapshot_path.write_text(json.dumps(saved), encoding="utf-8")
            snapshot_path.chmod(0o600)
            with self.assertRaisesRegex(control_contract.ControlContractError, "MAC|digest|content"):
                control_contract.verify_approval_snapshot(snapshot_path, controller_secret=SECRET)

            snapshot = self.capture(root, Path(td) / "private" / "snapshot-2.json", ["plan.json"])
            decision_path = Path(td) / "private" / "decision.json"
            control_contract.record_operator_decision(
                decision_path,
                snapshot_path=Path(td) / "private" / "snapshot-2.json",
                expected_snapshot_digest=snapshot["snapshot_digest"],
                decision="approved",
                operator_id="operator@example.com",
                controller_secret=SECRET,
                artifacts_dir=root,
            )
            with self.assertRaisesRegex(control_contract.ControlContractError, "conflicting"):
                control_contract.record_operator_decision(
                    decision_path,
                    snapshot_path=Path(td) / "private" / "snapshot-2.json",
                    expected_snapshot_digest=snapshot["snapshot_digest"],
                    decision="rejected",
                    operator_id="operator@example.com",
                    controller_secret=SECRET,
                    artifacts_dir=root,
                )


if __name__ == "__main__":
    unittest.main()
