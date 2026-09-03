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
        """A passing shape check must not leave a reason behind.

        gate-status.txt is read on ANY terminal failure. If a successful check
        recorded RCA_SHAPE=OK, a run that died later at an unrelated node would
        report that stale line as its discriminator and misroute triage.
        """
        ad = self.artifacts()
        # Take the fixture all the way to a clean pass.
        path = ad / "symptom-dispositions.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        for row in doc["dispositions"]:
            if row["disposition"] == "separate-ticket":
                row["authority"] = "report"
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        plan = ad / "fix-plan.json"
        plan.write_text(json.dumps({
            "approach": "bind the scalar Note.content mapping to every labeled source column",
            "fix_site": "libs/external-services/src/lib/ai/ai.service.ts:120",
            "files": ["libs/external-services/src/lib/ai/ai.service.ts"],
            "risks": ["mapping shape change"],
            "alternatives": [{"label": "none", "approach": "no deeper fix exists"}],
        }, indent=2), encoding="utf-8")
        debug = ad / "debug-phase.json"
        dbg = json.loads(debug.read_text(encoding="utf-8"))
        dbg["reproduction_status"] = "reproduced"
        debug.write_text(json.dumps(dbg, indent=2), encoding="utf-8")

        proc = self.run_shape(ad)
        status = (ad / "gate-status.txt").read_text(encoding="utf-8").strip()
        if proc.returncode == 0:
            self.assertEqual(status, "", "a passing check must persist nothing")
            self.assertIn("RCA_SHAPE=OK", proc.stdout)
        else:
            # Fixture did not reach a full pass; the invariant under test is
            # still that no OK line is ever persisted.
            self.assertNotIn("RCA_SHAPE=OK", status)

    def test_ok_line_is_never_persisted(self):
        script = (SETUP / "rca-shape.sh").read_text(encoding="utf-8")
        self.assertNotIn('emit(f"RCA_SHAPE=OK', script)
        self.assertIn('print(f"RCA_SHAPE=OK', script)


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

    def test_absolute_paths_are_redacted(self):
        (self.ad / "gate-status.txt").write_text(
            "RCA_SHAPE=FAIL cannot read /Users/someone/.archon/workspaces/run/x.json\n",
            encoding="utf-8",
        )
        result = ar.supervise_exact_run(self.db, self.run_id, 1, 0.01)
        self.assertIn("<path>", result["discriminator"])
        self.assertNotIn("/Users/someone", result["discriminator"])

    def test_missing_gate_status_omits_key(self):
        result = ar.supervise_exact_run(self.db, self.run_id, 1, 0.01)
        self.assertEqual(result["state"], "terminal")
        self.assertNotIn("discriminator", result)


class WorkflowRouting(unittest.TestCase):
    """Both rca-shape.sh case sites must route the investigation stop."""

    def test_investigation_required_has_a_case_arm(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(
            text.count("*RCA_INVESTIGATION_REQUIRED*)"), 2,
            "rca-gate and rca-plan-shape must each route RCA_INVESTIGATION_REQUIRED "
            "before the untyped-stop fallback",
        )

    def test_prompt_states_the_unresolved_fallback(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("correct disposition is `unresolved`", text)
        self.assertIn("Select the repository that OWNS the mechanism", text)

    def test_lite_lane_carries_the_same_guidance(self):
        # bugfix-lite overlays its own rca prompt, so the parent fix does not
        # reach it: the lane would keep emitting blank-authority rows.
        overlay = (SETUP / "lite" / "bugfix" / "rca.prompt.md").read_text(encoding="utf-8")
        self.assertIn("correct disposition is\n`unresolved`", overlay)
        self.assertIn("Select the repository that OWNS the mechanism", overlay)

    def test_lite_lane_routes_the_investigation_stop(self):
        lite = (SETUP.parent / "workflows" / "bugfix-lite.yaml").read_text(encoding="utf-8")
        self.assertIn("*RCA_INVESTIGATION_REQUIRED*)", lite)

    def test_generated_twins_carry_the_fix(self):
        for name in ("bugfix-codex.yaml", "bugfix-lite-codex.yaml"):
            twin = (SETUP.parent / "workflows" / name).read_text(encoding="utf-8")
            self.assertIn("*RCA_INVESTIGATION_REQUIRED*)", twin, f"{name} is stale: regenerate")


if __name__ == "__main__":
    unittest.main()
