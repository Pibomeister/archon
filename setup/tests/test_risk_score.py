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
    "capabilities": {"defaultRepo": "api", "webRepo": "web-app", "allowedRepos": ["api", "goodword-mcp", "web-app"]},
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

    def test_bare_factory_control_name_is_red_by_floor(self):
        for brief, name in (("Update the Jenkinsfile", "Jenkinsfile"), ("Edit CODEOWNERS", "CODEOWNERS"),
                            ("Change .gitlab-ci.yml", ".gitlab-ci.yml")):
            self.write_spec(f"# T\n\nKind: docs\n\n{brief}.\n")
            last = self.assert_tier(self.run_cli("intake"), "red", "intake")
            self.assertTrue(last.endswith("floors=factory-control"), last)
            self.assertEqual({f["id"]: f["paths"] for f in self.doc("intake")["floors"]}, {"factory-control": [name]})

    def test_ordinary_bare_word_is_not_a_brief_path(self):
        self.write_spec("# T\n\nKind: docs\n\nTidy the readme and the setup notes.\n")
        last = self.assert_tier(self.run_cli("intake"), "green", "intake")
        self.assertTrue(last.endswith("floors=none"), last)
        paths = [s for s in self.doc("intake")["mechanical"]["signals"] if s["id"] == "brief-paths"][0]["value"]
        self.assertEqual(paths, [])

    def test_bare_name_from_a_profile_protected_area_counts_as_a_brief_path(self):
        profile = json.loads(json.dumps(PROFILE))
        profile["risk"]["protectedAreas"].append({"paths": ["Makefile", "*.tf"], "floor": "red", "reason": "infra"})
        self.profile.write_text(json.dumps(profile), encoding="utf-8")
        self.write_spec("# T\n\nKind: docs\n\nEdit the Makefile, then main.tf.\n")
        self.assert_tier(self.run_cli("intake"), "red", "intake")
        self.assertEqual({f["id"]: f["paths"] for f in self.doc("intake")["floors"]},
                         {"protected:infra": ["Makefile", "main.tf"]})

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

    def test_repo_prefixed_brief_paths_hit_root_anchored_floors(self):
        for named, tier, fid in (("api/.github/workflows/ci.yml", "red", "factory-control"),
                                 ("web-app/.github/workflows/deploy.yml", "red", "factory-control"),
                                 ("api/uv.lock", "yellow", "lockfile"),
                                 ("web-app/uv.lock", "yellow", "lockfile"),
                                 ("goodword-mcp/uv.lock", "yellow", "lockfile"),
                                 ("api/apps/integration-service/handler.ts", "red", "protected:integration")):
            self.write_spec(f"# T\n\nKind: docs\n\nTouch {named} only.\n")
            self.assert_tier(self.run_cli("intake"), tier, "intake")
            floors = {f["id"]: f["paths"] for f in self.doc("intake")["floors"]}
            self.assertEqual(floors, {fid: [named]})

    def test_brief_path_naming_a_sibling_repo_is_not_matched_against_primary_codeowners(self):
        self.commit_file("CODEOWNERS", "*.md  @org/docs\n")
        self.write_spec("# T\n\nKind: docs\n\nTouch web-app/docs/readme.md only.\n")
        self.assert_tier(self.run_cli("intake"), "green", "intake")
        d = self.doc("intake")
        self.assertEqual(d["codeowners"], {"present": True, "ownersTouched": [], "floorsApplied": []})
        self.assertEqual(d["floors"], [])

    def test_brief_path_naming_the_primary_repo_is_matched_against_its_codeowners(self):
        self.commit_file("CODEOWNERS", "/docs/  @org/docs\n")
        self.write_spec("# T\n\nKind: docs\n\nTouch api/docs/readme.md only.\n")
        self.assert_tier(self.run_cli("intake"), "yellow", "intake")
        d = self.doc("intake")
        self.assertEqual(d["codeowners"]["ownersTouched"], ["@org/docs"])
        self.assertEqual({f["id"]: f["paths"] for f in d["floors"]}, {"codeowners:owned": ["api/docs/readme.md"]})

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

    def test_mid_path_dotdot_segments_resolve_instead_of_escaping(self):
        self.assertEqual(rs.canonical_path("apps/../apps/integration/x.ts", "path"), "apps/integration/x.ts")


