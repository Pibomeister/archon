import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / "workflows/portable/single-repo-feature/scripts/repair_publication.py"
spec = importlib.util.spec_from_file_location("repair_publication", SCRIPT)
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)
sys.path.insert(0, str(SCRIPT.parent))
project_spec = importlib.util.spec_from_file_location("repair_test_project", SCRIPT.parent / "project.py")
project = importlib.util.module_from_spec(project_spec)
project_spec.loader.exec_module(project)


class RepairPublicationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.repo = self.root / "work"
        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "repair", str(self.repo)], check=True)
        repair.git(self.repo, "config", "user.email", "fixture@example.invalid")
        repair.git(self.repo, "config", "user.name", "Fixture")
        (self.repo / "allowed").mkdir()
        (self.repo / "outside.txt").write_text("outside\n")
        self.base = self.commit("base")
        repair.git(self.repo, "push", str(self.remote), "HEAD:refs/heads/repair")
        self.context = {"executionBaseRevision": self.base, "remoteUrl": str(self.remote), "branch": "repair", "fileGlobs": ["allowed/**"]}
        self.authority = patch.object(repair, "assert_current_authority")
        self.authority.start()
        self.addCleanup(self.authority.stop)
        self.project_authority = patch.object(project.repair_publication, "assert_current_authority")
        self.project_authority.start()
        self.addCleanup(self.project_authority.stop)

    def commit(self, text):
        (self.repo / "allowed" / "one.txt").write_text(text + "\n")
        repair.git(self.repo, "add", ".")
        repair.git(self.repo, "commit", "-qm", text)
        return repair.git(self.repo, "rev-parse", "HEAD").strip()

    def test_conditional_fast_forward_publishes_exact_commit(self):
        head = self.commit("repair")
        repair.publish(self.repo, self.context, head)
        self.assertEqual(repair.git(self.remote, "rev-parse", "refs/heads/repair").strip(), head)

    def test_compatible_remote_move_between_check_and_push_is_rejected(self):
        middle = self.commit("middle")
        head = self.commit("final")
        actual_git = repair.git
        def racing_git(root, *args):
            if args[0] == "push":
                actual_git(self.repo, "push", str(self.remote), middle + ":refs/heads/repair")
            return actual_git(root, *args)
        with patch.object(repair, "git", racing_git), self.assertRaises(subprocess.CalledProcessError):
            repair.publish(self.repo, self.context, head)
        self.assertEqual(actual_git(self.remote, "rev-parse", "refs/heads/repair").strip(), middle)

    def test_unexpected_remote_head_rejects_before_push(self):
        middle = self.commit("middle")
        repair.git(self.repo, "push", str(self.remote), middle + ":refs/heads/repair")
        with self.assertRaisesRegex(ValueError, "remote-head-changed"):
            repair.publish(self.repo, self.context, self.commit("final"))

    def test_divergent_history_is_never_rewritten(self):
        repair.git(self.repo, "checkout", "--orphan", "divergent")
        head = self.commit("replacement")
        with self.assertRaises(subprocess.CalledProcessError):
            repair.publish(self.repo, self.context, head)
        self.assertEqual(repair.git(self.remote, "rev-parse", "refs/heads/repair").strip(), self.base)

    def test_rename_source_delete_and_untracked_paths_are_checked(self):
        repair.git(self.repo, "mv", "outside.txt", "allowed/moved.txt")
        with self.assertRaisesRegex(ValueError, "outside-file-scope:outside.txt"):
            repair.assert_scope(self.repo, self.context)
        repair.git(self.repo, "reset", "--hard", self.base)
        (self.repo / "outside.txt").unlink()
        with self.assertRaisesRegex(ValueError, "outside-file-scope:outside.txt"):
            repair.assert_scope(self.repo, self.context)
        repair.git(self.repo, "reset", "--hard", self.base)
        (self.repo / "untracked.txt").write_text("forbidden")
        with self.assertRaisesRegex(ValueError, "outside-file-scope:untracked.txt"):
            repair.assert_scope(self.repo, self.context)

    def test_unsupported_globs_fail_closed_and_segment_semantics_match(self):
        for pattern in ("../*", "[ab]", "!private/**", "/absolute"):
            with self.assertRaises(ValueError):
                repair.pattern_regex(pattern)
        self.assertIsNone(repair.pattern_regex("allowed/*").fullmatch("allowed/nested/a.txt"))
        self.assertIsNotNone(repair.pattern_regex("**/*.txt").fullmatch("one.txt"))

    def test_reverted_forbidden_commit_is_not_published_in_history(self):
        (self.repo / "outside.txt").write_text("forbidden intermediate content\n")
        forbidden = self.commit("forbidden")
        repair.git(self.repo, "revert", "--no-edit", forbidden)
        head = self.commit("allowed final")
        with self.assertRaisesRegex(ValueError, "outside-file-scope:outside.txt"):
            repair.publish(self.repo, self.context, head)
        self.assertEqual(repair.git(self.remote, "rev-parse", "refs/heads/repair").strip(), self.base)

    def test_missing_bound_context_cannot_fall_back_to_legacy_publish(self):
        with patch.dict(os.environ, {"FACTORY_REPAIR_ATTEMPT_ID": "repair:one", "FACTORY_REPAIR_PUBLICATION": ""}):
            with self.assertRaisesRegex(ValueError, "context is missing"):
                repair.from_environment({}, {})

    def test_production_ship_and_round_stop_on_stale_head_before_local_effects(self):
        head = self.commit("outside advance")
        repair.git(self.repo, "push", str(self.remote), head + ":refs/heads/repair")
        context = {"binding": {"repositoryRoot": str(self.repo)}, "profile": {}, "repairPublication": self.context}
        artifacts = self.root / "artifacts"
        artifacts.mkdir()
        with patch.object(project, "load_context", return_value=context), patch.object(project.repair_publication, "from_environment", return_value=self.context):
            for action in (lambda: project.begin_round(artifacts), lambda: project.ship(artifacts, {"title": "Repair", "body": "Repair"})):
                with self.assertRaisesRegex(ValueError, "remote-head-changed"):
                    action()
        self.assertEqual(list(artifacts.iterdir()), [])
        self.assertEqual(repair.git(self.repo, "rev-parse", "HEAD").strip(), head)

    def test_production_ship_repairs_exact_existing_pr_and_preserves_draft_state(self):
        for draft in (False, True):
            with self.subTest(draft=draft):
                base = repair.git(self.repo, "rev-parse", "HEAD").strip()
                context = {**self.context, "executionBaseRevision": base, "repository": {"provider": "github", "owner": "example", "name": "fixture"}, "pullRequestNumber": 42}
                binding = {"repositoryRoot": str(self.repo), "allowPublish": True, "baseCommit": base, "branch": "repair"}
                profile = {"repository": {"remote": "https://github.com/example/fixture.git"}, "verification": [{"id": "fixture"}]}
                run = {"binding": binding, "profile": profile, "repairPublication": context}
                artifacts = self.root / ("draft-" + str(draft))
                artifacts.mkdir()
                (self.repo / "allowed/one.txt").write_text("new repair " + str(draft))
                product = {"files": [{"path": "allowed/one.txt"}], "sha256": "fixture"}
                (artifacts / "review.json").write_text(json.dumps({"ready": True, "verification": {"workProduct": product}}))
                original_run = subprocess.run
                requests = []
                def gh_fixture(argv, **kwargs):
                    if argv[0] != "gh":
                        return original_run(argv, **kwargs)
                    requests.append(argv)
                    self.assertEqual(argv, ["gh", "api", "--method", "GET", "repos/example/fixture/pulls/42"])
                    head = repair.git(self.remote, "rev-parse", "refs/heads/repair").strip()
                    response = {"number": 42, "state": "open", "merged": False, "draft": draft, "html_url": "https://github.com/example/fixture/pull/42", "head": {"sha": head, "ref": "repair", "repo": {"full_name": "example/fixture"}}, "base": {"repo": {"full_name": "example/fixture"}}}
                    return subprocess.CompletedProcess(argv, 0, json.dumps(response), "")
                with patch.object(project, "load_context", return_value=run), patch.object(project, "work_product", return_value=product), patch.object(project, "plan_for", return_value={"goal": "Fix feedback"}), patch.object(project.repair_publication, "from_environment", return_value=context), patch.object(subprocess, "run", side_effect=gh_fixture):
                    result = project.ship(artifacts, {"title": "Resolve feedback", "body": "Verified repair"})
                self.assertEqual(result["draft"], draft)
                self.assertEqual(result["url"], "https://github.com/example/fixture/pull/42")
                self.assertEqual(len(requests), 2)
                self.assertEqual(repair.git(self.remote, "rev-parse", "refs/heads/repair").strip(), result["head"])

    def test_non_draft_repair_still_requires_bound_human_merge_review(self):
        artifacts = self.root / "merge-review"
        artifacts.mkdir()
        manifest = {"kind": "factory-merge-review.v1"}
        project.write(artifacts / "merge-review-manifest.json", manifest)
        project.write(artifacts / "pr-evidence.json", {"ready": True, "draft": False, "mergeReviewManifestPath": str(artifacts / "merge-review-manifest.json"), "mergeReviewManifest": manifest})
        self.assertTrue(project.merge_review_route(artifacts)["required"])

    def test_revoked_authority_after_coding_prevents_publication(self):
        head = self.commit("finished coding")
        repair.assert_current_authority.side_effect = ValueError("Repair publication authority rejected")
        with self.assertRaisesRegex(ValueError, "authority rejected"):
            repair.publish(self.repo, self.context, head)
        self.assertEqual(repair.git(self.remote, "rev-parse", "refs/heads/repair").strip(), self.base)


