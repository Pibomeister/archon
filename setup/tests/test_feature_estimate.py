#!/usr/bin/env python3
import json
import sys
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SETUP))

import control_contract  # noqa: E402
import feature_estimate as fe  # noqa: E402


CHAIN = "a" * 32


def observation(chain_id, repositories, phases, provider="codex"):
    return {
        "chain_id": chain_id,
        "provider": provider,
        "repositories": repositories,
        "phases": phases,
    }


def phase(tokens, complete):
    return {"tokens": tokens, "complete": complete}


class FeatureEstimateTest(unittest.TestCase):
    def test_one_small_success_does_not_replace_conservative_prior(self):
        rows = [observation("small", ["api"], {"planning": phase(1000, True)})]
        report = fe.estimate(["api"], 30_000_000, rows)
        self.assertEqual(40_000_000, report["recommended_total_tokens"])
        self.assertEqual("low", report["confidence"])

    def test_sparse_history_uses_cold_start_priors_and_retry_reserve(self):
        report = fe.estimate(["api", "goodword-mcp"], 50_000_000, [])

        self.assertEqual(report["remaining_range"], {"low": 24_000_000, "high": 60_000_000})
        self.assertEqual(report["base_remaining_range"], {"low": 24_000_000, "high": 48_000_000})
        self.assertEqual(report["retry_reserve_tokens"], 12_000_000)
        self.assertEqual(report["recommended_total_tokens"], 60_000_000)
        self.assertEqual(report["disposition"], "at_risk")
        self.assertEqual(report["confidence"], "low")

    def test_completed_samples_and_duplicate_chain_ids_are_deduped(self):
        rows = [
            observation("one", ["api"], {"planning": phase(8_000_000, True), "implement:api": phase(9_000_000, True), "integration": phase(2_000_000, True)}),
            observation("two", ["api"], {"planning": phase(10_000_000, True), "implement:api": phase(11_000_000, True), "integration": phase(3_000_000, True)}),
            observation("two", ["api"], {"planning": phase(99_000_000, True)}),
        ]

        report = fe.estimate(["api"], 40_000_000, rows)

        self.assertEqual(report["base_remaining_range"], {"low": 19_000_000, "high": 32_500_000})
        self.assertEqual(report["remaining_range"], {"low": 19_000_000, "high": 40_625_000})
        self.assertIn("observation two ignored as duplicate", report["diagnostics"])

    def test_incomplete_planning_is_lower_bound_not_cheap_completion(self):
        rows = [
            observation("burned", ["api", "goodword-mcp"], {"planning": phase(30_000_000, False)}),
        ]

        report = fe.estimate(["api", "goodword-mcp"], 30_000_000, rows)

        planning = next(item for item in report["phases"] if item["phase"] == "planning")
        self.assertEqual(planning["total_range"], {"low": 30_000_000, "high": 37_500_000})
        self.assertEqual(planning["range"], {"low": 30_000_000, "high": 37_500_000})
        self.assertEqual(report["disposition"], "at_risk")
        self.assertGreater(report["recommended_total_tokens"], 30_000_000)

    def test_current_phase_usage_reduces_remaining_without_counting_as_history(self):
        report = fe.estimate(
            ["api"],
            30_000_000,
            [],
            used_tokens=1_000_000,
            phase_usage={"planning": 1_000_000},
        )

        planning = next(item for item in report["phases"] if item["phase"] == "planning")
        self.assertEqual(planning["phase_used_tokens"], 1_000_000)
        self.assertEqual(planning["range"], {"low": 5_000_000, "high": 11_000_000})
        self.assertEqual(report["recommended_total_tokens"], 39_750_000)

    def test_phase_usage_overrun_keeps_retry_reserve(self):
        report = fe.estimate(
            ["api"],
            30_000_000,
            [],
            used_tokens=13_000_000,
            phase_usage={"planning": 13_000_000},
        )

        planning = next(item for item in report["phases"] if item["phase"] == "planning")
        self.assertTrue(planning["overrun"])
        self.assertEqual(planning["range"], {"low": 0, "high": 3_000_000})
        self.assertEqual(planning["overrun_reserve_tokens"], 3_000_000)

    def test_completed_phases_are_excluded_and_have_no_contingency_when_done(self):
        report = fe.estimate(
            ["api"],
            30_000_000,
            [],
            used_tokens=22_000_000,
            completed_repositories=["api"],
            planning_complete=True,
            integration_complete=True,
        )

        self.assertEqual(report["phases"], [])
        self.assertEqual(report["remaining_range"], {"low": 0, "high": 0})
        self.assertEqual(report["retry_reserve_tokens"], 0)
        self.assertEqual(report["recommended_total_tokens"], 22_000_000)
        self.assertEqual(report["disposition"], "sufficient")

    def test_rejects_bad_inputs(self):
        with self.assertRaisesRegex(fe.EstimateError, "duplicate repository"):
            fe.estimate(["api", "api"], 1, [])
        with self.assertRaisesRegex(fe.EstimateError, "unsupported repository"):
            fe.estimate(["../api"], 1, [])
        with self.assertRaisesRegex(fe.EstimateError, "used_tokens"):
            fe.estimate(["api"], 1, [], used_tokens=-1)
        with self.assertRaisesRegex(fe.EstimateError, "completed repositories"):
            fe.estimate(["api"], 1, [], completed_repositories=["web-app"])


class FeatureEstimateLoaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.control = Path(self.tmp.name) / "control"
        (self.control / "feature-chains-v2").mkdir(parents=True, mode=0o700)
        (self.control / "feature-budgets").mkdir(mode=0o700)

    def write_chain(self, chain_id=CHAIN):
        state = {
            "logical_chain_id": chain_id,
            "chain_secret": "x" * 48,
            "provider": "codex",
            "repositories": ["api", "goodword-mcp"],
            "approval": {"approved": True},
            "stages": {
                "api": {"status": "verified"},
                "goodword-mcp": {"status": "pending"},
            },
            "integration": None,
        }
        state = control_contract.seal_chain_state(state)
        control_contract.secure_write_json(self.control / "feature-chains-v2" / f"{chain_id}.json", state)

    def write_budget(self, chain_id=CHAIN):
        ledger = {
            "logical_chain_id": chain_id,
            "runs": [
                {"stage": "planning:all", "session_files": ["sessions/planning.jsonl"]},
                {"stage": "implement:api", "session_files": ["sessions/api.jsonl"]},
                {"stage": "implement:goodword-mcp", "session_files": ["sessions/mcp.jsonl"]},
            ],
            "session_token_high_water": {
                "sessions/planning.jsonl": {"total_tokens": 7_000_000},
                "sessions/api.jsonl": {"total_tokens": 8_000_000},
                "sessions/mcp.jsonl": {"total_tokens": 2_000_000},
            },
        }
        control_contract.secure_write_json(self.control / "feature-budgets" / f"{chain_id}.json", ledger)

    def test_load_observations_reads_signed_state_and_budget_metadata(self):
        self.write_chain()
        self.write_budget()

        observations, diagnostics = fe.load_observations(self.control)

        self.assertEqual(diagnostics, [])
        self.assertEqual(len(observations), 1)
        phases = observations[0]["phases"]
        self.assertEqual(phases["planning"], {"tokens": 7_000_000, "complete": True})
        self.assertEqual(phases["implement:api"], {"tokens": 8_000_000, "complete": True})
        self.assertEqual(phases["implement:goodword-mcp"], {"tokens": 2_000_000, "complete": False})

    def test_loader_skips_ambiguous_cross_phase_session_reuse(self):
        self.write_chain()
        ledger = {
            "logical_chain_id": CHAIN,
            "runs": [
                {"stage": "planning:all", "session_files": ["sessions/shared.jsonl"]},
                {"stage": "implement:api", "session_files": ["sessions/shared.jsonl"]},
            ],
            "session_token_high_water": {"sessions/shared.jsonl": {"total_tokens": 9_000_000}},
        }
        control_contract.secure_write_json(self.control / "feature-budgets" / f"{CHAIN}.json", ledger)

        observations, diagnostics = fe.load_observations(self.control)

        self.assertEqual(observations, [])
        self.assertIn("shared by phases planning and implement:api", diagnostics[0])

    def test_loader_counts_same_phase_reused_session_once(self):
        self.write_chain()
        ledger = {
            "logical_chain_id": CHAIN,
            "runs": [
                {"stage": "planning:all", "session_files": ["sessions/shared.jsonl"]},
                {"stage": "planning:all", "session_files": ["sessions/shared.jsonl"]},
            ],
            "session_token_high_water": {"sessions/shared.jsonl": {"total_tokens": 9_000_000}},
        }
        control_contract.secure_write_json(self.control / "feature-budgets" / f"{CHAIN}.json", ledger)

        observations, diagnostics = fe.load_observations(self.control)

        self.assertEqual(diagnostics, [])
        self.assertEqual(observations[0]["phases"]["planning"], {"tokens": 9_000_000, "complete": True})

    def test_loader_skips_missing_session_high_water(self):
        self.write_chain()
        ledger = {
            "logical_chain_id": CHAIN,
            "runs": [{"stage": "planning:all", "session_files": ["sessions/missing.jsonl"]}],
            "session_token_high_water": {},
        }
        control_contract.secure_write_json(self.control / "feature-budgets" / f"{CHAIN}.json", ledger)

        observations, diagnostics = fe.load_observations(self.control)

        self.assertEqual(observations, [])
        self.assertIn("has no token high-water", diagnostics[0])

    def test_loader_skips_empty_session_high_water_row(self):
        self.write_chain()
        ledger = {
            "logical_chain_id": CHAIN,
            "runs": [{"stage": "planning:all", "session_files": ["sessions/empty.jsonl"]}],
            "session_token_high_water": {"sessions/empty.jsonl": {}},
        }
        control_contract.secure_write_json(self.control / "feature-budgets" / f"{CHAIN}.json", ledger)

        observations, diagnostics = fe.load_observations(self.control)

        self.assertEqual(observations, [])
        self.assertIn("has no token high-water", diagnostics[0])

    def test_loader_reports_malformed_session_files_without_traceback(self):
        self.write_chain()
        ledger = {
            "logical_chain_id": CHAIN,
            "runs": [{"stage": "planning:all", "session_files": None}],
            "session_token_high_water": {},
        }
        control_contract.secure_write_json(self.control / "feature-budgets" / f"{CHAIN}.json", ledger)

        observations, diagnostics = fe.load_observations(self.control)

        self.assertEqual(observations, [])
        self.assertIn("session_files must be a list", diagnostics[0])

    def test_completed_state_with_empty_ledger_does_not_create_zero_cost_samples(self):
        self.write_chain()
        control_contract.secure_write_json(
            self.control / "feature-budgets" / f"{CHAIN}.json",
            {"logical_chain_id": CHAIN, "runs": [], "session_token_high_water": {}},
        )

        observations, diagnostics = fe.load_observations(self.control)
        report = fe.estimate(["api", "goodword-mcp"], 30_000_000, observations)

        self.assertIn("no attributable phase token usage", diagnostics[0])
        self.assertEqual(observations[0]["phases"], {})
        planning = next(item for item in report["phases"] if item["phase"] == "planning")
        self.assertEqual(planning["range"], {"low": 6_000_000, "high": 12_000_000})
        self.assertEqual(planning["evidence"]["completed_samples"], 0)


if __name__ == "__main__":
    unittest.main()
