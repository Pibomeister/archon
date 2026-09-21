#!/usr/bin/env python3
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parent.parent
ARCHON = SETUP.parent
sys.path.insert(0, str(SETUP))
import profile_bind as pb  # noqa: E402


class ProfileBindTest(unittest.TestCase):
    def test_goodword_keeps_bun_kb_and_aws_warn(self):
        profile = json.loads((ARCHON / "profiles/goodword/project.v1.json").read_text())
        with tempfile.TemporaryDirectory() as td:
            runtime = pb.bind(profile, ARCHON, Path(td) / "Goodword", Path(td) / "ad")
        self.assertEqual(runtime["layout"], "siblings")
        self.assertEqual(runtime["kbRootRel"], "goodword-kb")
        self.assertTrue(runtime["kbRequired"])
        self.assertEqual(runtime["gitnexusRepo"], "api")
        self.assertEqual(runtime["impactUnavailable"], "route-full")
        self.assertIn("api", runtime["allowedRepos"])
        self.assertEqual(runtime["webRepo"], "web-app")
        self.assertEqual(runtime["requiredCheckouts"], ["api", "web-app"])
        ids = {t["id"]: t["fail"] for t in runtime["requiredTools"]}
        self.assertTrue(ids["bun"])
        self.assertFalse(ids["aws"])
        self.assertIn("api", runtime["repos"])
        self.assertEqual(runtime["repos"]["api"]["typecheck"], ["bun", "run", "typecheck"])

    def test_fluxkeep_omits_bun_aws_and_sibling_kb(self):
        profile = json.loads((ARCHON / "profiles/fluxkeep-next.v1.json").read_text())
        with tempfile.TemporaryDirectory() as td:
            runtime = pb.bind(profile, ARCHON, Path(td) / "fluxkeep", Path(td) / "ad")
            sh = (Path(td) / "ad" / "profile-runtime.sh").read_text()
        self.assertEqual(runtime["layout"], "single")
        self.assertFalse(runtime["kbRequired"])
        self.assertEqual(runtime["kbRootRel"], "")
        self.assertEqual(runtime["impactUnavailable"], "allow")
        self.assertEqual(runtime["gitnexusRepo"], "")
        ids = {t["id"]: t["fail"] for t in runtime["requiredTools"]}
        self.assertNotIn("bun", ids)
        self.assertNotIn("aws", ids)
        self.assertTrue(ids["pnpm"])
        self.assertFalse(ids["gh"])
        self.assertIn("fluxkeep", runtime["repos"])
        self.assertIn("KB_REQUIRED=0", sh)

    def test_check_tools_goodword_bun_is_hard_fluxkeep_bun_is_not(self):
        goodword = json.loads((ARCHON / "profiles/goodword/project.v1.json").read_text())
        fluxkeep = json.loads((ARCHON / "profiles/fluxkeep-next.v1.json").read_text())
        gw_fail, gw_warn = pb.check_tools(goodword["capabilities"]["requiredTools"], {"gh", "pnpm"})
        self.assertIn("bun", gw_fail)
        self.assertIn("aws", gw_warn)
        fk_fail, fk_warn = pb.check_tools(fluxkeep["capabilities"]["requiredTools"], {"pnpm", "gh"})
        self.assertEqual(fk_fail, [])
        self.assertEqual(fk_warn, [])
        self.assertTrue(callable(shutil.which))

    def test_preflight_helper_has_no_goodword_stack_literals(self):
        text = (SETUP / "profile-preflight.sh").read_text(encoding="utf-8")
        self.assertNotIn("command -v bun", text)
        self.assertNotIn("goodword-kb", text)
        self.assertNotIn("command -v aws", text)
        self.assertIn("profile_bind.py", text)

    def test_resolve_params_single_layout_uses_project_root_worktree(self):
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "fluxkeep"
            ad = Path(td) / "artifacts"
            root.mkdir()
            ad.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            spec = Path(td) / "spec.md"
            spec.write_text("# t\n", encoding="utf-8")
            profile = json.loads((ARCHON / "profiles/fluxkeep-next.v1.json").read_text())
            pb.bind(profile, ARCHON, root, ad)
            env = dict(__import__("os").environ)
            env.update({
                "ARCHON_LAYER": str(ARCHON),
                "PROJECT_ROOT": str(root),
                "ARTIFACTS_DIR": str(ad),
                "ARCHON_REPO": "fluxkeep",
            })
            r = subprocess.run(
                ["bash", str(SETUP / "resolve-params.sh"), str(root), str(spec), str(ad), "4125",
                 "--allow", "fluxkeep"],
                capture_output=True, text=True, env=env,
            )
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            params = json.loads((ad / "params.json").read_text())
            self.assertEqual(params["repo"], "fluxkeep")
            self.assertEqual(params["layout"], "single")
            self.assertEqual(params["worktree"], str(root / ".worktrees" / "spec"))
            self.assertNotIn("api_port", params)


if __name__ == "__main__":
    unittest.main()