class LaneTierOfUnit(unittest.TestCase):
    """Direct checks of risk_score.lane_tier_of: the tier word must match a
    whole -/_ delimited segment of fromLane, not an arbitrary substring."""

    def test_returns_none_for_no_handoff(self):
        self.assertIsNone(rs.lane_tier_of(None))
        self.assertIsNone(rs.lane_tier_of({}))

    def test_matches_a_whole_dash_or_underscore_delimited_segment(self):
        self.assertEqual(rs.lane_tier_of({"fromLane": "sdlc-green"}), "green")
        self.assertEqual(rs.lane_tier_of({"fromLane": "sdlc_yellow_v2"}), "yellow")
        self.assertEqual(rs.lane_tier_of({"fromLane": "red"}), "red")

    def test_does_not_match_a_bare_substring_of_a_longer_segment(self):
        self.assertIsNone(rs.lane_tier_of({"fromLane": "evergreen-lane"}))
        self.assertIsNone(rs.lane_tier_of({"fromLane": "sdlc-starred"}))


def gathered(name, file, callers):
    return {"name": name, "file": file, "d1_callers": callers, "risk": "LOW",
            "query_status": "GATHERED", "query_repo": "api", "query_target": name}


class Plan(Base):
    def baseline(self, files=None, impact=True):
        self.write(self.ad, "files-allowlist.json", files or ["apps/api/src/notes/notes.service.ts",
                                                              "apps/api/src/notes/notes.service.spec.ts"])
        if impact:
            self.write(self.ad, "impact.json", {"status": "GATHERED", "symbols": [
                gathered("NotesService.list", "apps/api/src/notes/notes.service.ts", ["a", "b"])]})
        self.write(self.ad, "triage.json", {"size": "S", "reasons": [], "hot_path_hits": [], "unknowns": []})

    def test_small_feature_is_green_and_records_allowlist_digest(self):
        self.baseline()
        last = self.assert_tier(self.run_cli("plan"), "green", "plan")
        d = self.doc("plan")
        self.assertEqual(d["inputs"]["allowlistSha256"],
                         rp.sha256_bytes(rp.canonical_bytes(json.loads((self.ad / "files-allowlist.json").read_text()))))
        ids = {s["id"]: s for s in d["mechanical"]["signals"]}
        self.assertEqual((ids["files"]["value"], ids["files"]["points"]), (1, 0))
        self.assertEqual((ids["test-files"]["value"], ids["test-files"]["points"]), (1, 0))
        self.assertEqual((ids["d1-callers"]["value"], ids["d1-callers"]["points"]), (2, 0))
        self.assertEqual((ids["impact"]["value"], ids["impact"]["points"]), ("GATHERED", 0))
        self.assertEqual((ids["triage"]["value"], ids["triage"]["points"]), ("S", 0))
        self.assertEqual(ids["coverage"]["value"], "no probes")
        self.assertTrue(last.endswith("floors=none"))

    def test_points_push_a_big_plan_to_yellow(self):
        files = [f"apps/api/src/notes/f{i}.ts" for i in range(7)]
        self.baseline(files=files)
        self.assert_tier(self.run_cli("plan"), "yellow", "plan")
        d = self.doc("plan")
        pts = POLICY["points"]
        expected = pts["taskClass"]["feature"] + pts["filesOverMax"] + pts["perExtraFile"] * (7 - POLICY["sizeThresholds"]["max_files"])
        self.assertEqual(d["mechanical"]["score"], expected)
        self.assertEqual(d["floors"], [])

    def test_floors_dominate_points(self):
        self.baseline(files=["docs/auth/README.md"])
        self.write_spec("# T\n\nKind: docs\n")
        self.assert_tier(self.run_cli("plan"), "red", "plan")
        d = self.doc("plan")
        self.assertEqual(d["mechanical"]["score"], 0)
        self.assertEqual([f["id"] for f in d["floors"]], ["sensitive-domain:auth"])

    def test_lockfile_is_yellow_migration_red_public_contract_yellow_factory_red(self):
        for files, tier, fid in ((["bun.lock"], "yellow", "lockfile"),
                                 (["libs/data-access/src/lib/rds/migrations/0007.ts"], "red", "migration"),
                                 (["app/services/api-client.d.ts"], "yellow", "public-contract"),
                                 ([".github/workflows/ci.yml"], "red", "factory-control")):
            self.baseline(files=files)
            self.write_spec("# T\n\nKind: chore\n")
            self.assert_tier(self.run_cli("plan"), tier, "plan")
            self.assertIn(fid, [f["id"] for f in self.doc("plan")["floors"]])

    def test_profile_protected_area_with_repo_prefix(self):
        self.baseline(files=["apps/integration-service/handler.ts"])
        self.assert_tier(self.run_cli("plan"), "red", "plan")
        self.assertEqual([f["id"] for f in self.doc("plan")["floors"]], ["protected:integration"])

    def test_web_allowlist_is_tagged_with_the_web_repo(self):
        self.baseline()
        self.write(self.ad, "web-files-allowlist.json", ["app/services/api-client.d.ts"])
        self.assert_tier(self.run_cli("plan"), "yellow", "plan")
        d = self.doc("plan")
        self.assertEqual(d["floors"][0]["paths"], ["web-app/app/services/api-client.d.ts"])

    def test_web_allowlist_paths_hit_root_anchored_floors_in_the_web_repo(self):
        for web, tier, fid in (([".github/workflows/deploy.yml"], "red", "factory-control"),
                               (["uv.lock"], "yellow", "lockfile")):
            self.baseline()
            self.write(self.ad, "web-files-allowlist.json", web)
            self.assert_tier(self.run_cli("plan"), tier, "plan")
            floors = {f["id"]: f["paths"] for f in self.doc("plan")["floors"]}
            self.assertEqual(floors, {fid: [f"web-app/{web[0]}"]})

    def test_web_allowlist_path_matches_a_web_repo_prefixed_protected_area(self):
        profile = json.loads(json.dumps(PROFILE))
        profile["risk"]["protectedAreas"].append(
            {"paths": ["web-app/app/routes/editor/"], "floor": "red", "reason": "editor"})
        self.profile.write_text(json.dumps(profile), encoding="utf-8")
        self.baseline()
        self.write(self.ad, "web-files-allowlist.json", ["app/routes/editor/index.tsx"])
        self.assert_tier(self.run_cli("plan"), "red", "plan")
        floors = {f["id"]: f["paths"] for f in self.doc("plan")["floors"]}
        self.assertEqual(floors, {"protected:editor": ["web-app/app/routes/editor/index.tsx"]})

    def test_web_allowlist_paths_are_not_matched_against_primary_codeowners(self):
        self.commit_file("CODEOWNERS", "*.md  @org/docs\n")
        self.baseline()
        self.write(self.ad, "web-files-allowlist.json", ["docs/readme.md"])
        self.assert_tier(self.run_cli("plan"), "green", "plan")
        d = self.doc("plan")
        self.assertEqual(d["codeowners"], {"present": True, "ownersTouched": [], "floorsApplied": []})
        self.assertEqual(d["floors"], [])

    def test_missing_allowlist_fails_closed(self):
        self.assert_fail(self.run_cli("plan"), "files-allowlist.json", stage="plan")

    def test_bad_allowlist_entries_fail_closed(self):
        for bad in ([], ["/abs/path.ts"], ["//abs/path.ts"], ["apps\\api\\x.ts"], ["../escape.ts"], [""], "not a list"):
            self.write(self.ad, "files-allowlist.json", bad)
            self.assert_fail(self.run_cli("plan"), "files-allowlist.json", stage="plan")

    def test_impact_missing_or_unavailable_adds_unknown_points(self):
        self.baseline(impact=False)
        self.assert_tier(self.run_cli("plan"), "yellow", "plan")
        ids = {s["id"]: s for s in self.doc("plan")["mechanical"]["signals"]}
        self.assertEqual((ids["impact"]["value"], ids["impact"]["points"]), ("missing", POLICY["points"]["impactMissing"]))
        self.write(self.ad, "impact.json", {"status": "UNAVAILABLE", "symbols": []})
        self.assert_tier(self.run_cli("plan"), "yellow", "plan")
        ids = {s["id"]: s for s in self.doc("plan")["mechanical"]["signals"]}
        self.assertEqual(ids["impact"]["points"], POLICY["points"]["impactUnavailable"])
        self.write(self.ad, "impact.json", {"status": "NOPE"})
        self.assert_fail(self.run_cli("plan"), "impact.json", stage="plan")

    def test_callers_and_chain_links_over_max_add_points(self):
        self.baseline()
        many = [f"c{i}" for i in range(POLICY["sizeThresholds"]["max_d1_callers"] + 1)]
        self.write(self.ad, "impact.json", {"status": "GATHERED", "symbols": [gathered("X", "apps/api/src/notes/x.ts", many)]})
        self.write(self.ad, "causal-chain.json", {"links": [{"n": i} for i in range(POLICY["sizeThresholds"]["max_chain_links"] + 1)]})
        self.assert_tier(self.run_cli("plan"), "yellow", "plan")
        ids = {s["id"]: s for s in self.doc("plan")["mechanical"]["signals"]}
        self.assertEqual(ids["d1-callers"]["points"], POLICY["points"]["callersOverMax"])
        self.assertEqual(ids["chain-links"]["points"], POLICY["points"]["chainLinksOverMax"])

    def test_triage_L_adds_points_and_bad_triage_fails(self):
        self.baseline()
        self.write(self.ad, "triage.json", {"size": "L"})
        self.assert_tier(self.run_cli("plan"), "yellow", "plan")
        self.write(self.ad, "triage.json", {"size": "XL"})
        self.assert_fail(self.run_cli("plan"), "triage.json", stage="plan")

    def test_agent_judgment_joins_the_max_and_malformed_fails(self):
        self.baseline()
        self.write(self.ad, "risk-judgment.json", {"schema": rp.SCHEMA_JUDGMENT, "tier": "red",
                                                   "rationale": "touches the billing webhook path indirectly", "unknowns": ["retry semantics"]})
        self.assert_tier(self.run_cli("plan"), "red", "plan")
        d = self.doc("plan")
        self.assertEqual(d["agent"]["tier"], "red")
        self.assertEqual(d["mechanical"]["tier"], "green")  # disagreement recorded, not resolved
        self.write(self.ad, "risk-judgment.json", {"schema": rp.SCHEMA_JUDGMENT, "tier": "red"})
        self.assert_fail(self.run_cli("plan"), "risk-judgment.json", stage="plan")

    def test_prior_stage_tier_never_lowers(self):
        self.write_spec("# T\n\nKind: docs\n\nEdit apps/api/src/auth/a.md.\n")
        self.assert_tier(self.run_cli("intake"), "red", "intake")
        self.write_spec("# T\n\nKind: docs\n")
        self.baseline(files=["docs/a.md"])
        self.assert_tier(self.run_cli("plan"), "red", "plan")
        d = self.doc("plan")
        self.assertEqual(d["prior"], {"tier": "red", "stage": "intake", "handoffTier": None})
        self.assertEqual(d["mechanical"]["tier"], "green")

    def test_handoff_sets_prior_and_escalated_from(self):
        self.baseline()
        handoff = self.tmp / "escalation.json"
        handoff.write_text(json.dumps({"schema": rp.SCHEMA_ESCALATION, "fromLane": "sdlc-green", "toTier": "yellow",
                                       "stage": "plan", "runId": "run-123"}), encoding="utf-8")
        self.assert_tier(self.run_cli("plan", "--handoff", str(handoff)), "yellow", "plan")
        d = self.doc("plan")
        self.assertEqual(d["prior"], {"tier": "yellow", "stage": "plan", "handoffTier": "yellow"})
        self.assertEqual((d["escalated"], d["escalatedFrom"], d["handoffRunId"]), (True, "green", "run-123"))
        self.assertEqual(self.trajectory()[-1]["handoffRunId"], "run-123")
        handoff.write_text(json.dumps({"schema": "nope"}), encoding="utf-8")
        self.assert_fail(self.run_cli("plan", "--handoff", str(handoff)), "handoff", stage="plan")

    def test_prior_doc_missing_but_trajectory_has_the_stage_fails_closed(self):
        # risk-intake.json is gone but risk-trajectory.jsonl remembers intake
        # ran: that is an inconsistent artifacts directory, not "never ran".
        traj = self.ad / "risk-trajectory.jsonl"
        traj.write_text(json.dumps({"stage": "intake", "tier": "green"}) + "\n", encoding="utf-8")
        self.baseline()
        self.assert_fail(self.run_cli("plan"), "risk-intake.json", stage="plan")

    def test_prior_doc_genuinely_missing_with_no_trajectory_line_is_null(self):
        self.baseline()
        self.assert_tier(self.run_cli("plan"), "green", "plan")
        self.assertEqual(self.doc("plan")["prior"], {"tier": None, "stage": None, "handoffTier": None})

    def test_non_list_behavioral_probes_fail_closed(self):
        for bad in ("oops", {"covers": ["x"]}, [1, 2]):
            profile = dict(PROFILE)
            profile["evidence"] = {"behavioral": bad}
            self.profile.write_text(json.dumps(profile), encoding="utf-8")
            self.baseline()
            self.assert_fail(self.run_cli("plan"), "evidence.behavioral", stage="plan")

    def test_non_string_covers_entries_fail_closed(self):
        for bad in (5, [1, 2]):
            profile = dict(PROFILE)
            profile["evidence"] = {"behavioral": [{"covers": bad}]}
            self.profile.write_text(json.dumps(profile), encoding="utf-8")
            self.baseline()
            self.assert_fail(self.run_cli("plan"), "evidence.behavioral", stage="plan")

    def test_well_formed_probes_still_compute_coverage(self):
        profile = dict(PROFILE)
        profile["evidence"] = {"behavioral": [{"covers": ["apps/api/src/notes/notes.service.ts"]}]}
        self.profile.write_text(json.dumps(profile), encoding="utf-8")
        self.baseline()
        self.assert_tier(self.run_cli("plan"), "green", "plan")
        ids = {s["id"]: s for s in self.doc("plan")["mechanical"]["signals"]}
        self.assertEqual(ids["coverage"]["value"], [])

    def test_web_allowlist_without_web_repo_fails_closed(self):
        profile = dict(PROFILE)
        profile["capabilities"] = {"defaultRepo": "api"}  # no webRepo
        self.profile.write_text(json.dumps(profile), encoding="utf-8")
        self.baseline()
        self.write(self.ad, "web-files-allowlist.json", ["app/services/api-client.d.ts"])
        self.assert_fail(self.run_cli("plan"),
                         "web-files-allowlist.json present but profile has no capabilities.webRepo", stage="plan")

    def test_duplicate_allowlist_entries_count_once(self):
        self.baseline(files=["apps/api/src/notes/notes.service.ts"] * 5)
        self.assert_tier(self.run_cli("plan"), "green", "plan")
        ids = {s["id"]: s for s in self.doc("plan")["mechanical"]["signals"]}
        self.assertEqual(ids["files"]["value"], 1)

    def test_differently_spelled_duplicate_allowlist_entries_count_once(self):
        self.baseline(files=["apps/../apps/x.ts", "apps/x.ts"])
        self.assert_tier(self.run_cli("plan"), "green", "plan")
        ids = {s["id"]: s for s in self.doc("plan")["mechanical"]["signals"]}
        self.assertEqual(ids["files"]["value"], 1)

    def test_callers_value_is_the_max_across_symbols_not_the_sum(self):
        self.baseline()
        self.write(self.ad, "impact.json", {"status": "GATHERED", "symbols": [
            gathered("A", "apps/api/src/notes/a.ts", ["c1", "c2", "c3"]),
            gathered("B", "apps/api/src/notes/b.ts", ["c4"])]})
        self.assert_tier(self.run_cli("plan"), "green", "plan")
        ids = {s["id"]: s for s in self.doc("plan")["mechanical"]["signals"]}
        self.assertEqual(ids["d1-callers"]["value"], 3)


