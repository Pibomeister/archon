#!/usr/bin/env python3
import contextlib
import importlib.util
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "gitnexus-evidence.py"
spec = importlib.util.spec_from_file_location("gitnexus_evidence", SCRIPT)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class GitNexusEvidenceTest(unittest.TestCase):
    def test_query_terms_and_context_fanout_are_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            ad = Path(td)
            (ad / "bug-report-normalized.md").write_text(
                " ".join(f"distinctterm{i}" for i in range(40)), encoding="utf-8"
            )
            self.assertLessEqual(len(module.query_text(ad).split()), module.MAX_QUERY_TERMS)
        self.assertEqual(module.MAX_CONTEXTS, 3)

    def test_gitnexus_command_timeout_fails_typed(self):
        output = io.StringIO()
        with mock.patch.object(
            module.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["node", "run.cjs", "query"], module.COMMAND_TIMEOUT_SECONDS),
        ), contextlib.redirect_stdout(output), self.assertRaises(SystemExit):
            module.run_json(["node", "run.cjs", "query"])
        self.assertIn("GITNEXUS_CLI=FAIL GitNexus command timed out", output.getvalue())


if __name__ == "__main__":
    unittest.main()
