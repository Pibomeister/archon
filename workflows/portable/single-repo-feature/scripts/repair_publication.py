"""Frozen repair scope and compare-and-swap publication for qualified factory runs."""
from __future__ import annotations

import json
import http.client
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit


def git(root: Path, *args: str) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(["git", "--no-replace-objects", "-C", str(root), *args], check=True, capture_output=True, text=True, timeout=30, env=env).stdout


def pattern_regex(pattern: str):
    if not isinstance(pattern, str) or not pattern or pattern.startswith("/") or any(part in (".", "..") for part in pattern.split("/")) or any(char in pattern for char in "\\\0[]{}!"):
        raise ValueError("repair-publication-unsupported-file-glob")
    expression, index = "", 0
    while index < len(pattern):
        if pattern[index:index + 3] == "**/":
            expression += "(?:.*/)?"
            index += 3
        elif pattern[index:index + 2] == "**":
            expression += ".*"
            index += 2
        else:
            expression += {"*": "[^/]*", "?": "[^/]"}.get(pattern[index], re.escape(pattern[index]))
            index += 1
    return re.compile("^" + expression + "$")


def from_environment(binding: dict, profile: dict) -> dict | None:
    raw = os.environ.get("FACTORY_REPAIR_PUBLICATION")
    if not raw:
        if os.environ.get("FACTORY_REPAIR_ATTEMPT_ID"):
            raise ValueError("Repair publication context is missing")
        return None
    context = json.loads(raw)
    required = {"version", "repairAttemptId", "managedPrId", "pullRequestNumber", "readySnapshotId", "readyDigest", "originalReadyBaseRevision", "executionBaseRevision", "branch", "remoteUrl", "repository", "fileGlobs"}
    if not isinstance(context, dict) or set(context) != required or context["version"] != "factory.repair-publication.v1":
        raise ValueError("Invalid repair publication context")
    if context["repairAttemptId"] != os.environ.get("FACTORY_REPAIR_ATTEMPT_ID"):
        raise ValueError("Repair invocation identity mismatch")
    if not isinstance(context["managedPrId"], str) or not context["managedPrId"] or type(context["pullRequestNumber"]) is not int or context["pullRequestNumber"] <= 0:
        raise ValueError("Invalid managed PR identity")
    for field in ("originalReadyBaseRevision", "executionBaseRevision"):
        if not isinstance(context[field], str) or not re.fullmatch("[a-f0-9]{40}", context[field]):
            raise ValueError("Invalid repair revision")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", context["readyDigest"]) or not context["readySnapshotId"]:
        raise ValueError("Invalid repair Ready binding")
    if context["executionBaseRevision"] != binding["baseCommit"] or context["branch"] != binding["branch"]:
        raise ValueError("Repair execution base or branch mismatch")
    # The launch guard derives this URL from the registered source checkout, not the spec.
    # The portable profile independently binds its repository identity.
    def identity(url):
        match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?", url)
        if not match:
            raise ValueError("Unqualified repair repository URL")
        return match[1].lower()
    expected = context["repository"]
    if expected.get("provider") != "github" or identity(context["remoteUrl"]) != identity(profile["repository"]["remote"]) or identity(context["remoteUrl"]) != (expected["owner"] + "/" + expected["name"]).lower():
        raise ValueError("Repair repository mismatch")
    if not isinstance(context["fileGlobs"], list) or not context["fileGlobs"]:
        raise ValueError("Repair scope is missing")
    for pattern in context["fileGlobs"]:
        pattern_regex(pattern)
    return context


def assert_remote_head(root: Path, context: dict) -> None:
    ref = "refs/heads/" + context["branch"]
    git(root, "check-ref-format", ref)
    if git(root, "symbolic-ref", "HEAD").strip() != ref:
        raise ValueError("repair-publication-local-branch-mismatch")
    observed = git(root, "ls-remote", "--refs", "--exit-code", "--", context["remoteUrl"], ref).strip()
    if observed != context["executionBaseRevision"] + "\t" + ref:
        raise ValueError("repair-remote-head-changed")


