import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import feature_chain as fc
import review_session as rs


class ReviewSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.control = self.root / "control"
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.chain_id, self.run_id = "a" * 32, "b" * 32
        self.state = {"schema_version": 2, "logical_chain_id": self.chain_id,
                      "chain_secret": "s" * 64, "provider": "codex",
                      "current_run": {"run_id": self.run_id, "artifacts_dir": str(self.artifacts)},
                      "review_policy": {"qualification_status": "qualified"}}
        fc.write_state(self.control, self.state)
        self.candidate = {"repo": "api", "base": "c" * 40, "head": "d" * 40}
        self.assignment = {"slot": 1, "status": "pending", "candidate": self.candidate, "context_digest": "e" * 64}
        self.current = {"round": 7, "candidate": self.candidate, "assignments": [self.assignment]}
        (self.artifacts / "current-review.json").write_text(json.dumps(self.current))
        env = mock.patch.dict(os.environ, {"ARCHON_CONTROL_DIR": str(self.control), "ARCHON_FEATURE_CHAIN_ID": self.chain_id})
        env.start()
        self.addCleanup(env.stop)

    def bind(self, session="actual-thread-123"):
        return rs.bind(self.control, self.chain_id, self.run_id, session, 1, "reviewer", "gpt-5.6-sol", "medium")

    def test_actual_session_receipt_is_required_and_false_label_rejected(self):
        with self.assertRaisesRegex(ValueError, "missing or ambiguous"):
            rs.verify_session_provenance(self.artifacts, self.current, self.assignment, {})
        self.bind()
        verified = rs.verify_session_provenance(self.artifacts, self.current, self.assignment, {})
        self.assertEqual(verified["session_id"], "actual-thread-123")
        with self.assertRaisesRegex(ValueError, "missing or ambiguous"):
            rs.verify_session_provenance(self.artifacts, self.current, self.assignment, {"reviewer_id": "pretend-independent"})

    def test_changed_context_and_tampered_private_receipt_are_rejected(self):
        self.bind()
        with self.assertRaisesRegex(ValueError, "missing or ambiguous"):
            rs.verify_session_provenance(self.artifacts, self.current, {**self.assignment, "context_digest": "f" * 64}, {})
        path = next((self.control / "review-sessions").glob("*.json"))
        data = json.loads(path.read_text())
        data["session_id"] = "replacement-thread"
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "MAC mismatch"):
            rs.verify_session_provenance(self.artifacts, self.current, self.assignment, {})

    def test_wrong_model_and_unqualified_run_cannot_issue_provenance(self):
        with self.assertRaisesRegex(ValueError, "Sol/medium"):
            rs.bind(self.control, self.chain_id, self.run_id, "actual-thread-123", 1, "reviewer", "other-model", "medium")
        self.state["review_policy"]["qualification_status"] = "unqualified"
        fc.write_state(self.control, self.state)
        with self.assertRaisesRegex(ValueError, "qualified policy"):
            self.bind()

    def test_fixer_session_cannot_review_its_own_repair_under_another_slot(self):
        self.bind()
        rs.bind(self.control, self.chain_id, self.run_id, "actual-thread-123", None,
                "fixer", "gpt-5.6-sol", "medium")
        with self.assertRaisesRegex(ValueError, "also authored"):
            rs.verify_session_provenance(self.artifacts, self.current, self.assignment, {})


if __name__ == "__main__":
    unittest.main()
