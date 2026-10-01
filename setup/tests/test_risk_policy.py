#!/usr/bin/env python3
"""setup/risk_policy.py: the pure helpers behind the risk-tiered lanes.

Each rule the scorer relies on gets one named test here so a regression in a
single guard shows up as one named failure."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SETUP))
import risk_policy as rp  # noqa: E402

POLICY = json.loads((SETUP / "risk-policy.json").read_text(encoding="utf-8"))


class TierMath(unittest.TestCase):
    def test_tier_max_ignores_none_and_orders_tiers(self):
        self.assertEqual(rp.tier_max("green", None, "yellow"), "yellow")
        self.assertEqual(rp.tier_max("red", "green"), "red")
        self.assertIsNone(rp.tier_max(None, None))
        self.assertIsNone(rp.tier_max())

    def test_tier_max_rejects_unknown_tier(self):
        with self.assertRaises(rp.RiskPolicyError):
            rp.tier_max("green", "purple")

    def test_tier_from_score_uses_thresholds(self):
        th = {"yellow": 30, "red": 60}
        self.assertEqual(rp.tier_from_score(0, th), "green")
        self.assertEqual(rp.tier_from_score(29, th), "green")
        self.assertEqual(rp.tier_from_score(30, th), "yellow")
        self.assertEqual(rp.tier_from_score(60, th), "red")

    def test_tier_gt(self):
        self.assertTrue(rp.tier_gt("red", "yellow"))
        self.assertFalse(rp.tier_gt("yellow", "yellow"))
        self.assertFalse(rp.tier_gt("green", "red"))


class ShippedPolicy(unittest.TestCase):
    def test_shipped_policy_loads_and_passes_invariants(self):
        merged = rp.load_policy()
        self.assertEqual(merged["schema"], rp.SCHEMA_POLICY)
        self.assertEqual(merged["thresholds"], {"yellow": 30, "red": 60})
        self.assertEqual(merged["sensitiveDomains"]["floor"], "red")
        self.assertEqual(merged["factoryControl"]["floor"], "red")
        self.assertIn("auth", merged["sensitiveDomains"]["tokens"])
        self.assertIn("workflows/", merged["factoryControl"]["paths"])
        self.assertEqual(merged["repro_command_allow"],
                         json.loads((SETUP / "lite-envelope.json").read_text())["repro_command_allow"])

    def test_shipped_policy_file_carries_no_machine_paths(self):
        text = (SETUP / "risk-policy.json").read_text(encoding="utf-8")
        self.assertNotIn("/Use" + "rs/", text)

    def test_module_is_stdlib_only(self):
        import re
        text = (SETUP / "risk_policy.py").read_text(encoding="utf-8")
        imports = set(re.findall(r"^(?:import|from) (\w+)", text, re.M))
        self.assertTrue(imports <= {"__future__", "hashlib", "json", "os", "re", "typing"}, imports)


class Merge(unittest.TestCase):
    def test_profile_adds_protected_areas_and_owner_floors(self):
        profile = {"risk": {
            "protectedAreas": [{"paths": ["libs/rds/migrations/"], "floor": "red", "reason": "schema"}],
            "codeowners": {"ownerFloors": {"@org/platform": "red"}, "defaultOwnedFloor": "yellow"},
            "thresholds": {"yellow": 25, "red": 55},
        }}
        merged = rp.load_policy(profile=profile)
        self.assertEqual(merged["protectedAreas"], profile["risk"]["protectedAreas"])
        self.assertEqual(merged["codeowners"]["ownerFloors"], {"@org/platform": "red"})
        self.assertEqual(merged["thresholds"], {"yellow": 25, "red": 55})

    def test_overlay_wins_over_profile_and_both_extend_defaults(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        overlay = tmp / "overlay.json"
        overlay.write_text(json.dumps({
            "schema": "archon.risk-policy-overlay.v1", "overlayVersion": 3,
            "thresholds": {"yellow": 20},
            "protectedAreas": [{"paths": ["apps/bridge/"], "floor": "yellow", "reason": "flaky"}],
            "sensitiveDomains": {"extraPaths": ["apps/api/src/consent/"]},
        }), encoding="utf-8")
        profile = {"risk": {"thresholds": {"yellow": 25, "red": 55},
                            "protectedAreas": [{"paths": ["libs/rds/"], "floor": "red", "reason": "schema"}]}}
        merged = rp.load_policy(profile=profile, overlay_path=str(overlay))
        self.assertEqual(merged["thresholds"], {"yellow": 20, "red": 55})
        self.assertEqual([a["reason"] for a in merged["protectedAreas"]], ["schema", "flaky"])
        self.assertIn("apps/api/src/consent/", merged["sensitiveDomains"]["extraPaths"])
        self.assertEqual(merged["overlayVersion"], 3)

    def test_without_overlay_overlay_version_is_null(self):
        self.assertIsNone(rp.load_policy()["overlayVersion"])

    def test_bad_profile_risk_block_is_rejected(self):
        with self.assertRaises(rp.RiskPolicyError):
            rp.load_policy(profile={"risk": []})
        with self.assertRaises(rp.RiskPolicyError):
            rp.load_policy(profile={"risk": {"protectedAreas": [{"paths": "libs/", "floor": "red", "reason": "x"}]}})
        with self.assertRaises(rp.RiskPolicyError):
            rp.load_policy(profile={"risk": {"protectedAreas": [{"paths": ["libs/"], "floor": "orange", "reason": "x"}]}})
        with self.assertRaises(rp.RiskPolicyError):
            rp.load_policy(profile={"risk": {"thresholds": {"yellow": 70, "red": 60}}})

    def test_missing_or_unparsable_policy_file_is_an_error(self):
        with self.assertRaises(rp.RiskPolicyError):
            rp.load_policy(policy_path="/nonexistent/risk-policy.json")
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "p.json").write_text("{oops", encoding="utf-8")
        with self.assertRaises(rp.RiskPolicyError):
            rp.load_policy(policy_path=str(tmp / "p.json"))


class Invariants(unittest.TestCase):
    def test_cannot_lower_sensitive_domain_floor(self):
        with self.assertRaises(rp.RiskPolicyError) as cm:
            rp.load_policy(profile={"risk": {"sensitiveDomains": {"floor": "yellow"}}})
        self.assertIn("sensitiveDomains", str(cm.exception))

    def test_layers_can_only_add_tokens_and_factory_paths(self):
        # merge is additive: a profile cannot replace the auth token list, only extend it
        merged = rp.load_policy(profile={"risk": {"sensitiveDomains": {"tokens": {"auth": ["authz"]}},
                                                  "factoryControl": {"paths": ["ops/"]}}})
        self.assertIn("auth", merged["sensitiveDomains"]["tokens"]["auth"])
        self.assertIn("authz", merged["sensitiveDomains"]["tokens"]["auth"])
        self.assertIn("workflows/", merged["factoryControl"]["paths"])
        self.assertIn("ops/", merged["factoryControl"]["paths"])

    def test_invariants_reject_dropped_tokens_or_factory_paths(self):
        # assert_invariants also guards documents the merge never produced
        # (a hand-edited overlay, a Slice 5 proposal)
        defaults = rp.load_defaults()
        broken = json.loads(json.dumps(rp.load_policy()))
        broken["sensitiveDomains"]["tokens"]["auth"] = []
        with self.assertRaises(rp.RiskPolicyError) as cm:
            rp.assert_invariants(broken, defaults)
        self.assertIn("tokens.auth", str(cm.exception))
        broken = json.loads(json.dumps(rp.load_policy()))
        broken["factoryControl"]["paths"] = ["workflows/"]
        with self.assertRaises(rp.RiskPolicyError) as cm:
            rp.assert_invariants(broken, defaults)
        self.assertIn("factoryControl.paths", str(cm.exception))

    def test_cannot_lower_factory_control_floor(self):
        with self.assertRaises(rp.RiskPolicyError):
            rp.load_policy(profile={"risk": {"factoryControl": {"floor": "green"}}})

    def test_cannot_enable_auto_merge_from_policy(self):
        with self.assertRaises(rp.RiskPolicyError) as cm:
            rp.load_policy(profile={"risk": {"autoMerge": True}})
        self.assertIn("auto-merge", str(cm.exception))

    def test_assert_invariants_is_callable_on_a_merged_document(self):
        merged = rp.load_policy()
        rp.assert_invariants(merged, rp.load_defaults())  # no raise
        broken = json.loads(json.dumps(merged))
        broken["factoryControl"]["floor"] = "yellow"
        with self.assertRaises(rp.RiskPolicyError):
            rp.assert_invariants(broken, rp.load_defaults())


if __name__ == "__main__":
    unittest.main()
