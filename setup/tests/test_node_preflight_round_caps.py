#!/usr/bin/env python3
"""preflight: repository-scope runs are capped tighter than the legacy defaults.

The whole preflight body cannot run here — it needs gh auth, bun, the staged CE
skills and the knowledge base — so this extracts the round-cap block from the
SHIPPED body and runs that. Extraction, not a transcription: if the block is
edited or deleted in the YAML, this test runs the edit or fails to find it.

The readers already existed (round-pre, plan-round-pre, converge, exit-gate all
fall back to 4 and 3); writing the two files is the entire change.
"""
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from nodes.extract import runnable_body

BLOCK = re.compile(
    r'^( *)if \[ "\$\{ARCHON_FEATURE_SCOPE-\}" = repositories \]; then\n'
    r'(?:.*\n)*?\1fi\n',
    re.M,
)


def cap_block():
    body = runnable_body("full-sdlc-api", "preflight")
    blocks = [m.group(0) for m in BLOCK.finditer(body) if "round-cap.txt" in m.group(0)]
    assert len(blocks) == 1, f"expected exactly one round-cap block, found {len(blocks)}"
    return "set -euo pipefail\n" + blocks[0]


def run_block(scope="repositories", preset=None):
    art = Path(tempfile.mkdtemp(prefix="pf-"))
    for name, val in (preset or {}).items():
        (art / name).write_text(val)
    env = dict(os.environ, ARTIFACTS_DIR=str(art))
    if scope is None:
        env.pop("ARCHON_FEATURE_SCOPE", None)
    else:
        env["ARCHON_FEATURE_SCOPE"] = scope
    p = subprocess.run(["bash", "-c", cap_block()], capture_output=True, text=True, env=env)
    return p, art


class PreflightRoundCaps(unittest.TestCase):
    def test_repositories_scope_writes_both_caps(self):
        p, art = run_block()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual((art / "round-cap.txt").read_text().strip(), "3")
        self.assertEqual((art / "plan-round-cap.txt").read_text().strip(), "3")
        self.assertIn("ROUND_CAPS=OK review=3 plan=3", p.stdout)

    def test_the_legacy_scope_gets_no_cap_file(self):
        p, art = run_block(scope="legacy")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse((art / "round-cap.txt").exists())
        self.assertFalse((art / "plan-round-cap.txt").exists())
        self.assertNotIn("ROUND_CAPS", p.stdout)

    def test_an_unset_scope_gets_no_cap_file(self):
        p, art = run_block(scope=None)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse((art / "round-cap.txt").exists())
        self.assertNotIn("ROUND_CAPS", p.stdout)

    def test_an_operator_raise_survives(self):
        # RUNBOOK 3's raise-cap recipe is `echo 6 > round-cap.txt` then resume.
        # Clobbering it here would make the documented recipe a no-op for every
        # repository-scope run.
        p, art = run_block(preset={"round-cap.txt": "6\n"})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual((art / "round-cap.txt").read_text().strip(), "6")
        self.assertEqual((art / "plan-round-cap.txt").read_text().strip(), "3")
        self.assertIn("ROUND_CAPS=OK review=6 plan=3", p.stdout)


if __name__ == "__main__":
    unittest.main()
