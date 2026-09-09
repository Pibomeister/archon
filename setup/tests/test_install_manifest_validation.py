#!/usr/bin/env python3
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


SETUP = Path(__file__).resolve().parent.parent


class InstallManifestValidation(unittest.TestCase):
    def test_uncertified_distribution_refuses_install_before_external_commands(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "nondefault root with spaces"
            result = subprocess.run(
                ["/bin/bash", str(SETUP / "install.sh"), "--root", str(root), "-y"],
                env={"HOME": td, "PATH": "/nonexistent"},
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("INSTALL=DISABLED", result.stdout)
            self.assertEqual(list(Path(td).iterdir()), [])

    def test_root_assignments_survive_rendering_to_a_path_with_spaces(self):
        original = str(SETUP.parent.parent)
        replacement = "/tmp/archon root with spaces"
        parents = ("bugfix", "bugfix-smoke-deployed", "babysit", "full-sdlc-api", "full-sdlc-web")
        files = [SETUP.parent / "workflows" / f"{name}.yaml" for name in parents]
        files += list((SETUP / "lite").rglob("*.sh"))
        pattern = re.compile(r'^\s*([A-Z_]+)=(\"?)(' + re.escape(original) + r'([A-Za-z0-9_./-]*))\2\s*$', re.M)
        checked = 0
        for path in files:
            for match in pattern.finditer(path.read_text()):
                variable, quote, _, suffix = match.groups()
                assignment = f"{variable}={quote}{replacement}{suffix}{quote}"
                result = subprocess.run(["/bin/bash", "-c", 'set -eu; ' + assignment + f'; printf %s "${variable}"'],
                                        env={"PATH": "/nonexistent"}, capture_output=True, text=True, timeout=5)
                with self.subTest(path=path.name, variable=variable):
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, replacement + suffix)
                checked += 1
        self.assertGreater(checked, 0)

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
