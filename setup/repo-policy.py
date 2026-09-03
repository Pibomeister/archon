#!/usr/bin/env python3
"""Discover and enforce repository-local test placement policy for Archon."""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path

POLICY_GLOBS = (
    "AGENTS.md", "CLAUDE.md", ".claude/rules/*.md", ".claude/rules/**/*.md",
    ".cursor/rules/*.md", ".cursor/rules/**/*.md",
    ".github/**/*.md",
)
TEST_SUFFIXES = (".spec.ts", ".int.spec.ts", ".ai.spec.ts", ".ext.spec.ts", ".test.ts", ".test.js")
RULE_PATTERNS = (
    ("existing-test-preferred", "test-placement", r"existing test file|existing.*spec|extend.*existing|ultra-specific"),
    ("unit-test-colocation", "test-location", r"unit test location|__tests__.*folder|outside.*__tests__"),
    ("test-file-naming", "test-naming", r"test file naming|naming conventions|\.int\.spec|\.ai\.spec|\.ext\.spec"),
    ("no-real-timer-waits", "test-content", r"real timer|settimeout|timer waits"),
    ("no-barrel-imports", "test-content", r"barrel import|hub module|deep paths"),
    ("no-raw-sql-setup", "test-content", r"raw sql|datasource\.query|test setup.*repos"),
    ("unique-profile-fixtures", "test-content", r"profileRepo\.create|createUniqueTestProfile|collision-retrying"),
)


def fail(message: str) -> None:
    print(f"REPO_POLICY=FAIL {message}")
    raise SystemExit(1)


def write_json(path: Path, value) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def safe_file(path: Path, root: Path) -> Path:
    try:
        info = path.lstat()
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError) as exc:
        raise ValueError(str(exc)) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError("not a regular file")
    return path


def policy_files(repo: Path):
    found = set()
    for pattern in POLICY_GLOBS:
        for path in repo.glob(pattern):
            if path.is_file() and not path.is_symlink():
                found.add(path)
    return sorted(found)


def git_bytes(repo: Path, commit: str, relative: str) -> bytes | None:
    result = subprocess.run(["git", "-C", str(repo), "show", f"{commit}:{relative}"], capture_output=True)
    return result.stdout if result.returncode == 0 else None


def baseline_paths(repo: Path, commit: str | None) -> list[str]:
    if commit:
        result = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", "--name-only", commit],
                                capture_output=True, text=True)
        if result.returncode == 0:
            return [line for line in result.stdout.splitlines() if line]
    return [path.relative_to(repo).as_posix() for path in repo.rglob("*") if path.is_file() and not path.is_symlink()]


def policy_path_match(relative: str) -> bool:
    return any(fnmatch.fnmatch(relative, pattern) for pattern in POLICY_GLOBS)


