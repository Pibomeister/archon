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
    env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(["git", "-C", str(root), "-c", "commit.gpgsign=false", *args],
                          capture_output=True, encoding="utf-8", check=True, env=env).stdout.strip()


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

    def commit_bytes(self, rel, data, msg="c"):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", msg)

    def run_cli(self, stage, *extra, python=None, cwd=None):
        argv = [python or sys.executable, str(SCRIPT), stage, "--artifacts", str(self.ad),
                "--profile", str(self.profile), "--repo-root", str(self.root), *extra]
        return subprocess.run(argv, capture_output=True, encoding="utf-8", cwd=cwd)

    def doc(self, stage):
        return json.loads((self.ad / f"risk-{stage}.json").read_text(encoding="utf-8"))

    def trajectory(self):
        return [json.loads(l) for l in (self.ad / "risk-trajectory.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]

    def assert_tier(self, r, tier, stage):
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        last = r.stdout.rstrip().splitlines()[-1]
        self.assertTrue(last.startswith(f"RISK_TIER={tier} stage={stage} score="), last)
        return last

    def assert_fail(self, r, needle, stage="intake"):
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        last = r.stdout.rstrip().splitlines()[-1]
        self.assertTrue(last.startswith("RISK=FAIL"), last)
        self.assertIn(needle, last)
        self.assertEqual(sum(1 for l in r.stdout.splitlines() if l.startswith("RISK")), 1, r.stdout)
        self.assertFalse((self.ad / f"risk-{stage}.json").exists(), f"risk-{stage}.json must not exist after FAIL")


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
        os.remove(self.ad / "risk-intake.json")  # isolate the FAIL-leaves-no-doc check from the prior successful run
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
        # green is not a valid override; argparse's usage error gets the same typed line as any other FAIL
        os.remove(self.ad / "risk-intake.json")  # isolate the FAIL-leaves-no-doc check from the prior successful run
        self.assert_fail(self.run_cli("intake", "--override-tier", "green"), "usage:")

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

    def test_leading_dot_slash_normalises_to_the_same_path(self):
        self.write_spec("# T\n\nKind: chore\n\nTouch ./apps/integration-service/x.ts.\n")
        self.assert_tier(self.run_cli("intake"), "red", "intake")
        doc_a = self.doc("intake")
        self.write_spec("# T\n\nKind: chore\n\nTouch apps/integration-service/x.ts.\n")
        self.assert_tier(self.run_cli("intake"), "red", "intake")
        doc_b = self.doc("intake")
        self.assertEqual(doc_a["floors"], doc_b["floors"])
        paths_a = [s for s in doc_a["mechanical"]["signals"] if s["id"] == "brief-paths"][0]["value"]
        paths_b = [s for s in doc_b["mechanical"]["signals"] if s["id"] == "brief-paths"][0]["value"]
        self.assertEqual(paths_a, paths_b)
        self.assertEqual(paths_a, ["apps/integration-service/x.ts"])

    def test_path_escaping_the_repo_is_dropped_not_failed(self):
        self.write_spec("# T\n\nKind: chore\n\nDo not touch ../../etc/passwd.\n")
        last = self.assert_tier(self.run_cli("intake"), "green", "intake")
        self.assertTrue(last.endswith("floors=none"), last)
        paths = [s for s in self.doc("intake")["mechanical"]["signals"] if s["id"] == "brief-paths"][0]["value"]
        self.assertEqual(paths, [])

    def test_bare_two_segment_directory_without_repo_prefix_hits_sensitive_domain(self):
        self.write_spec("# T\n\nKind: chore\n\nRewrite src/auth please.\n")
        last = self.assert_tier(self.run_cli("intake"), "red", "intake")
        self.assertTrue(last.endswith("floors=sensitive-domain:auth"), last)
        paths = [s for s in self.doc("intake")["mechanical"]["signals"] if s["id"] == "brief-paths"][0]["value"]
        self.assertEqual(paths, ["src/auth"])

    def test_leading_slash_and_backslash_paths_both_resolve_and_hit_a_floor(self):
        self.write_spec("# T\n\nKind: chore\n\nSee /apps/api/src/auth/guard.ts and apps\\api\\src\\auth\\x.ts.\n")
        last = self.assert_tier(self.run_cli("intake"), "red", "intake")
        self.assertTrue(last.endswith("floors=sensitive-domain:auth"), last)
        paths = [s for s in self.doc("intake")["mechanical"]["signals"] if s["id"] == "brief-paths"][0]["value"]
        self.assertEqual(paths, ["apps/api/src/auth/guard.ts", "apps/api/src/auth/x.ts"])

    def test_non_utf8_brief_fails_closed(self):
        self.spec.write_bytes(b"# T\n\nKind: docs\n\n\xff\n")
        self.assert_fail(self.run_cli("intake"), "brief")

    def test_non_utf8_codeowners_fails_closed(self):
        self.commit_bytes("CODEOWNERS", b"*  @org/default\n\xff\n")
        base = git(self.root, "rev-parse", "HEAD")
        self.assert_fail(self.run_cli("intake", "--base", base), "git")

    def test_trajectory_write_failure_leaves_no_score_doc(self):
        (self.ad / "risk-trajectory.jsonl").mkdir()
        self.assert_fail(self.run_cli("intake"), "risk-trajectory.jsonl")

    def test_empty_brief_fails_closed(self):
        self.write_spec("   \n\t\n")
        self.assert_fail(self.run_cli("intake"), "brief is empty")

    def test_argparse_usage_error_emits_typed_line(self):
        argv = [sys.executable, str(SCRIPT), "intake", "--profile", str(self.profile), "--repo-root", str(self.root)]
        r = subprocess.run(argv, capture_output=True, encoding="utf-8")
        self.assert_fail(r, "usage:")

    def test_relative_spec_in_params_resolves_against_repo_root_not_cwd(self):
        (self.root / "brief.md").write_text("# T\n\nKind: docs\n\nEdit docs/a.md.\n", encoding="utf-8")
        self.write(self.ad, "params.json", {"spec": "brief.md", "slug": "x", "branch": "archon/x",
                                            "worktree": str(self.root), "repo": "api"})
        r = self.run_cli("intake", cwd=str(self.tmp))
        self.assert_tier(r, "green", "intake")

    def test_manifest_name_embedded_in_a_longer_path_is_not_double_counted(self):
        self.write_spec("# T\n\nKind: chore\n\nRegenerate apps/web/package.json only.\n")
        last = self.assert_tier(self.run_cli("intake"), "yellow", "intake")
        self.assertTrue(last.endswith("floors=manifest"), last)
        d = self.doc("intake")
        paths = [s for s in d["mechanical"]["signals"] if s["id"] == "brief-paths"][0]["value"]
        self.assertEqual(paths, ["apps/web/package.json"])
        self.assertEqual(d["floors"][0]["paths"], ["apps/web/package.json"])


class BriefPathsUnit(unittest.TestCase):
    """Direct checks of risk_score.brief_paths, independent of the CLI/git
    harness, for the canonicalisation and de-duplication rules."""

    def test_leading_dot_slash_and_bare_form_canonicalise_identically(self):
        text = "Touch ./apps/api/src/auth/a.ts and apps/api/src/auth/b.ts."
        self.assertEqual(rs.brief_paths(text, POLICY), ["apps/api/src/auth/a.ts", "apps/api/src/auth/b.ts"])

    def test_leading_slash_and_backslashes_are_canonicalised(self):
        text = "See /apps/api/src/auth/c.ts and apps\\api\\src\\auth\\d.ts."
        self.assertEqual(rs.brief_paths(text, POLICY), ["apps/api/src/auth/c.ts", "apps/api/src/auth/d.ts"])

    def test_escaping_and_still_absolute_tokens_are_dropped(self):
        text = "Do not touch ../../etc/passwd or //etc/shadow."
        self.assertEqual(rs.brief_paths(text, POLICY), [])

    def test_single_slash_directory_without_trailing_slash_is_captured(self):
        self.assertEqual(rs.brief_paths("Rewrite src/auth please.", POLICY), ["src/auth"])

    def test_bare_manifest_inside_a_longer_path_is_not_also_a_separate_hit(self):
        self.assertEqual(rs.brief_paths("Regenerate apps/web/package.json only.", POLICY), ["apps/web/package.json"])

    def test_bare_manifest_and_lockfile_names_still_count_on_their_own(self):
        self.assertEqual(rs.brief_paths("Bump package.json and bun.lock.", POLICY), ["bun.lock", "package.json"])


if __name__ == "__main__":
    unittest.main()
