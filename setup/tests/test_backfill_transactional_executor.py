#!/usr/bin/env python3
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parent.parent
SCRIPT = SETUP / "backfill-transactional-executor.py"
spec = importlib.util.spec_from_file_location("backfill_transactional_executor", SCRIPT)
assert spec and spec.loader
bte = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bte)


def proposal(**overrides):
    base = {
        "version": "backfill.proposal.v2",
        "run_id": "run-123",
        "instrument": {
            "repo": "api",
            "commit": "1" * 40,
            "tree": "2" * 40,
            "version": "instrument-v1",
            "source_files": ["tools/backfills/widgets.ts"],
        },
        "target": {
            "database_identity": {"environment": "staging", "database": "archon_test", "role": "postgres", "cluster": "local-pg"},
            "tables": ["widgets"],
            "key_columns": ["id"],
            "schema_metadata": {"widgets": {"columns": ["id", "status", "note"],
                                             "column_types": {"id": "integer", "status": "text", "note": "text"}}},
        },
        "bounds": {"max_rows": 2, "chunk_size": 2},
        "mutations": [
            {
                "operation_key": "op-1",
                "table": "widgets",
                "key": {"id": 1},
                "preconditions": {"status": "old"},
                "set": {"status": "new"},
            }
        ],
        "verification": {"postconditions": [{"operation_key": "op-1", "expect": {"status": "new"}}]},
        "undo": {"definition": {"mode": "restore-before-values"}},
    }
    base.update(overrides)
    base["undo"]["definition_digest"] = bte.digest_value(base["undo"]["definition"])
    base["proposal_digest"] = bte.digest_value(bte.proposal_payload_for_digest(base))
    return base


