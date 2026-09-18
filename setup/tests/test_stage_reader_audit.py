#!/usr/bin/env python3
"""Each repository-list stage audits its own repository's columns.

The planning run is anchored on one repository (api when selected) and writes
ONE reader-audit.json. Every stage got a copy, so a goodword-mcp stage grepped
goodword-mcp for api columns and reader-audit-gate passed having audited
nothing. The joint plan now carries stages.<repo>.reader_audit; a legacy plan
without it makes non-anchor stages derive their audit from their own diff."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

SETUP = Path(__file__).resolve().parents[1]
ARCHON = SETUP.parent
VALIDATOR = SETUP / "validate-joint-plan.py"
sys.path.insert(0, str(SETUP))
spec = importlib.util.spec_from_file_location("feature_chain_reader_audit", SETUP / "feature_chain.py")
fc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fc)

API_COLUMNS = {"columns": [{"table": "groups", "column": "visibility", "reason": "presentation"}]}
MCP_COLUMNS = {"columns": [{"table": "tool_calls", "column": "status", "reason": "interpretation"}]}


def plan(audits=None, repos=("api", "goodword-mcp")):
    stages = {}
    for repo in repos:
        stages[repo] = {"depends_on": [], "files_allowlist": [f"src/{repo}.ts"],
                        "test_patterns": [f"{repo}.spec.ts"], "verification": ["check"]}
        if audits and repo in audits:
            stages[repo]["reader_audit"] = audits[repo]
    return {
        "schema": "archon.joint-feature-plan.v1",
        "repositories": list(repos),
        "contracts": [],
        "integration": {"scenarios": [{"name": "local", "uses": [repos[0]],
                                       "commands": [{"repo": repos[0], "argv": ["fixture"]}],
                                       "expected_tests": ["local"]}]},
        "stages": stages,
    }


def validate(doc, anchor="api", files=None):
    with tempfile.TemporaryDirectory() as td:
        ad = Path(td)
        (ad / "params.json").write_text(json.dumps({"repositories": doc["repositories"], "repo": anchor}))
        (ad / "joint-plan.json").write_text(json.dumps(doc))
        for name, body in (files or {}).items():
            (ad / name).write_text(json.dumps(body))
        return subprocess.run(["python3", str(VALIDATOR), str(ad)], capture_output=True, encoding="utf-8")


class ValidatorContract(unittest.TestCase):
    def test_per_repository_audits_pass_with_matching_anchor_mirror(self):
        r = validate(plan({"api": API_COLUMNS, "goodword-mcp": MCP_COLUMNS}), files={"reader-audit.json": API_COLUMNS})
        self.assertEqual(0, r.returncode, r.stdout)
        self.assertNotIn("reader_audit", r.stdout)

    def test_every_stage_declares_one_when_any_does(self):
        r = validate(plan({"api": API_COLUMNS}))
        self.assertEqual(1, r.returncode, r.stdout)
        self.assertIn("JOINT_PLAN=FAIL stage for goodword-mcp must declare reader_audit", r.stdout)

    def test_malformed_audit_fails(self):
        r = validate(plan({"api": API_COLUMNS, "goodword-mcp": {"columns": [{"table": "t"}]}}))
        self.assertEqual(1, r.returncode, r.stdout)
        self.assertIn("stages.goodword-mcp.reader_audit must be", r.stdout)

    def test_anchor_mirror_drift_fails_so_the_api_stage_is_not_weakened(self):
        # The packet renders reader-audit.json; the api stage consumes the joint
        # plan. An api stage entry that drops a rendered column must not pass.
        doc = plan({"api": {"columns": []}, "goodword-mcp": MCP_COLUMNS})
        r = validate(doc, files={"reader-audit.json": API_COLUMNS})
        self.assertEqual(1, r.returncode, r.stdout)
        self.assertIn("JOINT_PLAN=FAIL reader-audit.json columns differ from stages.api.reader_audit", r.stdout)
        # Negative control: without the rendered mirror there is nothing to diverge from.
        self.assertEqual(0, validate(doc).returncode)

    def test_web_app_mirror_drift_fails(self):
        doc = plan({"api": API_COLUMNS, "web-app": {"columns": []}}, repos=("api", "web-app"))
        r = validate(doc, files={"reader-audit.json": API_COLUMNS, "web-reader-audit.json": MCP_COLUMNS})
        self.assertEqual(1, r.returncode, r.stdout)
        self.assertIn("web-reader-audit.json columns differ from stages.web-app.reader_audit", r.stdout)

    def test_legacy_multi_repo_plan_passes_with_warn(self):
        r = validate(plan())
        self.assertEqual(0, r.returncode, r.stdout)
        self.assertIn("JOINT_PLAN=WARN no stages.<repo>.reader_audit (legacy plan)", r.stdout)

    def test_legacy_single_repo_plan_passes_silently(self):
        r = validate(plan(repos=("goodword-mcp",)), anchor="goodword-mcp")
        self.assertEqual(0, r.returncode, r.stdout)
        self.assertNotIn("reader_audit", r.stdout)


class StageArtifacts(unittest.TestCase):
    """feature_chain.write_phase_artifacts gives each stage its own audit."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.control = self.root / "control"
        self.spec = self.root / "feature.md"
        self.spec.write_text("# Feature\n")
        self.host = SimpleNamespace(ROOT=self.root)
        self.args = Namespace(spec=str(self.spec), provider="claude", control_dir=self.control,
                              codex_home=self.root / "codex-home", wall_minutes=240,
                              max_total_tokens=30_000_000, budget_shepherd=False, no_watch=True)

    def init_repo(self, name):
        repo = self.root / name
        repo.mkdir()
        for argv in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", str(repo), *argv], check=True)
        (repo / "README.md").write_text(name)
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)

    def stage_audit(self, doc, repo, planning_files):
        repos = doc["repositories"]
        for name in repos:
            self.init_repo(name)
        state = fc.write_state(self.control, fc.make_initial_state(self.host, self.args, repos))
        source = self.root / "planning"
        source.mkdir()
        (source / fc.JOINT_PLAN_ARTIFACT).write_text(json.dumps(doc))
        files = {fc.JOINT_PLAN_ARTIFACT: fc.file_digest(source / fc.JOINT_PLAN_ARTIFACT)}
        for name, body in planning_files.items():
            (source / name).write_text(json.dumps(body))
            files[name] = fc.file_digest(source / name)
        state = fc.approve_plan_unlocked(self.control, state, doc, {"root": str(source), "files": files})
        artifacts = self.root / f"stage-{repo}"
        artifacts.mkdir()
        fc.write_phase_artifacts(artifacts, state, "implement", repo, {"id": "f" * 32})
        return json.loads((artifacts / "reader-audit.json").read_text())

    def test_mcp_stage_gets_its_own_audit_not_the_api_scoped_copy(self):
        doc = plan({"api": API_COLUMNS, "goodword-mcp": MCP_COLUMNS})
        self.assertEqual(MCP_COLUMNS, self.stage_audit(doc, "goodword-mcp", {"reader-audit.json": API_COLUMNS}))

    def test_negative_control_without_the_stage_writer_mcp_inherits_api_columns(self):
        doc = plan({"api": API_COLUMNS, "goodword-mcp": MCP_COLUMNS})
        with mock.patch.object(fc, "write_stage_reader_audit", lambda *a: None):
            self.assertEqual(API_COLUMNS, self.stage_audit(doc, "goodword-mcp", {"reader-audit.json": API_COLUMNS}))

    def test_api_anchor_stage_keeps_its_declared_columns(self):
        doc = plan({"api": API_COLUMNS, "goodword-mcp": MCP_COLUMNS})
        self.assertEqual(API_COLUMNS, self.stage_audit(doc, "api", {"reader-audit.json": API_COLUMNS}))

    def test_legacy_plan_non_anchor_stage_derives_its_audit(self):
        audit = self.stage_audit(plan(), "goodword-mcp", {"reader-audit.json": API_COLUMNS})
        self.assertEqual([], audit["columns"])
        self.assertEqual("stage-diff", audit["derive"])

    def test_legacy_plan_anchor_stage_keeps_the_planning_copy(self):
        self.assertEqual(API_COLUMNS, self.stage_audit(plan(), "api", {"reader-audit.json": API_COLUMNS}))

    def test_legacy_single_repo_plan_keeps_the_planning_copy(self):
        doc = plan(repos=("goodword-mcp",))
        self.assertEqual(MCP_COLUMNS, self.stage_audit(doc, "goodword-mcp", {"reader-audit.json": MCP_COLUMNS}))


