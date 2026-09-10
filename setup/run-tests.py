#!/usr/bin/env python3
import argparse
import unittest
from pathlib import Path


def test_cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from test_cases(item)
        else:
            yield item


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fail-closed unittest discovery for Archon setup tests")
    parser.add_argument("--start-directory", default=str(Path(__file__).resolve().parent / "tests"))
    parser.add_argument("--pattern", default="test*.py")
    parser.add_argument("--top-level-directory")
    parser.add_argument("--allow-zero", action="store_true")
    parser.add_argument("--exclude-note", action="append", default=[])
    parser.add_argument(
        "--allow-missing-execution",
        action="append",
        default=[],
        metavar="TEST_ID",
        help="Record an explicitly excluded discovered test that is not expected to execute",
    )
    args = parser.parse_args(argv)

    loader = unittest.defaultTestLoader
    suite = loader.discover(
        start_dir=args.start_directory,
        pattern=args.pattern,
        top_level_dir=args.top_level_directory,
    )
    cases = list(test_cases(suite))
    discovered = len(cases)
    exclusions = set(args.allow_missing_execution)
    if (len(exclusions) != len(args.allow_missing_execution)
            or not exclusions.issubset({case.id() for case in cases})):
        print("TEST_EXECUTION=FAIL reason=unknown-or-duplicate-exclusion")
        return 2
    suite = unittest.TestSuite(case for case in cases if case.id() not in exclusions)
    if discovered == 0:
        if args.allow_zero and args.exclude_note:
            print("TEST_DISCOVERY=SKIP discovered=0 exclusions=" + " | ".join(args.exclude_note))
            return 0
        print("TEST_DISCOVERY=FAIL discovered=0 (use --allow-zero with --exclude-note for intentional exclusions)")
        return 2

    print(f"TEST_DISCOVERY=OK discovered={discovered}")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    missing_allowed = len(args.allow_missing_execution)
    expected_executed = discovered - missing_allowed
    if expected_executed == 0:
        if args.allow_zero and args.exclude_note:
            print("TEST_EXECUTION=SKIP executed=0 exclusions="
                  + " | ".join(args.allow_missing_execution)
                  + " notes=" + " | ".join(args.exclude_note))
            return 0
        print("TEST_EXECUTION=FAIL executed=0 (use --allow-zero with --exclude-note for an intentional empty selection)")
        return 2
    if result.skipped:
        skipped = " | ".join(f"{case.id()}: {reason}" for case, reason in result.skipped)
        print("TEST_EXECUTION=FAIL reason=unexpected-skip exclusions=" + skipped)
        return 2
    if result.testsRun != expected_executed:
        suffix = ""
        if args.allow_missing_execution:
            suffix = " exclusions=" + " | ".join(args.allow_missing_execution)
        print(
            f"TEST_EXECUTION=FAIL discovered={discovered} executed={result.testsRun} "
            f"expected_executed={expected_executed}{suffix}"
        )
        return 2
    if result.wasSuccessful():
        suffix = ""
        if args.allow_missing_execution:
            suffix = " exclusions=" + " | ".join(args.allow_missing_execution)
        print(f"TEST_RUN=PASS executed={result.testsRun}{suffix}")
        return 0
    print(
        f"TEST_RUN=FAIL executed={result.testsRun} "
        f"failures={len(result.failures)} errors={len(result.errors)}"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
