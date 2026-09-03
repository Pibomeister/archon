#!/usr/bin/env python3
import unittest
from pathlib import Path

ARCHON = Path(__file__).resolve().parents[2]
SKILL = ARCHON / "skills/archon-linear/SKILL.md"


class ArchonLinearSkillContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SKILL.read_text(encoding="utf-8")

    def test_uses_only_supported_authenticated_mcp_namespaces(self):
        self.assertIn("mcp__linear__*", self.text)
        self.assertIn("mcp__linear-server__*", self.text)
        self.assertIn("extract_images", self.text)
        self.assertIn("Page until **all** comments", self.text)
        self.assertIn("oldest-first", self.text)
        self.assertIn("Do not use `curl`", self.text)

    def test_snapshot_is_immutable_and_evidence_complete(self):
        for token in ("canonical URL", "UUID", "UTC fetched timestamp", "verbatim description",
                      "## Intake gaps", "snapshot-collision"):
            self.assertIn(token, self.text)

    def test_authorization_and_provider_routing_are_explicit(self):
        self.assertIn("authorizes exactly one", self.text)
        self.assertIn("Implicit ticket selection", self.text)
        self.assertIn("bugfix --provider <provider>", self.text)
        self.assertIn("bugfix-codex", self.text)
        self.assertIn("ARCHON_LINEAR=UNSUPPORTED", self.text)
        self.assertIn("<WORKFLOW-NODE-STOP>", self.text)

    def test_start_hands_off_to_proactive_supervision(self):
        for token in ("not the end of", "watch the exact run", "Proactively surface",
                      "CHAIN_CONFLICT", "does **not** mean", "same-provider continuation seed"):
            self.assertIn(token, self.text)
        sdlc = (ARCHON / "skills/archon-sdlc/SKILL.md").read_text(encoding="utf-8")
        for token in ("A start creates a supervision obligation", "Do not end the operator",
                      "A disproved cause is not automatically a resolved ticket",
                      "guarded, scope-preserving successor"):
            self.assertIn(token, sdlc)

    def test_uuid_unavailable_is_explicit_not_fabricated(self):
        self.assertIn("<KEY>-uuid-unavailable.md", self.text)
        self.assertIn("never fabricate", self.text)

    def test_package_and_staging_cover_skill(self):
        package = (ARCHON / "setup/package.sh").read_text(encoding="utf-8")
        staging = (ARCHON / "setup/stage-skills.sh").read_text(encoding="utf-8")
        self.assertIn("skills/archon-linear/SKILL.md", package)
        self.assertGreaterEqual(staging.count("archon-linear"), 2)


if __name__ == "__main__":
    unittest.main()
