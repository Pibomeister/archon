#!/usr/bin/env python3
"""Bounded blocking e2e-mutex (wait/run) and its stale-owner recovery."""
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


SETUP = Path(__file__).resolve().parents[1]
SCRIPT = SETUP / "e2e-mutex.sh"
LIVE_RUN = "11111111-1111-1111-1111-111111111111"
DEAD_RUN = "22222222-2222-2222-2222-222222222222"


class E2eMutexWaitTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="e2e-mutex-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.lock = self.root / "e2e.lock"
        self.db = self.root / "archon.db"
        con = sqlite3.connect(self.db)
        con.execute("create table remote_agent_workflow_runs (id text primary key, status text)")
        con.executemany("insert into remote_agent_workflow_runs values (?, ?)",
                        [(LIVE_RUN, "running"), (DEAD_RUN, "failed")])
        con.commit()
        con.close()
        self.env = dict(os.environ, ARCHON_E2E_LOCK=str(self.lock), ARCHON_DB=str(self.db),
                        ARCHON_E2E_WAIT_SECONDS="20", ARCHON_E2E_POLL_SECONDS="0.1")

    def owner(self, run):
        return str(self.root / "runs" / run)

    def op(self, *args, script=SCRIPT, **env):
        return subprocess.run(["bash", str(script), *args], capture_output=True, text=True,
                              env=dict(self.env, **env), timeout=60)

    def hold(self, run, pid=None):
        """A lock left by `run`, optionally still held by a live process."""
        self.lock.mkdir()
        (self.lock / "owner").write_text(self.owner(run) + "\n")
        if pid is not None:
            (self.lock / "pid").write_text(f"{pid}\n")

    def critical_section(self, log, name):
        return ["sh", "-c", f'echo "start {name}" >> "{log}"; sleep 1; echo "end {name}" >> "{log}"']

    @staticmethod
    def serialized(log):
        lines = log.read_text().split()
        pairs = [lines[i:i + 4] for i in range(0, len(lines), 4)]
        return all(p[1] == p[3] and p[0] == "start" and p[2] == "end" for p in pairs)

    def start_two(self, log, wrapped):
        procs = []
        for name in ("A", "B"):
            cmd = self.critical_section(log, name)
            argv = ["bash", str(SCRIPT), "run", self.owner(name), "--", *cmd] if wrapped else cmd
            procs.append(subprocess.Popen(argv, stdout=subprocess.PIPE, text=True, env=self.env))
        outs = [p.communicate(timeout=60)[0] for p in procs]
        return [(p.returncode, out) for p, out in zip(procs, outs)]

    def test_two_concurrent_acquirers_serialize(self):
        log = self.root / "wrapped.log"
        results = self.start_two(log, wrapped=True)
        self.assertEqual([0, 0], [rc for rc, _ in results])
        self.assertTrue(self.serialized(log), log.read_text())
        self.assertIn("E2E_MUTEX=WAITING owner=", "".join(out for _, out in results))
        self.assertFalse(self.lock.exists())

    def test_negative_control_unwrapped_acquirers_interleave(self):
        log = self.root / "bare.log"
        self.start_two(log, wrapped=False)
        self.assertFalse(self.serialized(log), "the serialization check cannot fail: " + log.read_text())

    def test_run_releases_on_the_failure_path(self):
        r = self.op("run", self.owner("A"), "--", "sh", "-c", "exit 7")
        self.assertEqual(7, r.returncode, r.stdout)
        self.assertIn("E2E_MUTEX=RELEASED", r.stdout)
        self.assertFalse(self.lock.exists())

    def test_negative_control_without_the_release_trap_the_lock_strands(self):
        body = SCRIPT.read_text()
        anchor = "if [ \"$TOOK\" = yes ]; then trap 'release_lock' EXIT; fi"
        self.assertIn(anchor, body, "mutation anchor no longer matches")
        mutant = self.root / "mutant.sh"
        mutant.write_text(body.replace(anchor, ":"))
        r = self.op("run", self.owner("A"), "--", "sh", "-c", "exit 7", script=mutant)
        self.assertEqual(7, r.returncode)
        self.assertTrue(self.lock.exists(), "release-path test would pass without the trap")

    def test_nested_run_by_the_owner_does_not_release_the_outer_lock(self):
        r = self.op("run", self.owner("A"), "--", "bash", str(SCRIPT), "run", self.owner("A"), "--",
                    "sh", "-c", f'test -d "{self.lock}"')
        self.assertEqual(0, r.returncode, r.stdout)
        self.assertIn("E2E_MUTEX=HELD", r.stdout)
        self.assertEqual(1, r.stdout.count("E2E_MUTEX=RELEASED"), r.stdout)

    def test_terminal_owner_lock_is_reclaimed(self):
        self.hold(DEAD_RUN)
        r = self.op("wait", self.owner("B"))
        self.assertEqual(0, r.returncode, r.stdout)
        self.assertIn(f"E2E_MUTEX=RECLAIMED stale owner={self.owner(DEAD_RUN)} status=failed", r.stdout)
        self.assertEqual(self.owner("B"), (self.lock / "owner").read_text().strip())

    def test_non_blocking_acquire_also_reclaims_a_terminal_owner(self):
        self.hold(DEAD_RUN)
        r = self.op("acquire", self.owner("B"))
        self.assertEqual(0, r.returncode, r.stdout)
        self.assertIn("E2E_MUTEX=RECLAIMED", r.stdout)

    def test_live_owner_is_not_stolen_and_the_timeout_is_typed(self):
        self.hold(LIVE_RUN)
        r = self.op("wait", self.owner("B"), ARCHON_E2E_WAIT_SECONDS="1")
        self.assertEqual(1, r.returncode, r.stdout)
        self.assertRegex(r.stdout, rf"E2E_MUTEX=WAITING owner={self.owner(LIVE_RUN)} waited=\d+s")
        self.assertIn("E2E_MUTEX=FAIL timeout", r.stdout)
        self.assertEqual(self.owner(LIVE_RUN), (self.lock / "owner").read_text().strip())

    def test_terminal_run_with_a_live_holder_pid_is_not_stolen(self):
        self.hold(DEAD_RUN, pid=os.getpid())
        r = self.op("wait", self.owner("B"), ARCHON_E2E_WAIT_SECONDS="1")
        self.assertEqual(1, r.returncode, r.stdout)
        self.assertNotIn("RECLAIMED", r.stdout)
        self.assertEqual(self.owner(DEAD_RUN), (self.lock / "owner").read_text().strip())

    def test_owner_unknown_to_archon_db_is_not_stolen(self):
        self.hold("33333333-3333-3333-3333-333333333333")
        r = self.op("wait", self.owner("B"), ARCHON_E2E_WAIT_SECONDS="1")
        self.assertEqual(1, r.returncode, r.stdout)
        self.assertIn("E2E_MUTEX=FAIL timeout", r.stdout)

    def test_wait_releases_to_a_waiter_when_the_holder_finishes(self):
        self.hold(LIVE_RUN)
        waiter = subprocess.Popen(["bash", str(SCRIPT), "wait", self.owner("B")],
                                  stdout=subprocess.PIPE, text=True, env=self.env)
        time.sleep(0.5)
        self.assertIsNone(waiter.poll(), "waiter did not block on a live owner")
        self.assertEqual(0, self.op("release", self.owner(LIVE_RUN)).returncode)
        out = waiter.communicate(timeout=20)[0]
        self.assertEqual(0, waiter.returncode, out)
        self.assertIn("E2E_MUTEX=ACQUIRED owner=" + self.owner("B"), out)


if __name__ == "__main__":
    unittest.main()
