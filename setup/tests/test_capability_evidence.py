#!/usr/bin/env python3
"""Capability discovery, the capability gate, and occurrence-evidence reach.

Three defects this pins, in the order they were found:
  A  capability health was advisory everywhere, so an expired AWS session only
     ever produced a warning line;
  B  evidence-plan.json could name identifiers but not the SEARCH that would
     find one, so a report carrying a queryable fingerprint had no representable
     retrieval task;
  C  probe results were recorded after the gate that reads them, so retrieved
     evidence could never authorise an occurrence attribution.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ARCHON = Path(__file__).resolve().parents[2]
SETUP = ARCHON / "setup"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "rca-minimal"


def bugfix_contract():
    """bugfix-contract.py is not an importable module name; load it by path."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("bugfix_contract", SETUP / "bugfix-contract.py")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SETUP))
    spec.loader.exec_module(module)
    return module


def node(workflow, node_id):
    doc = yaml.safe_load((ARCHON / "workflows" / f"{workflow}.yaml").read_text(encoding="utf-8"))
    return next(n for n in doc["nodes"] if n["id"] == node_id)


def run_bash(body, artifacts, env=None):
    environ = dict(os.environ, ARTIFACTS_DIR=str(artifacts))
    environ.update(env or {})
    return subprocess.run(["bash", "-c", body], capture_output=True, encoding="utf-8", env=environ)


