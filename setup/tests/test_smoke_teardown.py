#!/usr/bin/env python3
"""A run must release lane-global ports even when it ends badly.

Ports 3124/4124 belong to the bugfix lane, not to one run. smoke-teardown
depended on smoke-approval with no always_run, so a run that failed upstream of
the gate never released them and every later run of the lane died at
`PREFLIGHT=FAIL port 3124 busy`. That is what happened after run 38d72218's
matrix render gate failed: the next launch could not start at all.

The node also opened by running validate-smoke-readiness and exiting 1 on
failure. Readiness governs shipping. Gating cleanup on it meant that a run whose
ticket was not RESOLVED refused to release its own ports -- which is precisely
the population of runs that end without approval, and so precisely the runs that
strand them."""
import unittest
from pathlib import Path

import yaml

ARCHON = Path(__file__).resolve().parent.parent.parent
LANES = ["bugfix", "bugfix-codex"]


def walk(nodes):
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        yield n
        for key in ("loop_group", "body"):
            v = n.get(key)
            if isinstance(v, dict):
                yield from walk(v.get("nodes"))
            elif isinstance(v, list):
                yield from walk(v)


def teardown(lane):
    doc = yaml.safe_load((ARCHON / "workflows" / f"{lane}.yaml").read_text(encoding="utf-8"))
    got = [n for n in walk(doc.get("nodes")) if n.get("id") == "smoke-teardown"]
    assert len(got) == 1, f"{lane}: expected one smoke-teardown, got {len(got)}"
    return got[0]


class SmokeTeardown(unittest.TestCase):
    def test_the_probe_finds_the_node(self):
        for lane in LANES:
            self.assertTrue(teardown(lane)["bash"], f"{lane}: teardown has no body")

    def test_teardown_runs_even_when_the_run_never_reached_the_gate(self):
        for lane in LANES:
            self.assertIs(teardown(lane).get("always_run"), True,
                          f"{lane}: a failed run strands ports 3124/4124")

    def test_readiness_never_blocks_cleanup(self):
        for lane in LANES:
            bash = teardown(lane)["bash"]
            self.assertIn("validate-smoke-readiness", bash, f"{lane}: readiness is not reported")
            i = bash.index("validate-smoke-readiness")
            window = bash[i:i + 400]
            self.assertNotIn("exit 1", window.split("port_pids")[0],
                             f"{lane}: cleanup still aborts when the ticket is unshippable")
            self.assertIn("SMOKE_TEARDOWN=NOTE", bash, f"{lane}: the unshippable case is silent")

    def test_a_missing_matrix_does_not_abort_the_teardown(self):
        # A run that died before smoke-matrix-render has no matrix to summarize,
        # and that must not stop it from releasing the ports.
        for lane in LANES:
            bash = teardown(lane)["bash"]
            self.assertIn('test -s "$AD/smoke-matrix.json" ||', bash,
                          f"{lane}: teardown reads a matrix it may not have")

    def test_the_ports_it_sweeps_are_the_lane_s_own(self):
        # Negative control on blast radius: teardown must never reach for the
        # 4123/3123 pair the feature lane owns, nor the shared e2e mutex.
        for lane in LANES:
            bash = teardown(lane)["bash"]
            self.assertIn("for P in 3124 4124", bash, f"{lane}: lane ports changed")
            for foreign in ("4123", "3123", "54322", "8001"):
                self.assertNotIn(f"kill {foreign}", bash, f"{lane}: touches {foreign}")


if __name__ == "__main__":
    unittest.main()
