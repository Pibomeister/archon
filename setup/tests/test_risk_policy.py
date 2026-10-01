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

try:
    import jsonschema
except ImportError:
    jsonschema = None


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

    def test_unknown_layer_key_is_rejected(self):
        with self.assertRaises(rp.RiskPolicyError):
            rp.load_policy(profile={"risk": {"sensitiveDomain": {"floor": "red"}}})

    def test_layer_floor_must_be_a_known_tier(self):
        for key in ("sensitiveDomains", "factoryControl", "publicContract", "sideEffects"):
            with self.assertRaises(rp.RiskPolicyError) as cm:
                rp.load_policy(profile={"risk": {key: {"floor": "purple"}}})
            self.assertIn(key, str(cm.exception))

    def test_unknown_nested_key_in_section_is_rejected(self):
        cases = [
            ({"sensitiveDomains": {"floor": "red", "bogus": 1}}, "sensitiveDomains"),
            ({"factoryControl": {"floor": "red", "bogus": 1}}, "factoryControl"),
            ({"publicContract": {"floor": "yellow", "bogus": 1}}, "publicContract"),
            ({"sideEffects": {"floor": "yellow", "bogus": 1}}, "sideEffects"),
            ({"codeowners": {"defaultOwnedFloor": "yellow", "bogus": 1}}, "codeowners"),
        ]
        for risk, section in cases:
            with self.assertRaises(rp.RiskPolicyError) as cm:
                rp.load_policy(profile={"risk": risk})
            self.assertIn(section, str(cm.exception))
            self.assertIn("bogus", str(cm.exception))

    def test_unknown_key_in_protected_area_is_rejected(self):
        with self.assertRaises(rp.RiskPolicyError) as cm:
            rp.load_policy(profile={"risk": {"protectedAreas": [
                {"paths": ["libs/"], "floor": "red", "reason": "x", "owner": "@org/platform"},
            ]}})
        self.assertIn("owner", str(cm.exception))

    def test_empty_protected_area_paths_is_rejected(self):
        with self.assertRaises(rp.RiskPolicyError):
            rp.load_policy(profile={"risk": {"protectedAreas": [
                {"paths": [], "floor": "red", "reason": "x"},
            ]}})

    def test_points_merge_is_scoped_and_additive(self):
        merged = rp.load_policy(profile={"risk": {
            "points": {"filesOverMax": 20, "taskClass": {"docs": 1}},
        }})
        self.assertEqual(merged["points"]["filesOverMax"], 20)
        self.assertEqual(merged["points"]["taskClass"]["docs"], 1)
        self.assertEqual(merged["points"]["taskClass"]["chore"], 5)  # untouched classes survive

    def test_points_rejects_unknown_task_class(self):
        with self.assertRaises(rp.RiskPolicyError):
            rp.load_policy(profile={"risk": {"points": {"taskClass": {"sprint": 1}}}})

    def test_points_rejects_non_integer_values(self):
        with self.assertRaises(rp.RiskPolicyError):
            rp.load_policy(profile={"risk": {"points": {"filesOverMax": True}}})

    def test_check_layer_deep_copies_so_merged_never_aliases_the_profile(self):
        profile = {"risk": {"protectedAreas": [{"paths": ["libs/"], "floor": "red", "reason": "x"}]}}
        merged = rp.load_policy(profile=profile)
        merged["protectedAreas"][0]["floor"] = "yellow"
        self.assertEqual(profile["risk"]["protectedAreas"][0]["floor"], "red")


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

    def test_autoMerge_rejects_any_non_false_value(self):
        for bad in (1, "true", "True"):
            with self.assertRaises(rp.RiskPolicyError) as cm:
                rp.load_policy(profile={"risk": {"autoMerge": bad}})
            self.assertIn("auto-merge", str(cm.exception))

    def test_autoMerge_false_is_accepted(self):
        merged = rp.load_policy(profile={"risk": {"autoMerge": False}})
        self.assertIn(merged.get("autoMerge"), (None, False))

    def test_assert_invariants_is_callable_on_a_merged_document(self):
        merged = rp.load_policy()
        rp.assert_invariants(merged, rp.load_defaults())  # no raise
        broken = json.loads(json.dumps(merged))
        broken["factoryControl"]["floor"] = "yellow"
        with self.assertRaises(rp.RiskPolicyError):
            rp.assert_invariants(broken, rp.load_defaults())

    def test_assert_invariants_rejects_malformed_merged_documents(self):
        defaults = rp.load_defaults()
        broken = json.loads(json.dumps(rp.load_policy()))
        del broken["factoryControl"]
        with self.assertRaises(rp.RiskPolicyError) as cm:
            rp.assert_invariants(broken, defaults)
        self.assertIn("factoryControl", str(cm.exception))
        broken = json.loads(json.dumps(rp.load_policy()))
        broken["sensitiveDomains"]["tokens"] = []
        with self.assertRaises(rp.RiskPolicyError):
            rp.assert_invariants(broken, defaults)

    def test_assert_invariants_rejects_unhashable_factory_control_paths(self):
        # a hand-built document with a dict in factoryControl.paths must raise
        # RiskPolicyError, not TypeError, from the set() comparison
        defaults = rp.load_defaults()
        broken = json.loads(json.dumps(rp.load_policy()))
        broken["factoryControl"]["paths"].append({"not": "a string"})
        with self.assertRaises(rp.RiskPolicyError):
            rp.assert_invariants(broken, defaults)


