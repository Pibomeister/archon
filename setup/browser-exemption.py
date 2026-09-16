#!/usr/bin/env python3
"""Decide whether a `not_applicable` browser policy is legitimate for a repo that
HAS a browser surface.

P11 (tests/test_repo_aware_params.py BrowserPolicyIsRepoGated) forbids the
artifact from switching the browser gate off by its own say-so. That rule stays:
the string is never sufficient. What this helper adds is a second, mechanical
source of truth that a planner cannot author: the repository PROFILE's
`browser_exempt` globs (repo-profile.sh), matched against the paths the run is
allowed to touch. A pure backend change -- a probe script, a migration, an async
lambda the local smoke stack never runs -- has no page to check, and before this
the lane could only complete it by writing a fictional browser policy.

The globs are the NON-surface list, not a surface list, so the check is
fail-closed: a path nobody classified is presumed browser-reachable. `*` in a
glob crosses `/` (fnmatch semantics); write them with that in mind.

Two modes, one rule:
  plan <artifacts-dir>
      At plan-shape time: every path in files-allowlist.json (the run's repo),
      web-files-allowlist.json (web-app) and each joint-plan.json stage
      allowlist must match its repo's exempt globs. Those allowlists are hashed
      at approval, so the exemption is frozen with them.
  diff <artifacts-dir> <worktree> <base-sha>
      After implement and at exit: every path the candidate actually changed
      must match. The allowlist is a plan; a human may widen it to clear a
      SCOPE_BREACH, and the fixer works within whatever it says by then. The
      diff is what ships, so the exemption is re-derived from it.

Exit 0 with BROWSER_EXEMPTION=NONE when the policy is populated or the repo has
no browser surface (nothing to decide), BROWSER_EXEMPTION=DERIVED when every
path is exempt, and 1 with BROWSER_EXEMPTION=FAIL naming the first surface path.
"""
import fnmatch
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def fail(message):
    print(f"BROWSER_EXEMPTION=FAIL {message}")
    raise SystemExit(1)


def load(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        if default is not None:
            return default
        fail(f"unreadable {Path(path).name}")


def profiles():
    out = subprocess.run(["bash", str(HERE / "repo-profile.sh"), "--json"],
                         capture_output=True, encoding="utf-8")
    if out.returncode != 0:
        fail("repo-profile.sh --json failed")
    return json.loads(out.stdout)["profiles"]


def check(pairs, registry):
    for repo, path in pairs:
        profile = registry.get(repo)
        if profile is None:
            fail(f"unknown repo {repo}")
        if not profile.get("browser"):
            continue
        globs = profile.get("browser_exempt") or []
        if not any(fnmatch.fnmatchcase(path, g) for g in globs):
            fail(f"not_applicable declared but repo={repo} path={path} is a browser surface "
                 f"(not matched by its profile's browser_exempt globs); write a populated browser policy")


def changed_paths(worktree, base):
    def git(*argv):
        r = subprocess.run(["git", "-C", worktree, *argv], capture_output=True, encoding="utf-8")
        if r.returncode != 0:
            fail(f"git {' '.join(argv)} failed: {r.stderr.strip()}")
        return [line for line in r.stdout.splitlines() if line]
    # The same install noise gate-tests' CONTENT_STATUS filter ignores.
    noise = {".env", "pnpm-lock.yaml"}
    return sorted((set(git("diff", "--name-only", base, "--"))
                   | set(git("ls-files", "--others", "--exclude-standard"))) - noise)


def main(argv):
    if len(argv) < 2 or argv[0] not in ("plan", "diff") or (argv[0] == "diff" and len(argv) != 4):
        print("usage: browser-exemption.py plan <artifacts-dir> | diff <artifacts-dir> <worktree> <base-sha>")
        return 2
    ad = Path(argv[1])
    if argv[0] == "diff" and not (ad / "browser-evidence.json").is_file():
        # No policy at all is not an exemption; plan-shape owns its presence.
        print("BROWSER_EXEMPTION=NONE no browser policy in this run")
        return 0
    policy = load(ad / "browser-evidence.json")
    reason = policy.get("not_applicable") if isinstance(policy, dict) else None
    if not (isinstance(reason, str) and reason.strip()):
        print("BROWSER_EXEMPTION=NONE policy is populated")
        return 0
    params = load(ad / "params.json", default={})
    repo = params.get("repo") or "api"
    registry = profiles()
    if argv[0] == "plan":
        pairs = [(repo, p) for p in load(ad / "files-allowlist.json")]
        pairs += [("web-app", p) for p in load(ad / "web-files-allowlist.json", default=[])]
        joint = ad / "joint-plan.json"
        if joint.is_file():
            for name, stage in (load(joint).get("stages") or {}).items():
                pairs += [(name, p) for p in stage.get("files_allowlist") or []]
    else:
        if not registry.get(repo, {}).get("browser"):
            print(f"BROWSER_EXEMPTION=NONE repo={repo} has no browser surface")
            return 0
        pairs = [(repo, p) for p in changed_paths(argv[2], argv[3])]
    check(pairs, registry)
    print(f"BROWSER_EXEMPTION=DERIVED mode={argv[0]} paths={len(pairs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