class CapabilityGateTest(unittest.TestCase):
    """Defect A: an unreachable prod DB stops a run that needs prod retrieval."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.body = node("bugfix", "capability-gate")["bash"]

    def write_caps(self, prod_db_status, reason="aws-session-expired"):
        (self.tmp / "capabilities.json").write_text(json.dumps({
            "schema_version": 1,
            "capabilities": {"prod-db": {"status": prod_db_status, "reason": reason}},
        }), encoding="utf-8")

    def write_report(self, classification, gap_line):
        (self.tmp / "bug-report.md").write_text(
            "# ENG-9999\n\nSomething broke.\n\n"
            f"## Intake gaps\n\n{gap_line}\n\n"
            f"## Classification\n\n{classification} — evidence: the reporter saw a wrong count.\n",
            encoding="utf-8")

    def test_defect_with_probe_gap_and_unreachable_db_stops(self):
        self.write_caps("UNAVAILABLE")
        self.write_report("defect", "- import id and timestamp — retrievable_by: probe")
        result = run_bash(self.body, self.tmp)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("CAPABILITY_GATE=FAIL capability-required reason=aws-session-expired",
                      result.stdout)

    def test_same_report_proceeds_once_the_db_is_reachable(self):
        self.write_caps("AVAILABLE", "reachable")
        self.write_report("defect", "- import id and timestamp — retrievable_by: probe")
        result = run_bash(self.body, self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("CAPABILITY_GATE=PASS", result.stdout)

    def test_local_repro_runs_with_zero_external_evidence(self):
        # The norm this gate must not break: nothing prod-shaped is required of a
        # report that never asked for prod.
        self.write_caps("UNAVAILABLE")
        self.write_report("defect", "- exact repo — retrievable_by: report-author")
        result = run_bash(self.body, self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("probe_gaps=0", result.stdout)

    def test_non_defect_classification_is_never_gated(self):
        self.write_caps("UNAVAILABLE")
        self.write_report("api-feature", "- import id — retrievable_by: probe")
        result = run_bash(self.body, self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("classification=api-feature", result.stdout)

    def test_hand_written_report_without_headings_is_never_gated(self):
        self.write_caps("UNAVAILABLE")
        (self.tmp / "bug-report.md").write_text("The importer duplicates rows.\n", encoding="utf-8")
        result = run_bash(self.body, self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("classification=unstated", result.stdout)

    def test_missing_capabilities_file_fails_closed(self):
        self.write_report("defect", "- import id — retrievable_by: probe")
        result = run_bash(self.body, self.tmp)
        self.assertEqual(result.returncode, 1)
        self.assertIn("CAPABILITY_GATE=FAIL no capabilities.json", result.stdout)


class CapabilityDiscoveryTest(unittest.TestCase):
    def test_preflight_discovers_capabilities_and_never_fails_on_aws(self):
        preflight = node("bugfix", "preflight")["bash"]
        self.assertIn("archon-run.py\" capabilities --artifacts", preflight)
        self.assertNotIn("PREFLIGHT=FAIL aws", preflight)

    def test_capabilities_json_names_every_evidence_source(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        result = subprocess.run(
            [sys.executable, str(SETUP / "archon-run.py"), "capabilities", "--artifacts", str(tmp)],
            capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        caps = json.loads((tmp / "capabilities.json").read_text(encoding="utf-8"))["capabilities"]
        # Sources Archon integrates with, plus the two it does not: naming an
        # absent source is what makes its absence visible instead of assumed.
        for name in ("aws-session", "prod-db", "cloudwatch", "sentry", "linear",
                     "gitnexus", "amplitude", "metabase"):
            self.assertIn(name, caps, name)
            self.assertIn(caps[name]["status"], ("AVAILABLE", "UNAVAILABLE"))
            self.assertTrue(caps[name]["reason"])
        self.assertEqual(caps["amplitude"]["status"], "UNAVAILABLE")

    def test_reader_guard_requires_replica_host_and_recovery(self):
        script = SETUP / "assert-ro.sh"
        result = subprocess.run(["bash", str(script)], capture_output=True, encoding="utf-8",
                                env=dict(os.environ, PGHOST="prod-primary.example.com"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("RO_GUARD=FAIL host-not-read-replica", result.stdout)

    def test_both_prod_query_nodes_assert_the_reader(self):
        for node_id in ("evidence-aws", "probe-run"):
            self.assertIn("assert-ro.sh", node("bugfix", node_id)["bash"], node_id)


class EvidencePlanRetrievalTest(unittest.TestCase):
    """Defect B: the plan must be able to express a search, not just a lookup."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def plan_check(self, plan):
        (self.tmp / "evidence-plan.json").write_text(json.dumps(plan), encoding="utf-8")
        body = node("bugfix", "intake-gate")["bash"]
        # Only the evidence-plan stanza; the ledger stanzas need a sealed run.
        start = body.index('python3 - "$ARTIFACTS_DIR/evidence-plan.json"')
        end = body.index("if [ -n \"${ARCHON_BUGFIX_CONTINUATION_SEED")
        return run_bash("set -euo pipefail\n" + body[start:end], self.tmp)

    def eng3860_plan(self):
        return {
            "identifiers": [{"kind": "fingerprint",
                             "value": "contacts=1171,notes=24,actions=672",
                             "resolution": "given"}],
            "time_window": None, "error_strings": [], "sentry_refs": [],
            "linear_refs": ["ENG-3860"], "repo_hint": "api", "local_repro_steps": None,
            "unknowns": [{"gap": "which account ran the import",
                          "retrievable_by": "probe",
                          "query_hint": "imports joined to contacts by owner, matching the counts"}],
        }

    def test_fingerprint_identifier_and_probe_unknown_are_accepted(self):
        result = self.plan_check(self.eng3860_plan())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unknowns_is_required(self):
        plan = self.eng3860_plan()
        del plan["unknowns"]
        self.assertEqual(self.plan_check(plan).returncode, 1)

    def test_retrievable_by_is_an_enum(self):
        plan = self.eng3860_plan()
        plan["unknowns"][0]["retrievable_by"] = "maybe"
        self.assertEqual(self.plan_check(plan).returncode, 1)

    def test_intake_prompt_offers_fingerprint_and_the_retrieval_queue(self):
        intake = node("bugfix", "intake")["prompt"]
        self.assertIn("fingerprint", intake)
        self.assertIn("retrievable_by", intake)
        self.assertIn("never licenses declining", intake)
        self.assertIn("fingerprint", (ARCHON / "setup/lite/bugfix/intake.prompt.md").read_text())

    def test_rca_prompt_ranks_occurrence_identification_first(self):
        rca = node("bugfix", "rca")["prompt"]
        self.assertIn("OCCURRENCE-IDENTIFICATION", rca)
        self.assertLess(rca.index("OCCURRENCE-IDENTIFICATION"), rca.index("Discriminating Probe from rca.md"))

    def test_linear_skill_emits_gap_and_retrievability_pairs(self):
        skill = (ARCHON / "skills/archon-linear/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("retrievable_by: probe|report-author|unretrievable", skill)
        self.assertIn("retrieval work queue", skill)


class OccurrenceEvidenceReachTest(unittest.TestCase):
    """Defect C: an occurrence-kind probe row must satisfy the contract check."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def record_probe(self, kind):
        (self.tmp / "probe-results.txt").write_text("rows: 1\n", encoding="utf-8")
        argv = [sys.executable, str(SETUP / "evidence-provenance.py"), "record",
                "--artifacts", str(self.tmp), "--source", "prod-probes",
                "--status", "complete", "--file", "probe-results.txt",
                "--provider", "claude", "--tool", "psql",
                "--query", "SELECT 1", "--baseline", "a" * 40, "--evidence-kind", kind]
        if kind == "occurrence":
            argv += ["--entity-watermark", "b" * 64,
                     "--occurrence-window", json.dumps({"start": "2026-08-01T00:00:00Z",
                                                        "end": "2026-08-02T00:00:00Z"})]
        result = subprocess.run(argv, capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_occurrence_row_authorises_attribution_and_a_class_row_does_not(self):
        # The real contract, not a restatement of it: the same function rca-gate
        # calls, against the same fixture every other RCA test uses.
        contract = bugfix_contract()
        shutil.copytree(FIXTURE, self.tmp, dirs_exist_ok=True)
        proof = json.loads((self.tmp / "proof-assessment.json").read_text(encoding="utf-8"))
        proof["occurrence_evidence_sources"] = ["prod-probes"]
        (self.tmp / "proof-assessment.json").write_text(json.dumps(proof), encoding="utf-8")

        # What the run produced before the reordering: probe-run's row landed
        # after this check, and the default kind is class either way.
        self.record_probe("class")
        with self.assertRaises(contract.ContractError):
            contract.validate_systematic_debugging(self.tmp)

        # What it produces now: an occurrence-kind row from the same node.
        self.record_probe("occurrence")
        result = contract.validate_systematic_debugging(self.tmp)
        self.assertEqual(result["active_hypothesis_id"], "H1")

    def test_probe_run_precedes_the_gate_that_reads_provenance(self):
        for workflow in ("bugfix", "bugfix-codex"):
            self.assertEqual(node(workflow, "probe-run")["depends_on"], ["rca"], workflow)
            self.assertEqual(node(workflow, "rca-gate")["depends_on"], ["probe-run"], workflow)

    def test_probe_run_validates_sql_with_the_same_script_the_gate_uses(self):
        self.assertIn("probe-shape.py", node("bugfix", "probe-run")["bash"])
        self.assertIn("probe-shape.py", (SETUP / "rca-shape.sh").read_text(encoding="utf-8"))

    def test_probe_shape_rejects_a_write_statement(self):
        (self.tmp / "probe.json").write_text(json.dumps(
            {"probes": [{"id": "p", "question": "q", "sql": "SELECT 1; DELETE FROM users"}]}),
            encoding="utf-8")
        result = subprocess.run([sys.executable, str(SETUP / "probe-shape.py"), str(self.tmp)],
                                capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, 1)
        self.assertIn("PROBE_SHAPE=FAIL", result.stdout)


if __name__ == "__main__":
    unittest.main()
