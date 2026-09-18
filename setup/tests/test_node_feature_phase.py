#!/usr/bin/env python3
"""feature-phase: the implement phase can be reopened for VERIFICATION only.

A repository-list chain re-runs a stage whose code already landed, to re-verify
it against a repaired upstream. Bootstrap still has to cut the worktree so the
gates have a tree to check; only the `implement` node is skipped, and
`commit-impl` stays unconditional so a verify-only pass still records its head.

The carrier is params.json, not the chain env: a resume drops
ARCHON_FEATURE_* (see local-candidate in the same lane), and a reopen that
silently re-implemented on resume is the failure this guards.
"""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from nodes.extract import runnable_body


def run_phase(scope="repositories", phase="implement", params=None):
    art = Path(tempfile.mkdtemp(prefix="fp-"))
    if params is not None:
        (art / "params.json").write_text(json.dumps(params))
    body = runnable_body("full-sdlc-api", "feature-phase")
    env = dict(os.environ, ARTIFACTS_DIR=str(art))
    for k, v in (("ARCHON_FEATURE_SCOPE", scope), ("ARCHON_FEATURE_PHASE", phase)):
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    p = subprocess.run(["bash", "-c", body], capture_output=True, text=True, env=env)
    out = None
    for line in p.stdout.splitlines():
        if line.startswith("{"):
            out = json.loads(line)
    return p, out


BASE = {"spec": "/x.md", "slug": "x", "branch": "archon/x", "worktree": "/tmp/x"}


class FeaturePhaseVerifyOnly(unittest.TestCase):
    def test_verify_only_bootstraps_without_implementing(self):
        p, out = run_phase(params=dict(BASE, feature_verify_only="yes"))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(out["bootstrap"], "yes", out)
        self.assertEqual(out["implement"], "no", out)
        self.assertEqual(out["plan"], "no", out)
        self.assertEqual(out["integration"], "no", out)

    def test_without_the_flag_implement_still_runs(self):
        p, out = run_phase(params=BASE)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(out["bootstrap"], "yes", out)
        self.assertEqual(out["implement"], "yes", out)

    def test_a_missing_params_file_reads_as_not_verify_only(self):
        # Fail-OPEN is right here: the absence of a flag is the absence of a
        # reopen, and refusing to implement on a read error would strand a
        # normal run. The node must not die on the read either.
        p, out = run_phase(params=None)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(out["implement"], "yes", out)

    def test_an_unparseable_params_file_reads_as_not_verify_only(self):
        art = Path(tempfile.mkdtemp(prefix="fp-"))
        (art / "params.json").write_text("{not json")
        body = runnable_body("full-sdlc-api", "feature-phase")
        env = dict(os.environ, ARTIFACTS_DIR=str(art),
                   ARCHON_FEATURE_SCOPE="repositories", ARCHON_FEATURE_PHASE="implement")
        p = subprocess.run(["bash", "-c", body], capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('"implement":"yes"', p.stdout)

    def test_the_flag_does_not_leak_into_the_other_phases(self):
        params = dict(BASE, feature_verify_only="yes")
        for phase, implement in (("planning", "no"), ("integration", "no")):
            with self.subTest(phase=phase):
                p, out = run_phase(phase=phase, params=params)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertEqual(out["implement"], implement, out)

    def test_the_flag_does_not_leak_into_the_legacy_scope(self):
        p, out = run_phase(scope="legacy", phase="legacy",
                           params=dict(BASE, feature_verify_only="yes"))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(out["implement"], "yes", out)
        self.assertEqual(out["plan"], "yes", out)


if __name__ == "__main__":
    unittest.main()
