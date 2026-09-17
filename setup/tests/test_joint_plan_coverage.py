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


class VerifyMirrorTest(unittest.TestCase):
    """verify.json must mirror stages.<anchor>.test_patterns: the stage run is
    seeded from the joint plan, not from verify.json (chain 228a0717, 2026-09-15)."""

    def _run(self, verify_patterns):
        plan = base_plan(
            [scenario("negative: unauthorized access rejected", covers=["AC1"])],
            acceptance_criteria=[{"id": "AC1", "text": "unauthorized access is rejected"}],
        )
        with tempfile.TemporaryDirectory() as td:
            ad = Path(td)
            (ad / "params.json").write_text(json.dumps({"repositories": plan["repositories"], "repo": "goodword-mcp"}), encoding="utf-8")
            (ad / "joint-plan.json").write_text(json.dumps(plan), encoding="utf-8")
            (ad / "verify.json").write_text(json.dumps({"test_patterns": verify_patterns}), encoding="utf-8")
            return subprocess.run(["python3", str(VALIDATOR), str(ad)], capture_output=True, encoding="utf-8")

    def test_matching_mirror_passes(self):
        result = self._run(["groups.test.ts"])
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_drifted_mirror_fails(self):
        result = self._run(["groups.test.ts", "groups\\.int\\.spec"])
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("JOINT_PLAN=FAIL verify.json test_patterns differ from stages.goodword-mcp.test_patterns", result.stdout)


SPEC_WITH_PINS = """# Share links

## Interface (pinned)

- No other change to `POST /group/share`: the managed-group sentence is the only
  behaviour this ticket adds.
- `GroupService.reshareGroup` keeps its current signature.

## Pinned decisions

### goodword-mcp

- The `groups.list` tool response shape is frozen.

## Notes

- `somethingElse` is discussed here but this section is not pinned.
"""

PIN = {"symbol": "shareGroup", "file": "src/group.service.ts", "spec_line": 5,
       "allowed_change": "none"}


class PinCoverageTest(unittest.TestCase):
    """Whole-bullet equality, not substring.

    v1's planner kept the managed-group sentence and dropped the rest of the
    spec's pin, so the reviewer argued with a rule the plan no longer stated for
    four rounds. A substring check accepts that paraphrase; equality does not.
    """

    RULES = [
        "No other change to `POST /group/share`: the managed-group sentence is the only "
        "behaviour this ticket adds.",
        "`GroupService.reshareGroup` keeps its current signature.",
        "The `groups.list` tool response shape is frozen.",
    ]

    def _run(self, pins, spec_text=SPEC_WITH_PINS):
        plan = base_plan(
            [scenario("negative: unauthorized access rejected", covers=["AC1"])],
            acceptance_criteria=[{"id": "AC1", "text": "unauthorized access is rejected"}],
        )
        if pins is not None:
            plan["pinned_decisions"] = pins
        with tempfile.TemporaryDirectory() as td:
            ad = Path(td)
            spec = ad / "spec.md"
            spec.write_text(spec_text, encoding="utf-8")
            (ad / "params.json").write_text(
                json.dumps({"repositories": plan["repositories"], "spec": str(spec)}),
                encoding="utf-8")
            (ad / "joint-plan.json").write_text(json.dumps(plan), encoding="utf-8")
            return subprocess.run(["python3", str(VALIDATOR), str(ad)],
                                  capture_output=True, encoding="utf-8")

    def full_pins(self):
        return [{**PIN, "symbol": f"sym{i}", "rule": rule}
                for i, rule in enumerate(self.RULES)]

    def test_every_pinned_bullet_covered_passes(self):
        result = self._run(self.full_pins())
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("JOINT_PLAN=PINS declared=3 covered=3", result.stdout)

    def test_a_wrapped_bullet_is_one_rule(self):
        """The first bullet wraps; its rule is the folded sentence, not the first line."""
        result = self._run(self.full_pins())
        self.assertEqual(0, result.returncode, result.stdout)

    def test_a_paraphrased_rule_is_not_coverage(self):
        pins = self.full_pins()
        pins[0]["rule"] = "No other change to `POST /group/share`"
        result = self._run(pins)
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("JOINT_PLAN=FAIL pin coverage symbol=POST /group/share", result.stdout)

    def test_a_missing_bullet_fails_by_its_symbol(self):
        result = self._run(self.full_pins()[:2])
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("JOINT_PLAN=FAIL pin coverage symbol=groups.list", result.stdout)

    def test_a_repo_subsection_is_inside_its_pinned_section(self):
        result = self._run([p for p in self.full_pins() if "groups.list" not in p["rule"]])
        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn("symbol=groups.list", result.stdout)

    def test_an_unpinned_section_is_not_covered(self):
        """`somethingElse` lives under ## Notes and must not demand an entry."""
        result = self._run(self.full_pins())
        self.assertNotIn("somethingElse", result.stdout)

    def test_a_pin_with_no_file_guards_nothing(self):
        pins = self.full_pins()
        pins[1].pop("file")
        result = self._run(pins)
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("pinned decision is missing file", result.stdout)

    def test_a_spec_with_no_pinned_section_skips_the_check(self):
        result = self._run(None, spec_text="# Share links\n\n## Notes\n\n- `x` is fine.\n")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn("JOINT_PLAN=PINS", result.stdout)

    def test_a_spec_that_cannot_be_read_skips_the_check(self):
        plan = base_plan(
            [scenario("negative: unauthorized access rejected", covers=["AC1"])],
            acceptance_criteria=[{"id": "AC1", "text": "unauthorized access is rejected"}],
        )
        with tempfile.TemporaryDirectory() as td:
            ad = Path(td)
            (ad / "params.json").write_text(
                json.dumps({"repositories": plan["repositories"], "spec": str(ad / "gone.md")}),
                encoding="utf-8")
            (ad / "joint-plan.json").write_text(json.dumps(plan), encoding="utf-8")
            result = subprocess.run(["python3", str(VALIDATOR), str(ad)],
                                    capture_output=True, encoding="utf-8")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