class BackfillProposalV2Contract(unittest.TestCase):
    def test_rejects_legacy_apply_command_and_sql_authority(self):
        for legacy in ({"apply_command": "bun run cli --apply"}, {"apply_sql": "UPDATE widgets SET status='new'"}):
            doc = proposal()
            doc["instrument"].update(legacy)
            doc["proposal_digest"] = bte.digest_value(bte.proposal_payload_for_digest(doc))
            with self.subTest(legacy=legacy), self.assertRaisesRegex(ValueError, "legacy write authority"):
                bte.validate_proposal(doc)


    def test_recomputes_full_proposal_digest_and_rejects_old_digest_mutation(self):
        doc = proposal()
        stale_digest = doc["proposal_digest"]
        doc["mutations"][0]["set"] = {"status": "tampered"}
        doc["proposal_digest"] = stale_digest

        with self.assertRaisesRegex(ValueError, "proposal_digest mismatch"):
            bte.validate_proposal(doc)

    def test_rejects_missing_instrument_and_undo_definition_binding(self):
        doc = proposal()
        del doc["instrument"]
        doc["proposal_digest"] = bte.digest_value(bte.proposal_payload_for_digest(doc))
        with self.assertRaisesRegex(ValueError, "instrument immutable binding"):
            bte.validate_proposal(doc)

        doc = proposal()
        del doc["undo"]["definition"]
        doc["proposal_digest"] = bte.digest_value(bte.proposal_payload_for_digest(doc))
        with self.assertRaisesRegex(ValueError, "undo.definition is required"):
            bte.validate_proposal(doc)

    def test_rejects_undo_definition_digest_mismatch(self):
        doc = proposal()
        doc["undo"]["definition"] = {"mode": "different"}
        doc["proposal_digest"] = bte.digest_value(bte.proposal_payload_for_digest(doc))

        with self.assertRaisesRegex(ValueError, "undo.definition_digest mismatch"):
            bte.validate_proposal(doc)

    def test_rejects_mutations_without_materialized_identity(self):
        doc = proposal()
        del doc["mutations"][0]["key"]

        with self.assertRaisesRegex(ValueError, "mutation key"):
            bte.validate_proposal(doc)


    def test_rejects_unmodeled_trigger_cascade_and_external_effects(self):
        for key in ("triggers", "cascades", "external_side_effects", "side_effects"):
            doc = proposal()
            doc["target"]["schema_metadata"]["widgets"][key] = ["unmodeled"]
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "unsupported schema effects"):
                bte.validate_proposal(doc)

    def test_valid_materialized_proposal_passes_validation(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "proposal.json"
            path.write_text(json.dumps(proposal()), encoding="utf-8")
            self.assertEqual(bte.main(["validate-plan", str(path)]), 0)

    def test_canonical_golden_fixture_uses_utf8_safe_integer_subset(self):
        path = SETUP / "tests" / "fixtures" / "backfill-proposal-v2-golden.json"
        doc = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(doc["proposal_digest"], bte.digest_value(bte.proposal_payload_for_digest(doc)))
        self.assertEqual(doc["undo"]["definition_digest"], bte.digest_value(doc["undo"]["definition"]))
        self.assertEqual(doc["mutations"][0]["set"]["status"], "Café 東京")
        bte.validate_proposal(doc)

    def test_rejects_decimal_numbers_and_unsafe_integers_in_mutations(self):
        doc = proposal()
        doc["mutations"][0]["set"] = {"status": 1.25}
        with self.assertRaisesRegex(ValueError, "canonical subset"):
            bte.validate_proposal(doc)

        doc = proposal()
        doc["mutations"][0]["key"] = {"id": 9007199254740992}
        with self.assertRaisesRegex(ValueError, "safe range"):
            bte.validate_proposal(doc)

    def test_rejects_unknown_bounds_and_missing_database_identity_parts(self):
        doc = proposal()
        doc["bounds"]["max_fraction"] = "0.1"
        doc["proposal_digest"] = bte.digest_value(bte.proposal_payload_for_digest(doc))
        with self.assertRaisesRegex(ValueError, "unsupported keys"):
            bte.validate_proposal(doc)

        doc = proposal()
        del doc["target"]["database_identity"]["role"]
        doc["proposal_digest"] = bte.digest_value(bte.proposal_payload_for_digest(doc))
        with self.assertRaisesRegex(ValueError, "database_identity.role"):
            bte.validate_proposal(doc)

    def test_rejects_unapproved_identities_columns_and_bounds_with_valid_digests(self):
        cases = [
            (lambda d: d.update(run_id="bad id"), "run_id"),
            (lambda d: d["mutations"][0].update(operation_key="bad op"), "operation_key"),
            (lambda d: d["mutations"][0].update(table="other"), "unapproved table"),
            (lambda d: d["mutations"][0].update(key={"note": "x"}), "key columns"),
            (lambda d: d["mutations"][0].update(set={"unknown": "x"}), "unapproved column"),
            (lambda d: d["mutations"][0].update(set={"id": 3}), "key columns"),
            (lambda d: d["target"].update(tables=["widgets", "widgets"]), "unique"),
            (lambda d: d["bounds"].update(max_rows=1000, chunk_size=501), "chunk_size"),
            (lambda d: d["verification"].update(nested=[{"apply_sql": "UPDATE widgets SET status='x'"}]), "legacy write authority"),
        ]
        for change, error in cases:
            doc = proposal()
            change(doc)
            doc["proposal_digest"] = bte.digest_value(bte.proposal_payload_for_digest(doc))
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                bte.validate_proposal(doc)
        doc = proposal(bounds={"max_rows": 1, "chunk_size": 1})
        doc["mutations"].append({**doc["mutations"][0], "operation_key": "op-2", "key": {"id": 2}})
        doc["proposal_digest"] = bte.digest_value(bte.proposal_payload_for_digest(doc))
        with self.assertRaisesRegex(ValueError, "row bound"):
            bte.validate_proposal(doc)

    def test_rejects_duplicate_record_identity_before_execution(self):
        doc = proposal()
        doc["mutations"].append({**doc["mutations"][0], "operation_key": "op-2"})
        doc["proposal_digest"] = bte.digest_value(bte.proposal_payload_for_digest(doc))
        with self.assertRaisesRegex(ValueError, "duplicate record identity"):
            bte.validate_proposal(doc)

    def test_canonical_subset_rejects_ambiguous_keys_and_freeform_numbers(self):
        for value in ({"\ue000": 1, "\U00010000": 2}, {"control\nkey": 1}, {"value": 1.0},
                      {"value": float("nan")}, {"value": 9007199254740992}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                bte.digest_value(value)

    def test_rejects_missing_or_lossy_involved_column_types(self):
        for sql_type in (None, "timestamp with time zone", "timestamp without time zone", "jsonb", "json", "real",
                         "double precision", "bytea", "integer[]", "bigint", "numeric"):
            with self.subTest(sql_type=sql_type):
                doc = proposal()
                types = doc["target"]["schema_metadata"]["widgets"]["column_types"]
                if sql_type is None:
                    del types["status"]
                else:
                    types["status"] = sql_type
                doc["proposal_digest"] = bte.digest_value(bte.proposal_payload_for_digest(doc))
                with self.assertRaisesRegex(ValueError, "type"):
                    bte.validate_proposal(doc)

    def test_rejects_values_incompatible_with_approved_column_types(self):
        for sql_type, value in (("integer", True), ("integer", 2147483648), ("smallint", 32768),
                                ("boolean", "true"), ("text", 1), ("uuid", "not-a-uuid")):
            with self.subTest(sql_type=sql_type, value=value):
                doc = proposal()
                doc["target"]["schema_metadata"]["widgets"]["column_types"]["id"] = sql_type
                doc["mutations"][0]["key"]["id"] = value
                doc["proposal_digest"] = bte.digest_value(bte.proposal_payload_for_digest(doc))
                with self.assertRaisesRegex(ValueError, "type|value|range"):
                    bte.validate_proposal(doc)

    def test_supported_scalar_types_and_null_nonkeys_validate(self):
        cases = (("smallint", -32768, 32767), ("integer", -2147483648, 2147483647),
                 ("boolean", True, False), ("text", "old", "Café 東京"),
                 ("character varying", "old", "new"), ("character varying(20)", None, "new"),
                 ("uuid", "00000000-0000-0000-0000-000000000000", "12345678-1234-1234-1234-123456789abc"),
                 ("text", "old", None))
        for sql_type, before, after in cases:
            with self.subTest(sql_type=sql_type, after=after):
                doc = proposal()
                doc["target"]["schema_metadata"]["widgets"]["column_types"]["status"] = sql_type
                doc["mutations"][0]["preconditions"]["status"] = before
                doc["mutations"][0]["set"]["status"] = after
                doc["proposal_digest"] = bte.digest_value(bte.proposal_payload_for_digest(doc))
                bte.validate_proposal(doc)

    def test_rejects_null_primary_key(self):
        doc = proposal()
        doc["mutations"][0]["key"]["id"] = None
        doc["proposal_digest"] = bte.digest_value(bte.proposal_payload_for_digest(doc))
        with self.assertRaisesRegex(ValueError, "null"):
            bte.validate_proposal(doc)

    def test_render_mode_refuses_a_second_write_implementation(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "proposal.json"
            path.write_text(json.dumps(proposal()), encoding="utf-8")
            out = Path(td) / "chunk.sql"
            self.assertEqual(bte.main(["render-sql", str(path), str(out)]), 1)
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
