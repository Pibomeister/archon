#!/usr/bin/env python3
"""review-loop/round-pre: the durable round cap is checked BEFORE a round is
spent, in both parents and both lite lanes (retained bytes), with
accept-residuals.txt as the human bypass. Mirrors plan-round-pre's doctrine."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ARCHON = Path(__file__).resolve().parent.parent.parent
LANES = {"full-sdlc-api.yaml": 4, "bugfix.yaml": 2, "full-sdlc-web.yaml": 4, "full-sdlc-api-lite.yaml": 1, "bugfix-lite.yaml": 2}
# The v2 lanes keep the cap in the node and hand everything else to
# round-state.py, which changed two things the assertions below have to
# distinguish. (1) The counter advances only on a `progressed` decision, so
# round-pre no longer prints `ROUND=<N+1>` on entry -- its stdout is the bare
# JSON line the sibling `review` node's `when:` parses. (2) The reclaim is
# consulted but its answer is NOT written back to round.txt, because a counter
# that never over-advances has nothing to give back. The CAP itself is
# unchanged on every lane, which is what this file is really about.
V2 = {"full-sdlc-api.yaml", "full-sdlc-api-lite.yaml"}


def round_pre(workflow):
    doc = yaml.safe_load((ARCHON / "workflows" / workflow).read_text(encoding="utf-8"))
    loop = next(n for n in doc["nodes"] if n["id"] == "review-loop")
    return next(b for b in loop["loop_group"]["nodes"] if b["id"] == "round-pre")["bash"]


class RoundPreCap(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ad = self.tmp / "ad"
        self.ad.mkdir()
        wt = self.tmp / "wt"
        wt.mkdir()
        subprocess.run("git init -q && git config user.email t@t && git config user.name t && echo a > a && git add . && git commit -qm base", cwd=wt, shell=True, check=True, capture_output=True)
        (self.ad / "params.json").write_text('{"spec": "/x.md", "slug": "x", "branch": "archon/x", "worktree": "%s"}' % wt)
        # The v2 lanes' round-pre hands off to round-state.py once the cap
        # passes, and that refuses to open a round without an allowlist (an
        # absent one used to read as "allow nothing", which empties the tree).
        # A bare JSON array is the shape check-scope.py and every lane write.
        (self.ad / "files-allowlist.json").write_text('["a"]')
        (self.ad / "bootstrap-head.txt").write_text(
            subprocess.run("git rev-parse HEAD", cwd=wt, shell=True,
                           capture_output=True, encoding="utf-8").stdout)
        (self.ad / "plan.md").write_text("# plan\n")

    def run_pre(self, workflow, round_txt, cap=None, accept=False, proven=None, reset=True):
        # round-pre reclaims a round that decided nothing: a review killed by
        # its cost cap, or one that returned while its personas were still
        # running, was BILLED and not spent. The proof is a verdict, not a file
        # name -- run 38d72218 round 3 left a 22KB envelope and {"verdict": ""}.
        # A counter of N therefore only means N rounds happened if each left a
        # verdict. Without them these cap fixtures would exercise the reclaim
        # path while claiming to test the cap. `proven` defaults to the whole
        # counter; pass fewer to leave the tail unproven.
        proven = round_txt if proven is None else proven
        if reset:
            for d in self.ad.glob("round-*"):
                shutil.rmtree(d, ignore_errors=True)
            (self.ad / "round-reclaimed.txt").unlink(missing_ok=True)
        for k in range(1, proven + 1):
            d = self.ad / f"round-{k}"
            d.mkdir(parents=True, exist_ok=True)
            (d / "review-summary.json").write_text(
                '{"verdict": "Ready to merge", "residual_count": 0, "degraded": false}',
                encoding="utf-8")
        (self.ad / "round.txt").write_text(f"{round_txt}\n")
        if cap is not None:
            (self.ad / "round-cap.txt").write_text(f"{cap}\n")
        elif (self.ad / "round-cap.txt").exists():
            os.remove(self.ad / "round-cap.txt")
        if accept:
            (self.ad / "accept-residuals.txt").write_text("human\n")
        elif (self.ad / "accept-residuals.txt").exists():
            os.remove(self.ad / "accept-residuals.txt")
        script = round_pre(workflow)
        return subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            encoding="utf-8",
            env={
                **os.environ,
                "ARTIFACTS_DIR": str(self.ad),
                "ARCHON_LAYER": str(ARCHON),
                "PROJECT_ROOT": str(self.tmp),
            },
        )

    def test_under_cap_proceeds(self):
        for wf, default in LANES.items():
            r = self.run_pre(wf, default - 1)
            self.assertEqual(r.returncode, 0, wf + r.stdout + r.stderr)
            if wf in V2:
                # Stdout is the decision, and the contract this asserts is that it
                # parses as ONE bare JSON object carrying `review` -- an
                # unparseable `when:` silently skips the reviewer while the run
                # still reports SUCCESS. Which round number it names is
                # round-state.py's counter logic and is tested there; it legally
                # differs between staying on an open round and opening the next.
                decision = json.loads(r.stdout.strip())
                self.assertIn(decision["review"], ("run", "reuse"), wf)
                self.assertGreaterEqual(decision["round"], 1, wf)
            else:
                self.assertIn(f"ROUND={default}", r.stdout)

    def test_at_default_cap_stops_before_spending(self):
        for wf, default in LANES.items():
            r = self.run_pre(wf, default)
            self.assertEqual(r.returncode, 1, wf + r.stdout + r.stderr)
            # v2 lanes send the stop to stderr (stdout is the JSON decision on every
            # path); the v1 lanes still tee it to stdout. Either stream reaches the
            # operator and node-<id>.out; stdout PURITY is asserted separately, in
            # test_node_review_loop_shape.
            self.assertIn(f"ROUND_CAP_REACHED round={default} cap={default}", r.stdout + r.stderr)
            self.assertEqual((self.ad / "round.txt").read_text().strip(), str(default), "round.txt must not be incremented")
            self.assertNotIn("ROUND_RECLAIM", r.stderr, wf + ": proven rounds must never be reclaimed")

    def test_unproven_last_round_is_reclaimed_once_then_the_cap_stops(self):
        # A round whose review died before writing an envelope is given back
        # exactly once per run. The second unproven round means something is
        # durably wrong, not unlucky, so the cap holds and the human gate is
        # reached with the run's rounds actually spent on reviews.
        for wf, default in LANES.items():
            if wf in V2:
                # v2 does not reclaim. round-state.py consults the script for its
                # typed line and its ledger but never writes the answer back,
                # because its counter advances only on a progressed decision and
                # so cannot over-count a round that died before it was spent. The
                # cap therefore holds on the FIRST unproven round rather than the
                # second -- stricter, and for a reason that no longer applies.
                r = self.run_pre(wf, default, proven=default - 1)
                self.assertEqual(r.returncode, 1, wf + r.stdout + r.stderr)
                self.assertIn(f"ROUND_CAP_REACHED round={default} cap={default}", r.stdout + r.stderr, wf)
                continue
            r = self.run_pre(wf, default, proven=default - 1)
            self.assertEqual(r.returncode, 0, wf + r.stdout + r.stderr)
            self.assertIn(f"ROUND_RECLAIM=round-{default}", r.stderr, wf)
            self.assertIn(f"ROUND={default}", r.stdout, wf)
            r = self.run_pre(wf, default, proven=default - 1, reset=False)
            self.assertEqual(r.returncode, 1, wf + r.stdout + r.stderr)
            self.assertIn(f"ROUND_CAP_REACHED round={default} cap={default}", r.stdout + r.stderr, wf)

    def test_acceptance_buys_the_cap_round_and_nothing_past_it(self):
        # Chain 1f7a896a: acceptance plus a deferred P1 ran to round 6. The cap
        # round itself is spent with the file present; the one after is not.
        for wf, default in LANES.items():
            if wf not in V2:
                continue
            r = self.run_pre(wf, default, accept=True)
            self.assertNotIn("ROUND_CAP", r.stdout + r.stderr, wf)
            r = self.run_pre(wf, default + 1, accept=True)
            self.assertEqual(r.returncode, 1, wf + r.stdout + r.stderr)
            self.assertIn(f"ROUND_CAP_EXCEEDED round={default + 1} cap={default}", r.stdout + r.stderr, wf)

    def test_explicit_cap_file_wins(self):
        for wf in LANES:
            r = self.run_pre(wf, 1, cap=1)
            self.assertEqual(r.returncode, 1, wf + r.stdout + r.stderr)
            r = self.run_pre(wf, 1, cap=3)
            self.assertEqual(r.returncode, 0, wf + r.stdout + r.stderr)

    def test_accept_residuals_bypasses_cap(self):
        for wf, default in LANES.items():
            r = self.run_pre(wf, default, accept=True)
            self.assertEqual(r.returncode, 0, wf + r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
