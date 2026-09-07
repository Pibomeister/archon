"""Real engine/mechanical qualification; agent judgments are explicit test stubs.

No provider or GitHub call is permitted. The production graph is loaded as-is,
then only agent nodes are replaced for the mechanical boundary exercise. The
native engine still owns source capture, gate persistence, response and resume.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

import yaml
from setup.tests import test_portable_project as fixtures

ROOT = fixtures.ROOT
portable = fixtures.portable

ENGINE = os.environ.get("ARCHON_ENGINE_ROOT")
PACK = ROOT / "workflows/portable/single-repo-feature"


@unittest.skipUnless(ENGINE, "Set ARCHON_ENGINE_ROOT to the qualified actual engine checkout")
class PortableEngineTest(unittest.TestCase):
    def fixture(self):
        fixture = fixtures.PortableProjectTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        # CLI title generation is auxiliary provider work outside the workflow
        # graph. Stub that seam too; keep the qualified engine files untouched.
        preload = fixture.root / "non-model-title.ts"
        title_module = str(Path(ENGINE) / "packages/core/src/services/title-generator.ts")
        preload.write_text("import { mock } from 'bun:test';\nmock.module(" + json.dumps(title_module) + ", () => ({ generateAndSetTitle: async () => {} }));\n")
        prefix = [shutil.which("bun"), "--preload", str(preload), str(Path(ENGINE) / "packages/cli/src/cli.ts")]
        fixture.binding["engineCommand"] = prefix
        fixture.save_binding()
        fixture.engine = prefix
        fixture.authoring = fixture.root / "authoring"
        # A fixture is not a child coding-agent task. Do not lend it this test
        # runner's Codex task identity or let observer state enter its worktree.
        fixture.env = {key: value for key, value in os.environ.items() if key not in ("CODEX_THREAD_ID", "CODEX_SESSION_ID") and not any(word in key.upper() for word in ("TOKEN", "API_KEY", "SECRET", "PASSWORD", "COOKIE"))}
        fixture.env.update(CODEX_HOME=str(fixture.root / "private-codex"), CLAUDE_CONFIG_DIR=str(fixture.root / "private-claude"), HERMES_HOME=str(fixture.root / "private-hermes"))
        fixture.env.update(ARCHON_HOME=str(fixture.root / "archon-home"), DATABASE_URL="", DO_NOT_TRACK="1", UV_OFFLINE="1", DISABLE_OMC="1", OMX_ROOT=str(fixture.root / "observer"), OMX_STATE_ROOT=str(fixture.root / "observer"))
        fixture.pack = fixture.authoring / ".archon/workflows/portable/single-repo-feature"
        shutil.copytree(PACK, fixture.pack, ignore=shutil.ignore_patterns("__pycache__"))
        (fixture.authoring / ".archon/config.yaml").write_text("worktree:\n  baseBranch: main\n")
        return fixture

    def cli(self, fixture, *args, json_output=False):
        result = subprocess.run([*fixture.engine, "workflow", *args, "--cwd", str(fixture.worktree), *(["--json"] if json_output else [])], env=fixture.env, cwd=fixture.worktree, capture_output=True, text=True, timeout=120)
        return result

    def test_production_graph_loads_with_qualified_engine(self):
        fixture = self.fixture()
        result = self.cli(fixture, "run", "portable-single-repo-feature", "--workflow-source", str(fixture.authoring), "--dry-run", "--stubs-init", str(fixture.root / "stubs.yaml"), "--input", "binding=" + str(fixture.binding_path), json_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["nodeCount"], 21)

    def mechanical_graph(self, fixture, mode="publication-hold"):
        workflow = yaml.safe_load((fixture.pack / "feature.yaml").read_text())
        workflow["name"] = "portable-mechanical-qualification"
        no_change = mode == "no-change"
        outputs = {
            "plan": {"goal": "CTA", "files": ["view.txt"], "approach": "Keep label" if no_change else "Change label", "testScenarios": ["CTA label"]},
            "critique": {"approved": True, "findings": []},
            "implement": {"summary": "Existing label already satisfied" if no_change else "Deterministic fixture changed label", "changes": [] if no_change else ["view.txt"], "testNotes": [], "category": "already-satisfied" if no_change else "changes-produced"},
            "review": {"status": "complete", "verdict": "Ready to merge", "findings": [], "summary": "Fixture judgment, not provider qualification"},
            "pr-body": {"title": "Fixture CTA", "body": "## Summary\nFixture only\n## Validation\nRecorded command\n## Known Residuals\nNo model qualification"},
            "kb-capture": {"summary": "Mechanical fixture", "promotionCandidates": []},
        }
        def replace(nodes):
            for node in nodes:
                if "loop_group" in node:
                    replace(node["loop_group"]["nodes"])
                if "command" not in node:
                    continue
                for key in ("command", "retry", "output_format"):
                    node.pop(key, None)
                source = "import json\n"
                if node["id"] == "implement" and not no_change:
                    source += "import os\nfrom pathlib import Path\nb = json.loads(Path(os.environ['INPUTS_BINDING']).read_text())\n(Path(b['repositoryRoot']) / 'view.txt').write_text('new CTA\\n')\n"
                source += "print(json.dumps(" + repr(outputs[node["id"]]) + "))\n"
                node.update(script=source, runtime="uv", with_={"binding": "$INPUTS.binding"})
                node["with"] = node.pop("with_")
        replace(workflow["nodes"])
        if mode == "factory-pr":
            for node in workflow["nodes"]:
                if node.get("id") == "ship":
                    node.pop("script", None)
                    node.pop("runtime", None)
                    source = """import json, os, re
