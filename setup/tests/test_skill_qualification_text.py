#!/usr/bin/env python3
"""Repository-list qualification text and the discriminator-count norm.

Two independent claims, both fixed by B9:

  1. `skills/archon-sdlc/SKILL.md` no longer reports ENG-3866 qualification as
     pending — the two-repository chain reached `locally_verified` and the
     receipt exists.
  2. RUNBOOK.md section 3 gained a fifth discriminator (`CROSS_REPO_FINDING`),
     so every file naming the count must say "five", and no tracked file
     (outside `dist/`, which is generated) may still say "four".

The roast-remediation operator contract (VERSION 2026.09.18-1) is a third
claim: skills, RUNBOOK, and `docs/operator-recovery.md` must name the same
typed stops and the same writer (`feature-scope-amend`), and must not tell
an agent to edit `files-allowlist.json`.
"""
import subprocess
import unittest
from pathlib import Path

ARCHON = Path(__file__).resolve().parents[2]
SKILL = ARCHON / "skills/archon-sdlc/SKILL.md"
LINEAR = ARCHON / "skills/archon-linear/SKILL.md"
INSTALL = ARCHON / "skills/archon-install/SKILL.md"
RUNBOOK = ARCHON / "RUNBOOK.md"
RECOVERY = ARCHON / "docs/operator-recovery.md"
PACKAGE = ARCHON / "setup/package.sh"

OPERATOR_DOCS = (SKILL, LINEAR, INSTALL, RUNBOOK, RECOVERY)
RECOVERY_DISCRIMINATORS = (
    "ALLOWLIST_DRIFT",
    "COMMIT_SCOPE=QUARANTINED",
    "STAGE_FAILED",
    "CHAIN_BUDGET=EXCEEDED",
    "class=infrastructure",
    "--base",
)


class QualificationTextContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skill_text = SKILL.read_text(encoding="utf-8")
        cls.linear_text = LINEAR.read_text(encoding="utf-8")
        cls.install_text = INSTALL.read_text(encoding="utf-8")
        cls.runbook_text = RUNBOOK.read_text(encoding="utf-8")
        cls.recovery_text = RECOVERY.read_text(encoding="utf-8")
        cls.package_text = PACKAGE.read_text(encoding="utf-8")
        cls.gist_readme = (ARCHON / "setup/gist-README.md").read_text(encoding="utf-8")

    def test_qualification_no_longer_reported_pending(self):
        for path in OPERATOR_DOCS:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(
                "remains deferred", text,
                f"{path.relative_to(ARCHON)} still reports qualification pending",
            )

    def test_cross_repo_finding_present_in_runbook_and_skill(self):
        self.assertIn("CROSS_REPO_FINDING", self.runbook_text)
        self.assertIn("CROSS_REPO_FINDING", self.skill_text)

    def test_runbook_section_says_five_not_four(self):
        self.assertIn("the five discriminators", self.runbook_text)
        self.assertIn("exactly one of five ways", self.runbook_text)
        self.assertNotIn("the four " + "discriminators", self.runbook_text)
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
            if "the four " + "discriminators" in text:
                offenders.append(rel)
        self.assertEqual(offenders, [])

    def test_operator_recovery_is_packaged_and_cited(self):
        self.assertTrue(RECOVERY.is_file(), "docs/operator-recovery.md missing")
        self.assertIn("docs/operator-recovery.md", self.package_text)
        for text, name in (
            (self.skill_text, "archon-sdlc"),
            (self.linear_text, "archon-linear"),
            (self.install_text, "archon-install"),
            (self.runbook_text, "RUNBOOK"),
            (self.gist_readme, "gist-README"),
        ):
            self.assertIn(
                "operator-recovery.md", text,
                f"{name} does not cite docs/operator-recovery.md",
            )

    def test_recovery_discriminators_named_in_operator_docs(self):
        for token in RECOVERY_DISCRIMINATORS:
            self.assertIn(token, self.skill_text, f"archon-sdlc missing {token}")
            self.assertIn(token, self.runbook_text, f"RUNBOOK missing {token}")
            self.assertIn(token, self.recovery_text, f"operator-recovery missing {token}")
        self.assertIn("--base", self.linear_text)
        self.assertIn("--scope goodword-mcp", self.linear_text)
        self.assertIn("--base", self.install_text)

    def test_allowlist_json_is_not_an_approval(self):
        for text, name in (
            (self.skill_text, "archon-sdlc"),
            (self.recovery_text, "operator-recovery"),
        ):
            self.assertIn("never edit `files-allowlist.json`", text, name)
            self.assertIn("feature-scope-amend", text, name)
        self.assertIn("ALLOWLIST_DRIFT=FAIL", self.runbook_text)
        self.assertIn("feature-scope-amend", self.runbook_text)
        self.assertIn("untracked", self.recovery_text)
        self.assertIn("strays/", self.recovery_text)
        self.assertIn("untracked", self.skill_text)
        self.assertIn("strays/", self.skill_text)

    def test_claude_active_budget_stops_dispatch(self):
        for text, name in (
            (self.skill_text, "archon-sdlc"),
            (self.linear_text, "archon-linear"),
            (self.runbook_text, "RUNBOOK"),
            (self.recovery_text, "operator-recovery"),
        ):
            self.assertIn("CHAIN_BUDGET=EXCEEDED active=", text, name)
            self.assertIn("locally_verified", text, name)

    def test_agents_never_write_human_approval_files(self):
        for text, name in (
            (self.skill_text, "archon-sdlc"),
            (self.recovery_text, "operator-recovery"),
        ):
            self.assertIn("accept-residuals.txt", text, name)
            self.assertIn("never write", text.lower(), name)


if __name__ == "__main__":
    unittest.main()
