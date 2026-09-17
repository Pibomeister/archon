#!/usr/bin/env python3
"""review-gate's half of GATE_5: that it delegates, and that GATE_4 is gone.

v1's GATE_4 compared a free-text `Scope:` string against `review-mode.txt`. That
was ambiguous by 789b2a1 -- the lane labels the bootstrap-head comparison
`delta`, so "delta" in an envelope no longer named one thing -- and it checked
the cheapest property: which slice the reviewer claimed to read. It could not see
the expensive failure, which is an envelope produced against a DIFFERENT
candidate being gated as this round's.

GATE_5 compares two mechanical values instead, and `round-state.py gate` is what
compares them. The BEHAVIOUR is therefore tested there, in
test_round_state.InputGate, which has the git worktree and the round files for
free: matching id and head pass, a mismatched id fails, a mismatched head fails,
an envelope with no footer fails, and a missing review-input.json fails rather
than skipping.

What is left here is the node's own half, which that file cannot see: this lane
calls the helper at all, both lanes run the same body, and the retired gate is
not still sitting beside the new one. None of it needs a fixture, and none of it
can skip.

This file used to drive the gate end to end behind a `skipUnless` keyed on the
literal `GATE_5_input_matches` appearing in the node body. Once review-gate
delegated, that literal moved into round-state.py and every case skipped green
forever -- six tests that could not fail, guarded by a predicate that had
quietly become "is this string still in a file it is no longer in".
"""
import unittest

from nodes.extract import runnable_body

LANES = ("full-sdlc-api", "full-sdlc-api-lite")


def gate_body(lane):
    return runnable_body(lane, "review-gate", outputs={"review": "x"})


class ReviewInputGateIsWired(unittest.TestCase):
    def test_every_v2_lane_delegates_the_identity_gate(self):
        for lane in LANES:
            with self.subTest(lane=lane):
                self.assertIn('round-state.py gate "$ARTIFACTS_DIR"', gate_body(lane))

    def test_the_retired_gate_4_is_gone_from_every_lane(self):
        # Leaving GATE_4 beside GATE_5 would let a lane keep the ambiguous scope
        # compare and still look migrated, and its SKIP path would come back
        # with it -- the lite lane is exactly where that path used to live.
        for lane in LANES:
            with self.subTest(lane=lane):
                body = gate_body(lane)
                self.assertNotIn("GATE_4_scope_matches_mode", body)
                self.assertNotIn("REVIEW_SCOPE=FAIL", body)

    def test_the_lite_lane_gates_on_the_same_body(self):
        # The lite lane overlays round-pre, not review-gate. If it ever grew its
        # own gate body, "the lite lane is covered because it runs the same
        # code" would stop being true and nothing else would notice.
        self.assertEqual(gate_body("full-sdlc-api-lite"), gate_body("full-sdlc-api"))

    def test_a_gate_failure_is_recorded_through_the_helper(self):
        # The node's own G1/G2/G3 failures call `gate --fail`, which writes
        # gate.txt and DELETES review.ok. Exiting directly instead would leave a
        # previous attempt's authorization alive for the attempt that replaced it.
        for lane in LANES:
            with self.subTest(lane=lane):
                self.assertIn('gate "$ARTIFACTS_DIR" --fail', gate_body(lane))


if __name__ == "__main__":
    unittest.main()
