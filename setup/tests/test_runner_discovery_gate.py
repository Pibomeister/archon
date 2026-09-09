#!/usr/bin/env python3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


RUNNER = Path(__file__).resolve().parent.parent / "run-tests.py"


class TestDiscoveryGateTest(unittest.TestCase):
    def write_test(self, root: Path, body: str) -> None:
        (root / "test_sample.py").write_text(textwrap.dedent(body), encoding="utf-8")

    def run_runner(self, root: Path, *args):
        return subprocess.run(
            [sys.executable, str(RUNNER), "--start-directory", str(root), *args],
            capture_output=True,
            text=True,
        )

    def test_zero_discovered_tests_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            result = self.run_runner(Path(td))

        self.assertEqual(result.returncode, 2)
        self.assertIn("TEST_DISCOVERY=FAIL discovered=0", result.stdout)

    def test_explicit_exclusion_can_account_for_empty_selection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = self.run_runner(root, "--allow-zero", "--exclude-note", "external fixture unavailable")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("TEST_DISCOVERY=SKIP discovered=0", result.stdout)
        self.assertIn("external fixture unavailable", result.stdout)

    def test_discovered_failure_is_executed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.write_test(
                root,
                """
                import unittest

                class Sample(unittest.TestCase):
                    def test_failure_is_not_ignored(self):
                        self.fail("injected failure")
                """,
            )
            result = self.run_runner(root)

        self.assertEqual(result.returncode, 1)
        self.assertIn("TEST_DISCOVERY=OK discovered=1", result.stdout)
        self.assertIn("injected failure", result.stderr)

    def test_discovered_test_with_shadowed_run_fails_when_not_executed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.write_test(
                root,
                """
                import unittest

                class ShadowedRun(unittest.TestCase):
                    def run(self, *args):
                        return None

                    def test_injected_failure_must_not_disappear(self):
                        self.fail("would fail if executed")
                """,
            )
            result = self.run_runner(root)

        self.assertEqual(result.returncode, 2)
        self.assertIn("TEST_DISCOVERY=OK discovered=1", result.stdout)
        self.assertIn("TEST_EXECUTION=FAIL discovered=1 executed=0 expected_executed=1", result.stdout)

    def test_missing_execution_requires_named_exclusion(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.write_test(
                root,
                """
                import unittest

                class ShadowedRun(unittest.TestCase):
                    def run(self, *args):
                        return None

                    def test_documented_external_exclusion(self):
                        self.fail("excluded test body is not run")
                """,
            )
            result = self.run_runner(
                root,
                "--allow-missing-execution",
                "test_sample.ShadowedRun.test_documented_external_exclusion",
                "--allow-zero", "--exclude-note", "external fixture unavailable",
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("TEST_EXECUTION=SKIP executed=0", result.stdout)
        self.assertIn("test_sample.ShadowedRun.test_documented_external_exclusion", result.stdout)

    def test_unexpected_unittest_skip_cannot_count_as_execution(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.write_test(root, """
                import unittest
                class Sample(unittest.TestCase):
                    @unittest.skip("unavailable fixture")
                    def test_never_executed(self):
                        self.fail("must not be reported as executed")
            """)
            result = self.run_runner(root)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("TEST_EXECUTION=FAIL reason=unexpected-skip", result.stdout)
        self.assertIn("test_sample.Sample.test_never_executed", result.stdout)
        self.assertIn("unavailable fixture", result.stdout)
        self.assertNotIn("TEST_RUN=PASS", result.stdout)

    def test_excluding_all_tests_requires_explicit_zero_disposition(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.write_test(root, "import unittest\nclass Sample(unittest.TestCase):\n def test_one(self): self.fail('excluded')\n")
            result = self.run_runner(root, "--allow-missing-execution", "test_sample.Sample.test_one")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("TEST_EXECUTION=FAIL executed=0", result.stdout)
        self.assertNotIn("TEST_RUN=PASS", result.stdout)

    def test_unknown_or_duplicate_exclusion_cannot_mask_execution(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.write_test(root, "import unittest\nclass Sample(unittest.TestCase):\n def test_one(self): self.fail('must run')\n")
            for ids in (("not-a-test",), ("test_sample.Sample.test_one", "test_sample.Sample.test_one")):
                args = [argument for test_id in ids for argument in ("--allow-missing-execution", test_id)]
                result = self.run_runner(root, *args)
                with self.subTest(ids=ids):
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("unknown-or-duplicate-exclusion", result.stdout)

    def test_injected_controller_assertion_really_executes(self):
        setup = RUNNER.parent
        source = (setup / "tests/test_controller_attest.py").read_text()
        anchor = "    def test_proof_seal_is_private_external_and_verified(self):\n"
        self.assertEqual(source.count(anchor), 1)
        source = source.replace("SETUP = Path(__file__).resolve().parent.parent", f"SETUP = Path({str(setup)!r})")
        source = source.replace(anchor, anchor + '        self.fail("injected-controller-negative-control")\n')
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "test_controller_injected.py"
            path.write_text(source)
            result = self.run_runner(Path(td))
        self.assertEqual(result.returncode, 1)
        self.assertIn("injected-controller-negative-control", result.stderr)
        self.assertIn("TEST_RUN=FAIL executed=4 failures=1 errors=0", result.stdout)


if __name__ == "__main__":
    unittest.main()
