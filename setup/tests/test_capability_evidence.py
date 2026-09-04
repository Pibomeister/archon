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
        # Observed live: one run matched user_imports.metadata->summary and found
        # the occurrence; the next recomputed the same counts from
        # user_import_actions and got zero rows for a row that was sitting there.
        self.assertIn("SUMMARY THE PRODUCT ITSELF RECORDED", rca)
        self.assertIn("zero rows from the wrong column is not evidence of absence", rca)
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
            self.assertEqual(node(workflow, "rca-gate")["depends_on"], ["rca-reassess"], workflow)

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


class RetrievalCanChangeTheVerdictTest(unittest.TestCase):
    """The blocker that stopped BOTH ENG-3860 runs, and its two siblings.

    Run 554842f9 and its successor c88d2571 died on the same contract line, and
    the surrounding shape hid two more dead ends. All three share one cause:
    evidence retrieved after a decision could not change that decision, and a
    symptom the chain does not fix could not be closed at all.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_separate_ticket_is_closable_with_a_citation(self):
        # by-design claims INTENT and needs a product receipt an unattended run
        # cannot get. separate-ticket claims a different MECHANISM, which is an
        # evidential claim the RCA is entitled to make. Requiring a receipt for
        # both made separate-ticket unreachable -- and it is the only non-open
        # way to close a symptom the chain does not fix, so every multi-symptom
        # report was unclosable.
        contract = bugfix_contract()
        ledger = json.loads((FIXTURE / "symptoms.json").read_text(encoding="utf-8"))
        row = {"symptom_id": "E1", "disposition": "separate-ticket",
               "repo": "api", "ticket_stub": "Split: different mechanism",
               "authority": "libs/import/src/mapping.ts:88 -- distinct code path"}
        result = contract.classify(ledger, {"E1": row}, incidental_mechanism=False)
        self.assertEqual(result["ticket_disposition"], "DISPOSITION_COMPLETE")

    def test_separate_ticket_still_refuses_a_silent_scope_out(self):
        # Negative control: the citation requirement itself must survive, or the
        # fix becomes a licence to drop symptoms.
        contract = bugfix_contract()
        ledger = json.loads((FIXTURE / "symptoms.json").read_text(encoding="utf-8"))
        row = {"symptom_id": "E1", "disposition": "separate-ticket",
               "repo": "api", "ticket_stub": "Split", "authority": "   "}
        with self.assertRaises(contract.ContractError) as caught:
            contract.classify(ledger, {"E1": row}, incidental_mechanism=False)
        self.assertIn("cite the evidence", str(caught.exception))

    def test_by_design_still_requires_a_product_receipt(self):
        # The distinction is the whole point: intent is not an evidential claim.
        contract = bugfix_contract()
        ledger = json.loads((FIXTURE / "symptoms.json").read_text(encoding="utf-8"))
        row = {"symptom_id": "E1", "disposition": "by-design", "repo": "api", "authority": ""}
        with self.assertRaises(contract.ContractError):
            contract.classify(ledger, {"E1": row}, incidental_mechanism=False)

    def test_reassess_runs_between_the_probe_and_the_gate(self):
        for workflow in ("bugfix", "bugfix-codex"):
            self.assertEqual(node(workflow, "rca-reassess")["depends_on"], ["probe-run"], workflow)
            self.assertEqual(node(workflow, "rca-gate")["depends_on"], ["rca-reassess"], workflow)

    def test_reassess_may_not_touch_the_diagnosis(self):
        probe = node("bugfix", "probe-run")["bash"]
        gate = node("bugfix", "rca-gate")["bash"]
        self.assertIn("reassess-pre.sha256", probe)
        self.assertIn('reassess-pre.sha256', gate)
        self.assertIn("RCA_REASSESS=DIRTY", gate)
        prompt = node("bugfix", "rca-reassess")["prompt"]
        for frozen in ("rca.md", "causal-chain.json", "hypotheses.json",
                       "residuals.json", "probe.json", "repo.json"):
            self.assertIn(frozen, prompt)

    def test_lite_lane_has_neither_probe_nor_reassess_and_its_gate_still_loads(self):
        doc = yaml.safe_load((ARCHON / "workflows/bugfix-lite.yaml").read_text(encoding="utf-8"))
        ids = {n["id"] for n in doc["nodes"]}
        self.assertNotIn("probe-run", ids)
        self.assertNotIn("rca-reassess", ids)
        self.assertEqual(node("bugfix-lite", "rca-gate")["depends_on"], ["rca"])
        # The inherited checkpoint check must be conditional, or lite dies on a
        # file only probe-run writes.
        self.assertIn('if [ -f "$AD/probe-results.txt" ]; then',
                      node("bugfix-lite", "rca-gate")["bash"])

    def test_occurrence_window_from_an_unanswered_probe_is_refused(self):
        gate = node("bugfix", "rca-gate")["bash"]
        self.assertIn("occurrence-window.json written for a probe that returned nothing", gate)
        self.assertIn("--evidence-kind occurrence", gate)

    def test_runtime_owner_fields_state_their_equality_contract(self):
        # bugfix-contract compares the three *_runtime_owner fields with `!=`,
        # but the prompt described the field only as "<implementation that
        # actually serves it>". Five runs running, every one wrote a call path
        # in one field and a bare symbol in another -- a guaranteed mismatch
        # that reads as "the test does not reach the runtime" when it is only
        # formatting.
        rca = node("bugfix", "rca")["prompt"]
        self.assertIn("EXACT STRING EQUALITY", rca)
        self.assertIn("byte-identically in all three", rca)
        self.assertIn("never make the strings match to get past the gate", rca)
        contract = (SETUP / "bugfix-contract.py").read_text(encoding="utf-8")
        self.assertIn('surface["test_runtime_owner"].strip() != runtime_owner', contract)
        self.assertIn('surface["smoke_runtime_owner"].strip() != runtime_owner', contract)

    def test_prompt_and_contract_agree_on_what_active_means(self):
        # bugfix-contract counts BOTH open and confirmed-by-experiment as active
        # and requires exactly one; the prompt said "keep exactly one hypothesis
        # open". Run 5e9c7682 obeyed the prompt (one confirmed, one open) and
        # died on "exactly one active hypothesis is required".
        rca = node("bugfix", "rca")["prompt"]
        self.assertIn("BOTH \"open\" and", rca)
        self.assertIn("confirmed-by-experiment\" as active", rca)
        contract = (SETUP / "bugfix-contract.py").read_text(encoding="utf-8")
        self.assertIn('{"open", "confirmed-by-experiment"}', contract)

    def test_an_existing_window_obliges_the_attribution_flag(self):
        # rca-gate promotes the probe row to occurrence evidence whenever the
        # window file exists, so an assessment that leaves occurrence_attributed
        # false alongside it denies an identification the run already recorded.
        prompt = node("bugfix", "rca-reassess")["prompt"]
        self.assertIn("the occurrence IS", prompt)
        self.assertIn("`occurrence_attributed` true", prompt)

    def test_retrieval_stays_in_bash_because_model_nodes_have_no_network(self):
        # Codex runs model nodes under sandbox_mode="workspace-write", which
        # denies network. Run a9b2b0fb proved it: a helper invoked from the
        # rca-reassess prompt returned "Could not connect to the endpoint URL"
        # for a query that succeeds from a bash node seconds earlier. Every
        # network call in this lane lives in a bash node, without exception.
        doc = yaml.safe_load((ARCHON / "workflows/bugfix.yaml").read_text(encoding="utf-8"))
        for entry in doc["nodes"]:
            prompt = entry.get("prompt") or ""
            for forbidden in ("occurrence-logs.py", "aws logs ", "aws sts ", "curl "):
                self.assertNotIn(forbidden, prompt,
                                 f"{entry['id']} prompt asks a model node to reach the network")
        probe = node("bugfix", "probe-run")["bash"]
        self.assertIn("occurrence-window.py", probe)
        self.assertIn("occurrence-logs.py", probe)

    def test_the_occurrence_window_is_derived_not_asserted(self):
        # A model-written window would let a run manufacture attribution
        # authority: rca-gate promotes the probe row to occurrence evidence off
        # that file. It now comes off the matched row's own columns.
        prompt = node("bugfix", "rca-reassess")["prompt"]
        self.assertIn("You do not write", prompt)
        body = (SETUP / "occurrence-window.py").read_text(encoding="utf-8")
        self.assertIn("match-count=", body)
        rca = node("bugfix", "rca")["prompt"]
        self.assertIn("occurrence_subject_columns", rca)
        self.assertIn("occurrence_time_columns", rca)

    def test_logs_are_reachable_after_the_occurrence_is_identified(self):
        # evidence-aws queries CloudWatch ONCE, early, from intake-time error
        # strings -- before any probe knows which entity the report is about.
        # So a run could identify an occurrence and still be unable to ask the
        # logs about it: the same defect class as recording probe results after
        # the gate that reads them.
        prompt = node("bugfix", "rca-reassess")["prompt"]
        self.assertIn("evidence/occurrence-logs.txt", prompt)
        self.assertIn("zero\nmatching events, is a real answer", prompt)
        helper = SETUP / "occurrence-logs.py"
        self.assertTrue(helper.is_file())
        self.assertIn("setup/occurrence-logs.py", (SETUP / "package.sh").read_text(encoding="utf-8"))

    def test_occurrence_logs_helper_is_bounded_and_read_only(self):
        body = (SETUP / "occurrence-logs.py").read_text(encoding="utf-8")
        self.assertIn("filter-log-events", body)
        for writer in ("put-log-events", "delete-", "create-"):
            self.assertNotIn(writer, body)
        self.assertIn("MAX_SUBJECTS", body)
        self.assertIn("MAX_EVENTS_PER_SUBJECT", body)
        self.assertIn("MAX_OUTPUT_BYTES", body)
        self.assertIn("TIMEOUT_SECONDS", body)

    def test_repo_scope_rejects_a_repo_narrower_than_the_fix(self):
        # repo names the repository THIS CHAIN CHANGES. Scoping it over the
        # ticket turns any bug with a cross-repo symptom into CROSS_REPO_BUG.
        self.assertIn("REPO_SCOPE", (SETUP / "rca-shape.sh").read_text(encoding="utf-8"))
        rca = node("bugfix", "rca")["prompt"]
        self.assertIn("repo is the repository THIS CHAIN CHANGES", rca)


if __name__ == "__main__":
    unittest.main()
