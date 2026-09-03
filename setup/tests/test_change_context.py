#!/usr/bin/env python3
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parent.parent
SCRIPT = SETUP / "change-context.py"
ARCHON = SETUP.parent


class ChangeContextTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(); self.addCleanup(self.td.cleanup)
        self.ad = Path(self.td.name); (self.ad / "evidence").mkdir()
        (self.ad / "bug-report-normalized.md").write_text(
            "# ENG-3814 People search fails for first name plus company\nExpected company search to find connection\n"
        )
        rows = [
            {"number": 2262, "title": "Make named-affiliation search exhaustive and deterministic",
             "headRefName": "feat/exact-third-party-affiliation", "mergedAt": "2026-09-01T00:00:00Z",
             "url": "https://example/2262", "files": [{"path": "libs/search-v4.service.ts"}]},
            {"number": 10, "title": "Unrelated billing cleanup", "headRefName": "chore/billing",
             "mergedAt": "2026-09-01T00:00:00Z", "files": [{"path": "billing.ts"}]},
        ]
        (self.ad / "evidence/merged-prs-api.json").write_text(json.dumps(rows))
        for name in ("open-prs-api.json", "open-prs-web-app.json", "merged-prs-web-app.json"):
            (self.ad / "evidence" / name).write_text("[]")

    def run_cli(self, action):
        return subprocess.run(["python3", str(SCRIPT), action, "--artifacts", str(self.ad)],
                              capture_output=True, text=True)

    def test_build_surfaces_recent_related_prs(self):
        result = self.run_cli("build"); self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        context = json.loads((self.ad / "change-context.json").read_text())
        self.assertEqual([row["id"] for row in context["candidates"]], ["api#2262"])

    def test_validate_requires_disposition_for_every_candidate(self):
        self.run_cli("build")
        (self.ad / "change-context-assessment.json").write_text(json.dumps({"assessments": []}))
        result = self.run_cli("validate"); self.assertNotEqual(result.returncode, 0)
        (self.ad / "change-context-assessment.json").write_text(json.dumps({"assessments": [
            {"id": "api#2262", "decision": "partial", "evidence": "current code still drops nameTokens", "reason": "affiliation only"}
        ]}))
        result = self.run_cli("validate"); self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_workflows_require_cli_fallback_and_change_assessment_without_new_nodes(self):
        import yaml
        for name in ("bugfix", "bugfix-codex", "bugfix-lite", "bugfix-lite-codex"):
            doc = yaml.safe_load((ARCHON / "workflows" / f"{name}.yaml").read_text())
            nodes = {node["id"]: node for node in doc["nodes"]}
            self.assertNotIn("change-context", nodes)
            self.assertIn("change-context.py", nodes["evidence-cheap"]["bash"])
            self.assertIn("change-context-assessment.json", nodes["rca"]["prompt"])
            self.assertIn("change-context.py", nodes["rca-gate"]["bash"])
            self.assertIn("gitnexus-evidence.py", nodes["evidence-gitnexus"]["prompt"])


if __name__ == "__main__": unittest.main()
