import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import review_qualification as rq

FIXTURE_BLOCKERS = (
    "malformed-claim-disclosure", "recipient-resurrection",
    "reconciliation-audit-id-reuse", "missing-iam-permission",
    "incorrect-index-types", "http-contract-mismatch",
)


class QualificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        snapshot = self.retain("repair.patch", "synthetic fixture; never pilot evidence")
        raw = self.retain("review.txt", "synthetic reviewer result")
        common = {"model": "gpt-5.6-sol", "reasoning_effort": "medium",
                  "status": "completed", "independent": True,
                  "raw_review": raw, "snapshot_sha256": snapshot["sha256"],
                  "responsibilities": ["correctness", "security"]}
        self.packet = {"schema": "archon.review-qualification.v1", "matched_repairs": [{
            "id": blocker, "snapshot": snapshot,
            "required_responsibilities": ["correctness", "security"],
            "known_blockers": [blocker],
            "historical": {**common},
            "risk_delta": {**common, "detected_blockers": [blocker]},
        } for blocker in sorted(FIXTURE_BLOCKERS)]}
        for index, pair in enumerate(self.packet["matched_repairs"]):
            for side, amounts in [("historical", (100, 80, 10, 10)), ("risk_delta", (40, 20, 10, 10))]:
                usage = {**self.usage(*amounts), "sample_id": f"fixture-{index}-{side}"}
                pair[side]["usage"] = self.retain(f"{index}-{side}.json", json.dumps(usage))

    def retain(self, name, text):
        (self.root / name).write_text(text)
        return {"path": name, "sha256": hashlib.sha256(text.encode()).hexdigest()}

    def usage(self, gross, cached, uncached, output):
        return {"accounting_unit": "provider-total-tokens-cached-input-counted-once", "gross_tokens": gross,
                "cached_input_tokens": cached, "uncached_input_tokens": uncached,
                "output_tokens": output, "active_ms": 1, "repeated_reads": 0,
                "new_validated_defects": 0, "reopened_findings": 0}

    def test_exact_sixty_percent_threshold(self):
        result = rq.qualify(self.packet, self.root)
        self.assertEqual(result["reduction_percent"], 60.0)
        self.assertEqual(result["totals"]["historical"]["gross_tokens"], 600)

    def test_rejects_failed_threshold_without_changing_unit(self):
        self.packet["matched_repairs"][0]["risk_delta"]["usage"] = self.retain(
            "new.json", json.dumps({**self.usage(41, 21, 10, 10), "sample_id": "above-threshold"}))
        with self.assertRaisesRegex(rq.QualificationError, "below 60"):
            rq.qualify(self.packet, self.root)

    def test_each_missing_blocker_prevents_qualification(self):
        for blocker in FIXTURE_BLOCKERS:
            with self.subTest(blocker=blocker):
                packet = copy.deepcopy(self.packet)
                pair = next(pair for pair in packet["matched_repairs"] if pair["id"] == blocker)
                pair["risk_delta"]["detected_blockers"].remove(blocker)
                with self.assertRaisesRegex(rq.QualificationError, "missed replay blockers"):
                    rq.qualify(packet, self.root)

    def test_unmatched_stale_wrong_model_interrupted_missing_coverage(self):
        cases = [("snapshot_sha256", "0" * 64), ("model", "gpt-5.5"),
                 ("status", "interrupted"), ("responsibilities", ["correctness"]),
                 ("independent", False)]
        for key, value in cases:
            with self.subTest(key=key):
                packet = copy.deepcopy(self.packet)
                packet["matched_repairs"][0]["risk_delta"][key] = value
                with self.assertRaises(rq.QualificationError):
                    rq.qualify(packet, self.root)
        (self.root / "review.txt").write_text("changed")
        with self.assertRaisesRegex(rq.QualificationError, "stale evidence"):
            rq.qualify(self.packet, self.root)

    def test_accounting_cannot_omit_cached_input_or_double_count_it(self):
        for data in [self.usage(20, 80, 10, 10), self.usage(180, 80, 10, 10)]:
            with self.assertRaisesRegex(rq.QualificationError, "disagree"):
                rq.metrics(data)

    def test_empty_missing_or_duplicate_repairs_block(self):
        for pairs in [[], self.packet["matched_repairs"] * 2]:
            with self.assertRaises(rq.QualificationError):
                rq.qualify({"schema": "archon.review-qualification.v1", "matched_repairs": pairs}, self.root)

    def test_aggregate_fixture_cannot_claim_all_six_historical_defects(self):
        aggregate = copy.deepcopy(self.packet["matched_repairs"][0])
        aggregate["known_blockers"] = sorted(FIXTURE_BLOCKERS)
        aggregate["risk_delta"]["detected_blockers"] = sorted(FIXTURE_BLOCKERS)
        with self.assertRaisesRegex(rq.QualificationError, "exactly six"):
            rq.qualify({"schema": "archon.review-qualification.v1", "matched_repairs": [aggregate]}, self.root)
        self.packet["matched_repairs"][0] = aggregate
        with self.assertRaisesRegex(rq.QualificationError, "exact named blocker"):
            rq.qualify(self.packet, self.root)

    def test_reused_usage_receipt_cannot_multiply_historical_cost(self):
        self.packet["matched_repairs"][1]["historical"]["usage"] = self.packet["matched_repairs"][0]["historical"]["usage"]
        with self.assertRaisesRegex(rq.QualificationError, "unique recorded sample"):
            rq.qualify(self.packet, self.root)


if __name__ == "__main__":
    unittest.main()
