#!/usr/bin/env python3
"""setup/skill-score.py: the weight table is pinned by exact scores, eligibility
follows the digest terminal, ingest appends exactly one ledger row and refuses
a repeat, attribution needs name + status + sha256, and a full window accepts
strictly-better candidates and rolls back everything else."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skill_fixtures import TempLibrary, skill_text
import skill_library as sl

SETUP = Path(__file__).resolve().parent.parent
SCRIPT = SETUP / "skill-score.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("skill_score", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ss = _load_module()


def digest(run_id="r-0001", terminal="completed", staged=None, files=("params.json",), **over):
    d = {
        "schema": sl.SCHEMA_DIGEST, "digest_version": 1, "run_id": run_id,
        "artifacts_dir": "/tmp/runs/" + run_id, "repo": "api", "lane": "feature",
        "spec": "x.md", "slug": "x", "branch": "archon/x", "terminal": terminal,
        "failed_at": None, "outcome": "CHANGED" if terminal == "completed" else None, "pr_url": None,
        "plan": {"rounds": 1, "exit": None, "critique_blocking": 0},
        "review": {"rounds": 1, "exit": None, "per_round": [
            {"round": 1, "verdict": "PASS", "residual_count": 0, "degraded": False, "applied": 0,
             "applied_by_severity": {"P0": 0, "P1": 0, "P2": 0, "P3": 0}, "incomplete": 0,
             "advisory": 0, "failed": 0, "reraised": 0, "converge_line": None}]},
        "deslop": {"rounds": 0, "dirty_rounds": 0, "slop_fail": False},
        "waivers": {"count": 0, "findings": []},
        "typed": {"fail_terminals": [], "fail_tokens": {}, "warn_lines": 0, "nodes": {}},
        "rca": None, "skills_staged": staged, "files_present": sorted(files),
    }
    d.update(over)
    return d


def run(args, cwd=None):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True,
                          encoding="utf-8", cwd=cwd)


def last(r):
    return (r.stdout.strip().splitlines() or [""])[-1]


class Base(unittest.TestCase):
    def setUp(self):
        self.lib = TempLibrary(repos=("api",))
        self.addCleanup(self.lib.cleanup)
        self.tmp = Path(tempfile.mkdtemp(prefix="ss-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write_digest(self, d):
        path = self.tmp / f"{d['run_id']}.json"
        path.write_text(json.dumps(d, indent=2, sort_keys=True))
        return path

    def ingest(self, d, evolve="ev-1", extra=()):
        return run(["ingest", "api", str(self.write_digest(d)), "--lib", str(self.lib.root),
                    "--evolve-run", evolve, *extra])

    def ledger(self):
        return sl.read_raw_ledger(self.lib.root, "api")

    def impact(self):
        return sl.read_impact(self.lib.root, "api")

    def staged_for(self, name, status="candidate", sha=None):
        sha = sha if sha is not None else self.lib.index()["skills"][name]["sha256"]
        return {"schema": sl.SCHEMA_STAGED, "repo": "api", "result": "OK",
                "skills": [{"name": name, "status": status, "sha256": sha, "bytes": 1, "path": f"skills/{name}/SKILL.md"}]}

    def seed_candidate(self, name="cand", baseline=None, window=3):
        # A frozen baseline is always exactly window_size runs long (admission
        # refuses a short one), so the default fixture is too.
        rows = list(baseline) if baseline is not None else [
            {"run_id": f"b{i + 1}", "score": 20.0} for i in range(window)]
        self.lib.add_skill(name, status="candidate",
                           scoring={"window_size": window, "baseline_runs": rows, "candidate_runs": []})
        return name


class Weights(unittest.TestCase):
    def test_score_version_and_weights_pinned(self):
        self.assertEqual(ss.SCORE_VERSION, 2)
        self.assertEqual(ss.WEIGHTS, {
            "review_rounds_extra": 10, "applied_p0": 8, "applied_p1": 4, "applied_p2": 2, "applied_p3": 1,
            "fixer_incomplete": 3, "reraised": 3, "waivers": 1, "deslop_dirty_rounds": 5,
            "plan_rounds_extra": 5, "loop_failure_tokens": 20, "other_fail_terminals": 10,
            "terminal_failed": 50})

    def test_clean_run_scores_zero(self):
        total, comp = ss.score(digest())
        self.assertEqual(total, 0.0)
        self.assertEqual(set(comp), set(ss.WEIGHTS))
        self.assertTrue(all(v == 0 for v in comp.values()))

    def full(self):
        return digest(
            terminal="failed", files=("params.json", "round.txt"),
            plan={"rounds": 3, "exit": None, "critique_blocking": 1},
            review={"rounds": 3, "exit": "NO_PROGRESS", "per_round": [
                {"round": 1, "applied_by_severity": {"P0": 1, "P1": 2, "P2": 3, "P3": 4}, "incomplete": 1, "reraised": 0},
                {"round": 2, "applied_by_severity": {"P0": 0, "P1": 1, "P2": 0, "P3": 1}, "incomplete": 2, "reraised": 2},
                {"round": 3, "applied_by_severity": {"P0": 0, "P1": 0, "P2": 0, "P3": 0}, "incomplete": 0, "reraised": 1}]},
            deslop={"rounds": 2, "dirty_rounds": 2, "slop_fail": False},
            waivers={"count": 4, "findings": ["a", "b", "c", "d"]},
            typed={"fail_terminals": ["NO_PROGRESS", "SMOKE", "RCA_PLAN_ROUND_CAP"],
                   "fail_tokens": {"NO_PROGRESS": 2, "RCA_PLAN_ROUND_CAP": 1, "CROSS_REPO_FINDING": 3},
                   "warn_lines": 2, "nodes": {}})

    def test_exact_score_of_a_busy_run(self):
        total, comp = ss.score(self.full())
        self.assertEqual(comp, {
            "review_rounds_extra": 2, "applied_p0": 1, "applied_p1": 3, "applied_p2": 3, "applied_p3": 5,
            "fixer_incomplete": 3, "reraised": 3, "waivers": 4, "deslop_dirty_rounds": 2,
            "plan_rounds_extra": 2, "loop_failure_tokens": 3, "other_fail_terminals": 1,
            "terminal_failed": 1})
        expected = 2 * 10 + 1 * 8 + 3 * 4 + 3 * 2 + 5 * 1 + 3 * 3 + 3 * 3 + 4 + 2 * 5 + 2 * 5 + 3 * 20 + 1 * 10 + 50
        self.assertEqual(total, float(expected))
        self.assertEqual(total, 213.0)

    def test_negative_control_one_component_moves_by_its_weight(self):
        base, _ = ss.score(self.full())
        d = self.full()
        d["waivers"]["count"] += 1
        self.assertEqual(ss.score(d)[0], base + ss.WEIGHTS["waivers"])
        d = self.full()
        d["review"]["per_round"][0]["applied_by_severity"]["P0"] += 1
        self.assertEqual(ss.score(d)[0], base + ss.WEIGHTS["applied_p0"])
        d = self.full()
        d["typed"]["fail_tokens"]["FIXER_BLOCKED"] = 1
        self.assertEqual(ss.score(d)[0], base + ss.WEIGHTS["loop_failure_tokens"])
        d = self.full()
        d["terminal"] = "completed"
        self.assertEqual(ss.score(d)[0], base - ss.WEIGHTS["terminal_failed"])
        d = self.full()
        d["typed"]["fail_terminals"].append("SMOKE")
        self.assertEqual(ss.score(d)[0], base + ss.WEIGHTS["other_fail_terminals"])

    def test_loop_failure_tokens(self):
        for t in ("NO_PROGRESS", "FIXER_BLOCKED", "ROUND_CAP_REACHED", "DESLOP_ROUND_CAP",
                  "PLAN_NO_PROGRESS", "PLAN_ROUND_CAP", "SCOPE_BREACH",
                  "RCA_PLAN_ROUND_CAP", "RCA_PLAN_NO_PROGRESS"):
            self.assertTrue(ss.is_loop_failure_token(t), t)
        # membership, never a prefix: a rejected or disputed plan is a verdict
        # on one plan, not a loop that ran out of rounds
        for t in ("CROSS_REPO_FINDING", "SMOKE", "PLAN_REJECTED",
                  "RCA_PLAN_REJECTED", "RCA_PLAN_SCOPE_DISPUTE", "RCA_PLAN_X"):
            self.assertFalse(ss.is_loop_failure_token(t), t)

    def test_a_rejected_rca_plan_costs_a_terminal_not_a_loop(self):
        # The regression the explicit set closes: RCA_PLAN_REJECTED used to
        # match the RCA_PLAN_ prefix and score 20 instead of 10.
        base, _ = ss.score(digest())
        d = digest(typed={"fail_terminals": ["RCA_PLAN_REJECTED"], "fail_tokens": {}, "warn_lines": 0, "nodes": {}})
        self.assertEqual(ss.score(d)[0], base + ss.WEIGHTS["other_fail_terminals"])
        d = digest(typed={"fail_terminals": ["RCA_PLAN_ROUND_CAP"],
                          "fail_tokens": {"RCA_PLAN_ROUND_CAP": 1}, "warn_lines": 0, "nodes": {}})
        comp = ss.score(d)[1]
        self.assertEqual((comp["loop_failure_tokens"], comp["other_fail_terminals"]), (1, 0))
        self.assertEqual(ss.score(d)[0], base + ss.WEIGHTS["loop_failure_tokens"])

    def test_missing_blocks_read_as_zero(self):
        d = digest()
        for k in ("plan", "review", "deslop", "waivers", "typed"):
            d.pop(k)
        self.assertEqual(ss.score(d)[0], 0.0)


class Eligibility(unittest.TestCase):
    def test_matrix(self):
        cases = [
            (digest(terminal="completed"), (True, "completed")),
            (digest(terminal="failed", files=("params.json", "round.txt")), (True, "failed_in_review")),
            (digest(terminal="failed", files=("params.json",)), (False, "failed_before_review")),
            (digest(terminal="no_change"), (False, "no_change")),
            (digest(terminal="incomplete"), (False, "incomplete")),
        ]
        for d, expect in cases:
            self.assertEqual(ss.eligibility(d), expect, d["terminal"])


class ScoreCli(Base):
    def test_score_prints_components_then_typed_line(self):
        d = digest(waivers={"count": 2, "findings": ["a", "b"]})
        r = run(["score", str(self.write_digest(d))])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        lines = r.stdout.strip().splitlines()
        self.assertEqual(lines, ["SCORE_COMPONENT name=waivers value=2 weight=1",
                                 "SKILL_SCORE=OK run=r-0001 score=2.00 eligible=yes reason=completed"])

    def test_score_fails_on_bad_digest(self):
        p = self.tmp / "bad.json"
        p.write_text("{nope")
        r = run(["score", str(p)])
        self.assertEqual(r.returncode, 1)
        self.assertTrue(r.stdout.startswith("SKILL_SCORE=FAIL"), r.stdout)
        p.write_text(json.dumps({"schema": "other", "run_id": "x", "terminal": "completed"}))
        r = run(["score", str(p)])
        self.assertEqual(r.returncode, 1)
        self.assertIn("schema", r.stdout)
        # a run_id with a separator would name a path, not a run
        p.write_text(json.dumps({"schema": sl.SCHEMA_DIGEST, "run_id": "a/b", "terminal": "completed"}))
        r = run(["score", str(p)])
        self.assertEqual(r.returncode, 1)
        self.assertIn("run_id missing or invalid", r.stdout)
        # an unrecognised terminal is refused rather than scored as incomplete
        p.write_text(json.dumps({"schema": sl.SCHEMA_DIGEST, "run_id": "r-1", "terminal": "exploded"}))
        r = run(["score", str(p)])
        self.assertEqual(r.returncode, 1)
        self.assertIn("terminal 'exploded' not in", r.stdout)
        r = run(["score", str(self.tmp / "missing.json")])
        self.assertEqual(r.returncode, 1)


class Ingest(Base):
    def test_appends_one_row_with_the_documented_fields(self):
        self.lib.commit("seed")
        r = self.ingest(digest(waivers={"count": 3, "findings": []}), extra=["--out", str(self.tmp / "s.json")])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(last(r), "SKILL_SCORE=OK run=r-0001 score=3.00 eligible=yes window=none")
        rows = self.ledger()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(set(row), {
            "schema", "run_id", "artifacts_dir", "lane", "terminal", "outcome", "eligible",
            "eligibility_reason", "score", "score_version", "skills_staged", "attributed_to",
            "library_head", "digest_sha256", "evolve_run_id", "ingested_at"})
        self.assertEqual(row["schema"], sl.SCHEMA_RAW)
        self.assertEqual((row["run_id"], row["lane"], row["terminal"], row["outcome"]),
                         ("r-0001", "feature", "completed", "CHANGED"))
        self.assertEqual((row["eligible"], row["eligibility_reason"], row["score"], row["score_version"]),
                         (True, "completed", 3.0, ss.SCORE_VERSION))
        self.assertEqual(row["skills_staged"], [])
        self.assertIsNone(row["attributed_to"])
        self.assertEqual(row["library_head"], self.lib.head())
        self.assertEqual(row["digest_sha256"], sl.sha256_file(self.tmp / "r-0001.json"))
        self.assertEqual(row["evolve_run_id"], "ev-1")
        self.assertTrue(row["ingested_at"].endswith("Z"))
        summary = json.loads((self.tmp / "s.json").read_text())
        self.assertEqual(summary["schema"], "archon.skill-score.v1")
        self.assertEqual(summary["ledger_row"], row)
        self.assertEqual(summary["window"], {"state": "none"})

    def test_refuses_re_ingest_with_exit_2(self):
        self.assertEqual(self.ingest(digest()).returncode, 0)
        r = self.ingest(digest())
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertEqual(last(r), "SKILL_SCORE=FAIL already ingested run=r-0001")
        self.assertEqual(len(self.ledger()), 1)
        r = run(["is-ingested", "api", "r-0001", "--lib", str(self.lib.root)])
        self.assertEqual((r.returncode, last(r)), (0, "SKILL_INGESTED=YES run=r-0001"))
        r = run(["is-ingested", "api", "r-0002", "--lib", str(self.lib.root)])
        self.assertEqual((r.returncode, last(r)), (0, "SKILL_INGESTED=NO run=r-0002"))

    def test_ineligible_runs_are_ingested_without_a_score(self):
        r = self.ingest(digest(terminal="no_change"))
        self.assertEqual(last(r), "SKILL_SCORE=OK run=r-0001 score=0.00 eligible=no window=none")
        row = self.ledger()[0]
        self.assertEqual((row["eligible"], row["eligibility_reason"], row["score"]), (False, "no_change", None))

    def test_invalid_index_fails_closed(self):
        self.lib.add_skill("a", status="candidate")
        self.lib.add_skill("b", status="candidate")
        r = self.ingest(digest())
        self.assertEqual(r.returncode, 1)
        self.assertIn("SKILL_SCORE=FAIL", r.stdout)
        self.assertIn("more than one candidate", r.stdout)
        self.assertEqual(self.ledger(), [])

    def test_missing_library_fails(self):
        r = run(["ingest", "nope", str(self.write_digest(digest())), "--lib", str(self.lib.root), "--evolve-run", "e"])
        self.assertEqual(r.returncode, 1)
        self.assertTrue(r.stdout.startswith("SKILL_SCORE=FAIL"))

    def test_stdout_is_deterministic(self):
        d = digest(waivers={"count": 1, "findings": ["a"]})
        a = run(["score", str(self.write_digest(d))]).stdout
        b = run(["score", str(self.write_digest(d))]).stdout
        self.assertEqual(a, b)


class Attribution(Base):
    def test_only_on_name_status_and_sha(self):
        name = self.seed_candidate()
        sha = self.lib.index()["skills"][name]["sha256"]
        cases = {
            "r-match": self.staged_for(name),
            "r-badsha": self.staged_for(name, sha="0" * 64),
            "r-active": self.staged_for(name, status="active"),
            "r-other": self.staged_for(name),
            "r-none": None,
        }
        cases["r-other"]["skills"][0]["name"] = "someone-else"
        for rid, staged in cases.items():
            r = self.ingest(digest(run_id=rid, staged=staged))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rows = {row["run_id"]: row for row in self.ledger()}
        self.assertEqual(rows["r-match"]["attributed_to"], name)
        self.assertEqual(rows["r-match"]["skills_staged"], [{"name": name, "status": "candidate", "sha256": sha}])
        for rid in ("r-badsha", "r-active", "r-other", "r-none"):
            self.assertIsNone(rows[rid]["attributed_to"], rid)
        runs = self.lib.index()["skills"][name]["scoring"]["candidate_runs"]
        self.assertEqual(runs, [{"run_id": "r-match", "score": 0.0}])

    def test_ineligible_attributed_run_never_enters_the_window(self):
        name = self.seed_candidate()
        r = self.ingest(digest(run_id="r-nc", terminal="no_change", staged=self.staged_for(name)))
        self.assertEqual(last(r), "SKILL_SCORE=OK run=r-nc score=0.00 eligible=no window=open(0/3)")
        r = self.ingest(digest(run_id="r-fb", terminal="failed", staged=self.staged_for(name)))
        self.assertIn("window=open(0/3)", last(r))
        self.assertEqual(self.lib.index()["skills"][name]["scoring"]["candidate_runs"], [])
        self.assertIsNone(self.ledger()[0]["attributed_to"])

    def test_window_line_reports_progress(self):
        name = self.seed_candidate()
        r = run(["window", "api", "--lib", str(self.lib.root)])
        self.assertEqual(last(r), f"SKILL_WINDOW=OPEN skill={name} runs=0/3 baseline=20.00")
        self.ingest(digest(run_id="r-1", staged=self.staged_for(name)))
        r = run(["window", "api", "--lib", str(self.lib.root)])
        self.assertEqual(last(r), f"SKILL_WINDOW=OPEN skill={name} runs=1/3 baseline=20.00")

    def test_window_none_without_candidate(self):
        self.lib.add_skill("act")
        r = run(["window", "api", "--lib", str(self.lib.root)])
        self.assertEqual((r.returncode, last(r)), (0, "SKILL_WINDOW=NONE"))

    def test_window_without_a_skills_index_is_a_typed_fail(self):
        empty = self.tmp / "no-library"
        empty.mkdir()
        r = run(["window", "api", "--lib", str(empty)])
        self.assertEqual((r.returncode, last(r)), (1, "SKILL_SCORE=FAIL no skills index for api"))
        self.assertNotIn("Traceback", r.stderr)


class ScoreVersionStamp(Base):
    """The index carries the version the NEXT candidate is frozen against."""

    def test_a_free_index_takes_the_current_version(self):
        self.assertEqual(self.lib.index()["score_version"], ss.SCORE_VERSION)
        nxt = ss.SCORE_VERSION + 1
        with mock.patch.object(ss, "SCORE_VERSION", nxt):
            ss.ingest(str(self.lib.root), "api", str(self.write_digest(digest())), "ev-1")
        self.assertEqual(self.lib.index()["score_version"], nxt)
        self.assertEqual(self.ledger()[0]["score_version"], nxt)

    def test_an_open_window_keeps_the_old_version_and_closes_inconclusive(self):
        name = self.seed_candidate()
        cur, nxt = ss.SCORE_VERSION, ss.SCORE_VERSION + 1
        with mock.patch.object(ss, "SCORE_VERSION", nxt):
            for i in (1, 2):
                d = digest(run_id=f"c-{i}", staged=self.staged_for(name))
                out = ss.ingest(str(self.lib.root), "api", str(self.write_digest(d)), f"ev-{i}")
                self.assertTrue(out["window"]["state"].startswith("open("), out["window"])
                self.assertEqual(self.lib.index()["score_version"], cur,
                                 "a pending candidate pins the version it was frozen against")
            d = digest(run_id="c-3", staged=self.staged_for(name))
            out = ss.ingest(str(self.lib.root), "api", str(self.write_digest(d)), "ev-3")
        self.assertEqual(out["window"]["state"], "closed:inconclusive")
        self.assertEqual(out["window"]["reason"], "score_version_mismatch")
        self.assertEqual(self.lib.index()["skills"][name]["status"], "rolled_back")
        # the window that was open when the version moved is the only one it costs
        self.assertEqual(self.lib.index()["score_version"], nxt)


class Windows(Base):
    def fill(self, name, scores, prefix="c"):
        out = None
        for i, s in enumerate(scores, 1):
            d = digest(run_id=f"{prefix}-{i}", staged=self.staged_for(name),
                       waivers={"count": int(s), "findings": []})
            out = self.ingest(d, evolve=f"ev-{i}")
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return out

    def test_accept_path(self):
        name = self.seed_candidate(baseline=({"run_id": "b1", "score": 20.0}, {"run_id": "b2", "score": 10.0},
                                            {"run_id": "b3", "score": 15.0}))
        r = self.fill(name, (5, 10, 15))  # mean 10 < 15
        lines = r.stdout.strip().splitlines()
        self.assertEqual(lines[-2], f"SKILL_WINDOW=ACCEPT skill={name} baseline=15.00 candidate=10.00")
        self.assertEqual(lines[-1], "SKILL_SCORE=OK run=c-3 score=15.00 eligible=yes window=closed:accept")
        entry = self.lib.index()["skills"][name]
        self.assertEqual(entry["status"], "active")
        self.assertIsNotNone(entry["activated_at"])
        self.assertIsNone(entry["candidate_since"])
        self.assertFalse((Path(self.lib.paths()["rollback_dir"]) / name).exists())
        self.assertTrue(Path(sl.skill_file(self.lib.root, "api", name)).is_file())
        self.assertEqual(entry["history"][-1]["outcome"], "accepted")
        ev = self.impact()[-1]
        self.assertEqual((ev["event"], ev["skill"], ev["evolve_run_id"], ev["reason"]), ("accepted", name, "ev-3", "better"))
        self.assertEqual(ev["details"]["candidate_runs"], ["c-1", "c-2", "c-3"])
        self.assertEqual(ev["details"]["baseline_runs"], ["b1", "b2", "b3"])
        self.assertEqual((ev["details"]["baseline_mean"], ev["details"]["candidate_mean"]), (15.0, 10.0))
        md = Path(self.lib.paths()["impact_md"]).read_text()
        self.assertIn(f"accepted {name}", md)
        log = Path(self.lib.paths()["wiki_log"]).read_text()
        self.assertIn(f"skill {name} accepted", log)

    def test_rollback_removes_a_created_candidate(self):
        name = self.seed_candidate(baseline=({"run_id": "b1", "score": 5.0}, {"run_id": "b2", "score": 5.0},
                                            {"run_id": "b3", "score": 5.0}))
        r = self.fill(name, (10, 10, 10))
        lines = r.stdout.strip().splitlines()
        self.assertEqual(lines[-2], f"SKILL_WINDOW=ROLLBACK skill={name} reason=worse baseline=5.00 candidate=10.00")
        self.assertTrue(lines[-1].endswith("window=closed:rollback"))
        entry = self.lib.index()["skills"][name]
        self.assertEqual(entry["status"], "rolled_back")
        self.assertIsNone(entry["sha256"])
        self.assertFalse(Path(sl.skill_dir(self.lib.root, "api", name)).exists())
        self.assertFalse((Path(self.lib.paths()["rollback_dir"]) / name).exists())
        ev = self.impact()[-1]
        self.assertEqual((ev["event"], ev["reason"], ev["details"]["restored"]), ("rolled_back", "worse", "removed"))
        self.assertIn(f"skill {name} rolled back (worse, removed)", Path(self.lib.paths()["wiki_log"]).read_text())

    def test_rollback_restores_a_patched_candidate(self):
        name = "patched"
        old = skill_text(name, steps=("OLD STEP",))
        new = skill_text(name, steps=("NEW STEP",))
        snap = Path(self.lib.paths()["rollback_dir"]) / name
        snap.mkdir(parents=True)
        (snap / "SKILL.md").write_text(old)
        (snap / "PURPOSE.md").write_text("origin: old\n")
        self.lib.add_skill(name, status="candidate", text=new,
                           rollback={"existed": True, "sha256": sl.sha256_bytes(old.encode()), "snapshot": f".rollback/{name}"},
                           scoring={"window_size": 2,
                                    "baseline_runs": [{"run_id": "b1", "score": 3.0},
                                                      {"run_id": "b2", "score": 3.0}],
                                    "candidate_runs": []})
        r = self.fill(name, (3, 3))  # tie
        lines = r.stdout.strip().splitlines()
        self.assertEqual(lines[-2], f"SKILL_WINDOW=ROLLBACK skill={name} reason=tie baseline=3.00 candidate=3.00")
        entry = self.lib.index()["skills"][name]
        self.assertEqual(entry["status"], "active")
        self.assertEqual(Path(sl.skill_file(self.lib.root, "api", name)).read_text(), old)
        self.assertEqual(entry["sha256"], sl.sha256_bytes(old.encode()))
        self.assertEqual(Path(sl.purpose_file(self.lib.root, "api", name)).read_text(), "origin: old\n")
        self.assertFalse(snap.exists())
        self.assertEqual(self.impact()[-1]["details"]["restored"], "restored")

    def test_tie_with_min_improvement_rolls_back(self):
        idx = self.lib.index()
        idx["min_improvement"] = 2.0
        self.lib.write_index(idx)
        name = self.seed_candidate(baseline=({"run_id": "b1", "score": 10.0},), window=1)
        r = self.fill(name, (8,))
        self.assertIn(f"SKILL_WINDOW=ROLLBACK skill={name} reason=tie", r.stdout)
        name2 = self.seed_candidate("cand2", baseline=({"run_id": "b1", "score": 10.0},), window=1)
        r = self.fill(name2, (7,), prefix="d")
        self.assertIn(f"SKILL_WINDOW=ACCEPT skill={name2}", r.stdout)

    def test_score_version_mismatch_is_inconclusive(self):
        idx = self.lib.index()
        idx["score_version"] = ss.SCORE_VERSION + 1
        self.lib.write_index(idx)
        name = self.seed_candidate(window=1)
        r = self.fill(name, (1,))
        lines = r.stdout.strip().splitlines()
        self.assertEqual(lines[-2],
                         f"SKILL_WINDOW=ROLLBACK skill={name} reason=score_version_mismatch baseline=20.00 candidate=1.00")
        self.assertTrue(lines[-1].endswith("window=closed:inconclusive"))
        self.assertEqual(self.lib.index()["skills"][name]["status"], "rolled_back")
        self.assertEqual(self.impact()[-1]["reason"], "score_version_mismatch")

    def test_short_baseline_is_inconclusive(self):
        # A hand-edited window whose baseline is shorter than K cannot be
        # compared: the two means are over different sample sizes.
        name = self.seed_candidate(baseline=({"run_id": "b1", "score": 20.0},), window=2)
        r = self.fill(name, (1, 1))
        lines = r.stdout.strip().splitlines()
        self.assertEqual(lines[-2],
                         f"SKILL_WINDOW=ROLLBACK skill={name} reason=short_baseline baseline=20.00 candidate=1.00")
        self.assertTrue(lines[-1].endswith("window=closed:inconclusive"))
        self.assertEqual(self.lib.index()["skills"][name]["status"], "rolled_back")
        self.assertEqual(self.impact()[-1]["reason"], "short_baseline")

    def test_no_baseline_is_inconclusive(self):
        name = self.seed_candidate(baseline=(), window=1)
        r = self.fill(name, (0,))
        lines = r.stdout.strip().splitlines()
        self.assertEqual(lines[-2], f"SKILL_WINDOW=ROLLBACK skill={name} reason=no_baseline baseline=none candidate=0.00")
        self.assertTrue(lines[-1].endswith("window=closed:inconclusive"))
        self.assertEqual(self.lib.index()["skills"][name]["status"], "rolled_back")

    def test_window_stays_open_until_full(self):
        name = self.seed_candidate()
        r = self.fill(name, (1, 1))
        self.assertTrue(last(r).endswith("window=open(2/3)"), r.stdout)
        self.assertNotIn("SKILL_WINDOW", r.stdout)
        self.assertEqual(self.lib.index()["skills"][name]["status"], "candidate")


class Baseline(Base):
    def test_last_k_eligible_scored_rows(self):
        for rid, elig, score, at in (("a", True, 1.0, "2026-09-01T00:00:01Z"),
                                     ("b", False, None, "2026-09-01T00:00:02Z"),
                                     ("c", True, 3.0, "2026-09-01T00:00:03Z"),
                                     ("d", True, 4.0, "2026-09-01T00:00:04Z"),
                                     ("e", True, 5.0, "2026-09-01T00:00:05Z")):
            self.lib.add_raw_run(rid, eligible=elig, score=score, ingested_at=at)
        self.assertEqual(sl.baseline_runs(self.lib.root, "api", 3),
                         [{"run_id": "c", "score": 3.0}, {"run_id": "d", "score": 4.0}, {"run_id": "e", "score": 5.0}])
        self.assertEqual(sl.baseline_runs(self.lib.root, "api", 2, before_iso="2026-09-01T00:00:04Z"),
                         [{"run_id": "a", "score": 1.0}, {"run_id": "c", "score": 3.0}])
        self.assertEqual(sl.baseline_runs(self.lib.root, "api", 10), [
            {"run_id": "a", "score": 1.0}, {"run_id": "c", "score": 3.0},
            {"run_id": "d", "score": 4.0}, {"run_id": "e", "score": 5.0}])
        self.assertEqual(sl.baseline_runs(self.lib.root, "api", 0), [])

    def test_empty_ledger(self):
        self.assertEqual(sl.baseline_runs(self.lib.root, "api", 3), [])


class Shipped(unittest.TestCase):
    def test_no_machine_paths_or_long_literals(self):
        text = SCRIPT.read_text()
        self.assertNotIn(sl.ABS_HOME_MARKER, text)
        import re
        self.assertIsNone(re.search(r"[A-Za-z0-9+/=]{60,}", text))
        self.assertNotIn("__", SCRIPT.name)


if __name__ == "__main__":
    unittest.main()
