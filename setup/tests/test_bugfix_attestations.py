#!/usr/bin/env python3
import importlib.util
import hashlib
import hmac
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parent.parent
SCRIPT = SETUP / "bugfix-contract.py"
CONTROLLER = SETUP / "controller-attest.py"
FIXTURE = SETUP / "tests" / "fixtures" / "rca-minimal"


def load_module():
    spec = importlib.util.spec_from_file_location("bugfix_contract", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(path: Path, value):
    if isinstance(value, str):
        path.write_text(value, encoding="utf-8")
    else:
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


class BugfixAttestationTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.ad = Path(self.td.name) / "artifacts"
        shutil.copytree(FIXTURE, self.ad)
        write(self.ad / "rca.md", "## Observation\noff-by-one on last page\nConfidence: High\n")
        write(self.ad / "causal-chain.json", {"links": [
            {"index": 1, "cause": "off-by-one on last page", "evidence": {"source": "report", "file": "bug-report.md", "quote": "off-by-one on last page"}},
            {"index": 2, "cause": "exclusive bound at root", "evidence": {"source": "code", "file": "src/foo.ts", "quote": "exclusive bound"}, "fixable": True, "fix_site": "src/foo.ts:42"},
        ]})
        write(self.ad / "evidence-manifest.json", {"sources": {"report": {"status": "gathered", "file": "bug-report.md"}}})
        self.bc = load_module()

    def run_contract(self, *args):
        return subprocess.run(["python3", str(SCRIPT), *args], cwd=SETUP.parent, text=True, capture_output=True)

    def test_manifest_hashes_are_stable_for_json_key_order(self):
        r = self.run_contract("write-proof-manifest", "--artifacts", str(self.ad))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        h1 = json.loads((self.ad / "proof-manifest.json").read_text())["artifact_hashes"]["hypotheses.json"]["sha256"]
        write(self.ad / "hypotheses.json", [{"note": "RED test reproduced predicted signature", "status": "confirmed-by-experiment", "hypothesis": "last page bound is exclusive", "id": "H1"}])
        r = self.run_contract("write-proof-manifest", "--artifacts", str(self.ad))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        h2 = json.loads((self.ad / "proof-manifest.json").read_text())["artifact_hashes"]["hypotheses.json"]["sha256"]
        self.assertEqual(h1, h2)

    def test_stale_proof_manifest_fails_current_check(self):
        self.assertEqual(self.run_contract("write-proof-manifest", "--artifacts", str(self.ad)).returncode, 0)
        write(self.ad / "proof-assessment.json", {**json.loads((self.ad / "proof-assessment.json").read_text()), "occurrence_attributed": False})
        r = self.run_contract("validate-current-manifest", "--artifacts", str(self.ad), "--kind", "proof")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("stale proof manifest", r.stdout)

    def test_experiment_assessment_mutation_invalidates_proof_manifest(self):
        self.assertEqual(self.run_contract("write-proof-manifest", "--artifacts", str(self.ad)).returncode, 0)
        assessment = json.loads((self.ad / "experiment-assessment.json").read_text())
        assessment["verdict"] = "conflict"
        write(self.ad / "experiment-assessment.json", assessment)
        r = self.run_contract("validate-current-manifest", "--artifacts", str(self.ad), "--kind", "proof")
        self.assertNotEqual(r.returncode, 0)

    def test_external_controller_attestation_allows_current_manifest(self):
        self.assertEqual(self.run_contract("write-proof-manifest", "--artifacts", str(self.ad)).returncode, 0)
        seal = Path(self.td.name) / "private-proof-seal.json"
        chain = json.loads((self.ad / "bugfix-chain.json").read_text())
        state = {"logical_chain_id": chain["logical_chain_id"], "current_run_id": chain["current_run_id"], "chain_secret": "s" * 48}
        payload = json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
        state["state_mac"] = hmac.new(state["chain_secret"].encode(), payload, hashlib.sha256).hexdigest()
        state_path = Path(self.td.name) / "chain.json"
        write(state_path, state)
        state_path.chmod(0o600)
        created = subprocess.run([
            "python3", str(CONTROLLER), "proof", "--artifacts", str(self.ad),
            "--chain-state", str(state_path), "--out", str(seal),
        ], text=True, capture_output=True)
        self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
        verified = subprocess.run([
            "python3", str(CONTROLLER), "proof", "--artifacts", str(self.ad),
            "--chain-state", str(state_path), "--out", str(seal), "--verify",
        ], text=True, capture_output=True)
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        r = self.run_contract("validate-attestation", "--artifacts", str(self.ad), "--kind", "proof", "--role", "blind-verifier", "--seal", str(seal))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("BUGFIX_ATTESTATION=OK", r.stdout)

        doc = json.loads(seal.read_text())
        doc["authority_mac"] = "0" * 64
        write(seal, doc)
        verified = subprocess.run([
            "python3", str(CONTROLLER), "proof", "--artifacts", str(self.ad),
            "--chain-state", str(state_path), "--out", str(seal), "--verify",
        ], text=True, capture_output=True)
        self.assertNotEqual(verified.returncode, 0)

    def test_artifact_side_or_world_readable_attestation_is_refused(self):
        self.assertEqual(self.run_contract("write-proof-manifest", "--artifacts", str(self.ad)).returncode, 0)
        manifest = json.loads((self.ad / "proof-manifest.json").read_text())
        seal = self.ad / "proof-seal.json"
        write(seal, {"schema_version": 1, "authority": "controller", "manifest_type": "proof", "role": "blind-verifier", "provider": manifest["provider"], "chain_id": manifest["chain_id"], "run_id": manifest["run_id"], "manifest_hash": manifest["semantic_hash"], "verdict": "ACCEPT"})
        seal.chmod(0o600)
        r = self.run_contract("validate-attestation", "--artifacts", str(self.ad), "--kind", "proof", "--role", "blind-verifier", "--seal", str(seal))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("external to artifacts", r.stdout)
        outside = Path(self.td.name) / "world.json"
        shutil.copyfile(seal, outside)
        outside.chmod(0o644)
        r = self.run_contract("validate-attestation", "--artifacts", str(self.ad), "--kind", "proof", "--role", "blind-verifier", "--seal", str(outside))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("group/world accessible", r.stdout)


if __name__ == "__main__":
    unittest.main()
