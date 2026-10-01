#!/usr/bin/env python3
"""setup/risk-score.py: the deterministic, fail-closed risk scorer.

A temp artifacts dir and a temp git repo per test. The shipped
setup/risk-policy.json is the threshold source; tests read it rather than
restating numbers."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parent.parent
SCRIPT = SETUP / "risk-score.py"
sys.path.insert(0, str(SETUP))
import risk_policy as rp  # noqa: E402

_spec = importlib.util.spec_from_file_location("risk_score", SCRIPT)
rs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rs)

POLICY = rp.load_defaults()
PROFILE = {
    "profileVersion": "archon.project-profile.v2", "projectId": "project:test",
    "capabilities": {"defaultRepo": "api", "webRepo": "web-app"},
    "risk": {"protectedAreas": [{"paths": ["api/apps/integration-service/"], "floor": "red", "reason": "integration"}],
             "codeowners": {"ownerFloors": {"@org/security": "red"}, "defaultOwnedFloor": "yellow"}},
}


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, encoding="utf-8", check=True).stdout.strip()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rs-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ad = self.tmp / "artifacts"
        self.ad.mkdir()
        self.root = self.tmp / "repo"
        self.root.mkdir()
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.email", "t@example.com")
        git(self.root, "config", "user.name", "t")
        self.commit_file("README.md", "# repo\n")
        self.base = git(self.root, "rev-parse", "HEAD")
        self.profile = self.tmp / "profile.json"
        self.profile.write_text(json.dumps(PROFILE), encoding="utf-8")
        self.spec = self.tmp / "spec.md"
        self.write_spec("# Add note pinning\n\nKind: feature\n\nTouch apps/api/src/notes/notes.service.ts only.\n")
        self.write(self.ad, "params.json", {"spec": str(self.spec), "slug": "x", "branch": "archon/x",
                                            "worktree": str(self.root), "repo": "api"})

    def write(self, d, name, obj):
        p = Path(d) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj) if not isinstance(obj, str) else obj, encoding="utf-8")

    def write_spec(self, text):
        self.spec.write_text(text, encoding="utf-8")

    def commit_file(self, rel, text, msg="c"):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", msg)

    def run_cli(self, stage, *extra, python=None):
        argv = [python or sys.executable, str(SCRIPT), stage, "--artifacts", str(self.ad),
                "--profile", str(self.profile), "--repo-root", str(self.root), *extra]
        return subprocess.run(argv, capture_output=True, encoding="utf-8")

    def doc(self, stage):
        return json.loads((self.ad / f"risk-{stage}.json").read_text(encoding="utf-8"))

    def trajectory(self):
        return [json.loads(l) for l in (self.ad / "risk-trajectory.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]

    def assert_tier(self, r, tier, stage):
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        last = r.stdout.rstrip().splitlines()[-1]
        self.assertTrue(last.startswith(f"RISK_TIER={tier} stage={stage} score="), last)
        return last

    def assert_fail(self, r, needle):
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        last = r.stdout.rstrip().splitlines()[-1]
        self.assertTrue(last.startswith("RISK=FAIL"), last)
        self.assertIn(needle, last)
        self.assertEqual(sum(1 for l in r.stdout.splitlines() if l.startswith("RISK")), 1, r.stdout)


class Intake(Base):
    def test_docs_only_brief_is_green(self):
        self.write_spec("# Fix a typo\n\nKind: docs\n\nEdit docs/guide.md.\n")
        last = self.assert_tier(self.run_cli("intake"), "green", "intake")
        self.assertTrue(last.endswith("floors=none"), last)
        d = self.doc("intake")
        self.assertEqual(d["schema"], rp.SCHEMA_SCORE)
        self.assertEqual((d["stage"], d["tier"], d["mechanical"]["score"]), ("intake", "green", POLICY["points"]["taskClass"]["docs"]))
        self.assertEqual(d["floors"], [])
        self.assertEqual(d["codeowners"], {"present": False, "ownersTouched": [], "floorsApplied": []})
        self.assertIsNone(d["inputs"]["codeownersSha256"])
        self.assertEqual(d["inputs"]["briefSha256"], rp.sha256_file(str(self.spec)))
        self.assertEqual(d["inputs"]["profileSha256"], rp.sha256_file(str(self.profile)))
        self.assertEqual(d["inputs"]["policySha256"], rp.sha256_file(str(SETUP / "risk-policy.json")))
        self.assertIsNone(d["inputs"]["allowlistSha256"])
        self.assertIsNone(d["inputs"]["diffSha256"])
        self.assertEqual((d["escalated"], d["escalatedFrom"], d["handoffRunId"]), (False, None, None))

    def test_brief_naming_an_auth_path_is_red_by_floor(self):
        self.write_spec("# Tweak wording\n\nKind: docs\n\nOnly apps/api/src/auth/README.md changes.\n")
        last = self.assert_tier(self.run_cli("intake"), "red", "intake")
        self.assertTrue(last.endswith("floors=sensitive-domain:auth"), last)
        d = self.doc("intake")
        self.assertEqual(d["floors"][0]["paths"], ["apps/api/src/auth/README.md"])
        self.assertEqual(d["mechanical"]["score"], 0)  # floors dominate, points stay honest

    def test_missing_kind_defaults_to_feature_and_unknown_kind_fails(self):
        self.write_spec("# No kind line\n\nJust prose.\n")
        self.assert_tier(self.run_cli("intake"), "green", "intake")
        sig = [s for s in self.doc("intake")["mechanical"]["signals"] if s["id"] == "task-class"][0]
        self.assertEqual((sig["value"], sig["points"]), ("feature", POLICY["points"]["taskClass"]["feature"]))
        self.write_spec("# Bad\n\nKind: sprint\n")
        self.assert_fail(self.run_cli("intake"), "task class")

    def test_kind_heading_form(self):
        self.write_spec("# T\n\n## Kind\n\nrefactor\n\n## Goal\nx\n")
        self.assert_tier(self.run_cli("intake"), "green", "intake")
        sig = [s for s in self.doc("intake")["mechanical"]["signals"] if s["id"] == "task-class"][0]
        self.assertEqual(sig["value"], "refactor")

    def test_brief_paths_ignore_urls_and_trailing_punctuation(self):
        self.write_spec("# T\n\nKind: chore\n\nSee https://example.com/auth/docs. Edit libs/util/str.ts, then apps/api/src/billing/ (all of it).\n")
        self.assert_tier(self.run_cli("intake"), "red", "intake")
        d = self.doc("intake")
        paths = [s for s in d["mechanical"]["signals"] if s["id"] == "brief-paths"][0]["value"]
        self.assertEqual(paths, ["apps/api/src/billing/", "libs/util/str.ts"])
        self.assertEqual([f["id"] for f in d["floors"]], ["sensitive-domain:payments"])

    def test_bare_lockfile_name_counts_as_a_brief_path(self):
        self.write_spec("# Bump deps\n\nKind: chore\n\nRegenerate bun.lock.\n")
        last = self.assert_tier(self.run_cli("intake"), "yellow", "intake")
        self.assertTrue(last.endswith("floors=lockfile"), last)
        self.assertEqual(self.doc("intake")["floors"][0]["paths"], ["bun.lock"])

    def test_codeowners_at_base_commit_populates_owners_and_floors(self):
        self.commit_file("CODEOWNERS", "*  @org/default\n/apps/api/src/auth/  @org/security\n")
        base = git(self.root, "rev-parse", "HEAD")
        self.write_spec("# T\n\nKind: feature\n\nTouch apps/api/src/auth/guard.ts.\n")
        self.assert_tier(self.run_cli("intake", "--base", base), "red", "intake")
        d = self.doc("intake")
        self.assertEqual(d["codeowners"], {"present": True, "ownersTouched": ["@org/security"],
                                           "floorsApplied": ["codeowners:@org/security", "codeowners:owned"]})
        self.assertEqual(d["inputs"]["codeownersSha256"], rp.sha256_text("*  @org/default\n/apps/api/src/auth/  @org/security\n"))
        self.assertEqual(d["inputs"]["baseCommit"], base)
        ids = [f["id"] for f in d["floors"]]
        self.assertEqual(ids, ["codeowners:@org/security", "codeowners:owned", "sensitive-domain:auth"])

    def test_codeowners_is_read_at_base_not_working_tree(self):
        self.commit_file("CODEOWNERS", "*  @org/default\n")
        base = git(self.root, "rev-parse", "HEAD")
        (self.root / "CODEOWNERS").write_text("/libs/  @org/later\n", encoding="utf-8")  # uncommitted edit
        self.write_spec("# T\n\nKind: feature\n\nTouch libs/x.ts.\n")
        self.assert_tier(self.run_cli("intake", "--base", base), "yellow", "intake")
        self.assertEqual(self.doc("intake")["codeowners"]["ownersTouched"], ["@org/default"])

    def test_malformed_codeowners_fails_closed(self):
        self.commit_file(".github/CODEOWNERS", "@org/team apps/\n")
        self.assert_fail(self.run_cli("intake", "--base", git(self.root, "rev-parse", "HEAD")), "CODEOWNERS")
        self.assertFalse((self.ad / "risk-intake.json").exists())

    def test_override_raises_and_never_lowers(self):
        self.write_spec("# T\n\nKind: docs\n\nEdit docs/a.md.\n")
        self.assert_tier(self.run_cli("intake", "--override-tier", "yellow"), "yellow", "intake")
        self.assertEqual(self.doc("intake")["override"], "yellow")
        self.write_spec("# T\n\nKind: docs\n\nEdit apps/api/src/auth/a.md.\n")
        self.assert_tier(self.run_cli("intake", "--override-tier", "yellow"), "red", "intake")
        r = self.run_cli("intake", "--override-tier", "green")
        self.assertEqual(r.returncode, 2, r.stderr)  # argparse: green is not a valid override

    def test_bad_profile_or_brief_fails_closed(self):
        self.profile.write_text("{nope", encoding="utf-8")
        self.assert_fail(self.run_cli("intake"), "profile")
        self.profile.write_text(json.dumps(PROFILE), encoding="utf-8")
        os.remove(self.spec)
        self.assert_fail(self.run_cli("intake"), "brief")
        self.write(self.ad, "params.json", "[]")
        self.assert_fail(self.run_cli("intake"), "params.json")

    def test_same_inputs_same_bytes_and_trajectory_appends(self):
        self.assert_tier(self.run_cli("intake"), "green", "intake")
        first = (self.ad / "risk-intake.json").read_bytes()
        self.assert_tier(self.run_cli("intake"), "green", "intake")
        self.assertEqual(first, (self.ad / "risk-intake.json").read_bytes())
        rows = self.trajectory()
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[0]["stage"], rows[0]["tier"], rows[0]["score"]), ("intake", "green", POLICY["points"]["taskClass"]["feature"]))
        self.assertIn("at", rows[0])
        self.assertNotIn("at", json.loads(first))

    def test_lane_tier_marks_escalation_required(self):
        self.write_spec("# T\n\nKind: docs\n\nEdit apps/api/src/auth/a.md.\n")
        self.assert_tier(self.run_cli("intake", "--lane-tier", "yellow"), "red", "intake")
        d = self.doc("intake")
        self.assertEqual(d["escalation"], {"required": True, "toTier": "red"})
        self.assertEqual(d["laneTier"], "yellow")
        self.assertTrue(self.trajectory()[-1]["escalate"])
        self.assert_tier(self.run_cli("intake", "--lane-tier", "red"), "red", "intake")
        self.assertEqual(self.doc("intake")["escalation"], {"required": False, "toTier": None})

    def test_runs_under_system_python3(self):
        if not os.path.exists("/usr/bin/python3"):
            self.skipTest("no /usr/bin/python3")
        self.assert_tier(self.run_cli("intake", python="/usr/bin/python3"), "green", "intake")


if __name__ == "__main__":
    unittest.main()