class PolicyFileValidation(unittest.TestCase):
    def _doctored(self, mutate):
        doc = json.loads(json.dumps(POLICY))
        mutate(doc)
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = tmp / "risk-policy.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        return str(path)

    def test_policy_file_requires_exact_task_classes(self):
        path = self._doctored(lambda d: d["points"]["taskClass"].pop("docs"))
        with self.assertRaises(rp.RiskPolicyError):
            rp.load_defaults(policy_path=path)

    def test_policy_file_requires_tier_valid_reversibility_floors(self):
        path = self._doctored(lambda d: d["reversibility"]["lockfiles"].__setitem__("floor", "purple"))
        with self.assertRaises(rp.RiskPolicyError):
            rp.load_defaults(policy_path=path)

    def test_policy_file_requires_tier_valid_public_contract_floor(self):
        path = self._doctored(lambda d: d["publicContract"].__setitem__("floor", "purple"))
        with self.assertRaises(rp.RiskPolicyError):
            rp.load_defaults(policy_path=path)

    def test_policy_file_requires_public_contract_paths_be_a_string_list(self):
        path = self._doctored(lambda d: d["publicContract"].__setitem__("paths", "not-a-list"))
        with self.assertRaises(rp.RiskPolicyError):
            rp.load_defaults(policy_path=path)

    def test_policy_file_requires_tier_valid_side_effects_floor(self):
        path = self._doctored(lambda d: d["sideEffects"].__setitem__("floor", "purple"))
        with self.assertRaises(rp.RiskPolicyError):
            rp.load_defaults(policy_path=path)

    def test_policy_file_requires_side_effects_paths_be_a_string_list(self):
        path = self._doctored(lambda d: d["sideEffects"].__setitem__("paths", [1]))
        with self.assertRaises(rp.RiskPolicyError):
            rp.load_defaults(policy_path=path)

    def test_policy_file_requires_tier_valid_codeowners_default_floor(self):
        path = self._doctored(lambda d: d["codeowners"].__setitem__("defaultOwnedFloor", "purple"))
        with self.assertRaises(rp.RiskPolicyError):
            rp.load_defaults(policy_path=path)

    def test_policy_file_requires_codeowners_owner_floors_be_an_object(self):
        path = self._doctored(lambda d: d["codeowners"].__setitem__("ownerFloors", []))
        with self.assertRaises(rp.RiskPolicyError):
            rp.load_defaults(policy_path=path)

    def test_policy_file_requires_codeowners_owner_tokens_be_valid(self):
        path = self._doctored(lambda d: d["codeowners"]["ownerFloors"].__setitem__("not an owner", "red"))
        with self.assertRaises(rp.RiskPolicyError):
            rp.load_defaults(policy_path=path)

    def test_policy_file_requires_codeowners_owner_floor_be_a_known_tier(self):
        path = self._doctored(lambda d: d["codeowners"]["ownerFloors"].__setitem__("@org/platform", "purple"))
        with self.assertRaises(rp.RiskPolicyError):
            rp.load_defaults(policy_path=path)


