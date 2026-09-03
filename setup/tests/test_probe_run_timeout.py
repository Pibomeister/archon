#!/usr/bin/env python3
import unittest
from pathlib import Path
import yaml

ARCHON = Path(__file__).resolve().parents[2]


class ProbeRunTimeoutContract(unittest.TestCase):
    def test_full_bugfix_probes_degrade_before_node_timeout(self):
        for name in ("bugfix", "bugfix-codex"):
            with self.subTest(workflow=name):
                doc = yaml.safe_load((ARCHON / "workflows" / f"{name}.yaml").read_text(encoding="utf-8"))
                node = next(n for n in doc["nodes"] if n["id"] == "probe-run")
                self.assertEqual(node["timeout"], 600000)
                self.assertIn("PGCONNECT_TIMEOUT=15", node["bash"])
                self.assertIn("statement_timeout=120000", node["bash"])
                self.assertIn("psql -X -v ON_ERROR_STOP=1", node["bash"])
                self.assertIn("PROBE_TMP=", node["bash"])
                self.assertIn("262144", node["bash"])
                self.assertIn("PROBE_RESULT=DEGRADED reason=output-truncated", node["bash"])
                self.assertIn('if [ "$OK" = "$NP" ]; then', node["bash"])
                self.assertIn('record_probe complete || { echo "PROBE_RUN=FAIL provenance"; exit 1; }', node["bash"])
                self.assertIn('record_probe degraded || { echo "PROBE_RUN=FAIL provenance"; exit 1; }', node["bash"])
                self.assertGreaterEqual(node["bash"].count("record_probe unavailable"), 3)
                self.assertIn("--evidence-kind occurrence", node["bash"])
                seal = next(n for n in doc["nodes"] if n["id"] == "evidence-seal")
                self.assertEqual(seal["depends_on"], ["probe-run"])
                self.assertIn("--require-source prod-probes", seal["bash"])


if __name__ == "__main__":
    unittest.main()
