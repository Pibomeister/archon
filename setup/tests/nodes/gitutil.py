"""Shared `git -C <repo> ...` subprocess wrapper for the drill test suites."""
import subprocess


def git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=check
    )
