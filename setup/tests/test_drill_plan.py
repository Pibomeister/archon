"""plan-round-pre (full-sdlc-api plan-loop), ported from an ad-hoc scratchpad
drill mirroring the rca-round-pre coverage in test_drill_rca.py.

plan-round-pre checks the round cap BEFORE a round is spent (a body node's
non-zero exit fails the whole group; `archon workflow resume` re-enters the
group with a fresh iteration, so a cap enforced only in plan-converge could be
walked past forever by resuming after every critic-gate failure). Both
plan-round.txt and plan-round-cap.txt are hand-editable, untrusted input:
`[ x -ge y ]` on a non-integer returns 2, which `if` reads as "not taken" —
silently switching the cap off. A junk cap falls back to the default (3); a
junk round counter stops the run instead, because resetting a counter to 0 on
garbage would itself be a way to run forever. This file always extracts the
node body live via nodes.extract.runnable_body, so it tracks the YAML.
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from nodes.extract import runnable_body


def run_plan_round_pre(art):
    import os

    body = runnable_body("full-sdlc-api", "plan-round-pre")
    env = dict(os.environ, ARTIFACTS_DIR=str(art))
    return subprocess.run(["bash", "-c", body], capture_output=True, text=True, env=env)


def assert_cap_file(case, art, name, line):
    """The cap discriminator is `echo … | tee -a <artifacts>/<name>`. archon
    persists a node's stderr but not its stdout, so this FILE — not the console
    line — is what the operator and the babysit loop actually read; asserting
    only on stdout would pass with the tee deleted."""
    f = art / name
    case.assertTrue(f.is_file(), f"{name} not written")
    case.assertIn(line, f.read_text())


class PlanRoundPreCapFirstTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "plan.md").write_text("## Goal\nDo the thing.\n", encoding="utf-8")

    def test_no_round_file_defaults_to_zero_and_progresses(self):
        r = run_plan_round_pre(self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("PLAN_ROUND=1 cap=3", r.stdout)
        self.assertEqual((self.tmp / "plan-round.txt").read_text().strip(), "1")
        self.assertTrue((self.tmp / "plan-round-1" / "plan.pre.md").is_file())

    def test_junk_round_counter_fails_closed_typed(self):
        (self.tmp / "plan-round.txt").write_text("banana\n")
        r = run_plan_round_pre(self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("PLAN_ROUND_PRE=FAIL plan-round.txt is not an integer: [banana]", r.stdout)

    def test_junk_cap_falls_back_to_default_and_still_enforces(self):
        # A junk cap does NOT switch the cap off (fail-open would be the bug);
        # it falls back to the documented default of 3.
        (self.tmp / "plan-round.txt").write_text("3\n")
        (self.tmp / "plan-round-cap.txt").write_text("not-a-number\n")
        r = run_plan_round_pre(self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("PLAN_ROUND_CAP round=3 cap=3", r.stdout)
        assert_cap_file(self, self.tmp, "plan-loop-exit.txt", "PLAN_ROUND_CAP round=3 cap=3")

    def test_cap_checked_before_round_is_spent(self):
        # round=3 with the default cap=3: the cap must fire BEFORE plan.md is
        # even required to exist, and before any plan-round-N dir is created.
        (self.tmp / "plan.md").unlink()
        (self.tmp / "plan-round.txt").write_text("3\n")
        r = run_plan_round_pre(self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("PLAN_ROUND_CAP round=3 cap=3", r.stdout)
        assert_cap_file(self, self.tmp, "plan-loop-exit.txt", "PLAN_ROUND_CAP round=3 cap=3")
        self.assertFalse((self.tmp / "plan-round-4").exists())

    def test_custom_cap_respected(self):
        (self.tmp / "plan-round.txt").write_text("5\n")
        (self.tmp / "plan-round-cap.txt").write_text("5\n")
        r = run_plan_round_pre(self.tmp)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("PLAN_ROUND_CAP round=5 cap=5", r.stdout)
        assert_cap_file(self, self.tmp, "plan-loop-exit.txt", "PLAN_ROUND_CAP round=5 cap=5")

    def test_below_cap_progresses_and_snapshots_optional_files(self):
        (self.tmp / "plan-round.txt").write_text("1\n")
        (self.tmp / "verify.json").write_text("{}")
        r = run_plan_round_pre(self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("PLAN_ROUND=2 cap=3", r.stdout)
        rd = self.tmp / "plan-round-2"
        self.assertTrue((rd / "plan.pre.md").is_file())
        self.assertTrue((rd / "pre-verify.json").is_file())
        # files-allowlist.json / reader-audit.json / premises.json /
        # smoke-probe.json are all optional; none exist here, so none of
        # their pre-* snapshots should either.
        for f in ("files-allowlist.json", "reader-audit.json", "premises.json", "smoke-probe.json"):
            self.assertFalse((rd / f"pre-{f}").exists())


if __name__ == "__main__":
    unittest.main()
