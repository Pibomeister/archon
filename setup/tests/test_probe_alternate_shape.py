#!/usr/bin/env python3
"""A second query shape is required only when the lookup key is a MEASUREMENT.

A stated fingerprint can be matched two ways: against the summary the product
recorded, or by recomputing the counts from child rows. They are different
measurements and they disagree the moment a row is soft-deleted or counted under
a slightly different definition, so one of them matching nothing says nothing
about whether the occurrence exists.

Two runs of ENG-3860, minutes apart against the same database, decided
everything on that choice. da65d1b3 read `metadata->'summary'` and found
user_imports id=17592 immediately, attributed the occurrence and flipped its
symptom to `fixed`. 57309e15 recomputed from user_import_actions, matched zero
rows, and shipped class-hardening for an import that was sitting right there.
The prompt already preferred the recorded-summary shape; the RCA had the rule
and did not follow it, so the rule is now mechanical.

It is deliberately NOT mechanical everywhere. The requirement fires only when
evidence-plan.json carries a `fingerprint` identifier — a quantity the product
reported. When the ticket names an id, an email or a trace id there is exactly
one sensible lookup, and demanding a second query would be satisfied by an
invented one that passes a textual-difference check while measuring nothing.
The field stays optional-but-validated in that case."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ARCHON = Path(__file__).resolve().parent.parent.parent
SHAPE = ARCHON / "setup" / "probe-shape.py"

IDENT = {
    "id": "identify-import",
    "question": "which import matches the reported fingerprint?",
    "sql": "SELECT id, user_id, created_at FROM user_imports WHERE (metadata->'summary'->>'totalRows')::int = 1171 LIMIT 100",
    "sql_alternate": "WITH c AS (SELECT user_import_id, count(*) n FROM user_import_actions GROUP BY 1) SELECT ui.id, ui.user_id, ui.created_at FROM c JOIN user_imports ui ON ui.id = c.user_import_id WHERE c.n = 672 LIMIT 100",
    "occurrence_subject_columns": ["id"],
    "occurrence_time_columns": ["created_at"],
}


def walk(nodes):
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        yield n
        for key in ("loop_group", "body"):
            v = n.get(key)
            if isinstance(v, dict):
                yield from walk(v.get("nodes"))
            elif isinstance(v, list):
                yield from walk(v)


class ProbeAlternateShape(unittest.TestCase):
    def setUp(self):
        self.ad = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.ad, ignore_errors=True)

    def write(self, probes, key="fingerprint"):
        (self.ad / "probe.json").write_text(json.dumps({"probes": probes}), encoding="utf-8")
        (self.ad / "evidence-plan.json").write_text(json.dumps({"identifiers": [
            {"kind": key, "value": "contacts=1171,notes=24", "resolution": "given"}]}),
            encoding="utf-8")

    def run_shape(self):
        return subprocess.run([sys.executable, str(SHAPE), str(self.ad)],
                              capture_output=True, encoding="utf-8")

    def test_a_well_formed_identification_probe_passes(self):
        self.write([IDENT])
        r = self.run_shape()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_a_fingerprint_key_requires_an_alternate(self):
        probe = {k: v for k, v in IDENT.items() if k != "sql_alternate"}
        self.write([probe], key="fingerprint")
        r = self.run_shape()
        self.assertEqual(r.returncode, 1)
        self.assertIn("requires sql_alternate", r.stdout + r.stderr)
        self.assertIn("reported quantity", r.stdout + r.stderr)

    def test_a_stable_id_key_needs_no_alternate(self):
        # The generalization that matters: most projects' tickets name an id,
        # an email or a trace id. There is one sensible lookup for those, and a
        # forced second query would be invented rather than measured.
        probe = {k: v for k, v in IDENT.items() if k != "sql_alternate"}
        for key in ("user_id", "email", "other"):
            with self.subTest(key=key):
                self.write([probe], key=key)
                r = self.run_shape()
                self.assertEqual(r.returncode, 0, key + ": " + r.stdout + r.stderr)

    def test_a_missing_evidence_plan_never_blocks(self):
        # This is a refinement, not a safety gate. A lane or project without an
        # evidence plan must not be stopped by it.
        probe = {k: v for k, v in IDENT.items() if k != "sql_alternate"}
        (self.ad / "probe.json").write_text(json.dumps({"probes": [probe]}), encoding="utf-8")
        r = self.run_shape()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_an_alternate_is_validated_even_when_not_required(self):
        # Optional does not mean unchecked: a supplied alternate still runs
        # against production and is held to the same read-only rules.
        self.write([dict(IDENT, sql_alternate="DELETE FROM t")], key="user_id")
        r = self.run_shape()
        self.assertEqual(r.returncode, 1)
        self.assertIn("must start with SELECT/WITH", r.stdout + r.stderr)

    def test_an_alternate_identical_to_the_primary_is_refused(self):
        # Satisfying the field by copying the query buys nothing: the point is
        # a different MEASUREMENT, not a second execution of the same one.
        probe = dict(IDENT, sql_alternate="  " + IDENT["sql"].upper() + " ")
        self.write([probe])
        r = self.run_shape()
        self.assertEqual(r.returncode, 1)
        self.assertIn("same query as sql", r.stdout + r.stderr)

    def test_the_alternate_is_held_to_the_same_safety_rules(self):
        for bad, expect in (
            ("DELETE FROM user_imports", "must start with SELECT/WITH"),
            ("UPDATE x SET y = 1", "must start with SELECT/WITH"),
            ("SELECT 1; SELECT 2", "write/DDL or multiple statements"),
            ("SELECT id FROM user_imports LIMIT 5000", "LIMIT exceeds 100"),
            ("SELECT id FROM t WHERE x = 1 UNION SELECT 1; DROP TABLE t", "write/DDL or multiple statements"),
        ):
            with self.subTest(bad=bad):
                self.write([dict(IDENT, sql_alternate=bad)])
                r = self.run_shape()
                self.assertEqual(r.returncode, 1, bad)
                self.assertIn(expect, r.stdout + r.stderr)

    def test_a_census_probe_needs_no_alternate(self):
        # The requirement is scoped to identification probes. A blast-radius
        # census has no subject to miss, and demanding a second shape there
        # would be ceremony.
        self.write([{"id": "census", "question": "how many?",
                     "sql": "SELECT count(*) FROM notes WHERE about_id IS NULL"}])
        r = self.run_shape()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_probe_run_spends_the_alternate_only_on_zero_rows(self):
        doc = yaml.safe_load((ARCHON / "workflows" / "bugfix.yaml").read_text(encoding="utf-8"))
        bash = [n for n in walk(doc["nodes"]) if n.get("id") == "probe-run"][0]["bash"]
        self.assertIn("sql_alternate", bash, "probe-run never reads the alternate")
        self.assertIn("PROBE_IDENT=ZERO_ROWS", bash)
        self.assertIn("PROBE_IDENT=MATCHED_BY_ALTERNATE", bash)
        self.assertIn("PROBE_IDENT=UNMATCHED", bash)
        # Guarded on the primary returning no rows, so a probe that matched
        # never pays for a second query.
        self.assertIn("(0 rows)", bash.replace("\\(", "(").replace("\\)", ")"))


if __name__ == "__main__":
    unittest.main()
