#!/usr/bin/env python3
"""setup/trace-digest.py: one run's artifacts tree becomes a portable, typed
archon.trace-digest.v1 document; facts are read, never inferred."""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parent.parent
SCRIPT = SETUP / "trace-digest.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "skill-evolve"

sys.path.insert(0, str(SETUP))
import skill_library as sl  # noqa: E402

_spec = importlib.util.spec_from_file_location("trace_digest", SCRIPT)
td = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(td)


def run(args, cwd=None):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True,
                          encoding="utf-8", cwd=cwd)


def write(root, rel, content):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, (dict, list)):
        content = json.dumps(content, indent=2, sort_keys=True) + "\n"
    p.write_text(content, encoding="utf-8")


PARAMS = {"spec": "specs/x.md", "slug": "x", "branch": "archon/x", "worktree": "wt/x", "repo": "api"}


class Base(unittest.TestCase):
    def setUp(self):
        self.ad = Path(tempfile.mkdtemp(prefix="td-")) / "run-abc"
        self.ad.mkdir()
        self.addCleanup(shutil.rmtree, self.ad.parent, ignore_errors=True)
        write(self.ad, "params.json", PARAMS)

    def digest(self):
        return td.digest(str(self.ad))

    def fixer(self, applied=(), incomplete=(), advisory=(), failed=()):
        return {"applied": [{"finding": f, "action": "a", "severity": s} for f, s in applied],
                "incomplete": [{"finding": f, "action": "a", "severity": "P2"} for f in incomplete],
                "advisory": [{"finding": f, "action": "a", "severity": "P3"} for f in advisory],
                "failed": [{"finding": f, "action": "a"} for f in failed], "cross_repo": []}


class Terminal(Base):
    def test_incomplete_when_nothing_typed(self):
        d = self.digest()
        self.assertEqual((d["terminal"], d["failed_at"], d["lane"]), ("incomplete", None, "unknown"))

    def test_completed_by_pr_url(self):
        write(self.ad, "pr-url.txt", "https://example.invalid/pr/7\n")
        d = self.digest()
        self.assertEqual((d["terminal"], d["pr_url"]), ("completed", "https://example.invalid/pr/7"))

    def test_completed_by_local_candidate_verification(self):
        write(self.ad, "feature-result.json", {"outcome": "CHANGED", "verification": {"status": "passed"}})
        self.assertEqual(self.digest()["terminal"], "completed")

    def test_completed_by_kb_capture_gate_pass(self):
        write(self.ad, "node-kb-capture-gate.out", "KB_CAPTURE_GATE=PASS\n")
        self.assertEqual(self.digest()["terminal"], "completed")

    def test_failed_by_fail_terminal_without_completion(self):
        write(self.ad, "node-exit-gate.out", "EXIT_GATE=FAIL round=1\n")
        d = self.digest()
        self.assertEqual((d["terminal"], d["failed_at"]), ("failed", "exit-gate"))
        self.assertEqual(d["typed"]["fail_terminals"], ["EXIT_GATE"])

    def test_completion_marker_beats_a_stale_fail_line(self):
        write(self.ad, "node-smoke.out", "SMOKE=FAIL boot\n")
        write(self.ad, "node-kb-capture-gate.out", "KB_CAPTURE_GATE=PASS\n")
        d = self.digest()
        self.assertEqual(d["terminal"], "completed")
        self.assertEqual(d["typed"]["fail_terminals"], ["SMOKE"])

    def test_no_change_from_feature_result(self):
        write(self.ad, "feature-result.json", {"outcome": "NO_CHANGE", "pr_url": None})
        write(self.ad, "node-kb-capture-gate.out", "KB_CAPTURE_GATE=PASS\n")
        d = self.digest()
        self.assertEqual((d["terminal"], d["outcome"]), ("no_change", "NO_CHANGE"))

    def test_no_change_from_closure_intent(self):
        write(self.ad, "no-change-closure-intent.json", {"lifecycleResult": "fulfilled-no-change"})
        self.assertEqual(self.digest()["terminal"], "no_change")

    def test_converge_txt_is_a_typed_source(self):
        write(self.ad, "round.txt", "1\n")
        write(self.ad, "round-1/converge.txt", "ROUND=1 verdict=[Not ready]\nNO_PROGRESS round=1\n")
        d = self.digest()
        self.assertEqual((d["terminal"], d["failed_at"]), ("failed", "round-1/converge"))
        self.assertEqual(d["review"]["exit"], "NO_PROGRESS")
        self.assertEqual(d["typed"]["fail_tokens"], {"NO_PROGRESS": 1})


