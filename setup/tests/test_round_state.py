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
from unittest import mock

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

    def run(self, *args, kill_after=None):
        env = dict(os.environ, ARCHON_LEDGER_PY=str(LEDGER_STUB))
        if kill_after:
            env["ROUND_STATE_KILL_AFTER"] = kill_after
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

    def converge(self, ledger=None):
        """`ledger` is this round's ledger.json, the same file the real ledger
        writes. The decision table is driven through it rather than through a
        stub-only side channel, so the closure predicate under test is the one
        that ships."""
        if ledger is not None:
            (self.rd / "ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
        return self.run("converge", self.ad)

    def ledger_calls(self):
        path = self.ad / "ledger-calls.txt"
        return path.read_text(encoding="utf-8").splitlines() if path.is_file() else []


def entry(eid, severity="P1", state="closed", round_no=1, **extra):
    """One ledger row, in the shape setup/ledger.py writes."""
    return {"id": eid, "severity": severity, "state": state, "round": round_no,
            "title": eid, **extra}


CLOSED = [entry("a"), entry("b")]


class LaneCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lane = Lane(self.tmp.name)

    def full_round(self, verdict="Ready to merge", result=None, edit=None, ledger=None):
        """A whole round, start to decision, with no kills."""
        lane = self.lane
        pre = lane.pre()
        lane.review(verdict)
        self.assertEqual(lane.gate().returncode, 0)
        plan = lane.fix_plan()
        if plan["fixer"] == "run":
            lane.fixer(result=result, edit=edit)
        commit = lane.commit_fixer()
        converge = lane.converge(ledger)
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
        lane.fixer(result={"applied": [{"finding_id": "f0000000feed", "finding": "f", "action": "a", "severity": "P2"}],
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
        lane.fixer(result={"applied": [{"finding_id": "f0000000feed", "finding": "f", "action": "a", "severity": "P2"}],
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
        lane.fixer(result={"applied": [{"finding_id": "f0000000feed", "finding": "f", "action": "a", "severity": "P2"}],
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
        _, _, _, converge = self.full_round(ledger=CLOSED)
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
        # A valid result, so the precondition passes and the ABSENT attestation
        # is what converge names.
        (lane.rd / "fixer-result.json").write_text(
            json.dumps({"applied": [], "failed": [], "advisory": []}), encoding="utf-8")
        proc = lane.converge()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("FIXER_ABSENT", proc.stdout)

    def test_an_unreadable_fixer_result_is_blocked_before_the_attestation(self):
        """0b runs ahead of V(fixer.ok), which itself needs a valid result: the
        stop has to name the result, not accuse the tree of drifting."""
        lane = self.lane
        lane.pre()
        lane.review()
        lane.gate()
        proc = lane.converge()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("FIXER_BLOCKED round=1", proc.stdout)
        self.assertNotIn("FIXER_TREE_DRIFT", proc.stdout)

    def test_a_failed_partition_is_blocked_not_drift(self):
        lane = self.lane
        lane.pre()
        lane.review("Ready with fixes")
        lane.gate()
        lane.fix_plan()
        lane.fixer(result={"applied": [], "advisory": [], "incomplete": [],
                           "failed": [{"finding_id": "x1", "finding": "f", "action": "a"}]})
        proc = lane.converge()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("FIXER_BLOCKED round=1", proc.stdout)

    def test_an_entry_without_a_finding_id_is_blocked(self):
        """The ledger keys on finding_id; without it a repair mints a second
        entry and the reviewer's original can never reach closed."""
        lane = self.lane
        lane.pre()
        lane.review("Ready with fixes")
        lane.gate()
        lane.fix_plan()
        lane.fixer(result={"applied": [{"finding": "f", "action": "a", "severity": "P2"}],
                           "failed": [], "advisory": [], "incomplete": []},
                   edit=SEED + "// repaired\n")
        lane.commit_fixer()
        proc = lane.converge(CLOSED)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("FIXER_BLOCKED round=1", proc.stdout)
        self.assertIn("applied entry missing finding_id", proc.stdout)

    def test_an_unclosed_blocker_progresses_to_a_verify_round(self):
        _, _, _, converge = self.full_round(ledger=[entry("a", state="open")])
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
        converge = lane.converge(CLOSED)
        self.assertEqual(converge.returncode, 1, converge.stdout)
        self.assertIn("REVIEW_TREE_DRIFT round=1", converge.stdout)

    def test_not_ready_with_no_open_blocker_is_a_contract_violation(self):
        _, _, _, converge = self.full_round(verdict="Not ready",
                                            ledger=[entry("a", severity="P3", state="deferred")])
        self.assertEqual(converge.returncode, 1)
        self.assertIn("NOT_READY_WITHOUT_BLOCKER round=1", converge.stdout)

    def test_a_pin_conflict_blocks_before_every_other_row(self):
        _, _, _, converge = self.full_round(
            result={"applied": [], "failed": [], "advisory": [],
                    "incomplete": [{"finding_id": "f0000000feed", "finding": "f", "action": "a", "severity": "P2"}]},
            ledger=[entry("a", state="pin_conflict", symbol="GroupService.shareGroup")])
        self.assertEqual(converge.returncode, 1)
        self.assertIn("PIN_CONFLICT round=1 symbol=GroupService.shareGroup", converge.stdout)

class CrossRepoAcknowledgement(LaneCase):
    """A cross_repo entry resolves only when a human records where it was filed.

    Mirrors main's converge: round-state calls cross-repo-keys.py --gate exactly
    as that node does. The closure allowance is the part that is ours -- a P0/P1
    filed against ANOTHER repository can never be `closed` here, because the
    defect is not in this repository, so `filed_acked` is what lets the round
    converge without pretending it was fixed.
    """

    FINDING = {"finding_id": "x0000000cr00", "finding": "mcp tool drops the group id",
               "action": "route to goodword-mcp", "producer_repo": "goodword-mcp",
               "severity": "P1"}

    def key(self):
        import hashlib
        text = " ".join(self.FINDING["finding"].split())
        return hashlib.sha256(
            f"{self.FINDING['producer_repo']}\n{text}".encode("utf-8")).hexdigest()[:16]

    def round_with_cross_repo(self, ack=None):
        lane = self.lane
        lane.pre()
        lane.review("Ready with fixes")
        lane.gate()
        lane.fix_plan()
        lane.fixer(result={"applied": [], "failed": [], "advisory": [], "incomplete": [],
                           "cross_repo": [self.FINDING]})
        lane.commit_fixer()
        if ack is not None:
            (lane.ad / "cross-repo-filed.json").write_text(json.dumps(ack), encoding="utf-8")
        return lane

    def test_an_unacknowledged_finding_blocks(self):
        lane = self.round_with_cross_repo()
        proc = lane.converge([entry("x0000000cr00", state="filed")])
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("CROSS_REPO_FINDING round=1 count=1 repos=goodword-mcp", proc.stdout)

    def test_an_acknowledged_finding_converges_and_is_recorded_filed_acked(self):
        lane = self.round_with_cross_repo(
            [{"key": self.key(), "filed": "https://github.com/o/goodword-mcp/issues/7",
              "by": "operator"}])
        proc = lane.converge([entry("x0000000cr00", state="filed")])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"CROSS_REPO_ACKED key={self.key()} repo=goodword-mcp", proc.stdout)
        self.assertNotIn("CROSS_REPO_FINDING", proc.stdout)
        self.assertIn("CONVERGED round=1", proc.stdout)
        recorded = json.loads((lane.rd / "ledger.json").read_text(encoding="utf-8"))
        self.assertEqual(["filed_acked"], [e["state"] for e in recorded])

    def test_an_acknowledged_blocker_does_not_hold_closure_open(self):
        """ledger.py counts any P0/P1 that is not `closed` as unclosed, and this
        one never can be. Without the allowance the round can never converge."""
        lane = self.round_with_cross_repo(
            [{"key": self.key(), "filed": "https://x.test/1", "by": "operator"}])
        proc = lane.converge([entry("x0000000cr00", severity="P1", state="filed")])
        self.assertIn("CONVERGED round=1", proc.stdout)

    def test_an_invalid_acknowledgement_still_stops(self):
        cases = {
            "wrong key": [{"key": "0" * 16, "filed": "https://x.test/1", "by": "op"}],
            "missing url": [{"key": self.key(), "filed": "", "by": "op"}],
            "not a url": [{"key": self.key(), "filed": "filed it in slack", "by": "op"}],
            "missing by": [{"key": self.key(), "filed": "https://x.test/1", "by": " "}],
            "not a list": {"key": self.key(), "filed": "https://x.test/1", "by": "op"},
        }
        for name, ack in cases.items():
            with self.subTest(case=name):
                self.setUp()
                lane = self.round_with_cross_repo(ack)
                proc = lane.converge([entry("x0000000cr00", state="filed")])
                self.assertEqual(proc.returncode, 1, proc.stdout)
                self.assertIn("CROSS_REPO_FINDING round=1 count=1 repos=goodword-mcp",
                              proc.stdout)

    def test_a_round_with_no_cross_repo_entries_prints_nothing_extra(self):
        """--gate is silent when there is nothing to gate. CROSS_REPO_KEYS=NONE
        belongs to the script's operator listing mode, not to converge."""
        _, _, _, proc = self.full_round(ledger=CLOSED)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        # CROSS_REPO=0 in check-fixer-result's summary is expected; the gate's
        # own tokens are what must be absent.
        self.assertNotIn("CROSS_REPO_ACKED", proc.stdout)
        self.assertNotIn("CROSS_REPO_FINDING", proc.stdout)
        self.assertNotIn("CROSS_REPO_KEYS", proc.stdout)


    def test_an_applied_p0_forces_a_full_next_round(self):
        _, _, _, converge = self.full_round(
            verdict="Ready with fixes",
            result={"applied": [{"finding_id": "f0000000p000", "finding": "f", "action": "a", "severity": "P0"}],
                    "failed": [], "advisory": [], "incomplete": []},
            edit=SEED + "// p0 repair\n")
        self.assertEqual(converge.returncode, 0, converge.stdout)
        decision = json.loads((self.lane.rd / "decision.json").read_text(encoding="utf-8"))
        self.assertEqual(decision["next_mode"], "full")

    def test_a_design_expanding_repair_forces_a_full_next_round(self):
        _, _, _, converge = self.full_round(
            verdict="Ready with fixes",
            result={"applied": [{"finding_id": "f0000000p100", "finding": "f", "action": "a",
                                 "severity": "P1", "design_expanded": True}],
                    "failed": [], "advisory": [], "incomplete": []},
            edit=SEED + "// widened\n")
        decision = json.loads((self.lane.rd / "decision.json").read_text(encoding="utf-8"))
        self.assertEqual(decision["next_mode"], "full")

    def test_a_p2_repair_asks_for_a_verify_round(self):
        self.full_round(verdict="Ready with fixes",
                        result={"applied": [{"finding_id": "f0000000feed", "finding": "f", "action": "a", "severity": "P2"}],
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
        converge = lane.converge([entry("new", state="closed", round_no=1)])
        self.assertEqual(converge.returncode, 0, converge.stdout)
        decision = json.loads((lane.rd / "decision.json").read_text(encoding="utf-8"))
        self.assertEqual(decision["next_mode"], "full")
        self.assertEqual(decision["reason"], "verify-found-new-blocker")


class RoundAdvance(LaneCase):
    def test_a_progressed_decision_opens_the_next_round(self):
        self.full_round(verdict="Ready with fixes",
                        result={"applied": [{"finding_id": "f0000000feed", "finding": "f", "action": "a", "severity": "P2"}],
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
        self.full_round(ledger=CLOSED)
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
        self.full_round(ledger=CLOSED)
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
        lane.fixer(result={"applied": [{"finding_id": "f0000000feed", "finding": "f", "action": "a", "severity": "P2"}],
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


class PrerunBaseline(LaneCase):
    """prerun-dirs.txt must sort exactly as the gate's post-dirs.txt does.

    review-gate builds the post list with `ls -1d …/*/ | LC_ALL=C sort` and then
    `comm -13`s the two. comm assumes both inputs are in the SAME order and says
    nothing when they are not -- it just reports the wrong difference, which
    picks the wrong ce-code-review run directory to read a verdict from. The
    baseline is written by Python here and the other side by the shell, so the
    two orderings are compared against each other rather than each being
    assumed correct.
    """

    def seed_root(self, names):
        root = self.lane.root / "ce-runs"
        for name in names:
            (root / name).mkdir(parents=True)
        return root

    def shell_listing(self, root, locale="C"):
        proc = subprocess.run(
            f'ls -1d "{root}"/*/ 2>/dev/null | LC_ALL=C sort',
            shell=True, capture_output=True, encoding="utf-8",
            env=dict(os.environ, LC_ALL=locale))
        return proc.stdout

    # Uppercase sorts before lowercase in C collation and interleaves in most
    # UTF-8 ones, so this fixture discriminates the two. No case-only pair: the
    # macOS filesystem is case-insensitive and would collide them.
    NAMES = ["Beta-run", "alpha-run", "20260916-zz", "_under", "élan"]

    def test_the_baseline_matches_the_shell_listing_the_gate_uses(self):
        root = self.seed_root(self.NAMES)
        with mock.patch.dict(os.environ, {"CE_REVIEW_ROOT": str(root)}):
            self.assertEqual(self.lane.run("pre", self.lane.ad).returncode, 0)
        written = (self.lane.rd / "prerun-dirs.txt").read_text(encoding="utf-8")
        self.assertEqual(written, self.shell_listing(root))

    def test_a_utf8_locale_does_not_reorder_the_baseline(self):
        root = self.seed_root(self.NAMES)
        with mock.patch.dict(os.environ, {"CE_REVIEW_ROOT": str(root),
                                                   "LC_ALL": "en_US.UTF-8"}):
            self.assertEqual(self.lane.run("pre", self.lane.ad).returncode, 0)
        written = (self.lane.rd / "prerun-dirs.txt").read_text(encoding="utf-8")
        self.assertEqual(written, self.shell_listing(root))

    def test_an_empty_root_writes_an_empty_file_not_a_missing_one(self):
        root = self.lane.root / "ce-empty"
        root.mkdir()
        with mock.patch.dict(os.environ, {"CE_REVIEW_ROOT": str(root)}):
            self.lane.run("pre", self.lane.ad)
        baseline = self.lane.rd / "prerun-dirs.txt"
        self.assertTrue(baseline.is_file())
        self.assertEqual(baseline.read_text(encoding="utf-8"), "")

    def test_a_root_that_does_not_exist_is_also_an_empty_file(self):
        with mock.patch.dict(os.environ,
                                      {"CE_REVIEW_ROOT": str(self.lane.root / "gone")}):
            self.lane.run("pre", self.lane.ad)
        self.assertTrue((self.lane.rd / "prerun-dirs.txt").is_file())


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

    REPAIR = {"applied": [{"finding_id": "f0000000feed", "finding": "f", "action": "a", "severity": "P2"}],
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
        tree = None
        if kill_after in ("ledger", "post-fix", "commit", "fixer.ok", "decision"):
            lane.stage()
            if kill_after in ("fixer.ok", "decision"):
                self.assertEqual(lane.run("commit-fixer", lane.ad).returncode, 0)
            else:
                # The real commit-fixer, killed at its own boundary. Hand-building
                # the post-kill state would test a state the harness invented
                # rather than the one this code leaves behind, which is the whole
                # point of the plan's recovery traces.
                proc = lane.run("commit-fixer", lane.ad, kill_after=self.KILLS[kill_after])
                self.assertNotEqual(proc.returncode, 0, proc.stdout)
                self.assertFalse((lane.rd / "fixer.ok").is_file())
        if kill_after == "decision":
            lane.converge(CLOSED)

        # --- the resume: every node runs again from round-pre ---
        review_if_asked(do_pre())
        self.assertEqual(lane.gate().returncode, 0)
        repair_if_asked(do_fix_plan())
        commit = lane.commit_fixer()
        self.assertEqual(commit.returncode, 0, commit.stdout + commit.stderr)
        converge = lane.converge(CLOSED)
        self.assertEqual(converge.returncode, 0, converge.stdout + converge.stderr)
        return duplicates

    # The plan's kill points 4, 5 and 6, named as round-state.py spells them.
    KILLS = {"ledger": "ledger-merged", "post-fix": "post-fix-written",
             "commit": "committed"}

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