class Impl(Base):
    def setUp(self):
        super().setUp()
        (self.ad / "bootstrap-head.txt").write_text(self.base + "\n", encoding="utf-8")
        self.write(self.ad, "impact.json", {"status": "GATHERED", "symbols": [
            gathered("NotesService.list", "apps/api/src/notes/notes.service.ts", ["a"])]})

    def test_diff_since_bootstrap_head_is_the_footprint(self):
        self.commit_file("apps/api/src/notes/notes.service.ts", "x")
        self.commit_file("apps/api/src/notes/notes.service.spec.ts", "t")
        self.assert_tier(self.run_cli("impl"), "green", "impl")
        d = self.doc("impl")
        ids = {s["id"]: s for s in d["mechanical"]["signals"]}
        self.assertEqual(ids["files"]["value"], 1)
        self.assertEqual(ids["test-files"]["value"], 1)
        diff = git(self.root, "diff", "-z", "--name-status", f"{self.base}..HEAD")
        self.assertEqual(d["inputs"]["diffSha256"], rp.sha256_text(diff))
        self.assertEqual(d["inputs"]["head"], git(self.root, "rev-parse", "HEAD"))
        self.assertEqual(d["inputs"]["baseCommit"], self.base)
        self.assertIsNone(d["inputs"]["allowlistSha256"])

    def test_changed_diff_changes_the_digest_and_can_raise_the_tier(self):
        self.commit_file("docs/a.md", "x")
        self.assert_tier(self.run_cli("impl"), "green", "impl")
        first = self.doc("impl")["inputs"]["diffSha256"]
        self.commit_file("apps/api/src/auth/guard.ts", "y")
        self.assert_tier(self.run_cli("impl"), "red", "impl")
        d = self.doc("impl")
        self.assertNotEqual(first, d["inputs"]["diffSha256"])
        self.assertEqual([f["id"] for f in d["floors"]], ["sensitive-domain:auth"])

    def test_deleted_and_renamed_paths_count(self):
        self.commit_file("apps/api/src/billing/old.ts", "x")
        base = git(self.root, "rev-parse", "HEAD")
        (self.ad / "bootstrap-head.txt").write_text(base + "\n", encoding="utf-8")
        os.remove(self.root / "apps/api/src/billing/old.ts")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "rm")
        self.assert_tier(self.run_cli("impl"), "red", "impl")
        self.assertEqual(self.doc("impl")["floors"][0]["paths"], ["apps/api/src/billing/old.ts"])

    def test_missing_base_fails_closed(self):
        os.remove(self.ad / "bootstrap-head.txt")
        self.assert_fail(self.run_cli("impl"), "base", stage="impl")

    def test_unreadable_diff_fails_closed(self):
        self.assert_fail(self.run_cli("impl", "--base", "0" * 40), "base commit", stage="impl")
        self.assert_fail(self.run_cli("impl", "--repo-root", str(self.tmp / "not-a-repo")), "repo root", stage="impl")

    def test_prior_from_plan_and_triage_post(self):
        self.write(self.ad, "risk-plan.json", {"schema": rp.SCHEMA_SCORE, "stage": "plan", "tier": "yellow"})
        self.write(self.ad, "triage-post.json", {"size": "M"})
        self.commit_file("docs/a.md", "x")
        self.assert_tier(self.run_cli("impl"), "yellow", "impl")
        d = self.doc("impl")
        self.assertEqual(d["prior"], {"tier": "yellow", "stage": "plan", "handoffTier": None})
        ids = {s["id"]: s for s in d["mechanical"]["signals"]}
        self.assertEqual((ids["triage"]["value"], ids["triage"]["points"]), ("M", POLICY["points"]["triage"]["M"]))

    def test_handoff_raises_prior_above_the_previous_stage_doc(self):
        self.write(self.ad, "risk-plan.json", {"schema": rp.SCHEMA_SCORE, "stage": "plan", "tier": "green"})
        handoff = self.tmp / "escalation.json"
        handoff.write_text(json.dumps({"schema": rp.SCHEMA_ESCALATION, "fromLane": "sdlc-yellow", "toTier": "red",
                                       "stage": "impl", "runId": "run-9"}), encoding="utf-8")
        self.commit_file("docs/a.md", "x")
        self.assert_tier(self.run_cli("impl", "--handoff", str(handoff)), "red", "impl")
        d = self.doc("impl")
        self.assertEqual(d["prior"], {"tier": "red", "stage": "plan", "handoffTier": "red"})

    def test_prior_doc_with_wrong_stage_fails_closed(self):
        self.write(self.ad, "risk-plan.json", {"schema": rp.SCHEMA_SCORE, "stage": "intake", "tier": "yellow"})
        self.commit_file("docs/a.md", "x")
        self.assert_fail(self.run_cli("impl"), "risk-plan.json", stage="impl")

    def test_prior_doc_missing_but_trajectory_has_the_stage_fails_closed(self):
        traj = self.ad / "risk-trajectory.jsonl"
        traj.write_text(json.dumps({"stage": "plan", "tier": "yellow"}) + "\n", encoding="utf-8")
        self.commit_file("docs/a.md", "x")
        self.assert_fail(self.run_cli("impl"), "risk-plan.json", stage="impl")

    def test_empty_diff_fails_closed(self):
        self.assert_fail(self.run_cli("impl"), "impl diff is empty", stage="impl")

    def test_dirty_tracked_worktree_fails_closed(self):
        self.commit_file("apps/api/src/notes/notes.service.ts", "x")
        (self.root / "apps/api/src/notes/notes.service.ts").write_text("dirty", encoding="utf-8")
        self.assert_fail(self.run_cli("impl"), "uncommitted tracked changes", stage="impl")

    def test_untracked_files_do_not_block_scoring(self):
        self.commit_file("apps/api/src/notes/notes.service.ts", "x")
        (self.root / "scratch.txt").write_text("untracked", encoding="utf-8")
        self.assert_tier(self.run_cli("impl"), "green", "impl")

    def test_base_not_an_ancestor_fails_closed(self):
        git(self.root, "checkout", "-q", "-b", "side")
        self.commit_file("side.txt", "x")
        side = git(self.root, "rev-parse", "HEAD")
        git(self.root, "checkout", "-q", "main")
        self.commit_file("main.txt", "y")
        (self.ad / "bootstrap-head.txt").write_text(side + "\n", encoding="utf-8")
        self.assert_fail(self.run_cli("impl"), "base is not an ancestor of HEAD", stage="impl")

    def test_non_ascii_path_in_diff_is_not_quoted(self):
        self.commit_file("apps/api/src/auth/café.ts", "x")
        self.assert_tier(self.run_cli("impl"), "red", "impl")
        d = self.doc("impl")
        self.assertEqual(d["floors"][0]["paths"], ["apps/api/src/auth/café.ts"])


