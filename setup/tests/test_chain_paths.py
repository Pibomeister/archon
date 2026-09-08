#!/usr/bin/env python3
"""The gate packet must print a command that works.

post-approval-integrity needs ARCHON_BUGFIX_CHAIN_STATE and
ARCHON_ATTESTATION_DIR. archon-run.py sets both when IT runs the workflow, so
they are absent on any pass the CLI drives -- and the CLI is exactly what the
gate packet's DECIDE box tells the operator to run. On run 127a883f,
`archon workflow approve` recorded the approval and then died with
`POST_APPROVAL=FAIL no chain state`, naming a file that existed and was
readable. The operator had to reconstruct two paths by hand to get past a gate
they had already approved.

Neither variable is a credential. They are paths derivable from the run's own
bugfix-chain.json, and the guard is controller-attest --verify, which checks the
seal's MAC against the chain secret. A wrongly located file fails verification;
locating it correctly cannot weaken anything."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ARCHON = Path(__file__).resolve().parent.parent.parent
SCRIPT = ARCHON / "setup" / "chain-paths.sh"
CHAIN = "65f111b1b8eb49d477c37144e44dba4a"


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


class ChainPaths(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ad = self.tmp / "artifacts"
        self.ad.mkdir()
        (self.ad / "bugfix-chain.json").write_text(
            json.dumps({"logical_chain_id": CHAIN}), encoding="utf-8")
        self.control = self.tmp / "control"
        (self.control / "bugfix-chains").mkdir(parents=True)
        (self.control / "bugfix-chains" / f"{CHAIN}.json").write_text("{}", encoding="utf-8")

    def run_it(self, **env):
        base = {k: v for k, v in os.environ.items()
                if k not in ("ARCHON_BUGFIX_CHAIN_STATE", "ARCHON_ATTESTATION_DIR",
                             "ARCHON_CONTROL_DIR")}
        base["ARCHON_CONTROL_DIR"] = str(self.control)
        base.update(env)
        return subprocess.run(["bash", str(SCRIPT), str(self.ad)],
                              capture_output=True, encoding="utf-8", env=base)

    def test_it_derives_both_paths(self):
        r = self.run_it()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"bugfix-chains/{CHAIN}.json", r.stdout)
        self.assertIn("attestations", r.stdout)

    def test_an_existing_environment_always_wins(self):
        # A launcher-set value is never second-guessed.
        r = self.run_it(ARCHON_BUGFIX_CHAIN_STATE="/set/by/launcher.json",
                        ARCHON_ATTESTATION_DIR="/set/by/launcher")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "", "it overrode an environment it did not set")

    def test_a_missing_chain_file_fails_typed(self):
        (self.ad / "bugfix-chain.json").unlink()
        r = self.run_it()
        self.assertEqual(r.returncode, 1)
        self.assertIn("CHAIN_PATHS=FAIL", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_a_chain_state_that_does_not_exist_fails_typed(self):
        # Deriving a path is not the same as finding one. A run whose control
        # dir is elsewhere must be told, not handed a path to nothing.
        (self.control / "bugfix-chains" / f"{CHAIN}.json").unlink()
        r = self.run_it()
        self.assertEqual(r.returncode, 1)
        self.assertIn("no chain state at", r.stderr)
        self.assertIn("ARCHON_CONTROL_DIR", r.stderr)

    def test_the_output_is_evalable(self):
        # The node consumes this with eval; a path containing a space would
        # break an unquoted printf, so the export lines are %q-quoted.
        spaced = self.tmp / "control dir"
        (spaced / "bugfix-chains").mkdir(parents=True)
        (spaced / "bugfix-chains" / f"{CHAIN}.json").write_text("{}", encoding="utf-8")
        r = self.run_it(ARCHON_CONTROL_DIR=str(spaced))
        self.assertEqual(r.returncode, 0, r.stderr)
        out = self.tmp / "exports.sh"
        out.write_text(r.stdout, encoding="utf-8")
        probe = subprocess.run(
            ["bash", "-c", f'set -u; . "{out}"; echo "$ARCHON_BUGFIX_CHAIN_STATE"'],
            capture_output=True, encoding="utf-8")
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertIn(f"control dir/bugfix-chains/{CHAIN}.json", probe.stdout)


class WiredIntoTheGate(unittest.TestCase):
    def node(self, lane, nid):
        doc = yaml.safe_load((ARCHON / "workflows" / f"{lane}.yaml").read_text(encoding="utf-8"))
        got = [n["bash"] for n in walk(doc.get("nodes")) if n.get("id") == nid]
        self.assertEqual(len(got), 1, f"{lane}/{nid}")
        return got[0]

    def test_post_approval_resolves_before_it_demands(self):
        for lane in ("bugfix", "bugfix-codex"):
            bash = self.node(lane, "post-approval-integrity")
            self.assertIn("chain-paths.sh", bash, f"{lane}: still requires a launcher-set env")
            self.assertLess(bash.index("chain-paths.sh"),
                            bash.index('test -n "${ARCHON_BUGFIX_CHAIN_STATE-}"'),
                            f"{lane}: demands the variable before resolving it")

    def test_the_verification_guard_is_still_there(self):
        # Locating the file must not have replaced checking it.
        for lane in ("bugfix", "bugfix-codex"):
            bash = self.node(lane, "post-approval-integrity")
            self.assertIn("controller-attest.py", bash, lane)
            self.assertIn("--verify", bash, lane)
            self.assertIn("validate-current-manifest", bash, lane)


if __name__ == "__main__":
    unittest.main()
