#!/usr/bin/env python3
"""Reject verify.json / joint-plan stage test_patterns the UNIT runner cannot see.

gate-tests, deslop-recheck and exit-gate run every test_patterns entry through
the repository's unit command (repo-profile.sh CMD_TEST). That command's config
ignores some spec kinds -- api's jest skips .int/.ai/.ext specs -- so a planner
that lists one gets "No tests found" at gate-tests, after approval and after
implement, where no resume can fix it. The planner cannot be trusted to know
each repo's config, so the excluded kinds are PROFILE data (`unit_test_excludes`)
and this check runs at plan-shape, before the human gate.

A pattern is rejected when either:
  - the pattern text itself matches an exclude regex (a new spec path the
    planner will create, e.g. `x.int.spec.ts`), or
  - every tracked spec file in that repo's worktree the pattern selects is
    excluded (a bare stem that only names an integration spec).
A pattern selecting nothing yet is left alone: the implement node may create
the spec, and gate-tests reports a genuinely empty match itself.

Usage: check-unit-patterns.py <artifacts-dir>
"""
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC_FILE = re.compile(r"\.(spec|test)\.[cm]?[jt]sx?$")


def fail(message):
    print(f"UNIT_PATTERNS=FAIL {message}")
    raise SystemExit(1)


def load(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def tracked_specs(worktree):
    if not worktree or not Path(worktree).is_dir():
        return []
    r = subprocess.run(["git", "-C", str(worktree), "ls-files"], capture_output=True, encoding="utf-8")
    return [f for f in r.stdout.splitlines() if SPEC_FILE.search(f)] if r.returncode == 0 else []


def main(argv):
    if len(argv) != 1:
        print("usage: check-unit-patterns.py <artifacts-dir>")
        return 2
    ad = Path(argv[0])
    params = load(ad / "params.json", {}) or {}
    repo = params.get("repo") or "api"
    worktrees = dict(params.get("worktrees_by_repo") or {})
    worktrees.setdefault(repo, params.get("worktree"))
    out = subprocess.run(["bash", str(HERE / "repo-profile.sh"), "--json"], capture_output=True, encoding="utf-8")
    if out.returncode != 0:
        fail("repo-profile.sh --json failed")
    profiles = json.loads(out.stdout)["profiles"]

    pairs = [(repo, p) for p in (load(ad / "verify.json", {}) or {}).get("test_patterns") or []]
    for name, stage in ((load(ad / "joint-plan.json", {}) or {}).get("stages") or {}).items():
        pairs += [(name, p) for p in stage.get("test_patterns") or []]

    specs = {}
    for name, pattern in pairs:
        excludes = [re.compile(x) for x in (profiles.get(name) or {}).get("unit_test_excludes") or []]
        if not excludes or not isinstance(pattern, str):
            continue
        hint = (f"repo={name} pattern={pattern} only selects specs its unit test command ignores "
                f"({', '.join(x.pattern for x in excludes)}); list unit specs in test_patterns and put "
                f"integration/e2e coverage in verification or integration scenarios")
        if any(x.search(pattern) for x in excludes):
            fail(hint)
        try:
            selector = re.compile(pattern)
        except re.error:
            continue
        if name not in specs:
            specs[name] = tracked_specs(worktrees.get(name))
        selected = [f for f in specs[name] if selector.search(f)]
        if selected and all(any(x.search(f) for x in excludes) for f in selected):
            fail(hint + f" (selected: {', '.join(selected[:3])})")
    print(f"UNIT_PATTERNS=OK patterns={len(pairs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
