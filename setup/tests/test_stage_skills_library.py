#!/usr/bin/env python3
"""setup/stage-skills-library.py: active then candidate SKILL.md bodies into
skills.md, provenance into skills-staged.json, nothing from PURPOSE.md or the
wiki, and only emptiness degrades to SKIP."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from skill_fixtures import TempLibrary, skill_text
import skill_library as sl

SETUP = Path(__file__).resolve().parent.parent
SCRIPT = SETUP / "stage-skills-library.py"
SENTINEL = "PURPOSE-SENTINEL-8f2a"
WIKI_SENTINEL = "WIKI-SENTINEL-4c1d"


def run(args, cwd=None):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True,
                          encoding="utf-8", cwd=cwd)


class Base(unittest.TestCase):
    def setUp(self):
        self.lib = TempLibrary(repos=("api", "web-app"))
        self.addCleanup(self.lib.cleanup)
        self.art = Path(tempfile.mkdtemp(prefix="art-"))
        self.addCleanup(shutil.rmtree, self.art, ignore_errors=True)

    def stage(self, repo="api", extra=()):
        return run(["--library", str(self.lib.root), "--repo", repo, "--artifacts", str(self.art), *extra])

    def staged_json(self):
        return json.loads((self.art / "skills-staged.json").read_text())

    def last(self, r):
        return (r.stdout.strip().splitlines() or [""])[-1]


class Skips(Base):
    def test_no_library(self):
        shutil.rmtree(self.lib.root / "api")
        r = self.stage()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.last(r), "SKILLS_STAGE=SKIP repo=api reason=no-library")
        self.assertFalse((self.art / "skills.md").exists())
        j = self.staged_json()
        self.assertEqual((j["schema"], j["result"], j["reason"]), (sl.SCHEMA_STAGED, "SKIP", "no-library"))

    def test_no_index(self):
        os.remove(self.lib.paths()["skills_index"])
        r = self.stage()
        self.assertEqual(self.last(r), "SKILLS_STAGE=SKIP repo=api reason=no-index")

    def test_empty_index(self):
        r = self.stage()
        self.assertEqual(self.last(r), "SKILLS_STAGE=SKIP repo=api reason=empty-index")
        self.assertEqual(self.staged_json()["library_head"], self.lib.head())

    def test_only_rolled_back(self):
        self.lib.add_skill("gone", status="rolled_back")
        r = self.stage()
        self.assertEqual(self.last(r), "SKILLS_STAGE=SKIP repo=api reason=no-eligible-skills")
        self.assertFalse((self.art / "skills.md").exists())

    def test_stale_skills_md_removed_on_skip(self):
        (self.art / "skills.md").write_text("stale")
        self.stage()
        self.assertFalse((self.art / "skills.md").exists())


class Ok(Base):
    def seed(self):
        self.lib.add_skill("zeta-active", text=skill_text("zeta-active", steps=("ZETA step",)),
                           purpose=SENTINEL + "\n")
        self.lib.add_skill("alpha-active", text=skill_text("alpha-active", steps=("ALPHA step",)))
        self.lib.add_skill("beta-candidate", status="candidate",
                           text=skill_text("beta-candidate", description="Candidate desc", steps=("BETA step",)))
        self.lib.add_skill("old", status="rolled_back")
        Path(self.lib.paths()["patterns_dir"], "x.md").write_text(WIKI_SENTINEL)

    def test_active_then_candidate_sorted(self):
        self.seed()
        r = self.stage()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        lines = r.stdout.strip().splitlines()
        self.assertEqual([l.split()[1] for l in lines[:-1]],
                         ["name=alpha-active", "name=zeta-active", "name=beta-candidate"])
        self.assertTrue(lines[-1].startswith("SKILLS_STAGE=OK repo=api active=2 candidate=1 bytes="), lines[-1])
        self.assertIn(f"head={self.lib.head()[:12]}", lines[-1])
        md = (self.art / "skills.md").read_text()
        self.assertTrue(md.startswith('<staged-skills repo="api" count="3">\n'))
        self.assertLess(md.index("ALPHA step"), md.index("ZETA step"))
        self.assertLess(md.index("ZETA step"), md.index("BETA step"))
        self.assertIn('<skill name="beta-candidate">\ndescription: Candidate desc\n', md)
        self.assertIn("</skill>", md)
        self.assertTrue(md.endswith("</staged-skills>\n"))

    def test_candidate_not_labelled_and_no_leaks(self):
        self.seed()
        r = self.stage()
        md = (self.art / "skills.md").read_text()
        self.assertNotIn("candidate", md.replace("beta-candidate", ""))
        self.assertNotIn("active", md.replace("alpha-active", "").replace("zeta-active", ""))
        for leak in (SENTINEL, WIKI_SENTINEL, "PURPOSE", "wiki", "rolled_back"):
            self.assertNotIn(leak, md, leak)
            self.assertNotIn(leak, r.stdout, leak)

    def test_staged_json_provenance(self):
        self.seed()
        self.stage()
        j = self.staged_json()
        self.assertEqual(j["result"], "OK")
        self.assertEqual([s["name"] for s in j["skills"]], ["alpha-active", "zeta-active", "beta-candidate"])
        self.assertEqual([s["status"] for s in j["skills"]], ["active", "active", "candidate"])
        for s in j["skills"]:
            path = self.lib.root / "api" / s["path"]
            self.assertEqual(s["sha256"], sl.sha256_file(path))
            self.assertEqual(s["bytes"], path.stat().st_size)
        self.assertEqual(j["skipped"], [{"name": "old", "status": "rolled_back"}])
        self.assertEqual(j["library_head"], self.lib.head())
        self.assertTrue(j["library_dirty"])  # seeded skills are uncommitted
        self.assertEqual(j["index_sha256"], sl.sha256_file(self.lib.paths()["skills_index"]))
        md = (self.art / "skills.md").read_bytes()
        self.assertEqual(j["skills_md_sha256"], sl.sha256_bytes(md))
        self.assertEqual(j["skills_md_bytes"], len(md))
        self.assertNotIn("candidate_since", json.dumps(j))

    def test_two_runs_are_byte_identical(self):
        self.seed()
        self.lib.commit("seeded")
        a = self.stage()
        first = ((self.art / "skills.md").read_bytes(), (self.art / "skills-staged.json").read_bytes(), a.stdout)
        b = self.stage()
        second = ((self.art / "skills.md").read_bytes(), (self.art / "skills-staged.json").read_bytes(), b.stdout)
        self.assertEqual(first, second)

    def test_check_writes_nothing(self):
        self.seed()
        r = run(["--check", "--library", str(self.lib.root), "--repo", "api"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(self.last(r).startswith("SKILLS_STAGE=OK repo=api active=2 candidate=1"))
        self.assertEqual(os.listdir(self.art), [])

    def test_repo_routing(self):
        self.lib.add_skill("api-only", text=skill_text("api-only", steps=("API-ONLY-STEP",)))
        self.lib.add_skill("web-only", repo="web-app", text=skill_text("web-only", steps=("WEB-ONLY-STEP",)))
        self.stage("web-app")
        md = (self.art / "skills.md").read_text()
        self.assertIn("WEB-ONLY-STEP", md)
        self.assertNotIn("API-ONLY-STEP", md)
        self.assertTrue(md.startswith('<staged-skills repo="web-app"'))
        self.assertEqual(self.staged_json()["repo"], "web-app")

    def test_outside_git_head_is_none(self):
        lib = TempLibrary(git_init=False)
        self.addCleanup(lib.cleanup)
        lib.add_skill("s")
        r = run(["--library", str(lib.root), "--repo", "api", "--artifacts", str(self.art)])
        self.assertTrue(self.last(r).endswith(" head=none"), r.stdout)
        j = self.staged_json()
        self.assertIsNone(j["library_head"])
        self.assertIsNone(j["library_dirty"])


class Fails(Base):
    def assert_fail(self, r, fragment):
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertTrue(self.last(r).startswith("SKILLS_STAGE=FAIL "), r.stdout)
        self.assertIn(fragment, self.last(r))
        self.assertFalse((self.art / "skills.md").exists())
        self.assertEqual(self.staged_json()["result"], "FAIL")

    def test_frontmatter_name_mismatch(self):
        self.lib.add_skill("one", text=skill_text("other"))
        self.assert_fail(self.stage(), "frontmatter name")

    def test_invalid_status(self):
        self.lib.add_skill("one")
        idx = self.lib.index()
        idx["skills"]["one"]["status"] = "pending"
        self.lib.write_index(idx)
        self.assert_fail(self.stage(), "status")

    def test_unregistered_dir(self):
        self.lib.add_skill("one")
        (Path(self.lib.paths()["skills_dir"]) / "ghost").mkdir()
        self.assert_fail(self.stage(), "unregistered")

    def test_missing_skill_md(self):
        self.lib.add_skill("one")
        os.remove(sl.skill_file(self.lib.root, "api", "one"))
        self.assert_fail(self.stage(), "missing")

    def test_sha_mismatch(self):
        self.lib.add_skill("one")
        Path(sl.skill_file(self.lib.root, "api", "one")).write_text(skill_text("one", steps=("edited",)))
        self.assert_fail(self.stage(), "sha256")

    def test_malformed_index(self):
        Path(self.lib.paths()["skills_index"]).write_text("{oops")
        self.assert_fail(self.stage(), "not JSON")

    def test_bad_name_in_index(self):
        self.lib.add_skill("one")
        idx = self.lib.index()
        idx["skills"]["Bad__Name"] = idx["skills"].pop("one")
        self.lib.write_index(idx)
        self.assert_fail(self.stage(), "invalid")

    def test_repo_traversal(self):
        r = self.stage("../api")
        self.assert_fail(r, "invalid repo")

    def test_two_candidates(self):
        self.lib.add_skill("a", status="candidate")
        self.lib.add_skill("b", status="candidate")
        self.assert_fail(self.stage(), "more than one candidate")

    def test_oversized_skill(self):
        big = skill_text("big", steps=("x" * 8200,))
        self.assertGreater(len(big.encode()), 8192)
        self.lib.add_skill("big", text=big)
        (self.art / "skills.md").write_text("stale")
        self.assert_fail(self.stage(), "cap 8192")

    def test_total_cap(self):
        for i in range(3):
            self.lib.add_skill(f"s{i}", text=skill_text(f"s{i}", steps=("y" * 3000,)))
        self.assert_fail(self.stage(extra=["--max-total-bytes", "8000"]), "total")
        r = self.stage(extra=["--max-total-bytes", "10000"])
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_check_also_fails_closed(self):
        self.lib.add_skill("one", text=skill_text("other"))
        r = run(["--check", "--library", str(self.lib.root), "--repo", "api"])
        self.assertEqual(r.returncode, 1)
        self.assertTrue(self.last(r).startswith("SKILLS_STAGE=FAIL"))
        self.assertEqual(os.listdir(self.art), [])

    def test_missing_artifacts_dir(self):
        r = run(["--library", str(self.lib.root), "--repo", "api", "--artifacts", str(self.art / "nope")])
        self.assertEqual(r.returncode, 1)
        self.assertTrue(r.stdout.startswith("SKILLS_STAGE=FAIL"))


if __name__ == "__main__":
    unittest.main()
