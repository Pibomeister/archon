#!/usr/bin/env python3
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from nodes.extract import runnable_body

ARCHON = Path(__file__).resolve().parents[2]
ROOT = ARCHON.parent


def canonical_digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_node(workflow, node_id, artifacts_dir):
    env = dict(os.environ, ARTIFACTS_DIR=str(artifacts_dir))
    return subprocess.run(["bash", "-c", runnable_body(workflow, node_id)], capture_output=True, text=True, env=env)


def init_git_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "README.md").write_text("test\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)
    return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()


def make_web_plan_artifacts(tmp):
    ad = tmp / "artifacts"
    apiwt = tmp / "apiwt"
    webwt = tmp / "webwt"
    ad.mkdir()
    api_head = init_git_repo(apiwt)
    webwt.mkdir()
    spec = tmp / "spec.md"
    spec.write_text("# Web feature\n", encoding="utf-8")
    write_json(ad / "params.json", {
        "feature_scope": "web",
        "api_head_sha": api_head,
        "api_worktree": str(apiwt),
        "spec": str(spec),
        "worktree": str(webwt),
    })
    (ad / "plan.md").write_text("## Scope\nweb only\n## Files\n- app/routes/feature.tsx\n## Verification\n- browser-1\n## Risks\nnone\n## No-change disposition\nchanged\n", encoding="utf-8")
    write_json(ad / "files-allowlist.json", ["app/routes/feature.tsx"])
    shutil.copyfile(ad / "files-allowlist.json", ad / "web-files-allowlist.json")
    write_json(ad / "verify.json", {"acceptance": ["browser-1"], "test_patterns": ["feature.spec.ts"]})
    write_json(ad / "premises.json", [])
    write_json(ad / "reader-audit.json", {"columns": []})
    policy = {"required": [{"id": "browser-1", "criterion": "Feature renders", "path": "/feature", "assertions": [{"type": "text", "value": "Feature"}]}]}
    write_json(ad / "browser-evidence.json", policy)
    (ad / "browser-evidence.sha256").write_text(canonical_digest(policy) + "\n", encoding="utf-8")
    return ad


class FullstackContractTest(unittest.TestCase):
    def load_workflow(self, workflow):
        return yaml.safe_load((ARCHON / "workflows" / f"{workflow}.yaml").read_text(encoding="utf-8"))

    def load_node(self, workflow, node_id):
        doc = self.load_workflow(workflow)
        for node in doc["nodes"]:
            if node.get("id") == node_id:
                return node
        raise AssertionError(f"missing node {node_id}")

    def load_prompt(self, workflow, node_id):
        node = self.load_node(workflow, node_id)
        return node.get("prompt", "") + node.get("bash", "")

    def test_api_plan_gate_is_single_cross_repo_approval(self):
        prompt = self.load_prompt("full-sdlc-api", "ralplan")
        self.assertIn("ONE SHARED CROSS-REPOSITORY implementation plan", prompt)
        self.assertIn("single approval for the full design", prompt)
        self.assertIn("web-files-allowlist.json", prompt)
        self.assertNotIn("API PRONG ONLY", prompt)
        self.assertNotIn("Ignore any web-app prongs", prompt)

    def test_web_implementation_uses_approved_handoff_allowlist(self):
        prompt = self.load_prompt("full-sdlc-web", "implement")
        self.assertIn("copied", prompt)
        self.assertIn("API lane's approved web-files-allowlist.json", prompt)
        self.assertIn("Do not edit or broaden that allowlist", prompt)
        self.assertNotIn("write files-allowlist.json", prompt.lower())

    def test_standalone_web_scope_has_no_dummy_api_pr_contract(self):
        doc = (ARCHON / "workflows" / "full-sdlc-web.yaml").read_text(encoding="utf-8")
        self.assertIn("feature --scope web", doc)
        self.assertIn("standalone web", doc)
        self.assertIn("NEEDS_CLARIFICATION", doc)
        self.assertIn("Do not create a dummy API change", doc)

    def test_standalone_web_scope_has_real_plan_approval_route(self):
        nodes = {node["id"]: node for node in self.load_workflow("full-sdlc-web")["nodes"]}

        self.assertIn('{"scope":"%s"}', nodes["web-scope"]["bash"])
        for node_id in ("web-plan", "web-plan-oracle", "web-plan-render", "web-plan-controller-freeze", "web-plan-approval", "web-plan-approval-verify", "web-plan-freeze", "web-plan-lock-gate"):
            self.assertEqual(nodes[node_id]["when"], "$web-scope.scope == 'web'")
        self.assertIn("approval", nodes["web-plan-approval"])
        self.assertIn("web-plan-review.html", nodes["web-plan-approval"]["approval"]["message"])
        self.assertEqual(nodes["web-plan-controller-freeze"]["controller_action"], "finalize-evidence")
        self.assertEqual(nodes["web-plan-controller-freeze"]["phase"], "planning-freeze")
        self.assertEqual(nodes["web-plan-approval"]["depends_on"], ["web-plan-controller-freeze"])
        self.assertIn("$web-plan-controller-freeze.output.binding_id", nodes["web-plan-approval"]["approval"]["message"])
        self.assertIn("$web-plan-controller-freeze.output.oracle_digest", nodes["web-plan-approval"]["approval"]["message"])
        self.assertEqual(nodes["web-plan-approval-verify"]["depends_on"], ["web-plan-approval"])
        self.assertEqual(nodes["web-plan-approval-verify"]["controller_action"], "verify-approval")
        self.assertEqual(nodes["web-plan-approval-verify"]["phase"], "planning-approval")
        self.assertEqual(nodes["web-plan-freeze"]["depends_on"], ["web-plan-approval-verify"])
        self.assertIn("SCOPE_ESCALATION", nodes["web-plan"]["prompt"])
        self.assertIn("web-plan-approved.json", nodes["web-plan-freeze"]["bash"])
        self.assertEqual(nodes["premise-strip"]["trigger_rule"], "none_failed_min_one_success")
        self.assertIn("web-plan-freeze", nodes["premise-strip"]["depends_on"])
        self.assertEqual(nodes["implement"]["trigger_rule"], "none_failed_min_one_success")
        self.assertIn("web-plan-lock-gate", nodes["implement"]["depends_on"])

    def test_api_plan_gate_is_bound_to_controller_freeze_and_verify(self):
        nodes = {node["id"]: node for node in self.load_workflow("full-sdlc-api")["nodes"]}

        self.assertEqual(nodes["plan-freeze"]["depends_on"], ["plan-render-gate"])
        self.assertEqual(nodes["plan-freeze"]["controller_action"], "finalize-evidence")
        self.assertEqual(nodes["plan-freeze"]["phase"], "planning-freeze")
        self.assertEqual(nodes["plan-gate"]["depends_on"], ["plan-freeze"])
        self.assertIn("$plan-freeze.output.binding_id", nodes["plan-gate"]["approval"]["message"])
        self.assertIn("$plan-freeze.output.oracle_digest", nodes["plan-gate"]["approval"]["message"])
        self.assertEqual(nodes["plan-gate"]["approval"]["on_reject"]["max_attempts"], 1)
        self.assertIn("fresh guarded run", nodes["plan-gate"]["approval"]["on_reject"]["prompt"])
        self.assertEqual(nodes["plan-approval-verify"]["depends_on"], ["plan-gate"])
        self.assertEqual(nodes["plan-approval-verify"]["controller_action"], "verify-approval")
        self.assertEqual(nodes["plan-approval-verify"]["phase"], "planning-approval")
        self.assertEqual(nodes["implement"]["depends_on"], ["plan-approval-verify"])

    def test_bugfix_rca_gate_is_bound_to_controller_freeze_and_verify(self):
        nodes = {node["id"]: node for node in self.load_workflow("bugfix")["nodes"]}

        self.assertEqual(nodes["rca-freeze"]["depends_on"], ["rca-render-gate"])
        self.assertEqual(nodes["rca-freeze"]["controller_action"], "finalize-evidence")
        self.assertEqual(nodes["rca-freeze"]["phase"], "planning-freeze")
        self.assertEqual(nodes["rca-approval"]["depends_on"], ["rca-freeze"])
        self.assertIn("$rca-freeze.output.binding_id", nodes["rca-approval"]["approval"]["message"])
        self.assertIn("$rca-freeze.output.oracle_digest", nodes["rca-approval"]["approval"]["message"])
        self.assertEqual(nodes["rca-approval"]["approval"]["on_reject"]["max_attempts"], 1)
        self.assertIn("fresh guarded run", nodes["rca-approval"]["approval"]["on_reject"]["prompt"])
        self.assertEqual(nodes["rca-approval-verify"]["depends_on"], ["rca-approval"])
        self.assertEqual(nodes["rca-approval-verify"]["controller_action"], "verify-approval")
        self.assertEqual(nodes["rca-approval-verify"]["phase"], "planning-approval")
        self.assertEqual(nodes["post-approval-integrity"]["depends_on"], ["rca-approval-verify"])

    def test_api_render_packets_do_not_promise_same_run_rework(self):
        paths = [ARCHON / "workflows" / f"{name}.yaml" for name in (
            "full-sdlc-api", "full-sdlc-api-lite", "full-sdlc-api-codex", "full-sdlc-api-lite-codex"
        )] + [ARCHON / "setup/lite/api/plan-render.prompt.md"]
        for path in paths:
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("third rejection", text)
                self.assertNotIn("revision pass: the run reworks", text)
                self.assertIn("fresh guarded run", text)

    def test_generated_variants_keep_controller_planning_gate_chain(self):
        api_lanes = ("full-sdlc-api-lite", "full-sdlc-api-codex", "full-sdlc-api-lite-codex")
        for workflow in api_lanes:
            with self.subTest(workflow=workflow):
                nodes = {node["id"]: node for node in self.load_workflow(workflow)["nodes"]}
                self.assertEqual(nodes["plan-freeze"]["controller_action"], "finalize-evidence")
                self.assertEqual(nodes["plan-freeze"]["phase"], "planning-freeze")
                self.assertEqual(nodes["plan-gate"]["depends_on"], ["plan-freeze"])
                self.assertIn("$plan-freeze.output.binding_id", nodes["plan-gate"]["approval"]["message"])
                self.assertIn("$plan-freeze.output.receipt", nodes["plan-gate"]["approval"]["message"])
                self.assertEqual(nodes["plan-approval-verify"]["depends_on"], ["plan-gate"])
                self.assertEqual(nodes["plan-approval-verify"]["controller_action"], "verify-approval")

        bugfix_lanes = ("bugfix-lite", "bugfix-codex", "bugfix-lite-codex")
        for workflow in bugfix_lanes:
            with self.subTest(workflow=workflow):
                nodes = {node["id"]: node for node in self.load_workflow(workflow)["nodes"]}
                self.assertEqual(nodes["rca-freeze"]["controller_action"], "finalize-evidence")
                self.assertEqual(nodes["rca-freeze"]["phase"], "planning-freeze")
                self.assertEqual(nodes["rca-approval"]["depends_on"], ["rca-freeze"])
                self.assertIn("$rca-freeze.output.binding_id", nodes["rca-approval"]["approval"]["message"])
                self.assertIn("$rca-freeze.output.receipt", nodes["rca-approval"]["approval"]["message"])
                self.assertEqual(nodes["rca-approval-verify"]["depends_on"], ["rca-approval"])
                self.assertEqual(nodes["rca-approval-verify"]["controller_action"], "verify-approval")

        nodes = {node["id"]: node for node in self.load_workflow("full-sdlc-web-codex")["nodes"]}
        self.assertEqual(nodes["web-plan-controller-freeze"]["controller_action"], "finalize-evidence")
        self.assertEqual(nodes["web-plan-approval"]["depends_on"], ["web-plan-controller-freeze"])
        self.assertIn(
            "$web-plan-controller-freeze.output.binding_id",
            nodes["web-plan-approval"]["approval"]["message"],
        )
        self.assertIn(
            "$web-plan-controller-freeze.output.receipt",
            nodes["web-plan-approval"]["approval"]["message"],
        )
        self.assertEqual(nodes["web-plan-approval-verify"]["depends_on"], ["web-plan-approval"])
        self.assertEqual(nodes["web-plan-approval-verify"]["controller_action"], "verify-approval")

    def test_no_change_outcomes_are_content_based_and_prless(self):
        api_gate = self.load_prompt("full-sdlc-api", "gate-tests")
        web_gate = self.load_prompt("full-sdlc-web", "gate-tests")
        self.assertIn("feature-result.json", api_gate)
        self.assertIn("NO_CHANGE", api_gate)
        self.assertIn("git diff --quiet", api_gate)
        self.assertIn("--untracked-files=all", api_gate)
        self.assertIn("CONTENT_STATUS", api_gate)
        self.assertIn("feature-result.json", web_gate)
        self.assertIn("NO_CHANGE", web_gate)
        self.assertIn("git diff --quiet", web_gate)
        self.assertIn("--untracked-files=all", web_gate)
        self.assertIn("CONTENT_STATUS", web_gate)

    def test_fullstack_handoff_carries_premise_and_reader_contract_hashes(self):
        api_doc = (ARCHON / "workflows" / "full-sdlc-api.yaml").read_text(encoding="utf-8")
        web_doc = (ARCHON / "workflows" / "full-sdlc-web.yaml").read_text(encoding="utf-8")
        self.assertIn("premises_sha256", api_doc)
        self.assertIn("reader_audit_sha256", api_doc)
        self.assertIn("web-premises.json", api_doc)
        self.assertIn("web-reader-audit.json", api_doc)
        self.assertIn("premises_sha256", web_doc)
        self.assertIn("reader_audit_sha256", web_doc)
        self.assertIn("web_premises_sha256", (ARCHON / "setup" / "resolve-web-params.sh").read_text(encoding="utf-8"))
        self.assertIn("web_reader_audit_sha256", (ARCHON / "setup" / "resolve-web-params.sh").read_text(encoding="utf-8"))
        self.assertIn("browser-evidence.json", api_doc)
        self.assertIn("browser_evidence_sha256", (ARCHON / "setup" / "archon-run.py").read_text(encoding="utf-8"))
        self.assertIn("browser_evidence_sha256", (ARCHON / "setup" / "resolve-web-params.sh").read_text(encoding="utf-8"))

    def test_web_plan_nodes_freeze_browser_policy_and_reject_drift(self):
        with tempfile.TemporaryDirectory() as td:
            ad = make_web_plan_artifacts(Path(td))
            oracle = run_node("full-sdlc-web", "web-plan-oracle", ad)
            self.assertEqual(oracle.returncode, 0, oracle.stdout + oracle.stderr)
            self.assertIn("WEB_PLAN_ORACLE=PASS", oracle.stdout)

            policy = json.loads((ad / "browser-evidence.json").read_text(encoding="utf-8"))
            policy["required"][0]["criterion"] = "Changed after oracle"
            write_json(ad / "browser-evidence.json", policy)
            (ad / "browser-evidence.sha256").write_text(canonical_digest(policy) + "\n", encoding="utf-8")

            freeze = run_node("full-sdlc-web", "web-plan-freeze", ad)
            self.assertEqual(freeze.returncode, 1)
            self.assertIn("approved artifact changed since oracle: browser-evidence.json", freeze.stdout + freeze.stderr)

    def test_web_plan_oracle_rejects_browser_policy_digest_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            ad = make_web_plan_artifacts(Path(td))
            (ad / "browser-evidence.sha256").write_text("0" * 64 + "\n", encoding="utf-8")
            result = run_node("full-sdlc-web", "web-plan-oracle", ad)
            self.assertEqual(result.returncode, 1)
            self.assertIn("browser-evidence.sha256 mismatch", result.stdout + result.stderr)

    def test_web_plan_freeze_fails_closed_after_reject_marker(self):
        with tempfile.TemporaryDirectory() as td:
            ad = make_web_plan_artifacts(Path(td))
            oracle = run_node("full-sdlc-web", "web-plan-oracle", ad)
            self.assertEqual(oracle.returncode, 0, oracle.stdout + oracle.stderr)
            write_json(ad / "feature-result.json", {"outcome": "NEEDS_CLARIFICATION", "reason": "rejected"})
            freeze = run_node("full-sdlc-web", "web-plan-freeze", ad)
            self.assertEqual(freeze.returncode, 1)
            self.assertIn("rejected plan needs renewed approval", freeze.stdout + freeze.stderr)

    def test_resolver_contract_allows_empty_fullstack_web_allowlist_and_copies_browser_policy(self):
        resolver = (ARCHON / "setup" / "resolve-web-params.sh").read_text(encoding="utf-8")
        self.assertIn("not isinstance(web_files, list) or not all", resolver)
        self.assertNotIn("or not web_files or not all", resolver)
        self.assertIn("browser-evidence.json", resolver)
        self.assertIn("browser-evidence.sha256", resolver)
        self.assertIn("require_browser_policy(api_artifacts, browser_expected)", resolver)


if __name__ == "__main__":
    unittest.main()
