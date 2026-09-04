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
reclaim, its bound (one per round number, so a node that dies every time still
walks the counter to the cap), and its placement -- it belongs in round-pre
ahead of the cap check, and must never touch converge, where N drives the
verdict."""
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ARCHON = Path(__file__).resolve().parents[2]
SCRIPT = ARCHON / "setup/round-reclaim.sh"
REVIEW_LANES = ["bugfix", "bugfix-lite", "full-sdlc-api", "full-sdlc-web",
                "full-sdlc-api-lite"]


def run(ad, n, prefix="round-", proof="review-envelope.txt"):
    r = subprocess.run(["bash", str(SCRIPT), str(ad), str(n), prefix, proof],
                       capture_output=True, encoding="utf-8")
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

    def test_a_second_unproven_round_is_not_reclaimed(self):
        # The bound is per RUN, not per round number: two unproven rounds mean
        # something is durably wrong, and buying a third attempt for every
        # round number would let a persistently dying review spend 2x the cap.
        self._round(2); self._round(3)
        self.assertEqual(run(self.ad, 2)[0], "1")
        self.assertEqual(run(self.ad, 3)[0], "3")

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
            r'N=\$\(bash "\$R" "\$ARTIFACTS_DIR" "\$N" "round-" "review-envelope\.txt"\)')


if __name__ == "__main__":
    unittest.main()
