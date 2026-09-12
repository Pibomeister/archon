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


def make_repo_chain_web_artifacts(tmp):
    ad = tmp / "artifacts"
    ad.mkdir()
    apiwt = tmp / "api-candidate"
    webwt = tmp / "web-candidate"
    api_head = init_git_repo(apiwt)
    web_head = init_git_repo(webwt)
    spec = tmp / "spec.md"
    spec.write_text("# Repository chain web feature\n", encoding="utf-8")
    write_json(ad / "params.json", {
        "schema_version": 2,
        "feature_scope": "repositories",
        "feature_phase": "implement",
        "repo": "web-app",
        "repository": "web-app",
        "repositories": ["api", "web-app"],
        "repository_scope": ["api", "web-app"],
        "spec": str(spec),
        "spec_sha256": hashlib.sha256(spec.read_bytes()).hexdigest(),
        "slug": "feature-abcd1234-implement-web-app",
        "branch": "archon/repository-chain-web",
        "baseline": web_head,
        "worktree": str(webwt),
        "worktrees_by_repo": {"api": str(apiwt), "web-app": str(webwt)},
        "logical_chain_id": "abcd1234abcd1234abcd1234abcd1234",
        "run_id": "a" * 32,
    })
    plan = {
        "schema": "archon.joint-feature-plan.v1",
        "repositories": ["api", "web-app"],
        "dependency_order": ["api", "web-app"],
        "contracts": [{"producer": "api", "consumer": "web-app", "artifact": "openapi.json", "description": "fixture"}],
        "integration": {"scenarios": [{"name": "local", "uses": ["api", "web-app"], "commands": ["fixture"], "expected_tests": ["fixture"]}]},
        "stages": {
            "api": {"depends_on": [], "files_allowlist": ["src/api.ts"], "test_patterns": ["api.spec.ts"], "verification": ["bun test"]},
            "web-app": {"depends_on": ["api"], "files_allowlist": ["app/routes/feature.tsx"], "test_patterns": ["feature.spec.ts"], "verification": ["pnpm typecheck", "pnpm lint", "pnpm test --run"]},
        },
    }
    write_json(ad / "joint-plan.json", plan)
    (ad / "plan.md").write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_json(ad / "files-allowlist.json", ["app/routes/feature.tsx"])
    write_json(ad / "verify.json", {"test_patterns": ["feature.spec.ts"], "verification": ["pnpm typecheck", "pnpm lint", "pnpm test --run"]})
    write_json(ad / "candidate-revisions.json", {
        "schema": "archon.joint-candidates.v1",
        "repositories": {
            "api": {"source_worktree": str(apiwt), "commit": api_head},
            "web-app": {"source_worktree": str(webwt), "commit": web_head},
        },
    })
    policy = {"required": [{"id": "browser-1", "criterion": "Feature renders", "path": "/feature", "assertions": [{"type": "text", "value": "Feature"}]}]}
    write_json(ad / "browser-evidence.json", policy)
    (ad / "browser-evidence.sha256").write_text(canonical_digest(policy) + "\n", encoding="utf-8")
    return ad, api_head


