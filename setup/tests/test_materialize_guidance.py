#!/usr/bin/env python3
import json
import sys
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parent.parent
ARCHON = SETUP.parent
sys.path.insert(0, str(SETUP))
import materialize_guidance as mg  # noqa: E402

GOLDEN = (
    'bun run test "',
    "SERIAL-ONLY",
    "pnpm ONLY",
    "selective-patch",
    "use aws cli to diagnose",
    "expired SSO degrades",
    "GitNexus",
    "api/libs/data-access/src/lib/rds/migrations/",
    "husky pre-push",
    "NODE_OPTIONS=--experimental-vm-modules",
)


class MaterializeGuidanceTest(unittest.TestCase):
    def test_goodword_pack_keeps_load_bearing_rules(self):
        with tempfile.TemporaryDirectory() as td:
            artifacts = Path(td)
            profile = json.loads((ARCHON / "profiles/goodword/project.v1.json").read_text())
            out = mg.materialize(profile, ARCHON, artifacts)
            text = (artifacts / "project-guidance.md").read_text(encoding="utf-8")
            for needle in GOLDEN:
                with self.subTest(needle=needle):
                    self.assertIn(needle, text)
            self.assertTrue((artifacts / "project-guidance.digest.txt").is_file())
            self.assertEqual(out["profileId"], "project:goodword")
            self.assertNotIn("/Users/", text)
            self.assertNotIn("Documents/Workspace/Goodword", text)

    def test_missing_guidance_root_fails_closed(self):
        profile = {
            "projectId": "project:x",
            "guidance": {"root": "profiles/does-not-exist", "playbook": "guidance.md", "conventions": []},
        }
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(mg.GuidanceError) as ctx:
                mg.materialize(profile, ARCHON, Path(td))
        self.assertIn("GUIDANCE=FAIL", str(ctx.exception))

    def test_declared_playbook_missing_fails_closed(self):
        profile = json.loads((ARCHON / "profiles/goodword/project.v1.json").read_text())
        profile = json.loads(json.dumps(profile))
        profile["guidance"]["playbook"] = "missing.md"
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(mg.GuidanceError) as ctx:
                mg.materialize(profile, ARCHON, Path(td))
        self.assertIn("GUIDANCE=FAIL", str(ctx.exception))

    def test_fluxkeep_omits_aws_and_generated_api(self):
        with tempfile.TemporaryDirectory() as td:
            artifacts = Path(td)
            profile = json.loads((ARCHON / "profiles/fluxkeep-next.v1.json").read_text())
            mg.materialize(profile, ARCHON, artifacts)
            text = (artifacts / "project-guidance.md").read_text(encoding="utf-8")
            self.assertIn("pnpm", text)
            self.assertNotIn("use aws cli to diagnose", text)
            self.assertNotIn("OpenAPI client regen", text)
            self.assertNotIn("selective-patch", text)
            self.assertNotIn("/Users/", text)

    def test_profile_without_guidance_writes_an_explicit_empty_packet(self):
        profile = {"projectId": "project:bare"}
        with tempfile.TemporaryDirectory() as td:
            artifacts = Path(td)
            out = mg.materialize(profile, ARCHON, artifacts)
            text = (artifacts / "project-guidance.md").read_text(encoding="utf-8")
            self.assertIn("No project guidance declared", text)
            self.assertEqual(out["status"], "EMPTY")


if __name__ == "__main__":
    unittest.main()