class Review(Base):
    def test_per_round_counts_and_reraised(self):
        write(self.ad, "round.txt", "3\n")
        write(self.ad, "round-1/review-summary.json", {"verdict": "Not ready", "residual_count": -1, "degraded": True})
        write(self.ad, "round-1/fixer-result.json",
              self.fixer(applied=(("Cache key omits tenant", "P0"), ("Missing test", "P2"), ("No severity", None)),
                         incomplete=("Snapshot",), advisory=("Rename",), failed=()))
        write(self.ad, "round-2/review-summary.json", {"verdict": "Ready with fixes", "residual_count": 1, "degraded": False})
        # same finding, different punctuation/case: the key normalisation catches it
        write(self.ad, "round-2/fixer-result.json",
              self.fixer(applied=(("cache key omits TENANT!", "P1"), ("Brand new", "P3"))))
        write(self.ad, "round-2/converge.txt", "CONVERGED round=2\n")
        write(self.ad, "round-3/fixer-result.json", self.fixer(applied=(("Brand new", "P3"),)))
        d = self.digest()
        r = d["review"]
        self.assertEqual(r["rounds"], 3)
        self.assertEqual(r["exit"], "CONVERGED")  # round 3 has no converge line; last known wins
        r1, r2, r3 = r["per_round"]
        self.assertEqual((r1["round"], r1["verdict"], r1["residual_count"], r1["degraded"]), (1, "Not ready", -1, True))
        self.assertEqual(r1["applied_by_severity"], {"P0": 2, "P1": 0, "P2": 1, "P3": 0})  # omitted -> P0
        self.assertEqual((r1["applied"], r1["incomplete"], r1["advisory"], r1["failed"], r1["reraised"]), (3, 1, 1, 0, 0))
        self.assertEqual((r2["reraised"], r2["applied_by_severity"]["P1"], r2["converge_line"]), (1, 1, "CONVERGED round=2"))
        self.assertEqual((r3["verdict"], r3["reraised"], r3["converge_line"]), (None, 1, None))

    def test_round_dirs_count_when_counter_missing(self):
        write(self.ad, "round-1/review-summary.json", {"verdict": "Not ready", "residual_count": -1, "degraded": False})
        write(self.ad, "round-2/review-summary.json", {"verdict": "Ready to merge", "residual_count": 0, "degraded": False})
        self.assertEqual(self.digest()["review"]["rounds"], 2)

    def test_key_matches_review_yield(self):
        spec = importlib.util.spec_from_file_location("review_yield", SETUP / "review-yield.py")
        ry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ry)
        for s in ("Cache-Key omits  TENANT!", "x" * 200, "  a  b  "):
            self.assertEqual(td._key(s), ry._key(s))

    def test_unreadable_round_json_is_fail(self):
        write(self.ad, "round-1/fixer-result.json", "{oops")
        with self.assertRaises(td.Fail) as cm:
            self.digest()
        self.assertIn("round-1/fixer-result.json is not JSON", str(cm.exception))


