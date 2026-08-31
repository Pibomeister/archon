#!/usr/bin/env python3
"""bugfix rca-gate chain-citation check: citations resolve against the on-disk
checkout first, then against origin/main of the cited repo (the checkouts are
whatever branch a human left them on; the lane's worktrees come from
origin/main). Fail-closed: a path on neither is still a missing file.

The python under test is extracted from the workflow YAML itself (the
`LINKS=$(python3 - ... <<'PY' ... PY)` block of the rca-gate node), so this
tests the shipped gate, not a copy."""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ARCHON = Path(__file__).resolve().parent.parent.parent
WORKFLOWS = [ARCHON / "workflows" / "bugfix.yaml", ARCHON / "workflows" / "bugfix-lite.yaml"]


def gate_python(workflow):
    node = next(n for n in yaml.safe_load(workflow.read_text(encoding="utf-8"))["nodes"] if n["id"] == "rca-gate")
    m = re.search(r"LINKS=\$\(python3 - \"\$ARTIFACTS_DIR\" \"\$ROOT\" <<'PY'\n(.*?)\nPY\n\)", node["bash"], re.S)
    assert m, "citation block not found in rca-gate"
    return m.group(1)


def sh(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, shell=True, check=True, capture_output=True)


class CitationResolution(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "root"
        self.ad = self.tmp / "ad"
        self.ad.mkdir()
        # a fake api repo whose origin/main carries a file the checkout lacks
        remote = self.tmp / "remote.git"
        sh(f"git init -q --bare {remote}", self.tmp)
        api = self.root / "api"
        api.mkdir(parents=True)
        sh("git init -q && git config user.email t@t && git config user.name t", api)
        (api / "on-disk.ts").write_text("export const onDisk = 'quote-on-disk';\n")
        sh("git add . && git commit -qm 'base commit for citation tests'", api)
        sh(f"git remote add origin {remote} && git push -q origin HEAD:main", api)
        # advance origin/main with a new file; local checkout stays behind
        adv = self.tmp / "adv"
        sh(f"git clone -q {remote} {adv}", self.tmp)
        sh("git config user.email t@t && git config user.name t", adv)
        (adv / "only-on-main.ts").write_text("export const onlyOnMain = 'quote-only-on-main';\n")
        sh("git add . && git commit -qm advance && git push -q origin HEAD:main", adv)
        sh("git fetch -q origin", api)
        self.assertFalse((api / "only-on-main.ts").exists())
        (self.ad / "hypotheses.json").write_text(json.dumps([{"id": 1, "status": "open"}]))

    def chain(self, file, quote):
        (self.ad / "causal-chain.json").write_text(json.dumps({"links": [
            {"index": 1, "cause": "symptom", "evidence": {"source": "code", "file": "api/on-disk.ts", "quote": "quote-on-disk"}},
            {"index": 2, "cause": "root", "evidence": {"source": "code", "file": file, "quote": quote}, "fixable": True, "fix_site": "x.ts:1"},
        ]}))

    def run_gate(self, workflow):
        return subprocess.run([sys.executable, "-", str(self.ad), str(self.root)], input=gate_python(workflow),
                              capture_output=True, encoding="utf-8")

    def test_on_disk_citation_passes(self):
        for wf in WORKFLOWS:
            self.chain("api/on-disk.ts", "quote-on-disk")
            r = self.run_gate(wf)
            self.assertEqual(r.returncode, 0, wf.name + r.stderr)

    def test_origin_main_only_citation_passes_and_is_cached(self):
        for wf in WORKFLOWS:
            shutil.rmtree(self.ad / "cite-cache", ignore_errors=True)
            self.chain("api/only-on-main.ts", "quote-only-on-main")
            r = self.run_gate(wf)
            self.assertEqual(r.returncode, 0, wf.name + r.stderr)
            self.assertTrue((self.ad / "cite-cache" / "api" / "only-on-main.ts").is_file(), wf.name)

    def test_origin_main_wrong_quote_fails(self):
        for wf in WORKFLOWS:
            self.chain("api/only-on-main.ts", "quote-that-is-not-there")
            r = self.run_gate(wf)
            self.assertNotEqual(r.returncode, 0, wf.name)
            self.assertIn("uncited", r.stderr)

    def test_nowhere_file_fails_closed(self):
        for wf in WORKFLOWS:
            self.chain("api/never-existed.ts", "anything at all here")
            r = self.run_gate(wf)
            self.assertNotEqual(r.returncode, 0, wf.name)
            self.assertIn("not on disk, not at origin/main", r.stderr)

    def test_commit_citation_resolves_via_git_show(self):
        api = self.root / "api"
        sha = subprocess.run("git rev-parse HEAD", cwd=api, shell=True, capture_output=True, encoding="utf-8").stdout.strip()
        for wf in WORKFLOWS:
            self.chain(f"api@{sha[:10]}", "base commit for citation tests")  # the commit message
            r = self.run_gate(wf)
            self.assertEqual(r.returncode, 0, wf.name + r.stderr)
            self.chain(f"api@{sha[:10]}", "quote-on-disk")  # a line of its diff
            r = self.run_gate(wf)
            self.assertEqual(r.returncode, 0, wf.name + r.stderr)

    def test_unknown_commit_fails_closed(self):
        for wf in WORKFLOWS:
            self.chain("api@deadbeefcafe", "anything at all here")
            r = self.run_gate(wf)
            self.assertNotEqual(r.returncode, 0, wf.name)
            self.assertIn("cites missing file", r.stderr)

    def test_bare_repo_with_commit_description_fails(self):
        for wf in WORKFLOWS:
            self.chain("api (commit f6c3d9488ad83540f4a66a7fbb871de4c18a3036)", "anything at all here")
            r = self.run_gate(wf)
            self.assertNotEqual(r.returncode, 0, wf.name)

    def test_stale_on_disk_file_falls_through_to_origin_main(self):
        # the file exists on disk but the quoted line only exists at origin/main
        api = self.root / "api"
        adv = self.tmp / "adv"
        (adv / "on-disk.ts").write_text("export const onDisk = 'quote-on-disk';\nexport const added = 'quote-added-at-main';\n")
        sh("git add . && git commit -qm 'advance on-disk file' && git push -q origin HEAD:main", adv)
        sh("git fetch -q origin", api)
        self.assertNotIn("quote-added-at-main", (api / "on-disk.ts").read_text())
        for wf in WORKFLOWS:
            self.chain("api/on-disk.ts", "quote-added-at-main")
            r = self.run_gate(wf)
            self.assertEqual(r.returncode, 0, wf.name + r.stderr)
            self.chain("api/on-disk.ts", "quote-nowhere-at-all")
            r = self.run_gate(wf)
            self.assertNotEqual(r.returncode, 0, wf.name)
            self.assertIn("uncited", r.stderr)

    def test_line_suffix_is_stripped(self):
        for wf in WORKFLOWS:
            self.chain("api/on-disk.ts:1-3", "quote-on-disk")
            r = self.run_gate(wf)
            self.assertEqual(r.returncode, 0, wf.name + r.stderr)
            self.chain("api/only-on-main.ts:7", "quote-only-on-main")
            r = self.run_gate(wf)
            self.assertEqual(r.returncode, 0, wf.name + r.stderr)

    def test_unknown_repo_prefix_is_not_resolved(self):
        for wf in WORKFLOWS:
            self.chain("mobile-app/x.ts", "anything at all here")
            r = self.run_gate(wf)
            self.assertNotEqual(r.returncode, 0, wf.name)
            self.assertIn("cites missing file", r.stderr)


if __name__ == "__main__":
    unittest.main()
