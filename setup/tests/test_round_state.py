#!/usr/bin/env python3
"""round-state.py: one activity, one completion record, across every kill point.

The property under test is not "the helper works" but "a resume never repeats a
completed activity and never skips an interrupted one". The injection sequence
drives the eight durable boundaries of a round, killing between subcommands the
way a resume does, and counts DUPLICATES: a `review: run` for a round whose
complete envelope already matched the identity, or a `fixer: run` when
repair.json or fixer.ok already matched. First invocations and genuinely
interrupted ones are not duplicates and must still be permitted.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ARCHON = Path(__file__).resolve().parent.parent.parent
ROUND_STATE = ARCHON / "setup" / "round-state.py"
LEDGER_STUB = ARCHON / "setup" / "tests" / "fixtures" / "ledger-stub.py"
SEED = "export function widen(a: number) {\n  return a + 1;\n}\n"


class Lane:
    """A throwaway artifacts directory and candidate worktree wired to the helper."""

    def __init__(self, root):
        self.root = Path(root)
        self.ad = self.root / "artifacts"
        self.wt = self.root / "wt"
        (self.wt / "src").mkdir(parents=True)
        self.ad.mkdir(parents=True)
        self.git("init", "-q", "-b", "main", ".")
        self.git("config", "user.email", "round-state@test")
        self.git("config", "user.name", "round-state")
        (self.wt / "src" / "a.ts").write_text(SEED, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-qm", "seed")
        self.write_json("params.json", {"spec": "s", "slug": "s", "branch": "main",
                                        "worktree": str(self.wt), "repo": "api"})
        self.write_json("files-allowlist.json", ["src/a.ts"])
        self.write_json("joint-plan.json", {"schema": "archon.joint-feature-plan.v1",
                                            "repositories": ["api"]})
        (self.ad / "bootstrap-head.txt").write_text(self.head() + "\n", encoding="utf-8")

    # --- plumbing ---------------------------------------------------------
    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.wt), *args],
                              capture_output=True, encoding="utf-8", check=True).stdout.strip()

    def head(self):
        return self.git("rev-parse", "HEAD")

    def write_json(self, name, value):
        (self.ad / name).write_text(json.dumps(value), encoding="utf-8")

    def run(self, *args):
        env = dict(os.environ, ARCHON_LEDGER_PY=str(LEDGER_STUB))
        return subprocess.run([sys.executable, str(ROUND_STATE), *[str(a) for a in args]],
                              capture_output=True, encoding="utf-8", env=env)

    # --- the round's files ------------------------------------------------
    @property
    def n(self):
        raw = (self.ad / "round.txt").read_text(encoding="utf-8").strip()
        return int(raw) if raw.isdigit() else 0

    @property
    def rd(self):
        return self.ad / f"round-{self.n}"

    def review_input(self):
        return json.loads((self.rd / "review-input.json").read_text(encoding="utf-8"))

    # --- the node steps, as the lane runs them ----------------------------
    def pre(self):
        proc = self.run("pre", self.ad)
        if proc.returncode != 0:
            return {"exit": proc.returncode, "stderr": proc.stderr, "stdout": proc.stdout}
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        payload["stderr"] = proc.stderr
        payload["exit"] = 0
        return payload

    def review(self, verdict="Ready to merge", head=None, input_id=None, complete=True):
        """The reviewer session: read the input record, write the envelope."""
        record = self.review_input()
        body = [
            "## Findings", "", "Scope: full", "",
            "Review complete" if complete else "Review still running",
            f"Input: {input_id or record['id']}",
            f"Head: {head or record['review_head']}",
            f"Verdict: {verdict}",
        ]
        envelope = self.root / "envelope.txt"
        envelope.write_text("\n".join(body) + "\n", encoding="utf-8")
        self.run("mark", self.ad, "review-done", envelope)
        self.write_summary(verdict)
        return envelope

    def write_summary(self, verdict):
        (self.rd / "review-summary.json").write_text(
            json.dumps({"verdict": verdict, "residual_count": -1, "degraded": False}),
            encoding="utf-8")

    def gate(self):
        return self.run("gate", self.ad)

    def fix_plan(self):
        proc = self.run("fix-plan", self.ad)
        if proc.returncode != 0:
            return {"fixer": "error", "stderr": proc.stderr, "exit": proc.returncode}
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        payload["exit"] = 0
        return payload

    def fixer(self, result=None, edit=None):
        """The fixer session: own the tree, edit, write the result, complete."""
        self.run("mark", self.ad, "repair-start")
        if edit is not None:
            (self.wt / "src" / "a.ts").write_text(edit, encoding="utf-8")
        payload = result if result is not None else {
            "applied": [], "failed": [], "advisory": [], "incomplete": []}
        (self.rd / "fixer-result.json").write_text(json.dumps(payload), encoding="utf-8")
        return self.run("mark", self.ad, "repair-done")

    def stage(self):
        self.git("add", "-A")

    def commit_fixer(self):
        self.stage()
        return self.run("commit-fixer", self.ad)

    def converge(self, closure=None):
        if closure is not None:
            self.write_json("stub-closure.json", closure)
        return self.run("converge", self.ad)

    def ledger_calls(self):
        path = self.ad / "ledger-calls.txt"
        return path.read_text(encoding="utf-8").splitlines() if path.is_file() else []


class LaneCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lane = Lane(self.tmp.name)

    def full_round(self, verdict="Ready to merge", result=None, edit=None, closure=None):
        """A whole round, start to decision, with no kills."""
        lane = self.lane
        pre = lane.pre()
        lane.review(verdict)
        self.assertEqual(lane.gate().returncode, 0)
        plan = lane.fix_plan()
        if plan["fixer"] == "run":
            lane.fixer(result=result, edit=edit)
        commit = lane.commit_fixer()
        converge = lane.converge(closure)
        return pre, plan, commit, converge


class Identity(LaneCase):
    def test_a_changed_plan_digest_invalidates_a_completed_review(self):
        lane = self.lane
        self.assertEqual(lane.pre()["reason"], "initial")
        lane.review()
        self.assertEqual(lane.gate().returncode, 0)
        # The same candidate, reviewed and gated: a second pre reuses it.
        self.assertEqual(lane.pre()["review"], "reuse")
        lane.write_json("joint-plan.json", {"schema": "archon.joint-feature-plan.v1",
                                            "repositories": ["api", "web-app"]})
        again = lane.pre()
        self.assertEqual(again["review"], "run")
        self.assertEqual(again["reason"], "identity")

    def test_the_allowlist_is_part_of_the_identity(self):
        lane = self.lane
        lane.pre()
        lane.review()
        lane.gate()
        lane.write_json("files-allowlist.json", ["src/a.ts", "src/b.ts"])
        self.assertEqual(lane.pre()["reason"], "identity")

    def test_a_fixer_commit_does_not_invalidate_the_review(self):
        lane = self.lane
        lane.pre()
        lane.review("Ready with fixes")
        lane.gate()
        lane.fix_plan()
        lane.fixer(result={"applied": [{"finding": "f", "action": "a", "severity": "P2"}],
                           "failed": [], "advisory": [], "incomplete": []},
                   edit=SEED + "// repaired\n")
        self.assertEqual(lane.commit_fixer().returncode, 0)
        after = lane.pre()
        self.assertEqual(after["review"], "reuse", after.get("stderr"))


class CommitRecovery(LaneCase):
    """The three traces the plan writes out for a lost marker."""

    def prepare_repair(self):
        lane = self.lane
        lane.pre()
        lane.review("Ready with fixes")
        lane.gate()
        lane.fix_plan()
        lane.fixer(result={"applied": [{"finding": "f", "action": "a", "severity": "P2"}],
                           "failed": [], "advisory": [], "incomplete": []},
                   edit=SEED + "// repaired\n")
        return lane

    def test_kill_after_the_commit_before_the_attestation_is_reconciled(self):
        lane = self.prepare_repair()
        # commit-fixer up to and including the commit, then killed.
        lane.stage()
        tree = json.loads(lane.run("fix-plan", lane.ad).stdout or "{}")
        self.assertEqual(tree.get("fixer"), "reuse-uncommitted")
        (lane.rd / "post-fix.json").write_text(
            json.dumps({"tree": json.loads((lane.rd / "repair.json").read_text())["tree"]}),
            encoding="utf-8")
        lane.git("commit", "-qm", "fix(review): apply fixer feedback")
        self.assertFalse((lane.rd / "fixer.ok").is_file())
        result = lane.pre()
        self.assertIn("FIXER_RECONCILED", result["stderr"])
        self.assertTrue((lane.rd / "fixer.ok").is_file())
        self.assertEqual(result["review"], "reuse")
        self.assertEqual(lane.fix_plan()["fixer"], "reuse-committed")

    def test_kill_between_the_ledger_merge_and_the_commit_reuses_the_repair(self):
        lane = self.prepare_repair()
        lane.stage()
        # The ledger merge ran; nothing was committed.
        lane.run("fix-plan", lane.ad)
        self.assertEqual(lane.fix_plan()["fixer"], "reuse-uncommitted")
        self.assertEqual(lane.commit_fixer().returncode, 0)
        self.assertEqual(lane.fix_plan()["fixer"], "reuse-committed")

    def test_a_no_change_result_attests_without_a_commit(self):
        lane = self.lane
        lane.pre()
        lane.review("Ready to merge")
        lane.gate()
        lane.fix_plan()
        lane.fixer()  # all-empty result, no edit
        head_before = lane.head()
        proc = lane.commit_fixer()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("COMMITTED=NO", proc.stdout)
        self.assertEqual(lane.head(), head_before)
        attestation = json.loads((lane.rd / "fixer.ok").read_text(encoding="utf-8"))
        self.assertFalse(attestation["committed"])
        self.assertEqual(attestation["head"], head_before)


class Authorization(LaneCase):
    def test_an_old_repair_under_a_new_failed_gate_is_never_reused(self):
        """Generation 1 repaired; the review is rejected and re-run; gate 2 fails."""
        lane = self.lane
        lane.pre()
        lane.review("Ready with fixes")
        lane.gate()
        lane.fix_plan()
        lane.fixer(result={"applied": [{"finding": "f", "action": "a", "severity": "P2"}],
                           "failed": [], "advisory": [], "incomplete": []},
                   edit=SEED + "// gen1\n")
        lane.commit_fixer()
        self.assertEqual(lane.fix_plan()["fixer"], "reuse-committed")
        # A human rejects the envelope; the next attempt is generation 2.
        self.assertEqual(lane.run("reject-review", lane.ad, "--reason", "wrong scope").returncode, 0)
        rerun = lane.pre()
        self.assertEqual(rerun["reason"], "rejected")
        lane.review("Ready with fixes")
        # The new gate fails: review.ok is deleted, so nothing downstream is authorized.
        self.assertEqual(lane.run("gate", lane.ad, "--fail", "degraded").returncode, 1)
        self.assertFalse((lane.rd / "review.ok").is_file())
        self.assertEqual(lane.fix_plan()["fixer"], "unauthorized")
        commit = lane.commit_fixer()
        self.assertEqual(commit.returncode, 1)
        self.assertIn("REVIEW_UNAUTHORIZED", commit.stdout)
        converge = lane.converge()
        self.assertEqual(converge.returncode, 1)
        self.assertIn("REVIEW_UNAUTHORIZED", converge.stdout)

    def test_a_repair_from_an_earlier_generation_is_not_reused_after_a_rejection(self):
        lane = self.lane
        lane.pre()
        lane.review("Ready with fixes")
        lane.gate()
        lane.fixer(result={"applied": [], "failed": [], "advisory": [], "incomplete": []})
        gen_one = json.loads((lane.rd / "repair.json").read_text(encoding="utf-8"))
        lane.run("reject-review", lane.ad, "--reason", "restating waived findings")
        lane.pre()
        lane.review("Ready with fixes")
        self.assertEqual(lane.gate().returncode, 0)
        gen_two = json.loads((lane.rd / "review.ok").read_text(encoding="utf-8"))["gen"]
        self.assertNotEqual(gen_one["review_gen"], gen_two)
        self.assertEqual(lane.fix_plan(), {"fixer": "run", "reason": "initial", "exit": 0})


class InputGate(LaneCase):
    """GATE_5: the envelope must name the review it was issued for.

    v1's GATE_4 compared a `Scope:` string, which stopped discriminating once
    789b2a1 labelled the bootstrap-head comparison `delta`. The id binds the
    head, base, scope, contract, plan and allowlist together, so an envelope
    that echoes it is provably about this candidate under this contract.
    """

    def test_an_envelope_that_echoes_the_wrong_id_fails_the_gate(self):
        lane = self.lane
        lane.pre()
        lane.review(input_id="0" * 64)
        proc = lane.gate()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("GATE_5_input_matches=FAIL", proc.stdout)
        self.assertIn("REVIEW_INPUT=FAIL round=1", proc.stdout)
        self.assertFalse((lane.rd / "review.ok").is_file())

    def test_an_envelope_that_echoes_the_wrong_head_fails_the_gate(self):
        lane = self.lane
        lane.pre()
        lane.review(head="0" * 40)
        proc = lane.gate()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("GATE_5_input_matches=FAIL", proc.stdout)

    def test_a_matching_envelope_passes_and_writes_the_authorization(self):
        lane = self.lane
        lane.pre()
        lane.review()
        proc = lane.gate()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("GATE_5_input_matches=PASS", proc.stdout)
        authorization = json.loads((lane.rd / "review.ok").read_text(encoding="utf-8"))
        self.assertEqual(authorization["gen"], lane.review_input()["attempt"])
        self.assertEqual(authorization["guard"], "PASS")


class ReadOnlyGuard(LaneCase):
    def test_a_reviewer_that_edited_the_tree_fails_the_gate(self):
        lane = self.lane
        lane.pre()
        (lane.wt / "src" / "a.ts").write_text(SEED + "// reviewer wrote this\n", encoding="utf-8")
        lane.review()
        proc = lane.gate()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("REVIEW_WROTE_TREE", proc.stdout)
        self.assertFalse((lane.rd / "review.ok").is_file())

    def test_the_fixer_owns_the_tree_from_repair_start(self):
        lane = self.lane
        lane.pre()
        lane.review("Ready with fixes")
        lane.gate()
        lane.run("mark", lane.ad, "repair-start")
        (lane.wt / "src" / "a.ts").write_text(SEED + "// fixer edit\n", encoding="utf-8")
        # Re-gating the same envelope after the fixer took ownership still passes.
        proc = lane.gate()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("owner=fixer", proc.stdout)

    def test_an_interrupted_fixer_resumes_instead_of_tripping_the_guard(self):
        lane = self.lane
        lane.pre()
        lane.review("Ready with fixes")
        lane.gate()
        lane.run("mark", lane.ad, "repair-start")
        (lane.wt / "src" / "a.ts").write_text(SEED + "// half a repair\n", encoding="utf-8")
        # Killed before repair.json.
        resumed = lane.pre()
        self.assertEqual(resumed["review"], "reuse", resumed.get("stderr"))
        self.assertEqual(lane.gate().returncode, 0)
        self.assertEqual(lane.fix_plan(), {"fixer": "run", "reason": "interrupted", "exit": 0})

    def test_a_reviewer_edit_without_repair_start_stops_at_round_pre(self):
        lane = self.lane
        lane.pre()
        lane.review(complete=False)
        (lane.wt / "src" / "a.ts").write_text(SEED + "// reviewer wrote this\n", encoding="utf-8")
        result = lane.pre()
        self.assertEqual(result["exit"], 1)
        self.assertIn("REVIEW_WROTE_TREE", result["stderr"])


class FixPlan(LaneCase):
    def test_a_result_that_no_longer_matches_the_tree_is_a_typed_stop(self):
        lane = self.lane
        lane.pre()
        lane.review("Ready with fixes")
        lane.gate()
        lane.fixer(result={"applied": [], "failed": [], "advisory": [], "incomplete": []},
                   edit=SEED + "// repaired\n")
        # An unaccounted edit after the repair completed.
        (lane.wt / "src" / "a.ts").write_text(SEED + "// and something else\n", encoding="utf-8")
        result = lane.fix_plan()
        self.assertEqual(result["fixer"], "error")
        self.assertIn("FIXER_TREE_DRIFT", result["stderr"])

    def test_commit_fixer_without_a_repair_record_is_incomplete(self):
        lane = self.lane
        lane.pre()
        lane.review("Ready with fixes")
        lane.gate()
        proc = lane.commit_fixer()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("FIXER_INCOMPLETE", proc.stdout)


class Converge(LaneCase):
    def test_a_clean_ready_round_converges_and_promises(self):
        _, _, _, converge = self.full_round(closure={"closure_ok": True, "closed": 2})
        self.assertEqual(converge.returncode, 0, converge.stdout + converge.stderr)
        self.assertIn("CLOSURE round=1 closed=2", converge.stdout)
        self.assertIn("CONVERGED round=1", converge.stdout)
        self.assertIn("<promise>REVIEW_CONVERGED</promise>", converge.stdout)
        self.assertTrue((self.lane.ad / "residuals.json").is_file())

    def test_converge_without_a_fixer_attestation_stops(self):
        lane = self.lane
        lane.pre()
        lane.review()
        lane.gate()
        proc = lane.converge()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("FIXER_ABSENT", proc.stdout)

    def test_an_unclosed_blocker_progresses_to_a_verify_round(self):
        _, _, _, converge = self.full_round(closure={"closure_ok": False, "open": 1})
        self.assertEqual(converge.returncode, 0, converge.stdout)
        self.assertIn("unverified P0/P1", converge.stdout)
        decision = json.loads((self.lane.rd / "decision.json").read_text(encoding="utf-8"))
        self.assertEqual(decision["result"], "progressed")
        self.assertEqual(decision["next_mode"], "verify")

    def test_a_ready_envelope_whose_head_is_stale_is_tree_drift(self):
        """Row 9: nothing was applied, and the tree moved anyway.

        The reviewer said Ready about a sha that is no longer the candidate, so
        the verdict describes code nobody is shipping.
        """
        lane = self.lane
        lane.pre()
        lane.review("Ready to merge")
        lane.gate()
        lane.fix_plan()
        # An all-empty result over an edited tree: applied == 0, HEAD still moves.
        lane.fixer(edit=SEED + "// landed without an applied entry\n")
        commit = lane.commit_fixer()
        self.assertIn("COMMITTED=YES", commit.stdout)
        converge = lane.converge({"closure_ok": True})
        self.assertEqual(converge.returncode, 1, converge.stdout)
        self.assertIn("REVIEW_TREE_DRIFT round=1", converge.stdout)

    def test_not_ready_with_no_open_blocker_is_a_contract_violation(self):
        _, _, _, converge = self.full_round(verdict="Not ready",
                                            closure={"blockers_open": 0})
        self.assertEqual(converge.returncode, 1)
        self.assertIn("NOT_READY_WITHOUT_BLOCKER round=1", converge.stdout)

    def test_a_pin_conflict_blocks_before_every_other_row(self):
        _, _, _, converge = self.full_round(
            result={"applied": [], "failed": [], "advisory": [],
                    "incomplete": [{"finding": "f", "action": "a", "severity": "P2"}]},
            closure={"pin_conflict": ["GroupService.shareGroup"]})
        self.assertEqual(converge.returncode, 1)
        self.assertIn("PIN_CONFLICT round=1 symbol=GroupService.shareGroup", converge.stdout)

    def test_a_filed_cross_repo_finding_blocks(self):
        _, _, _, converge = self.full_round(
            closure={"filed": [{"finding": "f", "action": "a", "producer_repo": "web-app"}]})
        self.assertEqual(converge.returncode, 1)
        self.assertIn("CROSS_REPO_FINDING round=1 count=1 repos=web-app", converge.stdout)
        self.assertTrue((self.lane.ad / "cross-repo-findings.json").is_file())

    def test_an_applied_p0_forces_a_full_next_round(self):
        _, _, _, converge = self.full_round(
            verdict="Ready with fixes",
            result={"applied": [{"finding": "f", "action": "a", "severity": "P0"}],
                    "failed": [], "advisory": [], "incomplete": []},
            edit=SEED + "// p0 repair\n")
        self.assertEqual(converge.returncode, 0, converge.stdout)
        decision = json.loads((self.lane.rd / "decision.json").read_text(encoding="utf-8"))
        self.assertEqual(decision["next_mode"], "full")

    def test_a_design_expanding_repair_forces_a_full_next_round(self):
        _, _, _, converge = self.full_round(
            verdict="Ready with fixes",
            result={"applied": [{"finding": "f", "action": "a", "severity": "P1",
                                 "design_expanded": True}],
                    "failed": [], "advisory": [], "incomplete": []},
            edit=SEED + "// widened\n")
        decision = json.loads((self.lane.rd / "decision.json").read_text(encoding="utf-8"))
        self.assertEqual(decision["next_mode"], "full")

    def test_a_p2_repair_asks_for_a_verify_round(self):
        self.full_round(verdict="Ready with fixes",
                        result={"applied": [{"finding": "f", "action": "a", "severity": "P2"}],
                                "failed": [], "advisory": [], "incomplete": []},
                        edit=SEED + "// tidy\n")
        decision = json.loads((self.lane.rd / "decision.json").read_text(encoding="utf-8"))
        self.assertEqual(decision["next_mode"], "verify")

    def test_a_verify_round_that_finds_a_new_blocker_goes_full(self):
        lane = self.lane
        lane.pre()
        (lane.rd / "review-scope.txt").write_text("verify\n", encoding="utf-8")
        lane.review()
        lane.gate()
        lane.fix_plan()
        lane.fixer()
        lane.commit_fixer()
        converge = lane.converge({"new_blockers": 1, "closure_ok": True})
        self.assertEqual(converge.returncode, 0, converge.stdout)
        decision = json.loads((lane.rd / "decision.json").read_text(encoding="utf-8"))
        self.assertEqual(decision["next_mode"], "full")
        self.assertEqual(decision["reason"], "verify-found-new-blocker")


class RoundAdvance(LaneCase):
    def test_a_progressed_decision_opens_the_next_round(self):
        self.full_round(verdict="Ready with fixes",
                        result={"applied": [{"finding": "f", "action": "a", "severity": "P2"}],
                                "failed": [], "advisory": [], "incomplete": []},
                        edit=SEED + "// tidy\n")
        lane = self.lane
        self.assertEqual(lane.n, 1)
        opened = lane.pre()
        self.assertEqual(lane.n, 2)
        self.assertEqual(opened["round"], 2)
        self.assertEqual(opened["review"], "run")
        self.assertEqual(opened["reason"], "initial")
        self.assertEqual(opened["attempt"], 1)
        self.assertEqual((lane.rd / "pre-head.txt").read_text(encoding="utf-8").strip(),
                         lane.head())

    def test_a_converged_decision_replays_the_promise_without_re_reviewing(self):
        self.full_round(closure={"closure_ok": True})
        lane = self.lane
        replay = lane.pre()
        self.assertEqual(replay["round"], 1)
        self.assertEqual(replay["review"], "reuse")
        self.assertEqual(replay["reason"], "terminal-replay")
        converge = lane.converge()
        self.assertEqual(converge.returncode, 0, converge.stdout)
        self.assertIn("CONVERGED round=1", converge.stdout)
        self.assertIn("<promise>REVIEW_CONVERGED</promise>", converge.stdout)

    def test_a_decision_that_is_not_bound_to_this_head_is_ignored(self):
        self.full_round(closure={"closure_ok": True})
        lane = self.lane
        (lane.wt / "src" / "a.ts").write_text(SEED + "// a hand edit\n", encoding="utf-8")
        lane.git("add", "-A")
        lane.git("commit", "-qm", "hand edit")
        result = lane.pre()
        self.assertIn("ROUND_DECISION_UNBOUND", result["stderr"])
        self.assertEqual(result["round"], 1)
        self.assertEqual(result["review"], "run")
        self.assertEqual(result["reason"], "head-moved")


class ReuseCases(LaneCase):
    """(a)-(g) of the plan's round-pre reuse table, driven through the helper."""

    def gated_round(self, verdict="Ready to merge"):
        lane = self.lane
        lane.pre()
        lane.review(verdict)
        self.assertEqual(lane.gate().returncode, 0)
        return lane

    def test_a_gated_envelope_at_the_pre_round_head_is_reused(self):
        lane = self.gated_round()
        result = lane.pre()
        self.assertEqual((result["review"], result["reason"]), ("reuse", "complete"))

    def test_the_post_fix_head_is_also_a_reuse_head(self):
        lane = self.gated_round("Ready with fixes")
        lane.fix_plan()
        lane.fixer(result={"applied": [{"finding": "f", "action": "a", "severity": "P2"}],
                           "failed": [], "advisory": [], "incomplete": []},
                   edit=SEED + "// repaired\n")
        lane.commit_fixer()
        attestation = json.loads((lane.rd / "fixer.ok").read_text(encoding="utf-8"))
        self.assertEqual(attestation["head"], lane.head())
        self.assertEqual(lane.pre()["review"], "reuse")

    def test_a_head_that_is_neither_is_head_moved(self):
        lane = self.gated_round()
        (lane.wt / "src" / "a.ts").write_text(SEED + "// foreign\n", encoding="utf-8")
        lane.git("add", "-A")
        lane.git("commit", "-qm", "foreign commit")
        result = lane.pre()
        self.assertEqual((result["review"], result["reason"]), ("run", "head-moved"))

    def test_an_incomplete_envelope_runs_again_and_consults_the_reclaim_ledger(self):
        lane = self.lane
        lane.pre()
        lane.review(complete=False)
        result = lane.pre()
        self.assertEqual((result["review"], result["reason"]), ("run", "interrupted"))
        self.assertIn("ROUND_RECLAIM_CONSULTED", result["stderr"])

    def test_a_complete_but_ungated_envelope_is_reused_and_gated_now(self):
        lane = self.lane
        lane.pre()
        lane.review()
        # Killed before review-gate ran at all.
        self.assertFalse((lane.rd / "review.ok").is_file())
        result = lane.pre()
        self.assertEqual(result["review"], "reuse")
        self.assertEqual(lane.gate().returncode, 0)
        self.assertTrue((lane.rd / "review.ok").is_file())

    def test_a_failed_gate_forces_a_fresh_review(self):
        lane = self.gated_round()
        lane.run("gate", lane.ad, "--fail", "degraded")
        result = lane.pre()
        self.assertEqual((result["review"], result["reason"]), ("run", "gate-failed"))