class PathRules(unittest.TestCase):
    def test_prefix_exact_and_glob(self):
        self.assertTrue(rp.match_rule("apps/api/src/auth/", "apps/api/src/auth/guard.ts"))
        self.assertFalse(rp.match_rule("apps/api/src/auth/", "apps/api/src/author/x.ts"))
        self.assertTrue(rp.match_rule("package.json", "package.json"))
        self.assertFalse(rp.match_rule("package.json", "apps/package.json"))
        self.assertTrue(rp.match_rule("**/package.json", "apps/package.json"))
        self.assertTrue(rp.match_rule("**/migrations/**", "libs/db/migrations/0001.ts"))
        self.assertFalse(rp.match_rule("**/migrations/**", "libs/db/migrations"))
        self.assertTrue(rp.match_rule("**/*.d.ts", "app/services/api-client.d.ts"))
        self.assertFalse(rp.match_rule("*.d.ts", "app/services/api-client.d.ts"))
        self.assertTrue(rp.match_rule("libs/*/src/**", "libs/db/src/a/b.ts"))
        self.assertFalse(rp.match_rule("libs/*/src/**", "libs/db/deep/src/a.ts"))

    def test_prefix_rule_matches_the_directory_itself(self):
        self.assertTrue(rp.match_rule("apps/api/src/auth/", "apps/api/src/auth"))
        self.assertFalse(rp.match_rule("apps/api/src/auth/", "apps/api/src/author"))

    def test_repo_prefix_is_tried_too(self):
        self.assertEqual(rp.match_any(["api/apps/api/src/auth/"], "apps/api/src/auth/x.ts", repo="api"),
                         "api/apps/api/src/auth/")
        self.assertIsNone(rp.match_any(["api/apps/api/src/auth/"], "apps/api/src/auth/x.ts", repo="web-app"))
        self.assertIsNone(rp.match_any(["api/apps/api/src/auth/"], "apps/api/src/auth/x.ts", repo=None))

    def test_repo_prefix_rule_equal_to_repo_itself_is_not_retried(self):
        # a rule that is exactly "<repo>/" must not be retried against the
        # repo-prefixed candidate: that would match every path in a repo
        # that happens to share the rule's directory name
        self.assertIsNone(rp.match_any(["setup/"], "README.md", repo="setup"))
        self.assertEqual(rp.match_any(["setup/"], "setup/x.py", repo="setup"), "setup/")
        self.assertEqual(rp.match_any(["api/apps/"], "apps/x.ts", repo="api"), "api/apps/")

    def test_sensitive_tokens_match_segments_and_name_parts_only(self):
        tokens = {"auth": ["auth", "token"]}
        self.assertEqual(rp.sensitive_domain(tokens, "apps/api/src/auth/guard.ts"), "auth")
        self.assertEqual(rp.sensitive_domain(tokens, "src/auth.service.ts"), "auth")
        self.assertEqual(rp.sensitive_domain(tokens, "src/AUTH-flow/x.ts"), "auth")
        self.assertEqual(rp.sensitive_domain(tokens, "docs/auth/README.md"), "auth")
        self.assertIsNone(rp.sensitive_domain(tokens, "src/author.ts"))
        self.assertIsNone(rp.sensitive_domain(tokens, "src/tokenizer.ts"))
        self.assertIsNone(rp.sensitive_domain(tokens, "src/notes/notes.service.ts"))

    def test_sensitive_tokens_match_camel_case_pascal_case_and_scoped_packages(self):
        tokens = POLICY["sensitiveDomains"]["tokens"]
        self.assertEqual(rp.sensitive_domain(tokens, "src/AuthGuard.ts"), "auth")
        self.assertEqual(rp.sensitive_domain(tokens, "src/authService.ts"), "auth")
        self.assertEqual(rp.sensitive_domain(tokens, "web-app/app/components/LoginForm.tsx"), "auth")
        self.assertEqual(rp.sensitive_domain(tokens, "node_modules/@auth/core/x.ts"), "auth")
        self.assertIsNone(rp.sensitive_domain(tokens, "src/author.ts"))
        self.assertIsNone(rp.sensitive_domain(tokens, "src/Authority.ts"))
        self.assertIsNone(rp.sensitive_domain(tokens, "apps/api/x.ts"))

    def test_sensitive_tokens_match_acronym_runs(self):
        tokens = rp.load_defaults()["sensitiveDomains"]["tokens"]
        self.assertEqual(rp.sensitive_domain(tokens, "src/PIIRedactor.ts"), "pii")
        self.assertEqual(rp.sensitive_domain(tokens, "src/JWTAuthGuard.ts"), "auth")
        self.assertIsNone(rp.sensitive_domain(tokens, "src/HTMLParser.ts"))


