#!/usr/bin/env python3
"""Regressions for the ENG-3860 composite defect.

Run 554842f9 died as an opaque `status=failed`. Three independent faults
stacked up: the RCA prompt let a `separate-ticket` row ship with a blank
`authority` (which the deterministic contract rejects), the typed reason for
that rejection never survived the workflow executor's failure path, and the
`RCA_INVESTIGATION_REQUIRED` stop -- the CORRECT terminal state for an
evidence-thin report -- was mistyped as "exited without a typed line".
"""
import importlib.util
import json
import shutil
import subprocess
import sqlite3
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parent.parent
RCA_SHAPE = SETUP / "rca-shape.sh"
CONTRACT = SETUP / "bugfix-contract.py"
WORKFLOW = SETUP.parent / "workflows" / "bugfix.yaml"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "rca-thin-report"

spec = importlib.util.spec_from_file_location("archon_run", SETUP / "archon-run.py")
assert spec and spec.loader
ar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ar)


class TypedStopPersistence(unittest.TestCase):
    """rca-shape.sh must leave a routable discriminator on disk."""

    def artifacts(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ad = Path(tmp.name) / "run"
        shutil.copytree(FIXTURE, ad)
        return ad

    def run_shape(self, ad: Path):
        return subprocess.run(
            ["bash", str(RCA_SHAPE), str(ad)],
            capture_output=True, text=True,
        )

    def test_blank_authority_persists_contract_reason(self):
        ad = self.artifacts()
        proc = self.run_shape(ad)
        self.assertEqual(proc.returncode, 1)
        status = (ad / "gate-status.txt").read_text(encoding="utf-8")
        self.assertIn("RCA_SHAPE=FAIL", status)
        self.assertIn("separate-ticket requires authority", status)

    def test_investigation_required_persists_typed_reason(self):
        ad = self.artifacts()
        path = ad / "symptom-dispositions.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        for row in doc["dispositions"]:
            if row["disposition"] == "separate-ticket":
                row["authority"] = "report"
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")

        proc = self.run_shape(ad)
        self.assertEqual(proc.returncode, 1)
        status = (ad / "gate-status.txt").read_text(encoding="utf-8")
        self.assertIn("RCA_INVESTIGATION_REQUIRED", status)
        self.assertIn("ticket=open", status)
        # The contract reason must NOT still be the surfaced line: populating
        # authority moves the run to a different, later typed stop.
        self.assertNotIn("separate-ticket requires authority", status)

    def test_gate_status_is_truncated_not_appended_across_runs(self):
        ad = self.artifacts()
        self.run_shape(ad)
        self.run_shape(ad)
        lines = [
            ln for ln in (ad / "gate-status.txt").read_text(encoding="utf-8").splitlines() if ln.strip()
        ]
        self.assertEqual(len(lines), 1, f"gate-status.txt accumulated stale lines: {lines}")


    def test_unwritable_run_dir_still_prints_the_typed_stop(self):
        # The surfacing path must never become a silent stop of its own.
        ad = self.artifacts()
        ad.chmod(0o555)
        self.addCleanup(ad.chmod, 0o755)
        proc = self.run_shape(ad)
        self.assertEqual(proc.returncode, 1)
        # The guarantee is the typed stop still reaches stdout and the script
        # does not abort before emitting it. An unwritable run dir also breaks
        # bugfix-contract.py's own artifact writes -- that stderr is not ours.
        self.assertIn("RCA_SHAPE=FAIL", proc.stdout)
        self.assertNotIn("gate-status.txt: Permission denied", proc.stderr)


    def test_success_leaves_no_stale_discriminator(self):
        """A passing shape check must persist nothing.

        gate-status.txt is read on ANY terminal status. If a successful check
        recorded RCA_SHAPE=OK, a run that died later at an unrelated node would
        report that stale line as its discriminator and misroute triage.

        This uses rca-minimal, which reaches a clean exit 0 -- the thin-report
        fixture cannot pass, so asserting against it proved nothing.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ad = Path(tmp.name) / "run"
        shutil.copytree(Path(__file__).resolve().parent / "fixtures" / "rca-minimal", ad)

        proc = self.run_shape(ad)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("RCA_SHAPE=OK", proc.stdout)
        self.assertEqual(
            (ad / "gate-status.txt").read_text(encoding="utf-8").strip(), "",
            "a passing check persisted a discriminator")

    def test_ok_line_is_never_persisted(self):
        script = (SETUP / "rca-shape.sh").read_text(encoding="utf-8")
        self.assertNotIn('emit(f"RCA_SHAPE=OK', script)
        self.assertIn('print(f"RCA_SHAPE=OK', script)


    def test_contract_crash_never_becomes_the_discriminator(self):
        """A traceback is not a typed stop.

        $CONTRACT_OUT is captured with 2>&1, so an uncaught exception inside
        bugfix-contract.py would otherwise be persisted verbatim and surfaced
        as the routing signal -- the exact opaque failure this file exists to
        eliminate.
        """
        ad = self.artifacts()
        path = ad / "symptom-dispositions.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        for row in doc["dispositions"]:
            if row["disposition"] == "separate-ticket":
                row["authority"] = "report"
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        # A list where the contract expects an object: crashes it mid-validate.
        (ad / "boundary-trace.json").write_text("[]", encoding="utf-8")

        proc = subprocess.run(["bash", str(RCA_SHAPE), str(ad), "RCA_GATE"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        lines = [l for l in (ad / "gate-status.txt").read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual(len(lines), 1, f"expected exactly one typed line, got {lines}")
        self.assertTrue(lines[0].startswith("RCA_GATE=FAIL"), lines[0])
        self.assertNotIn("Traceback", lines[0])
        self.assertNotIn("AttributeError", lines[0])

    def test_persisted_line_carries_the_caller_token(self):
        for token, expected in (("RCA_GATE", "RCA_GATE=FAIL"),
                                ("RCA_PLAN_SHAPE", "RCA_PLAN_SHAPE=FAIL"),
                                (None, "RCA_SHAPE=FAIL")):
            ad = self.artifacts()
            argv = ["bash", str(RCA_SHAPE), str(ad)] + ([token] if token else [])
            subprocess.run(argv, capture_output=True, text=True)
            status = (ad / "gate-status.txt").read_text(encoding="utf-8").strip()
            self.assertTrue(status.startswith(expected), f"{token}: {status}")

    def test_persisted_reason_is_always_one_line(self):
        ad = self.artifacts()
        subprocess.run(["bash", str(RCA_SHAPE), str(ad), "RCA_GATE"],
                       capture_output=True, text=True)
        raw = (ad / "gate-status.txt").read_text(encoding="utf-8")
        self.assertEqual(len([l for l in raw.splitlines() if l.strip()]), 1)


class ContractAuthority(unittest.TestCase):
    """Report authority is admissible; a blank string never is."""

    def classify(self, authority: str):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ad = Path(tmp.name) / "run"
        shutil.copytree(FIXTURE, ad)
        path = ad / "symptom-dispositions.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        for row in doc["dispositions"]:
            if row["disposition"] == "separate-ticket":
                row["authority"] = authority
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        return subprocess.run(
            ["python3", str(CONTRACT), "classify", str(ad)],
            capture_output=True, text=True,
        )

    def test_blank_authority_rejected(self):
        proc = self.classify("")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("separate-ticket requires authority", proc.stdout + proc.stderr)

    def test_report_authority_accepted(self):
        proc = self.classify("report")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class TerminalDiscriminator(unittest.TestCase):
    """A terminal `failed` must carry the typed reason, redacted."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.run_id = "b" * 32
        self.db = self.root / "archon.db"
        con = sqlite3.connect(self.db)
        con.execute(
            "CREATE TABLE remote_agent_workflow_runs "
            "(id TEXT, workflow_name TEXT, user_message TEXT, status TEXT, output_root TEXT, started_at TEXT)"
        )
        con.execute("CREATE TABLE remote_agent_workflow_events (workflow_run_id TEXT, created_at TEXT, node_name TEXT, payload TEXT)")
        con.execute(
            "INSERT INTO remote_agent_workflow_runs VALUES (?,?,?,?,?,?)",
            (self.run_id, "bugfix-codex", "/tmp/report.md", "failed", str(self.root / "out"), "2026-09-03 10:00:00"),
        )
        con.commit(); con.close()
        self.ad = self.root / "out" / "artifacts" / "runs" / self.run_id
        self.ad.mkdir(parents=True)

    def test_terminal_surfaces_discriminator(self):
        (self.ad / "gate-status.txt").write_text(
            "RCA_INVESTIGATION_REQUIRED reason=surface-ambiguous ticket=open no_implementation=true\n",
            encoding="utf-8",
        )
        result = ar.supervise_exact_run(self.db, self.run_id, 1, 0.01)
        self.assertEqual(result["state"], "terminal")
        self.assertEqual(result["status"], "failed")
        self.assertIn("RCA_INVESTIGATION_REQUIRED", result["discriminator"])

    def test_last_line_wins(self):
        (self.ad / "gate-status.txt").write_text(
            "RCA_SHAPE=FAIL earlier\nRCA_INVESTIGATION_REQUIRED reason=surface-ambiguous ticket=open\n",
            encoding="utf-8",
        )
        result = ar.supervise_exact_run(self.db, self.run_id, 1, 0.01)
        self.assertIn("RCA_INVESTIGATION_REQUIRED", result["discriminator"])
        self.assertNotIn("earlier", result["discriminator"])

    def test_machine_prefixes_are_stripped(self):
        (self.ad / "gate-status.txt").write_text(
            f"RCA_GATE=FAIL cannot read {self.ad}/x.json\n", encoding="utf-8")
        result = ar.supervise_exact_run(self.db, self.run_id, 1, 0.01)
        self.assertIn("<run>/x.json", result["discriminator"])
        self.assertNotIn(str(self.ad), result["discriminator"])

    def test_repo_relative_paths_survive_redaction(self):
        """The path IS the diagnostic for these two reasons.

        A path-shaped regex broad enough to catch an absolute path also eats
        `api/src/...`, truncating the only actionable content in the message.
        """
        for reason in (
            "RCA_GATE=FAIL fix-plan.files not subset of files-allowlist: "
            "['api/src/search/hybrid.service.ts']",
            "RCA_GATE=FAIL verify.json test_patterns must be unit specs: "
            "['app/routes/x.int.spec.ts']",
        ):
            (self.ad / "gate-status.txt").write_text(reason + "\n", encoding="utf-8")
            got = ar.supervise_exact_run(self.db, self.run_id, 1, 0.01)["discriminator"]
            self.assertEqual(got, reason, "redaction destroyed the actionable path")

    def test_home_is_stripped_even_with_a_space_in_the_path(self):
        home = str(Path.home())
        (self.ad / "gate-status.txt").write_text(
            f"RCA_GATE=FAIL leaked {home}/my docs/secret.json\n", encoding="utf-8")
        got = ar.supervise_exact_run(self.db, self.run_id, 1, 0.01)["discriminator"]
        self.assertNotIn(home, got)
        self.assertIn("~/my docs/secret.json", got)

    def test_control_tokens_are_redacted(self):
        (self.ad / "gate-status.txt").write_text(
            "RCA_GATE=FAIL launch failed control_token=s3cr3tvalue\n", encoding="utf-8")
        got = ar.supervise_exact_run(self.db, self.run_id, 1, 0.01)["discriminator"]
        self.assertNotIn("s3cr3tvalue", got)
        self.assertIn("control_token=<redacted>", got)

    def test_missing_gate_status_omits_key(self):
        result = ar.supervise_exact_run(self.db, self.run_id, 1, 0.01)
        self.assertEqual(result["state"], "terminal")
        self.assertNotIn("discriminator", result)


class WorkflowRouting(unittest.TestCase):
    """Both rca-shape.sh case sites must route the investigation stop."""

    def test_investigation_required_has_a_case_arm(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(
            text.count("*RCA_INVESTIGATION_REQUIRED*)"), 4,
            "all four call sites must route RCA_INVESTIGATION_REQUIRED: rca-gate, "
            "rca-plan-shape, and both planning-loop converge sites -- the loop sites "
            "otherwise mistype a valid open investigation as shape drift",
        )

    def test_prompt_states_the_unresolved_fallback(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("correct disposition is `unresolved`", text)
        self.assertIn("Select the repository that OWNS the mechanism", text)

    def test_lite_lane_carries_the_same_guidance(self):
        # bugfix-lite overlays its own rca prompt, so the parent fix does not
        # reach it: the lane would keep emitting blank-authority rows.
        overlay = (SETUP / "lite" / "bugfix" / "rca.prompt.md").read_text(encoding="utf-8")
        self.assertIn("correct disposition is", overlay)
        self.assertIn("`unresolved`", overlay)
        self.assertIn("Select the repository that OWNS the mechanism", overlay)

    def test_lite_lane_routes_the_investigation_stop(self):
        lite = (SETUP.parent / "workflows" / "bugfix-lite.yaml").read_text(encoding="utf-8")
        self.assertIn("*RCA_INVESTIGATION_REQUIRED*)", lite)

    def test_every_call_site_passes_its_caller_token(self):
        """All four call sites, or an operator cannot route the discriminator."""
        text = WORKFLOW.read_text(encoding="utf-8")
        calls = [ln for ln in text.splitlines() if "rca-shape.sh" in ln and "2>&1" in ln]
        self.assertEqual(len(calls), 4, f"expected 4 call sites, found {len(calls)}")
        for ln in calls:
            self.assertRegex(ln.strip(), r"rca-shape\.sh.*(RCA_GATE|RCA_PLAN_SHAPE) 2>&1",
                             f"call site passes no caller token: {ln.strip()}")
        lite = (SETUP.parent / "workflows" / "bugfix-lite.yaml").read_text(encoding="utf-8")
        for ln in [l for l in lite.splitlines() if "rca-shape.sh" in l and "2>&1" in l]:
            self.assertRegex(ln.strip(), r"rca-shape\.sh.*(RCA_GATE|RCA_PLAN_SHAPE) 2>&1", ln.strip())

    def test_converge_sites_type_the_investigation_stop_distinctly(self):
        """The loop sites must not report an open investigation as shape drift.

        A reviser that blanks fix-plan.approach leaves a valid open
        investigation; typing it `RCA_CONVERGE=FAIL shape` routes the operator
        to the repair-the-contracts row instead of evidence-gathering.
        """
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(text.count("RCA_CONVERGE=FAIL investigation-required"), 2,
                         "both planning-loop converge sites need the distinct token")

    def test_authority_prompt_demands_a_citation_not_a_bare_token(self):
        """A bare `report` is self-certifying; the contract only checks truthiness."""
        for path in (WORKFLOW, SETUP / "lite" / "bugfix" / "rca.prompt.md"):
            text = path.read_text(encoding="utf-8")
            self.assertIn("report:<verbatim clause from the sealed report>", text, str(path))

    def test_generated_twins_carry_the_fix(self):
        for name in ("bugfix-codex.yaml", "bugfix-lite-codex.yaml",
                     "bugfix-grok.yaml", "bugfix-lite-grok.yaml"):
            twin = (SETUP.parent / "workflows" / name).read_text(encoding="utf-8")
            self.assertIn("*RCA_INVESTIGATION_REQUIRED*)", twin, f"{name} is stale: regenerate")


if __name__ == "__main__":
    unittest.main()
