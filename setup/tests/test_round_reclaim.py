#!/usr/bin/env python3
"""A review round counter is durable and is incremented BEFORE the review runs.

Run 38d72218 made the consequence concrete: round 2's review was killed by its
cost cap 10.5 minutes in, having written six of its nine persona files and no
envelope. The counter still read 2. With the bugfix lane's default cap of 2,
that single infrastructure death left the run with one real review round; a
second would have exhausted the loop having reviewed nothing, and the run would
then have demanded a human accept-residuals.txt for a failure that was never
about the code.

A round that left no proof artifact was billed, not spent. These tests pin the
reclaim, its bound (one per run), and its placement -- it belongs in round-pre
ahead of the cap check, and must never touch converge, where N drives the
verdict.

Round 3 of the same run then showed that file existence is the wrong proof: the
review node returned while its personas were still running, leaving a 22KB
envelope truncated mid-persona and review-summary.json reading {"verdict": ""}.
Every downstream gate rejects an empty verdict, so that round produced nothing
-- but a file-existence check scored it spent. The proof is now the verdict
itself, via the script's optional pattern argument."""
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ARCHON = Path(__file__).resolve().parents[2]
SCRIPT = ARCHON / "setup/round-reclaim.sh"
REVIEW_LANES = ["bugfix", "bugfix-lite", "full-sdlc-api", "full-sdlc-web",
                "full-sdlc-api-lite"]


VERDICT_PATTERN = r'"verdict"[[:space:]]*:[[:space:]]*"[^"]'


def run(ad, n, prefix="round-", proof="review-envelope.txt", pattern=None):
    cmd = ["bash", str(SCRIPT), str(ad), str(n), prefix, proof]
    if pattern is not None:
        cmd.append(pattern)
    r = subprocess.run(cmd, capture_output=True, encoding="utf-8")
    return r.stdout.strip(), r.stderr, r.returncode


class RoundReclaimTest(unittest.TestCase):
    def setUp(self):
        self.ad = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: None)

    def _round(self, n, envelope=None):
        d = self.ad / f"round-{n}"
        d.mkdir(parents=True, exist_ok=True)
        if envelope is not None:
            (d / "review-envelope.txt").write_text(envelope, encoding="utf-8")
        return d

    def test_round_without_proof_is_reclaimed(self):
        self._round(2)  # died before writing an envelope
        out, err, rc = run(self.ad, 2)
        self.assertEqual(rc, 0)
        self.assertEqual(out, "1")
        self.assertIn("ROUND_RECLAIM=round-2", err)

    def test_round_with_proof_is_not_reclaimed(self):
        # Negative control: a round that really happened keeps its number even
        # if the gate later rejected its findings.
        self._round(2, envelope="# review envelope\nVerdict: not ready\n")
        out, _, _ = run(self.ad, 2)
        self.assertEqual(out, "2")

    def test_empty_proof_file_does_not_count_as_proof(self):
        self._round(2, envelope="")
        out, _, _ = run(self.ad, 2)
        self.assertEqual(out, "1")

    def test_reclaim_is_bounded_to_once_per_run(self):
        # A node that dies every time must still reach the cap, not spin.
        self._round(2)
        self.assertEqual(run(self.ad, 2)[0], "1")
        self.assertEqual(run(self.ad, 2)[0], "2")
        self.assertEqual(run(self.ad, 2)[0], "2")

    def test_two_distinct_unproven_rounds_are_both_reclaimed(self):
        # The budget is 2 because run 38d72218 needed exactly that: round 2 died
        # to a cost cap and round 3 returned mid-fan-out -- two unrelated
        # infrastructure failures, and a budget of 1 meant the run had to be
        # hand-carried past the second one.
        self._round(2); self._round(3)
        self.assertEqual(run(self.ad, 2)[0], "1")
        self.assertEqual(run(self.ad, 3)[0], "2")

    def test_a_third_unproven_round_exhausts_the_budget(self):
        # Three means the failure is the norm, not a flake. Stop and make
        # someone look instead of buying rounds forever.
        for n in (2, 3, 4):
            self._round(n)
        self.assertEqual(run(self.ad, 2)[0], "1")
        self.assertEqual(run(self.ad, 3)[0], "2")
        out, err, _ = run(self.ad, 4)
        self.assertEqual(out, "4")
        self.assertIn("ROUND_RECLAIM=EXHAUSTED used=2 budget=2", err)

    def test_the_same_round_number_is_never_reclaimed_twice(self):
        # A node that dies at the same round every time must walk the counter
        # up to the cap, not spin on one number until the budget is gone.
        self._round(2)
        self.assertEqual(run(self.ad, 2)[0], "1")
        self.assertEqual(run(self.ad, 2)[0], "2")

    def test_the_budget_is_operator_overridable(self):
        (self.ad / "round-reclaim-cap.txt").write_text("1\n", encoding="utf-8")
        self._round(2); self._round(3)
        self.assertEqual(run(self.ad, 2)[0], "1")
        out, err, _ = run(self.ad, 3)
        self.assertEqual(out, "3")
        self.assertIn("budget=1", err)

    def test_zero_and_junk_counters_pass_through_untouched(self):
        self.assertEqual(run(self.ad, 0)[0], "0")
        self.assertEqual(run(self.ad, "")[0], "")
        self.assertEqual(run(self.ad, "abc")[0], "abc")

    def test_never_writes_a_negative_counter(self):
        self._round(1)
        self.assertEqual(run(self.ad, 1)[0], "0")
        # The floor that actually matters is N=0: without it the first loop
        # iteration reclaims a round that never existed and hands the caller
        # -1, which then becomes a round--1 path. Exercise it here rather than
        # leaving it to the pass-through test, whose name hides the stake.
        fresh = Path(tempfile.mkdtemp())
        self.assertEqual(run(fresh, 0)[0], "0")
        self.assertFalse((fresh / "round-reclaimed.txt").exists(),
                         "a zero counter must not consume a ledger entry")