class Floors(unittest.TestCase):
    def setUp(self):
        self.policy = rp.load_policy(profile={"risk": {
            "protectedAreas": [{"paths": ["api/apps/integration-service/"], "floor": "red", "reason": "integration"}],
        }})

    def ids(self, paths, repo="api"):
        return sorted((f["id"], f["floor"]) for f in rp.path_floors(self.policy, paths, repo))

    def test_sensitive_domain_is_red_even_for_docs(self):
        floors = rp.path_floors(self.policy, ["apps/api/src/auth/README.md"], "api")
        self.assertEqual([(f["id"], f["floor"], f["paths"]) for f in floors],
                         [("sensitive-domain:auth", "red", ["apps/api/src/auth/README.md"])])
        self.assertIn("auth", floors[0]["reason"])
        self.assertEqual(floors[0]["rules"], ["auth"])

    def test_factory_control_migration_lockfile_public_contract(self):
        self.assertEqual(self.ids(["workflows/sdlc-red.yaml"]), [("factory-control", "red")])
        self.assertEqual(self.ids(["libs/data-access/src/lib/rds/migrations/0007.ts"]), [("migration", "red")])
        self.assertEqual(self.ids(["bun.lock"]), [("lockfile", "yellow")])
        self.assertEqual(self.ids(["apps/web/package.json"]), [("manifest", "yellow")])
        self.assertEqual(self.ids(["app/services/api-client.d.ts"]), [("public-contract", "yellow")])

    def test_protected_area_from_profile_with_repo_prefix(self):
        self.assertEqual(self.ids(["apps/integration-service/handler.ts"]), [("protected:integration", "red")])
        self.assertEqual(self.ids(["apps/integration-service/handler.ts"], repo="web-app"), [])

    def test_floors_group_paths_and_are_sorted_by_id(self):
        floors = rp.path_floors(self.policy, ["bun.lock", "pnpm-lock.yaml", "apps/api/src/billing/x.ts"], "api")
        self.assertEqual([f["id"] for f in floors], ["lockfile", "sensitive-domain:payments"])
        self.assertEqual(floors[0]["paths"], ["bun.lock", "pnpm-lock.yaml"])

    def test_plain_source_file_has_no_floor(self):
        self.assertEqual(self.ids(["apps/api/src/notes/notes.service.ts"]), [])

    def test_shared_floor_id_takes_the_max_tier_regardless_of_order(self):
        # two protectedAreas sharing a reason ("core") but different floors:
        # the aggregated floor must be the max, no matter which path is
        # scored first
        policy = rp.load_policy(profile={"risk": {"protectedAreas": [
            {"paths": ["x/"], "floor": "red", "reason": "core"},
            {"paths": ["y/"], "floor": "yellow", "reason": "core"},
        ]}})
        forward = rp.path_floors(policy, ["x/1.ts", "y/2.ts"], None)
        backward = rp.path_floors(policy, ["y/2.ts", "x/1.ts"], None)
        for floors in (forward, backward):
            self.assertEqual([f["id"] for f in floors], ["protected:core"])
            self.assertEqual(floors[0]["floor"], "red")
            self.assertEqual(floors[0]["paths"], ["x/1.ts", "y/2.ts"])

    def test_directory_forms_of_backfill_and_openapi_paths(self):
        self.assertEqual(self.ids(["scripts/backfill/x.ts"]), [("data-mutation", "red")])
        self.assertEqual(self.ids(["openapi/spec.yaml"]), [("public-contract", "yellow")])


