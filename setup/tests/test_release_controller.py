#!/usr/bin/env python3
import importlib.util
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
import yaml
from pathlib import Path
from unittest.mock import patch

SETUP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SETUP))
import control_contract
spec = importlib.util.spec_from_file_location("release_controller", SETUP / "release-controller.py")
release = importlib.util.module_from_spec(spec)
if spec.loader and (SETUP / "release-controller.py").exists():
    spec.loader.exec_module(release)


CONTROLLER_SECRET = "s" * 48


class ReleaseControllerTest(unittest.TestCase):
    def state(self):
        pins = {
            "run_id": "run-1", "chain_id": "chain-1", "epoch": 1,
            "repository": "GoodwordTeam/api", "commit": "a" * 40,
            "tree": "b" * 40, "baseline_commit": "f" * 40, "baseline_tree": "0" * 40, "workflow_hash": "c" * 64,
            "policy_hash": "d" * 64, "oracle_hash": "e" * 64,
            "environment_id": "verification-1",
        }
        state = {
            "logical_chain_id": "chain-1", "chain_secret": CONTROLLER_SECRET,
            "phase": "verified", "pins": pins,
            "release": {"operation_id": "release-1", "branch": "archon/test",
                        "base": "main", "expected_remote_sha": "f" * 40,
                        "required_stages": ["review", "tests"],
                        "required_approvals": ["plan"], "title": "Prevent stale publication"},
            "approvals": [], "receipts": [],
        }
        for stage in ("review", "tests"):
            body = {"schema_version": 1, "kind": "verification", "pins": dict(pins),
                    "stage": stage, "executed": 4, "failed": 0, "skipped": 0,
                    "evidence": {"result.json": "1" * 64}}
            body["authority_mac"] = control_contract.hmac_sha256(state["chain_secret"], body)
            state["receipts"].append(body)
        approval = {"schema_version": 1, "kind": "human-approval", "stage": "plan",
                    "decision": "approved", "pins": dict(pins)}
        approval["authority_mac"] = control_contract.hmac_sha256(state["chain_secret"], approval)
        state["approvals"].append(approval)
        return state

    def manifest(self, state):
        return release.finalize_manifest(control_contract.seal_chain_state(state), controller_secret=CONTROLLER_SECRET)

    def test_a_state_payload_cannot_supply_its_own_publication_authority(self):
        imported = control_contract.seal_chain_state(self.state())
        with self.assertRaises((TypeError, ValueError)):
            release.finalize_manifest(imported)

    def test_self_signed_state_is_not_authenticated_by_an_external_controller_key(self):
        forged = self.state()
        forged["chain_secret"] = "attacker-key-" * 4
        for receipt in [*forged["receipts"], *forged["approvals"]]:
            receipt.pop("authority_mac")
            receipt["authority_mac"] = control_contract.hmac_sha256(forged["chain_secret"], receipt)
        with self.assertRaisesRegex(ValueError, "authentication"):
            release.finalize_manifest(control_contract.seal_chain_state(forged), controller_secret=CONTROLLER_SECRET)

    def test_current_verified_commit_can_be_sealed(self):
        manifest = self.manifest(self.state())
        self.assertEqual(manifest["commit"], "a" * 40)
        self.assertEqual(manifest["operation_id"], "release-1")
        self.assertEqual(manifest["branch"], "archon/test")
        self.assertIn("tests | 4 | 0 | 0", manifest["pr_body"])

    def test_post_review_commit_and_swapped_repository_are_rejected(self):
        for field, value in (("commit", "9" * 40), ("repository", "Other/api"), ("environment_id", "other")):
            with self.subTest(field=field):
                state = self.state()
                state["pins"][field] = value
                with self.assertRaisesRegex(ValueError, "binding"):
                    self.manifest(state)

    def test_altered_receipt_and_missing_approval_are_rejected(self):
        state = self.state()
        state["receipts"][0]["executed"] = 99
        with self.assertRaisesRegex(ValueError, "authentication"):
            self.manifest(state)
        state = self.state()
        state["approvals"] = []
        with self.assertRaisesRegex(ValueError, "approval"):
            self.manifest(state)

    def test_zero_execution_failure_and_skips_do_not_certify(self):
        for field, value in (("executed", 0), ("failed", 1), ("skipped", 1)):
            state = self.state()
            receipt = state["receipts"][0]
            receipt.pop("authority_mac")
            receipt[field] = value
            receipt["authority_mac"] = control_contract.hmac_sha256(state["chain_secret"], receipt)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "execution"):
                self.manifest(state)

    def test_old_unsigned_artifacts_never_upgrade_authority(self):
        with self.assertRaises(ValueError):
            release.finalize_manifest({"readiness": "passed", "commit": "a" * 40}, controller_secret=CONTROLLER_SECRET)

    def test_missing_receipt_duplicate_stage_and_branch_injection_fail(self):
        for change in (lambda s: s["receipts"].pop(),
                       lambda s: s["receipts"].append(s["receipts"][0]),
                       lambda s: s["release"].update(branch="--all")):
            state = self.state()
            change(state)
            with self.assertRaises(ValueError):
                self.manifest(state)

    def test_remote_git_gets_only_controller_credentials_not_application_environment(self):
        fixture = subprocess.CompletedProcess([], 0, "", "")
        with patch.dict(os.environ, {"GH_TOKEN": "fixture-token", "GH_CONFIG_DIR": "/private/controller-gh", "DATABASE_URL": "must-not-leak"}), patch.object(release.subprocess, "run", return_value=fixture) as run:
            release.git(Path("/quarantine"), "ls-remote", "https://github.com/GoodwordTeam/api.git", "refs/heads/archon/test")
            remote_env = run.call_args.kwargs["env"]
            self.assertEqual(remote_env["GH_TOKEN"], "fixture-token")
            self.assertNotEqual(remote_env["GH_CONFIG_DIR"], "/private/controller-gh")
            self.assertEqual(remote_env["HOME"], remote_env["GH_CONFIG_DIR"])
            self.assertNotIn("DATABASE_URL", remote_env)
            release.git(Path("/quarantine"), "bundle", "verify", "/candidate.bundle")
            self.assertNotIn("GH_TOKEN", run.call_args.kwargs["env"])
            self.assertNotIn("GH_CONFIG_DIR", run.call_args.kwargs["env"])

    def test_controller_tools_ignore_ambient_path_and_home(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake = root / "gh"
            fake.write_text("#!/bin/sh\necho should-never-run\n")
            fake.chmod(0o700)
            fixture = subprocess.CompletedProcess([], 0, "{}", "")
            with patch.dict(os.environ, {"PATH": td, "HOME": td, "GH_CONFIG_DIR": td, "GH_TOKEN": "fixture-token"}), patch.object(release.subprocess, "run", return_value=fixture) as run:
                self.assertEqual(release.github("user"), {})
                command = run.call_args.args[0]
                env = run.call_args.kwargs["env"]
                self.assertTrue(Path(command[0]).is_absolute())
                self.assertNotEqual(Path(command[0]), fake)
                self.assertNotEqual(env["PATH"], td)
                self.assertNotEqual(env["HOME"], td)
                self.assertNotEqual(env["GH_CONFIG_DIR"], td)
                release.git(Path("/quarantine"), "ls-remote", "https://github.com/GoodwordTeam/api.git")
                git_command = run.call_args.args[0]
                self.assertTrue(Path(git_command[0]).is_absolute())
                helper = next(arg for arg in git_command if arg.startswith("credential.https://github.com.helper="))
                self.assertIn(command[0], helper)

    def test_publication_refuses_ambient_config_authentication(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(release.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "controller GH_TOKEN"):
                release.github("user")
            with self.assertRaisesRegex(ValueError, "controller GH_TOKEN"):
                release.git(Path("/quarantine"), "push", "https://github.com/GoodwordTeam/api.git")
            run.assert_not_called()


class ImmutablePublicationTest(ReleaseControllerTest):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "writer"
        self.source.mkdir()
        self.run_git(self.source, "init", "-b", "main")
        self.run_git(self.source, "config", "user.name", "Fixture")
        self.run_git(self.source, "config", "user.email", "fixture@example.invalid")
        (self.source / "code.txt").write_text("baseline\n")
        self.run_git(self.source, "add", ".")
        self.run_git(self.source, "commit", "-m", "baseline")
        self.baseline = self.run_git(self.source, "rev-parse", "HEAD")
        self.remote = self.root / "remote.git"
        self.run_git(self.root, "init", "--bare", str(self.remote))
        self.run_git(self.source, "push", str(self.remote), "HEAD:refs/heads/archon/test")
        (self.source / "code.txt").write_text("repaired\n")
        self.run_git(self.source, "commit", "-am", "repair")
        self.commit = self.run_git(self.source, "rev-parse", "HEAD")
        self.tree = self.run_git(self.source, "rev-parse", "HEAD^{tree}")
        self.bundle = self.root / "candidate.bundle"
        self.run_git(self.source, "bundle", "create", str(self.bundle), "HEAD")
        self.quarantine = self.root / "quarantine"
        release.import_candidate(self.bundle, self.quarantine, self.commit, self.tree)
        state = self.state()
        state["pins"].update(commit=self.commit, tree=self.tree, baseline_commit=self.baseline,
                             baseline_tree=self.run_git(self.source, "rev-parse", f"{self.baseline}^{{tree}}"))
        state["release"]["expected_remote_sha"] = self.baseline
        for receipt in [*state["receipts"], *state["approvals"]]:
            receipt.pop("authority_mac")
            receipt["pins"] = dict(state["pins"])
            receipt["authority_mac"] = control_contract.hmac_sha256(state["chain_secret"], receipt)
        self.private = control_contract.seal_chain_state(state)
        self.manifest_value = release.finalize_manifest(self.private, controller_secret=CONTROLLER_SECRET)
        self.intent = self.root / "intent.json"
        self.real_git = release.git

    def run_git(self, cwd, *args):
        return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def local_git(self, cwd, *args):
        args = tuple(str(self.remote) if a == "https://github.com/GoodwordTeam/api.git" else a for a in args)
        return self.real_git(cwd, *args)

    def test_only_sealed_object_is_pushed_even_if_writer_advances(self):
        (self.source / "code.txt").write_text("unreviewed\n")
        self.run_git(self.source, "commit", "-am", "unreviewed")
        with patch.object(release, "git", side_effect=self.local_git):
            result = release.publish_commit(self.private, self.manifest_value, self.quarantine, self.intent, controller_secret=CONTROLLER_SECRET)
        self.assertEqual(result["status"], "commit-published")
        self.assertEqual(self.run_git(self.remote, "show", "archon/test:code.txt"), "repaired")
        self.assertEqual(self.intent.stat().st_mode & 0o777, 0o600)

    def test_remote_race_blocks_publication(self):
        self.run_git(self.source, "commit", "--allow-empty", "-m", "other")
        self.run_git(self.source, "push", str(self.remote), "HEAD:refs/heads/archon/test")
        with patch.object(release, "git", side_effect=self.local_git), self.assertRaisesRegex(ValueError, "lease conflict"):
            release.publish_commit(self.private, self.manifest_value, self.quarantine, self.intent, controller_secret=CONTROLLER_SECRET)
        self.assertEqual(self.run_git(self.remote, "show", "archon/test:code.txt"), "repaired")

    def test_commit_before_acknowledgement_is_recovered_from_intent(self):
        def lose_ack(cwd, *args):
            value = self.local_git(cwd, *args)
            if args[0] == "push":
                raise TimeoutError("lost acknowledgement")
            return value
        with patch.object(release, "git", side_effect=lose_ack), self.assertRaises(TimeoutError):
            release.publish_commit(self.private, self.manifest_value, self.quarantine, self.intent, controller_secret=CONTROLLER_SECRET)
        with patch.object(release, "git", side_effect=self.local_git) as spy:
            result = release.publish_commit(self.private, self.manifest_value, self.quarantine, self.intent, controller_secret=CONTROLLER_SECRET)
        self.assertTrue(result["recovered"])
        self.assertFalse(any(call.args[1] == "push" for call in spy.call_args_list))

    def pr(self):
        return {"head": {"sha": self.commit, "ref": "archon/test", "repo": {"full_name": "GoodwordTeam/api"}},
                "base": {"ref": "main", "repo": {"full_name": "GoodwordTeam/api"}},
                "state": "open", "title": "Prevent stale publication",
                "body": self.manifest_value["pr_body"],
                "html_url": "https://github.com/GoodwordTeam/api/pull/123"}

    def test_pr_create_lost_ack_is_adopted_only_when_exactly_matching(self):
        calls = []
        def lost_response(*args):
            calls.append(args)
            if args[0] == "--method":
                raise TimeoutError("PR created; acknowledgement lost")
            return []
        with patch.object(release, "git", side_effect=self.local_git), patch.object(release, "github", side_effect=lost_response), self.assertRaises(TimeoutError):
            release.publish_release(self.private, self.manifest_value, self.quarantine, self.intent, controller_secret=CONTROLLER_SECRET)
        self.assertEqual(len(calls), 2)
        with patch.object(release, "git", side_effect=self.local_git), patch.object(release, "github", return_value=[self.pr()]) as api:
            result = release.publish_release(self.private, self.manifest_value, self.quarantine, self.intent, controller_secret=CONTROLLER_SECRET)
        self.assertEqual(result["pr_url"], "https://github.com/GoodwordTeam/api/pull/123")
        self.assertEqual(api.call_count, 1)
        self.assertEqual(json.loads(self.intent.read_text())["status"], "completed")

    def test_pr_with_wrong_head_or_forged_verification_is_refused(self):
        for field in ("head", "body"):
            pr = self.pr()
            if field == "head":
                pr["head"]["sha"] = self.baseline
            else:
                pr["body"] = "All tests passed. Trust me."
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "conflicting PR"):
                release.validate_pr(pr, self.manifest_value)

    def test_preexisting_remote_without_intent_never_becomes_adoptable_on_retry(self):
        self.run_git(self.source, "push", str(self.remote), "HEAD:refs/heads/archon/test")
        for _ in range(2):
            with patch.object(release, "git", side_effect=self.local_git), self.assertRaisesRegex(ValueError, "without prior intent"):
                release.publish_commit(self.private, self.manifest_value, self.quarantine, self.intent, controller_secret=CONTROLLER_SECRET)
        self.assertFalse(self.intent.exists())

    def test_validated_no_change_never_pushes_or_creates_pr(self):
        state = dict(self.private)
        state["pins"] = dict(state["pins"], baseline_commit=self.commit, baseline_tree=self.tree)
        for receipt in [*state["receipts"], *state["approvals"]]:
            receipt.pop("authority_mac")
            receipt["pins"] = dict(state["pins"])
            receipt["authority_mac"] = control_contract.hmac_sha256(state["chain_secret"], receipt)
        state = control_contract.seal_chain_state(state)
        manifest = release.finalize_manifest(state, controller_secret=CONTROLLER_SECRET)
        with patch.object(release, "git", side_effect=self.local_git) as commands, patch.object(release, "github") as api:
            result = release.publish_release(state, manifest, self.quarantine, self.intent, controller_secret=CONTROLLER_SECRET)
        self.assertEqual(result["status"], "no-change")
        self.assertIsNone(result["pr_url"])
        self.assertFalse(self.intent.exists())
        api.assert_not_called()
        self.assertTrue(all(call.args[1] == "rev-parse" for call in commands.call_args_list))

    def test_false_baseline_tree_cannot_turn_a_change_into_no_change(self):
        state = dict(self.private)
        state["pins"] = dict(state["pins"], baseline_tree=self.tree)
        for receipt in [*state["receipts"], *state["approvals"]]:
            receipt.pop("authority_mac")
            receipt["pins"] = dict(state["pins"])
            receipt["authority_mac"] = control_contract.hmac_sha256(state["chain_secret"], receipt)
        state = control_contract.seal_chain_state(state)
        with patch.object(release, "git", side_effect=self.local_git), patch.object(release, "github") as api:
            with self.assertRaisesRegex(ValueError, "pinned baseline commit"):
                release.finalize_manifest(state, controller_secret=CONTROLLER_SECRET)
        api.assert_not_called()
        self.assertFalse(self.intent.exists())

    def test_remote_update_after_preflight_is_not_overwritten(self):
        raced = False
        other = None
        def race_before_push(cwd, *args):
            nonlocal raced, other
            if args[0] == "push" and not raced:
                raced = True
                self.run_git(self.source, "commit", "--allow-empty", "-m", "concurrent owner update")
                other = self.run_git(self.source, "rev-parse", "HEAD")
                self.run_git(self.source, "push", str(self.remote), "HEAD:refs/heads/archon/test")
            return self.local_git(cwd, *args)
        with patch.object(release, "git", side_effect=race_before_push), self.assertRaisesRegex(ValueError, "git operation failed"):
            release.publish_commit(self.private, self.manifest_value, self.quarantine, self.intent, controller_secret=CONTROLLER_SECRET)
        self.assertTrue(raced)
        self.assertEqual(self.run_git(self.remote, "rev-parse", "refs/heads/archon/test"), other)

    def test_empty_commit_does_not_substitute_for_baseline_acceptance_verification(self):
        baseline_tree = self.run_git(self.source, "rev-parse", f"{self.baseline}^{{tree}}")
        empty_commit = self.run_git(self.source, "commit-tree", baseline_tree, "-p", self.baseline, "-m", "empty")
        state = copy.deepcopy(self.private)
        state["pins"].update(commit=empty_commit, tree=baseline_tree)
        for receipt in [*state["receipts"], *state["approvals"]]:
            receipt.pop("authority_mac")
            receipt["pins"] = dict(state["pins"])
            receipt["authority_mac"] = control_contract.hmac_sha256(state["chain_secret"], receipt)
        state = control_contract.seal_chain_state(state)
        with self.assertRaisesRegex(ValueError, "pinned baseline commit"):
            release.finalize_manifest(state, controller_secret=CONTROLLER_SECRET)
        self.assertFalse(self.intent.exists())

    def test_new_branch_cannot_publish_history_unrelated_to_the_pinned_baseline(self):
        orphan = self.run_git(self.source, "commit-tree", self.tree, "-m", "unrelated history")
        self.run_git(self.quarantine, "fetch", str(self.source), orphan)
        self.run_git(self.quarantine, "update-ref", "refs/candidates/sealed", orphan)
        state = copy.deepcopy(self.private)
        state["pins"]["commit"] = orphan
        state["release"].update(branch="archon/new-branch", expected_remote_sha=None)
        for receipt in [*state["receipts"], *state["approvals"]]:
            receipt.pop("authority_mac")
            receipt["pins"] = dict(state["pins"])
            receipt["authority_mac"] = control_contract.hmac_sha256(state["chain_secret"], receipt)
        state = control_contract.seal_chain_state(state)
        manifest = release.finalize_manifest(state, controller_secret=CONTROLLER_SECRET)
        with patch.object(release, "git", side_effect=self.local_git):
            with self.assertRaisesRegex(ValueError, "git operation failed"):
                release.publish_commit(state, manifest, self.quarantine, self.intent, controller_secret=CONTROLLER_SECRET)
        self.assertEqual(self.run_git(self.remote, "for-each-ref", "refs/heads/archon/new-branch"), "")
        self.assertFalse(self.intent.exists())

    def test_wrong_tree_and_preexisting_quarantine_are_refused(self):
        with self.assertRaisesRegex(ValueError, "tree mismatch"):
            release.import_candidate(self.bundle, self.root / "wrong", self.commit, "b" * 40)
        with self.assertRaisesRegex(ValueError, "new controller-owned"):
            release.import_candidate(self.bundle, self.quarantine, self.commit, self.tree)


class PublicationWorkflowTest(unittest.TestCase):
    def test_all_ship_nodes_publish_from_bash(self):
        """Inverse of the assertion 229090a introduced.

        release-controller.py is still correct code and stays tested above, but
        it has no call site: stock Archon has no controller_action node kind, so
        a ship node declaring one fails validation before the run starts. Ship is
        a bash node again, as it was for every completed run before 229090a.
        """
        ships = {}
        for path in (SETUP.parent / "workflows").glob("*.yaml"):
            doc = yaml.safe_load(path.read_text())
            for node in doc.get("nodes", []):
                if node.get("id") != "ship":
                    continue
                ships[path.stem] = node
                with self.subTest(workflow=path.stem):
                    self.assertNotIn("controller_action", node)
                    self.assertIn("bash", node)
        self.assertTrue({"bugfix", "full-sdlc-api", "full-sdlc-web", "wrap-ship"}.issubset(ships))


if __name__ == "__main__":
    unittest.main()