class AllowlistPrecondition(LaneCase):
    def test_an_empty_allowlist_is_a_typed_stop_not_an_empty_tree(self):
        """Otherwise T == HEAD's tree always, and every repair reads no-change."""
        self.lane.write_json("files-allowlist.json", [])
        result = self.lane.pre()
        self.assertEqual(result["exit"], 1)
        self.assertIn("files-allowlist.json is missing or empty", result["stderr"])

    def test_a_missing_allowlist_is_a_typed_stop(self):
        (self.lane.ad / "files-allowlist.json").unlink()
        result = self.lane.pre()
        self.assertEqual(result["exit"], 1)
        self.assertIn("ROUND_STATE=FAIL", result["stderr"])


class BaseValidation(LaneCase):
    def test_a_base_that_is_not_in_the_repository_fails(self):
        lane = self.lane
        (lane.ad / "bootstrap-head.txt").write_text("0" * 40 + "\n", encoding="utf-8")
        result = lane.pre()
        self.assertEqual(result["exit"], 1)
        self.assertIn("REVIEW_BASE=FAIL", result["stderr"])
        self.assertIn("reason=missing", result["stderr"])

    def test_a_base_that_is_not_an_ancestor_fails(self):
        lane = self.lane
        lane.git("checkout", "-q", "-b", "side")
        (lane.wt / "src" / "a.ts").write_text(SEED + "// side\n", encoding="utf-8")
        lane.git("add", "-A")
        lane.git("commit", "-qm", "side")
        side = lane.head()
        lane.git("checkout", "-q", "main")
        (lane.ad / "bootstrap-head.txt").write_text(side + "\n", encoding="utf-8")
        result = lane.pre()
        self.assertEqual(result["exit"], 1)
        self.assertIn("reason=not-ancestor", result["stderr"])


