#!/usr/bin/env python3
import importlib.util
import os
import sys
import tempfile
import unittest
import yaml
from pathlib import Path
from unittest.mock import patch

SETUP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SETUP))
spec = importlib.util.spec_from_file_location("hardened_launcher", SETUP / "archon-run.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class HardenedAdmissionTest(unittest.TestCase):
    def test_all_shipped_authoritative_workflows_require_container_isolation(self):
        for name in ("babysit", "bugfix", "bugfix-smoke-deployed", "cleanup", "full-sdlc-api", "full-sdlc-web", "register-probe"):
            doc = yaml.safe_load((SETUP.parent / "workflows" / f"{name}.yaml").read_text())
            with self.subTest(workflow=name):
                self.assertIs(doc.get("hardened", {}).get("required"), True)

    def test_environment_bypass_cannot_skip_missing_tools(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"CODEX_LITE_SKIP_ENV_CHECKS": "1"}), patch.object(launcher.shutil, "which", return_value=None):
            with self.assertRaises(SystemExit):
                launcher.ensure_environment(Path(td), Path(td), Path(td))

    def test_environment_bypass_cannot_relocate_private_authority(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"CODEX_LITE_SKIP_ENV_CHECKS": "1"}):
            with self.assertRaises(SystemExit):
                launcher.validate_control_location(Path(td) / "agent-chosen-control")

    def test_environment_bypass_cannot_put_private_state_in_workspace(self):
        with tempfile.TemporaryDirectory() as td, patch.object(launcher, "ROOT", Path(td)), patch.dict(os.environ, {"CODEX_LITE_SKIP_ENV_CHECKS": "1"}):
            with self.assertRaises(SystemExit):
                launcher.ensure_control_dir(Path(td) / "control")


if __name__ == "__main__":
    unittest.main()
