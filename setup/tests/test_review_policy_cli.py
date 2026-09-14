import hashlib
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import review_policy_cli as cli


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_retry_is_idempotent_but_cannot_overwrite_evidence(self):
        path = cli.checkpoint(self.root, "reviewer-one", {"raw": "first response"})
        cli.checkpoint(self.root, "reviewer-one", {"raw": "first response"})
        with self.assertRaisesRegex(ValueError, "checkpoint conflict"):
            cli.checkpoint(self.root, "reviewer-one", {"raw": "replacement"})
        self.assertEqual(json.loads(path.read_text()), {"raw": "first response"})

    def test_rejects_traversal_and_symlink(self):
        with self.assertRaisesRegex(ValueError, "invalid checkpoint"):
            cli.checkpoint(self.root, "../outside", {})
        (self.root / "linked.json").symlink_to(self.root / "other.json")
        with self.assertRaisesRegex(ValueError, "symlink"):
            cli.checkpoint(self.root, "linked", {})

    def test_malformed_normalization_retains_raw_and_unresolved_attempt(self):
        raw = "P1 blocker B-1 cannot be ignored"
        request = {"source_response": {"raw_output": raw, "blocker_ids": ["B-1"]},
                   "format_retry_attempts": 0,
                   "normalized": {"source_raw_output_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                                  "findings": []}}
        with self.assertRaisesRegex(ValueError, "dropped source blockers"):
            cli.normalize(self.root, "r1", request)
        source = json.loads((self.root / "r1.source.json").read_text())
        self.assertEqual(source["raw_output"], raw)
        self.assertEqual(json.loads((self.root / "r1.unresolved-0.json").read_text())["status"], "unresolved")
        self.assertFalse((self.root / "r1.normalized.json").exists())

    def test_second_format_retry_is_blocked_with_original_retained(self):
        request = {"source_response": {"raw_output": "broken"}, "format_retry_attempts": 2}
        with self.assertRaisesRegex(ValueError, "one format-only retry"):
            cli.normalize(self.root, "r1", request)
        self.assertEqual(json.loads((self.root / "r1.source.json").read_text())["raw_output"], "broken")

    def test_ledger_cannot_drop_or_retitle_a_finding_into_another_identity(self):
        finding = {"finding_id": "F-1", "violated_invariant": "no disclosure",
                   "affected_paths": ["claim.ts"], "severity": "P1", "evidence": "claim accepted",
                   "owning_repository": "api", "status": "open"}
        with self.assertRaisesRegex(ValueError, "dropped finding F-1"):
            cli.ledger_revision([finding], [])
        with self.assertRaisesRegex(ValueError, "immutable finding field"):
            cli.ledger_revision([finding], [{**finding, "severity": "P3"}])
        self.assertEqual(cli.ledger_revision([finding], [finding])["findings"][0]["finding_id"], "F-1")

    def test_brief_binds_actual_diff_and_context_and_rejects_dirty_checkout(self):
        repo = self.root / "repo"
        repo.mkdir()
        def git(*args):
            return subprocess.run(["git", "-C", str(repo), *args], check=True,
                                  capture_output=True, text=True).stdout.strip()
        git("init", "-q")
        git("config", "user.email", "test@example.com")
        git("config", "user.name", "Test")
        source = repo / "claim.txt"
        source.write_text("unsafe\n")
        git("add", ".")
        git("commit", "-qm", "fixture base")
        base = git("rev-parse", "HEAD")
        source.write_text("safe\n")
        git("commit", "-qam", "fixture repair")
        head = git("rev-parse", "HEAD")
        (self.root / "plan.md").write_text("no disclosure")
        request = {"change": {"mode": "initial", "candidate": {"repo": "api", "base": base, "head": head}},
                   "approved_plan": "plan.md", "invariants": ["no disclosure"], "open_finding_ids": ["F-1"],
                   "evidence": [{"path": "plan.md", "sha256": hashlib.sha256(b"no disclosure").hexdigest()}]}
        brief = cli.prepare(request, self.root, repo)
        self.assertIn("-unsafe\n+safe", brief["diff"])
        request["open_finding_ids"].append("F-2")
        self.assertNotEqual(brief["context_digest"], cli.prepare(request, self.root, repo)["context_digest"])
        source.write_text("unreviewed\n")
        with self.assertRaisesRegex(ValueError, "clean exact candidate"):
            cli.prepare(request, self.root, repo)


if __name__ == "__main__":
    unittest.main()