class RoundReclaimWiringTest(unittest.TestCase):
    def _sites(self, path):
        lines = Path(path).read_text().split("\n")
        out = []
        for i, l in enumerate(lines):
            if "round-reclaim.sh" not in l:
                continue
            back = "\n".join(lines[max(0, i - 40):i])
            owner = None
            for m in re.finditer(r"-\s*id:\s*([\w-]+)", back):
                owner = m.group(1)
            out.append((i, owner, lines))
        return out

    def test_every_review_lane_reclaims(self):
        for lane in REVIEW_LANES:
            p = ARCHON / f"workflows/{lane}.yaml"
            self.assertIn("round-reclaim.sh", p.read_text(),
                          f"{lane} never reclaims a billed-but-unspent round")

    def test_reclaim_runs_before_the_cap_check(self):
        # A reclaim after the cap check is decoration: the cap would already
        # have stopped the run on a round nobody reviewed.
        for lane in REVIEW_LANES:
            text = (ARCHON / f"workflows/{lane}.yaml").read_text()
            i = text.index("round-reclaim.sh")
            cap = text.index('CAP=$(cat "$ARTIFACTS_DIR/round-cap.txt"')
            self.assertLess(i, cap, f"{lane}: reclaim runs after the cap check")

    def test_reclaim_never_lands_in_converge(self):
        # converge reads N to report the verdict and enforce the cap; moving it
        # there would misreport the round and re-run a completed review.
        for lane in REVIEW_LANES:
            for _, owner, _ in self._sites(ARCHON / f"workflows/{lane}.yaml"):
                self.assertEqual(owner, "round-pre",
                                 f"{lane}: reclaim owned by {owner}, not round-pre")

    def test_lite_api_overlay_is_the_source(self):
        # full-sdlc-api-lite.yaml is generated; patching it alone is undone by
        # the next derive.
        ov = ARCHON / "setup/lite/api/review-loop.round-pre.bash.sh"
        # Assert the INVOCATION, not just the path: a mention of the script in
        # an assignment is not a call, and grepping the name alone still
        # matches after the call itself is deleted.
        self.assertRegex(
            ov.read_text(),
            r'N=\$\(bash "\$R" "\$ARTIFACTS_DIR" "\$N" "round-" "review-summary\.json" ')


if __name__ == "__main__":
    unittest.main()


class ProofMustCarryAVerdict(unittest.TestCase):
    """The pattern argument, and the exact shape run 38d72218 round 3 produced."""

    def setUp(self):
        self.ad = Path(tempfile.mkdtemp())

    def _summary(self, n, body):
        d = self.ad / f"round-{n}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "review-summary.json").write_text(body, encoding="utf-8")

    def test_an_empty_verdict_is_not_proof(self):
        # Verbatim from round 3: the file is present and non-empty, and the
        # round still decided nothing.
        self._summary(3, '{"verdict": "", "residual_count": -1, "degraded": false}')
        out, err, rc = run(self.ad, 3, proof="review-summary.json", pattern=VERDICT_PATTERN)
        self.assertEqual(rc, 0)
        self.assertEqual(out, "2")
        self.assertIn("ROUND_RECLAIM=round-3", err)

    def test_a_real_verdict_is_proof(self):
        self._summary(3, '{"verdict": "Ready with fixes", "residual_count": -1, "degraded": false}')
        out, err, _ = run(self.ad, 3, proof="review-summary.json", pattern=VERDICT_PATTERN)
        self.assertEqual(out, "3")
        self.assertNotIn("ROUND_RECLAIM", err)

    def test_a_not_ready_verdict_is_still_proof(self):
        # The round is spent by deciding, not by deciding in our favour.
        self._summary(3, '{"verdict": "Not ready", "residual_count": 4, "degraded": false}')
        out, _, _ = run(self.ad, 3, proof="review-summary.json", pattern=VERDICT_PATTERN)
        self.assertEqual(out, "3")

    def test_without_a_pattern_presence_is_still_enough(self):
        # The argument is optional; callers that pass no pattern keep the old
        # contract rather than silently getting a stricter one.
        self._summary(3, '{"verdict": ""}')
        out, _, _ = run(self.ad, 3, proof="review-summary.json")
        self.assertEqual(out, "3")

    def test_every_review_lane_proves_the_round_by_its_verdict(self):
        import yaml

        def walk(nodes):
            for n in nodes or []:
                if not isinstance(n, dict):
                    continue
                yield n
                for key in ("loop_group", "body"):
                    v = n.get(key)
                    if isinstance(v, dict):
                        yield from walk(v.get("nodes"))
                    elif isinstance(v, list):
                        yield from walk(v)

        for lane in REVIEW_LANES:
            doc = yaml.safe_load((ARCHON / "workflows" / f"{lane}.yaml").read_text(encoding="utf-8"))
            pre = [n for n in walk(doc["nodes"])
                   if n.get("id") == "round-pre" and "round-reclaim.sh" in (n.get("bash") or "")]
            self.assertTrue(pre, f"{lane}: round-pre does not reclaim")
            bash = pre[0]["bash"]
            self.assertIn("review-summary.json", bash,
                          f"{lane}: reclaim still proves the round by a file name, not a verdict")
            self.assertIn('"verdict"', bash, f"{lane}: reclaim passes no verdict pattern")
