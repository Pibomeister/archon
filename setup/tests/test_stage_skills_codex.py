#!/usr/bin/env python3
"""Tests for stage-skills.sh codex staging: the CE review skills must ALSO be
symlinked into <root>/.agents/skills/ (codex's skill discovery dir), with the
same contract validation and the same fail-loud discipline as the .claude path.

RED-first for the codex-provider work (S1): these tests define the contract
before stage-skills.sh learns the .agents seam.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parent.parent
SCRIPT = SETUP / "stage-skills.sh"

CODE_SKILL = "contract markers: mode:headless and \"verdict\" live here\n"
DOC_SKILL = "contract markers: mode:headless lives here\n"


def make_ce_skills(dest):
    """A minimal CE skills dir satisfying contract_ok()."""
    for s, body in (("ce-code-review", CODE_SKILL), ("ce-doc-review", DOC_SKILL)):
        d = dest / s
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(body, encoding="utf-8")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.root = self.tmp / "root"
        # plugin cache with the validated baseline
        self.cache = self.home / ".claude/plugins/cache/compound-engineering-plugin/compound-engineering/3.2.0/skills"
        make_ce_skills(self.cache)
        # shipped operator skills the script also stages
        for s in ("archon-install", "archon-sdlc", "archon-linear"):
            d = self.root / ".archon/skills" / s
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text("operator skill\n", encoding="utf-8")
        # vendor snapshot present (not used when the cache hits)
        make_ce_skills(self.root / ".archon/vendor/ce-skills/3.2.0")

    def run_script(self):
        env = {"HOME": str(self.home), "PATH": os.environ["PATH"]}
        return subprocess.run(["bash", str(SCRIPT), str(self.root)],
                              capture_output=True, encoding="utf-8", env=env)


class CodexStaging(Base):
    def test_agents_skills_staged(self):
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for s in ("ce-code-review", "ce-doc-review"):
            link = self.root / ".agents/skills" / s
            self.assertTrue(link.is_symlink(), f"{link} is not a symlink")
            self.assertTrue((link / "SKILL.md").is_file(), f"{link}/SKILL.md does not resolve")
        self.assertIn("STAGED_CODEX: ce-code-review", r.stdout)
        self.assertIn("STAGED_CODEX: ce-doc-review", r.stdout)
        for s in ("archon-install", "archon-sdlc", "archon-linear"):
            self.assertTrue((self.root / ".agents/skills" / s / "SKILL.md").is_file())
            self.assertTrue((self.root / ".claude/skills" / s / "SKILL.md").is_file())

    def test_agents_and_claude_point_at_same_source(self):
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for s in ("ce-code-review", "ce-doc-review"):
            a = os.path.realpath(self.root / ".agents/skills" / s)
            c = os.path.realpath(self.root / ".claude/skills" / s)
            self.assertEqual(a, c, f"{s}: .agents and .claude staged from different sources")

    def test_idempotent(self):
        r1 = self.run_script()
        self.assertEqual(r1.returncode, 0, r1.stdout + r1.stderr)
        r2 = self.run_script()
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        for s in ("ce-code-review", "ce-doc-review"):
            link = self.root / ".agents/skills" / s
            self.assertTrue(link.is_symlink())
            self.assertTrue((link / "SKILL.md").is_file())

    def test_preexisting_real_dir_is_replaced_not_nested(self):
        # ln -sfn into an EXISTING real directory nests the link inside it —
        # the classic trap. The script must leave a resolving symlink at the
        # path itself, not a link one level down.
        d = self.root / ".agents/skills/ce-code-review"
        d.mkdir(parents=True)
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        link = self.root / ".agents/skills/ce-code-review"
        self.assertTrue(link.is_symlink(), "path is still a real dir (link nested inside?)")
        self.assertTrue((link / "SKILL.md").is_file())
        self.assertFalse((self.root / ".agents/skills/ce-code-review/ce-code-review").exists(),
                         "link was nested inside the pre-existing dir")

    def test_missing_cache_and_vendor_fails_typed(self):
        shutil.rmtree(self.cache.parent.parent)  # remove 3.2.0 entirely
        shutil.rmtree(self.root / ".archon/vendor/ce-skills/3.2.0")
        r = self.run_script()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("FAIL: no compound-engineering skills", r.stdout + r.stderr)
        self.assertFalse((self.root / ".agents/skills/ce-code-review").exists(),
                         "codex staging happened despite the FAIL")

    def test_broken_agents_contract_fails(self):
        # Validation sweep must cover the .agents path: if the staged .agents
        # link resolves but the contract markers are gone from the source AFTER
        # .claude staging would pass, the sweep still checks .agents itself.
        # Simulate by making .agents/skills unwritable so ln fails.
        agents = self.root / ".agents"
        agents.mkdir()
        os.chmod(agents, 0o555)
        self.addCleanup(os.chmod, agents, 0o755)
        r = self.run_script()
        self.assertNotEqual(r.returncode, 0, "script passed despite unstageable .agents dir")
        self.assertIn("FAIL", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
