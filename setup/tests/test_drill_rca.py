"""bugfix.yaml RCA planning-critic loop (rca-round-pre / rca-converge, plus
thin wrapper coverage for rca-plan-shape and rca-critic-gate), ported from
scratchpad drills node-rca-round-pre.sh / node-rca-converge.sh /
node-rca-plan-shape.sh / node-rca-critic-gate.sh and patch_drill.py.

rca-shape.sh's own field-level validation (repo enum, failing-test shape,
probe/residuals/allowlist normalization, ...) is already covered by
test_rca_shape.py; this file does not re-test that. It covers what is new
here: the round-pre cap-first-with-junk-inputs guard (mirrored by
plan-round-pre in test_drill_plan.py), and rca-converge's ordered checks —
verdict validity, the dual-anchored immutability check (round-scoped AND the
pre-loop rca.pre.md/causal-chain.pre.json snapshot — the second anchor is
what stops a tampered artifact from being "laundered" into the new baseline
by a resume that re-snapshots it), the REJECT no-op stop, the CRITIC_GATE
self-consistency check, the ACCEPT+shape convergence, the scope-dispute stop,
and the no-progress / round-cap stops. Every node body is extracted live via
nodes.extract.runnable_body so these tests track the YAML, never a copy.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from nodes.extract import runnable_body
from test_drill_plan import assert_cap_file

MUTABLE = ["fix-plan.json", "files-allowlist.json", "verify.json", "failing-test.json"]
IMMUTABLE = ["rca.md", "causal-chain.json", "hypotheses.json", "residuals.json", "probe.json", "repo.json"]


def run_node(node_id, art, workflow="bugfix", outputs=None):
    body = runnable_body(workflow, node_id, outputs=outputs or {})
    env = dict(os.environ, ARTIFACTS_DIR=str(art))
    return subprocess.run(["bash", "-c", body], capture_output=True, text=True, env=env)


def write_json(art, name, obj):
    (art / name).write_text(json.dumps(obj), encoding="utf-8")


RCA_MD = """## Observation
test fails.

## Hypotheses
off by one.

## Evidence For
quote here.

## Evidence Against
none.

## Best Explanation
off by one at fix site.

## Critical Unknown
none.

