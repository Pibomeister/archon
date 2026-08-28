#!/usr/bin/env python3
"""Tests for parse-critique.py: schema validation, confidence-gated counting,
and the exact CRITIQUE summary line."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "parse-critique.py"


def finding(kind="scope", severity="P1", confidence=75, section="## Files",
            evidence=None, recommendation="drop it"):
    return {
        "kind": kind,
        "severity": severity,
        "confidence": confidence,
        "section": section,
        "evidence": [{"source": "spec", "quote": "the spec says X"}] if evidence is None else evidence,
        "recommendation": recommendation,
    }


def run(critique, round_no="1"):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump(critique, fh)
        path = fh.name
    try:
        return subprocess.run(
            [sys.executable, str(SCRIPT), path, "--round", str(round_no)],
            capture_output=True, encoding="utf-8",
        )
    finally:
        Path(path).unlink()


class ParseCritiqueTest(unittest.TestCase):
    def test_valid_accept_no_findings(self):
        r = run({"verdict": "ACCEPT", "findings": []}, round_no=1)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "CRITIQUE round=1 verdict=ACCEPT scope=0 regression=0 gap=0")
        self.assertEqual(r.stderr, "")

    def test_counts_only_blocking_confidence_by_kind(self):
        findings = [
            finding(kind="scope", confidence=100),
            finding(kind="scope", confidence=75),
            finding(kind="scope", confidence=50),  # dropped from blocking count
            finding(kind="regression", confidence=75),
            finding(kind="gap", confidence=100),
            finding(kind="gap", confidence=100),
            finding(kind="verifiability", confidence=100),  # not a counted kind
        ]
        r = run({"verdict": "REVISE", "findings": findings}, round_no=2)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "CRITIQUE round=2 verdict=REVISE scope=2 regression=1 gap=2")

    def test_malformed_json_fails(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
            fh.write("{not json")
            path = fh.name
        try:
            r = subprocess.run(
                [sys.executable, str(SCRIPT), path, "--round", "1"],
                capture_output=True, encoding="utf-8",
            )
        finally:
            Path(path).unlink()
        self.assertEqual(r.returncode, 1)
        self.assertTrue(r.stderr.strip().startswith("CRITIC_GATE=FAIL") or r.stdout.strip().startswith("CRITIC_GATE=FAIL"))

    def test_missing_verdict_fails(self):
        r = run({"findings": []})
        self.assertEqual(r.returncode, 1)
        combined = r.stdout + r.stderr
        self.assertIn("CRITIC_GATE=FAIL", combined)

    def test_bad_verdict_enum_fails(self):
        r = run({"verdict": "MAYBE", "findings": []})
        self.assertEqual(r.returncode, 1)
        self.assertIn("CRITIC_GATE=FAIL", r.stdout + r.stderr)

    def test_bad_severity_enum_fails(self):
        r = run({"verdict": "ACCEPT", "findings": [finding(severity="P9")]})
        self.assertEqual(r.returncode, 1)
        self.assertIn("CRITIC_GATE=FAIL", r.stdout + r.stderr)

    def test_bad_confidence_enum_fails(self):
        r = run({"verdict": "ACCEPT", "findings": [finding(confidence=60)]})
        self.assertEqual(r.returncode, 1)
        self.assertIn("CRITIC_GATE=FAIL", r.stdout + r.stderr)

    def test_missing_evidence_fails(self):
        r = run({"verdict": "ACCEPT", "findings": [finding(evidence=[])]})
        self.assertEqual(r.returncode, 1)
        self.assertIn("CRITIC_GATE=FAIL", r.stdout + r.stderr)

    def test_bad_evidence_source_fails(self):
        r = run({
            "verdict": "ACCEPT",
            "findings": [finding(evidence=[{"source": "hunch", "quote": "x"}])],
        })
        self.assertEqual(r.returncode, 1)
        self.assertIn("CRITIC_GATE=FAIL", r.stdout + r.stderr)

    def test_findings_not_a_list_fails(self):
        r = run({"verdict": "ACCEPT", "findings": "none"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("CRITIC_GATE=FAIL", r.stdout + r.stderr)

    def test_missing_file_fails(self):
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "/nonexistent/critique.json", "--round", "1"],
            capture_output=True, encoding="utf-8",
        )
        self.assertEqual(r.returncode, 1)
        self.assertIn("CRITIC_GATE=FAIL", r.stdout + r.stderr)

    def test_reject_verdict_still_counts_blocking_findings(self):
        r = run({"verdict": "REJECT", "findings": [finding(kind="scope", confidence=100)]}, round_no=3)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "CRITIQUE round=3 verdict=REJECT scope=1 regression=0 gap=0")


if __name__ == "__main__":
    unittest.main()
