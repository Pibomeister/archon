import copy
import importlib.util
from pathlib import Path
import sys
import unittest

SETUP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SETUP))
import review_delta_workflow as rw


class DeltaWorkflowTests(unittest.TestCase):
    def fixture(self):
        return {"name": "full-sdlc-api-codex", "provider": "codex", "nodes": [
            {"id": "plan-approval", "approval": True},
            {"id": "review-loop", "depends_on": ["commit-impl"], "loop_group": {
                "max_iterations": 4, "until": "REVIEW_CONVERGED", "nodes": [
                    {"id": "round-pre", "bash": "old pre"},
                    {"id": "review", "prompt": "base:origin/main"},
                    {"id": "review-gate", "bash": "old gate"},
                    {"id": "commit-fixes", "depends_on": ["review-gate"], "bash": "check-review-scope"},
                    {"id": "fixer", "prompt": "old fixer", "timeout": 6000},
                    {"id": "commit-fixer", "depends_on": ["fixer"], "bash": "scope-check && commit"},
                    {"id": "converge", "bash": "yield-stop"},
                ]}},
            {"id": "integration", "bash": "exact-candidate-verification"},
        ]}

    def test_only_review_loop_changes_and_stock_source_is_not_mutated(self):
        source = self.fixture()
        untouched = copy.deepcopy(source)
        effective = rw.transform(source, SETUP)
        self.assertEqual(source, untouched)
        self.assertEqual(effective["nodes"][0], {"id": "plan-approval", "approval": True})
        self.assertEqual(effective["nodes"][-1], {"id": "integration", "bash": "exact-candidate-verification"})
        loop = effective["nodes"][1]["loop_group"]
        self.assertEqual(loop["max_iterations"], 4)
        commit = next(node for node in loop["nodes"] if node["id"] == "delta-commit-fixer")
        self.assertEqual(commit["bash"], "scope-check && commit")

    def test_delta_base_is_explicit_and_convergence_has_no_yield_shortcut(self):
        nodes = rw.transform(self.fixture(), SETUP)["nodes"][1]["loop_group"]["nodes"]
        review = next(node for node in nodes if node["id"] == "delta-review-1")
        self.assertIn("base:<brief.candidate.base>", review["prompt"])
        self.assertNotIn("base:origin/main", review["prompt"])
        self.assertIn("mode:report-only", review["prompt"])
        self.assertIn("converge --artifacts", nodes[-1]["bash"])
        self.assertNotIn("yield-stop", nodes[-1]["bash"])
        old_completed_node_ids = {"round-pre", "review", "review-gate", "commit-fixes", "fixer", "commit-fixer", "converge"}
        self.assertFalse(old_completed_node_ids.intersection(node["id"] for node in nodes))

    def test_reviewers_are_conditional_and_limited_to_three_concurrent_slots(self):
        nodes = rw.transform(self.fixture(), SETUP)["nodes"][1]["loop_group"]["nodes"]
        reviews = [node for node in nodes if node["id"].removeprefix("delta-review-").isdigit()]
        self.assertEqual(len(reviews), 9)
        for index, node in enumerate(reviews, 1):
            self.assertEqual(node["when"], f"$delta-round-pre.review_{index} == 'yes'")
        self.assertEqual(reviews[3]["depends_on"], ["delta-checkpoint-1", "delta-checkpoint-2", "delta-checkpoint-3"])
        self.assertEqual(reviews[6]["depends_on"], ["delta-checkpoint-4", "delta-checkpoint-5", "delta-checkpoint-6"])
        checkpoint = next(node for node in nodes if node["id"] == "delta-checkpoint-1")
        self.assertIn("complete --slot 1 --artifacts", checkpoint["bash"])

    def test_unknown_shapes_and_other_providers_fail_closed(self):
        source = self.fixture()
        source["provider"] = "claude"
        with self.assertRaises(rw.DeltaWorkflowError):
            rw.transform(source, SETUP)
        source = self.fixture()
        source["nodes"][1]["loop_group"]["nodes"].append({"id": "unexpected"})
        with self.assertRaises(rw.DeltaWorkflowError):
            rw.transform(source, SETUP)

    def test_real_captured_shape_is_supported_without_changing_integration(self):
        spec = importlib.util.spec_from_file_location("derive_lite_delta_test", SETUP / "derive-lite.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # bugfix-codex, not full-sdlc-api-codex: the api lane's review-loop is the
        # v2 seven-node shape now (round-pre, review, review-gate, fix-plan, fixer,
        # commit-fixer, converge), and transform() pins the v1 list and refuses on
        # anything else. That refusal is TYPED and deliberate -- the pilot must
        # re-capture the api lane once transform learns the v2 list -- so this test
        # follows the v1 shape to the lane that still has it rather than asserting
        # a rewrite that would be wrong.
        source = module.load_yaml(str(SETUP.parent / "workflows/bugfix-codex.yaml"))
        transformed = rw.transform(source, SETUP)
        for before, after in zip(source["nodes"], transformed["nodes"]):
            if before["id"] != "review-loop":
                self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
