#!/usr/bin/env python3
"""browser-exemption.py diff mode: a plan-time exemption must not survive a
diff that reaches a browser surface, and the api lane must actually call it
after implement and at exit (a helper nobody calls is not a gate)."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

SETUP = Path(__file__).resolve().parent.parent
WORKFLOWS = SETUP.parent / "workflows"


def git(repo, *argv):
    return subprocess.run(["git", "-C", str(repo), *argv], check=True,
                          capture_output=True, encoding="utf-8").stdout.strip()


class BrowserExemptionDiffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.ad, self.wt = root / "ad", root / "wt"
        self.ad.mkdir()
        self.wt.mkdir()
        git(self.wt, "init", "-q")
        git(self.wt, "config", "user.email", "t@example.com")
        git(self.wt, "config", "user.name", "t")
        (self.wt / "README.md").write_text("x\n", encoding="utf-8")
        git(self.wt, "add", ".")
        git(self.wt, "commit", "-qm", "base")
        self.base = git(self.wt, "rev-parse", "HEAD")
        (self.ad / "params.json").write_text(json.dumps({"repo": "api"}), encoding="utf-8")

    def policy(self, not_applicable):
        body = ({"not_applicable": "backend only", "required": []} if not_applicable else
                {"required": [{"id": "b", "criterion": "c", "path": "/", "assertions": [{"type": "text", "value": "v"}]}]})
        (self.ad / "browser-evidence.json").write_text(json.dumps(body), encoding="utf-8")

    def touch(self, rel):
        path = self.wt / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")

    def run_diff(self):
        return subprocess.run(["python3", str(SETUP / "browser-exemption.py"), "diff",
                               str(self.ad), str(self.wt), self.base],
                              capture_output=True, encoding="utf-8")

    def test_exempt_only_diff_keeps_the_exemption(self):
        self.policy(True)
        self.touch("scripts/probe.ts")
        r = self.run_diff()
        self.assertEqual(0, r.returncode, r.stdout)
        self.assertIn("BROWSER_EXEMPTION=DERIVED", r.stdout)

    def test_surface_path_in_diff_voids_the_exemption(self):
        self.policy(True)
        self.touch("scripts/probe.ts")
        self.touch("apps/api/src/help/help.controller.ts")
        r = self.run_diff()
        self.assertEqual(1, r.returncode, r.stdout)
        self.assertIn("path=apps/api/src/help/help.controller.ts", r.stdout)

    def test_committed_surface_path_is_seen_too(self):
        self.policy(True)
        self.touch("apps/api/src/x.ts")
        git(self.wt, "add", ".")
        git(self.wt, "commit", "-qm", "c")
        self.assertEqual(1, self.run_diff().returncode)

    def test_populated_policy_and_non_browser_repo_are_not_judged(self):
        self.touch("apps/api/src/x.ts")
        self.policy(False)
        self.assertEqual(0, self.run_diff().returncode)
        self.policy(True)
        (self.ad / "params.json").write_text(json.dumps({"repo": "goodword-mcp"}), encoding="utf-8")
        self.assertEqual(0, self.run_diff().returncode)


class BrowserExemptionIsWiredTest(unittest.TestCase):
    def test_api_lanes_recheck_the_exemption_after_implement_and_at_exit(self):
        for name in ("full-sdlc-api.yaml", "full-sdlc-api-codex.yaml"):
            nodes = {n["id"]: n for n in yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))["nodes"]}
            for node in ("gate-tests", "exit-gate"):
                self.assertRegex(
                    nodes[node]["bash"],
                    r'browser-exemption\.py"?\s+diff',
                    f"{name}:{node}",
                )


if __name__ == "__main__":
    unittest.main()
