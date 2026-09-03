#!/usr/bin/env python3
import unittest
from pathlib import Path

import yaml

ARCHON = Path(__file__).resolve().parents[2]


class FullstackContractTest(unittest.TestCase):
    def load_prompt(self, workflow, node_id):
        doc = yaml.safe_load((ARCHON / "workflows" / f"{workflow}.yaml").read_text(encoding="utf-8"))
        for node in doc["nodes"]:
            if node.get("id") == node_id:
                return node.get("prompt", "") + node.get("bash", "")
        raise AssertionError(f"missing node {node_id}")

    def test_api_plan_gate_is_single_cross_repo_approval(self):
        prompt = self.load_prompt("full-sdlc-api", "ralplan")
        self.assertIn("ONE SHARED CROSS-REPOSITORY implementation plan", prompt)
        self.assertIn("single approval for the full design", prompt)
        self.assertIn("web-files-allowlist.json", prompt)
        self.assertNotIn("API PRONG ONLY", prompt)
        self.assertNotIn("Ignore any web-app prongs", prompt)

    def test_web_implementation_uses_approved_handoff_allowlist(self):
        prompt = self.load_prompt("full-sdlc-web", "implement")
        self.assertIn("copied", prompt)
        self.assertIn("API lane's approved web-files-allowlist.json", prompt)
        self.assertIn("Do not edit or broaden that allowlist", prompt)
        self.assertNotIn("write files-allowlist.json", prompt.lower())


if __name__ == "__main__":
    unittest.main()