def lane_bash(node_id):
    doc = yaml.safe_load((ARCHON / "workflows" / "full-sdlc-api.yaml").read_text(encoding="utf-8"))
    return next(n["bash"] for n in doc["nodes"] if n.get("id") == node_id)


class ReaderAuditGate(unittest.TestCase):
    def run_gate(self, declared, result=None):
        with tempfile.TemporaryDirectory() as td:
            ad = Path(td)
            (ad / "reader-audit.json").write_text(json.dumps(declared))
            if result is not None:
                (ad / "reader-audit-result.json").write_text(json.dumps(result))
            return subprocess.run(["bash", "-c", lane_bash("reader-audit-gate")], capture_output=True,
                                  encoding="utf-8", env=dict(os.environ, ARTIFACTS_DIR=str(ad)))

    DERIVE = {"columns": [], "derive": "stage-diff", "reason": "legacy"}

    def test_derived_stage_without_a_result_fails(self):
        r = self.run_gate(self.DERIVE)
        self.assertEqual(1, r.returncode, r.stdout)
        self.assertIn("READER_AUDIT_FAIL no reader-audit-result.json", r.stdout)

    def test_negative_control_the_derive_marker_is_what_requires_the_result(self):
        r = self.run_gate({"columns": []})
        self.assertEqual(0, r.returncode, r.stdout)
        self.assertIn("READER_AUDIT_GATE=PASS (no columns declared)", r.stdout)

    def test_derived_stage_result_must_be_marked_derived(self):
        r = self.run_gate(self.DERIVE, {"columns": []})
        self.assertEqual(1, r.returncode, r.stdout)
        self.assertIn("READER_AUDIT_FAIL stage-diff audit result is not marked derived", r.stdout)

    def test_derived_stage_with_no_columns_passes(self):
        r = self.run_gate(self.DERIVE, {"derived": True, "columns": []})
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        self.assertIn("READER_AUDIT_GATE=PASS", r.stdout)

    def test_derived_affected_reader_fails(self):
        result = {"derived": True, "columns": [{"table": "t", "column": "c", "readers": [
            {"file": "src/x.ts:1", "class": "affected", "why": "unchanged reader"}]}]}
        r = self.run_gate(self.DERIVE, result)
        self.assertEqual(1, r.returncode, r.stdout)
        self.assertIn("READER_AUDIT_FAIL affected reader t.c at src/x.ts:1", r.stdout)

    def test_declared_columns_must_still_be_covered(self):
        # The planned (api) path is unchanged: a declared column missing from
        # the result still fails.
        r = self.run_gate(API_COLUMNS, {"columns": []})
        self.assertNotEqual(0, r.returncode, r.stdout)
        self.assertIn("declared columns not covered", r.stdout + r.stderr)

    def test_exit_gate_belt_requires_a_derived_result(self):
        belt = lane_bash("exit-gate")
        self.assertIn('[ "$DERIVE" = stage-diff ]', belt)
        self.assertIn("stage-diff audit result is not marked derived", belt)


if __name__ == "__main__":
    unittest.main()
