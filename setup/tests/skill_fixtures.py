#!/usr/bin/env python3
"""Shared fixtures for the skills-library tests.

Every write-side test needs the same thing: a throwaway git repository whose
`library/<repo>` skeleton exists and is committed, plus a way to seed skills
and pattern pages without going through the gated writers. Keeping that here
means a change to the skeleton shape is fixed in one place.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SETUP = Path(__file__).resolve().parent.parent
if str(SETUP) not in sys.path:
    sys.path.insert(0, str(SETUP))

import skill_library as sl  # noqa: E402

GIT_ENV = {
    "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@test",
    "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@test",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}


def git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          encoding="utf-8", env=dict(os.environ, **GIT_ENV))


def skill_text(name, description="Do the thing carefully.", steps=("Read the plan.", "Run the tests.")):
    body = "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)) + "\n"
    return sl.render_frontmatter({"name": name, "description": description}, body)


def pattern_text(slug, runs=("r-0001",), kind="failure", title=None, support=None,
                 skills=(), status="active", evidence=None):
    meta = {
        "slug": slug, "title": title or f"Pattern {slug}", "kind": kind,
        "support_count": support if support is not None else len(runs),
        "first_observed": "2026-09-01T00:00:00Z", "last_observed": "2026-09-01T00:00:00Z",
        "runs": list(runs), "skills": list(skills), "status": status,
    }
    sections = {
        "Problem": "The review loop re-raises the same finding.",
        "Root cause": "The fixer edits the symptom, not the shared helper.",
        "Evidence": evidence or "\n".join(f"- run:{r}/round-1/fixer-result.json shows the repeat" for r in runs),
        "Action sequence": "1. Locate the helper.\n2. Patch it once.",
        "Known fix": "Patch the helper before the call sites.",
    }
    return sl.render_pattern(meta, sections)


class TempLibrary:
    """A temp git repo with library/<repo> skeletons committed.

    lib.root      -> <tmp>/library
    lib.top       -> <tmp> (git toplevel)
    """

    def __init__(self, repos=("api",), git_init=True, prefix="sklib-"):
        self.top = Path(tempfile.mkdtemp(prefix=prefix))
        self.root = self.top / "library"
        self.repos = tuple(repos)
        self.git = git_init
        if git_init:
            assert git(["init", "-q", "-b", "main"], self.top).returncode == 0
        for repo in self.repos:
            sl.ensure_skeleton(self.root, repo)
        if git_init:
            self.commit("skeleton")

    def cleanup(self):
        shutil.rmtree(self.top, ignore_errors=True)

    def commit(self, message="fixture"):
        assert git(["add", "-A"], self.top).returncode == 0
        r = git(["commit", "-q", "--allow-empty", "-m", message], self.top)
        assert r.returncode == 0, r.stderr
        return git(["rev-parse", "HEAD"], self.top).stdout.strip()

    def head(self):
        return git(["rev-parse", "HEAD"], self.top).stdout.strip()

    def log(self, repo=None):
        args = ["log", "--format=%H %an <%ae> %s"]
        if repo:
            args += ["--", f"library/{repo}"]
        return git(args, self.top).stdout.strip().splitlines()

    def porcelain(self, repo):
        return git(["status", "--porcelain", "--", f"library/{repo}"], self.top).stdout

    def paths(self, repo="api"):
        return sl.paths(self.root, repo)

    def index(self, repo="api"):
        return sl.read_json(self.paths(repo)["skills_index"])

    def write_index(self, index, repo="api"):
        sl.write_json_atomic(self.paths(repo)["skills_index"], index)

    def add_skill(self, name, status="active", text=None, repo="api", proposal="P-20260901-fixture",
                  purpose="origin: fixture\n", rollback=None, scoring=None, candidate_since=None):
        """Seed a skill directly (bypassing the gate) and register it."""
        text = text if text is not None else skill_text(name)
        d = Path(sl.skill_dir(self.root, repo, name))
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(text, encoding="utf-8")
        (d / "PURPOSE.md").write_text(purpose, encoding="utf-8")
        index = self.index(repo)
        entry = {
            "status": status, "proposal": proposal, "action": "create",
            "sha256": sl.sha256_bytes(text.encode("utf-8")),
            "candidate_since": candidate_since, "activated_at": None,
            "rollback": rollback, "scoring": scoring, "history": [],
        }
        if status == "candidate":
            entry["candidate_since"] = candidate_since or "2026-09-02T00:00:00Z"
            if rollback is None:
                snap = Path(self.paths(repo)["rollback_dir"]) / name
                snap.mkdir(parents=True, exist_ok=True)
                (snap / sl.ABSENT_MARKER).write_text("", encoding="utf-8")
                entry["rollback"] = {"existed": False, "sha256": None, "snapshot": f".rollback/{name}"}
            if scoring is None:
                entry["scoring"] = {"window_size": index["window_size"], "baseline_runs": [], "candidate_runs": []}
        elif status == "active":
            entry["activated_at"] = "2026-09-01T00:00:00Z"
        elif status == "rolled_back":
            shutil.rmtree(d)
            entry["sha256"] = None
            entry["rollback"] = rollback or {"existed": False, "sha256": None, "snapshot": f".rollback/{name}"}
        index["skills"][name] = entry
        self.write_index(index, repo)
        return entry

    def add_pattern(self, slug, repo="api", text=None, **kw):
        text = text if text is not None else pattern_text(slug, **kw)
        p = Path(sl.pattern_path(self.root, repo, slug))
        p.write_text(text, encoding="utf-8")
        sl.regenerate_index_md(self.root, repo)
        return text

    def add_raw_run(self, run_id, repo="api", **fields):
        row = {
            "schema": sl.SCHEMA_RAW, "run_id": run_id,
            "artifacts_dir": str(self.top / "runs" / run_id), "lane": "feature",
            "terminal": "completed", "outcome": "CHANGED", "eligible": True,
            "score": 10.0, "score_version": 1, "skills_staged": [],
            "library_head": None, "digest_sha256": None, "evolve_run_id": f"ev-{run_id}",
            "ingested_at": "2026-09-01T00:00:00Z",
        }
        row.update(fields)
        sl.append_jsonl(self.paths(repo)["raw_ledger"], row)
        return row


def write_json(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")