def assert_scope(root: Path, context: dict, revision: str | None = None) -> None:
    args = ["diff", "--no-ext-diff", "--no-renames", "--name-only", "-z", context["executionBaseRevision"]]
    if revision is not None:
        args.append(revision)
    paths = git(root, *args, "--").split("\0")
    # Reverted forbidden edits still leak through published commit history.
    commits = git(root, "rev-list", context["executionBaseRevision"] + ".." + (revision or "HEAD"), "--").splitlines()
    for commit in commits:
        paths += git(root, "diff-tree", "--no-commit-id", "--no-ext-diff", "--no-renames", "--name-only", "-r", "-m", "-z", commit, "--").split("\0")
    if revision is None:
        paths += git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    patterns = [pattern_regex(pattern) for pattern in context["fileGlobs"]]
    for path in filter(None, paths):
        if not any(pattern.fullmatch(path) for pattern in patterns):
            raise ValueError("repair-publication-outside-file-scope:" + path)


def publish(root: Path, context: dict, head: str) -> None:
    if not re.fullmatch("[a-f0-9]{40}", head):
        raise ValueError("Invalid publication commit")
    # Resolve one immutable commit; never push mutable HEAD after validating a different SHA.
    git(root, "merge-base", "--is-ancestor", context["executionBaseRevision"], head)
    assert_scope(root, context, head)
    assert_current_authority(context)
    assert_remote_head(root, context)
    ref = "refs/heads/" + context["branch"]
    # Explicit expected SHA is a server-side CAS, independent of local remote-tracking refs.
    # Although Git names this option force-with-lease, ancestry above forbids history rewrite.
    # A plain push allows an intervening compatible head move, violating the frozen attempt.
    git(root, "push", "--porcelain", "--force-with-lease=" + ref + ":" + context["executionBaseRevision"], "--", context["remoteUrl"], head + ":" + ref)


def assert_current_authority(context: dict) -> None:
    # This ephemeral capability never enters the frozen context or evidence artifacts.
    # It authenticates only to the invocation's local broker, which holds the machine token.
    url = urlsplit(os.environ.get("FACTORY_REPAIR_AUTHORITY_URL", ""))
    token = os.environ.get("FACTORY_REPAIR_AUTHORITY_TOKEN", "")
    job = os.environ.get("FACTORY_JOB_ID", "")
    if url.scheme != "http" or url.hostname not in ("127.0.0.1", "::1") or not url.port or url.path != "/repair-publication/preflight" or url.query or url.fragment or url.username or url.password or not token or not job:
        raise ValueError("Repair publication authority broker unavailable")
    body = {"version": "factory.repair-publication-preflight.v1", "factoryJobId": job, "repairAttemptId": context["repairAttemptId"], "managedPrId": context["managedPrId"], "expectedHead": {**context["repository"], "branch": context["branch"], "headSha": context["executionBaseRevision"]}, "readySnapshotId": context["readySnapshotId"], "readyDigest": context["readyDigest"], "originalReadyBaseRevision": context["originalReadyBaseRevision"], "executionBaseRevision": context["executionBaseRevision"], "fileGlobs": context["fileGlobs"]}
    connection = http.client.HTTPConnection(url.hostname, url.port, timeout=10)
    try:
        connection.request("POST", url.path, body=json.dumps(body), headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read(65537)
        if response.status != 200 or len(raw) > 65536:
            raise ValueError("Repair publication authority rejected")
        result = json.loads(raw)
        if result.get("ok") is not True or result.get("repairAttemptId") != context["repairAttemptId"] or result.get("managedPrId") != context["managedPrId"] or result.get("headSha") != context["executionBaseRevision"]:
            raise ValueError("Repair publication authority binding mismatch")
    finally:
        connection.close()


def inspect_managed_pr(root: Path, context: dict, head: str) -> dict:
    repository = context["repository"]["owner"] + "/" + context["repository"]["name"]
    result = subprocess.run(["gh", "api", "--method", "GET", "repos/" + repository + "/pulls/" + str(context["pullRequestNumber"])], cwd=root, check=True, capture_output=True, text=True, timeout=30)
    pr = json.loads(result.stdout)
    expected_url = "https://github.com/" + repository + "/pull/" + str(context["pullRequestNumber"])
    if pr.get("number") != context["pullRequestNumber"] or pr.get("state") != "open" or pr.get("merged") is not False or pr.get("html_url", "").lower() != expected_url.lower() or type(pr.get("draft")) is not bool:
        raise ValueError("Managed PR identity or state changed")
    for side in ("head", "base"):
        if pr.get(side, {}).get("repo", {}).get("full_name", "").lower() != repository.lower():
            raise ValueError("Managed PR repository changed")
    if pr["head"].get("sha") != head or pr["head"].get("ref") != context["branch"]:
        raise ValueError("Managed PR head changed")
    return {"url": pr["html_url"], "draft": pr["draft"]}
