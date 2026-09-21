#!/usr/bin/env python3
"""Admission for generic graphs: new helpers and project packs carry no host
paths. Production YAML is scanned here so a regression is a named failure
once the graphs themselves are parameterized; until then the scan is the
inventory the parameterization must drive to empty."""
import re
import unittest
from pathlib import Path

ARCHON = Path(__file__).resolve().parent.parent.parent
HOST = re.compile(r"/Users/|Documents/Workspace/Goodword")

HELPERS = (
    "setup/layer_root.py",
    "setup/materialize_guidance.py",
    "setup/profile_bind.py",
    "setup/profile-preflight.sh",
    "setup/graph_export.py",
    "setup/graph_render.py",
)

PACKS = (
    "profiles/goodword",
    "profiles/fluxkeep",
)


def hits(path: Path):
    text = path.read_text(encoding="utf-8", errors="replace")
    if not HOST.search(text):
        return []
    return [f"{path.relative_to(ARCHON)}:{i}" for i, line in enumerate(text.splitlines(), 1) if HOST.search(line)]


class GenericGraphAdmissionTest(unittest.TestCase):
    def test_new_helpers_have_no_host_paths(self):
        found = []
        for rel in HELPERS:
            found.extend(hits(ARCHON / rel))
        self.assertEqual(found, [])

    def test_project_packs_have_no_host_paths(self):
        found = []
        for rel in PACKS:
            root = ARCHON / rel
            self.assertTrue(root.is_dir(), rel)
            for path in root.rglob("*"):
                if path.is_file():
                    found.extend(hits(path))
        self.assertEqual(found, [])

    def test_goodword_pack_files_exist(self):
        root = ARCHON / "profiles/goodword"
        for name in (
            "project.v1.json",
            "envelope.json",
            "guidance.md",
            "conventions/api.md",
            "conventions/web-app.md",
            "conventions/mcp.md",
            "repos.json",
        ):
            self.assertTrue((root / name).is_file(), name)

    def test_production_graphs_have_no_host_paths(self):
        found = []
        for name in (
            "full-sdlc-api.yaml",
            "full-sdlc-api-lite.yaml",
            "full-sdlc-api-codex.yaml",
            "full-sdlc-api-lite-codex.yaml",
            "full-sdlc-api-grok.yaml",
            "full-sdlc-api-lite-grok.yaml",
            "bugfix.yaml",
            "bugfix-lite.yaml",
            "bugfix-codex.yaml",
            "bugfix-lite-codex.yaml",
            "bugfix-grok.yaml",
            "bugfix-lite-grok.yaml",
            "full-sdlc-web.yaml",
            "full-sdlc-web-codex.yaml",
            "full-sdlc-web-grok.yaml",
            "skill-evolve.yaml",
        ):
            found.extend(hits(ARCHON / "workflows" / name))
        self.assertEqual(found, [])

    def test_lite_overlays_have_no_host_paths(self):
        found = []
        for path in (ARCHON / "setup/lite").rglob("*"):
            if path.is_file():
                found.extend(hits(path))
        self.assertEqual(found, [])

    def test_runbook_has_no_host_paths(self):
        self.assertEqual(hits(ARCHON / "RUNBOOK.md"), [])

    def test_project_packs_are_in_the_package_manifest(self):
        manifest = (ARCHON / "setup/package.sh").read_text(encoding="utf-8")
        for rel in (
            "profiles/goodword/project.v1.json",
            "profiles/goodword/envelope.json",
            "profiles/goodword/guidance.md",
            "profiles/goodword/conventions/api.md",
            "profiles/goodword/conventions/web-app.md",
            "profiles/goodword/conventions/mcp.md",
            "profiles/fluxkeep/guidance.md",
            "setup/layer_root.py",
            "setup/materialize_guidance.py",
            "setup/graph_export.py",
            "setup/graph_render.py",
            "setup/profile_bind.py",
            "setup/profile-preflight.sh",
            "setup/derive-grok.py",
            "profiles/goodword/repos.json",
            "profiles/fluxkeep/repos.json",
            "profiles/fluxkeep/envelope.json",
        ):
            self.assertIn(rel, manifest, rel)

    def test_api_preflight_does_not_hardcode_goodword_stack(self):
        text = (ARCHON / "workflows/full-sdlc-api.yaml").read_text(encoding="utf-8")
        start = text.find("  - id: preflight")
        end = text.find("  - id: feature-phase")
        preflight = text[start:end]
        self.assertIn("profile-preflight.sh", preflight)
        self.assertNotIn("command -v bun", preflight)
        self.assertNotIn("goodword-kb", preflight)
        self.assertNotIn("command -v aws", preflight)
        helper = (ARCHON / "setup/profile-preflight.sh").read_text(encoding="utf-8")
        self.assertNotIn("command -v bun", helper)
        self.assertNotIn("goodword-kb", helper)

    def test_api_prompts_defer_stack_commands_to_guidance(self):
        text = (ARCHON / "workflows/full-sdlc-api.yaml").read_text(encoding="utf-8")
        self.assertNotIn('<repo-conventions repo="api">', text)
        self.assertIn("project-guidance.md", text)

    def test_bugfix_and_web_preflight_use_the_pack(self):
        for name, next_id in (("bugfix.yaml", "capability-gate"), ("full-sdlc-web.yaml", "bootstrap")):
            text = (ARCHON / "workflows" / name).read_text(encoding="utf-8")
            start = text.find("  - id: preflight")
            end = text.find(f"  - id: {next_id}")
            preflight = text[start:end]
            with self.subTest(lane=name):
                self.assertIn("profile-preflight.sh", preflight)
                self.assertNotIn("command -v bun", preflight)
                self.assertNotIn("command -v pnpm", preflight)
                self.assertNotIn("goodword-kb", preflight)
                self.assertNotIn("command -v aws", preflight)
        bugfix = (ARCHON / "workflows/bugfix.yaml").read_text(encoding="utf-8")
        web = (ARCHON / "workflows/full-sdlc-web.yaml").read_text(encoding="utf-8")
        self.assertNotIn('<repo-conventions repo="api">', bugfix)
        self.assertNotIn('<repo-conventions repo="web-app">', bugfix)
        self.assertNotIn('<repo-conventions repo="web-app">', web)
        self.assertIn("project-guidance.md", bugfix)
        self.assertIn("project-guidance.md", web)


if __name__ == "__main__":
    unittest.main()
