#!/usr/bin/env python3
import importlib.util
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parent.parent
SCRIPT = SETUP / "bugfix-contract.py"


def load_module():
    spec = importlib.util.spec_from_file_location("bugfix_contract", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_row(i, claim):
    return {
        "id": f"S{i}",
        "claim": claim,
        "source_document": "report.md",
        "source_field": "description",
        "byte_span": {"start": (i - 1) * 32, "end": (i - 1) * 32 + len(claim)},
        "source_quote": claim,
        "verbatim_evidence": claim,
        "source_order": i,
        "expected_behavior": f"expected {claim}",
        "actual_behavior": claim,
    }


def effective_row(i, refs, claim, relation="same-as-source"):
    return {
        "id": f"E{i}",
        "source_ids": refs,
        "relation": relation,
        "claim": claim,
        "expected_behavior": f"expected {claim}",
        "actual_behavior": claim,
    }


def ledger(effective=None):
    source = [source_row(1, "Granola icon is shown"), source_row(2, "Sahiba is missing")]
    value = {
        "schema_version": 2,
        "ledger_root_hash": hashlib.sha256(json.dumps(source, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "ledger_revision_hash": None,
        "revision": 1,
        "previous_revision_hash": None,
        "source_symptoms": source,
        "effective_symptoms": effective or [
            effective_row(1, ["S1"], source[0]["claim"]),
            effective_row(2, ["S2"], source[1]["claim"]),
        ],
    }
    value["ledger_revision_hash"] = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return value


class BugfixContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bc = load_module()

    def test_canonical_hash_ignores_key_order(self):
        self.assertEqual(self.bc.canonical_hash({"a": 1, "b": 2}), self.bc.canonical_hash({"b": 2, "a": 1}))

    def test_ledger_requires_nonempty_source_backed_symptoms(self):
        with self.assertRaisesRegex(self.bc.ContractError, "no-reportable-symptoms"):
            self.bc.validate_ledger({**ledger(), "source_symptoms": [], "effective_symptoms": []})

    def test_ledger_rejects_effective_unknown_source(self):
        bad = ledger([effective_row(1, ["S9"], "x")])
        with self.assertRaisesRegex(self.bc.ContractError, "unknown source symptom"):
            self.bc.validate_ledger(bad)

    def test_split_effective_rows_preserve_source_accounting(self):
        value = ledger([
            effective_row(1, ["S1"], "icon semantics", "split"),
            effective_row(2, ["S1"], "icon association", "split"),
            effective_row(3, ["S2"], "Sahiba missing"),
        ])
        result = self.bc.validate_ledger(value)
        self.assertEqual(result["source_to_effective"]["S1"], ["E1", "E2"])

    def test_full_fix_requires_every_effective_fixed(self):
        result = self.bc.classify(
            ledger(),
            {"E1": {"disposition": "fixed"}, "E2": {"disposition": "fixed"}},
            incidental_mechanism=False,
        )
        self.assertEqual(result["implementation_result"], "FULL_FIX")
        self.assertEqual(result["ticket_disposition"], "RESOLVED")
        self.assertTrue(result["ticket_closure_allowed"])

    def test_partial_and_product_decision_are_orthogonal(self):
        result = self.bc.classify(
            ledger(),
            {"E1": {"disposition": "product-semantics"}, "E2": {"disposition": "fixed"}},
            incidental_mechanism=False,
        )
        self.assertEqual(result["implementation_result"], "PARTIAL_FIX")
        self.assertEqual(result["ticket_disposition"], "PRODUCT_DECISION_NEEDED")
        self.assertEqual(result["approval_scope"], "ship-covered-symptoms")
        self.assertFalse(result["ticket_closure_allowed"])

    def test_class_hardening_never_closes_occurrence(self):
        result = self.bc.classify(
            ledger(),
            {"E1": {"disposition": "class-hardening-only"}, "E2": {"disposition": "unresolved"}},
            incidental_mechanism=True,
        )
        self.assertEqual(result["implementation_result"], "CLASS_HARDENING")
        self.assertEqual(result["ticket_disposition"], "OPEN")
        self.assertEqual(result["approval_scope"], "ship-hardening-only")
        self.assertFalse(result["ticket_closure_allowed"])

    def test_split_source_rollup_can_be_partial_and_open(self):
        value = ledger([
            effective_row(1, ["S1"], "a", "split"),
            effective_row(2, ["S1"], "b", "split"),
            effective_row(3, ["S2"], "c"),
        ])
        result = self.bc.classify(value, {
            "E1": {"disposition": "fixed"},
            "E2": {"disposition": "unresolved"},
            "E3": {"disposition": "by-design", "authority": "product-receipt"},
        }, incidental_mechanism=False)
        self.assertEqual(result["implementation_result"], "PARTIAL_FIX")
        self.assertEqual(result["ticket_disposition"], "OPEN")
        self.assertEqual(result["source_rollups"]["S1"]["any_fixed"], True)
        self.assertEqual(result["source_rollups"]["S1"]["any_open"], True)

    def test_by_design_requires_product_authority(self):
        with self.assertRaisesRegex(self.bc.ContractError, "by-design requires product authority"):
            self.bc.classify(ledger(), {
                "E1": {"disposition": "by-design"},
                "E2": {"disposition": "fixed"},
            }, incidental_mechanism=False)

    def test_ledger_hashes_are_recomputed_not_shape_checked(self):
        source_tamper = ledger()
        source_tamper["source_symptoms"][0]["claim"] = "rewritten symptom"
        with self.assertRaisesRegex(self.bc.ContractError, "ledger_root_hash mismatch"):
            self.bc.validate_ledger(source_tamper, require_sealed=True)
        revision_tamper = ledger()
        revision_tamper["effective_symptoms"][0]["claim"] = "rewritten effective symptom"
        with self.assertRaisesRegex(self.bc.ContractError, "ledger_revision_hash mismatch"):
            self.bc.validate_ledger(revision_tamper, require_sealed=True)

    def test_downstream_contract_rejects_unsealed_ledger(self):
        value = ledger()
        value["ledger_revision_hash"] = None
        with self.assertRaisesRegex(self.bc.ContractError, "required after intake sealing"):
            self.bc.classify(value, {
                "E1": {"disposition": "fixed"}, "E2": {"disposition": "fixed"}
            }, incidental_mechanism=False)

    def test_safe_artifact_path_rejects_traversal_and_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "ok.json").write_text("{}")
            self.assertEqual(self.bc.safe_artifact_path(root, "ok.json"), (root / "ok.json").resolve())
            with self.assertRaises(self.bc.ContractError):
                self.bc.safe_artifact_path(root, "../escape")
            (root / "link").symlink_to(root / "ok.json")
            with self.assertRaises(self.bc.ContractError):
                self.bc.safe_artifact_path(root, "link")

    def test_smoke_readiness_requires_resolved_ticket_without_explicit_acceptance(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "fix-classification.json").write_text(json.dumps({
                "implementation_result": "PARTIAL_FIX",
                "ticket_disposition": "OPEN",
                "approval_scope": "ship-covered-symptoms",
                "ticket_closure_allowed": False,
                "open_effective_ids": ["E2"],
            }))
            with self.assertRaisesRegex(self.bc.ContractError, "ticket disposition is not RESOLVED"):
                self.bc.validate_smoke_readiness(root)
            (root / "accept-residuals.txt").write_text("human accepted partial shipment")
            self.bc.validate_smoke_readiness(root, allow_open_ticket=True)

    def test_smoke_readiness_blocks_auto_product_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "fix-classification.json").write_text(json.dumps({
                "implementation_result": "FULL_FIX",
                "ticket_disposition": "RESOLVED",
                "approval_scope": "ship-covered-symptoms",
                "ticket_closure_allowed": True,
                "open_effective_ids": [],
            }))
            (root / "smoke-matrix.json").write_text(json.dumps({"rows": [{
                "id": "reported-surface-regression",
                "kind": "auto",
                "result": "fail",
                "failure_class": "product",
                "observed": "Reported surface still shows the original failure.",
            }]}))
            with self.assertRaisesRegex(self.bc.ContractError, "auto smoke product failure blocks"):
                self.bc.validate_smoke_readiness(root)

    def test_smoke_readiness_keeps_harness_drift_available_for_human_judgment(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "fix-classification.json").write_text(json.dumps({
                "implementation_result": "FULL_FIX",
                "ticket_disposition": "RESOLVED",
                "approval_scope": "ship-covered-symptoms",
                "ticket_closure_allowed": True,
                "open_effective_ids": [],
            }))
            (root / "smoke-matrix.json").write_text(json.dumps({"rows": [{
                "id": "reported-surface-selector-drifted",
                "kind": "auto",
                "result": "fail",
                "failure_class": "harness",
                "observed": "Expected selector is absent on the current surface.",
            }]}))
            result = self.bc.validate_smoke_readiness(root)
            self.assertEqual(result["auto_rows"], 1)
            self.assertEqual(result["product_failures"], [])


if __name__ == "__main__":
    unittest.main()
