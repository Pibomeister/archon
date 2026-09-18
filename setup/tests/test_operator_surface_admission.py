#!/usr/bin/env python3
"""Operator skills and RUNBOOK must match fail-closed YAML: install, package
publish, and production backfill are disabled. Stale v0.8.0 install pins and
live backfill-apply recipes are G12."""
import unittest
from pathlib import Path

ARCHON = Path(__file__).resolve().parents[2]
RUNBOOK = ARCHON / "RUNBOOK.md"
SDLC = ARCHON / "skills/archon-sdlc/SKILL.md"
INSTALL_SKILL = ARCHON / "skills/archon-install/SKILL.md"
LINEAR_SKILL = ARCHON / "skills/archon-linear/SKILL.md"
INSTALL_SH = ARCHON / "setup/install.sh"
BACKFILL = ARCHON / "workflows/backfill.yaml"


class OperatorSurfaceAdmission(unittest.TestCase):
    def setUp(self):
        self.runbook = RUNBOOK.read_text(encoding="utf-8")
        self.sdlc = SDLC.read_text(encoding="utf-8")
        self.install_skill = INSTALL_SKILL.read_text(encoding="utf-8")
        self.linear = LINEAR_SKILL.read_text(encoding="utf-8")
        self.install_sh = INSTALL_SH.read_text(encoding="utf-8")
        self.backfill = BACKFILL.read_text(encoding="utf-8")

    def test_install_script_is_disabled(self):
        self.assertIn('echo "INSTALL=DISABLED', self.install_sh)
        self.assertLess(self.install_sh.find("INSTALL=DISABLED"), self.install_sh.find("ARCHON_PIN="))

    def test_install_skill_leads_with_disabled_and_pins_v0_10_1(self):
        self.assertIn("INSTALL=DISABLED", self.install_skill)
        self.assertIn("v0.10.1", self.install_skill)
        self.assertNotIn("VERSION=v0.8.0", self.install_skill)
        self.assertNotIn("Archon CLI v0.8.0", self.install_skill)
        self.assertNotIn("FAIL archon on PATH is not v0.8.0", self.install_skill)

    def test_backfill_workflow_is_a_single_fail_closed_preflight(self):
        self.assertIn("BACKFILL_PRODUCTION_ADMISSION=DISABLED", self.backfill)
        self.assertNotIn("apply-approval", self.backfill)

    def test_runbook_section_13_is_not_a_live_apply_recipe(self):
        idx = self.runbook.find("## 13. The backfill lane")
        self.assertNotEqual(idx, -1)
        section = self.runbook[idx: idx + 800]
        self.assertIn("DISABLED", section)
        self.assertNotIn("archon workflow run backfill", section)

    def test_sdlc_skill_does_not_launch_production_backfill(self):
        idx = self.sdlc.find("## 10. The backfill lane")
        self.assertNotEqual(idx, -1)
        section = self.sdlc[idx: idx + 600]
        self.assertIn("DISABLED", section)
        self.assertNotIn("archon workflow run backfill", section)

    def test_plan_reject_is_not_an_in_run_revision(self):
        self.assertNotIn("full-sdlc-api` plan-gate | revises `plan.md`", self.runbook)
        self.assertIn("yield-stop.txt", self.sdlc)
        self.assertIn("PLAN_REJECTION_RECORDED", self.sdlc)

    def test_runbook_reject_table_matches_yaml(self):
        self.assertIn("records a rejection receipt", self.runbook)
        self.assertNotIn("revises `rca.md` / `fix-plan.json`", self.runbook)

    def test_skills_teach_layer_and_project_roots(self):
        for text in (self.sdlc, self.install_skill, self.linear, self.runbook):
            self.assertIn("$ARCHON_LAYER", text)
            self.assertIn("$PROJECT_ROOT", text)
        self.assertNotIn("machine-specific by construction", self.install_skill)
        self.assertIn("$ARCHON_LAYER/setup/archon-run.py", self.linear)
        self.assertIn("$PROJECT_ROOT/.omc/research/linear/", self.linear)
        self.assertNotIn("<Goodword>/.archon/setup", self.linear)
        self.assertNotIn("$ROOT/.archon/setup", self.install_skill)
        self.assertNotIn("python3 .archon/setup/", self.runbook)
        self.assertNotIn("bash .archon/setup/", self.runbook)
        self.assertNotIn("rendered per machine at install time", self.runbook)
        self.assertNotIn("/Users/eduardopicazo/Documents/Workspace/Goodword", self.runbook)


if __name__ == "__main__":
    unittest.main()
