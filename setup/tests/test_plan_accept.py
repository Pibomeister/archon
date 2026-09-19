#!/usr/bin/env python3
"""full-sdlc-api plan-loop: accept-by-hand at the round cap (plan-accept.txt),
the mirror of rca-plan-accept.txt. Uses the plan-minimal fixture, which passes
plan-shape.sh. Only the cap branch of plan-converge is under test: the run is
staged so the verdict is REVISE with a moved plan."""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

SETUP = Path(__file__).resolve().parent.parent
ARCHON = SETUP.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "plan-minimal"


def converge_bash():
    doc = yaml.safe_load((ARCHON / "workflows" / "full-sdlc-api.yaml").read_text(encoding="utf-8"))
    loop = next(n for n in doc["nodes"] if n["id"] == "plan-loop")
    return next(b for b in loop["loop_group"]["nodes"] if b["id"] == "plan-converge")["bash"]


class PlanAccept(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ad = self.tmp / "ad"
        shutil.copytree(FIXTURE, self.ad)
        self.wt = self.tmp / "wt"
        shutil.copytree(FIXTURE, self.wt)  # premise evidence files, if any, resolve here
        (self.ad / "params.json").write_text(json.dumps({"spec": str(self.ad / "spec.md"), "slug": "x", "branch": "archon/x", "worktree": str(self.wt)}))
        (self.ad / "plan-round.txt").write_text("1\n")
        (self.ad / "plan-round-cap.txt").write_text("1\n")
        self.rd = self.ad / "plan-round-1"
        self.rd.mkdir()
        (self.rd / "pre-plan.md").write_text((self.ad / "plan.md").read_text() + "\n<!-- pre -->\n")
        for f in ("files-allowlist.json", "verify.json", "reader-audit.json"):
            shutil.copy(self.ad / f, self.rd / f"pre-{f}")
        (self.rd / "revision.json").write_text(json.dumps({"applied": [{"id": 1}], "declined": []}))

    def critique(self, findings):
        (self.rd / "critique.json").write_text(json.dumps({"verdict": "REVISE", "findings": findings}))

    def go(self):
        script = converge_bash()
        return subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            encoding="utf-8",
            env={
                **os.environ,
                "ARTIFACTS_DIR": str(self.ad),
                "ARCHON_LAYER": str(ARCHON),
                "PROJECT_ROOT": str(self.tmp),
            },
        )

    def test_cap_without_accept_stops(self):
        self.critique([{"severity": "P1", "kind": "gap", "confidence": 100}])
        r = self.go()
        self.assertNotIn("<promise>PLAN_CONVERGED</promise>", r.stdout)
        self.assertIn("PLAN_ROUND_CAP round=1 cap=1", r.stdout + r.stderr)

    def test_reject_detects_joint_contract_mutation(self):
        (self.rd / "critique.json").write_text(json.dumps({"verdict": "REJECT", "findings": []}))
        shutil.copy(self.ad / "plan.md", self.rd / "plan.pre.md")
        (self.rd / "pre-joint-plan.json").write_text('{"contracts": []}')
        (self.ad / "joint-plan.json").write_text('{"contracts": [{"artifact": "changed"}]}')
        result = self.go()
        self.assertIn("joint-plan.json", result.stdout)
        self.assertNotIn("PLAN_CONVERGED", result.stdout)

    def test_accept_with_only_p1_converges(self):
        self.critique([{"severity": "P1", "kind": "gap", "confidence": 100}])
        (self.ad / "plan-accept.txt").write_text("edy: accepted\n")
        r = self.go()
        self.assertIn("<promise>PLAN_CONVERGED</promise>", r.stdout, r.stdout + r.stderr)
        self.assertIn("human accepted at cap: edy:", r.stdout)

    def revision(self, applied=(), declined=()):
        (self.rd / "revision.json").write_text(json.dumps({"applied": list(applied), "declined": list(declined)}))

    P1 = {"severity": "P1", "kind": "verifiability", "confidence": 75, "section": "## Files"}

    def test_cap_routes_to_the_gate_when_every_blocking_finding_was_applied(self):
        # Chain 42b42a13 / run e35b7bd5: the last round's only P1 was applied,
        # plus P2s, and PLAN_ROUND_CAP threw the converging plan away.
        self.critique([self.P1, {"severity": "P2", "kind": "gap", "confidence": 75}])
        self.revision(applied=[dict(self.P1, what_changed="walk ancestors")])
        r = self.go()
        self.assertIn("<promise>PLAN_CONVERGED</promise>", r.stdout, r.stdout + r.stderr)
        self.assertIn("cap reached: 1 applied blocking findings not re-critiqued", r.stdout)
        marker = json.loads((self.ad / "plan-cap-unverified.json").read_text())
        self.assertEqual({"round": 1, "cap": 1}, {k: marker[k] for k in ("round", "cap")})
        self.assertEqual("walk ancestors", marker["unverified_applied"][0]["what_changed"])

    def test_cap_still_stops_on_a_declined_blocking_finding(self):
        self.critique([self.P1])
        self.revision(declined=[dict(self.P1, justification="no", citation={"source": "spec", "quote": "x"})])
        r = self.go()
        self.assertNotIn("<promise>PLAN_CONVERGED</promise>", r.stdout)
        self.assertIn("PLAN_ROUND_CAP round=1 cap=1", r.stdout)
        self.assertFalse((self.ad / "plan-cap-unverified.json").exists())

    def test_cap_still_stops_when_a_blocking_finding_went_unanswered(self):
        self.critique([self.P1, dict(self.P1, kind="gap", section="## Approach")])
        self.revision(applied=[self.P1])
        r = self.go()
        self.assertIn("PLAN_ROUND_CAP round=1 cap=1", r.stdout)
        self.assertFalse((self.ad / "plan-cap-unverified.json").exists())

    def test_a_duplicated_applied_row_cannot_stand_in_for_an_unanswered_finding(self):
        other = dict(self.P1, kind="gap", section="## Approach")
        self.critique([self.P1, other])
        self.revision(applied=[self.P1, self.P1])
        r = self.go()
        self.assertIn("PLAN_ROUND_CAP round=1 cap=1", r.stdout)
        self.assertFalse((self.ad / "plan-cap-unverified.json").exists())

    def test_an_unknown_severity_is_never_routed(self):
        self.critique([dict(self.P1, severity="p0")])
        self.revision(applied=[self.P1])
        self.assertIn("PLAN_ROUND_CAP round=1 cap=1", self.go().stdout)

    def test_a_stale_marker_from_an_earlier_route_is_removed(self):
        (self.ad / "plan-cap-unverified.json").write_text('{"round": 0}')
        self.critique([dict(self.P1, severity="P0")])
        self.revision(applied=[dict(self.P1, severity="P0")])
        self.go()
        self.assertFalse((self.ad / "plan-cap-unverified.json").exists())

    def test_cap_never_routes_a_p0_even_when_applied(self):
        p0 = dict(self.P1, severity="P0", confidence=100)
        self.critique([p0])
        self.revision(applied=[p0])
        r = self.go()
        self.assertIn("PLAN_ROUND_CAP round=1 cap=1", r.stdout)
        self.assertNotIn("<promise>PLAN_CONVERGED</promise>", r.stdout)

    def test_accept_never_waives_a_p0(self):
        self.critique([{"severity": "P0", "kind": "regression", "confidence": 90}])
        (self.ad / "plan-accept.txt").write_text("edy: force\n")
        r = self.go()
        self.assertNotIn("<promise>PLAN_CONVERGED</promise>", r.stdout)
        self.assertIn("P0=1 open", r.stdout + r.stderr)


