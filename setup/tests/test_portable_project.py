import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "workflows/portable/single-repo-feature/scripts/project.py"
sys.path.insert(0, str(SCRIPT.parent))
module_spec = importlib.util.spec_from_file_location("portable_project", SCRIPT)
portable = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(portable)


class PortableProjectTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "source"
        self.repo.mkdir()
        self.git(self.repo, "init", "-q", "-b", "main")
        self.git(self.repo, "config", "user.email", "test@example.invalid")
        self.git(self.repo, "config", "user.name", "Test")
        (self.repo / "view.txt").write_text("original CTA\n")
        (self.repo / "README.md").write_text("Pinned project context\n")
        (self.repo / "package.json").write_text(json.dumps({"packageManager": "pnpm@10.21.0"}))
        self.git(self.repo, "add", ".")
        self.git(self.repo, "commit", "-qm", "Fixture")
        self.base = self.git(self.repo, "rev-parse", "HEAD")
        self.remote = "https://github.com/example/fixture.git"
        self.git(self.repo, "remote", "add", "origin", self.remote)
        self.allowed = self.root / "worktrees"
        self.worktree = self.allowed / "attempt-1"
        self.git(self.repo, "worktree", "add", "-q", "-b", "factory/attempt-1", str(self.worktree), self.base)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.profile = {
            "profileVersion": "archon.project-profile.v1", "projectId": "project:fixture",
            "repository": {"remote": self.remote, "defaultBranch": "main", "stack": "nextjs-workspace", "packageManager": "pnpm@10.21.0"},
            "verification": [{"id": "test", "argv": [sys.executable, "-c", "assert open('view.txt').read().strip() == 'new CTA'"], "timeoutSeconds": 30}],
            "scope": {"allowedPaths": ["view.txt"], "forbiddenPaths": [".env*", "billing/**"]},
            "knowledge": {"paths": ["README.md"], "maxBytes": 4096},
            "recovery": {"maxRounds": 2},
            "delivery": {"draftOnly": True, "autoMerge": False, "autoDeploy": False},
        }
        self.profile_path = self.root / "profile.json"
        self.profile_path.write_text(json.dumps(self.profile))
        self.brief = self.root / "brief.md"
        self.brief.write_text("Change the CTA label, with a focused test.\n")
        self.binding = {
            "bindingVersion": "archon.machine-binding.v1", "projectId": "project:fixture", "machineId": "machine:test",
            "repositoryRoot": str(self.worktree), "allowedWorktreeRoot": str(self.allowed),
            "branch": "factory/attempt-1", "baseCommit": self.base,
            "profilePath": str(self.profile_path), "profileSha256": portable.file_digest(self.profile_path),
            "briefPath": str(self.brief), "briefSha256": portable.file_digest(self.brief),
            "knowledgeRoot": str(self.repo), "knowledgeCommit": self.base, "allowPublish": False, "engineCommand": [sys.executable],
        }
        self.binding_path = self.root / "binding.json"
        self.save_binding()
        # These tests can themselves run inside an Archon verification node.
        # Bind each unit fixture to its own context, not that parent workflow.
        inputs = patch.dict(os.environ, {"INPUTS_BINDING": str(self.binding_path)})
        inputs.start()
        self.addCleanup(inputs.stop)

    def git(self, repo, *args):
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()

    def save_binding(self):
        self.binding_path.write_text(json.dumps(self.binding))

    def capture(self):
        return portable.capture(self.binding_path, self.artifacts)

    def factory_execution(self):
        return {
            "factoryJobId": "job:fixture:001",
            "logicalChainId": "chain:fixture:001",
            "readySnapshotId": "ready:fixture:001",
            "readyDigest": "sha256:" + "1" * 64,
            "commandId": "command:fixture:001",
            "launchId": "launch:fixture:001",
            "attemptId": "attempt:fixture:001",
            "runtimeBundleId": "runtime:archon:test",
        }

    def test_captures_pinned_knowledge_without_reading_dirty_worktree(self):
        (self.repo / "README.md").write_text("dirty unapproved knowledge\n")
        result = self.capture()
        self.assertIn("contextDigest", result)
        self.assertEqual((self.artifacts / "knowledge/README.md").read_text(), "Pinned project context\n")
        self.assertEqual((self.repo / "README.md").read_text(), "dirty unapproved knowledge\n")

    def test_rejects_manual_checkout_even_inside_configured_root(self):
        self.binding.update(repositoryRoot=str(self.repo), allowedWorktreeRoot=str(self.root))
        self.save_binding()
        with self.assertRaisesRegex(ValueError, "linked worktree"):
            self.capture()

    def test_rejects_wrong_root(self):
        self.binding["allowedWorktreeRoot"] = str(self.root / "elsewhere")
        self.save_binding()
        with self.assertRaisesRegex(ValueError, "allowed worktree root"):
            self.capture()

    def test_missing_profile_has_no_goodword_fallback(self):
        self.profile_path.unlink()
        with self.assertRaises((ValueError, FileNotFoundError)):
            self.capture()

    def test_profile_and_brief_drift_fail_closed(self):
        self.capture()
        self.profile_path.write_text(self.profile_path.read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "profile digest"):
            self.capture()
        self.profile_path.write_text(json.dumps(self.profile))
        self.brief.write_text("Expanded scope\n")
        with self.assertRaisesRegex(ValueError, "brief digest"):
            self.capture()

    def test_snapshot_drift_is_rejected_on_resume(self):
        self.capture()
        (self.artifacts / "knowledge/README.md").write_text("altered run context\n")
        with self.assertRaisesRegex(ValueError, "snapshot drift"):
            portable.load_context(self.artifacts)

    def test_plan_cannot_expand_profile_scope(self):
        self.capture()
        plan = {"goal": "CTA", "files": ["billing/change.py"], "approach": "Change", "testScenarios": ["CTA works"]}
        with self.assertRaisesRegex(ValueError, "scope"):
            portable.seal_plan(self.artifacts, plan, {"approved": True, "findings": []})

    def test_recovery_round_limit_survives_resume(self):
        self.capture()
        portable.seal_plan(self.artifacts, {"goal": "CTA", "files": ["view.txt"], "approach": "Change", "testScenarios": ["CTA works"]}, {"approved": True, "findings": []})
        self.assertEqual(portable.begin_round(self.artifacts)["round"], 1)
        self.assertEqual(portable.begin_round(self.artifacts)["round"], 2)
        with self.assertRaisesRegex(ValueError, "round limit"):
            portable.begin_round(self.artifacts)

    def test_real_verification_observes_failure_then_success(self):
        self.capture()
        portable.seal_plan(self.artifacts, {"goal": "CTA", "files": ["view.txt"], "approach": "Change", "testScenarios": ["CTA works"]}, {"approved": True, "findings": []})
        portable.begin_round(self.artifacts)
        self.assertFalse(portable.verify(self.artifacts)["passed"])
        (self.worktree / "view.txt").write_text("new CTA\n")
        self.assertTrue(portable.verify(self.artifacts)["passed"])


    def ready_product(self):
        self.capture()
        portable.seal_plan(self.artifacts, {"goal": "Make the CTA clear", "files": ["view.txt"], "approach": "Change label", "testScenarios": ["CTA works"]}, {"approved": True, "findings": []})
        portable.begin_round(self.artifacts)
        (self.worktree / "view.txt").write_text("new CTA\n")
        portable.verify(self.artifacts)
        portable.converge(self.artifacts, {"status": "complete", "verdict": "Ready to merge", "findings": []})

    def test_branch_drift_is_rejected_during_later_stages(self):
        self.capture()
        self.git(self.worktree, "checkout", "-qb", "factory/other")
        with self.assertRaisesRegex(ValueError, "branch"):
            portable.load_context(self.artifacts)

    def test_empty_knowledge_scope_does_not_capture_everything(self):
        self.profile["knowledge"]["paths"] = []
        self.profile_path.write_text(json.dumps(self.profile))
        self.binding["profileSha256"] = portable.file_digest(self.profile_path)
        self.save_binding()
        with self.assertRaisesRegex(ValueError, "explicit knowledge paths"):
            self.capture()

    def test_knowledge_capture_cannot_write_or_publish_external_knowledge(self):
        self.capture()
        before = self.git(self.repo, "status", "--porcelain")
        portable.capture_knowledge(self.artifacts, {"summary": "Learned a test", "promotionCandidates": ["Test the CTA"], "status": "published"})
        self.assertEqual(json.loads((self.artifacts / "kb-capture.json").read_text())["status"], "proposed")
        self.assertEqual(self.git(self.repo, "status", "--porcelain"), before)


    def test_capture_freezes_factory_execution_identity_sidecar(self):
        execution = self.factory_execution()
        execution_path = self.root / "factory-execution.json"
        execution_path.write_text(json.dumps(execution))
        with patch.dict(os.environ, {"INPUTS_FACTORY_EXECUTION": str(execution_path)}):
            self.capture()
        context = json.loads((self.artifacts / "run-context.json").read_text())
        self.assertEqual(context["factoryExecution"], execution)
        execution_path.write_text(json.dumps({**execution, "attemptId": "attempt:fixture:other"}))
        with patch.dict(os.environ, {"INPUTS_FACTORY_EXECUTION": str(execution_path)}):
            with self.assertRaisesRegex(ValueError, "factory execution identity drift"):
                portable.capture(self.binding_path, self.artifacts)

    def test_merge_review_manifest_binds_exact_pr_head_and_factory_execution(self):
        execution = self.factory_execution()
        context = {"factoryExecution": execution}
        manifest = portable.merge_review_manifest(
            context,
            "example/fixture",
            "https://github.com/example/fixture/pull/42",
            self.base,
        )
        self.assertEqual(manifest["kind"], "factory-merge-review.v1")
        self.assertEqual(manifest["repository"], {"provider": "github", "owner": "example", "name": "fixture"})
        self.assertEqual(manifest["pullRequestNumber"], 42)
        self.assertEqual(manifest["headSha"], self.base)
        self.assertEqual(manifest["execution"], execution)
        with self.assertRaisesRegex(ValueError, "exact PR URL"):
            portable.merge_review_manifest(context, "example/fixture", "https://github.com/example/other/pull/42", self.base)

    def test_no_publish_authorization_records_intent_without_commit_or_push(self):
        self.ready_product()
        result = portable.ship(self.artifacts, {"title": "Clarify the CTA", "body": "Verified change"})
        self.assertFalse(result["ready"])
        self.assertEqual(result["status"], "publication_requires_authorization")
        self.assertEqual(self.git(self.worktree, "rev-parse", "HEAD"), self.base)

    def test_commit_hook_change_requires_re_review_before_push(self):
        self.binding["allowPublish"] = True
        self.save_binding()
        self.ready_product()
        hooks = self.root / "hooks"
        hooks.mkdir()
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\nprintf 'hook change\\n' >> view.txt\ngit add -- view.txt\n")
        hook.chmod(0o700)
        self.git(self.repo, "config", "core.hooksPath", str(hooks))
        with self.assertRaisesRegex(ValueError, "Post-commit-hook"):
            portable.ship(self.artifacts, {"title": "Clarify the CTA", "body": "Verified change"})
        self.assertFalse((self.artifacts / "commit-receipt.json").exists())
        self.assertNotEqual(self.git(self.worktree, "rev-parse", "HEAD"), self.base)

    def test_fluxkeep_profile_uses_verified_main_scripts_and_exact_cta_files(self):
        profile = json.loads((ROOT / "profiles/fluxkeep-next.v1.json").read_text())
        portable.validate_profile(profile)
        self.assertEqual(profile["repository"]["packageManager"], "pnpm@10.21.0")
        test = next(check for check in profile["verification"] if check["id"] == "document-detail-test")
        self.assertEqual(test["argv"], ["pnpm", "exec", "jest", "--watchman=false", "--runInBand", "--runTestsByPath", "test/src/app/(admin)/documents/components/document-detail.test.tsx"])
        self.assertEqual(len(profile["scope"]["allowedPaths"]), 2)
        self.assertNotIn("typecheck:all", json.dumps(profile))

    def test_v2_no_change_closure_names_pinned_verifiers(self):
        profile = json.loads((ROOT / "profiles/goodword-archon.v2.json").read_text())
        portable.validate_profile(profile)
        self.assertEqual(profile["noChangeClosure"]["enabled"], False)
        self.assertEqual(profile["noChangeClosure"]["verifierIds"], ["portable-and-policy-tests"])
        without_policy = dict(profile)
        without_policy.pop("noChangeClosure")
        portable.validate_profile(without_policy)
        profile["noChangeClosure"]["verifierIds"] = ["portable-and-policy-tests", "missing"]
        with self.assertRaisesRegex(ValueError, "pinned verification"):
            portable.validate_profile(profile)
        profile["noChangeClosure"]["verifierIds"] = ["portable-and-policy-tests", "portable-and-policy-tests"]
        with self.assertRaisesRegex(ValueError, "pinned verification"):
            portable.validate_profile(profile)
        profile["noChangeClosure"] = {"enabled": True, "verifierIds": []}
        with self.assertRaisesRegex(ValueError, "pinned verification"):
            portable.validate_profile(profile)

    def test_no_change_closure_requires_claim_and_pinned_baseline_final_verification(self):
        self.capture()
        portable.seal_plan(self.artifacts, {"goal": "CTA", "files": ["view.txt"], "approach": "Keep current label", "testScenarios": ["CTA already works"]}, {"approved": True, "findings": []})
        self.profile["noChangeClosure"] = {"enabled": True, "verifierIds": ["test"]}
        self.profile["verification"] = [{"id": "test", "argv": [sys.executable, "-c", "assert open('view.txt').read().strip() == 'original CTA'"], "timeoutSeconds": 30}]
        with patch.object(portable, "load_context", return_value={"binding": self.binding, "profile": self.profile, "toolchain": {}}):
            self.assertTrue(portable.baseline_verify(self.artifacts)["passed"])
            portable.begin_round(self.artifacts)
            claim_status = portable.record_no_change_claim(self.artifacts, {"summary": "Already correct", "changes": [], "testNotes": [], "category": "already-satisfied"})
            self.assertTrue(claim_status["recorded"])
            self.assertTrue(portable.verify(self.artifacts)["passed"])
            closure = portable.no_change_closure(self.artifacts)
            self.assertEqual(closure["lifecycleResult"], "fulfilled-no-change")
            portable.converge(self.artifacts, {"status": "complete", "verdict": "Ready to merge", "findings": []})
            result = portable.ship(self.artifacts, {"title": "Unused", "body": "Unused"})
            self.assertEqual(result["status"], "fulfilled-no-change")
            self.assertFalse(result["draft"])
            self.assertFalse((self.artifacts / "commit-receipt.json").exists())

    def test_later_non_no_change_outcome_invalidates_previous_claim_and_intent(self):
        self.capture()
        portable.seal_plan(self.artifacts, {"goal": "CTA", "files": ["view.txt"], "approach": "Keep current label", "testScenarios": ["CTA already works"]}, {"approved": True, "findings": []})
        self.profile["noChangeClosure"] = {"enabled": True, "verifierIds": ["test"]}
        self.profile["verification"] = [{"id": "test", "argv": [sys.executable, "-c", "assert open('view.txt').read().strip() == 'original CTA'"], "timeoutSeconds": 30}]
        with patch.object(portable, "load_context", return_value={"binding": self.binding, "profile": self.profile, "toolchain": {}}):
            portable.baseline_verify(self.artifacts)
            portable.begin_round(self.artifacts)
            self.assertTrue(portable.record_no_change_claim(self.artifacts, {"summary": "Already correct", "category": "already-satisfied"})["recorded"])
            portable.verify(self.artifacts)
            portable.no_change_closure(self.artifacts)
            self.assertTrue((self.artifacts / "no-change-closure-intent.json").exists())
            portable.record_no_change_claim(self.artifacts, {"summary": "Need human detail", "category": "blocked"})
            self.assertFalse((self.artifacts / "no-change-claim.json").exists())
            self.assertFalse((self.artifacts / "no-change-closure-intent.json").exists())

    def test_ship_revalidates_no_change_intent_against_current_policy_and_product(self):
        self.capture()
        portable.seal_plan(self.artifacts, {"goal": "CTA", "files": ["view.txt"], "approach": "Keep current label", "testScenarios": ["CTA already works"]}, {"approved": True, "findings": []})
        self.profile["noChangeClosure"] = {"enabled": True, "verifierIds": ["test"]}
        self.profile["verification"] = [{"id": "test", "argv": [sys.executable, "-c", "assert open('view.txt').read().strip() == 'original CTA'"], "timeoutSeconds": 30}]
        with patch.object(portable, "load_context", return_value={"binding": self.binding, "profile": self.profile, "toolchain": {}}):
            portable.baseline_verify(self.artifacts)
            portable.begin_round(self.artifacts)
            portable.record_no_change_claim(self.artifacts, {"summary": "Already correct", "category": "already-satisfied"})
            portable.verify(self.artifacts)
            portable.no_change_closure(self.artifacts)
            portable.converge(self.artifacts, {"status": "complete", "verdict": "Ready to merge", "findings": []})
            self.profile["noChangeClosure"] = {"enabled": False, "verifierIds": ["test"]}
            with self.assertRaisesRegex(ValueError, "disabled|enabled"):
                portable.ship(self.artifacts, {"title": "Unused", "body": "Unused"})

    def test_no_change_ship_requires_ready_final_review(self):
        self.capture()
        portable.seal_plan(self.artifacts, {"goal": "CTA", "files": ["view.txt"], "approach": "Keep current label", "testScenarios": ["CTA already works"]}, {"approved": True, "findings": []})
        self.profile["noChangeClosure"] = {"enabled": True, "verifierIds": ["test"]}
        self.profile["verification"] = [{"id": "test", "argv": [sys.executable, "-c", "assert open('view.txt').read().strip() == 'original CTA'"], "timeoutSeconds": 30}]
        with patch.object(portable, "load_context", return_value={"binding": self.binding, "profile": self.profile, "toolchain": {}}):
            portable.baseline_verify(self.artifacts)
            portable.begin_round(self.artifacts)
            portable.record_no_change_claim(self.artifacts, {"summary": "Already correct", "category": "already-satisfied"})
            portable.verify(self.artifacts)
            portable.no_change_closure(self.artifacts)
            with self.assertRaisesRegex(ValueError, "review"):
                portable.ship(self.artifacts, {"title": "Unused", "body": "Unused"})
            portable.converge(self.artifacts, {"status": "complete", "verdict": "Not ready", "findings": ["missing proof"]})
            with self.assertRaisesRegex(ValueError, "review"):
                portable.ship(self.artifacts, {"title": "Unused", "body": "Unused"})

    def test_no_change_claim_with_changed_work_product_fails_closed(self):
        self.capture()
        portable.seal_plan(self.artifacts, {"goal": "CTA", "files": ["view.txt"], "approach": "Keep current label", "testScenarios": ["CTA already works"]}, {"approved": True, "findings": []})
        self.profile["noChangeClosure"] = {"enabled": True, "verifierIds": ["test"]}
        with patch.object(portable, "load_context", return_value={"binding": self.binding, "profile": self.profile, "toolchain": {}}):
            portable.begin_round(self.artifacts)
            (self.worktree / "view.txt").write_text("changed CTA\n")
            with self.assertRaisesRegex(ValueError, "already-satisfied"):
                portable.record_no_change_claim(self.artifacts, {"summary": "Already correct", "changes": [], "testNotes": [], "category": "already-satisfied"})


    def test_feature_workflow_has_conditional_post_pr_merge_review_gate(self):
        workflow = (ROOT / "workflows/portable/single-repo-feature/feature.yaml").read_text()
        self.assertIn("- id: merge-review-route", workflow)
        self.assertIn("- id: prepare-merge-review", workflow)
        self.assertIn("- id: merge-review", workflow)
        self.assertIn("when: \"$merge-review-route.output.required == true\"", workflow)
        self.assertIn("approval-evidence packet", workflow)
        self.assertIn("Approving this gate authorizes only the Control API", workflow)
        self.assertIn("- id: bind-merge-review", workflow)
        self.assertIn("- id: kb-capture\n  command: capture-knowledge", workflow)
        self.assertLess(workflow.index("- id: ship"), workflow.index("- id: merge-review-route"))
        self.assertLess(workflow.index("- id: prepare-merge-review"), workflow.index("\n- id: merge-review\n"))
        self.assertLess(workflow.index("- id: bind-merge-review"), workflow.index("\n- id: kb-capture\n"))
        kb_block = workflow[workflow.index("- id: kb-capture"):workflow.index("- id: capture-knowledge")]
        self.assertIn("depends_on:\n  - merge-review-route\n  - bind-merge-review", kb_block)
        self.assertIn("trigger_rule: all_done", kb_block)

    def test_merge_review_route_is_false_without_managed_pr_manifest(self):
        self.ready_product()
        portable.write(self.artifacts / "pr-evidence.json", {"ready": False, "status": "publication_requires_authorization"})
        result = portable.merge_review_route(self.artifacts)
        self.assertFalse(result["required"])

    def test_prepare_merge_review_replaces_plan_packet_with_exact_pr_packet(self):
        self.capture()
        portable.seal_plan(self.artifacts, {"goal": "CTA", "files": ["view.txt"], "approach": "Change", "testScenarios": ["CTA works"]}, {"approved": True, "findings": []})
        plan_manifest = json.loads((self.artifacts / "approval-evidence/manifest.json").read_text())
        self.assertIn("plan.json", plan_manifest["files"])
        execution = self.factory_execution()
        manifest = portable.merge_review_manifest({"factoryExecution": execution}, "example/fixture", "https://github.com/example/fixture/pull/42", self.base)
        portable.write(self.artifacts / "merge-review-manifest.json", manifest)
        portable.write(self.artifacts / "pr-evidence.json", {"ready": True, "draft": True, "url": "https://github.com/example/fixture/pull/42", "head": self.base, "mergeReviewManifest": manifest, "mergeReviewManifestPath": str(self.artifacts / "merge-review-manifest.json")})
        prepared = portable.prepare_merge_review(self.artifacts)
        self.assertTrue(prepared["prepared"])
        packet_manifest = json.loads((self.artifacts / "approval-evidence/manifest.json").read_text())
        self.assertEqual(packet_manifest["files"], ["merge-review-manifest.json", "pr-evidence.json"])
        self.assertEqual(json.loads((self.artifacts / "approval-evidence/merge-review-manifest.json").read_text()), manifest)
        self.assertFalse((self.artifacts / "approval-evidence/plan.json").exists())

    def test_portable_resources_are_in_team_package_manifest(self):
        manifest = (ROOT / "setup/package.sh").read_text()
        for path in (ROOT / "workflows/portable/single-repo-feature").rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                self.assertIn(str(path.relative_to(ROOT)), manifest)
        for helper in ("repo_policy.py", "review_envelope.py"):
            path = SCRIPT.parent / helper
            self.assertTrue(path.is_symlink())
            self.assertIn(ROOT / "setup", path.resolve().parents)


    def test_approval_manifest_contains_only_fixed_inputs_and_pinned_knowledge(self):
        self.capture()
        secret = self.artifacts / "codex-home/auth.json"
        secret.parent.mkdir()
        secret.write_text("TEST_ONLY_SECRET")
        portable.seal_plan(self.artifacts, {"goal": "CTA", "files": ["view.txt"], "approach": "Change", "testScenarios": ["CTA works"]}, {"approved": True, "findings": []})
        manifest = json.loads((self.artifacts / "approval-evidence/manifest.json").read_text())
        self.assertEqual(manifest["version"], 1)
        self.assertIn("plan.json", manifest["files"])
        self.assertIn("knowledge/README.md", manifest["files"])
        self.assertFalse(any("codex-home" in name for name in manifest["files"]))
        self.assertFalse((self.artifacts / "approval-evidence/codex-home").exists())

    def test_planner_cannot_select_extra_evidence_by_rewriting_local_context_and_seal(self):
        self.capture()
        context = json.loads((self.artifacts / "run-context.json").read_text())
        (self.artifacts / "knowledge/extra.json").write_text("unapproved data")
        context["knowledgeFiles"]["extra.json"] = portable.file_digest(self.artifacts / "knowledge/extra.json")
        portable.write(self.artifacts / "run-context.json", context)
        portable.write(self.artifacts / "context-seal.json", {"sha256": portable.file_digest(self.artifacts / "run-context.json")})
        with self.assertRaisesRegex(ValueError, "snapshot drift"):
            portable.seal_plan(self.artifacts, {"goal": "CTA", "files": ["view.txt"], "approach": "Change", "testScenarios": ["CTA works"]}, {"approved": True, "findings": []})


if __name__ == "__main__":
    unittest.main()
