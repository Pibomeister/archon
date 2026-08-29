"""Every node that READS a durable round counter must reject a non-integer
counter before it builds a path from it.

ba249eb guarded the WRITERS (round-pre, attempt-pre, plan-round-pre,
rca-round-pre). The readers were left unguarded: `round.txt = 1/../round-1`
made `converge` tee its log into the PREVIOUS round's directory and read that
round's review-summary.json as the current verdict, at rc 0 with a PASS-class
line (final architect review, 2026-08-29). Same class as the writer guard.

Sites reachable with a counter file and nothing else live here; sites that
need a worktree or params.json before they reach the counter live in
test_node_stress.py (DeslopStress / ExitGateCounter).
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from nodes.extract import runnable_body

# workflow, node, counter file, typed token, the directory the unguarded body
# would have written into, node outputs needed to make the body runnable.
SITES = [
    ("full-sdlc-api", "review-gate", "round.txt", "REVIEW_GATE", "round-1", {"review": ""}),
    ("full-sdlc-api", "converge", "round.txt", "CONVERGE", "round-1", {}),
    ("full-sdlc-web", "review-gate", "round.txt", "REVIEW_GATE", "round-1", {"review": ""}),
    ("full-sdlc-web", "converge", "round.txt", "CONVERGE", "round-1", {}),
    ("bugfix", "review-gate", "round.txt", "REVIEW_GATE", "round-1", {"review": ""}),
    ("bugfix", "converge", "round.txt", "CONVERGE", "round-1", {}),
    ("bugfix", "green-check", "fix-attempt.txt", "GREEN_CHECK", "attempt-1", {}),
    ("bugfix", "fix-converge", "fix-attempt.txt", "GREEN_GATE", "attempt-1", {}),
    ("full-sdlc-api", "report", "round.txt", "REPORT", "round-1", {}),
    ("bugfix", "report", "round.txt", "REPORT", "round-1", {}),
]

# `report` is `always_run` + `trigger_rule: all_done`: it is the node that
# reports on a run which stopped before review-loop ever wrote round.txt, and
# its existing `test -n "$N" &&` says so. An ABSENT counter is a legitimate
# state there, so only a present-but-junk counter is a stop. Every other reader
# treats '' as junk — a reader with no counter is a bug.
EMPTY_IS_LEGITIMATE = {("full-sdlc-api", "report"), ("bugfix", "report")}

# The traversal value is built from the site's OWN prefix, so it resolves to
# a sibling round/attempt directory that really exists in a live run.
JUNK_TAIL = ("one", " 1")


def run_site(workflow, node, counter_file, shadow, value, outputs):
    """Minimal fixture: the counter file, plus an EMPTY directory named as the
    round the traversal resolves to. An unguarded body writes its log/envelope
    into that directory, so its contents afterwards are the negative control."""
    art = Path(tempfile.mkdtemp(prefix="cr-"))
    (art / counter_file).write_text(value)
    (art / shadow).mkdir()
    body = runnable_body(workflow, node, outputs=outputs)
    p = subprocess.run(["bash", "-c", body], capture_output=True, text=True,
                       env=dict(os.environ, ARTIFACTS_DIR=str(art)))
    listing = sorted(x.name for x in art.iterdir())
    shadow_listing = sorted(x.name for x in (art / shadow).iterdir())
    shutil.rmtree(art, ignore_errors=True)
    return p, listing, shadow_listing


class CounterReaders(unittest.TestCase):
    def test_junk_counter_fails_closed_and_writes_nothing(self):
        for workflow, node, cf, token, shadow, outs in SITES:
            for bad in (f"1/../{shadow}",) + JUNK_TAIL:
                with self.subTest(workflow=workflow, node=node, value=bad):
                    p, listing, shadow_listing = run_site(
                        workflow, node, cf, shadow, bad, outs)
                    out = p.stdout + p.stderr
                    self.assertEqual(p.returncode, 1, out)
                    self.assertIn(f"{token}=FAIL {cf} is not an integer: [{bad}]", out)
                    self.assertEqual(listing, sorted([cf, shadow]),
                                     "reader must not create artifacts from a junk counter")
                    self.assertEqual(shadow_listing, [],
                                     f"reader wrote into {shadow}/ built from a junk counter")

    def test_empty_counter_is_a_stop_except_in_report(self):
        for workflow, node, cf, token, shadow, outs in SITES:
            with self.subTest(workflow=workflow, node=node):
                p, listing, shadow_listing = run_site(
                    workflow, node, cf, shadow, "", outs)
                out = p.stdout + p.stderr
                if (workflow, node) in EMPTY_IS_LEGITIMATE:
                    self.assertEqual(p.returncode, 0, out)
                    self.assertNotIn("=FAIL", out)
                else:
                    self.assertEqual(p.returncode, 1, out)
                    self.assertIn(f"{token}=FAIL {cf} is not an integer: []", out)
                self.assertEqual(shadow_listing, [])


if __name__ == "__main__":
    unittest.main()