from pathlib import Path
artifacts = Path(os.environ['ARTIFACTS_DIR'])
binding = json.loads(Path(os.environ['INPUTS_BINDING']).read_text())
head = '""" + fixture.base + """'
manifest = {
  'kind': 'factory-merge-review.v1',
  'repository': {'provider': 'github', 'owner': 'example', 'name': 'fixture'},
  'pullRequestNumber': 42,
  'headSha': head,
  'execution': {
    'factoryJobId': 'job:fixture:001',
    'logicalChainId': 'chain:fixture:001',
    'readySnapshotId': 'ready:fixture:001',
    'readyDigest': 'sha256:' + '1' * 64,
    'commandId': 'command:fixture:001',
    'launchId': 'launch:fixture:001',
    'attemptId': 'attempt:fixture:001',
    'runtimeBundleId': 'runtime:archon:test',
  },
}
(artifacts / 'merge-review-manifest.json').write_text(json.dumps(manifest, sort_keys=True) + '\\n')
evidence = {'ready': True, 'draft': True, 'url': 'https://github.com/example/fixture/pull/42', 'head': head, 'baseCommit': binding['baseCommit'], 'workProduct': {'files': [{'path': 'view.txt', 'sha256': 'fixture', 'executable': False}], 'sha256': '""" + ("2" * 64) + """'}, 'mergeReviewManifest': manifest, 'mergeReviewManifestPath': str(artifacts / 'merge-review-manifest.json')}
(artifacts / 'pr-evidence.json').write_text(json.dumps(evidence, sort_keys=True) + '\\n')
print(json.dumps(evidence))
"""
                    node.update(script=source, runtime="uv")
        def assert_mechanical(nodes):
            for node in nodes:
                if "loop_group" in node:
                    self.assertEqual(node["loop_group"]["until_bash"], "[ $converge.output.ready = true ]")
                    assert_mechanical(node["loop_group"]["nodes"])
                else:
                    self.assertTrue("script" in node or "approval" in node)
                    self.assertFalse(any(key in node for key in ("command", "prompt", "workflow", "include", "loop")))
        assert_mechanical(workflow["nodes"])
        (fixture.pack / "feature.yaml").write_text(yaml.safe_dump(workflow, sort_keys=False))
        return workflow

    def exercise(self, tamper):
        fixture = self.fixture()
        self.mechanical_graph(fixture)
        start = self.cli(fixture, "run", "portable-mechanical-qualification", "--workflow-source", str(fixture.authoring), "--input", "binding=" + str(fixture.binding_path), "--launch-key", "mechanical-1")
        self.assertEqual(start.returncode, 0, start.stdout + start.stderr)
        state = json.loads(self.cli(fixture, "launch-status", "mechanical-1", json_output=True).stdout)
        self.assertEqual(state["status"], "paused")
        run_id = state["receipt"]["runId"]
        response = json.loads(self.cli(fixture, "get", run_id, json_output=True).stdout)
        run = response.get("run", response)
        gate = run["metadata"]["approval"]
        artifacts = Path(run["output_root"]) / "artifacts/runs" / run_id
        self.assertTrue((artifacts / "plan.json").is_file())
        decision = self.cli(fixture, "respond", run_id, "approve", "--command-id", "approved-plan", "--expected-occurrence", gate["occurrenceId"], "--expected-evidence-digest", gate["evidenceDigest"], json_output=True)
        self.assertEqual(decision.returncode, 0, decision.stdout + decision.stderr)
        if tamper:
            plan = json.loads((artifacts / "plan.json").read_text())
            plan["approach"] = "Unapproved alteration"
            portable.write(artifacts / "plan.json", plan)
            portable.write(artifacts / "plan-seal.json", {"sha256": portable.file_digest(artifacts / "plan.json")})
        resumed = self.cli(fixture, "resume", run_id)
        self.assertNotEqual(resumed.returncode, 0, "Qualification must stop at evidence drift or unauthorized publication")
        if tamper:
            self.assertEqual((fixture.worktree / "view.txt").read_text(), "original CTA\n")
            self.assertIn("original evidence drift", resumed.stdout + resumed.stderr)
            self.assertFalse((artifacts / "verification.json").exists())
        else:
            self.assertEqual((fixture.worktree / "view.txt").read_text(), "new CTA\n", resumed.stdout + resumed.stderr)
            self.assertTrue((artifacts / "verification.json").is_file(), resumed.stdout + resumed.stderr)
            verification = json.loads((artifacts / "verification.json").read_text())
            self.assertTrue(verification["passed"])
            self.assertEqual(verification["commands"][0]["exitCode"], 0)
            self.assertEqual(json.loads((artifacts / "pr-evidence.json").read_text())["status"], "publication_requires_authorization")
            self.assertIn("Draft publication not authorized", resumed.stdout + resumed.stderr)
        self.assertEqual(fixture.git(fixture.worktree, "rev-parse", "HEAD"), fixture.base)
        self.assertEqual(fixture.git(fixture.repo, "status", "--porcelain"), "")

    def test_native_gate_resume_executes_only_mechanics_and_records_publication_hold(self):
        self.exercise(False)

    def test_editing_plan_and_local_checksum_cannot_change_engine_approved_original(self):
        self.exercise(True)

    def test_verified_no_change_completes_without_merge_review_gate(self):
        fixture = self.fixture()
        fixture.profile["verification"] = [{"id": "test", "argv": ["python3", "-c", "assert open('view.txt').read().strip() == 'original CTA'"], "timeoutSeconds": 30}]
        fixture.profile["noChangeClosure"] = {"enabled": True, "verifierIds": ["test"]}
        fixture.profile_path.write_text(json.dumps(fixture.profile))
        fixture.binding["profileSha256"] = portable.file_digest(fixture.profile_path)
        fixture.save_binding()
        self.mechanical_graph(fixture, "no-change")
        start = self.cli(fixture, "run", "portable-mechanical-qualification", "--workflow-source", str(fixture.authoring), "--input", "binding=" + str(fixture.binding_path), "--launch-key", "mechanical-no-change")
        self.assertEqual(start.returncode, 0, start.stdout + start.stderr)
        state = json.loads(self.cli(fixture, "launch-status", "mechanical-no-change", json_output=True).stdout)
        run_id = state["receipt"]["runId"]
        gate_response = json.loads(self.cli(fixture, "get", run_id, json_output=True).stdout)
        gate = gate_response.get("run", gate_response)["metadata"]["approval"]
        decision = self.cli(fixture, "respond", run_id, "approve", "--command-id", "approved-plan-no-change", "--expected-occurrence", gate["occurrenceId"], "--expected-evidence-digest", gate["evidenceDigest"], json_output=True)
        self.assertEqual(decision.returncode, 0, decision.stdout + decision.stderr)
        resumed = self.cli(fixture, "resume", run_id)
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        run_response = json.loads(self.cli(fixture, "get", run_id, json_output=True).stdout)
        run = run_response.get("run", run_response)
        self.assertEqual(run["status"], "completed")
        artifacts = Path(run["output_root"]) / "artifacts/runs" / run_id
        self.assertEqual(json.loads((artifacts / "pr-evidence.json").read_text())["status"], "fulfilled-no-change")
        self.assertFalse((artifacts / "merge-review-manifest.json").exists())
        self.assertFalse((artifacts / "merge-review-approval-binding.json").exists())

    def test_factory_pr_pauses_at_merge_review_and_completes_after_bound_approval(self):
        fixture = self.fixture()
        self.mechanical_graph(fixture, "factory-pr")
        start = self.cli(fixture, "run", "portable-mechanical-qualification", "--workflow-source", str(fixture.authoring), "--input", "binding=" + str(fixture.binding_path), "--launch-key", "mechanical-factory-pr")
        self.assertEqual(start.returncode, 0, start.stdout + start.stderr)
        state = json.loads(self.cli(fixture, "launch-status", "mechanical-factory-pr", json_output=True).stdout)
        run_id = state["receipt"]["runId"]
        plan_response = json.loads(self.cli(fixture, "get", run_id, json_output=True).stdout)
        plan_gate = plan_response.get("run", plan_response)["metadata"]["approval"]
        decision = self.cli(fixture, "respond", run_id, "approve", "--command-id", "approved-plan-factory-pr", "--expected-occurrence", plan_gate["occurrenceId"], "--expected-evidence-digest", plan_gate["evidenceDigest"], json_output=True)
        self.assertEqual(decision.returncode, 0, decision.stdout + decision.stderr)
        resumed = self.cli(fixture, "resume", run_id)
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        paused_response = json.loads(self.cli(fixture, "get", run_id, json_output=True).stdout)
        paused = paused_response.get("run", paused_response)
        self.assertEqual(paused["status"], "paused")
        merge_gate = paused["metadata"]["approval"]
        self.assertEqual(merge_gate["nodeId"], "merge-review")
        artifacts = Path(paused["output_root"]) / "artifacts/runs" / run_id
        packet = json.loads((artifacts / "approval-evidence/manifest.json").read_text())
        self.assertEqual(packet["files"], ["merge-review-manifest.json", "pr-evidence.json"])
        merge_decision = self.cli(fixture, "respond", run_id, "approve", "--command-id", "approved-merge-factory-pr", "--expected-occurrence", merge_gate["occurrenceId"], "--expected-evidence-digest", merge_gate["evidenceDigest"], json_output=True)
        self.assertEqual(merge_decision.returncode, 0, merge_decision.stdout + merge_decision.stderr)
        completed = self.cli(fixture, "resume", run_id)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        run_response = json.loads(self.cli(fixture, "get", run_id, json_output=True).stdout)
        run = run_response.get("run", run_response)
        self.assertEqual(run["status"], "completed")
        self.assertTrue((artifacts / "merge-review-approval-binding.json").is_file())
        self.assertTrue((artifacts / "kb-capture.json").is_file())


if __name__ == "__main__":
    unittest.main()
