#!/usr/bin/env python3
"""reopen-gate.py: the reopen reason is bound, and a reopened stage cannot pass
unchanged or silently outside its allowlist. The gate-tests node wiring is
covered in test_node_stress.GateTestsStress."""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

GATE = Path(__file__).resolve().parents[1] / "reopen-gate.py"


class ReopenGate(unittest.TestCase):
    def artifacts(self, context=b'{"reason": "x"}\n', bound=True):
        ad = Path(tempfile.mkdtemp(prefix="reopen-gate-"))
        params = {"run_id": "aab1f254", "logical_chain_id": "42b42a13"}
        if context is not None:
            (ad / "reopen-context.json").write_bytes(context)
        if bound:
            params["feature_reopen_context_sha256"] = hashlib.sha256(context or b"").hexdigest()
        (ad / "params.json").write_text(json.dumps(params), encoding="utf-8")
        return ad

    def gate(self, ad, outcome):
        return subprocess.run([sys.executable, str(GATE), str(ad), outcome], capture_output=True, text=True)

    def test_without_a_bound_context_it_skips_even_on_no_change(self):
        p = self.gate(self.artifacts(bound=False), "NO_CHANGE")
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("REOPEN_GATE=SKIP", p.stdout)

    def test_verify_only_needs_a_change_since_the_previous_head(self):
        ad = self.artifacts(context=None, bound=False)
        params = json.loads((ad / "params.json").read_text(encoding="utf-8"))
        params.update(feature_verify_only="yes", feature_previous_head="3945463acb8d936efb36cef997cd76a8adefde3a")
        (ad / "params.json").write_text(json.dumps(params), encoding="utf-8")
        p = self.gate(ad, "NO_CHANGE")
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("IMPLEMENT=FAIL verify-only reopen has no change since previous head 3945463acb8d", p.stdout)
        p = self.gate(ad, "CHANGED")
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("REOPEN_GATE=PASS verify-only", p.stdout)

    def test_no_change_with_context_fails_and_a_change_passes(self):
        ad = self.artifacts()
        p = self.gate(ad, "NO_CHANGE")
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("IMPLEMENT=FAIL reopen produced no change", p.stdout)
        p = self.gate(ad, "CHANGED")
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("REOPEN_GATE=PASS", p.stdout)

    def test_an_altered_or_missing_context_fails(self):
        ad = self.artifacts()
        (ad / "reopen-context.json").write_bytes(b'{"reason": "edited"}\n')
        p = self.gate(ad, "CHANGED")
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("IMPLEMENT=FAIL reopen-context.json missing or altered", p.stdout)
        (ad / "reopen-context.json").unlink()
        self.assertIn("missing or altered", self.gate(ad, "CHANGED").stdout)

    def test_a_blocked_fix_fails_even_with_a_partial_change(self):
        ad = self.artifacts()
        (ad / "reopen-blocked.txt").write_text("apps/api/src/main.ts\n", encoding="utf-8")
        p = self.gate(ad, "CHANGED")
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("IMPLEMENT=FAIL reopen needs files outside the approved allowlist: apps/api/src/main.ts", p.stdout)
        self.assertIn("feature-scope-amend aab1f254 --chain 42b42a13", p.stdout)
        # Negative control: an empty note is not a block.
        (ad / "reopen-blocked.txt").write_text("\n", encoding="utf-8")
        self.assertEqual(self.gate(ad, "CHANGED").returncode, 0)


class ImplementPromptCarriesReopenContext(unittest.TestCase):
    def prompt(self, workflow):
        import yaml
        doc = yaml.safe_load((GATE.parents[1] / "workflows" / f"{workflow}.yaml").read_text(encoding="utf-8"))
        return next(n["prompt"] for n in doc["nodes"] if n["id"] == "implement")

    def test_every_repository_stage_lane_makes_the_reopen_context_mandatory(self):
        for workflow in ("full-sdlc-api", "full-sdlc-api-codex", "full-sdlc-web", "full-sdlc-web-codex"):
            with self.subTest(workflow=workflow):
                text = self.prompt(workflow)
                self.assertIn("reopen-context.json exists in ARTIFACTS_DIR", text)
                self.assertIn("MANDATORY", text)
                self.assertIn("IMPLEMENT=FAIL reopen produced no change", text)
                self.assertIn("reopen-blocked.txt", text)


if __name__ == "__main__":
    unittest.main()
