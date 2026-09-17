#!/usr/bin/env python3
"""library/<repo>/ skeletons are committed for every repo that
setup/repo-profile.sh --list knows, are exactly what ensure_skeleton would
create, validate as empty registries, and carry no machine-specific paths."""
import json
import re
import subprocess
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import skill_library as sl  # noqa: E402

ARCHON = Path(__file__).resolve().parents[2]
LIBRARY = ARCHON / "library"
SKELETON = ("README.md", "raw/index.jsonl", "wiki/index.md", "wiki/log.md",
            "wiki/skill-impact.md", "wiki/skill-impact.jsonl", "wiki/patterns/.keep",
            "skills/index.json")


def repos():
    out = subprocess.run(["bash", str(ARCHON / "setup/repo-profile.sh"), "--list"],
                         capture_output=True, encoding="utf-8", check=True).stdout.split()
    return sorted(out)


class Skeleton(unittest.TestCase):
    def test_every_repo_has_the_skeleton(self):
        names = repos()
        self.assertEqual(names, ["api", "goodword-mcp", "web-app"])
        for repo in names:
            for rel in SKELETON:
                self.assertTrue((LIBRARY / repo / rel).exists(), f"{repo}/{rel}")
            self.assertTrue(sl.skeleton_present(LIBRARY, repo))

    def test_index_json_is_an_empty_valid_registry(self):
        for repo in repos():
            idx = sl.load_index(LIBRARY, repo)
            self.assertEqual(idx["repo"], repo)
            self.assertEqual(idx["skills"], {})
            self.assertEqual(idx["window_size"], sl.DEFAULT_WINDOW)
            self.assertEqual(idx["score_version"], 1)

    def test_ledgers_start_empty(self):
        for repo in repos():
            p = sl.paths(LIBRARY, repo)
            self.assertEqual(Path(p["raw_ledger"]).read_text(), "")
            self.assertEqual(Path(p["impact_jsonl"]).read_text(), "")
            self.assertEqual(sorted(x.name for x in Path(p["patterns_dir"]).iterdir()), [".keep"])

    def test_generated_files_match_the_module_templates(self):
        for repo in repos():
            p = sl.paths(LIBRARY, repo)
            self.assertEqual(Path(p["readme"]).read_text(), sl.REPO_README.format(repo=repo))
            self.assertEqual(Path(p["wiki_index"]).read_text(), sl.WIKI_INDEX_HEADER.format(repo=repo))
            self.assertEqual(Path(p["wiki_log"]).read_text(), sl.WIKI_LOG_HEADER.format(repo=repo))
            self.assertEqual(Path(p["impact_md"]).read_text(), sl.IMPACT_HEADER.format(repo=repo))

    def test_no_machine_paths_or_urls_anywhere(self):
        bad = re.compile(re.escape(sl.ABS_HOME_MARKER) + r"|https?://|(?<![\w.])~/")
        for path in LIBRARY.rglob("*"):
            if path.is_file():
                self.assertIsNone(bad.search(path.read_text(errors="replace")), str(path))

    def test_readmes_state_the_rules(self):
        top = (LIBRARY / "README.md").read_text()
        for phrase in ("Rollback is asymmetric", "runtime agent sees skills only", "One candidate per repository",
                       "Every write is mechanical", "skill-evolve@archon.local", "No absolute paths",
                       "setup/skill-score.py", "Only this file ships"):
            self.assertIn(phrase, top, phrase)
        for repo in repos():
            text = (LIBRARY / repo / "README.md").read_text()
            for phrase in ("never staged", "skills roll back, the wiki never does", "contested",
                           "At most one", "Do not edit this directory by hand", "Ties roll back"):
                self.assertIn(phrase, text, f"{repo}: {phrase}")

    def test_ensure_skeleton_creates_nothing_on_the_committed_tree(self):
        for repo in repos():
            self.assertEqual(sl.ensure_skeleton(LIBRARY, repo), [])


if __name__ == "__main__":
    unittest.main()
