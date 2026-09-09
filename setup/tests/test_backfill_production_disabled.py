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
        for node in ("snapshot", "apply", "reconcile"):
            section = node_section(text, node)
            self.assertIn("controller_action: backfill", section)
            self.assertNotIn("bash:", section)
        self.assertIn("approval:", node_section(text, "apply-approval"))

    def test_resume_cannot_skip_disabled_preflight_or_fetch_credentials(self):
        section = node_section(WORKFLOW.read_text(), "preflight")
        self.assertIn("always_run: true", section)
        self.assertIn("BACKFILL_PRODUCTION_ADMISSION=DISABLED", section)
        self.assertIn("exit 1", section)
        self.assertNotIn("aws ", section)

    def test_snapshot_and_apply_fail_closed_before_write_credentials(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        for node_id in ("snapshot", "apply"):
            section = node_section(text, node_id)
            self.assertIn("controller_action: backfill", section)
            self.assertNotIn("bash:", section)
            self.assertNotIn("aws ", section)

    def test_intake_gate_requires_controller_proposal_validation(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        section = node_section(text, "intake-gate")
        self.assertIn("controller_action: backfill", section)
        self.assertIn("phase: proposal-v2-validation", section)

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
