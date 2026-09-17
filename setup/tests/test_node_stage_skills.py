#!/usr/bin/env python3
"""stage-skills node (five lanes + five codex twins), engine-free.

Each lane's bash body is executed against a temp root whose `.archon/setup`
mirrors this checkout and whose `.archon/library` is seeded per test. The
routing negative controls plant a distinct sentinel step in every repository
so a lane that stages the wrong library is caught by content, not by exit code.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from nodes.extract import runnable_body
from nodes.runner import run_node
from skill_fixtures import TempLibrary, skill_text

SETUP = Path(__file__).resolve().parent.parent
LANES = ("full-sdlc-api", "full-sdlc-web", "bugfix", "full-sdlc-api-lite", "bugfix-lite")
TWINS = tuple(f"{lane}-codex" for lane in LANES)
ALL = LANES + TWINS
# Which repo the node must stage when params.json says nothing (web lane) or
# names a repo explicitly (api and bugfix lanes read it through params-env.sh).
DEFAULT_REPO = {"full-sdlc-api": "api", "full-sdlc-web": "web-app", "bugfix": "api",
                "full-sdlc-api-lite": "api", "bugfix-lite": "api"}
SENTINEL = {"api": "API-SENTINEL-STEP-1a7c", "goodword-mcp": "MCP-SENTINEL-STEP-9e2b",
            "web-app": "WEB-SENTINEL-STEP-5d0f"}


def family(lane):
    return lane[:-len("-codex")] if lane.endswith("-codex") else lane


def params(repo=None):
    p = {"spec": "/x.md", "slug": "x", "branch": "archon/x", "worktree": "/wt"}
    if repo is not None:
        p["repo"] = repo
    return p


class Root:
    """A temp `<root>` with `.archon/setup` mirrored and `.archon/library`
    pointing at a TempLibrary (git-less, so head is `none` and stable)."""

    def __init__(self, repos=("api", "goodword-mcp", "web-app"), library=True):
        self.dir = Path(tempfile.mkdtemp(prefix="ss-root-"))
        mirror = self.dir / ".archon" / "setup"
        mirror.mkdir(parents=True)
        for p in SETUP.iterdir():
            (mirror / p.name).symlink_to(p)
        self.lib = TempLibrary(repos=repos, git_init=False)
        if library:
            (self.dir / ".archon" / "library").symlink_to(self.lib.root)

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        self.lib.cleanup()

    def seed_sentinels(self):
        for repo, step in SENTINEL.items():
            self.lib.add_skill(f"{repo}-skill", repo=repo,
                               text=skill_text(f"{repo}-skill", steps=(step,)))


def run_lane(lane, root, artifacts, repo=None):
    (artifacts / "params.json").write_text(json.dumps(params(repo)))
    body = runnable_body(lane, "stage-skills", root=str(root.dir))
    return subprocess.run(["bash", "-c", body], capture_output=True, encoding="utf-8",
                          env=dict(os.environ, ARTIFACTS_DIR=str(artifacts)), cwd=str(root.dir))


class Base(unittest.TestCase):
    def artifacts(self):
        ad = Path(tempfile.mkdtemp(prefix="ss-art-"))
        self.addCleanup(shutil.rmtree, ad, ignore_errors=True)
        return ad

    def root(self, **kw):
        r = Root(**kw)
        self.addCleanup(r.cleanup)
        return r

    def last(self, p):
        return (p.stdout.strip().splitlines() or [""])[-1]


class EveryLane(Base):
    def test_no_library_is_skip(self):
        root = self.root(library=False)
        for lane in ALL:
            with self.subTest(lane=lane):
                ad = self.artifacts()
                (ad / "skills.md").write_text("stale")
                p = run_lane(lane, root, ad, repo=DEFAULT_REPO[family(lane)])
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertEqual(self.last(p),
                                 f"SKILLS_STAGE=SKIP repo={DEFAULT_REPO[family(lane)]} reason=no-library")
                self.assertFalse((ad / "skills.md").exists())
                self.assertEqual(json.loads((ad / "skills-staged.json").read_text())["result"], "SKIP")
                self.assertIn("SKILLS_STAGE=SKIP", (ad / "node-stage-skills.out").read_text())

    def test_active_and_candidate_are_staged(self):
        root = self.root()
        for repo in SENTINEL:
            root.lib.add_skill("zz-active", repo=repo, text=skill_text("zz-active", steps=("ACTIVE-STEP",)))
            root.lib.add_skill("aa-candidate", repo=repo, status="candidate",
                               text=skill_text("aa-candidate", steps=("CANDIDATE-STEP",)))
        for lane in ALL:
            with self.subTest(lane=lane):
                ad = self.artifacts()
                repo = DEFAULT_REPO[family(lane)]
                p = run_lane(lane, root, ad, repo=repo)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertEqual(self.last(p),
                                 f"SKILLS_STAGE=OK repo={repo} active=1 candidate=1 bytes="
                                 f"{sum(s['bytes'] for s in json.loads((ad / 'skills-staged.json').read_text())['skills'])} head=none")
                md = (ad / "skills.md").read_text()
                self.assertTrue(md.startswith(f'<staged-skills repo="{repo}" count="2">'))
                # active first even though the candidate sorts earlier by name
                self.assertLess(md.index("ACTIVE-STEP"), md.index("CANDIDATE-STEP"))
                self.assertNotIn("candidate", md.replace("aa-candidate", ""))
                j = json.loads((ad / "skills-staged.json").read_text())
                self.assertEqual([(s["name"], s["status"]) for s in j["skills"]],
                                 [("zz-active", "active"), ("aa-candidate", "candidate")])
                self.assertIsNone(j["library_head"])

    def test_oversized_skill_fails_closed(self):
        root = self.root()
        for repo in SENTINEL:
            root.lib.add_skill("big", repo=repo, text=skill_text("big", steps=("x" * 8200,)))
        for lane in ALL:
            with self.subTest(lane=lane):
                ad = self.artifacts()
                (ad / "skills.md").write_text("stale")
                p = run_lane(lane, root, ad, repo=DEFAULT_REPO[family(lane)])
                self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                self.assertTrue(self.last(p).startswith("SKILLS_STAGE=FAIL "), p.stdout)
                self.assertIn("cap 8192", self.last(p))
                self.assertFalse((ad / "skills.md").exists())
                self.assertEqual(json.loads((ad / "skills-staged.json").read_text())["result"], "FAIL")


class RepoRouting(Base):
    """Negative controls: the sentinel of exactly one repository reaches skills.md."""

    def assert_only(self, ad, repo):
        md = (ad / "skills.md").read_text()
        for other, step in SENTINEL.items():
            if other == repo:
                self.assertIn(step, md)
            else:
                self.assertNotIn(step, md, f"{other} leaked into {repo}")
        self.assertEqual(json.loads((ad / "skills-staged.json").read_text())["repo"], repo)

    def test_web_lane_with_repo_less_params_stages_web_app(self):
        root = self.root()
        root.seed_sentinels()
        for lane in ("full-sdlc-web", "full-sdlc-web-codex"):
            with self.subTest(lane=lane):
                ad = self.artifacts()
                p = run_lane(lane, root, ad, repo=None)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertIn("SKILLS_STAGE=OK repo=web-app ", self.last(p))
                self.assert_only(ad, "web-app")

    def test_web_lane_ignores_a_repo_key(self):
        # The web body passes --repo web-app literally: even a params.json that
        # names another repo cannot route it elsewhere.
        root = self.root()
        root.seed_sentinels()
        ad = self.artifacts()
        p = run_lane("full-sdlc-web", root, ad, repo="api")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assert_only(ad, "web-app")

    def test_api_lanes_follow_params_repo(self):
        root = self.root()
        root.seed_sentinels()
        for lane in ("full-sdlc-api", "full-sdlc-api-lite", "full-sdlc-api-codex", "full-sdlc-api-lite-codex"):
            for repo in ("api", "goodword-mcp"):
                with self.subTest(lane=lane, repo=repo):
                    ad = self.artifacts()
                    p = run_lane(lane, root, ad, repo=repo)
                    self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                    self.assertIn(f"SKILLS_STAGE=OK repo={repo} ", self.last(p))
                    self.assert_only(ad, repo)

    def test_api_lane_without_repo_key_defaults_to_api(self):
        root = self.root()
        root.seed_sentinels()
        ad = self.artifacts()
        p = run_lane("full-sdlc-api", root, ad, repo=None)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assert_only(ad, "api")

    def test_bugfix_lanes_follow_bound_repo(self):
        root = self.root()
        root.seed_sentinels()
        for lane in ("bugfix", "bugfix-lite", "bugfix-codex", "bugfix-lite-codex"):
            for repo in ("web-app", "goodword-mcp", "api"):
                with self.subTest(lane=lane, repo=repo):
                    ad = self.artifacts()
                    p = run_lane(lane, root, ad, repo=repo)
                    self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                    self.assertIn(f"SKILLS_STAGE=OK repo={repo} ", self.last(p))
                    self.assert_only(ad, repo)

    def test_unknown_repo_fails_before_staging(self):
        root = self.root()
        root.seed_sentinels()
        ad = self.artifacts()
        p = run_lane("full-sdlc-api", root, ad, repo="not-a-repo")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("REPO_PROFILE=FAIL unknown repo not-a-repo", p.stdout + p.stderr)
        self.assertFalse((ad / "skills.md").exists())
        self.assertFalse((ad / "skills-staged.json").exists())


class Determinism(Base):
    """One run_node stress per lane: identical typed lines and artifacts across
    concurrent repetitions against the same seeded library."""

    def test_each_lane_is_deterministic(self):
        root = self.root()
        root.seed_sentinels()
        root.lib.add_skill("cand", status="candidate", text=skill_text("cand", steps=("CAND",)))

        for lane in ALL:
            repo = DEFAULT_REPO[family(lane)]

            def fixture(tmp, repo=repo):
                (tmp / "artifacts" / "params.json").write_text(json.dumps(params(repo)))
                return None

            with self.subTest(lane=lane):
                s = run_node(lane, "stage-skills", fixture, root=str(root.dir))
                self.assertEqual(s["rc"], 0)
                self.assertTrue(any(l.startswith(f"SKILLS_STAGE=OK repo={repo} ") for l in s["typed"]), s["typed"])
                self.assertIn("skills.md", s["files"])
                self.assertIn("skills-staged.json", s["files"])
                self.assertIn(SENTINEL[repo], s["files"]["skills.md"])


if __name__ == "__main__":
    unittest.main()
