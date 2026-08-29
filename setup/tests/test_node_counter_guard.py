"""Every node that advances a durable round counter must reject a non-integer
counter before `$((N+1))` and before building a path from it. plan-round-pre
and rca-round-pre carry the guard ("both files are hand-editable, so both are
untrusted input"); round-pre (3 lanes), deslop-recheck (2) and attempt-pre did
not: `round.txt = 1/../round-1` gave rc 0, a PASS-class `ROUND=1/../round-1`
line, a frozen counter and every later round clobbering round-1/ (final
architect review, 2026-08-29). Same class as the critic-gate guard."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from nodes.extract import runnable_body

SITES = [
    ("full-sdlc-api", "round-pre", "round.txt", "ROUND_PRE"),
    ("full-sdlc-web", "round-pre", "round.txt", "ROUND_PRE"),
    ("bugfix", "round-pre", "round.txt", "ROUND_PRE"),
    # deslop-recheck (both lanes) needs a worktree before it reads its counter;
    # its junk-counter cases live in test_node_stress.DeslopStress.
    ("bugfix", "attempt-pre", "fix-attempt.txt", "ATTEMPT_PRE"),
]


def run_site(workflow, node, counter_file, value):
    art = Path(tempfile.mkdtemp(prefix="cg-"))
    (art / counter_file).write_text(value)
    body = runnable_body(workflow, node, outputs={})
    p = subprocess.run(["bash", "-c", body], capture_output=True, text=True,
                       env=dict(os.environ, ARTIFACTS_DIR=str(art)))
    after = (art / counter_file).read_text()
    shutil.rmtree(art, ignore_errors=True)
    return p, after


class CounterGuard(unittest.TestCase):
    def test_junk_counter_fails_closed_and_does_not_advance(self):
        for workflow, node, cf, token in SITES:
            for bad in ("1/../round-1", "one", " 1"):
                with self.subTest(workflow=workflow, node=node, value=bad):
                    p, after = run_site(workflow, node, cf, bad)
                    self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                    self.assertIn(f"{token}=FAIL {cf} is not an integer: [{bad}]", p.stdout + p.stderr)
                    self.assertEqual(after, bad, "counter must not be rewritten on the failure path")


if __name__ == "__main__":
    unittest.main()
