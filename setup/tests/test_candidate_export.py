import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ARCHON = Path(__file__).resolve().parents[2]


class CandidateExportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="candidate export ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repository with spaces"
        self.artifacts = self.root / "artifacts with spaces"
        self.repo.mkdir()
        self.artifacts.mkdir()
        self.env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.com")
        (self.repo / "app.txt").write_text("baseline\n")
        self.git("add", "app.txt")
        self.git("commit", "-qm", "baseline")
        self.baseline = self.git("rev-parse", "HEAD").strip()
        (self.artifacts / "params.json").write_text(json.dumps({"worktree": str(self.repo)}))

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], env=self.env, text=True)

    def export(self):
        return subprocess.run(
            ["python3", str(ARCHON / "setup/export-candidate.py"), "--artifacts", str(self.artifacts)],
            env=self.env, text=True, capture_output=True,
        )

    def test_exports_unchanged_baseline_without_fabricating_commit(self):
        result = self.export()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("authority=none", result.stdout)
        proposal = json.loads((self.artifacts / "candidate.json").read_text())
        self.assertEqual(set(proposal), {"schema", "commit", "tree"})
        self.assertEqual(proposal["schema"], "archon.candidate-proposal.v1")
        self.assertEqual(proposal["commit"], self.baseline)
        self.assertEqual(self.git("rev-list", "--count", "HEAD").strip(), "1")
        self.assertEqual(self.git("bundle", "list-heads", str(self.artifacts / "candidate.bundle")).strip(),
                         self.baseline + " refs/candidates/sealed")

    def test_exports_changed_commit_and_exact_content_to_isolated_clone(self):
        (self.repo / "app.txt").write_text("candidate\n")
        self.git("commit", "-qam", "candidate")
        result = self.export()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        clone = self.root / "imported"
        subprocess.run(["git", "init", "-q", str(clone)], env=self.env, check=True, capture_output=True)
        subprocess.run(["git", "-C", str(clone), "fetch", "-q", "--no-tags", str(self.artifacts / "candidate.bundle"),
                        "refs/candidates/sealed:refs/candidates/sealed"], env=self.env, check=True, capture_output=True)
        proposal = json.loads((self.artifacts / "candidate.json").read_text())
        contents = subprocess.check_output(["git", "-C", str(clone), "show", proposal["commit"] + ":app.txt"],
                                           env=self.env, text=True)
        self.assertEqual(contents, "candidate\n")

    def test_refuses_dirty_or_untracked_input_before_export(self):
        for kind in ("unstaged", "staged", "untracked"):
            with self.subTest(kind=kind):
                if kind == "untracked":
                    (self.repo / "extra.txt").write_text("untracked")
                else:
                    (self.repo / "app.txt").write_text("dirty\n")
                    if kind == "staged":
                        self.git("add", "app.txt")
                result = self.export()
                self.assertEqual(result.returncode, 1)
                self.assertIn("dirty", result.stdout)
                self.assertFalse((self.artifacts / "candidate.bundle").exists())
                self.git("reset", "--hard", "HEAD")

    def test_refuses_existing_output_instead_of_replacing_artifacts(self):
        (self.artifacts / "candidate.json").write_text("preserve this artifact")
        result = self.export()
        self.assertEqual(result.returncode, 1)
        self.assertIn("already exists", result.stdout)
        self.assertEqual((self.artifacts / "candidate.json").read_text(), "preserve this artifact")

    def test_workflow_variants_export_then_import_before_publication(self):
        import yaml
        names = ("full-sdlc-api", "full-sdlc-api-lite", "full-sdlc-api-codex", "full-sdlc-api-lite-codex",
                 "full-sdlc-web", "full-sdlc-web-codex", "bugfix", "bugfix-lite", "bugfix-codex", "bugfix-lite-codex")
        for name in names:
            with self.subTest(workflow=name):
                doc = yaml.safe_load((ARCHON / "workflows" / (name + ".yaml")).read_text())
                nodes = {node["id"]: node for node in doc["nodes"]}
                self.assertEqual(nodes["ship"]["depends_on"], ["candidate-import"])
                self.assertEqual(nodes["candidate-import"]["depends_on"], ["candidate-export"])
                self.assertEqual(nodes["candidate-import"]["controller_action"], "finalize-evidence")
                self.assertEqual(nodes["candidate-import"]["phase"], "candidate-import")
                self.assertIn("export-candidate.py", nodes["candidate-export"]["bash"])
