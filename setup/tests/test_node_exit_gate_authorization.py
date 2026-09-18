#!/usr/bin/env python3
"""exit-gate authorizes on review.ok and fixer.ok, not on a readable verdict.

The hole: `review-summary.json` is written by review-gate BEFORE its final
checks and rewritten by `ledger.py` on every envelope merge, so a Ready verdict
sits on disk for a round that was never gated. exit-gate read only that verdict,
so a resume that walked past converge's stop reached ship on an unauthorized
round -- the last gate before the candidate ships, deciding on the one artifact
that cannot speak to authorization.

`round-state.py exit-check` is the check; this is that it is WIRED, ahead of
everything an operator can waive. accept-residuals waives the verdict and the
fixer result. It has never waived whether a gate ran, and the ordering here is
what keeps that true.
"""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ARCHON = Path(__file__).resolve().parents[2]
HELPER = ARCHON / "setup/round-state.py"
V2 = ("full-sdlc-api", "full-sdlc-api-lite", "full-sdlc-api-codex")


def exit_gate(lane):
    doc = yaml.safe_load((ARCHON / "workflows" / f"{lane}.yaml").read_text(encoding="utf-8"))
    return next(n["bash"] for n in doc["nodes"] if n["id"] == "exit-gate")


class ExitGateAuthorization(unittest.TestCase):
    def test_every_v2_lane_calls_exit_check(self):
        for lane in V2:
            with self.subTest(lane=lane):
                self.assertIn('round-state.py exit-check "$ARTIFACTS_DIR"', exit_gate(lane))

    def test_it_runs_before_anything_an_operator_can_waive(self):
        # accept-residuals waives the verdict and the fixer result; it does not
        # waive whether a gate ran. If the call moved inside or below that
        # branch, the bypass would be back and every other test here would
        # still pass.
        for lane in V2:
            with self.subTest(lane=lane):
                body = exit_gate(lane)
                self.assertLess(body.index('exit-check "$ARTIFACTS_DIR"'),
                                body.index('accept-residuals.txt'))
                # The VERDICT READ, not any mention: the comment above the call names
                # review-summary.json too, and indexing on that passed for the
                # wrong reason until this test failed on it.
                self.assertLess(body.index('exit-check "$ARTIFACTS_DIR"'),
                                body.index("['verdict']"))

    def test_an_unauthorized_round_fails_the_gate(self):
        """The negative control the verifier asked for: remove review.ok."""
        if not HELPER.is_file():
            self.skipTest("round-state.py is not in this tree yet")
        for present, expect_rc in ((True, 0), (False, 1)):
            with self.subTest(review_ok=present):
                tmp = Path(tempfile.mkdtemp(prefix="exitauth-"))
                ad, wt = tmp / "ad", tmp / "wt"
                ad.mkdir(); wt.mkdir()
                subprocess.run(
                    "git init -q && git config user.email t@t && git config user.name t"
                    " && echo a > a && git add . && git commit -qm base",
                    cwd=wt, shell=True, check=True, capture_output=True)
                head = subprocess.run("git rev-parse HEAD", cwd=wt, shell=True,
                                      capture_output=True, encoding="utf-8").stdout.strip()
                (ad / "params.json").write_text(json.dumps(
                    {"spec": str(ad / "s.md"), "slug": "x", "branch": "archon/x",
                     "worktree": str(wt)}))
                (ad / "s.md").write_text("# spec\n")
                (ad / "files-allowlist.json").write_text('["a"]')
                (ad / "bootstrap-head.txt").write_text(head + "\n")
                (ad / "plan.md").write_text("# plan\n")
                env = {**os.environ, "ARTIFACTS_DIR": str(ad)}
                # Open the round through the helper so review-input.json carries
                # an identity this test never has to recompute.
                subprocess.run(["python3", str(HELPER), "pre", str(ad)],
                               capture_output=True, env=env)
                n = (ad / "round.txt").read_text().strip()
                rd = ad / f"round-{n}"
                record = json.loads((rd / "review-input.json").read_text())
                # authorization() requires gate.txt PASS as well as review.ok.
                (rd / "gate.txt").write_text("PASS\n")
                if present:
                    (rd / "review.ok").write_text(json.dumps(
                        {"gen": record["attempt"], "id": record["id"], "guard": "PASS"}))
                (rd / "fixer.ok").write_text(json.dumps(
                    {"attempt": record["attempt"], "review_id": record["id"],
                     "review_gen": record["attempt"], "head": head,
                     "tree": "", "committed": False}))
                r = subprocess.run(["python3", str(HELPER), "exit-check", str(ad)],
                                   capture_output=True, encoding="utf-8", env=env)
                if expect_rc:
                    self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                    self.assertIn("EXIT_GATE=FAIL", r.stdout)
                else:
                    self.assertNotIn("REVIEW_UNAUTHORIZED", r.stdout)


if __name__ == "__main__":
    unittest.main()