class Plan(Base):
    def test_feature_plan_rounds_and_blocking(self):
        write(self.ad, "plan.md", "# plan\n")
        write(self.ad, "plan-round.txt", "2\n")
        write(self.ad, "plan-round-1/critique.json", {"verdict": "REVISE", "findings": [
            {"severity": "P0", "confidence": 100}, {"severity": "P1", "confidence": 50}, {"severity": "P2", "confidence": 100}]})
        write(self.ad, "plan-round-1/converge.txt", "PLAN_ROUND_PROGRESSED round=1\n")
        write(self.ad, "plan-round-2/critique.json", {"verdict": "ACCEPT", "findings": []})
        write(self.ad, "plan-round-2/converge.txt", "PLAN_CONVERGED round=2\n")
        d = self.digest()
        self.assertEqual(d["lane"], "feature")
        self.assertEqual(d["plan"], {"prefix": "plan-round", "rounds": 2, "exit": "PLAN_CONVERGED", "critique_blocking": 1})
        self.assertIsNone(d["rca"])

    def test_bugfix_uses_rca_rounds(self):
        write(self.ad, "rca.md", "# rca\n")
        write(self.ad, "plan.md", "# also present\n")
        write(self.ad, "rca-round.txt", "1\n")
        write(self.ad, "rca-round-1/critique.json", {"verdict": "ACCEPT", "findings": []})
        write(self.ad, "rca-round-1/converge.txt", "RCA_PLAN_CONVERGED round=1\n")
        d = self.digest()
        self.assertEqual(d["lane"], "bugfix")
        self.assertEqual(d["plan"]["prefix"], "rca-round")
        self.assertEqual(d["rca"], {"present": True, "rounds": 1, "exit": "RCA_PLAN_CONVERGED", "critique_blocking": 0})

    def test_rca_plan_fail_token_counted(self):
        write(self.ad, "rca.md", "# rca\n")
        write(self.ad, "rca-round-1/converge.txt", "RCA_PLAN_ROUND_CAP round=3 cap=3\n")
        d = self.digest()
        self.assertEqual(d["typed"]["fail_tokens"], {"RCA_PLAN_ROUND_CAP": 1})
        self.assertEqual(d["terminal"], "failed")


class Typed(Base):
    def test_tokens_warns_and_nodes(self):
        write(self.ad, "node-a.out", "A_GATE=WARN slow\nFIXER_BLOCKED round=1\nFIXER_BLOCKED round=2\nA_GATE=OK\n")
        write(self.ad, "node-b.out", "not typed at all\n")
        write(self.ad, "node-c.out", "C_GATE=WARN\nSCOPE_BREACH round=1\n")
        d = self.digest()
        t = d["typed"]
        self.assertEqual(t["fail_tokens"], {"FIXER_BLOCKED": 2, "SCOPE_BREACH": 1})
        self.assertEqual(t["warn_lines"], 2)
        self.assertEqual(t["fail_terminals"], ["SCOPE_BREACH"])
        self.assertEqual(t["nodes"]["a"], {"last": "A_GATE=OK", "pass": True})
        self.assertEqual(t["nodes"]["b"], {"last": None, "pass": None})
        self.assertEqual(t["nodes"]["c"], {"last": "SCOPE_BREACH round=1", "pass": False})
        self.assertEqual(d["failed_at"], "c")

    def test_deslop_counters(self):
        write(self.ad, "deslop-round.txt", "2\n")
        write(self.ad, "deslop-dirty.txt", "1\n")
        write(self.ad, "node-deslop-recheck.out", "DESLOP_GATE=FAIL slop round=1\nDESLOP_GATE=PASS files=1 round=2 checkpoint=t\n")
        d = self.digest()
        self.assertEqual(d["deslop"], {"rounds": 2, "dirty_rounds": 1, "slop_fail": True})

    def test_deslop_dirty_counted_from_lines_when_counter_absent(self):
        write(self.ad, "deslop-round-1/recheck.json", {})
        write(self.ad, "node-deslop-review-gate.out", "DESLOP=DIRTY round=1 blocking=2\n")
        self.assertEqual(self.digest()["deslop"], {"rounds": 1, "dirty_rounds": 1, "slop_fail": False})

    def test_waivers(self):
        write(self.ad, "waivers.json", {"schema": "archon.waiver-ledger.v1",
                                        "entries": [{"finding": "Rename it"}, {"finding": "Split it"}, {"no": "finding"}]})
        self.assertEqual(self.digest()["waivers"], {"count": 3, "findings": ["Rename it", "Split it"]})


