import json
from pathlib import Path
import unittest
from unittest.mock import patch

from setup.tests import test_portable_project as fixtures

portable = fixtures.portable


class SourceRecipeTest(unittest.TestCase):
    def fixture(self):
        fixture = fixtures.PortableProjectTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def v2(self, fixture, recipe):
        fixture.profile["profileVersion"] = "archon.project-profile.v2"
        fixture.profile["sourceRecipe"] = recipe
        fixture.profile["repository"].pop("packageManager")
        fixture.profile["delivery"]["baseBranch"] = "main"
        fixture.profile["noChangeClosure"] = {"enabled": False, "verifierIds": ["test"]}
        return fixture.profile

    def test_explicit_engine_recipe_does_not_invent_package_manager_metadata(self):
        fixture = self.fixture()
        profile = self.v2(fixture, "archon-engine-bun.v1")
        portable.validate_profile(profile)
        (fixture.worktree / "package.json").write_text(json.dumps({"name": "archon", "version": "0.10.1", "workspaces": ["packages/*"]}))
        (fixture.worktree / "bun.lock").write_text("fixture lock bytes\n")
        portable.source_recipes.check_metadata(profile, fixture.worktree)
        self.assertNotIn("packageManager", json.loads((fixture.worktree / "package.json").read_text()))

    def test_python_workflow_recipe_needs_no_node_manifest(self):
        fixture = self.fixture()
        profile = self.v2(fixture, "goodword-workflows-python.v1")
        portable.validate_profile(profile)
        (fixture.worktree / "package.json").unlink()
        for name in ("setup/package.sh", "setup/repo-policy.py", "setup/parse-review-envelope.py"):
            path = fixture.worktree / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# fixture source\n")
        (fixture.worktree / "workflows").mkdir()
        portable.source_recipes.check_metadata(profile, fixture.worktree)
        (fixture.worktree / "setup/package.sh").unlink()
        with self.assertRaises(ValueError):
            portable.source_recipes.check_metadata(profile, fixture.worktree)

    def test_unknown_recipe_and_malformed_pr_base_fail_closed(self):
        fixture = self.fixture()
        profile = self.v2(fixture, "invented-runtime")
        with self.assertRaisesRegex(ValueError, "recipe"):
            portable.validate_profile(profile)
        profile["sourceRecipe"] = "archon-engine-bun.v1"
        profile["delivery"]["baseBranch"] = "--repo=elsewhere"
        with self.assertRaisesRegex(ValueError, "branch"):
            portable.validate_profile(profile)

    def test_fake_git_pointer_is_not_a_registered_linked_worktree(self):
        fixture = self.fixture()
        fake = fixture.allowed / "fake"
        fake.mkdir()
        (fake / ".git").write_text("gitdir: " + str(fixture.repo / ".git") + "\n")
        (fake / "package.json").write_text((fixture.repo / "package.json").read_text())
        fixture.profile["repository"]["defaultBranch"] = "trunk"
        fixture.profile_path.write_text(json.dumps(fixture.profile))
        fixture.binding.update(repositoryRoot=str(fake), branch="main", profileSha256=portable.file_digest(fixture.profile_path))
        fixture.save_binding()
        original_index = (fixture.repo / ".git/index").read_bytes()
        with self.assertRaisesRegex(ValueError, "registered linked worktree"):
            portable.binding_and_profile(fixture.binding_path)
        self.assertEqual((fixture.repo / ".git/index").read_bytes(), original_index)

    def test_tool_version_mismatch_and_same_version_binary_drift_are_detected(self):
        fixture = self.fixture()
        executable = fixture.root / "bun"
        executable.write_text("#!/bin/sh\necho 1.3.14\n")
        executable.chmod(0o755)
        with patch("source_recipes.shutil.which", return_value=str(executable)):
            first = portable.source_recipes.tool("bun", ["--version"], "1.3.14")
            executable.write_text("#!/bin/sh\n# changed binary bytes\necho 1.3.14\n")
            second = portable.source_recipes.tool("bun", ["--version"], "1.3.14")
            self.assertNotEqual(first["sha256"], second["sha256"])
            with self.assertRaisesRegex(ValueError, "version"):
                portable.source_recipes.tool("bun", ["--version"], "0.0.0")