CODEOWNERS = """# comment line
*                 @org/default
/apps/api/src/auth/   @org/security @alice
*.md              docs@example.com
/libs/data-access/  @org/platform
apps/api/src/notes/notes.service.ts  @bob
/scratch/
"""


class CodeownersParsing(unittest.TestCase):
    def test_rules_in_order_with_owners(self):
        rules = rp.parse_codeowners(CODEOWNERS)
        self.assertEqual([r[0] for r in rules],
                         ["*", "/apps/api/src/auth/", "*.md", "/libs/data-access/",
                          "apps/api/src/notes/notes.service.ts", "/scratch/"])
        self.assertEqual(rules[1][1], ["@org/security", "@alice"])
        self.assertEqual(rules[2][1], ["docs@example.com"])
        self.assertEqual(rules[5][1], [])  # pattern with no owners clears ownership

    def test_last_matching_rule_wins(self):
        rules = rp.parse_codeowners(CODEOWNERS)
        self.assertEqual(rp.codeowners_owners(rules, "apps/api/src/auth/guard.ts"), ["@org/security", "@alice"])
        self.assertEqual(rp.codeowners_owners(rules, "apps/api/src/auth/README.md"), ["docs@example.com"])
        self.assertEqual(rp.codeowners_owners(rules, "libs/data-access/src/x.ts"), ["@org/platform"])
        self.assertEqual(rp.codeowners_owners(rules, "apps/api/src/notes/notes.service.ts"), ["@bob"])
        self.assertEqual(rp.codeowners_owners(rules, "apps/api/src/notes/other.ts"), ["@org/default"])
        self.assertEqual(rp.codeowners_owners(rules, "scratch/tmp.txt"), [])

    def test_unanchored_pattern_floats_and_anchored_does_not(self):
        rules = rp.parse_codeowners("docs/  @a\n/src/  @b\n")
        self.assertEqual(rp.codeowners_owners(rules, "apps/web/docs/x.md"), ["@a"])
        self.assertEqual(rp.codeowners_owners(rules, "src/x.ts"), ["@b"])
        self.assertEqual(rp.codeowners_owners(rules, "apps/src/x.ts"), [])

    def test_malformed_lines_raise(self):
        with self.assertRaises(rp.RiskPolicyError):
            rp.parse_codeowners("@org/team apps/\n")  # owner where the pattern should be
        with self.assertRaises(rp.RiskPolicyError):
            rp.parse_codeowners("apps/ org-team\n")  # owner token is neither @handle nor email
        with self.assertRaises(rp.RiskPolicyError):
            rp.parse_codeowners("apps/ @org/team\n\x00")  # binary junk


class OwnerFloors(unittest.TestCase):
    def test_owner_floors_and_default_owned_floor(self):
        policy = rp.load_policy(profile={"risk": {"codeowners": {
            "ownerFloors": {"@org/security": "red"}, "defaultOwnedFloor": "yellow"}}})
        rules = rp.parse_codeowners(CODEOWNERS)
        touched, floors = rp.codeowner_floors(policy, rules,
                                              ["apps/api/src/auth/guard.ts", "apps/api/src/notes/other.ts"])
        self.assertEqual(touched, ["@alice", "@org/default", "@org/security"])
        self.assertEqual([(f["id"], f["floor"]) for f in floors],
                         [("codeowners:@org/security", "red"), ("codeowners:owned", "yellow")])
        self.assertEqual(floors[0]["paths"], ["apps/api/src/auth/guard.ts"])
        self.assertEqual(floors[1]["paths"], ["apps/api/src/auth/guard.ts", "apps/api/src/notes/other.ts"])
        self.assertEqual(floors[0]["rules"], ["/apps/api/src/auth/"])

    def test_unowned_paths_contribute_nothing(self):
        policy = rp.load_policy()
        rules = rp.parse_codeowners("/apps/  @org/a\n")
        touched, floors = rp.codeowner_floors(policy, rules, ["libs/x.ts"])
        self.assertEqual((touched, floors), ([], []))


