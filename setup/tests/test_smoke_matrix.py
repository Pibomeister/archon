#!/usr/bin/env python3
import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent.parent / "smoke-matrix.py"
spec = importlib.util.spec_from_file_location("smoke_matrix", SCRIPT)
assert spec and spec.loader
sm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sm)


def matrix(title="case"):
    return {"rows": [{"id": "row", "kind": "auto", "spec_title": title}]}


def report(*, title="case", ok=False, annotations=None, message=""):
    return {"suites": [{"specs": [{
        "title": title,
        "ok": ok,
        "tests": [{
            "annotations": annotations or [],
            "results": [{"error": {"message": message}}] if message else [],
        }],
    }]}]}


class SmokeMatrixTest(unittest.TestCase):
    def test_product_evidence_dominates_simultaneous_harness_drift(self):
        annotations = [
            {"type": "failure_class", "description": "harness"},
            {"type": "failure_class", "description": "product"},
        ]
        result = sm.apply_report(matrix(), report(annotations=annotations, message="visible wrong result"))
        self.assertEqual(result["rows"][0]["failure_class"], "product")

    def test_harness_annotation_is_structured(self):
        annotations = [{"type": "failure_class", "description": "harness"}]
        result = sm.apply_report(matrix(), report(annotations=annotations, message="locator absent"))
        self.assertEqual(result["rows"][0]["failure_class"], "harness")

    def test_structured_harness_annotation_outranks_incidental_product_marker_in_stack(self):
        annotations = [{"type": "failure_class", "description": "harness"}]
        message = "SMOKE_HARNESS_DRIFT input absent\nsource: failureClass ? SMOKE_PRODUCT_FAIL : SMOKE_HARNESS_DRIFT"
        result = sm.apply_report(matrix(), report(annotations=annotations, message=message))
        self.assertEqual(result["rows"][0]["failure_class"], "harness")

    def test_legacy_markers_remain_a_compatibility_fallback(self):
        result = sm.apply_report(matrix(), report(message="SMOKE_PRODUCT_FAIL wrong result"))
        self.assertEqual(result["rows"][0]["failure_class"], "product")

    def test_unmarked_failure_is_unknown_and_preserves_evidence(self):
        result = sm.apply_report(matrix(), report(message="unexpected assertion"))
        row = result["rows"][0]
        self.assertEqual(row["failure_class"], "unknown")
        self.assertEqual(row["observed"], "unexpected assertion")

    def test_missing_title_is_infrastructure(self):
        result = sm.apply_report(matrix("expected"), report(title="other", message="failed"))
        self.assertEqual(result["rows"][0]["result"], "not-run")
        self.assertEqual(result["rows"][0]["failure_class"], "infrastructure")

    def test_malformed_report_fails_closed_with_reason(self):
        result = sm.apply_report(matrix(), None, "Playwright report unavailable or malformed: JSONDecodeError")
        row = result["rows"][0]
        self.assertEqual(row["result"], "not-run")
        self.assertEqual(row["failure_class"], "infrastructure")
        self.assertIn("malformed", row["observed"])

    def test_passing_spec_clears_failure_fields(self):
        result = sm.apply_report(matrix(), report(ok=True))
        self.assertEqual(result["rows"][0]["result"], "pass")
        self.assertIsNone(result["rows"][0]["failure_class"])
        self.assertIsNone(result["rows"][0]["observed"])


if __name__ == "__main__":
    unittest.main()
