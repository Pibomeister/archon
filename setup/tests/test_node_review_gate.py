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
    def check(self, workflow):
        p = run_gate(workflow)
        self.assertIn("GATE_3_verdict_in_enum=PASS verdict=[Ready with fixes] source=envelope", p.stdout, p.stdout + p.stderr)
        self.assertIn("REVIEW_GATE=PASS round=1", p.stdout, p.stdout + p.stderr)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("unbound variable", p.stderr)

    def test_full_sdlc_api(self):
        self.check("full-sdlc-api")

    def test_full_sdlc_web(self):
        self.check("full-sdlc-web")

    def test_bugfix(self):
        self.check("bugfix")


if __name__ == "__main__":
    unittest.main()