class CodeownersHardening(unittest.TestCase):
    def test_inline_hash_is_literal_unless_comment_start(self):
        self.assertEqual(rp.parse_codeowners("foo#bar @a\n"), [("foo#bar", ["@a"])])
        self.assertEqual(rp.parse_codeowners("\\#file @a\n"), [("#file", ["@a"])])
        self.assertEqual(rp.parse_codeowners("docs/ @a # trailing comment\n"), [("docs/", ["@a"])])

    def test_leading_bom_is_stripped(self):
        self.assertEqual(rp.parse_codeowners("﻿* @sec\n"), [("*", ["@sec"])])

    def test_at_sign_inside_a_path_is_not_an_owner(self):
        rules = rp.parse_codeowners("/packages/@scope/ui/ @team\n")
        self.assertEqual(rules, [("/packages/@scope/ui/", ["@team"])])
        self.assertEqual(rp.codeowners_owners(rules, "packages/@scope/ui/x.ts"), ["@team"])

    def test_owner_floors_are_case_insensitive(self):
        policy = rp.load_policy(profile={"risk": {"codeowners": {"ownerFloors": {"@Org/Security": "red"}}}})
        rules = rp.parse_codeowners("* @org/SECURITY\n")
        touched, floors = rp.codeowner_floors(policy, rules, ["x.ts"])
        self.assertEqual(touched, ["@org/security"])
        self.assertIn(("codeowners:@org/security", "red"), [(f["id"], f["floor"]) for f in floors])

    def test_unsupported_syntax_is_rejected(self):
        with self.assertRaises(rp.RiskPolicyError):
            rp.parse_codeowners("!x @a\n")
        with self.assertRaises(rp.RiskPolicyError):
            rp.parse_codeowners("[ab].js @a\n")

    def test_unicode_line_separators_do_not_split_a_line(self):
        with self.assertRaises(rp.RiskPolicyError):
            rp.parse_codeowners("* @a\x85x @b\n")

    def test_trailing_slash_pattern_is_directory_only(self):
        rules = rp.parse_codeowners("build/ @a\n")
        self.assertEqual(rp.codeowners_owners(rules, "build"), [])
        self.assertEqual(rp.codeowners_owners(rules, "build/x"), ["@a"])

    def test_no_slash_pattern_matches_bare_name_and_contents(self):
        rules = rp.parse_codeowners("/docs @a\n")
        self.assertEqual(rp.codeowners_owners(rules, "docs/readme.md"), ["@a"])
        self.assertEqual(rp.codeowners_owners(rules, "docs"), ["@a"])

    def test_parse_codeowners_rejects_non_string_input(self):
        with self.assertRaises(rp.RiskPolicyError):
            rp.parse_codeowners(None)

    def test_owner_floors_casefold_collision_keeps_the_higher_tier(self):
        policy = rp.load_policy(profile={"risk": {"codeowners": {"ownerFloors": {"@A": "red", "@a": "green"}}}})
        rules = rp.parse_codeowners("* @a\n")
        touched, floors = rp.codeowner_floors(policy, rules, ["x.ts"])
        self.assertIn(("codeowners:@a", "red"), [(f["id"], f["floor"]) for f in floors])

    def test_pattern_ending_in_bare_backslash_is_malformed(self):
        with self.assertRaises(rp.RiskPolicyError):
            rp.parse_codeowners("a\\ #b @x\n")