def extract_rules(repo: Path, commit: str | None) -> tuple[list[dict], list[dict]]:
    documents, rules = [], []
    seen = set()
    paths = [path for path in baseline_paths(repo, commit) if policy_path_match(path)]
    paths.sort(key=lambda path: (
        0 if "test" in Path(path).name.lower() else
        1 if "/rules/" in f"/{path}" else 2,
        path,
    ))
    for rel in paths:
        raw = git_bytes(repo, commit, rel) if commit else safe_file(repo / rel, repo).read_bytes()
        if raw is None:
            continue
        text = raw.decode("utf-8", errors="replace")
        documents.append({"path": rel, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if not re.search(r"(?i)\bblocking\b", line):
                continue
            context = "\n".join(lines[max(0, index - 12): index + 2])
            for rule_id, kind, pattern in RULE_PATTERNS:
                if rule_id in seen or not re.search(pattern, context, re.I):
                    continue
                rules.append({
                    "id": rule_id, "kind": kind, "severity": "blocking",
                    "source": f"{rel}:{index + 1}",
                    "requirement": re.sub(r"\s+", " ", line.strip(" -*`")),
                })
                seen.add(rule_id)
    return documents, rules


def repo_roots(root: Path) -> dict[str, Path]:
    repos = {name: root / name for name in ("api", "web-app") if (root / name).is_dir()}
    if not repos:
        repos = {root.name: root}
    return repos


def snapshot(root: Path, artifacts: Path) -> None:
    result = {"schema_version": 1, "baseline": {}, "repositories": {}}
    chain_commits = {}
    try:
        chain_commits = json.loads((artifacts / "bugfix-chain.json").read_text())["baseline"]["commits"]
    except (OSError, KeyError, json.JSONDecodeError):
        chain_commits = {
            "api": os.environ.get("ARCHON_API_BASELINE"),
            "web-app": os.environ.get("ARCHON_WEB_BASELINE"),
        }
    for name, repo in repo_roots(root).items():
        commit = chain_commits.get(name)
        if not commit:
            resolved = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True)
            commit = resolved.stdout.strip() if resolved.returncode == 0 else None
        docs, rules = extract_rules(repo, commit)
        result["repositories"][name] = {"root": str(repo), "policy_documents": docs, "rules": rules}
        result["baseline"][name] = commit
    write_json(artifacts / "repo-policy.json", result)
    print(f"REPO_POLICY=SNAPSHOT repositories={len(result['repositories'])} rules={sum(len(x['rules']) for x in result['repositories'].values())}")


def is_test(path: str) -> bool:
    return any(path.endswith(suffix) for suffix in TEST_SUFFIXES)


def test_candidates(repo: Path, production: str, commit: str | None) -> list[str]:
    stem = Path(production).name
    for suffix in (".ts", ".js", ".tsx", ".jsx"):
        if stem.endswith(suffix):
            stem = stem[:-len(suffix)]
            break
    names = {stem + suffix for suffix in TEST_SUFFIXES}
    return sorted(path for path in baseline_paths(repo, commit) if Path(path).name in names)


def selected_repo(root: Path, artifacts: Path) -> tuple[str, Path, dict]:
    policy = json.loads((artifacts / "repo-policy.json").read_text(encoding="utf-8"))
    try:
        name = json.loads((artifacts / "repo.json").read_text(encoding="utf-8"))["repo"]
    except (OSError, KeyError, json.JSONDecodeError):
        name = "api" if "api" in policy["repositories"] else next(iter(policy["repositories"]))
    if name not in policy["repositories"]:
        fail(f"selected repository has no policy snapshot: {name}")
    return name, Path(policy["repositories"][name]["root"]), policy["repositories"][name]


def validate_plan(root: Path, artifacts: Path) -> None:
    if not (artifacts / "repo-policy.json").is_file():
        snapshot(root, artifacts)
    name, repo, policy = selected_repo(root, artifacts)
    plan = json.loads((artifacts / "fix-plan.json").read_text(encoding="utf-8"))
    failing = json.loads((artifacts / "failing-test.json").read_text(encoding="utf-8"))
    proposed = failing.get("test_file")
    if not isinstance(proposed, str) or not proposed:
        fail("failing-test.json has no test_file")
    productions = [path for path in plan.get("files", []) if isinstance(path, str) and not is_test(path)]
    rows = []
    rule_ids = {rule.get("id") for rule in policy.get("rules", [])}
    if "unit-test-colocation" in rule_ids and proposed.endswith(".spec.ts"):
        if "/__tests__/" not in f"/{proposed}" and not proposed.startswith("apps/api-e2e/"):
            source = next(rule["source"] for rule in policy["rules"] if rule["id"] == "unit-test-colocation")
            fail(f"TEST_LOCATION unit_spec_outside___tests__ proposed={proposed} rule={source}")
    if "test-file-naming" in rule_ids:
        kind = failing.get("kind")
        expected = {"integration": ".int.spec.ts", "unit": ".spec.ts", "vitest": ".spec.ts"}.get(kind)
        if expected and not proposed.endswith(expected):
            source = next(rule["source"] for rule in policy["rules"] if rule["id"] == "test-file-naming")
            fail(f"TEST_NAMING kind={kind} expected_suffix={expected} proposed={proposed} rule={source}")
    blocking = any(rule.get("id") == "existing-test-preferred" for rule in policy.get("rules", []))
    for production in productions:
        candidates = test_candidates(repo, production, json.loads((artifacts / "repo-policy.json").read_text())["baseline"].get(name))
        decision = "extend-existing" if proposed in candidates else "new-file"
        rows.append({"production_file": production, "proposed_test": proposed,
                     "existing_candidates": candidates, "decision": decision})
        if blocking and candidates and proposed not in candidates:
            source = next(rule["source"] for rule in policy["rules"] if rule["id"] == "existing-test-preferred")
            write_json(artifacts / "test-placement.json", {"schema_version": 1, "repo": name, "rows": rows})
            fail(f"TEST_PLACEMENT existing_spec={candidates[0]} proposed={proposed} rule={source}")
    write_json(artifacts / "test-placement.json", {"schema_version": 1, "repo": name, "rows": rows})
    print(f"REPO_POLICY=PASS repo={name} test={proposed} production_files={len(productions)}")


def validate_diff(root: Path, artifacts: Path) -> None:
    validate_plan(root, artifacts)
    name, repo, policy = selected_repo(root, artifacts)
    try:
        params = json.loads((artifacts / "params.json").read_text(encoding="utf-8"))
        worktree = Path(params["worktree"])
    except (OSError, KeyError, json.JSONDecodeError):
        worktree = repo
    try:
        base = (artifacts / "bootstrap-head.txt").read_text(encoding="utf-8").strip()
    except OSError:
        base = json.loads((artifacts / "repo-policy.json").read_text())["baseline"].get(name) or "HEAD"
    changed = subprocess.run(["git", "-C", str(worktree), "diff", "--name-status", base],
                             capture_output=True, text=True)
    if changed.returncode != 0:
        fail("cannot inspect final diff for repository policy")
    added_tests = []
    changed_tests = []
    productions = []
    for line in changed.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status, path = parts[0], parts[-1]
        if is_test(path):
            changed_tests.append(path)
            if status.startswith("A"):
                added_tests.append(path)
        elif not is_test(path):
            productions.append(path)
    status = subprocess.run(["git", "-C", str(worktree), "status", "--porcelain"],
                            capture_output=True, text=True)
    for line in status.stdout.splitlines():
        if line.startswith("?? ") and is_test(line[3:]):
            added_tests.append(line[3:])
            changed_tests.append(line[3:])
    blocking = any(rule.get("id") == "existing-test-preferred" for rule in policy.get("rules", []))
    commit = json.loads((artifacts / "repo-policy.json").read_text())["baseline"].get(name)
    for production in productions:
        candidates = test_candidates(repo, production, commit)
        if blocking and candidates:
            invalid = [test for test in added_tests if test not in candidates]
            if invalid:
                source = next(rule["source"] for rule in policy["rules"] if rule["id"] == "existing-test-preferred")
                fail(f"TEST_PLACEMENT_DIFF existing_spec={candidates[0]} added={invalid[0]} rule={source}")
    rule_ids = {rule.get("id") for rule in policy.get("rules", [])}
    for relative in sorted(set(changed_tests)):
        path = worktree / relative
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 1024 * 1024:
            fail(f"cannot inspect changed test safely: {relative}")
        diff = subprocess.run(
            ["git", "-C", str(worktree), "diff", "--unified=0", base, "--", relative],
            capture_output=True, text=True,
        )
        if diff.returncode != 0:
            fail(f"cannot inspect changed test diff safely: {relative}")
        added = "\n".join(
            line[1:] for line in diff.stdout.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        checks = (
            ("no-real-timer-waits", r"(?s)await\s+(?:new\s+Promise[^;]*setTimeout|[^;\n]*setTimeout\s*\()"),
            ("no-barrel-imports", r"(?m)^\s*import\b.*\bfrom\s+['\"]@lib/[^/'\"]+['\"]"),
            ("no-raw-sql-setup", r"(?is)\b(?:dataSource|manager|queryRunner)\.query\s*\(\s*[`'\"]\s*(?:insert|update|delete)\b"),
        )
        for rule_id, pattern in checks:
            if rule_id in rule_ids and re.search(pattern, added):
                source = next(rule["source"] for rule in policy["rules"] if rule["id"] == rule_id)
                fail(f"TEST_CONTENT rule_id={rule_id} file={relative} rule={source}")
        if "unique-profile-fixtures" in rule_ids:
            if re.search(r"(?<!Unique)\bcreateTestProfile\s*\(", added):
                source = next(rule["source"] for rule in policy["rules"] if rule["id"] == "unique-profile-fixtures")
                fail(f"TEST_CONTENT rule_id=unique-profile-fixtures file={relative} rule={source}")
    print("REPO_POLICY_DIFF=PASS")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("snapshot", "validate-plan", "validate-diff"):
        command = sub.add_parser(action)
        command.add_argument("--root", type=Path, required=True)
        command.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    try:
        if args.action == "snapshot":
            snapshot(args.root.resolve(), args.artifacts.resolve())
        elif args.action == "validate-plan":
            validate_plan(args.root.resolve(), args.artifacts.resolve())
        else:
            validate_diff(args.root.resolve(), args.artifacts.resolve())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        fail(str(exc))


if __name__ == "__main__":
    main()
