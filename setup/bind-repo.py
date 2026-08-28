#!/usr/bin/env python3
"""Bugfix-lane repo binding: the home repo is a FINDING of the RCA, so the
provisional params.json written by resolve-params.sh gets rewritten here,
after the human approval gate. Adds "repo" and corrects "worktree" to
<root>/<repo>/.worktrees/<slug>. Idempotent: same repo.json, same params.
"both" is a typed v1 hard stop — every downstream mechanism is
single-repo-shaped; the operator splits the report or escalates.
Usage: bind-repo.py <params.json> <repo.json> <goodword-root>"""
import json
import sys

params_path, repo_path, root = sys.argv[1], sys.argv[2], sys.argv[3]
repo = json.load(open(repo_path, encoding="utf-8"))["repo"]
if repo == "both":
    sys.exit("BIND=FAIL CROSS_REPO_BUG (v1 is single-repo: split the bug report per repo, or escalate)")
if repo not in ("api", "web-app"):
    sys.exit(f"BIND=FAIL unknown repo {repo!r} (expected api or web-app)")
params = json.load(open(params_path, encoding="utf-8"))
params["repo"] = repo
params["worktree"] = f"{root}/{repo}/.worktrees/{params['slug']}"
json.dump(params, open(params_path, "w", encoding="utf-8"), indent=2)
print(f"BIND=OK repo={repo} worktree={params['worktree']}")
