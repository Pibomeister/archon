#!/usr/bin/env python3
"""review-gate GATE_5: the envelope must describe the candidate the round opened on.

v1's GATE_4 compared a free-text `Scope:` string against `review-mode.txt`. That
was ambiguous by 789b2a1 -- the lane labels the bootstrap-head comparison
`delta`, so "delta" in an envelope no longer named one thing -- and it checked
the cheapest property: which slice the reviewer claimed to read. It could not see
the expensive failure, which is an envelope produced against a DIFFERENT
candidate being gated as this round's. A resume replays a review, a fixer commit
moves HEAD, a `feature-pin-amend` changes the approved plan; each of those makes
a complete, well-formed, entirely valid envelope describe something that is no
longer the thing about to be committed.

GATE_5 compares two mechanical values instead. `Input:` is the review identity --
sha256 over head, base, scope, and the contract, plan and allowlist digests -- so
any of those moving invalidates the envelope. `Head:` is the sha the round
recorded when it opened. Both come from `round-N/review-input.json`, which
`round-pre` writes before the reviewer starts.

There is no SKIP path. The lite lane overlays `round-pre` and writes
`review-input.json` with the same constituents, so "no input file" means the
round is unidentifiable, not unchecked.

The fixture files are written by hand rather than by calling `review-mode.py` or
`round-state.py`: the gate's contract is the FILES, and pinning it to a sibling's
script would test that script instead.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from nodes.extract import runnable_body

FIX = Path(__file__).resolve().parent / "nodes" / "fixtures" / "review-gate" / "round-1"
BODY = (FIX / "review-envelope.txt").read_text(encoding="utf-8")
FOOTER = (FIX / "review-footer.txt").read_text(encoding="utf-8")
INPUT = json.loads((FIX / "review-input.json").read_text(encoding="utf-8"))
REVIEW_ID = INPUT["id"]
HEAD = INPUT["review_head"]


def footer(review_id=REVIEW_ID, head=HEAD):
    """The fixture footer with either value swapped out."""
    return (FOOTER.replace(f"Input: {REVIEW_ID}", f"Input: {review_id}")
                  .replace(f"Head: {HEAD}", f"Head: {head}"))


def run_gate(workflow="full-sdlc-api", review_input=INPUT, envelope=None):
    """Run review-gate over an envelope, with `review-input.json` present unless
    `review_input` is None.

    A short `$review.output` forces the envelope-file fallback, exactly as a
    resume does, so the verdict comes from the fixture envelope. CE_REVIEW_ROOT
    points at an empty dir so no ce-code-review run on this host can be read as
    this round's.
    """
    art = Path(tempfile.mkdtemp(prefix="rs-"))
    rd = art / "round-1"
    rd.mkdir()
    (art / "round.txt").write_text("1\n")
    (rd / "review-envelope.txt").write_text(
        BODY + (FOOTER if envelope is None else envelope), encoding="utf-8")
    (rd / "pre-head.txt").write_text(HEAD + "\n")
    (rd / "prerun-dirs.txt").write_text("")
    if review_input is not None:
        (rd / "review-input.json").write_text(json.dumps(review_input), encoding="utf-8")
    ce_root = art / "ce-root"
    ce_root.mkdir()
    body = runnable_body(workflow, "review-gate", outputs={"review": "resumed"})
    env = dict(os.environ, ARTIFACTS_DIR=str(art), CE_REVIEW_ROOT=str(ce_root))
    p = subprocess.run(["bash", "-c", body], capture_output=True, text=True, env=env)
    shutil.rmtree(art, ignore_errors=True)
    return p


def gate_body(lane="full-sdlc-api"):
    return runnable_body(lane, "review-gate", outputs={"review": "x"})


def gate_5_is_wired():
    try:
        return "GATE_5_input_matches" in gate_body()
    except Exception:
        return False


# The behavioural cases drive a node body this worker does not own. Running
# seven identical "GATE_4 is still here" failures would bury the one fact worth
# reading, so the wiring itself is asserted ONCE, hard, by
# test_the_retired_gate_4_is_gone_from_every_lane below -- which is also the
# negative control that stops this skip from going quiet forever.
NEEDS_GATE_5 = unittest.skipUnless(
    gate_5_is_wired(),
    "review-gate still emits GATE_4; see test_the_retired_gate_4_is_gone_from_every_lane")


@NEEDS_GATE_5
class ReviewInputGate(unittest.TestCase):
    def test_a_matching_input_and_head_pass(self):
        p = run_gate()
        self.assertIn("GATE_5_input_matches=PASS", p.stdout, p.stdout + p.stderr)
        self.assertIn("REVIEW_GATE=PASS round=1", p.stdout, p.stdout + p.stderr)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_a_mismatched_input_id_fails(self):
        # The identity covers the contract, the approved plan and the allowlist.
        # An envelope carrying a different id reviewed a candidate whose plan or
        # contract has since changed -- valid work, about a different thing.
        p = run_gate(envelope=footer(review_id="0" * 64))
        self.assertIn("GATE_5_input_matches=FAIL", p.stdout, p.stdout + p.stderr)
        self.assertIn("REVIEW_INPUT=FAIL", p.stdout, p.stdout + p.stderr)
        self.assertNotIn("REVIEW_GATE=PASS", p.stdout)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)

    def test_a_mismatched_head_fails(self):
        # The id alone is not enough: a replayed envelope can carry the right id
        # and still have been written against a head the round has moved past.
        p = run_gate(envelope=footer(head="f" * 40))
        self.assertIn("GATE_5_input_matches=FAIL", p.stdout, p.stdout + p.stderr)
        self.assertIn("REVIEW_INPUT=FAIL", p.stdout, p.stdout + p.stderr)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)

    def test_an_envelope_with_no_footer_fails(self):
        # Fail-closed. The CE contract writes `Scope:` lines of its own, so the
        # absence of the round's footer must never read as "nothing to check".
        p = run_gate(envelope="")
        self.assertIn("GATE_5_input_matches=FAIL", p.stdout, p.stdout + p.stderr)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)

    def test_a_missing_review_input_fails_rather_than_skipping(self):
        # v1's GATE_4 had a SKIP state for the lite lane, which wrote no mode
        # file. The lite overlay now writes review-input.json with the same
        # constituents, so an absent file means the round cannot be identified.
        p = run_gate(review_input=None)
        self.assertIn("GATE_5_input_matches=FAIL", p.stdout, p.stdout + p.stderr)
        self.assertNotIn("GATE_5_input_matches=SKIP", p.stdout)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)

    def test_the_lite_lane_gates_on_the_same_body(self):
        p = run_gate(workflow="full-sdlc-api-lite")
        self.assertIn("GATE_5_input_matches=PASS", p.stdout, p.stdout + p.stderr)
        self.assertEqual(p.returncode, 0, p.stderr)

class ReviewInputGateIsWired(unittest.TestCase):
    """The one test that is never skipped.

    It is the negative control for the skip above: without it, a lane that never
    migrated would show seven green skips and nothing else, which is the exact
    shape of a test suite that cannot fail.
    """

    def test_the_retired_gate_4_is_gone_from_every_lane(self):
        # Leaving GATE_4 in place beside GATE_5 would let a lane keep the
        # ambiguous scope compare and still look migrated, and the SKIP path
        # would come back with it.
        for lane in ("full-sdlc-api", "full-sdlc-api-lite"):
            with self.subTest(lane=lane):
                body = gate_body(lane)
                self.assertNotIn("GATE_4_scope_matches_mode", body)
                self.assertIn("GATE_5_input_matches", body)


if __name__ == "__main__":
    unittest.main()