class InjectionSequence(LaneCase):
    """Kill after each of the eight durable boundaries; count duplicates.

    A duplicate is a `review: run` for a round whose complete envelope already
    matched the identity, or a `fixer: run` when repair.json or fixer.ok already
    matched. A first invocation (`initial`) and a genuinely interrupted one
    (`interrupted`) are neither.
    """

    BOUNDARIES = ("envelope", "gate", "repair", "ledger", "post-fix",
                  "commit", "fixer.ok", "decision")

    REPAIR = {"applied": [{"finding": "f", "action": "a", "severity": "P2"}],
              "failed": [], "advisory": [], "incomplete": []}

    def drive(self, kill_after):
        """Run a round, stop after `kill_after`, then resume to completion.

        Completion is tracked per ROUND: a decision that progresses opens a new
        round, and the review and repair that round asks for are first
        invocations, not repeats of the previous round's.
        """
        lane = self.lane
        duplicates = {"review": 0, "fixer": 0}
        completed = set()

        def do_pre():
            result = lane.pre()
            self.assertEqual(result.get("exit"), 0, result.get("stderr"))
            if result["review"] == "run" and (result["round"], "review") in completed:
                duplicates["review"] += 1
            self.assertIn(result["reason"],
                          ("initial", "interrupted", "identity", "head-moved",
                           "gate-failed", "rejected", "complete", "terminal-replay"))
            return result

        def do_fix_plan():
            result = lane.fix_plan()
            self.assertEqual(result.get("exit"), 0, result.get("stderr"))
            if result["fixer"] == "run" and (lane.n, "fixer") in completed:
                duplicates["fixer"] += 1
            return result

        def review_if_asked(result):
            if result["review"] == "run":
                lane.review("Ready with fixes")
                completed.add((result["round"], "review"))

        def repair_if_asked(plan):
            if plan["fixer"] == "run":
                lane.fixer(result=self.REPAIR, edit=SEED + f"// repaired r{lane.n}\n")
                completed.add((lane.n, "fixer"))

        review_if_asked(do_pre())
        if kill_after != "envelope":
            self.assertEqual(lane.gate().returncode, 0)
        if kill_after not in ("envelope", "gate"):
            repair_if_asked(do_fix_plan())
        if kill_after in ("ledger", "post-fix", "commit", "fixer.ok", "decision"):
            # Replay commit-fixer's own steps, stopping at the named boundary.
            lane.stage()
            tree = json.loads((lane.rd / "repair.json").read_text(encoding="utf-8"))["tree"]
            if kill_after in ("fixer.ok", "decision"):
                self.assertEqual(lane.run("commit-fixer", lane.ad).returncode, 0)
        if kill_after == "ledger":
            self.ledger_merge(lane)
        elif kill_after in ("post-fix", "commit"):
            self.ledger_merge(lane)
            (lane.rd / "post-fix.json").write_text(json.dumps({"tree": tree}), encoding="utf-8")
            if kill_after == "commit":
                lane.git("commit", "-qm", "fix(review): apply fixer feedback")
        if kill_after == "decision":
            lane.converge({"closure_ok": True})

        # --- the resume: every node runs again from round-pre ---
        review_if_asked(do_pre())
        self.assertEqual(lane.gate().returncode, 0)
        repair_if_asked(do_fix_plan())
        commit = lane.commit_fixer()
        self.assertEqual(commit.returncode, 0, commit.stdout + commit.stderr)
        converge = lane.converge({"closure_ok": True})
        self.assertEqual(converge.returncode, 0, converge.stdout + converge.stderr)
        return duplicates

    @staticmethod
    def ledger_merge(lane):
        """commit-fixer's step 1, run alone: the merge landed, the commit did not."""
        subprocess.run([sys.executable, str(LEDGER_STUB), "merge-fixer", str(lane.ad),
                        str(lane.n), "--result", str(lane.rd / "fixer-result.json"),
                        "--attempt", "1", "--head", lane.head()],
                       capture_output=True, encoding="utf-8", check=True)

    def test_every_kill_point_resumes_without_a_duplicate_activity(self):
        for boundary in self.BOUNDARIES:
            with self.subTest(boundary=boundary):
                self.setUp()
                duplicates = self.drive(boundary)
                self.assertEqual(duplicates, {"review": 0, "fixer": 0},
                                 f"kill after {boundary} repeated a completed activity")

    def test_a_truncated_envelope_is_never_reused(self):
        lane = self.lane
        lane.pre()
        record = lane.review_input()
        (lane.rd / "review-envelope.txt").write_text(
            f"Review complete\nInput: {record['id']}\nHead: {record['review_head']}\n",
            encoding="utf-8")
        self.assertEqual(lane.pre()["reason"], "interrupted")


if __name__ == "__main__":
    unittest.main()