## Discriminating Probe
none needed.
"""


def valid_files():
    """A complete, rca-shape.sh- and rca-gate-passing artifact set (verified
    directly against both scripts before being folded into this fixture)."""
    return {
        "repo.json": {"repo": "api"},
        "failing-test.json": {
            "repo": "api", "kind": "unit", "test_file": "src/foo.spec.ts",
            "test_name": "does the thing", "command": "bun run test -- foo",
            "predicted_failure_signature": "expected 1 to equal 2",
        },
        "fix-plan.json": {
            "approach": "fix the off-by-one", "fix_site": "src/foo.ts:42",
            "alternatives": [{"label": "widen the check", "why_not": "hides the bug"}],
            "files": ["src/foo.ts", "src/foo.spec.ts"],
        },
        "probe.json": {"probes": [], "none_reason": "root cause already evidenced in the chain"},
        "residuals.json": {"residuals": [
            {"symptom": "off-by-one on last page", "disposition": "fixed-by-this-chain", "citation": "src/foo.ts:42"}
        ]},
        "verify.json": {"test_patterns": ["src/foo.spec.ts"]},
        "files-allowlist.json": ["src/foo.ts", "src/foo.spec.ts"],
        "causal-chain.json": {"links": [
            {"claim": "signature reproduces",
             "evidence": {"quote": "expected 1 to equal 2", "file": "failing-test.json"}, "fixable": False},
            {"claim": "off by one at fix site",
             "evidence": {"quote": "fix the off-by-one", "file": "fix-plan.json"},
             "fixable": True, "fix_site": "src/foo.ts:42"},
        ]},
        "hypotheses.json": [{"id": "h1", "statement": "off by one", "status": "confirmed-by-experiment"}],
    }


def seed_valid_artifacts(art):
    for name, obj in valid_files().items():
        write_json(art, name, obj)
    (art / "rca.md").write_text(RCA_MD, encoding="utf-8")


def placeholder_artifacts(art):
    """Existence-only fixture: rca-round-pre only `test -f`s these, plus the
    durable imm-<f> anchor rca-gate is supposed to have already written
    (content validity is rca-gate/rca-shape.sh's job, covered elsewhere)."""
    for name in IMMUTABLE:
        (art / name).write_text("{}", encoding="utf-8")
        (art / f"imm-{name}").write_text("{}", encoding="utf-8")
    for name in MUTABLE:
        (art / name).write_text("{}", encoding="utf-8")


class RcaRoundPreCapFirstTest(unittest.TestCase):
    """Mirrors plan-round-pre's cap-first-with-junk-inputs coverage
    (test_drill_plan.py) against the bugfix-lane sibling."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        placeholder_artifacts(self.tmp)

    def test_junk_round_counter_fails_closed_typed(self):
        (self.tmp / "rca-round.txt").write_text("banana\n")
        r = run_node("rca-round-pre", self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("RCA_ROUND_PRE=FAIL rca-round.txt is not an integer: [banana]", r.stdout)

    def test_junk_cap_falls_back_to_default_and_still_enforces(self):
        (self.tmp / "rca-round.txt").write_text("3\n")
        (self.tmp / "rca-round-cap.txt").write_text("not-a-number\n")
        r = run_node("rca-round-pre", self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("RCA_PLAN_ROUND_CAP round=3 cap=3", r.stdout)
        assert_cap_file(self, self.tmp, "rca-plan-loop-exit.txt", "RCA_PLAN_ROUND_CAP round=3 cap=3")

    def test_cap_checked_before_round_is_spent(self):
        (self.tmp / "fix-plan.json").unlink()  # would fail differently if reached
        (self.tmp / "rca-round.txt").write_text("3\n")
        r = run_node("rca-round-pre", self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("RCA_PLAN_ROUND_CAP round=3 cap=3", r.stdout)
        assert_cap_file(self, self.tmp, "rca-plan-loop-exit.txt", "RCA_PLAN_ROUND_CAP round=3 cap=3")
        self.assertFalse((self.tmp / "rca-round-4").exists())

    def test_missing_fix_plan_typed(self):
        (self.tmp / "fix-plan.json").unlink()
        r = run_node("rca-round-pre", self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("RCA_ROUND_PRE=FAIL no fix-plan.json round=1", r.stdout)

    def test_missing_diagnosis_artifact_typed(self):
        # imm-causal-chain.json (the durable anchor) is left in place; only the
        # live diagnosis file is gone, so the loop must report THAT first.
        (self.tmp / "causal-chain.json").unlink()
        r = run_node("rca-round-pre", self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("RCA_ROUND_PRE=FAIL missing diagnosis artifact causal-chain.json round=1", r.stdout)

    def test_missing_durable_anchor_typed(self):
        # The live file is present but rca-gate's one-time anchor never was
        # (or was hand-deleted): round-pre must stop here rather than let a
        # round reach rca-converge with nothing durable to compare against.
        (self.tmp / "imm-causal-chain.json").unlink()
        r = run_node("rca-round-pre", self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("RCA_ROUND_PRE=FAIL missing durable anchor imm-causal-chain.json round=1", r.stdout)

    def test_below_cap_progresses_and_snapshots_both_sets(self):
        (self.tmp / "fix-plan.json").write_text('{"x": 1}', encoding="utf-8")
        r = run_node("rca-round-pre", self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("RCA_PLAN_ROUND=1 cap=3", r.stdout)
        rd = self.tmp / "rca-round-1"
        for f in MUTABLE:
            self.assertTrue((rd / f"pre-{f}").is_file(), f)
        for f in IMMUTABLE:
            self.assertTrue((rd / f"imm-{f}").is_file(), f)


class RcaConvergeTest(unittest.TestCase):
    """Ordered checks in rca-converge, run against a real rca-gate ->
    rca-round-pre seeded state so the round-scoped and pre-loop snapshots
    are the genuine artifacts those two nodes produce."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        seed_valid_artifacts(self.tmp)
        gate = run_node("rca-gate", self.tmp)
        assert gate.returncode == 0, gate.stdout + gate.stderr
        pre = run_node("rca-round-pre", self.tmp)
        assert pre.returncode == 0, pre.stdout + pre.stderr  # round=1

    def _write_round(self, n, critique, revision):
        rd = self.tmp / f"rca-round-{n}"
        write_json(rd, "critique.json", critique)
        write_json(rd, "revision.json", revision)

    def test_unknown_verdict_typed(self):
        self._write_round(1, {"verdict": "MAYBE"}, {"applied": [], "declined": []})
        r = run_node("rca-converge", self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("RCA_CONVERGE=FAIL unknown verdict [MAYBE] round=1", r.stdout)

    def test_no_revision_json_typed(self):
        rd = self.tmp / "rca-round-1"
        write_json(rd, "critique.json", {"verdict": "REVISE"})
        r = run_node("rca-converge", self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("RCA_CONVERGE=FAIL no revision.json round=1", r.stdout)

    def test_immutable_tamper_round_copy_catches_it(self):
        (self.tmp / "causal-chain.json").write_text('{"links": []}', encoding="utf-8")
        self._write_round(1, {"verdict": "REVISE"}, {"applied": [], "declined": []})
        r = run_node("rca-converge", self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("RCA_PLAN=FAIL immutable artifact modified causal-chain.json round=1", r.stdout)

    def test_immutable_tamper_laundering_on_resume_still_caught(self):
        # rca-round-pre ALSO takes a round-scoped copy into $RD/imm-<f> every
        # round -- but that copy is diagnostics only. If rca-converge's cmp
        # used it, a tamper followed by a failed round and a resume would let
        # the next round-pre re-snapshot the ALREADY-tampered file as its own
        # baseline, silently laundering the mutation. rca-converge instead
        # cmps against $AD/imm-<f>, written exactly once by rca-gate before
        # the loop ever ran, so no later round-pre call can move it. Prove
        # both halves: round 2's own round-scoped copy DOES match the
        # tampered content (so that copy genuinely could launder it), yet
        # rca-converge still catches the drift via the durable anchor.
        (self.tmp / "causal-chain.json").write_text('{"links": []}', encoding="utf-8")
        pre2 = run_node("rca-round-pre", self.tmp)
        self.assertEqual(pre2.returncode, 0, pre2.stdout + pre2.stderr)  # round=2
        self.assertIn("RCA_PLAN_ROUND=2", pre2.stdout)
        rd2 = self.tmp / "rca-round-2"
        self.assertEqual(
            (rd2 / "imm-causal-chain.json").read_text(),
            (self.tmp / "causal-chain.json").read_text(),
            "test invariant: the round-scoped copy must match the tampered file",
        )
        # The durable, once-written anchor must NOT match the tamper.
        self.assertNotEqual(
            (self.tmp / "imm-causal-chain.json").read_text(),
            (self.tmp / "causal-chain.json").read_text(),
        )
        self._write_round(2, {"verdict": "REVISE"}, {"applied": [], "declined": []})
        r = run_node("rca-converge", self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("RCA_PLAN=FAIL immutable artifact modified causal-chain.json round=2", r.stdout)

    def test_reject_is_typed_stop_when_noop(self):
        self._write_round(1, {"verdict": "REJECT"}, {"applied": [], "declined": []})
        r = run_node("rca-converge", self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("RCA_PLAN_REJECTED round=1 mutated=NO", r.stdout)

    def test_reject_mutation_is_detected_and_named(self):
        d = json.loads((self.tmp / "fix-plan.json").read_text())
        d["approach"] = "reviser touched this despite REJECT"
        write_json(self.tmp, "fix-plan.json", d)
        self._write_round(1, {"verdict": "REJECT"}, {"applied": [], "declined": []})
        r = run_node("rca-converge", self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("RCA_PLAN_REJECTED round=1 mutated=fix-plan.json", r.stdout)

    def test_critic_gate_inconsistent_accept_with_blocking_findings(self):
        self._write_round(1, {
            "verdict": "ACCEPT",
            "findings": [{"severity": "P0", "confidence": 100}],
        }, {"applied": [], "declined": []})
        r = run_node("rca-converge", self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(
            "CRITIC_GATE=FAIL verdict inconsistent round=1 declared ACCEPT with 1 blocking findings", r.stdout
        )

    def test_accept_with_valid_shape_converges(self):
        self._write_round(1, {"verdict": "ACCEPT", "findings": []}, {"applied": [], "declined": []})
        r = run_node("rca-converge", self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("RCA_PLAN_CONVERGED round=1", r.stdout)
        self.assertIn("<promise>RCA_PLAN_CONVERGED</promise>", r.stdout)

    def test_accept_with_broken_shape_forwards_typed_failure(self):
        # repo.json is IMMUTABLE, so mutating it would trip the immutable
        # check first (covered above) rather than reach the shape check.
        # failing-test.json is MUTABLE and legitimately revisable, so this
        # breaks shape (an overly generic signature) without tripping that
        # earlier guard, isolating the ACCEPT+shape branch.
        d = json.loads((self.tmp / "failing-test.json").read_text())
        d["predicted_failure_signature"] = "Error"
        write_json(self.tmp, "failing-test.json", d)
        self._write_round(1, {"verdict": "ACCEPT", "findings": []}, {"applied": [], "declined": []})
        r = run_node("rca-converge", self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("RCA_PLAN_SHAPE=FAIL signature too generic", r.stdout)
        self.assertIn("RCA_CONVERGE=FAIL shape round=1", r.stdout)

    def test_scope_dispute_declined_at_confidence_100(self):
        self._write_round(1, {"verdict": "REVISE", "findings": []}, {
            "applied": [],
            "declined": [{"kind": "scope", "confidence": 100}],
        })
        r = run_node("rca-converge", self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("RCA_PLAN_SCOPE_DISPUTE round=1 declined_scope_100=1", r.stdout)

    def test_scope_dispute_not_triggered_below_confidence_100(self):
        d = json.loads((self.tmp / "fix-plan.json").read_text())
        d["approach"] = "actually revised this round"
        write_json(self.tmp, "fix-plan.json", d)
        self._write_round(1, {"verdict": "REVISE", "findings": []}, {
            "applied": [],
            "declined": [{"kind": "scope", "confidence": 75}],
        })
        r = run_node("rca-converge", self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("RCA_PLAN_ROUND_PROGRESSED round=1", r.stdout)

    def test_revision_json_malformed_missing_lists(self):
        rd = self.tmp / "rca-round-1"
        write_json(rd, "critique.json", {"verdict": "REVISE", "findings": []})
        write_json(rd, "revision.json", {"applied": []})  # no "declined"
        r = run_node("rca-converge", self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("RCA_CONVERGE=FAIL revision.json malformed round=1", r.stdout)

    def test_no_progress_when_fix_plan_untouched(self):
        self._write_round(1, {"verdict": "REVISE", "findings": []}, {"applied": [], "declined": []})
        r = run_node("rca-converge", self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("RCA_PLAN_NO_PROGRESS round=1 other_mutated=none", r.stdout)

    def test_no_progress_reports_other_mutated_files(self):
        d = json.loads((self.tmp / "verify.json").read_text())
        d["test_patterns"].append("src/extra.spec.ts")
        write_json(self.tmp, "verify.json", d)
        self._write_round(1, {"verdict": "REVISE", "findings": []}, {"applied": [], "declined": []})
        r = run_node("rca-converge", self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("RCA_PLAN_NO_PROGRESS round=1 other_mutated=verify.json", r.stdout)

    def test_round_progressed_below_cap(self):
        d = json.loads((self.tmp / "fix-plan.json").read_text())
        d["approach"] = "actually revised this round"
        write_json(self.tmp, "fix-plan.json", d)
        self._write_round(1, {"verdict": "REVISE", "findings": []}, {"applied": [], "declined": []})
        r = run_node("rca-converge", self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("RCA_PLAN_ROUND_PROGRESSED round=1", r.stdout)

    def test_post_round_cap_stops_a_progressing_round(self):
        # Drive round-pre to round 3 (the default cap) with genuine progress
        # each time, then show the THIRD progressing round still stops here
        # even though round-pre already let it through pre-round.
        for n in (1, 2):
            d = json.loads((self.tmp / "fix-plan.json").read_text())
            d["approach"] = f"revision {n}"
            write_json(self.tmp, "fix-plan.json", d)
            self._write_round(n, {"verdict": "REVISE", "findings": []}, {"applied": [], "declined": []})
            r = run_node("rca-converge", self.tmp)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            pre = run_node("rca-round-pre", self.tmp)
            self.assertEqual(pre.returncode, 0, pre.stdout + pre.stderr)
        d = json.loads((self.tmp / "fix-plan.json").read_text())
        d["approach"] = "revision 3"
        write_json(self.tmp, "fix-plan.json", d)
        self._write_round(3, {"verdict": "REVISE", "findings": []}, {"applied": [], "declined": []})
        r = run_node("rca-converge", self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("RCA_PLAN_ROUND_CAP round=3 cap=3", r.stdout)
        assert_cap_file(self, self.tmp / "rca-round-3", "converge.txt", "RCA_PLAN_ROUND_CAP round=3 cap=3")


class RcaPlanShapeWrapperTest(unittest.TestCase):
    """rca-plan-shape re-runs rca-shape.sh post-loop and renames its token;
    the field-level checks themselves are test_rca_shape.py's job."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        seed_valid_artifacts(self.tmp)

    def test_valid_artifacts_pass_renamed_token(self):
        r = run_node("rca-plan-shape", self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("RCA_PLAN_SHAPE=OK repo=api kind=unit", r.stdout)

    def test_broken_artifacts_fail_renamed_token(self):
        (self.tmp / "repo.json").unlink()
        r = run_node("rca-plan-shape", self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("RCA_PLAN_SHAPE=FAIL", r.stdout)
        self.assertNotIn("RCA_SHAPE=FAIL", r.stdout)  # token must be renamed, not just prefixed


class RcaCriticGateWrapperTest(unittest.TestCase):
    """rca-critic-gate delegates schema validation entirely to
    parse-critique.py (see test_parse_critique.py); this checks only the
    round-file plumbing this node adds around that call."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_no_rca_round_txt_typed(self):
        r = run_node("rca-critic-gate", self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("CRITIC_GATE=FAIL no rca-round.txt", r.stdout)

    def test_no_critique_json_for_round_typed(self):
        (self.tmp / "rca-round.txt").write_text("1\n")
        r = run_node("rca-critic-gate", self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("CRITIC_GATE=FAIL no critique.json round=1", r.stdout)

    def test_valid_critique_delegates_to_parser(self):
        (self.tmp / "rca-round.txt").write_text("1\n")
        rd = self.tmp / "rca-round-1"
        rd.mkdir()
        write_json(rd, "critique.json", {
            "verdict": "ACCEPT",
            "findings": [],
        })
        r = run_node("rca-critic-gate", self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("CRITIQUE round=1 verdict=ACCEPT", r.stdout)


if __name__ == "__main__":
    unittest.main()