def make_repo_chain_web_fixture_only_artifacts(tmp):
    ad, api_head = make_repo_chain_web_artifacts(tmp)
    plan = json.loads((ad / "joint-plan.json").read_text(encoding="utf-8"))
    plan["repositories"] = ["web-app"]
    plan["dependency_order"] = ["web-app"]
    plan["contracts"] = []
    plan["stages"]["web-app"]["depends_on"] = []
    del plan["stages"]["api"]
    write_json(ad / "joint-plan.json", plan)
    (ad / "plan.md").write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    params = json.loads((ad / "params.json").read_text(encoding="utf-8"))
    fixture = params["worktrees_by_repo"].pop("api")
    params["repositories"] = ["web-app"]
    params["repository_scope"] = ["web-app"]
    params["api_fixture_worktree"] = fixture
    params["api_fixture_head_sha"] = api_head
    write_json(ad / "params.json", params)
    (ad / "candidate-revisions.json").unlink()
    return ad, api_head


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
        for node_id in ("web-plan", "web-plan-oracle", "web-plan-render", "web-plan-approval", "web-plan-freeze", "web-plan-lock-gate"):
            self.assertEqual(nodes[node_id]["when"], "$web-scope.scope == 'web'")
        self.assertIn("approval", nodes["web-plan-approval"])
        self.assertIn("web-plan-review.html", nodes["web-plan-approval"]["approval"]["message"])
        # The two controller nodes that used to bracket this gate are gone with
        # the hardened runtime; web-plan-freeze carries the seal in bash.
        self.assertNotIn("web-plan-controller-freeze", nodes)
        self.assertNotIn("web-plan-approval-verify", nodes)
        self.assertEqual(nodes["web-plan-approval"]["depends_on"], ["web-plan-render"])
        self.assertEqual(nodes["web-plan-freeze"]["depends_on"], ["web-plan-approval"])
        self.assertIn("SCOPE_ESCALATION", nodes["web-plan"]["prompt"])
        self.assertIn("web-plan-approved.json", nodes["web-plan-freeze"]["bash"])
        self.assertEqual(nodes["premise-strip"]["trigger_rule"], "none_failed_min_one_success")
        self.assertIn("web-plan-freeze", nodes["premise-strip"]["depends_on"])
        self.assertEqual(nodes["implement"]["trigger_rule"], "none_failed_min_one_success")
        self.assertIn("web-plan-lock-gate", nodes["implement"]["depends_on"])

    def test_api_plan_gate_runs_between_render_gate_and_implement(self):
        """Inverse of the assertion 229090a introduced: the freeze and verify
        controller nodes that bracketed this gate are gone, so the gate sits
        directly between plan-render-gate and implement as it did for every
        completed run before that checkpoint."""
        nodes = {node["id"]: node for node in self.load_workflow("full-sdlc-api")["nodes"]}

        self.assertNotIn("plan-freeze", nodes)
        self.assertNotIn("plan-approval-verify", nodes)
        self.assertEqual(nodes["plan-gate"]["depends_on"], ["plan-render-gate"])
        self.assertEqual(nodes["plan-gate"]["approval"]["on_reject"]["max_attempts"], 3)
        self.assertIn("PLAN_REVISED", nodes["plan-gate"]["approval"]["on_reject"]["prompt"])
        self.assertEqual(nodes["implement"]["depends_on"], ["plan-gate", "implementation-ready"])
        self.assertEqual(nodes["implement"].get("trigger_rule"), "none_failed_min_one_success")

    def test_bugfix_rca_gate_runs_between_render_gate_and_post_approval_integrity(self):
        """Inverse of the assertion 229090a introduced. controller-attest.py
        still seals the RCA from ordinary bash nodes either side of this gate;
        only the controller_action nodes were removed."""
        nodes = {node["id"]: node for node in self.load_workflow("bugfix")["nodes"]}

        self.assertNotIn("rca-freeze", nodes)
        self.assertNotIn("rca-approval-verify", nodes)
        self.assertEqual(nodes["rca-approval"]["depends_on"], ["rca-render-gate"])
        self.assertEqual(nodes["rca-approval"]["approval"]["on_reject"]["max_attempts"], 3)
        self.assertIn("RCA_REJECTION_RECORDED", nodes["rca-approval"]["approval"]["on_reject"]["prompt"])
        self.assertEqual(nodes["post-approval-integrity"]["depends_on"], ["rca-approval"])
        self.assertIn("controller-attest.py", nodes["post-approval-integrity"]["bash"])

    def test_api_render_packets_describe_the_revision_pass_they_actually_run(self):
        """Inverse of the assertion 229090a introduced. The gate does three
        rejection attempts with a revision pass again, so the rendered packet
        must say so rather than promising a controller freeze that no longer
        exists. A packet describing the wrong rejection behaviour is the failure
        this test exists to catch, in either direction."""
        paths = [ARCHON / "workflows" / f"{name}.yaml" for name in (
            "full-sdlc-api", "full-sdlc-api-lite", "full-sdlc-api-codex", "full-sdlc-api-lite-codex"
        )] + [ARCHON / "setup/lite/api/plan-render.prompt.md"]
        for path in paths:
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIn("third rejection", text)
                self.assertNotIn("fresh guarded run", text)
                self.assertNotIn("controller_action", text)

    def test_generated_variants_carry_no_controller_planning_gate_chain(self):
        """Inverse of the assertion 229090a introduced. The generated twins are
        the place a stale controller node survives unnoticed, because nobody
        hand-edits them: derive-lite/derive-codex must carry the removal through."""
        api_lanes = ("full-sdlc-api-lite", "full-sdlc-api-codex", "full-sdlc-api-lite-codex")
        for workflow in api_lanes:
            with self.subTest(workflow=workflow):
                nodes = {node["id"]: node for node in self.load_workflow(workflow)["nodes"]}
                self.assertNotIn("plan-freeze", nodes)
                self.assertNotIn("plan-approval-verify", nodes)
                self.assertEqual(nodes["plan-gate"]["depends_on"], ["plan-render-gate"])
                self.assertNotIn("controller_action", nodes["plan-gate"])

        bugfix_lanes = ("bugfix-lite", "bugfix-codex", "bugfix-lite-codex")
        for workflow in bugfix_lanes:
            with self.subTest(workflow=workflow):
                nodes = {node["id"]: node for node in self.load_workflow(workflow)["nodes"]}
                self.assertNotIn("rca-freeze", nodes)
                self.assertNotIn("rca-approval-verify", nodes)
                self.assertEqual(nodes["rca-approval"]["depends_on"], ["rca-render-gate"])
                self.assertEqual(nodes["post-approval-integrity"]["depends_on"], ["rca-approval"])

        nodes = {node["id"]: node for node in self.load_workflow("full-sdlc-web-codex")["nodes"]}
        self.assertNotIn("web-plan-controller-freeze", nodes)
        self.assertNotIn("web-plan-approval-verify", nodes)
        self.assertEqual(nodes["web-plan-approval"]["depends_on"], ["web-plan-render"])
        self.assertEqual(nodes["web-plan-freeze"]["depends_on"], ["web-plan-approval"])

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

    def test_repository_chain_web_scope_reuses_controller_bound_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            ad, api_head = make_repo_chain_web_artifacts(Path(td))
            env = dict(os.environ,
                       ARCHON_FEATURE_SCOPE="repositories",
                       ARCHON_FEATURE_PHASE="implement",
                       ARCHON_FEATURE_REPO="web-app",
                       ARCHON_FEATURE_CHAIN_ID="abcd1234abcd1234abcd1234abcd1234",
                       ARCHON_FEATURE_PROVIDER="codex",
                       ARCHON_FEATURE_LANE="full-sdlc-web-codex")
            result = subprocess.run([
                "bash", str(ARCHON / "setup" / "resolve-web-params.sh"),
                str(ROOT), "", str(ad), "4127", "3127",
            ], env=env, capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("WEB_PARAMS=OK scope=repositories", result.stdout)

            params = json.loads((ad / "params.json").read_text(encoding="utf-8"))
            self.assertEqual("repositories", params["feature_scope"])
            self.assertEqual("web-app", params["repo"])
            self.assertEqual(api_head, params["api_head_sha"])
            self.assertEqual(params["worktrees_by_repo"]["api"], params["api_worktree"])
            self.assertIsInstance(params["api_port"], int)
            self.assertIsInstance(params["web_port"], int)
            self.assertEqual(["app/routes/feature.tsx"], json.loads((ad / "web-files-allowlist.json").read_text(encoding="utf-8")))
            self.assertEqual([], json.loads((ad / "premises.json").read_text(encoding="utf-8")))
            self.assertEqual({"columns": []}, json.loads((ad / "reader-audit.json").read_text(encoding="utf-8")))
            handoff = json.loads((ad / "api-handoff.json").read_text(encoding="utf-8"))
            self.assertEqual("archon-repository-chain-local-candidates", handoff["kind"])
            self.assertEqual(api_head, handoff["api_head_sha"])
            self.assertEqual("candidate-revisions", handoff["api_source"])

    def test_repository_chain_web_rejects_missing_or_mismatched_api_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            ad, _api_head = make_repo_chain_web_artifacts(Path(td))
            env = dict(os.environ,
                       ARCHON_FEATURE_SCOPE="repositories",
                       ARCHON_FEATURE_PHASE="implement",
                       ARCHON_FEATURE_REPO="web-app",
                       ARCHON_FEATURE_CHAIN_ID="abcd1234abcd1234abcd1234abcd1234",
                       ARCHON_FEATURE_PROVIDER="codex",
                       ARCHON_FEATURE_LANE="full-sdlc-web-codex")
            (ad / "candidate-revisions.json").unlink()
            result = subprocess.run([
                "bash", str(ARCHON / "setup" / "resolve-web-params.sh"),
                str(ROOT), "", str(ad), "4127", "3127",
            ], env=env, capture_output=True, text=True)
            self.assertEqual(1, result.returncode)
            self.assertIn("missing candidate-revisions.json for api dependency", result.stdout + result.stderr)

            second = Path(td) / "second"
            second.mkdir()
            ad, _api_head = make_repo_chain_web_artifacts(second)
            candidate = json.loads((ad / "candidate-revisions.json").read_text(encoding="utf-8"))
            candidate["repositories"]["api"]["commit"] = "0" * 40
            write_json(ad / "candidate-revisions.json", candidate)
            result = subprocess.run([
                "bash", str(ARCHON / "setup" / "resolve-web-params.sh"),
                str(ROOT), "", str(ad), "4127", "3127",
            ], env=env, capture_output=True, text=True)
            self.assertEqual(1, result.returncode)
            self.assertIn("API candidate head does not match approved revision", result.stdout + result.stderr)

    def test_repository_chain_web_without_api_dependency_uses_only_approved_fixture(self):
        with tempfile.TemporaryDirectory() as td:
            ad, api_head = make_repo_chain_web_fixture_only_artifacts(Path(td))
            env = dict(os.environ,
                       ARCHON_FEATURE_SCOPE="repositories",
                       ARCHON_FEATURE_PHASE="implement",
                       ARCHON_FEATURE_REPO="web-app",
                       ARCHON_FEATURE_CHAIN_ID="abcd1234abcd1234abcd1234abcd1234",
                       ARCHON_FEATURE_PROVIDER="codex",
                       ARCHON_FEATURE_LANE="full-sdlc-web-codex")
            result = subprocess.run([
                "bash", str(ARCHON / "setup" / "resolve-web-params.sh"),
                str(ROOT), "", str(ad), "4127", "3127",
            ], env=env, capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            params = json.loads((ad / "params.json").read_text(encoding="utf-8"))
            self.assertEqual(params["api_fixture_worktree"], params["api_worktree"])
            self.assertEqual(api_head, params["api_head_sha"])
            handoff = json.loads((ad / "api-handoff.json").read_text(encoding="utf-8"))
            self.assertEqual("approved-fixture", handoff["api_source"])

            missing = Path(td) / "missing-fixture"
            missing.mkdir()
            ad, _api_head = make_repo_chain_web_fixture_only_artifacts(missing)
            params = json.loads((ad / "params.json").read_text(encoding="utf-8"))
            del params["api_fixture_worktree"]
            write_json(ad / "params.json", params)
            result = subprocess.run([
                "bash", str(ARCHON / "setup" / "resolve-web-params.sh"),
                str(ROOT), "", str(ad), "4127", "3127",
            ], env=env, capture_output=True, text=True)
            self.assertEqual(1, result.returncode)
            self.assertIn("requires approved api fixture", result.stdout + result.stderr)

    def test_repository_chain_web_graph_skips_legacy_plan_and_handoff_gate(self):
        nodes = {node["id"]: node for node in self.load_workflow("full-sdlc-web")["nodes"]}
        self.assertIn('${ARCHON_FEATURE_SCOPE-}" != "repositories"', nodes["preflight"]["bash"])
        self.assertIn("fullstack|web|repositories", nodes["web-scope"]["bash"])
        for node_id in ("web-plan", "web-plan-oracle", "web-plan-render", "web-plan-approval", "web-plan-freeze", "web-plan-lock-gate"):
            self.assertEqual(nodes[node_id]["when"], "$web-scope.scope == 'web'")
        self.assertIn("$SCOPE\" = \"repositories", nodes["premise-strip"]["bash"])
        self.assertIn("joint-plan.json", nodes["premise-strip"]["bash"])
        self.assertIn("$SCOPE\" = \"repositories", nodes["premise-gate"]["bash"])
        self.assertIn("joint-plan.json", nodes["premise-gate"]["bash"])
        self.assertEqual(nodes["implement"]["trigger_rule"], "none_failed_min_one_success")
        self.assertIn("repository-chain scope", nodes["implement"]["prompt"])
        self.assertNotIn("verification_evidence", nodes["uat-gate"]["bash"])
        self.assertEqual(nodes["local-candidate"]["depends_on"], ["kb-capture-gate"])
        self.assertNotIn("trigger_rule", nodes["local-candidate"])
        self.assertNotIn("always_run", nodes["local-candidate"])
        self.assertEqual(nodes["report"]["depends_on"], ["local-candidate"])
        self.assertIn("trusted-local-candidate.sh", nodes["local-candidate"]["bash"])
        self.assertIn("node-smoke.out", nodes["local-candidate"]["bash"])
        self.assertIn("smoke-result.txt", nodes["local-candidate"]["bash"])
        self.assertIn("browser-verifier-receipt.json", nodes["local-candidate"]["bash"])
        self.assertIn("browser receipt candidate does not match current head", nodes["local-candidate"]["bash"])
        self.assertNotIn("local-candidate-test-$INDEX.json", nodes["local-candidate"]["bash"])
        self.assertIn("SHIP=HELD repository-chain local candidate only", nodes["ship"]["bash"])


if __name__ == "__main__":
    unittest.main()