class RepoAndLane(Base):
    def test_web_repo_from_node_names_when_params_has_no_repo(self):
        write(self.ad, "params.json", {k: v for k, v in PARAMS.items() if k != "repo"})
        write(self.ad, "node-web-scope.out", "WEB_SCOPE=OK\n")
        self.assertEqual(self.digest()["repo"], "web-app")

    def test_api_default_without_repo_or_web_nodes(self):
        write(self.ad, "params.json", {k: v for k, v in PARAMS.items() if k != "repo"})
        self.assertEqual(self.digest()["repo"], "api")

    def test_params_repo_wins(self):
        write(self.ad, "params.json", dict(PARAMS, repo="goodword-mcp"))
        write(self.ad, "node-web-scope.out", "WEB_SCOPE=OK\n")
        d = self.digest()
        self.assertEqual((d["repo"], d["spec"], d["slug"], d["branch"]), ("goodword-mcp", "x.md", "x", "archon/x"))


class Cli(Base):
    def test_fail_on_missing_dir(self):
        r = run([str(self.ad / "nope")])
        self.assertEqual(r.returncode, 1)
        self.assertTrue(r.stdout.startswith("TRACE_DIGEST=FAIL artifacts dir missing"), r.stdout)

    def test_fail_on_missing_params(self):
        os.remove(self.ad / "params.json")
        r = run([str(self.ad)])
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout.strip(), "TRACE_DIGEST=FAIL params.json missing")

    def test_fail_on_bad_params(self):
        write(self.ad, "params.json", "[]")
        r = run([str(self.ad)])
        self.assertEqual(r.stdout.strip(), "TRACE_DIGEST=FAIL params.json is not an object")

    def test_without_out_writes_nothing(self):
        before = sorted(os.listdir(self.ad))
        r = run([str(self.ad)])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.strip(), "TRACE_DIGEST=OK run=run-abc lane=unknown terminal=incomplete rounds=0 score_inputs=0")
        self.assertEqual(sorted(os.listdir(self.ad)), before)

    def test_out_is_written_and_byte_identical_twice(self):
        out = self.ad.parent / "d.json"
        write(self.ad, "round.txt", "2\n")
        write(self.ad, "round-1/fixer-result.json", self.fixer(applied=(("f", "P1"),)))
        write(self.ad, "waivers.json", {"entries": [{"finding": "w"}]})
        write(self.ad, "node-exit-gate.out", "EXIT_GATE=FAIL\n")
        a = run([str(self.ad), "--out", str(out)])
        first = out.read_bytes()
        b = run([str(self.ad), "--out", str(out)])
        self.assertEqual((a.returncode, a.stdout), (0, b.stdout))
        self.assertEqual(first, out.read_bytes())
        # review rounds beyond first, applied, waivers, terminal failed, other fail terminal = 5
        self.assertEqual(a.stdout.strip(), "TRACE_DIGEST=OK run=run-abc lane=unknown terminal=failed rounds=2 score_inputs=5")
        doc = json.loads(first)
        self.assertEqual(doc["schema"], sl.SCHEMA_DIGEST)
        self.assertEqual(doc["artifacts_dir"], str(self.ad))

    def test_no_absolute_paths_beyond_artifacts_dir(self):
        write(self.ad, "plan.md", "# plan\n")
        write(self.ad, "round-1/fixer-result.json", self.fixer(applied=(("f", "P1"),)))
        doc = self.digest()
        doc.pop("artifacts_dir")
        text = json.dumps(doc)
        self.assertNotIn(str(self.ad.parent), text)
        self.assertNotIn(sl.ABS_HOME_MARKER, text)
        self.assertIn("round-1/fixer-result.json", doc["files_present"])
        self.assertIn("plan.md", doc["files_present"])

    def test_files_present_skips_traces_kb_and_tars(self):
        write(self.ad, "round-1/traces/x.json", {})
        write(self.ad, "round-1/kb/y.md", "k")
        write(self.ad, "round-1/checkpoint.tar", "t")
        write(self.ad, "round-1/keep.txt", "k")
        write(self.ad, "worktree.tar", "t")
        write(self.ad, "traces/z.json", {})
        fp = self.digest()["files_present"]
        self.assertEqual(fp, ["params.json", "round-1/keep.txt"])

    def test_shipped_file_carries_no_machine_paths(self):
        text = SCRIPT.read_text()
        self.assertNotIn(sl.ABS_HOME_MARKER, text)
        self.assertIsNone(re.search(r"[A-Za-z0-9+/=]{60,}", text))
        imports = set(re.findall(r"^(?:import|from) (\w+)", text, re.M))
        self.assertTrue(imports <= {"argparse", "json", "os", "re", "sys", "skill_library"}, imports)