class RepairAuthorityBrokerTest(unittest.TestCase):
    def test_loopback_request_is_bound_and_denial_cannot_be_ignored(self):
        context = {"repairAttemptId": "repair:one", "managedPrId": "pr:one", "repository": {"provider": "github", "owner": "example", "name": "fixture"}, "branch": "repair", "executionBaseRevision": "a" * 40, "originalReadyBaseRevision": "b" * 40, "readySnapshotId": "ready:one", "readyDigest": "sha256:" + "c" * 64, "fileGlobs": ["allowed/**"]}
        requests = []
        response = {"ok": True, "repairAttemptId": "repair:one", "managedPrId": "pr:one", "headSha": "a" * 40}
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append({"path": self.path, "authorization": self.headers.get("Authorization"), "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(response).encode())
            def log_message(self, *_args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.dict(os.environ, {"FACTORY_REPAIR_AUTHORITY_URL": "http://127.0.0.1:" + str(server.server_port) + "/repair-publication/preflight", "FACTORY_REPAIR_AUTHORITY_TOKEN": "invocation-only-capability", "FACTORY_JOB_ID": "job:one"}):
                repair.assert_current_authority(context)
                response["headSha"] = "d" * 40
                with self.assertRaisesRegex(ValueError, "binding mismatch"):
                    repair.assert_current_authority(context)
            self.assertEqual(requests[0]["authorization"], "Bearer invocation-only-capability")
            self.assertEqual(requests[0]["body"]["factoryJobId"], "job:one")
            self.assertEqual(requests[0]["body"]["expectedHead"]["headSha"], context["executionBaseRevision"])
            self.assertNotIn("TOKEN", json.dumps(context))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_missing_or_remote_broker_fails_closed(self):
        for endpoint in ("", "http://example.com/repair-publication/preflight", "http://127.0.0.1:8/redirect"):
            with patch.dict(os.environ, {"FACTORY_REPAIR_AUTHORITY_URL": endpoint, "FACTORY_REPAIR_AUTHORITY_TOKEN": "ephemeral", "FACTORY_JOB_ID": "job:one"}):
                with self.assertRaisesRegex(ValueError, "broker unavailable"):
                    repair.assert_current_authority({})


if __name__ == "__main__":
    unittest.main()
