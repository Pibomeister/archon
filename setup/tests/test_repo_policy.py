#!/usr/bin/env python3
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "repo-policy.py"


class RepoPolicyTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(); self.addCleanup(self.td.cleanup)
        self.root = Path(self.td.name); self.repo = self.root / "api"; self.ad = self.root / "artifacts"
        (self.repo / ".claude/rules").mkdir(parents=True)
        (self.repo / ".claude/rules/testing.md").write_text(
            "# Reuse Existing Test Files\nPrefer the existing test file.\n\n"
            "**Blocking**: do not add an ultra-specific scenario spec when a spec exists.\n\n"
            "# Unit Test Location (__tests__ folder)\n**Blocking** if a unit spec is outside __tests__.\n\n"
            "# Test File Naming conventions\n**Blocking** if .int.spec, .ai.spec, or .ext.spec naming is wrong.\n\n"
            "# No real timer waits\n**Blocking** if a test awaits setTimeout.\n\n"
            "# No barrel imports; use deep paths\n**Blocking** when a test imports a barrel hub module.\n\n"
            "# Test setup uses repos, never raw SQL dataSource.query\n**Blocking** when setup uses raw SQL.\n"
            "# Collision-retrying profile fixtures\nUse createUniqueTestProfile instead of profileRepo.create.\n"
            "**Blocking** if profile setup bypasses the collision-retrying helper.\n"
        )
        (self.repo / "src/__tests__").mkdir(parents=True)
        (self.repo / "src/foo.ts").write_text("export const foo = 1\n")
        (self.repo / "src/__tests__/foo.spec.ts").write_text("test('foo',()=>{})\n")
        self.ad.mkdir(); (self.ad / "repo.json").write_text('{"repo":"api"}')
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.repo, check=True)
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "baseline"], cwd=self.repo, check=True)
        self.run_cli("snapshot")

    def run_cli(self, action, env=None):
        return subprocess.run(["python3", str(SCRIPT), action, "--root", str(self.root),
                               "--artifacts", str(self.ad)], capture_output=True, text=True, env=env)

    def plan(self, test_file, production="src/foo.ts"):
        (self.ad / "fix-plan.json").write_text(json.dumps({"files": [production, test_file]}))
        (self.ad / "failing-test.json").write_text(json.dumps({"test_file": test_file}))

    def test_new_scenario_spec_is_blocked_when_existing_spec_exists(self):
        self.plan("src/__tests__/foo.repro-eng-1.spec.ts")
        result = self.run_cli("validate-plan")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TEST_PLACEMENT", result.stdout)
        self.assertIn("foo.spec.ts", result.stdout)

    def test_extending_existing_spec_passes(self):
        self.plan("src/__tests__/foo.spec.ts")
        result = self.run_cli("validate-plan")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        placement = json.loads((self.ad / "test-placement.json").read_text())
        self.assertEqual(placement["rows"][0]["decision"], "extend-existing")

    def test_new_test_passes_when_no_existing_candidate_exists(self):
        (self.repo / "src/bar.ts").write_text("export const bar = 1\n")
        self.plan("src/__tests__/bar.spec.ts", "src/bar.ts")
        result = self.run_cli("validate-plan")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_snapshot_carries_rule_provenance(self):
        policy = json.loads((self.ad / "repo-policy.json").read_text())
        rule = policy["repositories"]["api"]["rules"][0]
        self.assertEqual(rule["severity"], "blocking")
        self.assertIn(".claude/rules/testing.md:", rule["source"])
        ids = {rule["id"] for rule in policy["repositories"]["api"]["rules"]}
        self.assertTrue({"unit-test-colocation", "test-file-naming", "no-real-timer-waits",
                         "no-barrel-imports", "no-raw-sql-setup",
                         "unique-profile-fixtures"}.issubset(ids))

    def test_snapshot_uses_controlled_baseline_env_before_chain_receipt_exists(self):
        baseline = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, check=True,
                                  capture_output=True, text=True).stdout.strip()
        (self.ad / "bugfix-chain.json").unlink(missing_ok=True)
        env = os.environ.copy(); env["ARCHON_API_BASELINE"] = baseline
        result = self.run_cli("snapshot", env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        policy = json.loads((self.ad / "repo-policy.json").read_text())
        self.assertEqual(policy["baseline"]["api"], baseline)

    def test_unit_spec_outside_colocated_tests_directory_is_blocked(self):
        self.plan("src/foo.spec.ts")
        result = self.run_cli("validate-plan")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TEST_LOCATION", result.stdout)

    def test_final_diff_catches_unplanned_scenario_file(self):
        self.plan("src/__tests__/foo.spec.ts")
        (self.ad / "params.json").write_text(json.dumps({"worktree": str(self.repo)}))
        base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, check=True,
                              capture_output=True, text=True).stdout.strip()
        (self.ad / "bootstrap-head.txt").write_text(base)
        (self.repo / "src/foo.ts").write_text("export const foo = 2\n")
        (self.repo / "src/__tests__/foo.repro-eng-1.spec.ts").write_text("test('scenario',()=>{})\n")
        result = self.run_cli("validate-diff")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TEST_PLACEMENT_DIFF", result.stdout)

    def test_final_diff_enforces_content_rules(self):
        for marker, expected in (
            ("test('x', async () => { await new Promise(r => setTimeout(r, 5)); })\n", "no-real-timer-waits"),
            ("import { X } from '@lib/business-logic';\ntest('x',()=>X)\n", "no-barrel-imports"),
            ("test('x', async()=>dataSource.query(`INSERT INTO x VALUES (1)`))\n", "no-raw-sql-setup"),
        ):
            with self.subTest(rule=expected):
                subprocess.run(["git", "restore", "."], cwd=self.repo, check=True)
                self.plan("src/__tests__/foo.spec.ts")
                (self.ad / "params.json").write_text(json.dumps({"worktree": str(self.repo)}))
                base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, check=True,
                                      capture_output=True, text=True).stdout.strip()
                (self.ad / "bootstrap-head.txt").write_text(base)
                (self.repo / "src/foo.ts").write_text("export const foo = 2\n")
                (self.repo / "src/__tests__/foo.spec.ts").write_text(marker)
                result = self.run_cli("validate-diff")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stdout)

    def test_preexisting_content_violation_does_not_block_unrelated_test_change(self):
        existing = self.repo / "src/__tests__/foo.spec.ts"
        existing.write_text(
            "test('legacy', async () => { await new Promise(r => setTimeout(r, 5)); })\n"
        )
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "legacy timer debt"], cwd=self.repo, check=True)
        (self.ad / "repo-policy.json").unlink()
        self.run_cli("snapshot")
        self.plan("src/__tests__/foo.spec.ts")
        (self.ad / "params.json").write_text(json.dumps({"worktree": str(self.repo)}))
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        (self.ad / "bootstrap-head.txt").write_text(base)
        with existing.open("a") as handle:
            handle.write("test('new behavior', () => expect(1).toBe(1))\n")

        result = self.run_cli("validate-diff")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_final_diff_rejects_new_non_unique_profile_fixture(self):
        self.plan("src/__tests__/foo.spec.ts")
        (self.ad / "params.json").write_text(json.dumps({"worktree": str(self.repo)}))
        base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, check=True,
                              capture_output=True, text=True).stdout.strip()
        (self.ad / "bootstrap-head.txt").write_text(base)
        (self.repo / "src/__tests__/foo.spec.ts").write_text(
            "test('x', async () => { await createTestProfile({ name: 'x' }); })\n"
        )
        result = self.run_cli("validate-diff")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unique-profile-fixtures", result.stdout)


