#!/usr/bin/env python3
import unittest
import subprocess
import sys
import tempfile
from pathlib import Path

ARCHON = Path(__file__).resolve().parents[2]
WORKFLOW = ARCHON / "workflows/backfill.yaml"


def node_section(text: str, node_id: str) -> str:
    marker = f"  - id: {node_id}\n"
    start = text.index(marker)
    next_start = text.find("\n  - id:", start + len(marker))
    if next_start == -1:
        return text[start:]
    return text[start:next_start]


class BackfillProductionDisabledTest(unittest.TestCase):
    def test_workflow_contains_no_legacy_write_or_credential_acquisition_path(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        for forbidden in ("apply_command", "apply_sql", "secretsmanager", "aws sts", "armed-exec.sh"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)
        # Stronger than the controller_action shape this replaced: the write
        # nodes are absent outright. They were controller nodes, and stock
        # Archon has no controller_action node kind, so they could only have
        # come back as bash -- which is the legacy armed apply path this lane
        # was disabled to prevent.
        for node in ("snapshot", "apply", "reconcile", "intake", "intake-gate", "render-gate"):
            with self.subTest(node=node):
                self.assertNotIn(f"  - id: {node}\n", text)
        self.assertNotIn("controller_action", text)

    def test_resume_cannot_skip_disabled_preflight_or_fetch_credentials(self):
        section = node_section(WORKFLOW.read_text(), "preflight")
        self.assertIn("always_run: true", section)
        self.assertIn("BACKFILL_PRODUCTION_ADMISSION=DISABLED", section)
        self.assertIn("exit 1", section)
        self.assertNotIn("aws ", section)

    def test_preflight_is_the_only_node_so_nothing_can_run_after_it(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual([m for m in text.split("\n") if m.startswith("  - id: ")],
                         ["  - id: preflight"])
        self.assertNotIn("aws ", text)

    def test_direct_apply_cli_cannot_bypass_disabled_workflow(self):
        with tempfile.TemporaryDirectory() as td:
            result = subprocess.run([sys.executable, str(ARCHON / "setup/backfill-transactional-executor.py"),
                                     "apply-chunk", str(Path(td) / "missing-proposal"), td],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertIn("BACKFILL_PRODUCTION_ADMISSION=DISABLED", result.stdout)
            self.assertEqual(list(Path(td).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
