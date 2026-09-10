#!/usr/bin/env python3
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SETUP = Path(__file__).resolve().parent.parent
SCRIPT = SETUP / "armed-exec.sh"


class ArmedExecCliProgressTest(unittest.TestCase):
    def write_fixture(self, root: Path, command: str, max_rows=2) -> None:
        (root / "backfill-plan.json").write_text(
            json.dumps({"instrument": {"kind": "cli", "progress_regex": r"TOUCHED=([0-9]+)"}}),
            encoding="utf-8",
        )
        (root / "bounds.json").write_text(
            json.dumps({
                "max_rows": max_rows,
                "chunk_size": 1,
                "stall_seconds": 5,
                "statement_timeout_ms": 1000,
            }),
            encoding="utf-8",
        )
        (root / "armed-command.txt").write_text(command, encoding="utf-8")
        digest = hashlib.sha256(command.encode()).hexdigest()
        (root / "armed-command.sha256").write_text(digest, encoding="utf-8")

    def run_fixture(self, command: str, max_rows=2):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.write_fixture(root, command, max_rows=max_rows)
            result = subprocess.run(
                ["bash", str(SCRIPT), str(root), "apply"],
                capture_output=True,
                text=True,
                env={**os.environ, "HOME": str(root / "home"), "TMPDIR": str(root / "tmp")},
            )
            payload = (root / "apply-result.json").exists()
            return result, payload

    def test_unterminated_legacy_progress_cannot_authorize_execution(self):
        result, wrote_accounting = self.run_fixture("printf 'TOUCHED=3'", max_rows=2)
        self.assertEqual(result.returncode, 10, result.stdout + result.stderr)
        self.assertIn("LEGACY_BACKFILL_EXECUTION=DISABLED", result.stdout)
        self.assertFalse(wrote_accounting)

    def test_legacy_apply_is_disabled_even_with_matching_command_hash(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            marker = root / "executed"
            self.write_fixture(root, f"touch '{marker}'; printf 'TOUCHED=3'")
            result = subprocess.run(["bash", str(SCRIPT), str(root), "apply"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 10)
            self.assertIn("LEGACY_BACKFILL_EXECUTION=DISABLED", result.stdout)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
