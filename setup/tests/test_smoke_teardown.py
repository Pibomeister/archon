#!/usr/bin/env python3
"""A run must release what it holds even when it ends badly.

smoke-teardown depended on smoke-approval with no always_run, so a run that
failed upstream of the gate never released its servers. Under the lane-global
ports of the time that meant every later run of the lane died at
`PREFLIGHT=FAIL port 3124 busy` -- what happened after run 38d72218's matrix
render gate failed: the next launch could not start at all. Ports are per-run
now (RUNBOOK 5a), so the stranded resource is the machine-global e2e mutex
instead, and every lane needs it, not just this one.

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
                          f"{lane}: a failed run strands its servers and the e2e mutex")

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

    def test_the_ports_it_sweeps_are_this_run_s_own(self):
        # Blast radius: the sweep must be driven by THIS run's allocation, never
        # by a port literal -- a literal reaches whoever is listening, including
        # a concurrent run's server.
        for lane in LANES:
            bash = teardown(lane)["bash"]
            self.assertIn('for P in "$WEBPORT" "$APIPORT"', bash,
                          f"{lane}: teardown no longer sweeps its own allocation")
            self.assertIn("params-env.sh", bash, f"{lane}: ports are never resolved")
            for foreign in ("4123", "3123", "4124", "3124", "54322", "8001"):
                self.assertNotIn(f"for P in {foreign}", bash, f"{lane}: literal sweep of {foreign}")
                self.assertNotIn(f"kill {foreign}", bash, f"{lane}: touches {foreign}")

    def test_it_releases_the_shared_e2e_mutex(self):
        # The compose stack is left up on purpose; the LOCK is not. A run that
        # keeps it blocks every later run of every lane at its smoke stack.
        for lane in LANES:
            bash = teardown(lane)["bash"]
            self.assertRegex(bash, r'e2e-mutex\.sh"?\s+release',
                             f"{lane}: teardown strands the e2e mutex")

    def test_negative_control_a_literal_sweep_would_be_caught(self):
        bash = teardown("bugfix")["bash"]
        mutated = bash.replace('for P in "$WEBPORT" "$APIPORT"', "for P in 3124 4124")
        self.assertNotEqual(bash, mutated, "mutation anchor no longer matches the shipped node")
        self.assertNotIn('for P in "$WEBPORT" "$APIPORT"', mutated)
        self.assertIn("for P in 3124", mutated)


if __name__ == "__main__":
    unittest.main()