class SpecFixtures(Base):
    """Verification step 3 of the design spec, as executable fixtures."""

    def test_docs_only_spec_is_green(self):
        self.write_spec("# Clarify the README\n\nKind: docs\n\nUpdate README.md and docs/setup.md.\n")
        self.assert_tier(self.run_cli("intake"), "green", "intake")

    def test_spec_touching_auth_is_red_with_the_named_floor(self):
        self.write_spec("# Session refresh\n\nKind: feature\n\nChange apps/api/src/auth/session.service.ts.\n")
        last = self.assert_tier(self.run_cli("intake"), "red", "intake")
        self.assertTrue(last.endswith("floors=sensitive-domain:auth"), last)

    def test_spec_touching_an_oauth2_client_is_red_with_the_named_floor(self):
        self.write_spec("# OAuth2 client\n\nKind: feature\n\nChange src/OAuth2Client.ts.\n")
        last = self.assert_tier(self.run_cli("intake"), "red", "intake")
        self.assertTrue(last.endswith("floors=sensitive-domain:auth"), last)

    def test_same_with_codeowners_mapping_security_populates_owners_touched(self):
        self.commit_file("CODEOWNERS", "/apps/api/src/auth/  @org/security\n")
        self.write_spec("# Session refresh\n\nKind: feature\n\nChange apps/api/src/auth/session.service.ts.\n")
        self.assert_tier(self.run_cli("intake", "--base", git(self.root, "rev-parse", "HEAD")), "red", "intake")
        d = self.doc("intake")
        self.assertEqual(d["codeowners"]["ownersTouched"], ["@org/security"])
        self.assertIn("codeowners:@org/security", [f["id"] for f in d["floors"]])

    def test_lockfile_bump_is_yellow(self):
        self.write_spec("# Bump deps\n\nKind: chore\n\nRegenerate bun.lock.\n")
        last = self.assert_tier(self.run_cli("intake"), "yellow", "intake")
        self.assertTrue(last.endswith("floors=lockfile"), last)


if __name__ == "__main__":
    unittest.main()
