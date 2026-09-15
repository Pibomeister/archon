#!/usr/bin/env python3
"""review-gate GATE_4: the reviewed scope must be the scope the round asked for.

`round-pre` writes `round-N/review-mode.txt` (full|delta) and
`round-N/review-base.txt` (a sha) before the reviewer session starts; the
reviewer opens its envelope with a matching `Scope:` line. A delta round that
re-reviewed everything, or a full round that reviewed a slice, costs the same
budget and proves something else, so the gate compares the two and fails closed.

The mode files are written directly here rather than by calling `review-mode.py`
— the gate's contract is the FILES, and pinning it to a sibling's script would
test that script instead.

SKIP is the third state and is not a loophole: the lite lanes overlay
`round-pre` (setup/lite/api/review-loop.round-pre.bash.sh) and never write a
mode file, while inheriting this gate body verbatim, so "no mode file" has to
mean "this lane does not do delta review" rather than "unchecked".
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from nodes.extract import runnable_body

FIX = Path(__file__).resolve().parent / "nodes" / "fixtures" / "review-gate"
ENVELOPE = (FIX / "round-1" / "review-envelope.txt").read_text(encoding="utf-8")
BASE_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


def run_gate(workflow="full-sdlc-api", mode=None, scope_line=None, base=BASE_SHA):
    """Run review-gate over an envelope whose first line is `scope_line`.

    A short `$review.output` forces the envelope-file fallback, exactly as a
    resume does, so the verdict comes from the fixture envelope. CE_REVIEW_ROOT
    points at an empty dir so no ce-code-review run on this host can be read as
    this round's.
    """
    art = Path(tempfile.mkdtemp(prefix="rs-"))
    rd = art / "round-1"
    rd.mkdir()
    (art / "round.txt").write_text("1\n")
    body_text = ENVELOPE if scope_line is None else scope_line + "\n" + ENVELOPE
    (rd / "review-envelope.txt").write_text(body_text, encoding="utf-8")
    (rd / "pre-head.txt").write_text("0" * 40 + "\n")
    (rd / "prerun-dirs.txt").write_text("")
    if mode is not None:
        (rd / "review-mode.txt").write_text(mode + "\n")
        (rd / "review-base.txt").write_text(base + "\n")
    ce_root = art / "ce-root"
    ce_root.mkdir()
    body = runnable_body(workflow, "review-gate", outputs={"review": "resumed"})
    env = dict(os.environ, ARTIFACTS_DIR=str(art), CE_REVIEW_ROOT=str(ce_root))
    p = subprocess.run(["bash", "-c", body], capture_output=True, text=True, env=env)
    shutil.rmtree(art, ignore_errors=True)
    return p


class ReviewScopeGate(unittest.TestCase):
    def test_full_mode_with_a_full_envelope_passes(self):
        p = run_gate(mode="full", scope_line="Scope: full")
        self.assertIn("GATE_4_scope_matches_mode=PASS", p.stdout, p.stdout + p.stderr)
        self.assertIn("REVIEW_GATE=PASS round=1", p.stdout, p.stdout + p.stderr)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_delta_mode_with_a_delta_envelope_passes(self):
        p = run_gate(mode="delta", scope_line=f"Scope: delta base={BASE_SHA}")
        self.assertIn("GATE_4_scope_matches_mode=PASS", p.stdout, p.stdout + p.stderr)
        self.assertIn("REVIEW_GATE=PASS round=1", p.stdout, p.stdout + p.stderr)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_delta_mode_with_a_full_envelope_fails(self):
        p = run_gate(mode="delta", scope_line="Scope: full")
        self.assertIn("GATE_4_scope_matches_mode=FAIL", p.stdout, p.stdout + p.stderr)
        self.assertIn("REVIEW_SCOPE=FAIL mode=delta", p.stdout, p.stdout + p.stderr)
        self.assertNotIn("REVIEW_GATE=PASS", p.stdout)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)

    def test_delta_mode_over_the_wrong_base_fails(self):
        # The word "delta" is not the claim; the base is. An envelope that
        # reviewed a delta against some other sha reviewed the wrong diff.
        p = run_gate(mode="delta", scope_line="Scope: delta base=" + "f" * 40)
        self.assertIn("GATE_4_scope_matches_mode=FAIL", p.stdout, p.stdout + p.stderr)
        self.assertIn("REVIEW_SCOPE=FAIL mode=delta", p.stdout, p.stdout + p.stderr)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)

    def test_an_envelope_with_no_scope_line_fails(self):
        # Fail-closed: the CE envelope carries a free-text `Scope:` line of its
        # own, so "some Scope line exists" is never evidence the round was sized.
        p = run_gate(mode="full", scope_line=None)
        self.assertIn("GATE_4_scope_matches_mode=FAIL", p.stdout, p.stdout + p.stderr)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)

    def test_no_mode_file_skips(self):
        p = run_gate(mode=None, scope_line=None)
        self.assertIn("GATE_4_scope_matches_mode=SKIP", p.stdout, p.stdout + p.stderr)
        self.assertIn("REVIEW_GATE=PASS round=1", p.stdout, p.stdout + p.stderr)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_the_lite_lane_inherits_the_gate_and_skips(self):
        # full-sdlc-api-lite overlays round-pre but inherits this body, so the
        # SKIP branch is the lite lane's whole experience of GATE_4.
        p = run_gate(workflow="full-sdlc-api-lite", mode=None, scope_line=None)
        self.assertIn("GATE_4_scope_matches_mode=SKIP", p.stdout, p.stdout + p.stderr)
        self.assertEqual(p.returncode, 0, p.stderr)


if __name__ == "__main__":
    unittest.main()
