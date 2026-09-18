#!/usr/bin/env python3
"""skill-evolve bash nodes, engine-free.

Each node body is executed through `nodes.extract.runnable_body` against a
temp root whose `.archon/setup` mirrors this checkout, with
`ARCHON_LIBRARY_ROOT` pointing at a `TempLibrary` git repo and `ARGUMENTS`
at a private copy of a fixture run. Covers the preflight PASS/FAIL matrix,
the preflight -> trace-digest -> score-run -> evolve-route chain (one commit
by the evolve author, route `propose=no reason=too_few_runs`), the route's
four refusal reasons and its 5/3 maintainer sample, the gates' no-file
refusals and empty-patch / no_action happy paths, and the report's lock
release and typed terminal line.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from nodes.extract import runnable_body
from skill_fixtures import TempLibrary

SETUP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SETUP))
import skill_library as sl  # noqa: E402

FIXTURES = SETUP / "tests" / "fixtures" / "skill-evolve"
WF = "skill-evolve"
BILLING = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
           "ANTHROPIC_PROFILE")


class Root:
    """<root>/.archon/setup mirrors this checkout; the library is a TempLibrary."""

    def __init__(self, repos=("api", "goodword-mcp", "web-app")):
        self.dir = Path(tempfile.mkdtemp(prefix="se-root-"))
        mirror = self.dir / ".archon" / "setup"
        mirror.mkdir(parents=True)
        for p in SETUP.iterdir():
            (mirror / p.name).symlink_to(p)
        self.lib = TempLibrary(repos=repos, git_init=True)

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        self.lib.cleanup()


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Root()
        self.addCleanup(self.root.cleanup)
        self.lib = self.root.lib
        self.ad = Path(tempfile.mkdtemp(prefix="se-art-"))
        self.addCleanup(shutil.rmtree, self.ad, ignore_errors=True)
        self.rid_self = self.ad.name

    def fixture(self, name="run-completed"):
        """A private, absolute copy of a fixture run (its basename is the run id)."""
        parent = Path(tempfile.mkdtemp(prefix="se-src-"))
        self.addCleanup(shutil.rmtree, parent, ignore_errors=True)
        dst = parent / name
        shutil.copytree(FIXTURES / name, dst)
        return dst

    def run_node(self, node, src=None, env=None):
        e = {k: v for k, v in os.environ.items() if k not in BILLING}
        e.update(ARTIFACTS_DIR=str(self.ad), ARCHON_LIBRARY_ROOT=str(self.lib.root))
        if src is not None:
            e["ARGUMENTS"] = str(src)
        else:
            e.pop("ARGUMENTS", None)
        e.update(env or {})
        body = runnable_body(WF, node, root=str(self.root.dir))
        return subprocess.run(["bash", "-c", body], capture_output=True, encoding="utf-8",
                              env=e, cwd=str(self.root.dir))

    def last(self, p):
        return (p.stdout.strip().splitlines() or [""])[-1]

    def assert_typed(self, p, node):
        """The harness rule: rc 0 needs a PASS-class last typed line, rc != 0 a
        FAIL-class one, and the tee copy carries it too."""
        lt = sl.last_typed(p.stdout)
        self.assertIsNotNone(lt, (node, p.stdout, p.stderr))
        cls = sl.classify_typed(*lt)
        self.assertEqual(cls, "pass" if p.returncode == 0 else "fail", (node, lt, p.returncode))
        self.assertIn(lt[2], (self.ad / f"node-{node}.out").read_text())

    def lock(self, repo="api"):
        return self.lib.root / ".locks" / f"{repo}.evolve.lock"

    def evolve_commits(self, repo="api"):
        return [l for l in self.lib.log(repo) if sl.GIT_AUTHOR[0] in l]

    def chain(self, src, upto="evolve-route"):
        out = {}
        for node in ("preflight", "trace-digest", "score-run", "evolve-route"):
            p = self.run_node(node, src)
            self.assertEqual(p.returncode, 0, (node, p.stdout, p.stderr))
            self.assert_typed(p, node) if node != "evolve-route" else None
            out[node] = p
            if node == upto:
                break
        return out


class Preflight(Base):
    def test_pass_writes_context_and_takes_the_lock(self):
        src = self.fixture()
        p = self.run_node("preflight", src)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.last(p), f"PREFLIGHT=PASS run={src.name} repo=api candidate=none")
        self.assert_typed(p, "preflight")
        self.assertIn("LIBRARY_SKELETON=PRESENT", p.stdout)
        self.assertIn("SKILL_INDEX=OK candidate=none active=0 rolled_back=0", p.stdout)
        self.assertIn(f"SKILL_INGESTED=NO run={src.name}", p.stdout)
        self.assertIn("LIBRARY_GIT=OK head=", p.stdout)
        self.assertEqual((self.ad / "source-run.txt").read_text().strip(), str(src))
        self.assertEqual((self.ad / "repo.txt").read_text().strip(), "api")
        ctx = json.loads((self.ad / "evolve-context.json").read_text())
        self.assertEqual(ctx["schema"], "archon.evolve-context.v1")
        self.assertEqual((ctx["source_run_id"], ctx["repo"], ctx["evolve_run_id"], ctx["candidate"]),
                         (src.name, "api", self.rid_self, None))
        self.assertEqual(ctx["library_head"], self.lib.head())
        self.assertEqual(ctx["lock"], str(self.lock()))
        self.assertEqual((self.lock() / "owner").read_text().strip(), self.rid_self)
        # the lock never dirties the repo tree the gates require clean
        self.assertEqual(self.lib.porcelain("api"), "")
        self.assertEqual(self.evolve_commits(), [])

    def test_repo_less_params_route_to_web_app_when_web_nodes_exist(self):
        src = self.fixture()
        d = json.loads((src / "params.json").read_text())
        d.pop("repo")
        (src / "params.json").write_text(json.dumps(d))
        (src / "node-web-implement.out").write_text("WEB_IMPLEMENT=OK\n")
        p = self.run_node("preflight", src)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.last(p), f"PREFLIGHT=PASS run={src.name} repo=web-app candidate=none")
        self.assertTrue((self.lock("web-app") / "owner").exists())

    def test_reports_a_pending_candidate(self):
        self.lib.add_skill("cand", status="candidate")
        self.lib.commit("seed candidate")
        src = self.fixture()
        p = self.run_node("preflight", src)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.last(p), f"PREFLIGHT=PASS run={src.name} repo=api candidate=cand")
        self.assertEqual(json.loads((self.ad / "evolve-context.json").read_text())["candidate"], "cand")

    def test_fresh_library_gets_a_committed_skeleton(self):
        shutil.rmtree(self.lib.root / "api")
        self.lib.commit("drop api skeleton")
        src = self.fixture()
        p = self.run_node("preflight", src)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("LIBRARY_SKELETON=CREATED ", p.stdout)
        self.assertIn("LIBRARY_COMMIT=OK sha=", p.stdout)
        self.assertTrue(self.last(p).startswith("PREFLIGHT=PASS "))
        self.assertTrue(sl.skeleton_present(self.lib.root, "api"))
        self.assertEqual(len(self.evolve_commits()), 1)
        self.assertIn("library(api): skeleton", self.evolve_commits()[0])
        self.assertEqual(self.lib.porcelain("api"), "")

    def fail(self, src, needle, env=None):
        p = self.run_node("preflight", src, env=env)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertTrue(self.last(p).startswith("PREFLIGHT=FAIL "), p.stdout)
        self.assertIn(needle, self.last(p))
        self.assert_typed(p, "preflight")
        return p

    def test_fail_no_run_message(self):
        self.fail(None, "run message must be a finished run's artifacts dir")
        self.fail("", "run message must be a finished run's artifacts dir")
        self.assertFalse(self.lock().exists())

    def test_fail_missing_dir_and_relative_path(self):
        self.fail(self.ad / "nope", "not a directory")
        self.fail("relative/run", "not a directory")

    def test_fail_missing_params(self):
        src = self.fixture()
        (src / "params.json").unlink()
        self.fail(src, f"no params.json in {src}")
        self.assertFalse(self.lock().exists())
        self.assertFalse((self.ad / "repo.txt").exists())

    def test_fail_unparseable_params_reports_the_python_reason(self):
        # The heredoc prints its own PREFLIGHT=FAIL and exits 1. Under set -e a
        # bare assignment would abort the node with that message still captured
        # in the variable, so the guard has to echo it.
        src = self.fixture()
        (src / "params.json").write_text("{not json")
        p = self.fail(src, "params.json unreadable")
        self.assertTrue(self.last(p).startswith("PREFLIGHT=FAIL params.json unreadable"), p.stdout)
        self.assertFalse(self.lock().exists())
        self.assertFalse((self.ad / "repo.txt").exists())

    def test_fail_unknown_repo_before_touching_the_library(self):
        src = self.fixture()
        d = json.loads((src / "params.json").read_text())
        d["repo"] = "not-a-repo"
        (src / "params.json").write_text(json.dumps(d))
        self.fail(src, "unknown repo not-a-repo")
        self.assertFalse((self.lib.root / "not-a-repo").exists())
        self.assertFalse(self.lock("not-a-repo").exists())

    def test_fail_billing_env(self):
        src = self.fixture()
        p = self.fail(src, "ANTHROPIC_API_KEY is set", env={"ANTHROPIC_API_KEY": "sk-x"})
        self.assertNotIn("LIBRARY_SKELETON", p.stdout)
        self.assertFalse(self.lock().exists())

    def test_fail_already_ingested(self):
        src = self.fixture()
        self.lib.add_raw_run(src.name)
        self.lib.commit("ledger row")
        self.fail(src, f"run {src.name} is already in the api raw ledger")
        self.assertFalse(self.lock().exists())

    def test_fail_dirty_tree_takes_no_lock(self):
        src = self.fixture()
        (self.lib.root / "api" / "stray.txt").write_text("x")
        p = self.fail(src, "library/api must be a clean git tree")
        self.assertIn("LIBRARY_GIT=FAIL dirty", p.stdout)
        self.assertFalse(self.lock().exists())

    def test_fail_lock_held_names_the_owner_and_keeps_it(self):
        src = self.fixture()
        self.lock().mkdir(parents=True)
        (self.lock() / "owner").write_text("other-run\n")
        self.fail(src, "lock held")
        self.assertIn("(owner other-run)", self.last(self.run_node("preflight", src)))
        self.assertEqual((self.lock() / "owner").read_text().strip(), "other-run")

    def test_fail_corrupt_index(self):
        src = self.fixture()
        Path(self.lib.paths("api")["skills_index"]).write_text("{not json")
        self.lib.commit("corrupt")
        p = self.fail(src, "library index invalid")
        self.assertIn("SKILL_INDEX=FAIL", p.stdout)
        self.assertFalse(self.lock().exists())


class Chain(Base):
    def test_digest_score_route_on_the_completed_fixture(self):
        src = self.fixture()
        out = self.chain(src)
        d = out["trace-digest"]
        self.assertIn(f"TRACE_DIGEST=OK run={src.name} lane=feature terminal=completed rounds=2", d.stdout)
        digest = json.loads((self.ad / "trace-digest.json").read_text())
        self.assertEqual((digest["schema"], digest["repo"], digest["artifacts_dir"]),
                         (sl.SCHEMA_DIGEST, "api", str(src)))

        s = out["score-run"]
        lines = s.stdout.strip().splitlines()
        self.assertTrue(any(l.startswith(f"SKILL_SCORE=OK run={src.name} score=") and l.endswith("eligible=yes window=none")
                            for l in lines), s.stdout)
        self.assertTrue(lines[-1].startswith("LIBRARY_COMMIT=OK sha="), s.stdout)
        self.assertNotIn("SKILL_WINDOW=", s.stdout)
        res = json.loads((self.ad / "score-result.json").read_text())
        self.assertEqual(res["run_id"], src.name)
        rows = sl.read_raw_ledger(self.lib.root, "api")
        self.assertEqual([r["run_id"] for r in rows], [src.name])
        self.assertEqual((rows[0]["evolve_run_id"], rows[0]["artifacts_dir"], rows[0]["eligible"]),
                         (self.rid_self, str(src), True))
        commits = self.evolve_commits()
        self.assertEqual(len(commits), 1, commits)
        self.assertIn(f"ingest(api): run {src.name} (skill-evolve {self.rid_self})", commits[0])
        self.assertIn(f"{sl.GIT_AUTHOR[0]} <{sl.GIT_AUTHOR[1]}>", commits[0])
        self.assertEqual(self.lib.porcelain("api"), "")

        r = out["evolve-route"]
        json_lines = [l for l in r.stdout.splitlines() if l.startswith("{")]
        self.assertEqual(len(json_lines), 1, r.stdout)
        self.assertEqual(json_lines[-1], self.last(r))
        route = json.loads(json_lines[0])
        self.assertEqual(route, {"propose": "no", "reason": "too_few_runs", "candidate": "none", "runs": 1,
                                 "eligible": 1, "sample_failed": 0, "sample_completed": 0})
        self.assertIsNone(sl.last_typed(r.stdout))  # no typed line: the JSON is the output
        sample = json.loads((self.ad / "maintain-sample.json").read_text())
        self.assertEqual(sample, {"schema": "archon.maintain-sample.v1", "repo": "api", "current_run": src.name,
                                  "failed": [], "completed": []})
        self.assertTrue((self.ad / "traces").is_dir())

    def test_failed_fixture_scores_and_commits(self):
        src = self.fixture("run-failed")
        out = self.chain(src, upto="score-run")
        self.assertIn(f"TRACE_DIGEST=OK run={src.name} lane=bugfix terminal=failed", out["trace-digest"].stdout)
        self.assertIn("eligible=yes window=none", out["score-run"].stdout)
        self.assertEqual(len(self.evolve_commits()), 1)

    def test_digest_refuses_a_repo_that_disagrees_with_preflight(self):
        src = self.fixture()
        self.assertEqual(self.run_node("preflight", src).returncode, 0)
        (self.ad / "repo.txt").write_text("goodword-mcp\n")
        p = self.run_node("trace-digest", src)
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertEqual(self.last(p), "TRACE_DIGEST=FAIL digest repo api != preflight repo goodword-mcp")
        self.assert_typed(p, "trace-digest")

    def test_second_ingest_of_the_same_run_is_refused(self):
        src = self.fixture()
        self.chain(src, upto="score-run")
        p = self.run_node("score-run", src)
        self.assertEqual(p.returncode, 2, p.stdout)
        self.assertIn(f"SKILL_SCORE=FAIL already ingested run={src.name}", p.stdout)
        self.assertEqual(len(self.evolve_commits()), 1)


class Route(Base):
    """evolve-route reads only repo.txt, source-run.txt and the library."""

    def setUp(self):
        super().setUp()
        self.src = self.fixture()
        (self.ad / "repo.txt").write_text("api\n")
        (self.ad / "source-run.txt").write_text(str(self.src) + "\n")

    def route(self):
        p = self.run_node("evolve-route", self.src)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return json.loads(self.last(p))

    def seed(self, n_failed, n_done, score=10.0, eligible=True):
        failed_src = self.fixture("run-failed")
        done_src = self.fixture("run-completed")
        for i in range(n_failed):
            self.lib.add_raw_run(f"f-{i:02d}", terminal="failed", lane="bugfix", artifacts_dir=str(failed_src),
                                 score=score + 50, eligible=eligible, ingested_at=f"2026-09-01T00:{i:02d}:00Z")
        for i in range(n_done):
            self.lib.add_raw_run(f"c-{i:02d}", terminal="completed", artifacts_dir=str(done_src),
                                 score=score, eligible=eligible, ingested_at=f"2026-09-02T00:{i:02d}:00Z")

    def test_sample_is_the_last_five_failed_and_three_completed(self):
        self.seed(7, 5)
        # a row without a real artifacts dir is listed as missing, not dropped
        self.lib.add_raw_run("gone", terminal="failed", artifacts_dir=str(self.ad / "gone"),
                             ingested_at="2026-09-03T00:00:00Z")
        route = self.route()
        self.assertEqual((route["propose"], route["reason"]), ("yes", "ok"))
        self.assertEqual((route["runs"], route["sample_failed"], route["sample_completed"]), (13, 5, 3))
        sample = json.loads((self.ad / "maintain-sample.json").read_text())
        self.assertEqual([s["run_id"] for s in sample["failed"]], ["gone", "f-06", "f-05", "f-04", "f-03"])
        self.assertEqual([s["run_id"] for s in sample["completed"]], ["c-04", "c-03", "c-02"])
        self.assertEqual(sample["failed"][0]["missing"], True)
        self.assertIsNone(sample["failed"][0]["trace"])
        for s in sample["failed"][1:] + sample["completed"]:
            self.assertFalse(s["missing"])
            self.assertEqual(s["trace"], f"traces/{s['run_id']}.json")
            self.assertTrue((self.ad / s["trace"]).is_file(), s)
            self.assertEqual(json.loads((self.ad / s["trace"]).read_text())["schema"], sl.SCHEMA_DIGEST)
        self.assertEqual(len(list((self.ad / "traces").iterdir())), 7)  # the missing run leaves no trace

    def test_current_run_is_excluded_from_its_own_sample(self):
        self.seed(2, 2)
        self.lib.add_raw_run(self.src.name, terminal="completed", artifacts_dir=str(self.src))
        sample = self.route() and json.loads((self.ad / "maintain-sample.json").read_text())
        self.assertNotIn(self.src.name, [s["run_id"] for s in sample["completed"]])

    def test_candidate_pending(self):
        self.seed(3, 3)
        self.lib.add_skill("cand", status="candidate")
        route = self.route()
        self.assertEqual((route["propose"], route["reason"], route["candidate"]), ("no", "candidate_pending", "cand"))

    def test_too_few_runs(self):
        self.seed(2, 1)
        self.assertEqual(self.route()["reason"], "too_few_runs")

    def test_too_few_eligible(self):
        self.seed(3, 3, eligible=False)
        self.lib.add_raw_run("one", score=5.0)  # a single eligible run < window_size 3
        route = self.route()
        self.assertEqual((route["reason"], route["runs"], route["eligible"]), ("too_few_eligible", 7, 1))

    def test_nothing_to_fix(self):
        self.seed(0, 4, score=0.0)
        self.assertEqual(self.route()["reason"], "nothing_to_fix")

    def test_a_pending_candidate_beats_every_other_reason(self):
        self.lib.add_skill("cand", status="candidate")
        self.assertEqual(self.route()["reason"], "candidate_pending")


class Gates(Base):
    def setUp(self):
        super().setUp()
        self.src = self.fixture()
        self.chain(self.src)

    def test_wiki_gate_refuses_a_missing_patch(self):
        p = self.run_node("wiki-gate", self.src)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(self.last(p), "WIKI_GATE=FAIL maintainer wrote no wiki-patch.json")
        self.assert_typed(p, "wiki-gate")
        self.assertFalse((self.ad / "wiki-gate-result.json").exists())

    def test_wiki_gate_applies_an_empty_patch_and_commits(self):
        (self.ad / "wiki-patch.json").write_text(json.dumps(
            {"schema": sl.SCHEMA_WIKI_PATCH, "pages": [], "log": "nothing new this run"}))
        p = self.run_node("wiki-gate", self.src)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.last(p), "WIKI_GATE=PASS created=0 patched=0 index_regenerated=yes log_appended=yes")
        self.assert_typed(p, "wiki-gate")
        self.assertEqual(json.loads((self.ad / "wiki-gate-result.json").read_text())["result"], "PASS")
        self.assertEqual(len(self.evolve_commits()), 2)
        self.assertIn("wiki(api): nothing new this run", self.evolve_commits()[0])
        self.assertIn("nothing new this run", (self.lib.root / "api" / "wiki" / "log.md").read_text())

    def test_proposal_gate_refuses_a_missing_proposal(self):
        p = self.run_node("proposal-gate", self.src)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(self.last(p), "PROPOSAL_GATE=FAIL proposer wrote no skill-proposal.json")
        self.assert_typed(p, "proposal-gate")

    def test_proposal_gate_no_action_records_and_commits(self):
        (self.ad / "skill-proposal.json").write_text(json.dumps(
            {"schema": sl.SCHEMA_PROPOSAL, "action": "no_action", "rationale": "too few signals yet",
             "motivating_patterns": [], "traces_read": [], "rejected_reviewed": []}))
        p = self.run_node("proposal-gate", self.src)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("PROPOSAL_GATE=PASS action=no_action ", p.stdout)
        self.assertIn("SKILL_ADMIT=SKIP no_action", p.stdout)
        self.assertTrue(self.last(p).startswith("LIBRARY_COMMIT=OK sha="), p.stdout)
        self.assert_typed(p, "proposal-gate")
        gate = json.loads((self.ad / "proposal-gate-result.json").read_text())
        self.assertEqual((gate["result"], gate["action"]), ("PASS", "no_action"))
        events = [e["event"] for e in sl.read_impact(self.lib.root, "api")]
        self.assertEqual(events, ["no_action"])
        self.assertIn("propose(api): no_action", self.evolve_commits()[0])
        self.assertEqual(self.lib.porcelain("api"), "")

        # skill-admit on that gate result is a SKIP with nothing new to commit
        (self.ad / "skill-verdict.json").write_text(json.dumps(
            {"schema": sl.SCHEMA_VERDICT, "verdict": "SKIP", "reasons": ["no_action"], "checks": {}}))
        a = self.run_node("skill-admit", self.src)
        self.assertEqual(a.returncode, 0, a.stdout + a.stderr)
        self.assertIn("SKILL_ADMIT=SKIP", a.stdout)
        self.assertEqual(self.last(a), "LIBRARY_COMMIT=SKIP nothing to commit")
        self.assert_typed(a, "skill-admit")

    def test_proposal_gate_failure_is_committed_then_fails(self):
        (self.ad / "skill-proposal.json").write_text(json.dumps(
            {"schema": sl.SCHEMA_PROPOSAL, "action": "create", "skill": "x", "content": "# x\n",
             "rationale": "weak", "motivating_patterns": ["nope"], "traces_read": ["a"], "rejected_reviewed": []}))
        p = self.run_node("proposal-gate", self.src)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("PROPOSAL_GATE=FAIL ", p.stdout)
        self.assertEqual(json.loads((self.ad / "proposal-gate-result.json").read_text())["result"], "FAIL")
        self.assertEqual([e["event"] for e in sl.read_impact(self.lib.root, "api")], ["gate_failed"])
        self.assertIn("gate(api): proposal refused", self.evolve_commits()[0])
        self.assertEqual(self.lib.porcelain("api"), "")

    def test_skill_admit_refuses_a_missing_verdict(self):
        p = self.run_node("skill-admit", self.src)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(self.last(p), "SKILL_ADMIT=FAIL critic wrote no skill-verdict.json")
        self.assert_typed(p, "skill-admit")


class Report(Base):
    def test_done_after_the_chain_and_releases_the_lock(self):
        src = self.fixture()
        self.chain(src)
        self.assertTrue(self.lock().exists())
        p = self.run_node("report", src)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.last(p), f"SKILL_EVOLVE=OK run={src.name} repo=api wiki=none proposal=none outcome=none")
        self.assert_typed(p, "report")
        self.assertIn("EVOLVE_LOCK=RELEASED", p.stdout)
        self.assertFalse(self.lock().exists())
        for node in ("preflight", "trace-digest", "score-run", "evolve-route"):
            self.assertRegex(p.stdout, rf"(?m)^NODE {node}: (?!<)", node)
        self.assertIn("NODE wiki-gate: <not run>", p.stdout)
        self.assertIn("NODE proposal-gate: <not run>", p.stdout)
        self.assertIn("NODE preflight: PREFLIGHT=PASS ", p.stdout)
        self.assertIn('NODE evolve-route: {"candidate"', p.stdout)

    def test_not_ingested_is_a_failed_run_but_still_releases_the_lock(self):
        src = self.fixture()
        self.assertEqual(self.run_node("preflight", src).returncode, 0)
        p = self.run_node("report", src)
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertEqual(self.last(p), f"SKILL_EVOLVE=FAIL run {src.name} was not ingested (see node outputs above)")
        self.assert_typed(p, "report")
        self.assertIn("EVOLVE_LOCK=RELEASED", p.stdout)
        self.assertFalse(self.lock().exists())

    def test_never_releases_a_lock_it_does_not_own(self):
        src = self.fixture()
        self.chain(src)
        (self.lock() / "owner").write_text("someone-else\n")
        p = self.run_node("report", src)
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("EVOLVE_LOCK=NOT_OWNED", p.stdout)
        self.assertEqual((self.lock() / "owner").read_text().strip(), "someone-else")

    def test_silent_when_skip_is_a_failed_run(self):
        src = self.fixture()
        self.chain(src)
        (self.ad / "node-evolve-route.out").write_text('{"propose": "yes", "reason": "ok"}\n')
        p = self.run_node("report", src)
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertTrue(self.last(p).startswith("SKILL_EVOLVE=FAIL route said propose=yes but proposal-gate left no result"))
        self.assert_typed(p, "report")

    def test_reports_wiki_and_proposal_outcomes(self):
        src = self.fixture()
        self.chain(src)
        (self.ad / "wiki-gate-result.json").write_text(json.dumps({"result": "PASS"}))
        (self.ad / "proposal-gate-result.json").write_text(json.dumps({"result": "PASS", "action": "create"}))
        (self.ad / "node-skill-admit.out").write_text("SKILL_ADMIT=REJECTED skill=x reasons=2\nLIBRARY_COMMIT=OK sha=abc\n")
        p = self.run_node("report", src)
        self.assertEqual(self.last(p), f"SKILL_EVOLVE=OK run={src.name} repo=api wiki=PASS proposal=create outcome=REJECTED")
        (self.ad / "proposal-gate-result.json").write_text(json.dumps({"result": "FAIL", "action": "patch"}))
        p = self.run_node("report", src)
        self.assertTrue(self.last(p).endswith("wiki=PASS proposal=patch outcome=GATE_FAIL"), p.stdout)

    def test_prints_the_impact_tail(self):
        src = self.fixture()
        self.chain(src)
        for i in range(4):
            sl.record_impact(self.lib.root, "api", "no_action", "-", f"P-2026090{i}-abcdef01", self.rid_self,
                             reason=f"reason {i}")
        p = self.run_node("report", src)
        self.assertIn("SKILL_IMPACT_TAIL (api, last three sections)", p.stdout)
        self.assertNotIn("reason 0", p.stdout)
        for i in (1, 2, 3):
            self.assertIn(f"reason {i}", p.stdout)


if __name__ == "__main__":
    unittest.main()
