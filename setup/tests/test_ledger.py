#!/usr/bin/env python3
"""What the ledger must not get wrong.

Two of these are the whole reason the file exists. A merge that is not
idempotent turns a resume into a state change, and a resume happens after every
provider stream drop -- five of v1's thirteen review invocations. A merge that
downgrades a verified closure back to `applied` makes the positive closure
requirement unsatisfiable: the round that re-ran commit-fixer re-reads its own
older fixer-result.json and un-closes a P1 nothing has touched.

The third is compatibility. `exit-gate` indexes review-summary.json['verdict']
and `round-reclaim.sh` greps the same file for a non-empty verdict string;
neither has heard of the ledger, so every envelope merge still writes that file
in exactly the shape write-review-summary.py writes it.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ARCHON = Path(__file__).resolve().parents[2]
LEDGER = ARCHON / "setup" / "ledger.py"
HEAD_A = "1111111111111111111111111111111111111111"
HEAD_B = "2222222222222222222222222222222222222222"


def envelope(findings=(), dispositions=None, scope="full", verdict="Ready with fixes",
             head=HEAD_A, degraded=False):
    body = ["Envelope body."]
    if degraded:
        body.append("Code review degraded (headless mode)")
    payload = {"reviewer": "trio", "findings": list(findings),
               "residual_risks": [], "testing_gaps": []}
    if dispositions is not None:
        payload["dispositions"] = list(dispositions)
    body.append(json.dumps(payload))
    body += [f"Scope: {scope}", f"Base: {HEAD_B}", f"Head: {head}",
             "Input: abc123", f"Verdict: {verdict}", "Review complete"]
    return "\n".join(body) + "\n"


def finding(title, severity="P1", file="libs/a/b.service.ts", line=12):
    return {"title": title, "severity": severity, "file": file, "line": line,
            "why_it_matters": "it breaks", "autofix_class": "manual",
            "owner": "api", "requires_verification": True, "confidence": 80,
            "evidence": "read the line", "pre_existing": False}


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.ad = Path(tempfile.mkdtemp(prefix="ledger-"))
        self.addCleanup(shutil.rmtree, self.ad, ignore_errors=True)
        (self.ad / "round-1").mkdir()
        (self.ad / "round-1" / "pre-head.txt").write_text(HEAD_A + "\n")

    def run_ledger(self, *args, expect=0):
        p = subprocess.run([sys.executable, str(LEDGER), str(self.ad), *[str(a) for a in args]],
                           capture_output=True, encoding="utf-8")
        self.assertEqual(p.returncode, expect, p.stdout + p.stderr)
        return p.stdout

    def write_envelope(self, name="env.txt", **kw):
        p = self.ad / name
        p.write_text(envelope(**kw), encoding="utf-8")
        return p

    def write_fixer(self, name="fixer-result.json", **partitions):
        body = {"applied": [], "failed": [], "advisory": [], "incomplete": []}
        body.update(partitions)
        p = self.ad / name
        p.write_text(json.dumps(body), encoding="utf-8")
        return p

    def ledger(self, n=1):
        return json.loads((self.ad / f"round-{n}" / "ledger.json").read_text())

    def by_id(self, n=1):
        return {e["id"]: e for e in self.ledger(n)}

    # --- idempotency -----------------------------------------------------

    def test_merging_the_same_envelope_twice_changes_nothing(self):
        env = self.write_envelope(findings=[finding("a race in the share path"),
                                            finding("a missing guard", "P2")])
        self.run_ledger("merge-envelope", 1, env)
        once = (self.ad / "round-1" / "ledger.json").read_text()
        self.run_ledger("merge-envelope", 1, env)
        self.assertEqual((self.ad / "round-1" / "ledger.json").read_text(), once)
        self.assertEqual(len(self.ledger()), 2)

    def test_merging_the_same_fixer_result_twice_changes_nothing(self):
        env = self.write_envelope(findings=[finding("a race in the share path")])
        self.run_ledger("merge-envelope", 1, env)
        fx = self.write_fixer(applied=[{"finding": "a race in the share path",
                                        "file": "libs/a/b.service.ts",
                                        "severity": "P1", "action": "fixed"}])
        self.run_ledger("merge-fixer", 1, fx, 1)
        once = (self.ad / "round-1" / "ledger.json").read_text()
        self.run_ledger("merge-fixer", 1, fx, 1)
        self.assertEqual((self.ad / "round-1" / "ledger.json").read_text(), once)

    def test_the_fixer_result_lands_on_the_envelope_entry_not_a_second_one(self):
        # The whole point of finding_id: the fixer restates the finding text with
        # its own provenance and line number, and a second entry for the same
        # defect would make closure unreachable.
        env = self.write_envelope(findings=[finding("a race in the share path")])
        self.run_ledger("merge-envelope", 1, env)
        fx = self.write_fixer(applied=[{
            "finding": "a race in the share path -- re-raised by correctness this round "
                       "(b.service.ts:98)",
            "file": "libs/a/b.service.ts", "severity": "P1", "action": "fixed"}])
        self.run_ledger("merge-fixer", 1, fx, 1)
        self.assertEqual(len(self.ledger()), 1, self.ledger())
        self.assertEqual(self.ledger()[0]["state"], "applied")

    # --- the no-downgrade rule -------------------------------------------

    def test_a_verified_closure_is_not_downgraded_by_a_re_merged_repair(self):
        env = self.write_envelope(findings=[finding("a race in the share path")])
        self.run_ledger("merge-envelope", 1, env)
        fx = self.write_fixer(applied=[{"finding": "a race in the share path",
                                        "file": "libs/a/b.service.ts",
                                        "severity": "P1", "action": "fixed"}])
        self.run_ledger("merge-fixer", 1, fx, 1)
        fid = self.ledger()[0]["id"]
        # Round 2 verifies it closed at the post-repair head.
        (self.ad / "round-2").mkdir()
        (self.ad / "round-2" / "pre-head.txt").write_text(HEAD_B + "\n")
        self.run_ledger("copy-forward", 1, 2)
        vr = self.ad / "verify.txt"
        vr.write_text(envelope(scope="verify", head=HEAD_B, dispositions=[{
            "id": fid, "state": "closed", "file": "libs/a/b.service.ts",
            "line": 98, "evidence": "the guard is here now"}]), encoding="utf-8")
        self.run_ledger("merge-envelope", 2, vr)
        self.assertEqual(self.by_id(2)[fid]["state"], "closed")
        # A resume re-runs commit-fixer, which re-merges round 1's own result.
        self.run_ledger("merge-fixer", 2, fx, 2)
        self.assertEqual(self.by_id(2)[fid]["state"], "closed",
                         "a re-merged repair un-closed a verified finding")

    def test_a_repair_after_the_closure_does_reopen_it(self):
        # Negative control for the rule above. Without this, "never downgrade a
        # closed entry" could be implemented as "never downgrade", and a repair
        # that landed AFTER the verification would be silently ignored.
        env = self.write_envelope(findings=[finding("a race in the share path")])
        self.run_ledger("merge-envelope", 1, env)
        fid = self.ledger()[0]["id"]
        vr = self.ad / "verify.txt"
        vr.write_text(envelope(scope="verify", head=HEAD_A, dispositions=[{
            "id": fid, "state": "closed", "file": "libs/a/b.service.ts",
            "line": 98, "evidence": "closed"}]), encoding="utf-8")
        self.run_ledger("merge-envelope", 1, vr)
        self.assertEqual(self.by_id()[fid]["state"], "closed")
        (self.ad / "round-2").mkdir()
        (self.ad / "round-2" / "pre-head.txt").write_text(HEAD_B + "\n")
        self.run_ledger("copy-forward", 1, 2)
        fx = self.write_fixer(applied=[{"finding": "a race in the share path",
                                        "file": "libs/a/b.service.ts",
                                        "severity": "P1", "action": "fixed again"}])
        self.run_ledger("merge-fixer", 2, fx, 1)
        self.assertEqual(self.by_id(2)[fid]["state"], "applied",
                         "a later repair should move the entry off closed")

    def test_a_closure_without_a_cited_line_is_read_as_open(self):
        env = self.write_envelope(findings=[finding("a race in the share path")])
        self.run_ledger("merge-envelope", 1, env)
        fid = self.ledger()[0]["id"]
        vr = self.ad / "verify.txt"
        vr.write_text(envelope(scope="verify", head=HEAD_B, dispositions=[{
            "id": fid, "state": "closed", "evidence": "the fixer said so"}]),
            encoding="utf-8")
        self.run_ledger("merge-envelope", 1, vr)
        self.assertEqual(self.by_id()[fid]["state"], "open")

    def test_severity_is_never_lowered_by_a_later_merge(self):
        env = self.write_envelope(findings=[finding("a race in the share path", "P0")])
        self.run_ledger("merge-envelope", 1, env)
        fx = self.write_fixer(applied=[{"finding": "a race in the share path",
                                        "file": "libs/a/b.service.ts",
                                        "severity": "P3", "action": "fixed"}])
        self.run_ledger("merge-fixer", 1, fx, 1)
        self.assertEqual(self.ledger()[0]["severity"], "P0")

    # --- closure ---------------------------------------------------------

    def test_an_applied_p1_does_not_satisfy_closure(self):
        env = self.write_envelope(findings=[finding("a race in the share path")])
        self.run_ledger("merge-envelope", 1, env)
        fx = self.write_fixer(applied=[{"finding": "a race in the share path",
                                        "file": "libs/a/b.service.ts",
                                        "severity": "P1", "action": "fixed"}])
        self.run_ledger("merge-fixer", 1, fx, 1)
        out = self.run_ledger("closure", 1, expect=1)
        self.assertIn("CLOSURE round=1", out)
        self.assertIn("CLOSURE_UNCLOSED", out)

    def test_closure_holds_once_every_blocker_is_verified_closed(self):
        env = self.write_envelope(findings=[finding("a race in the share path"),
                                            finding("a nit", "P3")])
        self.run_ledger("merge-envelope", 1, env)
        fid = next(e["id"] for e in self.ledger() if e["severity"] == "P1")
        vr = self.ad / "verify.txt"
        vr.write_text(envelope(scope="verify", head=HEAD_B, dispositions=[{
            "id": fid, "state": "closed", "file": "libs/a/b.service.ts",
            "line": 98, "evidence": "guarded"}]), encoding="utf-8")
        self.run_ledger("merge-envelope", 1, vr)
        out = self.run_ledger("closure", 1, expect=0)
        self.assertIn("closed=1", out)
        self.assertNotIn("CLOSURE_UNCLOSED", out)

    def test_a_regressed_blocker_fails_closure(self):
        env = self.write_envelope(findings=[finding("a race in the share path")])
        self.run_ledger("merge-envelope", 1, env)
        fid = self.ledger()[0]["id"]
        vr = self.ad / "verify.txt"
        vr.write_text(envelope(scope="verify", head=HEAD_B, verdict="Not ready",
                               dispositions=[{"id": fid, "state": "regressed",
                                              "file": "libs/a/b.service.ts", "line": 99,
                                              "evidence": "now throws instead"}]),
                      encoding="utf-8")
        self.run_ledger("merge-envelope", 1, vr)
        out = self.run_ledger("closure", 1, expect=1)
        self.assertIn("regressed=1", out)

    def test_a_deferred_p2_does_not_block_closure(self):
        env = self.write_envelope(findings=[finding("a nit", "P2")])
        self.run_ledger("merge-envelope", 1, env)
        fx = self.write_fixer(advisory=[{"finding": "a nit", "file": "libs/a/b.service.ts",
                                         "severity": "P2", "action": "Deferred: cosmetic"}])
        self.run_ledger("merge-fixer", 1, fx, 1)
        self.run_ledger("closure", 1, expect=0)

    # --- copy-forward, residuals, summary compatibility -------------------

    def test_history_is_carried_into_the_next_round(self):
        env = self.write_envelope(findings=[finding("a race in the share path"),
                                            finding("a nit", "P3")])
        self.run_ledger("merge-envelope", 1, env)
        (self.ad / "round-2").mkdir()
        self.run_ledger("copy-forward", 1, 2)
        self.assertEqual(len(self.ledger(2)), 2)
        self.assertEqual({e["round"] for e in self.ledger(2)}, {1})

    def test_copy_forward_does_not_clobber_a_round_that_already_merged(self):
        env = self.write_envelope(findings=[finding("a race in the share path")])
        self.run_ledger("merge-envelope", 1, env)
        (self.ad / "round-2").mkdir()
        (self.ad / "round-2" / "pre-head.txt").write_text(HEAD_B + "\n")
        self.run_ledger("copy-forward", 1, 2)
        env2 = self.write_envelope("env2.txt", findings=[finding("a second one", "P2")])
        self.run_ledger("merge-envelope", 2, env2)
        out = self.run_ledger("copy-forward", 1, 2)
        self.assertIn("skipped=already-present", out)
        self.assertEqual(len(self.ledger(2)), 2)

    def test_a_round_with_no_ledger_does_not_erase_the_history(self):
        # Measured on v1 run 8ddb9cce: round 6 wrote no fixer-result.json, so a
        # straight copy-forward carried 0 of 36 entries into round 7 and closure
        # passed on an empty set. An empty ledger is not "no findings".
        env = self.write_envelope(findings=[finding("a race in the share path"),
                                            finding("a nit", "P3")])
        self.run_ledger("merge-envelope", 1, env)
        (self.ad / "round-2").mkdir()
        (self.ad / "round-3").mkdir()
        out = self.run_ledger("copy-forward", 2, 3)
        self.assertIn("LEDGER_COPY_BACKFILL from=1", out)
        self.assertEqual(len(self.ledger(3)), 2)
        self.run_ledger("closure", 3, expect=1)

    def test_round_1_copying_from_nothing_is_still_empty(self):
        # Negative control for the backfill: a genuinely empty history must not
        # invent entries, or every round 1 would inherit a phantom ledger.
        (self.ad / "round-2").mkdir()
        out = self.run_ledger("copy-forward", 1, 2)
        self.assertNotIn("LEDGER_COPY_BACKFILL", out)
        self.assertEqual(self.ledger(2), [])

    def test_residuals_carry_p2_p3_dispositions_and_never_a_blocker(self):
        env = self.write_envelope(findings=[finding("a race in the share path"),
                                            finding("a nit", "P3"),
                                            finding("a second nit", "P2",
                                                    file="libs/a/c.service.ts")])
        self.run_ledger("merge-envelope", 1, env)
        fx = self.write_fixer(
            applied=[{"finding": "a race in the share path", "file": "libs/a/b.service.ts",
                      "severity": "P1", "action": "fixed"}],
            advisory=[{"finding": "a nit", "file": "libs/a/b.service.ts",
                       "severity": "P3", "action": "Deferred: cosmetic"},
                      {"finding": "a second nit", "file": "libs/a/c.service.ts",
                       "severity": "P2", "action": "Deferred: out of scope"}])
        self.run_ledger("merge-fixer", 1, fx, 1)
        out_path = self.ad / "residuals.json"
        out = self.run_ledger("residuals", 1, out_path)
        rows = json.loads(out_path.read_text())
        self.assertIn("count=2", out)
        self.assertEqual({r["severity"] for r in rows}, {"P2", "P3"})

    def test_every_envelope_merge_writes_the_summary_exit_gate_reads(self):
        env = self.write_envelope(findings=[finding("a race in the share path")],
                                  verdict="Not ready")
        self.run_ledger("merge-envelope", 1, env)
        summary = json.loads((self.ad / "round-1" / "review-summary.json").read_text())
        self.assertEqual(set(summary), {"verdict", "residual_count", "degraded"})
        self.assertEqual(summary["verdict"], "Not ready")
        self.assertIs(summary["degraded"], False)
        self.assertEqual(summary["residual_count"], 1)

    def test_the_summary_is_written_by_the_one_script_every_lane_calls(self):
        # Eleven workflows and the linux smoke run call write-review-summary.py.
        # A second implementation of the same three keys inside the ledger would
        # drift from it silently, and the drift surfaces as round-reclaim no
        # longer recognising reviewed rounds. Delete the script: the merge fails
        # loudly instead of writing a shape nothing else agrees with.
        script = ARCHON / "setup" / "write-review-summary.py"
        moved = self.ad / "write-review-summary.py.moved"
        shutil.move(str(script), str(moved))
        self.addCleanup(lambda: shutil.move(str(moved), str(script))
                        if moved.exists() else None)
        env = self.write_envelope(findings=[])
        self.run_ledger("merge-envelope", 1, env, expect=1)

    def test_a_degraded_envelope_is_recorded_as_degraded(self):
        env = self.write_envelope(findings=[], degraded=True)
        self.run_ledger("merge-envelope", 1, env)
        summary = json.loads((self.ad / "round-1" / "review-summary.json").read_text())
        self.assertIs(summary["degraded"], True)

    def test_the_summary_verdict_is_greppable_the_way_round_reclaim_greps_it(self):
        # round-reclaim.sh matches '"verdict"[[:space:]]*:[[:space:]]*"[^"]' to
        # decide a round produced a review at all. A compact separator or a key
        # rename makes every round look unreviewed and the counter reclaims it.
        env = self.write_envelope(findings=[])
        self.run_ledger("merge-envelope", 1, env)
        raw = (self.ad / "round-1" / "review-summary.json").read_text()
        p = subprocess.run(["grep", "-qE", '"verdict"[[:space:]]*:[[:space:]]*"[^"]'],
                           input=raw, encoding="utf-8")
        self.assertEqual(p.returncode, 0, raw)

    def test_a_cross_repo_partition_is_filed_and_blocks_closure(self):
        env = self.write_envelope(findings=[finding("the mcp side needs this too")])
        self.run_ledger("merge-envelope", 1, env)
        fx = self.write_fixer(cross_repo=[{"finding": "the mcp side needs this too",
                                           "file": "libs/a/b.service.ts",
                                           "severity": "P1", "action": "filed"}])
        self.run_ledger("merge-fixer", 1, fx, 1)
        self.assertEqual(self.ledger()[0]["state"], "filed")
        self.run_ledger("closure", 1, expect=1)

    def test_a_pin_conflict_partition_keeps_its_symbol(self):
        env = self.write_envelope(findings=[finding("only fixable by changing the pin")])
        self.run_ledger("merge-envelope", 1, env)
        fx = self.write_fixer(pin_conflict=[{"finding": "only fixable by changing the pin",
                                             "file": "libs/a/b.service.ts", "severity": "P1",
                                             "symbol": "GroupService.shareGroup",
                                             "action": "blocked"}])
        self.run_ledger("merge-fixer", 1, fx, 1)
        self.assertEqual(self.ledger()[0]["state"], "pin_conflict")
        self.assertEqual(self.ledger()[0]["symbol"], "GroupService.shareGroup")

    def test_design_expanded_survives_the_merge(self):
        # converge row 5 reads this flag off the fixer result, but review-mode
        # reads the persisted next_mode; the ledger is where the flag stays
        # legible after the round closes.
        env = self.write_envelope(findings=[finding("needs a new method")])
        self.run_ledger("merge-envelope", 1, env)
        fx = self.write_fixer(applied=[{"finding": "needs a new method",
                                        "file": "libs/a/b.service.ts", "severity": "P1",
                                        "design_expanded": True, "action": "added one"}])
        self.run_ledger("merge-fixer", 1, fx, 1)
        self.assertIs(self.ledger()[0]["design_expanded"], True)

    def test_an_unknown_command_fails_loudly(self):
        p = subprocess.run([sys.executable, str(LEDGER), str(self.ad), "frobnicate"],
                           capture_output=True, encoding="utf-8")
        self.assertEqual(p.returncode, 2)
        self.assertIn("LEDGER=FAIL", p.stdout)


if __name__ == "__main__":
    unittest.main()
