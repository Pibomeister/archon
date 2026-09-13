#!/usr/bin/env python3
"""smoke (full-sdlc-api): a `goodword-mcp` run routes to mcp-smoke.sh and never
reaches the api boot path.

The route is an `exec` placed before the HAS_SMOKE emptiness check, so the
NOT_APPLICABLE skip is unreachable for that repo even while repo-profile.sh
still declares no boot smoke for it (B5 flips that separately). mcp-smoke.sh is
stood in for inside a temp mirror of setup/, which also proves the argument
order the node passes.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from nodes.extract import runnable_body

SETUP = Path(__file__).resolve().parent.parent
SHIM = """#!/usr/bin/env bash
printf 'MCP_SMOKE_SHIM wt=%s artifacts=%s port=%s\\n' "$1" "$2" "$3"
"""


class SmokeRoutesMcp(unittest.TestCase):
    def run_smoke(self, repo, port=4123):
        tmp = Path(tempfile.mkdtemp(prefix="sm-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        mirror = tmp / ".archon" / "setup"
        mirror.mkdir(parents=True)
        for p in SETUP.iterdir():
            if p.name != "mcp-smoke.sh":
                (mirror / p.name).symlink_to(p)
        (mirror / "mcp-smoke.sh").write_text(SHIM, encoding="utf-8")
        wt = tmp / "wt"
        wt.mkdir()
        ad = tmp / "ad"
        ad.mkdir()
        (ad / "params.json").write_text(json.dumps(
            {"spec": "/x.md", "slug": "x", "branch": "archon/x", "worktree": str(wt),
             "repo": repo, "api_port": port}))
        # Present on disk so a path that read it would be visible in the output.
        (ad / "smoke-probe.json").write_text(json.dumps({"path": "/public", "expect": "200"}))
        body = runnable_body("full-sdlc-api", "smoke", root=str(tmp))
        p = subprocess.run(["bash", "-c", body], capture_output=True, encoding="utf-8",
                           env=dict(os.environ, ARTIFACTS_DIR=str(ad)), cwd=str(tmp))
        return p, ad, wt

    def test_goodword_mcp_execs_the_mcp_smoke_with_the_port(self):
        p, ad, wt = self.run_smoke("goodword-mcp")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(f"MCP_SMOKE_SHIM wt={wt} artifacts={ad} port=4123", p.stdout)
        self.assertNotIn("SMOKE=NOT_APPLICABLE", p.stdout)
        self.assertNotIn("probe", p.stdout)
        self.assertFalse((ad / "smoke-result.txt").exists())

    def test_the_route_precedes_the_capability_gate(self):
        # Running a non-mcp repo here would boot a real server, so the ordering
        # that makes NOT_APPLICABLE unreachable is asserted on the body instead.
        body = runnable_body("full-sdlc-api", "smoke")
        self.assertLess(body.index("mcp-smoke.sh"), body.index('[ -z "$HAS_SMOKE" ]'))


if __name__ == "__main__":
    unittest.main()
