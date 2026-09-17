#!/usr/bin/env python3
"""Prepare a disposable candidate or stage worktree so a repository's normal
commands run in it.

A detached candidate worktree carries only tracked files. Chain worktrees under
<repo>/.worktrees/ get their dependencies from the root clone through Node's
ancestor lookup; a candidate under an artifacts directory has no such ancestor,
and ignored env files never follow a checkout at all. Planners used to paper
over that with per-feature bootstrap scripts. The repository profile now
declares what a checkout needs (`runtime_deps`, `runtime_env_files` in
repo-profile.sh) and this module resolves each entry, in order:

  1. already present in the target: left alone;
  2. present in the source worktree, else in the root clone that owns it:
     symlinked (a dependency directory only when the target's package manifest
     and lockfiles are byte-identical to that owner's, so a candidate that
     changed its dependencies never runs against stale ones);
  3. a dependency directory with no identical owner: the profile's install
     command runs in the target.

Anything still missing is a CANDIDATE_ENV=FAIL naming the entry and every place
searched. Nothing is copied: env files are credentials, and a symlink leaves no
second copy behind in an artifacts directory.

Usage: candidate_env.py <repo> <target-worktree> <source-worktree>
"""
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
INSTALL_TIMEOUT_SECONDS = 900


class CandidateEnvError(Exception):
    pass


def load_profile(repo: str) -> dict:
    out = subprocess.run(["bash", str(HERE / "repo-profile.sh"), "--json"], capture_output=True, encoding="utf-8")
    try:
        if out.returncode != 0:
            raise ValueError(out.stderr.strip())
        profile = json.loads(out.stdout)["profiles"].get(repo)
    except (ValueError, KeyError) as exc:
        raise CandidateEnvError(f"repo-profile.sh --json unusable: {exc}") from exc
    if profile is None:
        raise CandidateEnvError(f"unknown repo {repo}")
    return profile


def search_roots(source: Path) -> list[Path]:
    roots = [source]
    common = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True, encoding="utf-8",
    )
    if common.returncode == 0:
        clone = Path(common.stdout.strip()).parent
        if clone.resolve() != source.resolve():
            roots.append(clone)
    return roots


def manifests_match(owner: Path, target: Path, profile: dict) -> bool:
    for name in ("package.json", *profile["lockfiles"]):
        a, b = owner / name, target / name
        if a.is_file() != b.is_file() or (a.is_file() and a.read_bytes() != b.read_bytes()):
            return False
    return True


def install(target: Path, profile: dict, name: str) -> str:
    argv = profile["install"]
    try:
        done = subprocess.run(argv, cwd=target, stdin=subprocess.DEVNULL, capture_output=True,
                              encoding="utf-8", errors="replace", timeout=INSTALL_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CandidateEnvError(f"{name}: install {argv} did not run: {exc}") from exc
    if done.returncode != 0:
        tail = (done.stdout + done.stderr).strip().splitlines()[-5:]
        raise CandidateEnvError(f"{name}: install {argv} rc={done.returncode}: {' | '.join(tail)}")
    if not (target / name).is_dir():
        raise CandidateEnvError(f"{name}: install {argv} succeeded but produced no {name}")
    return "installed"


def link(target: Path, name: str, owner: Path) -> str:
    (target / name).symlink_to(owner / name)
    if not (target / name).exists():
        raise CandidateEnvError(f"{name}: link to {owner / name} does not resolve")
    return f"linked:{owner / name}"


def present(target: Path, name: str) -> bool:
    entry = target / name
    if entry.is_symlink() and not entry.exists():
        raise CandidateEnvError(f"{name}: {entry} is a dangling symlink to {entry.readlink()}")
    return entry.exists()


def resolve_dep(name: str, target: Path, roots: list[Path], profile: dict) -> str:
    if present(target, name):
        return "present"
    owners = [root for root in roots if (root / name).is_dir()]
    for owner in owners:
        if manifests_match(owner, target, profile):
            return link(target, name, owner)
    if not profile["install"]:
        raise CandidateEnvError(f"{name}: not found in {[str(r) for r in roots]} and the profile declares no install")
    return install(target, profile, name)


def resolve_env_file(name: str, target: Path, roots: list[Path]) -> str:
    if present(target, name):
        return "present"
    for root in roots:
        if (root / name).is_file():
            return link(target, name, root)
    raise CandidateEnvError(f"{name}: not found in {[str(r) for r in roots]}")


def prepare(repo: str, target: Path, source: Path) -> dict:
    profile = load_profile(repo)
    target, source = target.resolve(), source.resolve()
    if not target.is_dir():
        raise CandidateEnvError(f"target worktree missing: {target}")
    if not source.is_dir():
        raise CandidateEnvError(f"source worktree missing: {source}")
    roots = search_roots(source)
    try:
        return {
            "deps": {n: resolve_dep(n, target, roots, profile) for n in profile["runtime_deps"]},
            "env_files": {n: resolve_env_file(n, target, roots) for n in profile["runtime_env_files"]},
        }
    except CandidateEnvError as exc:
        raise CandidateEnvError(f"repo={repo} {exc}") from exc


def main() -> int:
    if len(sys.argv) != 4:
        print("CANDIDATE_ENV=FAIL usage: candidate_env.py <repo> <target-worktree> <source-worktree>")
        return 2
    repo, target, source = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    try:
        result = prepare(repo, target, source)
    except CandidateEnvError as exc:
        print(f"CANDIDATE_ENV=FAIL {exc}")
        return 1
    print(f"CANDIDATE_ENV=PASS repo={repo} {json.dumps(result, sort_keys=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
