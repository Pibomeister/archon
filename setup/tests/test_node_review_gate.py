"""review-gate (full-sdlc-api + full-sdlc-web + bugfix) against the live round-2 envelope from
run d3aa3b55: the parser returns `VERDICT=Ready with fixes`; the gate evals it.
An unquoted assignment makes `eval` execute the word `with`, leaves ENV_VERDICT
unset, and `set -u` aborts the node before any typed line. Observed live
2026-08-28 (round 2 of d3aa3b55)."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from nodes.extract import runnable_body

FIX = Path(__file__).resolve().parent / "nodes" / "fixtures" / "review-gate"
ENVELOPE = FIX / "round-1" / "review-envelope.txt"


def run_gate(workflow, review_output="resumed"):
    """Run the gate with no new ce-code-review dir (prerun == post) so the
    verdict can only come from the envelope. A short $review.output forces the
    file fallback, mirroring a resume."""
    art = Path(tempfile.mkdtemp(prefix="rg-"))
    rd = art / "round-1"
    rd.mkdir()
    (art / "round.txt").write_text("1\n")
    shutil.copy(ENVELOPE, rd / "review-envelope.txt")
    (rd / "pre-head.txt").write_text("0" * 40 + "\n")
    # Hermetic: the gate scans $CE_REVIEW_ROOT (default: the shared /tmp root);
    # point it at an empty per-test dir so no ce-code-review run on this host
    # can be mistaken for this round's, and the verdict must come from the envelope.
    ce_root = art / "ce-root"
    ce_root.mkdir()
    (rd / "prerun-dirs.txt").write_text("")
    body = runnable_body(workflow, "review-gate", outputs={"review": review_output})
    env = dict(os.environ, ARTIFACTS_DIR=str(art), CE_REVIEW_ROOT=str(ce_root))
    p = subprocess.run(["bash", "-c", body], capture_output=True, text=True, env=env)
    shutil.rmtree(art, ignore_errors=True)
    return p


class ReviewGateVerdictQuoting(unittest.TestCase):
    # full-sdlc-api's gate hands off to round-state.py after GATE_3, and that
    # needs a real candidate worktree plus a review-input.json whose id the
    # helper minted. Seeding all of it here would test the identity gate, which
    # test_round_state.py already covers and which the injection sequence
    # exercises seventeen times against real node bodies. The property THIS file
    # exists for is the quoting one -- an unquoted assignment makes `eval`
    # execute the word `with`, leaves ENV_VERDICT unset, and `set -u` aborts
    # before any typed line -- and it is fully decided by the GATE_3 line, which
    # is printed before the handoff. So the api lane asserts to there and stops.
    DELEGATES = ("full-sdlc-api",)

    def check(self, workflow):
        p = run_gate(workflow)
        self.assertIn("GATE_3_verdict_in_enum=PASS verdict=[Ready with fixes] source=envelope", p.stdout, p.stdout + p.stderr)
        self.assertNotIn("unbound variable", p.stderr)
        if workflow in self.DELEGATES:
            return
        self.assertIn("REVIEW_GATE=PASS round=1", p.stdout, p.stdout + p.stderr)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_full_sdlc_api(self):
        self.check("full-sdlc-api")

    def test_full_sdlc_web(self):
        self.check("full-sdlc-web")

    def test_bugfix(self):
        self.check("bugfix")


class ReviewGateKeepsTheMarkedEnvelope(unittest.TestCase):
    """mode:ce reviewers write the envelope and `mark review-done`; their session
    output is narration. Run 44483cda (2026-09-17): the gate copied that
    narration over the file, erased every footer line, and GATE_5 failed on
    envelope_input=[]. The node output may only fill a MISSING envelope."""

    NARRATION = ("Production churn is 52 lines, so I capped the conditional personas at 3. "
                 "Now dispatching the persona agents in parallel. Now marking review-done. "
                 "Review complete. Verdict: **Not ready**. Full findings are in the envelope.")

    def test_substantive_narration_does_not_overwrite_the_envelope_on_disk(self):
        self.assertGreaterEqual(len(self.NARRATION), 100)
        p = run_gate("full-sdlc-api", review_output=self.NARRATION)
        self.assertIn("GATE_3_verdict_in_enum=PASS verdict=[Ready with fixes] source=envelope",
                      p.stdout, p.stdout + p.stderr)

    def test_web_substantive_narration_does_not_overwrite_the_envelope_on_disk(self):
        self.assertGreaterEqual(len(self.NARRATION), 100)
        p = run_gate("full-sdlc-web", review_output=self.NARRATION)
        self.assertIn("GATE_3_verdict_in_enum=PASS verdict=[Ready with fixes] source=envelope",
                      p.stdout, p.stdout + p.stderr)

    def test_bugfix_substantive_narration_does_not_overwrite_the_envelope_on_disk(self):
        self.assertGreaterEqual(len(self.NARRATION), 100)
        p = run_gate("bugfix", review_output=self.NARRATION)
        self.assertIn("GATE_3_verdict_in_enum=PASS verdict=[Ready with fixes] source=envelope",
                      p.stdout, p.stdout + p.stderr)


class DocreviewGateKeepsTheMarkedEnvelope(unittest.TestCase):
    """Same fill-if-missing rule as review-gate: session narration must not
    replace a docreview envelope already on disk."""

    NARRATION = ReviewGateKeepsTheMarkedEnvelope.NARRATION

    def test_api_docreview_gate_does_not_clobber_a_marked_envelope(self):
        self.assertGreaterEqual(len(self.NARRATION), 100)
        art = Path(tempfile.mkdtemp(prefix="drg-"))
        try:
            marked = "Review complete\nThis envelope was marked on disk.\n"
            (art / "docreview-envelope.txt").write_text(marked, encoding="utf-8")
            (art / "plan.md").write_text("# plan\n", encoding="utf-8")
            (art / "plan.post-critic.md").write_text("# plan\n", encoding="utf-8")
            body = runnable_body("full-sdlc-api", "docreview-gate",
                                 outputs={"docreview": self.NARRATION})
            env = dict(os.environ, ARTIFACTS_DIR=str(art))
            p = subprocess.run(["bash", "-c", body], capture_output=True, text=True, env=env)
            self.assertEqual((art / "docreview-envelope.txt").read_text(encoding="utf-8"), marked)
            self.assertIn("DOCREVIEW_GATE=PASS", p.stdout, p.stdout + p.stderr)
            self.assertEqual(p.returncode, 0, p.stderr)
        finally:
            shutil.rmtree(art, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
