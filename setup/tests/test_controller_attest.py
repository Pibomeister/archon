#!/usr/bin/env python3
import hashlib
import hmac
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parent.parent
CONTRACT = SETUP / "bugfix-contract.py"
SCRIPT = SETUP / "controller-attest.py"
FIXTURE = SETUP / "tests" / "fixtures" / "rca-minimal"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class AttestControllerTest(unittest.TestCase):
    def fixture(self, root: Path, provider="claude"):
        ad = root / "artifacts" / ("a" * 32)
        shutil.copytree(FIXTURE, ad)
        (ad / "rca.md").write_text("x")
        (ad / "causal-chain.json").write_text('{"links":[]}')
        (ad / "evidence-manifest.json").write_text('{"sources":{}}')
        chain = json.loads((ad / "bugfix-chain.json").read_text())
        chain.update({"provider": provider, "current_run_id": ad.name, "root_run_id": ad.name})
        (ad / "bugfix-chain.json").write_text(json.dumps(chain))
        secret = "s" * 48
        state = {
            "logical_chain_id": chain["logical_chain_id"],
            "current_run_id": ad.name,
            "chain_secret": secret,
        }
        state["state_mac"] = hmac.new(secret.encode(), canonical(state), hashlib.sha256).hexdigest()
        state_path = root / "state.json"
        state_path.write_text(json.dumps(state))
        state_path.chmod(0o600)
        return ad, state_path

    def run(self, *args):
        return subprocess.run(["python3", *map(str, args)], capture_output=True, text=True)

    def test_proof_seal_is_private_external_and_verified(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ad, state = self.fixture(root)
            result = self.run(CONTRACT, "write-proof-manifest", "--artifacts", ad)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            out = root / "private" / "proof.json"
            created = self.run(SCRIPT, "proof", "--artifacts", ad, "--chain-state", state, "--out", out)
            self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
            self.assertEqual(out.stat().st_mode & 0o777, 0o600)
            self.assertIn("authority_mac", json.loads(out.read_text()))
            verified = self.run(SCRIPT, "proof", "--verify", "--artifacts", ad, "--chain-state", state, "--out", out)
            self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)

    def test_approval_seal_binds_final_critic_manifest_role_and_mac(self):
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                ad, state = self.fixture(root, provider)
                self.assertEqual(self.run(CONTRACT, "write-proof-manifest", "--artifacts", ad).returncode, 0)
                self.assertEqual(self.run(CONTRACT, "write-approval-manifest", "--artifacts", ad).returncode, 0)
                (ad / "rca-round.txt").write_text("1\n")
                critic = ad / "rca-round-1"
                critic.mkdir()
                (critic / "critique.json").write_text('{"verdict":"ACCEPT"}')
                out = root / "private" / "approval.json"
                created = self.run(SCRIPT, "approval", "--artifacts", ad, "--chain-state", state, "--out", out)
                self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
                seal = json.loads(out.read_text())
                self.assertEqual(seal["role"], "final-critic")
                self.assertEqual(seal["provider"], provider)
                self.assertEqual(self.run(SCRIPT, "approval", "--verify", "--artifacts", ad, "--chain-state", state, "--out", out).returncode, 0)
                seal["run_id"] = "b" * 32
                out.write_text(json.dumps(seal))
                out.chmod(0o600)
                tampered = self.run(SCRIPT, "approval", "--verify", "--artifacts", ad, "--chain-state", state, "--out", out)
                self.assertNotEqual(tampered.returncode, 0)

    def test_rejected_final_critic_cannot_issue_approval_seal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ad, state = self.fixture(root)
            self.assertEqual(self.run(CONTRACT, "write-proof-manifest", "--artifacts", ad).returncode, 0)
            self.assertEqual(self.run(CONTRACT, "write-approval-manifest", "--artifacts", ad).returncode, 0)
            (ad / "rca-round.txt").write_text("1\n")
            critic = ad / "rca-round-1"
            critic.mkdir()
            (critic / "critique.json").write_text('{"verdict":"REVISE"}')
            result = self.run(SCRIPT, "approval", "--artifacts", ad, "--chain-state", state, "--out", root / "approval.json")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("did not ACCEPT", result.stdout)

    def test_lite_approval_seal_is_current_and_tamper_evident(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ad, state = self.fixture(root)
            self.assertEqual(self.run(CONTRACT, "write-proof-manifest", "--artifacts", ad).returncode, 0)
            self.assertEqual(self.run(CONTRACT, "write-approval-manifest", "--artifacts", ad).returncode, 0)
            self.assertEqual(self.run(CONTRACT, "write-lite-approval-manifest", "--artifacts", ad).returncode, 0)
            out = root / "private" / "lite.json"
            created = self.run(SCRIPT, "lite-approval", "--artifacts", ad, "--chain-state", state, "--out", out)
            self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
            self.assertEqual(self.run(SCRIPT, "lite-approval", "--verify", "--artifacts", ad, "--chain-state", state, "--out", out).returncode, 0)
            plan = json.loads((ad / "fix-plan.json").read_text())
            plan["approach"] = "stale gate edit"
            (ad / "fix-plan.json").write_text(json.dumps(plan))
            current = self.run(CONTRACT, "validate-current-manifest", "--artifacts", ad, "--kind", "lite-approval")
            self.assertNotEqual(current.returncode, 0)


if __name__ == "__main__":
    unittest.main()
