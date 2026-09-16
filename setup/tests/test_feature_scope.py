import contextlib
import importlib.util
import io
from pathlib import Path
import unittest
import tempfile
from argparse import Namespace
from unittest import mock


SETUP = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("scope_launcher", SETUP / "archon-run.py")
ar = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ar)


class FeatureScope(unittest.TestCase):
    def parse(self, scope, provider="codex", repo=None):
        return ar.parse_feature_scope(scope, provider, repo)

    def test_lists_keep_presentation_order_and_canonicalize_web(self):
        self.assertEqual(self.parse("goodword-mcp,api,web"),
                         ("repositories", ["goodword-mcp", "api", "web-app"]))
        self.assertEqual(self.parse("fullstack"), ("repositories", ["api", "web-app"]))
        self.assertEqual(self.parse("goodword-mcp"), ("repositories", ["goodword-mcp"]))

    def test_legacy_scalar_commands_remain_supported(self):
        self.assertEqual(self.parse("api"), ("api", ["api"]))
        self.assertEqual(self.parse("web"), ("web", ["web-app"]))
        self.assertEqual(self.parse("fullstack", "claude"),
                         ("fullstack", ["api", "web-app"]))

    def test_legacy_environment_selection_warns(self):
        with contextlib.redirect_stderr(io.StringIO()) as warnings:
            self.assertEqual(self.parse("api", repo="goodword-mcp"),
                             ("api", ["goodword-mcp"]))
        self.assertIn("deprecated", warnings.getvalue())

    def test_invalid_scope_fails_before_dispatch(self):
        for value in ("", "api,", ",api", "api,,web", "api,api", "web,web-app",
                      "fullstack,api", "../api", "/api", "api/", "API", "unknown"):
            with self.subTest(value=value), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                self.parse(value)

    def test_explicit_scope_cannot_be_narrowed_by_environment(self):
        for value, repo in (("api,goodword-mcp", "api"), ("fullstack", "api"),
                            ("goodword-mcp", "api"), ("web", "api")):
            with self.subTest(value=value), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                self.parse(value, repo=repo)
        self.assertEqual(self.parse("goodword-mcp", repo="goodword-mcp"),
                         ("repositories", ["goodword-mcp"]))

    def test_claude_lists_use_repository_mode(self):
        self.assertEqual(self.parse("api,goodword-mcp", "claude"),
                         ("repositories", ["api", "goodword-mcp"]))
        self.assertEqual(self.parse("goodword-mcp", "claude"),
                         ("repositories", ["goodword-mcp"]))

    def test_parser_accepts_repository_list(self):
        args = ar.parser().parse_args(["feature", "--provider", "codex", "--scope",
                                      "api,goodword-mcp", "/tmp/spec.md"])
        self.assertEqual(args.scope, "api,goodword-mcp")

    def test_new_codex_fullstack_uses_joint_controller(self):
        with tempfile.TemporaryDirectory() as directory:
            spec = Path(directory) / "spec.md"
            spec.write_text("A joint feature")
            args = Namespace(spec=str(spec), provider="codex", scope="fullstack")
            with mock.patch.dict(ar.os.environ, {}, clear=True), \
                    mock.patch.object(ar, "repository_feature_call") as joint, \
                    mock.patch.object(ar, "adaptive_legacy_feature") as legacy:
                ar.adaptive_feature(args)
            joint.assert_called_once_with("launch", args, ["api", "web-app"])
            legacy.assert_not_called()

    def test_claude_repository_list_uses_joint_controller_and_reports_pause(self):
        with tempfile.TemporaryDirectory() as directory:
            spec = Path(directory) / "spec.md"
            spec.write_text("A joint feature")
            args = Namespace(spec=str(spec), provider="claude", scope="api,goodword-mcp")
            launched = {"state": {"logical_chain_id": "c" * 32}, "row": {}, "result": None}
            with mock.patch.dict(ar.os.environ, {}, clear=True), \
                    mock.patch.object(ar, "repository_feature_call", return_value=launched) as joint, \
                    mock.patch.object(ar, "print_feature_chain_pause") as pause, \
                    mock.patch.object(ar, "adaptive_legacy_feature") as legacy:
                ar.adaptive_feature(args)
            joint.assert_called_once_with("launch", args, ["api", "goodword-mcp"])
            pause.assert_called_once_with(args, "c" * 32)
            legacy.assert_not_called()

    def pause_line(self, probe_state, status):
        args = Namespace(control_dir=Path("/c"), db=Path("/db"))
        state = {"status": "running", "current_run": {"run_id": "r" * 32, "phase": "implement"}}
        controller = mock.Mock(read_state=mock.Mock(return_value=state))
        out = io.StringIO()
        with mock.patch.object(ar, "feature_repository_controller", return_value=controller), \
                mock.patch.object(ar, "run_row_by_id", return_value={"id": "r" * 32, "workflow_name": "full-sdlc-api"}), \
                mock.patch.object(ar, "supervise_exact_run", return_value={"state": probe_state, "status": status}), \
                contextlib.redirect_stdout(out):
            ar.print_feature_chain_pause(args, "c" * 32)
        return out.getvalue()

    def test_pause_line_says_approve_only_at_a_gate(self):
        self.assertIn('next="archon workflow approve rrrrrrrr"', self.pause_line("gate", "paused"))
        failed = self.pause_line("terminal", "failed")
        self.assertNotIn("approve", failed)
        self.assertIn("resume.sh rrrrrrrr", failed)

    def test_replan_parser_accepts_chain_without_token(self):
        args = ar.parser().parse_args(["feature-replan", "abc12345", "--chain", "c" * 32])
        self.assertEqual((args.chain, args.token), ("c" * 32, None))

    def test_parser_accepts_feature_advance(self):
        args = ar.parser().parse_args(["feature-advance", "--chain", "c" * 32])
        self.assertEqual(args.action, "feature-advance")
        self.assertEqual(args.chain, "c" * 32)

    def test_parser_accepts_feature_reopen(self):
        args = ar.parser().parse_args(["feature-reopen", "--chain", "c" * 32, "--repo", "api", "--reason", "why"])
        self.assertEqual((args.action, args.repo, args.reason), ("feature-reopen", "api", "why"))

    def test_parser_accepts_feature_reopen_verify_only(self):
        base = ["feature-reopen", "--chain", "c" * 32, "--repo", "api", "--reason", "why"]
        self.assertTrue(ar.parser().parse_args([*base, "--verify-only"]).verify_only)
        self.assertFalse(ar.parser().parse_args(base).verify_only)

    def test_parser_accepts_feature_publish(self):
        args = ar.parser().parse_args(["feature-publish", "--chain", "c" * 32])
        self.assertEqual(args.action, "feature-publish")
        self.assertEqual(args.chain, "c" * 32)


if __name__ == "__main__":
    unittest.main()