class RenderGateCapFlag(unittest.TestCase):
    """plan-render-gate: a cap-routed plan reaches the human only with the flag."""

    def run_gate(self, marker_file, html_flag, guidance=None, guidance_attr=None, recorded=None):
        doc = yaml.safe_load((ARCHON / "workflows" / "full-sdlc-api.yaml").read_text(encoding="utf-8"))
        body = next(n for n in doc["nodes"] if n["id"] == "plan-render-gate")["bash"]
        body = body.replace('OPENER="$(command -v xdg-open 2>/dev/null || command -v open 2>/dev/null || true)"', 'OPENER=""')
        ad = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, ad, ignore_errors=True)
        (ad / "plan.md").write_text("plan\n")
        (ad / "plan.post-docreview.md").write_text("plan\n")
        sections = "".join(f"<!-- {m} -->" for m in ("GIST", "KB", "MAP", "PLAN", "REVIEW", "CRITIC", "DECIDE"))
        commands = " ".join(f"archon workflow {v}" for v in ("approve", "reject", "abandon"))
        flag = '<p data-cap-unverified>cap</p>' if html_flag else ""
        (ad / "plan-review.html").write_text(f"{sections}{flag}{ad.name} {commands}")
        if marker_file:
            (ad / "plan-cap-unverified.json").write_text('{"round": 3}')
        if recorded is not None:
            (ad / "feature-chain-request.json").write_text(json.dumps({"operator_guidance": {"sha256": recorded}}))
        if guidance is not None:
            (ad / "operator-guidance.md").write_text(guidance)
            if guidance_attr:
                with open(ad / "plan-review.html", "a") as html:
                    html.write(f'<section data-operator-guidance="{guidance_attr}">{guidance}</section>')
        return subprocess.run(["bash", "-c", body], capture_output=True, encoding="utf-8", env={**os.environ, "ARTIFACTS_DIR": str(ad)})

    def test_cap_routed_plan_without_the_flag_fails(self):
        r = self.run_gate(marker_file=True, html_flag=False)
        self.assertEqual(1, r.returncode)
        self.assertIn("RENDER_GATE=FAIL plan-cap-unverified.json present", r.stdout)

    GUIDANCE = "The week window must look ahead, not back.\n"
    GSHA = hashlib.sha256(GUIDANCE.encode()).hexdigest()

    def test_recorded_guidance_must_be_shown_with_its_recorded_hash(self):
        ok = self.run_gate(False, False, guidance=self.GUIDANCE, guidance_attr=self.GSHA, recorded=self.GSHA)
        self.assertEqual(0, ok.returncode, ok.stdout)
        hidden = self.run_gate(False, False, guidance=self.GUIDANCE, recorded=self.GSHA)
        self.assertIn(f"operator guidance {self.GSHA} is not shown in the PLAN section", hidden.stdout)
        self.assertEqual(1, hidden.returncode)

    def test_guidance_the_controller_did_not_record_is_refused(self):
        forged = self.run_gate(False, False, guidance=self.GUIDANCE, guidance_attr=self.GSHA)
        self.assertEqual(1, forged.returncode)
        self.assertIn("feature-chain-request.json records no operator_guidance", forged.stdout)

    def test_edited_or_deleted_recorded_guidance_is_refused(self):
        edited = "Look back instead.\n"
        changed = self.run_gate(False, False, guidance=edited,
                                guidance_attr=hashlib.sha256(edited.encode()).hexdigest(), recorded=self.GSHA)
        self.assertEqual(1, changed.returncode)
        self.assertIn(f"does not match the recorded {self.GSHA}", changed.stdout)
        deleted = self.run_gate(False, False, recorded=self.GSHA)
        self.assertEqual(1, deleted.returncode)
        self.assertIn("operator-guidance.md is missing", deleted.stdout)

    def test_flagged_or_uncapped_packets_pass(self):
        self.assertEqual(0, self.run_gate(marker_file=True, html_flag=True).returncode)
        self.assertEqual(0, self.run_gate(marker_file=False, html_flag=False).returncode)


if __name__ == "__main__":
    unittest.main()
