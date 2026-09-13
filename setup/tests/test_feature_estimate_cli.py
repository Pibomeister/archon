import contextlib
import importlib.util
import io
import json
from argparse import Namespace
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SETUP = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("scope_launcher", SETUP / "archon-run.py")
ar = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ar)


class FeatureEstimateCli(unittest.TestCase):
    def test_underfunded_json_checkpoint_reports_once_and_continues(self):
        report = {"disposition": "at_risk", "recommended_total_tokens": 60_000_000}
        args = Namespace(chain="a" * 32, json=True)
        with mock.patch.object(ar, "repository_feature_call", return_value=report), contextlib.redirect_stdout(io.StringIO()) as output:
            ar.feature_shepherd_command(args)
        self.assertEqual(report, json.loads(output.getvalue()))

    def test_human_output_shows_real_phase_range_and_uncertainty(self):
        report = {"disposition": "at_risk", "confidence": "low",
                  "phases": [{"phase": "planning", "range": {"low": 6_000_000, "high": 12_000_000},
                              "confidence": "low", "evidence": {"completed_samples": 0}}],
                  "model_caveat": "not statistically calibrated"}
        with contextlib.redirect_stdout(io.StringIO()) as output:
            ar.print_feature_estimate(report)
        self.assertIn("phase=planning", output.getvalue())
        self.assertIn("12000000", output.getvalue())
        self.assertIn("not statistically calibrated", output.getvalue())
        self.assertNotIn("phase=None", output.getvalue())

    def test_parser_accepts_spec_estimate_and_json(self):
        args = ar.parser().parse_args([
            "feature-estimate",
            "--scope",
            "api,goodword-mcp",
            "--json",
            "/tmp/spec.md",
        ])

        self.assertEqual(args.action, "feature-estimate")
        self.assertEqual(args.scope, "api,goodword-mcp")
        self.assertEqual(args.spec, "/tmp/spec.md")
        self.assertTrue(args.json)

    def test_parser_accepts_chain_estimate_without_scope_or_spec(self):
        args = ar.parser().parse_args(["feature-estimate", "--chain", "c" * 32])

        self.assertEqual(args.chain, "c" * 32)
        self.assertIsNone(args.scope)
        self.assertIsNone(args.spec)


    def test_parser_accepts_provider_usage_accounting_command(self):
        args = ar.parser().parse_args([
            "feature-account-provider-usage",
            "r" * 32,
            "--token",
            "operator-token",
            "--event-id",
            "d2f8f63a-2b7d-4023-9209-d56851b28e02",
            "--transcript",
            "/tmp/provider.jsonl",
        ])

        self.assertEqual(args.action, "feature-account-provider-usage")
        self.assertEqual(args.run_id, "r" * 32)
        self.assertEqual(args.token, "operator-token")
        self.assertEqual(args.event_id, "d2f8f63a-2b7d-4023-9209-d56851b28e02")
        self.assertEqual(args.transcript, Path("/tmp/provider.jsonl"))

    def test_provider_usage_accounting_routes_through_controller(self):
        row = {"id": "r" * 32}
        result = {
            "chain": "c" * 32,
            "run_id": row["id"],
            "event_id": "d2f8f63a-2b7d-4023-9209-d56851b28e02",
        }
        args = Namespace(
            action="feature-account-provider-usage",
            run_id=row["id"],
            token="operator-token",
            event_id=result["event_id"],
            transcript=Path("/tmp/provider.jsonl"),
            control_dir=Path("/private/control"),
            db=Path("/tmp/archon.db"),
        )
        with mock.patch.object(ar, "validate_control_location") as validate, \
                mock.patch.object(ar, "resolve_run", return_value=row) as resolve, \
                mock.patch.object(ar, "repository_feature_call", return_value=result) as controller, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            ar.feature_account_provider_usage_command(args)

        validate.assert_called_once_with(args.control_dir)
        resolve.assert_called_once_with(args.db, row["id"])
        controller.assert_called_once_with("account_provider_usage_command", args, row)
        self.assertIn("ARCHON_FEATURE_ACCOUNT_PROVIDER_USAGE=APPLIED", output.getvalue())
        self.assertIn("event=d2f8f63a-2b7d-4023-9209-d56851b28e02", output.getvalue())


    def test_main_dispatches_provider_usage_accounting_command(self):
        args = Namespace(action="feature-account-provider-usage")

        class Parser:
            @staticmethod
            def parse_args():
                return args

        with mock.patch.object(ar, "parser", return_value=Parser()), \
                mock.patch.object(ar, "feature_account_provider_usage_command") as command:
            ar.main()

        command.assert_called_once_with(args)

    def test_parser_accepts_shepherd_command(self):
        args = ar.parser().parse_args(["feature-shepherd", "--chain", "c" * 32, "--json"])

        self.assertEqual(args.action, "feature-shepherd")
        self.assertEqual(args.chain, "c" * 32)
        self.assertTrue(args.json)

    def test_spec_estimate_routes_to_repository_controller_without_launch(self):
        report = {
            "disposition": "sufficient",
            "allowance": 30_000_000,
            "used_tokens": 0,
            "remaining_allowance": 30_000_000,
            "recommended_total_tokens": 12_000_000,
            "remaining_range": [8_000_000, 12_000_000],
            "confidence": "low",
            "phases": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            spec = Path(directory) / "spec.md"
            spec.write_text("# Feature\n", encoding="utf-8")
            args = Namespace(
                action="feature-estimate",
                chain=None,
                scope="api,goodword-mcp",
                spec=str(spec),
                json=True,
                max_total_tokens=None,
            )
            with mock.patch.dict(ar.os.environ, {}, clear=True), \
                    mock.patch.object(ar, "repository_feature_call", return_value=report) as controller, \
                    mock.patch.object(ar, "adaptive_feature") as launch, \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                ar.feature_estimate_command(args)

        controller.assert_called_once_with("estimate_command", args)
        launch.assert_not_called()
        self.assertIn('"disposition": "sufficient"', output.getvalue())

    def test_chain_estimate_rejects_extra_spec_arguments_in_controller(self):
        with tempfile.TemporaryDirectory() as directory:
            spec = Path(directory) / "spec.md"
            spec.write_text("# Feature\n", encoding="utf-8")
            args = Namespace(
                action="feature-estimate",
                chain="c" * 32,
                scope="api",
                spec=str(spec),
                json=False,
                max_total_tokens=None,
            )
            with mock.patch.object(ar, "repository_feature_call", side_effect=SystemExit(1)) as controller, \
                    self.assertRaises(SystemExit):
                ar.feature_estimate_command(args)

        controller.assert_called_once_with("estimate_command", args)

    def test_shepherd_routes_existing_chain_without_control_action(self):
        report = {
            "disposition": "sufficient",
            "allowance": 30_000_000,
            "used_tokens": 1,
            "remaining_allowance": 29_999_999,
            "recommended_total_tokens": 2,
            "remaining_range": [1, 1],
            "confidence": "medium",
            "phases": [],
        }
        args = Namespace(action="feature-shepherd", chain="c" * 32, json=False)
        with mock.patch.object(ar, "repository_feature_call", return_value=report) as controller, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            ar.feature_shepherd_command(args)

        controller.assert_called_once_with("shepherd_command", args)
        self.assertIn("ARCHON_FEATURE_ESTIMATE=SUFFICIENT", output.getvalue())


if __name__ == "__main__":
    unittest.main()
