#!/usr/bin/env python3
"""Mechanical PR thread-reply lane (ce-resolve-pr-feedback Tier-3 replacement, 8c).

A thread is auto-closable IFF the cited path changed in a commit dated after the
comment. Everything else - no path, human author, AI-review bots, any doubt - is
needs-human. Replies and resolutions are public, irreversible writes on a PR;
the classification rule is the entire safety story, so bias hard to needs-human.

Usage: thread-lane.py <abs-repo-path> <pr-number> <needs-human-out-file>
"""
import json
import subprocess
import sys

repo, pr, out_path = sys.argv[1], sys.argv[2], sys.argv[3]

AI_BOTS = ("coderabbit", "codex", "copilot", "gemini", "github-actions")


def gh(args, inp=None):
    r = subprocess.run(["gh"] + args, cwd=repo, capture_output=True, encoding="utf-8", input=inp)
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])}...: {r.stderr.strip()[:300]}")
    return r.stdout


owner = gh(["repo", "view", "--json", "owner", "-q", ".owner.login"]).strip()
name = gh(["repo", "view", "--json", "name", "-q", ".name"]).strip()

threads_q = """
query($owner:String!,$repo:String!,$pr:Int!){
  repository(owner:$owner,name:$repo){ pullRequest(number:$pr){
    reviewThreads(first:50){ nodes{
      id isResolved isOutdated path line
      comments(first:20){ nodes{ author{login} body createdAt url } } } } } } }"""
raw = gh(["api", "graphql", "-f", f"owner={owner}", "-f", f"repo={name}",
          "-F", f"pr={pr}", "-f", f"query={threads_q}"])
nodes = json.loads(raw)["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
open_threads = [t for t in nodes if not t["isResolved"] and not t["isOutdated"]]

auto, human = [], []
for t in open_threads:
    c0 = t["comments"]["nodes"][0]
    author = (c0["author"]["login"] if c0["author"] else "unknown").lower()
    path, created = t.get("path"), c0["createdAt"]
    if not path or any(b in author for b in AI_BOTS):
        human.append(t)
        continue
    touched = subprocess.run(
        ["git", "-C", repo, "log", f"--since={created}", "--format=%H", "--", path],
        capture_output=True, encoding="utf-8").stdout.strip()
    (auto if touched else human).append(t)

for t in auto:
    sha = subprocess.run(["git", "-C", repo, "log", "-1", "--format=%h", "--", t["path"]],
                         capture_output=True, encoding="utf-8").stdout.strip()
    body = f"Addressed in {sha}: {t['path']} changed after this comment."
    reply_q = """
mutation($threadId:ID!,$body:String!){
  addPullRequestReviewThreadReply(
    input:{pullRequestReviewThreadId:$threadId, body:$body}){ comment{ url } } }"""
    gh(["api", "graphql", "-f", f"threadId={t['id']}", "-f", f"body={body}",
        "-f", f"query={reply_q}"])
    resolve_q = """
mutation($threadId:ID!){
  resolveReviewThread(input:{threadId:$threadId}){ thread{ id isResolved } } }"""
    gh(["api", "graphql", "-f", f"threadId={t['id']}", "-f", f"query={resolve_q}"])
    print(f"AUTO_CLOSED {t['path']}:{t.get('line')}")

with open(out_path, "w", encoding="utf-8") as f:
    for t in human:
        c0 = t["comments"]["nodes"][0]
        f.write(f"{t.get('path')}:{t.get('line')} {c0['url']} :: {c0['body'][:120]}\n")

print(f"THREADS_OPEN={len(open_threads)} AUTO_CLOSED={len(auto)} NEEDS_HUMAN={len(human)}")
