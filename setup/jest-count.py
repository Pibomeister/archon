#!/usr/bin/env python3
"""Run a repository's existing jest command and print the integration count line.

run-joint-integration.py requires every command to print
ARCHON_INTEGRATION_TESTS=<n>; plain jest does not. This adapter appends
`--json --outputFile <tmp>` to the given argv (jest accepts both after any
pattern, and `bun run <script> -- …` forwards them), runs it in the current
directory with output passed through, then prints numPassedTests. Exit status
is the command's, or 1 when the report shows a failure or zero passed tests.

Usage (cwd = the candidate worktree):
  jest-count.py bun run test:integration -- apps/api/src/x.int.spec.ts
"""
import json
import os
import subprocess
import sys
import tempfile


def main(argv: list[str]) -> int:
    if not argv:
        print("JEST_COUNT=FAIL usage: jest-count.py <jest command argv...>")
        return 2
    fd, report = tempfile.mkstemp(prefix="jest-count-", suffix=".json")
    os.close(fd)
    try:
        rc = subprocess.run([*argv, "--json", "--outputFile", report], stdin=subprocess.DEVNULL).returncode
        try:
            with open(report, encoding="utf-8") as handle:
                data = json.load(handle)
            passed, failed = int(data.get("numPassedTests", 0)), int(data.get("numFailedTests", 0))
        except (OSError, ValueError):
            passed, failed = 0, 1
    finally:
        os.unlink(report)
    print(f"ARCHON_INTEGRATION_TESTS={passed}")
    if rc != 0:
        return rc
    if failed or not passed:
        print(f"JEST_COUNT=FAIL passed={passed} failed={failed}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
