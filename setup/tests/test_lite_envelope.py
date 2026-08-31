#!/usr/bin/env python3
"""Tests for lite-envelope.sh — the lite-lane routing gate.

Every check gets a passing baseline and one failing fixture that flips exactly
that check, so a regression in any single guard shows up as one named failure.
Fixtures are built in a temp dir per test; the shipped lite-envelope.json is the
threshold source, so the tests read it rather than restating numbers."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parent.parent
SCRIPT = SETUP / "lite-envelope.sh"
ENVELOPE = json.loads((SETUP / "lite-envelope.json").read_text(encoding="utf-8"))


def write(ad, name, obj):
    (ad / name).write_text(json.dumps(obj) if not isinstance(obj, str) else obj, encoding="utf-8")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "root"
        self.ad = self.tmp / "artifacts"
        self.root.mkdir()
        self.ad.mkdir()
        self.spec = self.tmp / "spec.md"
        self.spec.write_text("# A small ticket\n\nDo one thing.\n", encoding="utf-8")
        write(self.ad, "params.json", {"spec": str(self.spec), "slug": "x", "branch": "archon/x", "worktree": str(self.root / "api/.worktrees/x")})

    def run_gate(self, lane, stage):
        r = subprocess.run(["bash", str(SCRIPT), str(self.root), str(self.ad), lane, stage],
                           capture_output=True, encoding="utf-8")
        return r

    def assert_lite(self, r):
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(r.stdout.rstrip().endswith("ROUTE=LITE"), r.stdout)

    def assert_full(self, r, reason):
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        last = r.stdout.rstrip().splitlines()[-1]
        self.assertTrue(last.startswith(f"ROUTE=FULL reason={reason}"), r.stdout)
        # exactly one ROUTE line
        self.assertEqual(sum(1 for l in r.stdout.splitlines() if l.startswith("ROUTE=")), 1, r.stdout)

    def assert_durable(self, r, stage):
        f = self.ad / f"envelope-{stage}.txt"
        self.assertTrue(f.is_file(), "envelope file not written")
        self.assertEqual(f.read_text(encoding="utf-8").rstrip(), r.stdout.rstrip())

    # --- api baseline ---------------------------------------------------
    def api_plan_baseline(self, stage="plan"):
        write(self.ad, "files-allowlist.json", ["apps/api/src/notes/notes.service.ts", "apps/api/src/notes/notes.service.spec.ts"])
        write(self.ad, "reader-audit.json", {"columns": []})
        write(self.ad, "impact.json", {"status": "GATHERED", "symbols": [
            {"name": "NotesService.list", "file": "apps/api/src/notes/notes.service.ts", "d1_callers": ["a", "b", "c"], "risk": "LOW"}]})
        write(self.ad, "triage-post.json" if stage == "post" else "triage.json",
              {"size": "S", "reasons": ["one mechanism"], "hot_path_hits": [], "unknowns": []})

    # --- bugfix baselines -----------------------------------------------
    def bugfix_pre_baseline(self):
        write(self.ad, "evidence-plan.json", {
            "identifiers": [], "time_window": None, "error_strings": [], "sentry_refs": [], "linear_refs": [],
            "repo_hint": "api", "local_repro_steps": None,
            "repro_command": "bun run test -- apps/api/src/notes/notes.service.spec.ts",
            "repro_observed": "expected 2 received 3 (1 failed, 12 passed)"})
        write(self.ad, "triage.json", {"size": "S", "reasons": [], "hot_path_hits": [], "unknowns": []})

    def bugfix_plan_baseline(self, stage="plan"):
        write(self.ad, "repo.json", {"repo": "api", "rationale": "stack trace"})
        write(self.ad, "fix-plan.json", {"approach": "dedupe", "fix_site": "libs/business-logic/src/lib/services/meeting-notes-sync.service.ts:449",
                                        "files": ["libs/business-logic/src/lib/services/meeting-notes-sync.service.ts"],
                                        "risks": [], "alternatives": []})
        write(self.ad, "files-allowlist.json", ["libs/business-logic/src/lib/services/meeting-notes-sync.service.ts",
                                                "libs/business-logic/src/lib/services/__tests__/meeting-notes-sync.service.spec.ts"])
        write(self.ad, "causal-chain.json", {"links": [{"index": 1, "cause": "a"}, {"index": 2, "cause": "b"}, {"index": 3, "cause": "c", "fixable": True}]})
        write(self.ad, "impact.json", {"status": "GATHERED", "symbols": [
            {"name": "upsertSyntheticCalendarEvent", "file": "x.ts", "d1_callers": ["processMeeting"], "risk": "LOW"}]})
        write(self.ad, "triage-post.json" if stage == "post" else "triage.json",
              {"size": "S", "reasons": [], "hot_path_hits": [], "unknowns": []})


class ApiPlanStage(Base):
    def test_baseline_lite(self):
        self.api_plan_baseline()
        r = self.run_gate("api", "plan")
        self.assert_lite(r)
        self.assert_durable(r, "plan")
        for tag in ("premises=none", "triage=S", "files=1/", "test_files=1/", "hot_paths=0", "reader_audit=0", "impact=GATHERED", "d1_callers=3/"):
            self.assertIn(f"ENVELOPE {tag}", r.stdout, r.stdout)

    def test_post_stage_reads_triage_post_only(self):
        self.api_plan_baseline(stage="post")
        # a stale S in triage.json must not rescue a missing triage-post.json
        r = self.run_gate("api", "post")
        self.assert_lite(r)
        self.assert_durable(r, "post")
        os.remove(self.ad / "triage-post.json")
        write(self.ad, "triage.json", {"size": "S", "reasons": [], "hot_path_hits": [], "unknowns": []})
        r = self.run_gate("api", "post")
        self.assert_full(r, "malformed")
        self.assertIn("triage-post.json missing", r.stdout)
        self.assert_durable(r, "post")

    def test_premises_route_full(self):
        self.api_plan_baseline()
        self.spec.write_text("# t\n\n## Premises to verify\n\n- x\n", encoding="utf-8")
        self.assert_full(self.run_gate("api", "plan"), "premises")

    def test_triage_L(self):
        self.api_plan_baseline()
        write(self.ad, "triage.json", {"size": "L", "reasons": ["migration"], "hot_path_hits": [], "unknowns": []})
        self.assert_full(self.run_gate("api", "plan"), "triage")

    def test_triage_M_without_override(self):
        self.api_plan_baseline()
        write(self.ad, "triage.json", {"size": "M", "reasons": [], "hot_path_hits": [], "unknowns": []})
        self.assert_full(self.run_gate("api", "plan"), "triage")

    def test_triage_M_with_override_passes(self):
        self.api_plan_baseline()
        write(self.ad, "triage.json", {"size": "M", "reasons": [], "hot_path_hits": [], "unknowns": []})
        self.spec.write_text("# t\n\nLane: lite-ok\n\nbody\n", encoding="utf-8")
        r = self.run_gate("api", "plan")
        self.assert_lite(r)
        self.assertIn("ENVELOPE triage=M override=lite-ok OK", r.stdout)

    def test_triage_L_ignores_override(self):
        self.api_plan_baseline()
        write(self.ad, "triage.json", {"size": "L", "reasons": [], "hot_path_hits": [], "unknowns": []})
        self.spec.write_text("Lane: lite-ok\n", encoding="utf-8")
        self.assert_full(self.run_gate("api", "plan"), "triage")

    def test_missing_triage_fails_closed(self):
        self.api_plan_baseline()
        os.remove(self.ad / "triage.json")
        self.assert_full(self.run_gate("api", "plan"), "malformed")

    def test_files_over_cap(self):
        self.api_plan_baseline()
        n = ENVELOPE["max_files"] + 1
        write(self.ad, "files-allowlist.json", [f"apps/api/src/notes/f{i}.ts" for i in range(n)])
        r = self.run_gate("api", "plan")
        self.assert_full(r, "files")
        self.assertIn(f"{n}/{ENVELOPE['max_files']}", r.stdout)

    def test_test_files_over_cap_but_code_under(self):
        self.api_plan_baseline()
        n = ENVELOPE["max_test_files"] + 1
        write(self.ad, "files-allowlist.json", ["apps/api/src/notes/a.ts"] + [f"apps/api/src/notes/__tests__/t{i}.ts" for i in range(n)])
        self.assert_full(self.run_gate("api", "plan"), "test_files")

    def test_hot_path_directory_prefix(self):
        self.api_plan_baseline()
        write(self.ad, "files-allowlist.json", ["libs/data-access/src/lib/rds/migrations/1700000000000-x.ts"])
        r = self.run_gate("api", "plan")
        self.assert_full(r, "hot_paths")
        self.assertIn("api/libs/data-access/src/lib/rds/migrations/", r.stdout)

    def test_hot_path_prefix_is_anchored(self):
        # a path that merely CONTAINS a hot segment but does not start with it passes
        self.api_plan_baseline()
        write(self.ad, "files-allowlist.json", ["apps/api/src/notes/auth-helper.ts"])
        self.assert_lite(self.run_gate("api", "plan"))

    def test_hot_path_exact_file_only_matches_that_file(self):
        # the exact-file hot path is web-app-side, so this runs the bugfix lane with repo=web-app
        self.bugfix_plan_baseline()
        write(self.ad, "repo.json", {"repo": "web-app", "rationale": ""})
        write(self.ad, "fix-plan.json", {"approach": "", "fix_site": "", "files": ["app/services/api-client.d.ts"], "risks": [], "alternatives": []})
        os.remove(self.ad / "files-allowlist.json")
        self.assert_full(self.run_gate("bugfix", "plan"), "hot_paths")
        write(self.ad, "fix-plan.json", {"approach": "", "fix_site": "", "files": ["app/services/api-client.d.ts.bak"], "risks": [], "alternatives": []})
        self.assert_lite(self.run_gate("bugfix", "plan"))

    def test_path_normalisation(self):
        self.api_plan_baseline()
        write(self.ad, "files-allowlist.json", ["./libs//data-access/src/lib/rds/entities/profile.entity.ts"])
        self.assert_full(self.run_gate("api", "plan"), "hot_paths")
        write(self.ad, "files-allowlist.json", ["../web-app/app/x.ts"])
        self.assert_full(self.run_gate("api", "plan"), "malformed")

    def test_rooted_allowlist_entries_are_malformed_not_bypass(self):
        # F2: a repo-prefixed or absolute entry must never dodge hot_paths
        self.api_plan_baseline()
        for bad in ("api/apps/api/src/auth/x.ts", "/Users/x/Goodword/api/apps/api/src/auth/x.ts", "web-app/app/services/api-client.d.ts", "./api/apps/api/src/auth/x.ts", ".//api/apps/api/src/auth/x.ts", "././api/apps/api/src/auth/x.ts", "//api/apps/api/src/auth/x.ts", "API/apps/api/src/auth/x.ts", "Web-App/app/x.ts", "./", "."):
            write(self.ad, "files-allowlist.json", [bad])
            r = self.run_gate("api", "plan")
            self.assert_full(r, "malformed")
            self.assertTrue("repo-relative" in r.stdout or "empty after normalisation" in r.stdout, r.stdout)
            self.assertNotIn("hot_paths=0 OK", r.stdout)

    def test_all_failing_checks_are_reported(self):
        self.api_plan_baseline()
        write(self.ad, "triage.json", {"size": "L", "reasons": [], "hot_path_hits": [], "unknowns": []})
        write(self.ad, "files-allowlist.json", ["apps/api/src/auth/x.ts"])
        r = self.run_gate("api", "plan")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ENVELOPE triage=FAIL", r.stdout)
        self.assertIn("ENVELOPE hot_paths=FAIL", r.stdout)
        self.assertTrue(r.stdout.rstrip().endswith("ROUTE=FULL reason=triage,hot_paths"), r.stdout)

    def test_reader_audit_columns_route_full(self):
        self.api_plan_baseline()
        write(self.ad, "reader-audit.json", {"columns": [{"table": "profiles", "column": "email"}]})
        self.assert_full(self.run_gate("api", "plan"), "reader_audit")

    def test_impact_unavailable_fails_closed(self):
        self.api_plan_baseline()
        write(self.ad, "impact.json", {"status": "UNAVAILABLE", "symbols": []})
        self.assert_full(self.run_gate("api", "plan"), "impact")

    def test_impact_skipped_passes_with_zero(self):
        self.api_plan_baseline()
        write(self.ad, "impact.json", {"status": "SKIPPED", "symbols": []})
        r = self.run_gate("api", "plan")
        self.assert_lite(r)
        self.assertIn("ENVELOPE d1_callers=0/", r.stdout)

    def test_d1_callers_over_cap(self):
        self.api_plan_baseline()
        n = ENVELOPE["max_d1_callers"] + 1
        write(self.ad, "impact.json", {"status": "GATHERED", "symbols": [{"name": "x", "file": "f", "d1_callers": [str(i) for i in range(n)], "risk": "HIGH"}]})
        self.assert_full(self.run_gate("api", "plan"), "d1_callers")

    def test_malformed_impact_json(self):
        self.api_plan_baseline()
        write(self.ad, "impact.json", "{not json")
        self.assert_full(self.run_gate("api", "plan"), "malformed")

    def test_api_has_no_pre_stage(self):
        self.api_plan_baseline()
        self.assert_full(self.run_gate("api", "pre"), "malformed")

    def test_junk_threshold_fails_closed(self):
        self.api_plan_baseline()
        bad = self.tmp / "bad-envelope.json"
        bad.write_text(json.dumps({**ENVELOPE, "max_files": "4"}), encoding="utf-8")
        r = subprocess.run(["bash", str(SCRIPT), str(self.root), str(self.ad), "api", "plan"],
                           capture_output=True, encoding="utf-8", env={**os.environ, "LITE_ENVELOPE_JSON": str(bad)})
        self.assert_full(r, "malformed")


class BugfixPreStage(Base):
    def test_baseline_lite(self):
        self.bugfix_pre_baseline()
        r = self.run_gate("bugfix", "pre")
        self.assert_lite(r)
        self.assert_durable(r, "pre")
        for tag in ("repro=given", "repo_hint=api", "triage=S"):
            self.assertIn(f"ENVELOPE {tag}", r.stdout)

    def test_missing_repro_command(self):
        self.bugfix_pre_baseline()
        ep = json.loads((self.ad / "evidence-plan.json").read_text())
        ep["repro_command"] = None
        write(self.ad, "evidence-plan.json", ep)
        self.assert_full(self.run_gate("bugfix", "pre"), "repro")

    def test_repro_prefix_not_allowed(self):
        self.bugfix_pre_baseline()
        ep = json.loads((self.ad / "evidence-plan.json").read_text())
        ep["repro_command"] = "bun test apps/api/src/x.spec.ts"  # bare bun test shadows jest
        write(self.ad, "evidence-plan.json", ep)
        self.assert_full(self.run_gate("bugfix", "pre"), "repro")

    def test_repro_metacharacter(self):
        self.bugfix_pre_baseline()
        ep = json.loads((self.ad / "evidence-plan.json").read_text())
        ep["repro_command"] = "bun run test -- x.spec.ts; rm -rf /"
        write(self.ad, "evidence-plan.json", ep)
        self.assert_full(self.run_gate("bugfix", "pre"), "repro")

    def test_repro_observed_required(self):
        self.bugfix_pre_baseline()
        ep = json.loads((self.ad / "evidence-plan.json").read_text())
        ep["repro_observed"] = ""
        write(self.ad, "evidence-plan.json", ep)
        self.assert_full(self.run_gate("bugfix", "pre"), "repro")

    def test_repo_hint_unknown(self):
        self.bugfix_pre_baseline()
        ep = json.loads((self.ad / "evidence-plan.json").read_text())
        ep["repo_hint"] = "unknown"
        write(self.ad, "evidence-plan.json", ep)
        self.assert_full(self.run_gate("bugfix", "pre"), "repo_hint")

    def test_triage_L_at_pre(self):
        self.bugfix_pre_baseline()
        write(self.ad, "triage.json", {"size": "L", "reasons": ["authorization"], "hot_path_hits": [], "unknowns": []})
        self.assert_full(self.run_gate("bugfix", "pre"), "triage")


class BugfixPlanStage(Base):
    def test_baseline_lite(self):
        self.bugfix_plan_baseline()
        r = self.run_gate("bugfix", "plan")
        self.assert_lite(r)
        self.assert_durable(r, "plan")
        for tag in ("triage=S", "repo=api", "files=1/", "test_files=1/", "hot_paths=0", "chain_links=3/", "impact=GATHERED", "d1_callers=1/"):
            self.assertIn(f"ENVELOPE {tag}", r.stdout, r.stdout)

    def test_post_requires_triage_post(self):
        self.bugfix_plan_baseline(stage="post")
        self.assert_lite(self.run_gate("bugfix", "post"))
        os.remove(self.ad / "triage-post.json")
        self.assert_full(self.run_gate("bugfix", "post"), "malformed")

    def test_repo_both(self):
        self.bugfix_plan_baseline()
        write(self.ad, "repo.json", {"repo": "both", "rationale": ""})
        self.assert_full(self.run_gate("bugfix", "plan"), "repo")

    def test_chain_links_over_cap(self):
        self.bugfix_plan_baseline()
        n = ENVELOPE["max_chain_links"] + 1
        write(self.ad, "causal-chain.json", {"links": [{"index": i, "cause": "c"} for i in range(n)]})
        self.assert_full(self.run_gate("bugfix", "plan"), "chain_links")

    def test_fix_plan_and_allowlist_are_unioned(self):
        self.bugfix_plan_baseline()
        n = ENVELOPE["max_files"]
        write(self.ad, "fix-plan.json", {"approach": "", "fix_site": "", "files": [f"libs/a{i}.ts" for i in range(n)], "risks": [], "alternatives": []})
        write(self.ad, "files-allowlist.json", ["libs/extra.ts"])
        self.assert_full(self.run_gate("bugfix", "plan"), "files")

    def test_hot_path_integration_service(self):
        self.bugfix_plan_baseline()
        write(self.ad, "fix-plan.json", {"approach": "", "fix_site": "", "files": ["apps/integration-service/src/functions/sync-meeting-notes-handler.ts"], "risks": [], "alternatives": []})
        os.remove(self.ad / "files-allowlist.json")
        self.assert_full(self.run_gate("bugfix", "plan"), "hot_paths")


if __name__ == "__main__":
    unittest.main()
