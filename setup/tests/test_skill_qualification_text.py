#!/usr/bin/env python3
"""Repository-list qualification text and the discriminator-count norm.

Two independent claims, both fixed by B9:

  1. `skills/archon-sdlc/SKILL.md` no longer reports ENG-3866 qualification as
     pending — the two-repository chain reached `locally_verified` and the
     receipt exists.
  2. RUNBOOK.md section 3 gained a fifth discriminator (`CROSS_REPO_FINDING`),
     so every file naming the count must say "five", and no tracked file
     (outside `dist/`, which is generated) may still say "four".
"""
import subprocess
import unittest
from pathlib import Path

ARCHON = Path(__file__).resolve().parents[2]
SKILL = ARCHON / "skills/archon-sdlc/SKILL.md"
RUNBOOK = ARCHON / "RUNBOOK.md"


class QualificationTextContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skill_text = SKILL.read_text(encoding="utf-8")
        cls.runbook_text = RUNBOOK.read_text(encoding="utf-8")

    def test_qualification_no_longer_reported_pending(self):
        self.assertNotIn("remains deferred", self.skill_text)

    def test_cross_repo_finding_present_in_runbook_and_skill(self):
        self.assertIn("CROSS_REPO_FINDING", self.runbook_text)
        self.assertIn("CROSS_REPO_FINDING", self.skill_text)

    def test_runbook_section_says_five_not_four(self):
        self.assertIn("the five discriminators", self.runbook_text)
        self.assertIn("exactly one of five ways", self.runbook_text)
        self.assertNotIn("the four discriminators", self.runbook_text)
        self.assertNotIn("exactly one of four ways", self.runbook_text)

    def test_no_tracked_file_still_says_the_four_discriminators(self):
        files = subprocess.run(
            ["/usr/bin/git", "ls-files"], cwd=ARCHON,
            capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        offenders = []
        for rel in files:
            if rel.startswith("dist/"):
                continue
            path = ARCHON / rel
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if "the four discriminators" in text:
                offenders.append(rel)
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
