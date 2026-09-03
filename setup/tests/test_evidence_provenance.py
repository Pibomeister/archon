import hashlib
import hmac
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "evidence-provenance.py"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class EvidenceTest(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(["python3", str(SCRIPT), *map(str, args)], capture_output=True, text=True)

    def state(self, root):
        secret = "s" * 48
        value = {"logical_chain_id": "chain", "current_run_id": "run", "chain_secret": secret}
        value["state_mac"] = hmac.new(secret.encode(), canonical(value), hashlib.sha256).hexdigest()
        path = root / "state.json"
        path.write_text(json.dumps(value)); path.chmod(0o600)
        return path

    def test_record_hashes_and_cleanup_expires_exact_file(self):
        with tempfile.TemporaryDirectory() as td:
            artifacts = Path(td); (artifacts / "evidence").mkdir(); (artifacts / "evidence/x.txt").write_text("secret")
            result = self.run_cli("record", "--artifacts", artifacts, "--source", "db", "--status", "complete", "--file", "evidence/x.txt", "--query", "SELECT 1", "--now", "2026-01-01T00:00:00Z")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            document = json.loads((artifacts / "evidence-provenance.json").read_text())
            self.assertEqual(document["sources"][0]["bytes"], 6)
            self.assertEqual(document["sources"][0]["completeness"], "complete")
            self.assertEqual((artifacts / "evidence-provenance.json").stat().st_mode & 0o777, 0o600)
            result = self.run_cli("cleanup", "--artifacts", artifacts, "--now", "2026-02-01T00:00:01Z")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse((artifacts / "evidence/x.txt").exists())
            audit = json.loads((artifacts / "evidence-provenance.json").read_text())["sources"][0]
            self.assertIsNone(audit["file"])
            self.assertEqual(audit["bytes"], 0)
            self.assertIsNotNone(audit["output_sha256"])

    def test_collection_seal_rejects_post_collection_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); artifacts = root / "artifacts"; artifacts.mkdir(); (artifacts / "e.txt").write_text("original")
            state = self.state(root); seal = root / "private" / "evidence.json"
            self.assertEqual(self.run_cli("record", "--artifacts", artifacts, "--source", "db", "--status", "complete", "--file", "e.txt").returncode, 0)
            self.assertEqual(self.run_cli("seal", "--artifacts", artifacts, "--chain-state", state, "--out", seal).returncode, 0)
            self.assertEqual(self.run_cli("verify", "--artifacts", artifacts, "--chain-state", state, "--out", seal).returncode, 0)
            (artifacts / "e.txt").write_text("mutated")
            result = self.run_cli("verify", "--artifacts", artifacts, "--chain-state", state, "--out", seal)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("mutated after collection", result.stdout)

    def test_occurrence_watermark_change_invalidates_prior_epoch_only(self):
        with tempfile.TemporaryDirectory() as td:
            artifacts = Path(td); (artifacts / "a").write_text("v1"); (artifacts / "b").write_text("v2")
            base = ["record", "--artifacts", artifacts, "--source", "entity", "--status", "complete", "--evidence-kind", "occurrence", "--query", "SELECT 1", "--baseline", "a" * 40, "--occurrence-window", '{"start":"2026-01-01T00:00:00Z","end":"2026-01-02T00:00:00Z"}']
            self.assertEqual(self.run_cli(*base, "--file", "a", "--entity-watermark", "1", "--now", "2026-01-01T00:00:00Z").returncode, 0)
            self.assertEqual(self.run_cli(*base, "--file", "b", "--entity-watermark", "2", "--now", "2026-01-02T00:00:00Z").returncode, 0)
            rows = json.loads((artifacts / "evidence-provenance.json").read_text())["sources"]
            self.assertFalse(rows[0]["occurrence_attribution_valid"])
            self.assertTrue(rows[1]["occurrence_attribution_valid"])

    def test_complete_probe_without_window_stays_class_only(self):
        with tempfile.TemporaryDirectory() as td:
            artifacts = Path(td); (artifacts / "result").write_text("count=10")
            result = self.run_cli("record", "--artifacts", artifacts, "--source", "prod-probes",
                                  "--status", "complete", "--file", "result", "--query", "SELECT count(*)",
                                  "--baseline", "a" * 40, "--evidence-kind", "class")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            row = json.loads((artifacts / "evidence-provenance.json").read_text())["sources"][0]
            self.assertEqual(row["evidence_kind"], "class")
            self.assertIsNone(row["occurrence_window"])
            rejected = self.run_cli("record", "--artifacts", artifacts, "--source", "bad-occurrence",
                                    "--status", "complete", "--file", "result", "--query", "SELECT count(*)",
                                    "--baseline", "a" * 40, "--evidence-kind", "occurrence",
                                    "--entity-watermark", "entity-v1", "--occurrence-window", "null")
            self.assertNotEqual(rejected.returncode, 0)

    def test_failed_source_cannot_support_negative_product_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            result = self.run_cli("record", "--artifacts", td, "--source", "db", "--status", "timed-out", "--supports-negative")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("cannot be negative", result.stdout)

    def test_sensitive_canaries_are_redacted_before_downstream_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            artifacts = Path(td)
            canary = "person@example.com password=hunter2 123e4567-e89b-12d3-a456-426614174000"
            (artifacts / "raw.txt").write_text(canary)
            result = self.run_cli("record", "--artifacts", artifacts, "--source", "prod",
                                  "--status", "complete", "--file", "raw.txt")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            rendered = (artifacts / "raw.txt").read_text()
            manifest = (artifacts / "evidence-provenance.json").read_text()
            for secret in ("person@example.com", "hunter2", "123e4567-e89b-12d3-a456-426614174000"):
                self.assertNotIn(secret, rendered)
                self.assertNotIn(secret, manifest)
            self.assertIn("REDACTED_EMAIL", rendered)
            self.assertIn("REDACTED_SECRET", rendered)
            self.assertIn("REDACTED_ID", rendered)

    def test_traversal_symlink_and_oversize_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = self.run_cli("record", "--artifacts", root, "--source", "x", "--status", "complete", "--file", "../x")
            self.assertNotEqual(result.returncode, 0)
            target = root / "target"; target.write_text("x")
            (root / "link").symlink_to(target)
            result = self.run_cli("record", "--artifacts", root, "--source", "x", "--status", "complete", "--file", "link")
            self.assertNotEqual(result.returncode, 0)
            (root / "large").write_bytes(b"x" * (1024 * 1024 + 1))
            result = self.run_cli("record", "--artifacts", root, "--source", "x", "--status", "complete", "--file", "large")
            self.assertNotEqual(result.returncode, 0)

    def test_required_probe_source_fails_closed_when_recording_did_not_land(self):
        with tempfile.TemporaryDirectory() as td:
            artifacts = Path(td)
            (artifacts / "evidence-provenance.json").write_text('{"schema_version":2,"sources":[]}')
            result = self.run_cli("validate", "--artifacts", artifacts,
                                  "--require-source", "prod-probes",
                                  "--require-file", "probe-results.txt")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("required evidence source missing", result.stdout)


if __name__ == "__main__":
    unittest.main()