class Fixtures(unittest.TestCase):
    def test_run_completed(self):
        d = td.digest(str(FIXTURES / "run-completed"))
        self.assertEqual((d["run_id"], d["repo"], d["lane"], d["terminal"]), ("run-completed", "api", "feature", "completed"))
        self.assertEqual((d["review"]["rounds"], d["review"]["exit"]), (2, "CONVERGED"))
        self.assertEqual(d["plan"], {"prefix": "plan-round", "rounds": 1, "exit": "PLAN_CONVERGED", "critique_blocking": 0})
        r1, r2 = d["review"]["per_round"]
        self.assertEqual((r1["applied_by_severity"]["P1"], r1["applied_by_severity"]["P2"], r1["incomplete"], r1["advisory"]), (1, 1, 1, 1))
        self.assertEqual((r2["reraised"], r2["applied"], r2["incomplete"]), (1, 2, 0))
        self.assertEqual(d["waivers"]["count"], 1)
        self.assertEqual(d["deslop"], {"rounds": 1, "dirty_rounds": 0, "slop_fail": False})
        self.assertEqual(d["typed"]["warn_lines"], 1)
        self.assertEqual(d["typed"]["fail_terminals"], [])
        self.assertEqual([s["name"] for s in d["skills_staged"]["skills"]], ["c-skill"])
        self.assertEqual(d["skills_staged"]["skills"][0]["status"], "candidate")
        self.assertIn("round.txt", d["files_present"])
        self.assertEqual(d["outcome"], "CHANGED")
        r = run([str(FIXTURES / "run-completed")])
        self.assertEqual(r.stdout.strip(), "TRACE_DIGEST=OK run=run-completed lane=feature terminal=completed rounds=2 score_inputs=5")

    def test_run_failed(self):
        d = td.digest(str(FIXTURES / "run-failed"))
        self.assertEqual((d["run_id"], d["repo"], d["lane"], d["terminal"]), ("run-failed", "api", "bugfix", "failed"))
        self.assertEqual(d["failed_at"], "round-1/converge")
        self.assertEqual((d["review"]["rounds"], d["review"]["exit"]), (1, "NO_PROGRESS"))
        self.assertEqual(d["typed"]["fail_tokens"], {"NO_PROGRESS": 1})
        self.assertEqual(d["rca"], {"present": True, "rounds": 2, "exit": "RCA_PLAN_CONVERGED", "critique_blocking": 1})
        self.assertEqual(d["waivers"]["count"], 0)
        self.assertEqual(d["skills_staged"]["result"], "SKIP")
        self.assertIsNone(d["pr_url"])
        self.assertIn("round.txt", d["files_present"])  # reached review -> scorer eligible
        r = run([str(FIXTURES / "run-failed")])
        self.assertEqual(r.stdout.strip(), "TRACE_DIGEST=OK run=run-failed lane=bugfix terminal=failed rounds=1 score_inputs=3")

    def test_fixtures_are_portable(self):
        bad = re.compile(re.escape(sl.ABS_HOME_MARKER) + r"|(?<![\w.])~/|[A-Za-z0-9+/=]{60,}")
        for p in FIXTURES.rglob("*"):
            if p.is_file():
                self.assertIsNone(bad.search(p.read_text(errors="replace")), str(p))


if __name__ == "__main__":
    unittest.main()
