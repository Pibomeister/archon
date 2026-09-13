#!/usr/bin/env python3
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parents[1]
VALIDATOR = SETUP / "validate-joint-plan.py"


def scenario(name, covers=None, expected_tests=None):
    body = {
        "name": name,
        "uses": ["goodword-mcp"],
        "commands": ["pnpm test"],
        "expected_tests": expected_tests or [name],
    }
    if covers is not None:
        body["covers"] = covers
    return body


def base_plan(scenarios, acceptance_criteria=None):
    plan = {
        "schema": "archon.joint-feature-plan.v1",
        "repositories": ["goodword-mcp"],
        "dependency_order": ["goodword-mcp"],
        "stages": {
            "goodword-mcp": {
                "depends_on": [],
                "files_allowlist": ["src/tools/groups.ts"],
                "test_patterns": ["groups.test.ts"],
                "verification": ["pnpm test"],
            }
        },
        "contracts": [],
        "integration": {"scenarios": scenarios},
    }
    if acceptance_criteria is not None:
        plan["acceptance_criteria"] = acceptance_criteria
    return plan


def run_validator(plan):
    with tempfile.TemporaryDirectory() as td:
        ad = Path(td)
        (ad / "params.json").write_text(json.dumps({"repositories": plan["repositories"]}), encoding="utf-8")
        (ad / "joint-plan.json").write_text(json.dumps(plan), encoding="utf-8")
        return subprocess.run(["python3", str(VALIDATOR), str(ad)], capture_output=True, encoding="utf-8")


class JointPlanCoverageTest(unittest.TestCase):
    def test_positive_only_scenarios_fail_rule_1(self):
        plan = base_plan(
            [scenario("mcp local success", covers=["AC1"])],
            acceptance_criteria=[{"id": "AC1", "text": "success path works"}],
        )
        result = run_validator(plan)
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("JOINT_PLAN=FAIL integration has no negative scenario", result.stdout)

    def test_uncovered_criterion_fails_rule_2(self):
        plan = base_plan(
            [scenario("negative: unauthorized access rejected", covers=["AC1", "AC2"])],
            acceptance_criteria=[
                {"id": "AC1", "text": "success path works"},
                {"id": "AC2", "text": "unauthorized access is rejected"},
                {"id": "AC3", "text": "quota is enforced"},
            ],
        )
        result = run_validator(plan)
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("JOINT_PLAN=FAIL acceptance criterion AC3 is uncovered", result.stdout)

    def test_unknown_covered_id_fails_rule_2(self):
        plan = base_plan(
            [scenario("negative: forbidden action rejected", covers=["AC1", "AC9"])],
            acceptance_criteria=[{"id": "AC1", "text": "forbidden action is rejected"}],
        )
        result = run_validator(plan)
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("JOINT_PLAN=FAIL unknown covered criterion AC9", result.stdout)

    def test_complete_plan_passes(self):
        plan = base_plan(
            [
                scenario("happy path", covers=["AC1"]),
                scenario("negative: unauthorized rejected", covers=["AC2"]),
            ],
            acceptance_criteria=[
                {"id": "AC1", "text": "success path works"},
                {"id": "AC2", "text": "unauthorized access is rejected"},
            ],
        )
        result = run_validator(plan)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("JOINT_PLAN=PASS", result.stdout)
        self.assertNotIn("JOINT_PLAN=WARN", result.stdout)

    def test_legacy_plan_passes_with_warn(self):
        plan = base_plan([scenario("mcp local success")])
        result = run_validator(plan)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("JOINT_PLAN=WARN no acceptance_criteria (legacy plan)", result.stdout)
        self.assertIn("JOINT_PLAN=PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
