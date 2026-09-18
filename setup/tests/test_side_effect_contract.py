#!/usr/bin/env python3
"""Every workflow node that mutates remotes, GitHub, docker compose, or the
shared e2e stack must be listed in setup/side_effect_contract.py with a
settlement note. Generated Codex and Grok twins inherit the parent list."""
import unittest
from pathlib import Path
import sys

SETUP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SETUP))
import side_effect_contract as sec  # noqa: E402


class SideEffectContract(unittest.TestCase):
    def test_every_detected_node_is_registered(self):
        missing = sec.unregistered(SETUP.parent / "workflows")
        self.assertEqual(missing, [],
                         "side-effectful nodes missing from the contract: " + repr(missing))

    def test_every_registry_entry_exists(self):
        stale = sec.stale_entries(SETUP.parent / "workflows")
        self.assertEqual(stale, [],
                         "contract lists nodes that are gone: " + repr(stale))

    def test_ship_nodes_are_disabled(self):
        disabled = [e for e in sec.ENTRIES if e.kind == "publish"]
        self.assertTrue(disabled)
        for e in disabled:
            self.assertEqual(e.settlement, "disabled")

    def test_compose_nodes_declare_mutex_or_disabled(self):
        for e in sec.ENTRIES:
            if e.kind == "compose":
                self.assertIn(e.settlement, {"e2e-mutex", "disabled"})


if __name__ == "__main__":
    unittest.main()
