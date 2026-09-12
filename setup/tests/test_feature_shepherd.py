import contextlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import subprocess
import unittest
from unittest import mock
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import feature_chain as fc
import yaml


class FeatureShepherd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.spec = self.root / "feature.md"
        self.spec.write_text("# Share a group\nAPI and MCP share one interface.\n")
        self.args = SimpleNamespace(control_dir=self.root / "private", provider="codex",
                                    spec=str(self.spec), max_total_tokens=30_000_000)

    def test_underfunded_start_warns_and_dispatches(self):
        with mock.patch.object(fc, "make_initial_state", return_value={}) as create, \
                mock.patch.object(fc, "write_state", side_effect=lambda _, state: state), \
                mock.patch.object(fc, "budget_init"), \
                mock.patch.object(fc, "dispatch_planning", return_value={"state": {}, "row": {}}) as dispatch, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            fc.launch(SimpleNamespace(), self.args, ["api", "goodword-mcp"])
        create.assert_called_once()
        dispatch.assert_called_once()
        self.assertIn("BUDGET_SHEPHERD=WARN", output.getvalue())

    def test_spec_estimate_does_not_launch_or_write_product_files(self):
        self.args.scope = "api,goodword-mcp"
        report = fc.estimate_command(SimpleNamespace(), self.args)
        self.assertEqual("at_risk", report["disposition"])
        self.assertEqual(60_000_000, report["recommended_total_tokens"])
        self.assertEqual("# Share a group\nAPI and MCP share one interface.\n", self.spec.read_text())
        self.assertFalse(self.args.control_dir.exists())

    def test_legacy_checkpoint_skips_accounting_and_does_not_mutate(self):
        state = {"provider": "codex", "budget": {"max_total_tokens": 30_000_000}}
        with mock.patch.object(fc, "budget_usage") as usage, mock.patch.object(fc, "write_state") as write:
            self.assertIsNone(fc.shepherd_unlocked(self.args, state))
        usage.assert_not_called()
        write.assert_not_called()

    def test_boundary_warning_preserves_approval_and_records_forecast(self):
        artifacts = self.root / "artifacts"
        state = {"logical_chain_id": "a" * 32, "provider": "codex", "repositories": ["api", "goodword-mcp"],
                 "spec_sha256": "b" * 64, "budget_shepherd_version": 1,
                 "budget": {"max_total_tokens": 30_000_000}, "approval": {"digest": "frozen"},
                 "stages": {"api": {"status": "pending"}, "goodword-mcp": {"status": "pending"}},
                 "current_run": {"run_id": "original", "artifacts_dir": str(artifacts)}}
        with mock.patch.object(fc, "budget_usage", return_value={"total_tokens": 20_000_000}), mock.patch.object(fc, "write_state"):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                fc.shepherd_unlocked(self.args, state)
        self.assertIn("BUDGET_SHEPHERD=WARN", output.getvalue())
        self.assertEqual({"digest": "frozen"}, state["approval"])
        self.assertEqual("original", state["current_run"]["run_id"])
        report = json.loads((artifacts / "budget-forecast.json").read_text())
        self.assertEqual(20_000_000, report["used_tokens"])
        self.assertEqual("at_risk", report["disposition"])
        self.assertEqual(30_000_000, state["budget"]["max_total_tokens"])

    def test_shepherd_still_stops_at_actual_hard_caps(self):
        state = {"logical_chain_id": "a" * 32, "provider": "codex", "budget_shepherd_version": 1}
        for flag, reason in (("tokens_exhausted", "token"), ("wall_exhausted", "wall")):
            with self.subTest(flag=flag), mock.patch.object(fc, "budget_usage", return_value={flag: True}):
                with self.assertRaisesRegex(fc.FeatureChainError, reason + " budget is exhausted"):
                    fc.shepherd_unlocked(self.args, state)

    def test_estimate_chain_rejects_allowance_override(self):
        self.args.chain = "a" * 32
        with self.assertRaisesRegex(fc.FeatureChainError, "existing scope and allowance"):
            fc.estimate_command(SimpleNamespace(), self.args)

    def test_review_boundary_obeys_hard_failure_but_continues_on_warning(self):
        workflow = Path(__file__).resolve().parents[2] / "workflows/full-sdlc-api.yaml"
        doc = yaml.safe_load(workflow.read_text())
        loop = next(node for node in doc["nodes"] if node["id"] == "plan-loop")
        script = next(node["bash"] for node in loop["loop_group"]["nodes"] if node["id"] == "plan-round-pre")
        stub = self.root / "forecast.py"
        stub.write_text('import sys\nprint("BUDGET_SHEPHERD=STOP")\nsys.exit(1)\n')
        script = script.replace("/Users/eduardopicazo/Documents/Workspace/Goodword/.archon/setup/archon-run.py", str(stub))
        (self.root / "plan-round.txt").write_text("2\n")
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                env=dict(os.environ, ARTIFACTS_DIR=str(self.root),
                                         ARCHON_FEATURE_SCOPE="repositories", ARCHON_FEATURE_CHAIN_ID="a" * 32))
        self.assertNotEqual(0, result.returncode)
        self.assertIn("BUDGET_SHEPHERD=STOP", result.stdout)
        self.assertEqual("2\n", (self.root / "plan-round.txt").read_text())
        self.assertFalse((self.root / "plan-round-3").exists())
        stub.write_text('print("BUDGET_SHEPHERD=WARN")\n')
        (self.root / "plan.md").write_text("# Existing plan\n")
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                env=dict(os.environ, ARTIFACTS_DIR=str(self.root),
                                         ARCHON_FEATURE_SCOPE="repositories", ARCHON_FEATURE_CHAIN_ID="a" * 32))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("BUDGET_SHEPHERD=WARN", result.stdout)
        self.assertEqual("3\n", (self.root / "plan-round.txt").read_text())


if __name__ == "__main__":
    unittest.main()
