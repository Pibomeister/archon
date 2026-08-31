#!/usr/bin/env python3
"""review-loop/round-pre: the durable round cap is checked BEFORE a round is
spent, in both parents and both lite lanes (retained bytes), with
accept-residuals.txt as the human bypass. Mirrors plan-round-pre's doctrine."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ARCHON = Path(__file__).resolve().parent.parent.parent
LANES = {"full-sdlc-api.yaml": 4, "bugfix.yaml": 2, "full-sdlc-web.yaml": 4, "full-sdlc-api-lite.yaml": 1, "bugfix-lite.yaml": 2}


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

    def run_pre(self, workflow, round_txt, cap=None, accept=False):
        (self.ad / "round.txt").write_text(f"{round_txt}\n")
        if cap is not None:
            (self.ad / "round-cap.txt").write_text(f"{cap}\n")
        elif (self.ad / "round-cap.txt").exists():
            os.remove(self.ad / "round-cap.txt")
        if accept:
            (self.ad / "accept-residuals.txt").write_text("human\n")
        elif (self.ad / "accept-residuals.txt").exists():
            os.remove(self.ad / "accept-residuals.txt")
        script = round_pre(workflow).replace("/Users/eduardopicazo/Documents/Workspace/Goodword/.archon/setup", str(Path(__file__).resolve().parent.parent))
        # the web lane is toy-pinned: its round-pre cds into a hardcoded worktree
        script = script.replace("/Users/eduardopicazo/Documents/Workspace/Goodword/web-app/.worktrees/archon-toy", str(self.tmp / "wt"))
        return subprocess.run(["bash", "-c", script], capture_output=True, encoding="utf-8", env={**os.environ, "ARTIFACTS_DIR": str(self.ad)})

    def test_under_cap_proceeds(self):
        for wf, default in LANES.items():
            r = self.run_pre(wf, default - 1)
            self.assertEqual(r.returncode, 0, wf + r.stdout + r.stderr)
            self.assertIn(f"ROUND={default}", r.stdout)

    def test_at_default_cap_stops_before_spending(self):
        for wf, default in LANES.items():
            r = self.run_pre(wf, default)
            self.assertEqual(r.returncode, 1, wf + r.stdout)
            self.assertIn(f"ROUND_CAP_REACHED round={default} cap={default}", r.stdout)
            self.assertEqual((self.ad / "round.txt").read_text().strip(), str(default), "round.txt must not be incremented")

    def test_explicit_cap_file_wins(self):
        for wf in LANES:
            r = self.run_pre(wf, 1, cap=1)
            self.assertEqual(r.returncode, 1, wf + r.stdout)
            r = self.run_pre(wf, 1, cap=3)
            self.assertEqual(r.returncode, 0, wf + r.stdout + r.stderr)

    def test_accept_residuals_bypasses_cap(self):
        for wf, default in LANES.items():
            r = self.run_pre(wf, default, accept=True)
            self.assertEqual(r.returncode, 0, wf + r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
