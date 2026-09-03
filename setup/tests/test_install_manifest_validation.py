#!/usr/bin/env python3
import re
import unittest
from pathlib import Path


SETUP = Path(__file__).resolve().parent.parent


class InstallManifestValidation(unittest.TestCase):
    def test_install_validates_every_packaged_workflow(self):
        package = (SETUP / "package.sh").read_text(encoding="utf-8")
        install = (SETUP / "install.sh").read_text(encoding="utf-8")
        manifest = package.split("MANIFEST=(", 1)[1].split("\n)", 1)[0]
        shipped = {
            Path(path).stem
            for path in re.findall(r"^\s*(workflows/[A-Za-z0-9-]+\.yaml)\s*$", manifest, re.M)
        }
        loop = re.search(r"for w in (.+?); do", install, re.S)
        self.assertIsNotNone(loop, "install workflow validation loop missing")
        validated = set(re.findall(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", loop.group(1)))
        self.assertEqual(shipped - validated, set(),
                         f"packaged workflows not validated by install.sh: {sorted(shipped - validated)}")
        for lane in ("full-sdlc-api-lite-codex", "bugfix-lite-codex"):
            self.assertIn(lane, validated)

    def test_gitnexus_setup_is_optional_and_non_blocking(self):
        install = (SETUP / "install.sh").read_text(encoding="utf-8")

        self.assertIn("=== 7. Optional GitNexus evidence acceleration ===", install)
        self.assertIn("GitNexus acceleration unavailable; code workflows continue", install)
        self.assertIn("codex mcp add gitnexus", install)
        self.assertNotIn("MCP/index readiness is a launch prerequisite", install)


if __name__ == "__main__":
    unittest.main()