class WorkflowIntegrationTest(unittest.TestCase):
    def test_policy_contract_uses_existing_nodes_only(self):
        import yaml
        archon = Path(__file__).resolve().parents[2]
        for workflow in ("bugfix", "bugfix-codex", "bugfix-lite", "bugfix-lite-codex"):
            doc = yaml.safe_load((archon / "workflows" / f"{workflow}.yaml").read_text())
            nodes = {node["id"]: node for node in doc["nodes"]}
            self.assertNotIn("repo-policy", nodes)
            self.assertIn("repo-policy.py", nodes["preflight"]["bash"])
            self.assertIn("validate-plan", nodes["rca-gate"]["bash"])
            self.assertIn("validate-plan", nodes["red-gate"]["bash"])
            self.assertIn("validate-diff", nodes["red-gate"]["bash"])
            self.assertIn("validate-diff", nodes["exit-gate"]["bash"])
            review = next(node for node in nodes["review-loop"]["loop_group"]["nodes"] if node["id"] == "review")
            self.assertIn("repo-policy.json", review["prompt"])

    def test_code_workflow_preflights_never_fail_on_aws_auth(self):
        import yaml
        archon = Path(__file__).resolve().parents[2]
        for workflow in ("bugfix", "bugfix-codex", "bugfix-lite", "bugfix-lite-codex",
                         "full-sdlc-api", "full-sdlc-api-codex",
                         "full-sdlc-api-lite", "full-sdlc-api-lite-codex",
                         "full-sdlc-web", "full-sdlc-web-codex"):
            with self.subTest(workflow=workflow):
                doc = yaml.safe_load((archon / "workflows" / f"{workflow}.yaml").read_text())
                preflight = next(node for node in doc["nodes"] if node["id"] == "preflight")["bash"]
                self.assertNotIn("PREFLIGHT=FAIL aws", preflight)


if __name__ == "__main__":
    unittest.main()