class GoodwordSeed(unittest.TestCase):
    ARCHON = SETUP.parent

    def test_goodword_profile_seeds_protected_areas_from_hot_paths(self):
        profile = json.loads((self.ARCHON / "profiles/goodword/project.v1.json").read_text(encoding="utf-8"))
        hot = json.loads((self.ARCHON / "profiles/goodword/envelope.json").read_text(encoding="utf-8"))["hot_paths"]
        seeded = [p for area in profile["risk"]["protectedAreas"] for p in area["paths"]]
        self.assertEqual(sorted(seeded), sorted(hot))
        merged = rp.load_policy(profile=profile)
        floors = rp.path_floors(merged, ["apps/api/src/global-search/x.ts"], "api")
        self.assertEqual([f["floor"] for f in floors], ["red"])
        floors = rp.path_floors(merged, ["app/services/api-client.d.ts"], "web-app")
        self.assertEqual(sorted(f["id"] for f in floors), ["protected:generated-api-client", "public-contract"])

    def test_both_schemas_accept_the_risk_block(self):
        for name in ("project-profile.v1.schema.json", "project-profile.v2.schema.json"):
            schema = json.loads((self.ARCHON / "profiles" / name).read_text(encoding="utf-8"))
            risk = schema["properties"]["risk"]
            self.assertEqual(risk["type"], "object")
            self.assertFalse(risk.get("additionalProperties", True))
            self.assertIn("protectedAreas", risk["properties"])
            self.assertIn("codeowners", risk["properties"])
            self.assertNotIn("risk", schema["required"])


class OwnerFloorsValidation(unittest.TestCase):
    def test_owner_floors_rejects_non_handle_keys(self):
        with self.assertRaises(rp.RiskPolicyError):
            rp.load_policy(profile={"risk": {"codeowners": {"ownerFloors": {"team-x": "red"}}}})


class PointsSchemaKeys(unittest.TestCase):
    ARCHON = SETUP.parent

    def test_points_schema_matches_module_constants(self):
        expected_points = set(rp._REQUIRED_POINTS) | {"taskClass", "triage"}
        for name in ("project-profile.v1.schema.json", "project-profile.v2.schema.json"):
            schema = json.loads((self.ARCHON / "profiles" / name).read_text(encoding="utf-8"))
            points = schema["properties"]["risk"]["properties"]["points"]
            self.assertEqual(set(points["properties"]), expected_points)
            self.assertEqual(set(points["properties"]["taskClass"]["properties"]), set(rp.TASK_CLASSES))
            self.assertEqual(set(points["properties"]["triage"]["properties"]), {"S", "M", "L"})


class SchemaValidation(unittest.TestCase):
    ARCHON = SETUP.parent
    MINIMAL_PROFILE = {
        "profileVersion": "archon.project-profile.v1",
        "projectId": "project:fixture",
        "repository": {"remote": "https://github.com/example/fixture.git", "defaultBranch": "main",
                        "stack": "nextjs-workspace", "packageManager": "pnpm@10.21.0"},
        "verification": [{"id": "test", "argv": ["true"], "timeoutSeconds": 30}],
        "scope": {"allowedPaths": ["x"], "forbiddenPaths": []},
        "knowledge": {"paths": ["README.md"], "maxBytes": 4096},
        "recovery": {"maxRounds": 1},
        "delivery": {"draftOnly": True, "autoMerge": False, "autoDeploy": False},
    }

    @unittest.skipUnless(jsonschema is not None, "jsonschema not installed")
    def test_goodword_profile_validates_against_v1_schema(self):
        schema = json.loads((self.ARCHON / "profiles/project-profile.v1.schema.json").read_text(encoding="utf-8"))
        profile = json.loads((self.ARCHON / "profiles/goodword/project.v1.json").read_text(encoding="utf-8"))
        jsonschema.validate(profile, schema)

    @unittest.skipUnless(jsonschema is not None, "jsonschema not installed")
    def test_blank_reason_is_rejected_by_schema(self):
        schema = json.loads((self.ARCHON / "profiles/project-profile.v1.schema.json").read_text(encoding="utf-8"))
        profile = dict(self.MINIMAL_PROFILE)
        profile["risk"] = {"protectedAreas": [{"paths": ["x/"], "floor": "red", "reason": " "}]}
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(profile, schema)


if __name__ == "__main__":
    unittest.main()
