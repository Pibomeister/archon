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

    def test_the_checkpoint_precedes_every_exit_in_probe_run(self):
        # Run dc099da8 passed RCA_SHAPE=OK and still died on
        # "RCA_GATE=FAIL no reassess checkpoint": probe-run has eleven exit
        # points and the checkpoint was written at the END, so only the success
        # path produced one. Every degraded run -- expired session, no probes,
        # refused reader -- then failed the gate on a file it never wrote.
        import re
        body = node("bugfix", "probe-run")["bash"]
        lines = body.splitlines()
        checkpoint = [i for i, l in enumerate(lines) if "reassess-pre.sha256" in l]
        exits = [i for i, l in enumerate(lines) if re.search(r"\bexit [01]\b", l)]
        self.assertTrue(checkpoint, "probe-run writes no checkpoint")
        self.assertTrue(exits, "probe-run has no exits -- test assumption broken")
        self.assertLess(max(checkpoint), min(exits),
                        "the checkpoint must precede every exit it protects")

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
        self.assertIn("matching events, is a real answer", prompt)
        helper = SETUP / "occurrence-logs.py"
        self.assertTrue(helper.is_file())
        self.assertIn("setup/occurrence-logs.py", (SETUP / "package.sh").read_text(encoding="utf-8"))

    def test_occurrence_logs_does_not_re_pad_an_already_padded_window(self):
        # occurrence-window.py brackets the occurrence; padding again widened the
        # window by five more minutes each side, and with oldest-first ordering
        # the per-query cap was spent on earlier polling traffic before reaching
        # the request that started the occurrence. Measured against production:
        # padded-once/max40 finds the entrypoint, double-padded/max40 finds none.
        body = (SETUP / "occurrence-logs.py").read_text(encoding="utf-8")
        self.assertNotIn("PAD_MS", body)
        self.assertIn("MAX_REQUEST_EVENTS", body)
        # The request slice is what names the route, so it gets its own headroom.
        self.assertIn('"Controller"', body)

    def test_occurrence_logs_helper_is_bounded_and_read_only(self):
        body = (SETUP / "occurrence-logs.py").read_text(encoding="utf-8")
        self.assertIn("filter-log-events", body)
        for writer in ("put-log-events", "delete-", "create-"):
            self.assertNotIn(writer, body)
        self.assertIn("MAX_SUBJECTS", body)
        self.assertIn("MAX_EVENTS_PER_SUBJECT", body)
        self.assertIn("MAX_OUTPUT_BYTES", body)
        self.assertIn("TIMEOUT_SECONDS", body)

    def test_occurrence_logs_have_their_own_provenance_source(self):
        # `logs` names the early evidence-aws fetch, which ran before any probe
        # knew the entity and is class evidence. Run 61a7fd35 cited it for an
        # occurrence and was rejected: "occurrence evidence source is
        # incomplete, stale, or invalidated: logs".
        gate = node("bugfix", "rca-gate")["bash"]
        self.assertIn("--source occurrence-logs", gate)
        self.assertIn("evidence/occurrence-logs.txt", gate)
        prompt = node("bugfix", "rca-reassess")["prompt"]
        self.assertIn("`occurrence-logs`", prompt)
        self.assertIn("Do NOT cite `logs`", prompt)

    def test_a_capability_is_re_checked_at_the_point_of_use(self):
        # Run 0ceb2816: capabilities.json recorded prod-db=AVAILABLE at preflight
        # and probe-run needed those credentials ~40 minutes later, by which time
        # the ~15-minute SSO session had lapsed. The probes silently degraded and
        # the run produced exactly the unattributable RCA the capability gate
        # exists to prevent -- the original defect, displaced in time.
        probe = node("bugfix", "probe-run")["bash"]
        self.assertIn("capability-expired", probe)
        self.assertIn("retrievable_by", probe)
        self.assertIn("resume.sh", probe)
        # A stale AVAILABLE must be distinguishable from a live one.
        runner = (SETUP / "archon-run.py").read_text(encoding="utf-8")
        self.assertIn('"checked_at"', runner)

    def test_empty_occurrence_columns_mean_not_an_identification_probe(self):
        # Run e2634be9 reached class-hardening-only correctly and then died
        # because its census probes wrote [] for the occurrence columns rather
        # than omitting them. occurrence-window.py already treats [] as absent
        # (a truthiness check), so the validator was stricter than its own
        # consumer and rejected the natural way to say not-applicable.
        import subprocess, tempfile
        tmp = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        probes = {"probes": [
            {"id": "ident", "question": "q", "sql": "SELECT id, created_at FROM t LIMIT 10",
             "occurrence_subject_columns": ["id"], "occurrence_time_columns": ["created_at"]},
            {"id": "census", "question": "q", "sql": "SELECT COUNT(*) FROM t",
             "occurrence_subject_columns": [], "occurrence_time_columns": []},
        ]}
        (tmp / "probe.json").write_text(json.dumps(probes), encoding="utf-8")
        ok = subprocess.run([sys.executable, str(SETUP / "probe-shape.py"), str(tmp)],
                            capture_output=True, encoding="utf-8")
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        # Negative control: one set without the other is still a contract error.
        probes["probes"][1]["occurrence_subject_columns"] = ["id"]
        (tmp / "probe.json").write_text(json.dumps(probes), encoding="utf-8")
        bad = subprocess.run([sys.executable, str(SETUP / "probe-shape.py"), str(tmp)],
                             capture_output=True, encoding="utf-8")
        self.assertEqual(bad.returncode, 1)
        self.assertIn("when either is set", bad.stdout)

    def test_plan_accounts_for_blocking_layout_rules(self):
        # Run 38d72218 passed RED, GREEN, deslop and the negative control, then
        # SCOPE_BREACH on commit-import.util.ts: the reviewer filed a Blocking
        # finding citing business-logic.md:11-12 (a pure helper outside the
        # @Injectable class must move to a sibling .util.ts), the fixer complied,
        # and the file was not in the allowlist. repo-policy.json exposes only
        # the TESTING rules, so the planner never saw the layout rule the
        # reviewer enforces.
        rca = node("bugfix", "rca")["prompt"]
        self.assertIn("BLOCKING LAYOUT RULE", rca)
        self.assertIn("carries only the repo's TESTING rules", rca)
        self.assertIn("trips SCOPE_BREACH", rca)

    def test_fix_site_must_be_reachable_from_the_declared_test(self):
        # Run f53b1b58 planned fix_site at stage-user-import-handler.ts:701,
        # inside linkSameRowEntitiesToProfile -- a module-private function whose
        # only exported caller is the Lambda handler. The kind=unit RED test
        # could not import it, and RED may only touch the test file so it could
        # not add the export. The plan passed every gate; the failure surfaced
        # only after bind-repo, bootstrap and red-test were paid for.
        rca = node("bugfix", "rca")["prompt"]
        self.assertIn("REACHABLE FROM THE TEST YOU DECLARE", rca)
        self.assertIn("Module-private and reachable only through a heavy entrypoint", rca)
        gate = node("bugfix", "red-gate")["bash"]
        self.assertIn("is not a function", gate)
        self.assertIn("may only touch the test file", gate)

    def test_split_out_symptoms_still_need_a_coverage_row(self):
        # Run b7797562 wrote coverage for E1 only and failed with
        # coverage_missing=['E2','E3'] -- the two symptoms it had split to their
        # own tickets. The bijection is over EVERY effective symptom; a
        # split-out row is what records that the chain does not cover it.
        contract = (SETUP / "bugfix-contract.py").read_text(encoding="utf-8")
        self.assertIn("including ones dispositioned separate-ticket", contract)
        self.assertIn("records the absence of coverage", contract)
        rca = node("bugfix", "rca")["prompt"]
        self.assertIn("ONE ROW PER EFFECTIVE SYMPTOM", rca)
        self.assertIn("optional and not implied", rca)

    def test_incidental_finding_is_defined_because_it_gates_shipping(self):
        # Run d2ea8d56 dispositioned E1 class-hardening-only with
        # occurrence_attributed false and incidental_finding false, so classify()
        # returned implementation=NONE / approval_scope="none": a proven defect,
        # a written fix plan, and nothing shippable. The field shipped in the
        # schema with no definition at all, and it is the ONLY thing separating
        # CLASS_HARDENING from NONE.
        rca = node("bugfix", "rca")["prompt"]
        self.assertIn("decides", rca)
        self.assertIn("whether a run with no attributed occurrence ships at all", rca)
        self.assertIn("ship-hardening-only", rca)
        self.assertIn('approval scope "none"', rca)

    def test_separate_ticket_error_names_the_missing_field(self):
        # "requires repo and ticket_stub" sent a reviser at a repo that was
        # already correct while ticket_stub was the omission.
        contract = (SETUP / "bugfix-contract.py").read_text(encoding="utf-8")
        self.assertIn("is missing", contract)
        self.assertIn("ticket_stub (one-line title for the split ticket)", contract)
        rca = node("bugfix", "rca")["prompt"]
        self.assertIn("needs ALL THREE", rca)

    def test_class_hardening_is_presented_as_shippable(self):
        # Run 0ceb2816 wrote reproduction_status "class-only" AND disposition
        # "fixed" -- over-claiming, rejected as "fixed symptom E1 lacks
        # occurrence attribution". The honest class-hardening-only disposition
        # ships with residual acceptance, and nothing told the RCA that, so it
        # reached for `fixed` to look complete and killed the run instead.
        rca = node("bugfix", "rca")["prompt"]
        self.assertIn("FIRST-CLASS, SHIPPABLE outcome", rca)
        self.assertIn("Do not reach for `fixed` to make the run look complete", rca)
        reassess = node("bugfix", "rca-reassess")["prompt"]
        self.assertIn("class-hardening-only\" is the correct and shippable", reassess)

    def test_an_expired_session_degrades_rather_than_killing_the_run(self):
        # I first made this a hard stop. That was wrong: an unattributable
        # occurrence still leaves an honest class-hardening outcome this lane
        # ships, so killing the run throws away a paid-for RCA to avoid a claim
        # the contract already refuses on its own.
        probe = node("bugfix", "probe-run")["bash"]
        self.assertIn("PROBE_RUN=DEGRADED capability-expired", probe)
        self.assertNotIn("PROBE_RUN=FAIL capability-expired", probe)
        self.assertIn("can only reach class-hardening", probe)

    def test_a_local_repro_still_survives_an_expired_session(self):
        # Negative control on the guard above: with no probe-retrievable gap the
        # node must still DEGRADE rather than stop, or every local-repro bug dies
        # on an expired credential it never needed.
        probe = node("bugfix", "probe-run")["bash"]
        self.assertIn('PROBE_RUN=DEGRADED sso expired', probe)
        self.assertIn('if [ "${NEEDS:-0}" -gt 0 ]; then', probe)

    def test_boundary_evidence_claims_are_a_closed_set(self):
        # Run e3db3ae9 cleared every earlier blocker -- equivalent True, fix plan
        # filled, occurrence attributed -- then failed because reassess cited the
        # log by ADDING a fourth evidence row, claim "runtime-entrypoint-occurrence".
        # The list accepts exactly three ownership claims and rejects any other.
        prompt = node("bugfix", "rca-reassess")["prompt"]
        self.assertIn("CLOSED set of exactly three", prompt)
        self.assertIn("Do NOT add a row", prompt)
        contract = (SETUP / "bugfix-contract.py").read_text(encoding="utf-8")
        self.assertIn("belongs in surface_selection_basis", contract)

    def test_chain_citations_accept_the_repo_relative_paths_the_prompt_asks_for(self):
        # The RCA prompt says "Cite repo-relative paths as they exist at that
        # SHA". The resolver split the ref on the first "/" and required the head
        # to be a repo name, so a citation that followed the instruction
        # literally ("libs/data-access/.../note.repo.ts") was reported missing
        # while sitting in the pinned baseline. Run c8882a7f died on exactly that.
        gate = node("bugfix", "rca-gate")["bash"]
        self.assertIn("def git_specs(ref)", gate)
        self.assertIn("REPO-RELATIVE", gate)
        # The prefixed and <repo>@<sha> forms must keep working.
        self.assertIn('head in ("api", "web-app")', gate)
        self.assertIn('r"(api|web-app)@([0-9a-f]{7,40})"', gate)

    def test_reported_surface_status_is_about_the_surface_not_the_route(self):
        # Runs f459f074 and dc099da8 marked a report that explicitly names one
        # product surface ("imported 1,171 contacts from Mesh using a custom
        # mapping") as ambiguous, because the exact CLIENT was unknown -- while
        # simultaneously asserting all three runtime owners identical with both
        # booleans true, i.e. every candidate client converges on one owner.
        # The field is about the surface; the route is runtime_entrypoint.
        rca = node("bugfix", "rca")["prompt"]
        self.assertIn("describes the REPORTED SURFACE", rca)
        self.assertIn("does NOT make it ambiguous", rca)
        # The guard the field exists for must survive verbatim.
        self.assertIn("owners DIFFER", rca)
        self.assertIn("marking divergent surfaces", rca)

    def test_ambiguous_surface_error_names_both_exits(self):
        # Run f459f074 wrote a fix plan alongside reported_surface_status
        # "ambiguous" -- an internal contradiction the contract correctly
        # refuses. The message read as a shape complaint; the real choice is
        # evidential, and one of its two exits (get the runtime record) is
        # exactly what an expired AWS session takes away.
        contract = (SETUP / "bugfix-contract.py").read_text(encoding="utf-8")
        self.assertIn("captured runtime record", contract)
        self.assertIn("leave fix-plan.json empty", contract)
        self.assertIn("Never pick an entrypoint to make the plan pass", contract)

    def test_materiality_is_relative_to_the_selected_chain(self):
        # Run aa75cd46 diagnosed "finalization deletes staging, eliminating the
        # durable row-level evidence" and then listed "the probes did not return
        # the specific contacts/Note ids/resolution state" as a
        # changes-causal-boundary difference -- which is that chain being
        # CONFIRMED. A finding about destroyed evidence cannot be required to
        # reproduce the evidence it proved is destroyed.
        rca = node("bugfix", "rca")["prompt"]
        self.assertIn("material TO THE SELECTED CHAIN", rca)
        self.assertIn("does my", rca)
        self.assertIn("is the chain being CONFIRMED", rca)
        self.assertIn("not licence to empty the list", rca)

    def test_material_difference_impact_values_are_explained(self):
        # A gate blocks on changes-causal-boundary and the prompt shipped the
        # enum with no statement of what either value means.
        rca = node("bugfix", "rca")["prompt"]
        self.assertIn("changes-causal-boundary — the difference could make", rca)
        self.assertIn("covered-by-secondary-proof — the difference is real but", rca)
        self.assertIn("never relabel a row to clear the gate", rca)

    def test_a_probe_contradicting_the_chain_is_a_typed_stop(self):
        # Run 0f012f8a: the probes identified the occurrence AND disconfirmed the
        # chain (import 17592 had 47 notes, all linked; the census class it was
        # assigned to has zero overlap with it). reassess correctly refused to
        # attribute -- and the run then reported
        # "RCA_INVESTIGATION_REQUIRED reason=surface-ambiguous", pointing the
        # operator at a surface problem instead of a contradicted diagnosis.
        gate = node("bugfix", "rca-gate")["bash"]
        self.assertIn("PROBE_CONFLICT", gate)
        self.assertIn("reassess-assessment.json", gate)
        prompt = node("bugfix", "rca-reassess")["prompt"]
        self.assertIn("reassess-assessment.json", prompt)
        self.assertIn("not soften a conflict into cannot_determine", prompt)

    def test_repo_scope_rejects_a_repo_narrower_than_the_fix(self):
        # repo names the repository THIS CHAIN CHANGES. Scoping it over the
        # ticket turns any bug with a cross-repo symptom into CROSS_REPO_BUG.
        self.assertIn("REPO_SCOPE", (SETUP / "rca-shape.sh").read_text(encoding="utf-8"))
        rca = node("bugfix", "rca")["prompt"]
        self.assertIn("repo is the repository THIS CHAIN CHANGES", rca)


if __name__ == "__main__":
    unittest.main()
