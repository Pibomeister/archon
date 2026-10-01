# Risk-tiered SDLC, Slice 1: scorer, policy, CODEOWNERS, ledger pointer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a deterministic, fail-closed risk score exist for a run at three stages (intake, plan, impl), with a policy file, an importable policy module (merge, invariants, path rules, CODEOWNERS), and a `risk` block in the trace digest, without changing any lane yet.

**Architecture:** `setup/risk_policy.py` is a pure, importable module (loading and merging policy, floor invariants, path matching, CODEOWNERS parsing). `setup/risk-score.py` is the stdlib-only CLI the lanes will call; it reads stage inputs from the artifacts dir, computes `tier = max(mechanical, agent, prior, override)`, writes `risk-<stage>.json` atomically and appends one line to `risk-trajectory.jsonl`. `setup/trace-digest.py` learns to summarize that trajectory as a `risk` block so the skills library ledger can see it through the existing ingest pointer. Profiles gain an optional `risk` object (schemas v1 and v2, additive) and the Goodword pack is seeded from today's `hot_paths`.

**Tech Stack:** Python stdlib only (workflow nodes run the system `python3`, which is 3.9.6 on this machine, so no `match`, no `X | Y` at runtime). Tests are `unittest`, run through `uv run --offline --no-project --python 3.13.9 --with pyyaml==6.0.3 python -m unittest ...` from the repository root, and once through `/usr/bin/python3` to prove 3.9 compatibility.

**Spec:** `docs/superpowers/specs/2026-09-29-sdlc-risk-tiers-design.md`, section "Slice 1". Decisions carried: floors dominate points; `max` across sources; override never lowers; fail closed to red; CODEOWNERS optional, sensitive domains always red.

**Worktree and branch:** `/Users/eduardopicazo/orca/workspaces/Archon/sdlc-risk-tiers`, branch `Pibomeister/sdlc-risk-tiers`. All paths below are relative to that root. Every `git commit` ends with the trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Never push to `origin`; the slice ships through `git push no-mistakes Pibomeister/sdlc-risk-tiers` at the end.

**Test command (alias used below):**

```bash
UVT='uv run --offline --no-project --python 3.13.9 --with pyyaml==6.0.3 python -m unittest'
```

Run it from the worktree root, e.g. `$UVT setup.tests.test_risk_policy -v`.

---

## File map

| File | Responsibility |
|---|---|
| `setup/risk-policy.json` (create) | Default policy: thresholds, points, size thresholds, sensitive domains, factory control, reversibility, public contract, `repro_command_allow` (copied from `setup/lite-envelope.json` so bugfix can switch later). |
| `setup/risk_policy.py` (create) | Pure helpers: constants, tier math, JSON loading, policy merge (defaults ← profile.risk ← overlay), `assert_invariants`, path rules, floor evaluation, CODEOWNERS parser and owner lookup. No I/O beyond reading the files it is handed. |
| `setup/risk-score.py` (create) | The CLI. Stage readers, signal computation, output document, typed line. Imports `risk_policy` and `skill_library`. |
| `setup/trace-digest.py` (modify) | New `risk` block from `risk-trajectory.jsonl` (+ `delivery.json` when present). `DIGEST_VERSION` stays 1: the key is additive. |
| `profiles/project-profile.v1.schema.json`, `profiles/project-profile.v2.schema.json` (modify) | Optional `risk` property (same sub-schema in both). |
| `profiles/goodword/project.v1.json` (modify) | Seeded `risk.protectedAreas` from today's `hot_paths`. |
| `setup/package.sh` (modify) | Ship the three new setup files. |
| `setup/tests/test_risk_policy.py`, `setup/tests/test_risk_score.py` (create); `setup/tests/test_trace_digest.py`, `setup/tests/test_setup_scripts_are_packaged.py` (modify) | Tests. |

## Contracts fixed by this plan

**Typed last line of `risk-score.py`:**

```
RISK_TIER=<red|yellow|green> stage=<intake|plan|impl> score=<n> floors=<id,id|none>   exit 0
RISK=FAIL <reason>                                                                   exit 1
```

`RISK=FAIL` is a fail-class line (`FAIL` is in `skill_library.FAIL_VALUES`), so a node that ends on it is a failed terminal in the trace digest; a calling node treats it as red. `RISK_TIER=<tier>` is neutral (not in pass or fail vocabularies) on purpose: the lane overlays in Slice 4 decide what to do with it.

**`risk-<stage>.json` (`archon.risk-score.v1`)** is byte-deterministic for the same inputs (no timestamps). Shape:

```json
{
  "schema": "archon.risk-score.v1",
  "scoringVersion": 1, "policyVersion": 1, "overlayVersion": null,
  "stage": "plan", "laneTier": null,
  "mechanical": {"tier": "red", "score": 32,
                 "signals": [{"id": "task-class", "value": "feature", "points": 15, "floor": null,
                              "source": "mechanical", "evidence": "Kind: feature"}]},
  "agent": {"tier": null, "rationale": null, "unknowns": []},
  "prior": {"tier": null, "stage": null},
  "override": null,
  "tier": "red",
  "floors": [{"id": "sensitive-domain:auth", "reason": "sensitive domain auth", "paths": ["apps/api/src/auth/x.ts"]}],
  "codeowners": {"present": false, "ownersTouched": [], "floorsApplied": []},
  "inputs": {"briefSha256": "...", "allowlistSha256": null, "diffSha256": null, "codeownersSha256": null,
             "profileSha256": "...", "policySha256": "...", "overlaySha256": null,
             "baseCommit": null, "head": null},
  "escalation": {"required": false, "toTier": null},
  "escalated": false, "escalatedFrom": null, "handoffRunId": null
}
```

`laneTier`, `escalation`, `handoffRunId` are in addition to the spec's field list: `--lane-tier` lets the Slice 4 overlays ask "is this above my lane?" without re-deriving it, and `handoffRunId` is how the ledger links the two runs of an escalation.

**`risk-trajectory.jsonl`**: one line per scorer invocation, `{"at", "stage", "tier", "score", "floors": [ids], "mechanical", "agent", "prior", "override", "laneTier", "escalate", "escalatedFrom", "handoffRunId"}`. `at` is the only non-deterministic field and lives only here.

**Path rules** (`risk_policy.match_rule`): `"dir/"` is a prefix rule; `"name.ext"` is exact; anything with `*` or `?` is a glob where `**` spans segments and `*` stays inside one segment; a glob starting with `**/` floats, any other glob is anchored at the repo root. Every rule is tried against the bare repo-relative path AND against `<repo>/<path>`, so the Goodword `hot_paths` convention (`api/apps/api/src/auth/`) keeps working.

**Sensitive-domain tokens** match path segments or dot/dash/underscore separated name parts, case-insensitively: `auth` matches `apps/api/src/auth/guard.ts` and `src/auth.service.ts` but not `src/author.ts` or `tokenizer.ts`.

---

### Task 1: Policy file and the pure policy module (constants, tier math, loading, merge, invariants)

**Files:**
- Create: `setup/risk-policy.json`
- Create: `setup/risk_policy.py`
- Test: `setup/tests/test_risk_policy.py`

- [ ] **Step 1: Write the failing tests for tier math, loading, merge and invariants**

Create `setup/tests/test_risk_policy.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `$UVT setup.tests.test_risk_policy -v`
Expected: `ModuleNotFoundError: No module named 'risk_policy'` (import error at collection).

- [ ] **Step 3: Write `setup/risk-policy.json`**

```json
{
  "schema": "archon.risk-policy.v1",
  "version": 1,
  "scoringVersion": 1,
  "thresholds": {"yellow": 30, "red": 60},
  "points": {
    "taskClass": {"docs": 0, "chore": 5, "bugfix": 10, "feature": 15, "refactor": 20, "migration": 30},
    "triage": {"S": 0, "M": 10, "L": 25},
    "filesOverMax": 15,
    "perExtraFile": 2,
    "testFilesOverMax": 5,
    "callersOverMax": 15,
    "chainLinksOverMax": 10,
    "impactUnavailable": 20,
    "impactMissing": 20,
    "unknownCoverage": 10
  },
  "sizeThresholds": {"max_files": 4, "max_test_files": 3, "max_d1_callers": 10, "max_chain_links": 7},
  "sensitiveDomains": {
    "floor": "red",
    "tokens": {
      "auth": ["auth", "oauth", "session", "sessions", "token", "tokens", "jwt", "sso", "login", "password", "passwords"],
      "crypto": ["crypto", "kms", "cipher", "encrypt", "encryption", "signing"],
      "secrets": ["secret", "secrets", "credential", "credentials", "vault"],
      "payments": ["payment", "payments", "billing", "stripe", "invoice", "invoices", "checkout"],
      "pii": ["pii", "gdpr", "consent", "personal-data"]
    },
    "extraPaths": []
  },
  "factoryControl": {
    "floor": "red",
    "paths": ["workflows/", "setup/", "profiles/", "CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS",
              ".github/workflows/", ".gitlab-ci.yml", ".circleci/", "Jenkinsfile", ".buildkite/"]
  },
  "reversibility": {
    "lockfiles": {"floor": "yellow", "paths": ["package-lock.json", "pnpm-lock.yaml", "bun.lock", "bun.lockb",
                                                "yarn.lock", "uv.lock", "poetry.lock", "Cargo.lock", "go.sum",
                                                "**/package-lock.json", "**/pnpm-lock.yaml", "**/bun.lock", "**/yarn.lock"]},
    "manifests": {"floor": "yellow", "paths": ["package.json", "pyproject.toml", "Cargo.toml", "go.mod", "**/package.json"]},
    "migrations": {"floor": "red", "paths": ["migrations/", "**/migrations/**", "**/migrate/**"]},
    "dataMutation": {"floor": "red", "paths": ["**/backfill*", "**/scripts/data/**", "**/seeds/**"]}
  },
  "publicContract": {"floor": "yellow", "paths": ["**/generated/**", "**/*.d.ts", "**/dto/**", "**/openapi*", "**/*.proto", "**/*.graphql"]},
  "sideEffects": {"floor": "yellow", "paths": []},
  "protectedAreas": [],
  "codeowners": {"defaultOwnedFloor": "yellow", "ownerFloors": {}},
  "repro_command_allow": ["bun run test ", "bun run test:integration ", "pnpm test --run "]
}
```

- [ ] **Step 4: Write `setup/risk_policy.py` (part 1: constants, tier math, loading, merge, invariants)**

```python
#!/usr/bin/env python3
"""Pure helpers for the risk-tiered lanes (sdlc-red / sdlc-yellow / sdlc-green).

Policy loading and merging, the floor invariants, path rules, floor
evaluation, and the CODEOWNERS parser. Imported by setup/risk-score.py (the
scorer), later by setup/risk-calibrate.py and the lane gates. Stdlib only and
Python 3.9 compatible: workflow nodes run the system python3.

Precedence: setup/risk-policy.json defaults <- profile["risk"] <- overlay file.
Later layers ADD protected areas, extra sensitive paths, owner floors and may
move thresholds. assert_invariants rejects any merged document that lowers a
sensitiveDomains or factoryControl floor below the defaults, drops a default
sensitive token or factory path, or carries autoMerge: true. The same check
runs at load, at proposal and at admit (Slice 5), so no layer can sneak a
weaker floor in.

Path rules ("anchored-prefix globs", the lite-envelope.sh semantics plus globs):
  "dir/"        prefix: dir/ and everything beneath it
  "file.ext"    exact match
  "**/x/**"     glob: ** spans segments, * stays inside one segment; a pattern
                starting with **/ floats, any other glob is anchored at the root
Every rule is tried against the bare repo-relative path AND "<repo>/<path>",
so a profile pack that lists paths with a repository prefix (the Goodword
hot_paths convention, "api/apps/api/src/auth/") keeps working unchanged.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Optional

SCHEMA_POLICY = "archon.risk-policy.v1"
SCHEMA_OVERLAY = "archon.risk-policy-overlay.v1"
SCHEMA_SCORE = "archon.risk-score.v1"
SCHEMA_JUDGMENT = "archon.risk-judgment.v1"
SCHEMA_ESCALATION = "archon.risk-escalation.v1"
TIERS = ("green", "yellow", "red")
TIER_RANK = {t: i for i, t in enumerate(TIERS)}
TASK_CLASSES = ("docs", "chore", "bugfix", "feature", "refactor", "migration")
STAGES = ("intake", "plan", "impl")
CODEOWNERS_CANDIDATES = ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS")
DEFAULT_POLICY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "risk-policy.json")
_REQUIRED_POINTS = ("taskClass", "triage", "filesOverMax", "perExtraFile", "testFilesOverMax",
                    "callersOverMax", "chainLinksOverMax", "impactUnavailable", "impactMissing",
                    "unknownCoverage")
_REQUIRED_SIZES = ("max_files", "max_test_files", "max_d1_callers", "max_chain_links")


class RiskPolicyError(ValueError):
    pass


# --- tier math ---------------------------------------------------------------
def _check_tier(tier: Any, label: str = "tier") -> str:
    if tier not in TIERS:
        raise RiskPolicyError(f"{label} must be one of {', '.join(TIERS)}: {tier!r}")
    return tier


def tier_max(*tiers: Optional[str]) -> Optional[str]:
    """Highest tier among the non-None arguments; None when there is none."""
    best = None
    for t in tiers:
        if t is None:
            continue
        _check_tier(t)
        if best is None or TIER_RANK[t] > TIER_RANK[best]:
            best = t
    return best


def tier_gt(a: str, b: str) -> bool:
    return TIER_RANK[_check_tier(a)] > TIER_RANK[_check_tier(b)]


def tier_from_score(score: int, thresholds: dict) -> str:
    if score >= thresholds["red"]:
        return "red"
    if score >= thresholds["yellow"]:
        return "yellow"
    return "green"


# --- hashing -----------------------------------------------------------------
def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: str) -> str:
    with open(path, "rb") as fh:
        return sha256_bytes(fh.read())


# --- loading -----------------------------------------------------------------
def load_json(path: str, label: str) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except OSError as exc:
        raise RiskPolicyError(f"{label} unreadable: {exc}")
    except ValueError as exc:
        raise RiskPolicyError(f"{label} is not JSON: {exc}")


def load_defaults(policy_path: Optional[str] = None) -> dict:
    doc = load_json(policy_path or DEFAULT_POLICY_PATH, "risk-policy.json")
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA_POLICY:
        raise RiskPolicyError(f"risk-policy.json schema must be {SCHEMA_POLICY}")
    for key in ("version", "scoringVersion"):
        if not isinstance(doc.get(key), int) or isinstance(doc.get(key), bool):
            raise RiskPolicyError(f"risk-policy.json {key} must be an integer")
    _check_thresholds(doc.get("thresholds"), "risk-policy.json thresholds")
    points = doc.get("points")
    if not isinstance(points, dict) or any(k not in points for k in _REQUIRED_POINTS):
        raise RiskPolicyError("risk-policy.json points is incomplete")
    sizes = doc.get("sizeThresholds")
    if not isinstance(sizes, dict) or any(not _is_nonneg_int(sizes.get(k)) for k in _REQUIRED_SIZES):
        raise RiskPolicyError("risk-policy.json sizeThresholds is incomplete")
    for key in ("sensitiveDomains", "factoryControl", "reversibility", "publicContract", "sideEffects", "codeowners"):
        if not isinstance(doc.get(key), dict):
            raise RiskPolicyError(f"risk-policy.json {key} must be an object")
    _check_tier(doc["sensitiveDomains"].get("floor"), "sensitiveDomains.floor")
    _check_tier(doc["factoryControl"].get("floor"), "factoryControl.floor")
    _check_str_list(doc["factoryControl"].get("paths"), "factoryControl.paths")
    tokens = doc["sensitiveDomains"].get("tokens")
    if not isinstance(tokens, dict) or not tokens:
        raise RiskPolicyError("sensitiveDomains.tokens must be a non-empty object")
    for name, words in tokens.items():
        _check_str_list(words, f"sensitiveDomains.tokens.{name}")
    _check_areas(doc.get("protectedAreas", []), "risk-policy.json protectedAreas")
    _check_str_list(doc.get("repro_command_allow"), "repro_command_allow")
    return doc


def _is_nonneg_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _check_str_list(value: Any, label: str) -> list:
    if not isinstance(value, list) or not all(isinstance(s, str) and s for s in value):
        raise RiskPolicyError(f"{label} must be a list of non-empty strings")
    return value


def _check_thresholds(th: Any, label: str) -> dict:
    if not isinstance(th, dict) or not _is_nonneg_int(th.get("yellow")) or not _is_nonneg_int(th.get("red")):
        raise RiskPolicyError(f"{label} must carry integer yellow and red")
    if th["yellow"] > th["red"]:
        raise RiskPolicyError(f"{label}: yellow threshold above red")
    return th


def _check_areas(areas: Any, label: str) -> list:
    if not isinstance(areas, list):
        raise RiskPolicyError(f"{label} must be a list")
    for i, area in enumerate(areas):
        if not isinstance(area, dict):
            raise RiskPolicyError(f"{label}[{i}] must be an object")
        _check_str_list(area.get("paths"), f"{label}[{i}].paths")
        _check_tier(area.get("floor"), f"{label}[{i}].floor")
        if not isinstance(area.get("reason"), str) or not area["reason"].strip():
            raise RiskPolicyError(f"{label}[{i}].reason is required")
    return areas


def _check_layer(layer: Any, label: str) -> dict:
    """A profile.risk block or an overlay: every key optional, each typed."""
    if not isinstance(layer, dict):
        raise RiskPolicyError(f"{label} must be an object")
    if "thresholds" in layer:
        th = layer["thresholds"]
        if not isinstance(th, dict):
            raise RiskPolicyError(f"{label}.thresholds must be an object")
        for k, v in th.items():
            if k not in ("yellow", "red") or not _is_nonneg_int(v):
                raise RiskPolicyError(f"{label}.thresholds.{k} must be a non-negative integer")
    if "protectedAreas" in layer:
        _check_areas(layer["protectedAreas"], f"{label}.protectedAreas")
    for key in ("sensitiveDomains", "factoryControl", "publicContract", "sideEffects", "codeowners"):
        if key in layer and not isinstance(layer[key], dict):
            raise RiskPolicyError(f"{label}.{key} must be an object")
    sd = layer.get("sensitiveDomains") or {}
    if "extraPaths" in sd:
        _check_str_list(sd["extraPaths"], f"{label}.sensitiveDomains.extraPaths")
    if "tokens" in sd:
        if not isinstance(sd["tokens"], dict):
            raise RiskPolicyError(f"{label}.sensitiveDomains.tokens must be an object")
        for name, words in sd["tokens"].items():
            _check_str_list(words, f"{label}.sensitiveDomains.tokens.{name}")
    for key in ("factoryControl", "publicContract", "sideEffects"):
        if "paths" in (layer.get(key) or {}):
            _check_str_list(layer[key]["paths"], f"{label}.{key}.paths")
    co = layer.get("codeowners") or {}
    if "ownerFloors" in co:
        if not isinstance(co["ownerFloors"], dict):
            raise RiskPolicyError(f"{label}.codeowners.ownerFloors must be an object")
        for owner, tier in co["ownerFloors"].items():
            _check_tier(tier, f"{label}.codeowners.ownerFloors[{owner}]")
    if "defaultOwnedFloor" in co:
        _check_tier(co["defaultOwnedFloor"], f"{label}.codeowners.defaultOwnedFloor")
    return layer


def _merge_layer(merged: dict, layer: dict) -> None:
    if "thresholds" in layer:
        merged["thresholds"] = dict(merged["thresholds"], **layer["thresholds"])
    if "protectedAreas" in layer:
        merged["protectedAreas"] = list(merged.get("protectedAreas", [])) + list(layer["protectedAreas"])
    sd = layer.get("sensitiveDomains")
    if sd:
        target = merged["sensitiveDomains"]
        if "floor" in sd:
            target["floor"] = sd["floor"]
        if "extraPaths" in sd:
            target["extraPaths"] = list(target.get("extraPaths", [])) + list(sd["extraPaths"])
        if "tokens" in sd:
            tokens = {name: list(words) for name, words in target["tokens"].items()}
            for name, words in sd["tokens"].items():
                have = tokens.setdefault(name, [])
                have.extend(w for w in words if w not in have)
            target["tokens"] = tokens
    for key in ("factoryControl", "publicContract", "sideEffects"):
        src = layer.get(key)
        if src:
            target = merged[key]
            if "floor" in src:
                target["floor"] = src["floor"]
            if "paths" in src:
                target["paths"] = list(target.get("paths", [])) + list(src["paths"])
    co = layer.get("codeowners")
    if co:
        target = merged["codeowners"]
        if "ownerFloors" in co:
            target["ownerFloors"] = dict(target.get("ownerFloors", {}), **co["ownerFloors"])
        if "defaultOwnedFloor" in co:
            target["defaultOwnedFloor"] = co["defaultOwnedFloor"]
    if "autoMerge" in layer:
        merged["autoMerge"] = layer["autoMerge"]


def assert_invariants(merged: dict, defaults: dict) -> None:
    """The floors that no layer may weaken. Raised at load, proposal and admit."""
    for key in ("sensitiveDomains", "factoryControl"):
        if tier_gt(defaults[key]["floor"], merged[key]["floor"]):
            raise RiskPolicyError(f"{key} floor cannot be lowered below {defaults[key]['floor']}")
    for name, words in defaults["sensitiveDomains"]["tokens"].items():
        kept = set(merged["sensitiveDomains"]["tokens"].get(name, []))
        missing = sorted(set(words) - kept)
        if missing:
            raise RiskPolicyError(f"sensitiveDomains.tokens.{name} cannot drop {', '.join(missing)}")
    missing = sorted(set(defaults["factoryControl"]["paths"]) - set(merged["factoryControl"]["paths"]))
    if missing:
        raise RiskPolicyError(f"factoryControl.paths cannot drop {', '.join(missing)}")
    if merged.get("autoMerge") is True:
        raise RiskPolicyError("risk policy cannot enable auto-merge")
    _check_thresholds(merged.get("thresholds"), "merged thresholds")


def load_policy(policy_path: Optional[str] = None, profile: Optional[dict] = None,
                overlay_path: Optional[str] = None) -> dict:
    """defaults <- profile.risk <- overlay, validated and invariant-checked.
    The result carries overlayVersion (int or None) for the score document."""
    defaults = load_defaults(policy_path)
    merged = json.loads(json.dumps(defaults))
    merged.setdefault("protectedAreas", [])
    merged["sensitiveDomains"].setdefault("extraPaths", [])
    merged["overlayVersion"] = None
    if profile is not None:
        if not isinstance(profile, dict):
            raise RiskPolicyError("profile must be an object")
        if "risk" in profile:
            _merge_layer(merged, _check_layer(profile["risk"], "profile.risk"))
    if overlay_path:
        overlay = load_json(overlay_path, "policy overlay")
        if not isinstance(overlay, dict) or overlay.get("schema") != SCHEMA_OVERLAY:
            raise RiskPolicyError(f"policy overlay schema must be {SCHEMA_OVERLAY}")
        if not _is_nonneg_int(overlay.get("overlayVersion")):
            raise RiskPolicyError("policy overlay overlayVersion must be a non-negative integer")
        body = {k: v for k, v in overlay.items() if k not in ("schema", "overlayVersion")}
        _merge_layer(merged, _check_layer(body, "policy overlay"))
        merged["overlayVersion"] = overlay["overlayVersion"]
    assert_invariants(merged, defaults)
    return merged
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `$UVT setup.tests.test_risk_policy -v`
Expected: all tests in `TierMath`, `ShippedPolicy`, `Merge`, `Invariants` PASS (`OK`).

- [ ] **Step 6: Commit**

```bash
git add setup/risk-policy.json setup/risk_policy.py setup/tests/test_risk_policy.py
git commit -m "feat(risk): add the risk policy file and the pure policy module

Defaults, profile.risk and an overlay merge in that order; assert_invariants
refuses any layer that lowers a sensitive-domain or factory-control floor,
drops a default token or path, or enables auto-merge.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Path rules and floor evaluation

**Files:**
- Modify: `setup/risk_policy.py` (append)
- Test: `setup/tests/test_risk_policy.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `setup/tests/test_risk_policy.py` (before `if __name__ == "__main__":`):

```python
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

    def test_repo_prefix_is_tried_too(self):
        self.assertEqual(rp.match_any(["api/apps/api/src/auth/"], "apps/api/src/auth/x.ts", repo="api"),
                         "api/apps/api/src/auth/")
        self.assertIsNone(rp.match_any(["api/apps/api/src/auth/"], "apps/api/src/auth/x.ts", repo="web-app"))
        self.assertIsNone(rp.match_any(["api/apps/api/src/auth/"], "apps/api/src/auth/x.ts", repo=None))

    def test_sensitive_tokens_match_segments_and_name_parts_only(self):
        tokens = {"auth": ["auth", "token"]}
        self.assertEqual(rp.sensitive_domain(tokens, "apps/api/src/auth/guard.ts"), "auth")
        self.assertEqual(rp.sensitive_domain(tokens, "src/auth.service.ts"), "auth")
        self.assertEqual(rp.sensitive_domain(tokens, "src/AUTH-flow/x.ts"), "auth")
        self.assertEqual(rp.sensitive_domain(tokens, "docs/auth/README.md"), "auth")
        self.assertIsNone(rp.sensitive_domain(tokens, "src/author.ts"))
        self.assertIsNone(rp.sensitive_domain(tokens, "src/tokenizer.ts"))
        self.assertIsNone(rp.sensitive_domain(tokens, "src/notes/notes.service.ts"))


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$UVT setup.tests.test_risk_policy.PathRules setup.tests.test_risk_policy.Floors -v`
Expected: `AttributeError: module 'risk_policy' has no attribute 'match_rule'` (and the like).

- [ ] **Step 3: Append the path rules and floor evaluation to `setup/risk_policy.py`**

```python
# --- path rules --------------------------------------------------------------
def _glob_regex(pattern: str) -> "re.Pattern[str]":
    """** spans segments, * stays inside one segment, ? is one char.
    A pattern starting with **/ floats; everything else is anchored."""
    out = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


_GLOB_CACHE: dict = {}


def match_rule(rule: str, path: str) -> bool:
    if "*" in rule or "?" in rule:
        rx = _GLOB_CACHE.get(rule)
        if rx is None:
            rx = _GLOB_CACHE[rule] = _glob_regex(rule)
        return rx.match(path) is not None
    if rule.endswith("/"):
        return path.startswith(rule)
    return path == rule


def match_any(rules: list, path: str, repo: Optional[str] = None) -> Optional[str]:
    """The first rule matching the bare path or "<repo>/<path>", else None."""
    candidates = [path] + ([f"{repo}/{path}"] if repo else [])
    for rule in rules:
        for cand in candidates:
            if match_rule(rule, cand):
                return rule
    return None


_TOKEN_RX_CACHE: dict = {}


def sensitive_domain(tokens: dict, path: str) -> Optional[str]:
    """The first domain whose token appears as a whole path segment or as a
    dot/dash/underscore separated part of a segment, case-insensitively."""
    for domain, words in tokens.items():
        for word in words:
            rx = _TOKEN_RX_CACHE.get(word)
            if rx is None:
                rx = _TOKEN_RX_CACHE[word] = re.compile(r"(?:^|[/._-])" + re.escape(word) + r"(?:$|[/._-])", re.I)
            if rx.search(path):
                return domain
    return None


# --- floors ------------------------------------------------------------------
def path_floors(policy: dict, paths: list, repo: Optional[str]) -> list:
    """[{id, floor, reason, paths:[...]}] sorted by id, one entry per floor id,
    paths sorted and de-duplicated. Points never appear here: a floor is a
    minimum tier with a named reason, and the scorer takes the max."""
    hits: dict = {}

    def hit(fid: str, floor: str, reason: str, path: str) -> None:
        entry = hits.setdefault(fid, {"id": fid, "floor": floor, "reason": reason, "paths": set()})
        entry["paths"].add(path)

    sd = policy["sensitiveDomains"]
    fc = policy["factoryControl"]
    rev = policy["reversibility"]
    for path in paths:
        domain = sensitive_domain(sd["tokens"], path)
        if domain:
            hit(f"sensitive-domain:{domain}", sd["floor"], f"sensitive domain {domain}", path)
        if match_any(sd.get("extraPaths", []), path, repo):
            hit("sensitive-domain:profile", sd["floor"], "profile sensitive path", path)
        if match_any(fc["paths"], path, repo):
            hit("factory-control", fc["floor"], "factory control path", path)
        for fid, key, reason in (("migration", "migrations", "schema migration"),
                                 ("data-mutation", "dataMutation", "data mutation script"),
                                 ("lockfile", "lockfiles", "dependency lockfile"),
                                 ("manifest", "manifests", "package manifest")):
            rule = rev.get(key) or {}
            if match_any(rule.get("paths", []), path, repo):
                hit(fid, rule["floor"], reason, path)
        pc = policy["publicContract"]
        if match_any(pc.get("paths", []), path, repo):
            hit("public-contract", pc["floor"], "public contract surface", path)
        se = policy["sideEffects"]
        if match_any(se.get("paths", []), path, repo):
            hit("side-effects", se["floor"], "external side effect", path)
        for area in policy.get("protectedAreas", []):
            if match_any(area["paths"], path, repo):
                hit(f"protected:{area['reason']}", area["floor"], f"protected area: {area['reason']}", path)
    out = []
    for fid in sorted(hits):
        entry = hits[fid]
        out.append({"id": fid, "floor": entry["floor"], "reason": entry["reason"], "paths": sorted(entry["paths"])})
    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `$UVT setup.tests.test_risk_policy -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add setup/risk_policy.py setup/tests/test_risk_policy.py
git commit -m "feat(risk): path rules and floor evaluation

Prefix, exact and glob rules tried against the bare path and <repo>/<path>;
sensitive-domain tokens match whole segments or name parts only; floors are
grouped by id with their paths.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: CODEOWNERS parser and owner floors

**Files:**
- Modify: `setup/risk_policy.py` (append)
- Test: `setup/tests/test_risk_policy.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `setup/tests/test_risk_policy.py`:

```python
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

    def test_unowned_paths_contribute_nothing(self):
        policy = rp.load_policy()
        rules = rp.parse_codeowners("/apps/  @org/a\n")
        touched, floors = rp.codeowner_floors(policy, rules, ["libs/x.ts"])
        self.assertEqual((touched, floors), ([], []))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$UVT setup.tests.test_risk_policy.CodeownersParsing setup.tests.test_risk_policy.OwnerFloors -v`
Expected: `AttributeError: module 'risk_policy' has no attribute 'parse_codeowners'`.

- [ ] **Step 3: Append the CODEOWNERS parser to `setup/risk_policy.py`**

```python
# --- CODEOWNERS --------------------------------------------------------------
_OWNER_RE = re.compile(r"^(@[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9][A-Za-z0-9_.-]*)?|[^@\s]+@[^@\s]+\.[^@\s]+)$")


def parse_codeowners(text: str) -> list:
    """[(pattern, [owners])] in file order. GitHub semantics: '#' comments,
    gitignore-style patterns, a pattern with no owners clears ownership.
    Malformed (owner in the pattern slot, an owner token that is neither an
    @handle nor an email, control characters) raises: the scorer fails closed."""
    if any(ord(c) < 32 and c not in "\t\n\r" for c in text):
        raise RiskPolicyError("CODEOWNERS contains control characters")
    rules = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip() if not raw.lstrip().startswith("#") else ""
        if not line:
            continue
        parts = line.split()
        pattern, owners = parts[0], parts[1:]
        if pattern.startswith("@") or "@" in pattern:
            raise RiskPolicyError(f"CODEOWNERS line {n}: pattern slot holds an owner: {pattern}")
        for owner in owners:
            if not _OWNER_RE.match(owner):
                raise RiskPolicyError(f"CODEOWNERS line {n}: bad owner token: {owner}")
        rules.append((pattern, owners))
    return rules


_CO_RX_CACHE: dict = {}


def _codeowners_regex(pattern: str) -> "re.Pattern[str]":
    rx = _CO_RX_CACHE.get(pattern)
    if rx is not None:
        return rx
    anchored = pattern.startswith("/")
    body = pattern.lstrip("/")
    dir_only = body.endswith("/")
    body = body.rstrip("/")
    if "/" not in body and not anchored:
        # "docs/" or "*.md": matches at any depth
        prefix = "(?:.*/)?"
    elif anchored:
        prefix = ""
    else:
        # "apps/api/x.ts" without a leading slash: GitHub treats a pattern
        # with an inner slash as anchored too.
        prefix = ""
    core = _glob_regex(body).pattern[1:-1]  # strip ^ and $
    if dir_only or "*" not in body.split("/")[-1]:
        # a directory pattern or a plain name matches itself and everything under it
        suffix = "(?:/.*)?"
    else:
        suffix = ""
    rx = _CO_RX_CACHE[pattern] = re.compile("^" + prefix + core + suffix + "$")
    return rx


def codeowners_owners(rules: list, path: str) -> list:
    """Owners of path under last-match-wins; [] when unowned."""
    owners: list = []
    for pattern, rule_owners in rules:
        if _codeowners_regex(pattern).match(path):
            owners = rule_owners
    return list(owners)


def codeowner_floors(policy: dict, rules: list, paths: list):
    """(ownersTouched sorted, floors) from the merged policy's codeowners block:
    an ownerFloors entry names its floor; any owned path gets defaultOwnedFloor."""
    co = policy.get("codeowners") or {}
    owner_floors = co.get("ownerFloors") or {}
    default_floor = co.get("defaultOwnedFloor")
    touched: set = set()
    hits: dict = {}
    for path in paths:
        owners = codeowners_owners(rules, path)
        if not owners:
            continue
        touched.update(owners)
        for owner in owners:
            floor = owner_floors.get(owner)
            if floor:
                entry = hits.setdefault(f"codeowners:{owner}", {"floor": floor, "reason": f"owned by {owner}", "paths": set()})
                entry["paths"].add(path)
        if default_floor:
            entry = hits.setdefault("codeowners:owned", {"floor": default_floor, "reason": "path has a code owner", "paths": set()})
            entry["paths"].add(path)
    floors = [{"id": fid, "floor": hits[fid]["floor"], "reason": hits[fid]["reason"], "paths": sorted(hits[fid]["paths"])}
              for fid in sorted(hits)]
    return sorted(touched), floors
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `$UVT setup.tests.test_risk_policy -v`
Expected: all PASS. If `test_unanchored_pattern_floats_and_anchored_does_not` fails on `apps/src/x.ts`, the `/src/` rule is being treated as floating; confirm `anchored` is computed before `lstrip("/")`.

- [ ] **Step 5: Commit**

```bash
git add setup/risk_policy.py setup/tests/test_risk_policy.py
git commit -m "feat(risk): CODEOWNERS parser and owner floors

Last matching rule wins, gitignore-style anchoring, pattern-only lines clear
ownership, malformed lines raise so the scorer fails closed.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: The scorer CLI, intake stage

**Files:**
- Create: `setup/risk-score.py`
- Test: `setup/tests/test_risk_score.py`

- [ ] **Step 1: Write the failing tests (harness + intake)**

Create `setup/tests/test_risk_score.py`:

```python
#!/usr/bin/env python3
"""setup/risk-score.py: the deterministic, fail-closed risk scorer.

A temp artifacts dir and a temp git repo per test. The shipped
setup/risk-policy.json is the threshold source; tests read it rather than
restating numbers."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parent.parent
SCRIPT = SETUP / "risk-score.py"
sys.path.insert(0, str(SETUP))
import risk_policy as rp  # noqa: E402

_spec = importlib.util.spec_from_file_location("risk_score", SCRIPT)
rs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rs)

POLICY = rp.load_defaults()
PROFILE = {
    "profileVersion": "archon.project-profile.v2", "projectId": "project:test",
    "capabilities": {"defaultRepo": "api", "webRepo": "web-app"},
    "risk": {"protectedAreas": [{"paths": ["api/apps/integration-service/"], "floor": "red", "reason": "integration"}],
             "codeowners": {"ownerFloors": {"@org/security": "red"}, "defaultOwnedFloor": "yellow"}},
}


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, encoding="utf-8", check=True).stdout.strip()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rs-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ad = self.tmp / "artifacts"
        self.ad.mkdir()
        self.root = self.tmp / "repo"
        self.root.mkdir()
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.email", "t@example.com")
        git(self.root, "config", "user.name", "t")
        self.commit_file("README.md", "# repo\n")
        self.base = git(self.root, "rev-parse", "HEAD")
        self.profile = self.tmp / "profile.json"
        self.profile.write_text(json.dumps(PROFILE), encoding="utf-8")
        self.spec = self.tmp / "spec.md"
        self.write_spec("# Add note pinning\n\nKind: feature\n\nTouch apps/api/src/notes/notes.service.ts only.\n")
        self.write(self.ad, "params.json", {"spec": str(self.spec), "slug": "x", "branch": "archon/x",
                                            "worktree": str(self.root), "repo": "api"})

    def write(self, d, name, obj):
        p = Path(d) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj) if not isinstance(obj, str) else obj, encoding="utf-8")

    def write_spec(self, text):
        self.spec.write_text(text, encoding="utf-8")

    def commit_file(self, rel, text, msg="c"):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", msg)

    def run_cli(self, stage, *extra, python=None):
        argv = [python or sys.executable, str(SCRIPT), stage, "--artifacts", str(self.ad),
                "--profile", str(self.profile), "--repo-root", str(self.root), *extra]
        return subprocess.run(argv, capture_output=True, encoding="utf-8")

    def doc(self, stage):
        return json.loads((self.ad / f"risk-{stage}.json").read_text(encoding="utf-8"))

    def trajectory(self):
        return [json.loads(l) for l in (self.ad / "risk-trajectory.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]

    def assert_tier(self, r, tier, stage):
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        last = r.stdout.rstrip().splitlines()[-1]
        self.assertTrue(last.startswith(f"RISK_TIER={tier} stage={stage} score="), last)
        return last

    def assert_fail(self, r, needle):
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        last = r.stdout.rstrip().splitlines()[-1]
        self.assertTrue(last.startswith("RISK=FAIL"), last)
        self.assertIn(needle, last)
        self.assertEqual(sum(1 for l in r.stdout.splitlines() if l.startswith("RISK")), 1, r.stdout)


class Intake(Base):
    def test_docs_only_brief_is_green(self):
        self.write_spec("# Fix a typo\n\nKind: docs\n\nEdit docs/guide.md.\n")
        last = self.assert_tier(self.run_cli("intake"), "green", "intake")
        self.assertTrue(last.endswith("floors=none"), last)
        d = self.doc("intake")
        self.assertEqual(d["schema"], rp.SCHEMA_SCORE)
        self.assertEqual((d["stage"], d["tier"], d["mechanical"]["score"]), ("intake", "green", POLICY["points"]["taskClass"]["docs"]))
        self.assertEqual(d["floors"], [])
        self.assertEqual(d["codeowners"], {"present": False, "ownersTouched": [], "floorsApplied": []})
        self.assertIsNone(d["inputs"]["codeownersSha256"])
        self.assertEqual(d["inputs"]["briefSha256"], rp.sha256_file(str(self.spec)))
        self.assertEqual(d["inputs"]["profileSha256"], rp.sha256_file(str(self.profile)))
        self.assertEqual(d["inputs"]["policySha256"], rp.sha256_file(str(SETUP / "risk-policy.json")))
        self.assertIsNone(d["inputs"]["allowlistSha256"])
        self.assertIsNone(d["inputs"]["diffSha256"])
        self.assertEqual((d["escalated"], d["escalatedFrom"], d["handoffRunId"]), (False, None, None))

    def test_brief_naming_an_auth_path_is_red_by_floor(self):
        self.write_spec("# Tweak wording\n\nKind: docs\n\nOnly apps/api/src/auth/README.md changes.\n")
        last = self.assert_tier(self.run_cli("intake"), "red", "intake")
        self.assertTrue(last.endswith("floors=sensitive-domain:auth"), last)
        d = self.doc("intake")
        self.assertEqual(d["floors"][0]["paths"], ["apps/api/src/auth/README.md"])
        self.assertEqual(d["mechanical"]["score"], 0)  # floors dominate, points stay honest

    def test_missing_kind_defaults_to_feature_and_unknown_kind_fails(self):
        self.write_spec("# No kind line\n\nJust prose.\n")
        self.assert_tier(self.run_cli("intake"), "green", "intake")
        sig = [s for s in self.doc("intake")["mechanical"]["signals"] if s["id"] == "task-class"][0]
        self.assertEqual((sig["value"], sig["points"]), ("feature", POLICY["points"]["taskClass"]["feature"]))
        self.write_spec("# Bad\n\nKind: sprint\n")
        self.assert_fail(self.run_cli("intake"), "task class")

    def test_kind_heading_form(self):
        self.write_spec("# T\n\n## Kind\n\nrefactor\n\n## Goal\nx\n")
        self.assert_tier(self.run_cli("intake"), "green", "intake")
        sig = [s for s in self.doc("intake")["mechanical"]["signals"] if s["id"] == "task-class"][0]
        self.assertEqual(sig["value"], "refactor")

    def test_brief_paths_ignore_urls_and_trailing_punctuation(self):
        self.write_spec("# T\n\nKind: chore\n\nSee https://example.com/auth/docs. Edit libs/util/str.ts, then apps/api/src/billing/ (all of it).\n")
        self.assert_tier(self.run_cli("intake"), "red", "intake")
        d = self.doc("intake")
        paths = [s for s in d["mechanical"]["signals"] if s["id"] == "brief-paths"][0]["value"]
        self.assertEqual(paths, ["apps/api/src/billing/", "libs/util/str.ts"])
        self.assertEqual([f["id"] for f in d["floors"]], ["sensitive-domain:payments"])

    def test_bare_lockfile_name_counts_as_a_brief_path(self):
        self.write_spec("# Bump deps\n\nKind: chore\n\nRegenerate bun.lock.\n")
        last = self.assert_tier(self.run_cli("intake"), "yellow", "intake")
        self.assertTrue(last.endswith("floors=lockfile"), last)
        self.assertEqual(self.doc("intake")["floors"][0]["paths"], ["bun.lock"])

    def test_codeowners_at_base_commit_populates_owners_and_floors(self):
        self.commit_file("CODEOWNERS", "*  @org/default\n/apps/api/src/auth/  @org/security\n")
        base = git(self.root, "rev-parse", "HEAD")
        self.write_spec("# T\n\nKind: feature\n\nTouch apps/api/src/auth/guard.ts.\n")
        self.assert_tier(self.run_cli("intake", "--base", base), "red", "intake")
        d = self.doc("intake")
        self.assertEqual(d["codeowners"], {"present": True, "ownersTouched": ["@org/security"],
                                           "floorsApplied": ["codeowners:@org/security", "codeowners:owned"]})
        self.assertEqual(d["inputs"]["codeownersSha256"], rp.sha256_text("*  @org/default\n/apps/api/src/auth/  @org/security\n"))
        self.assertEqual(d["inputs"]["baseCommit"], base)
        ids = [f["id"] for f in d["floors"]]
        self.assertEqual(ids, ["codeowners:@org/security", "codeowners:owned", "sensitive-domain:auth"])

    def test_codeowners_is_read_at_base_not_working_tree(self):
        self.commit_file("CODEOWNERS", "*  @org/default\n")
        base = git(self.root, "rev-parse", "HEAD")
        (self.root / "CODEOWNERS").write_text("/libs/  @org/later\n", encoding="utf-8")  # uncommitted edit
        self.write_spec("# T\n\nKind: feature\n\nTouch libs/x.ts.\n")
        self.assert_tier(self.run_cli("intake", "--base", base), "yellow", "intake")
        self.assertEqual(self.doc("intake")["codeowners"]["ownersTouched"], ["@org/default"])

    def test_malformed_codeowners_fails_closed(self):
        self.commit_file(".github/CODEOWNERS", "@org/team apps/\n")
        self.assert_fail(self.run_cli("intake", "--base", git(self.root, "rev-parse", "HEAD")), "CODEOWNERS")
        self.assertFalse((self.ad / "risk-intake.json").exists())

    def test_override_raises_and_never_lowers(self):
        self.write_spec("# T\n\nKind: docs\n\nEdit docs/a.md.\n")
        self.assert_tier(self.run_cli("intake", "--override-tier", "yellow"), "yellow", "intake")
        self.assertEqual(self.doc("intake")["override"], "yellow")
        self.write_spec("# T\n\nKind: docs\n\nEdit apps/api/src/auth/a.md.\n")
        self.assert_tier(self.run_cli("intake", "--override-tier", "yellow"), "red", "intake")
        r = self.run_cli("intake", "--override-tier", "green")
        self.assertEqual(r.returncode, 2, r.stderr)  # argparse: green is not a valid override

    def test_bad_profile_or_brief_fails_closed(self):
        self.profile.write_text("{nope", encoding="utf-8")
        self.assert_fail(self.run_cli("intake"), "profile")
        self.profile.write_text(json.dumps(PROFILE), encoding="utf-8")
        os.remove(self.spec)
        self.assert_fail(self.run_cli("intake"), "brief")
        self.write(self.ad, "params.json", "[]")
        self.assert_fail(self.run_cli("intake"), "params.json")

    def test_same_inputs_same_bytes_and_trajectory_appends(self):
        self.assert_tier(self.run_cli("intake"), "green", "intake")
        first = (self.ad / "risk-intake.json").read_bytes()
        self.assert_tier(self.run_cli("intake"), "green", "intake")
        self.assertEqual(first, (self.ad / "risk-intake.json").read_bytes())
        rows = self.trajectory()
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[0]["stage"], rows[0]["tier"], rows[0]["score"]), ("intake", "green", POLICY["points"]["taskClass"]["feature"]))
        self.assertIn("at", rows[0])
        self.assertNotIn("at", json.loads(first))

    def test_lane_tier_marks_escalation_required(self):
        self.write_spec("# T\n\nKind: docs\n\nEdit apps/api/src/auth/a.md.\n")
        self.assert_tier(self.run_cli("intake", "--lane-tier", "yellow"), "red", "intake")
        d = self.doc("intake")
        self.assertEqual(d["escalation"], {"required": True, "toTier": "red"})
        self.assertEqual(d["laneTier"], "yellow")
        self.assertTrue(self.trajectory()[-1]["escalate"])
        self.assert_tier(self.run_cli("intake", "--lane-tier", "red"), "red", "intake")
        self.assertEqual(self.doc("intake")["escalation"], {"required": False, "toTier": None})

    def test_runs_under_system_python3(self):
        if not os.path.exists("/usr/bin/python3"):
            self.skipTest("no /usr/bin/python3")
        self.assert_tier(self.run_cli("intake", python="/usr/bin/python3"), "green", "intake")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$UVT setup.tests.test_risk_score -v`
Expected: `FileNotFoundError` for `setup/risk-score.py` at import.

- [ ] **Step 3: Write `setup/risk-score.py` with the intake stage**

```python
#!/usr/bin/env python3
"""Score one run's risk at one stage and write the typed verdict.

  risk-score.py <intake|plan|impl> --artifacts <dir> --profile <profile.json>
                --repo-root <worktree> [--base <sha>] [--policy <risk-policy.json>]
                [--overlay <policy-overlay.json>] [--handoff <escalation.json>]
                [--override-tier <red|yellow>] [--lane-tier <tier>] [--brief <path>]

Typed last line:
  RISK_TIER=<red|yellow|green> stage=<stage> score=<n> floors=<a,b|none>   exit 0
  RISK=FAIL <reason>                                                       exit 1

Writes <artifacts>/risk-<stage>.json (archon.risk-score.v1) atomically and
appends one line to <artifacts>/risk-trajectory.jsonl. Nothing is written on
FAIL. The calling node treats FAIL as red: a missing or unparsable required
input never scores green.

tier = max(mechanical, agent, prior, override). mechanical = max(floor tiers,
tier_from_score(points)): floors dominate, points only decide where no floor
fires. agent is risk-judgment.json (archon.risk-judgment.v1, written by
ralplan from Slice 3 on; absent is null, malformed is FAIL). prior is the
previous stage's tier in this run (risk-intake.json at plan, risk-plan.json at
impl) or the handoff's toTier when --handoff names an escalation.json.
override is --override-tier; it can only raise because it joins the max.

Stage inputs:
  intake  the brief (params.json spec, or --brief), task class from a
          "Kind: <class>" line or a "## Kind" heading (missing -> feature,
          unknown -> FAIL), the repo-relative paths the brief names, and
          CODEOWNERS at --base (or the working tree when no --base)
  plan    files-allowlist.json (required), web-files-allowlist.json (optional,
          tagged with capabilities.webRepo), impact.json, triage.json,
          risk-judgment.json, causal-chain.json (optional)
  impl    git diff --name-status <base>..HEAD where base is --base or
          bootstrap-head.txt; impact.json, triage-post.json, risk-judgment.json

The repo label for "<repo>/<path>" rule matching is params.json repo, else
profile capabilities.defaultRepo, else the basename of --repo-root.
"""
import argparse
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import risk_policy as rp  # noqa: E402
import skill_library as sl  # noqa: E402

SCORING_VERSION = 1
TEST_RE = re.compile(r"(/__tests__/|(^|/)tests?/|(^|/)test_[^/]+\.py$|_test\.(go|py)$|\.(spec|test|int\.spec|e2e\.spec)\.[cm]?[jt]sx?$)")
PATH_TOKEN_RE = re.compile(r"(?<![\w/:.])(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]*")
KIND_LINE_RE = re.compile(r"^\s*Kind:\s*([A-Za-z][\w-]*)\s*$", re.M)
KIND_HEADING_RE = re.compile(r"^## Kind\s*$\n+\s*([A-Za-z][\w-]*)", re.M)


class Fail(Exception):
    pass


# --- small readers -----------------------------------------------------------
def read_text(path, label):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        raise Fail(f"{label} unreadable: {exc}")


def read_json_optional(ad, name):
    """dict/list or None when absent. Present-but-unparseable is FAIL."""
    path = os.path.join(ad, name)
    if not os.path.isfile(path):
        return None
    try:
        return sl.read_json(path)
    except (OSError, ValueError) as exc:
        raise Fail(f"{name} is not JSON: {exc}")


def canonical_path(entry, label):
    """Repo-relative, normalised. Absolute, escaping or empty entries FAIL."""
    if not isinstance(entry, str) or not entry.strip():
        raise Fail(f"{label} entry is not a non-empty string")
    raw = entry.strip().replace("\\", "/")
    if raw.startswith("/"):
        raise Fail(f"{label} entry must be repo-relative: {entry}")
    e = re.sub(r"/{2,}", "/", raw)
    while e.startswith("./"):
        e = re.sub(r"/{2,}", "/", e[2:])
    if ".." in e.split("/"):
        raise Fail(f"{label} entry escapes the repo: {entry}")
    if e in ("", "."):
        raise Fail(f"{label} entry is empty after normalisation: {entry}")
    return e


def git(root, *args):
    try:
        r = subprocess.run(["git", "-C", root, *args], capture_output=True, encoding="utf-8")
    except OSError as exc:
        raise Fail(f"git unavailable: {exc}")
    return r


# --- brief -------------------------------------------------------------------
def brief_kind(text):
    m = KIND_LINE_RE.search(text) or KIND_HEADING_RE.search(text)
    if not m:
        return "feature", "no Kind line; default feature"
    kind = m.group(1).lower()
    if kind not in rp.TASK_CLASSES:
        raise Fail(f"task class {kind!r} not in {','.join(rp.TASK_CLASSES)}")
    return kind, m.group(0).strip().splitlines()[0]


def brief_paths(text, policy):
    """Repo-relative paths the brief names: tokens with a slash (no URL
    scheme, trailing sentence punctuation stripped) plus bare lockfile and
    manifest names from the policy ("bump bun.lock"), de-duplicated, sorted."""
    text = re.sub(r"\w+://\S+", " ", text)
    out = set()
    for tok in PATH_TOKEN_RE.findall(text):
        tok = tok.rstrip(".,;:)")
        if tok.endswith("/") or "." in tok.rsplit("/", 1)[-1] or tok.count("/") >= 2:
            if "/" in tok and not tok.startswith("/"):
                out.add(tok)
    rev = policy["reversibility"]
    names = {n for key in ("lockfiles", "manifests") for n in rev[key]["paths"] if "/" not in n and "*" not in n}
    for tok in re.findall(r"[A-Za-z0-9_.-]+", text):
        if tok.rstrip(".,;:)") in names:
            out.add(tok.rstrip(".,;:)"))
    return sorted(out)


# --- CODEOWNERS --------------------------------------------------------------
def codeowners_text(root, base):
    """(text, sha) of the first CODEOWNERS candidate at base (git show) or in
    the working tree when base is None; (None, None) when absent."""
    for rel in rp.CODEOWNERS_CANDIDATES:
        if base:
            r = git(root, "show", f"{base}:{rel}")
            if r.returncode == 0:
                return r.stdout, rp.sha256_text(r.stdout)
        else:
            path = os.path.join(root, rel)
            if os.path.isfile(path):
                text = read_text(path, rel)
                return text, rp.sha256_text(text)
    return None, None


# --- stage: intake -----------------------------------------------------------
def signals_intake(ctx):
    kind, evidence = brief_kind(ctx["brief"])
    pts = ctx["policy"]["points"]
    signals = [{"id": "task-class", "value": kind, "points": pts["taskClass"][kind], "floor": None,
                "source": "mechanical", "evidence": evidence}]
    paths = brief_paths(ctx["brief"], ctx["policy"])
    signals.append({"id": "brief-paths", "value": paths, "points": 0, "floor": None,
                    "source": "mechanical", "evidence": f"{len(paths)} path(s) named in the brief"})
    return signals, paths


# --- assembly ----------------------------------------------------------------
def floors_to_signals(floors):
    return [{"id": f["id"], "value": f["paths"], "points": 0, "floor": f["floor"],
             "source": "mechanical", "evidence": f["reason"]} for f in floors]


def read_judgment(ad):
    doc = read_json_optional(ad, "risk-judgment.json")
    if doc is None:
        return {"tier": None, "rationale": None, "unknowns": []}
    if not isinstance(doc, dict) or doc.get("schema") != rp.SCHEMA_JUDGMENT:
        raise Fail(f"risk-judgment.json schema must be {rp.SCHEMA_JUDGMENT}")
    if doc.get("tier") not in rp.TIERS:
        raise Fail("risk-judgment.json tier out of enum")
    if not isinstance(doc.get("rationale"), str) or not doc["rationale"].strip():
        raise Fail("risk-judgment.json rationale is required")
    unknowns = doc.get("unknowns", [])
    if not isinstance(unknowns, list) or not all(isinstance(u, str) for u in unknowns):
        raise Fail("risk-judgment.json unknowns must be a list of strings")
    return {"tier": doc["tier"], "rationale": doc["rationale"].strip(), "unknowns": unknowns}


def read_handoff(path):
    if not path:
        return None
    try:
        doc = sl.read_json(path)
    except (OSError, ValueError) as exc:
        raise Fail(f"handoff unreadable: {exc}")
    if not isinstance(doc, dict) or doc.get("schema") != rp.SCHEMA_ESCALATION:
        raise Fail(f"handoff schema must be {rp.SCHEMA_ESCALATION}")
    if doc.get("toTier") not in rp.TIERS or doc.get("stage") not in rp.STAGES:
        raise Fail("handoff toTier/stage out of enum")
    for key in ("fromLane", "runId"):
        if not isinstance(doc.get(key), str) or not doc[key].strip():
            raise Fail(f"handoff {key} is required")
    return doc


def prior_tier(ad, stage, handoff):
    """The previous stage's tier in this run, else the handoff's target tier."""
    previous = {"plan": "risk-intake.json", "impl": "risk-plan.json"}.get(stage)
    if previous:
        doc = read_json_optional(ad, previous)
        if doc is not None:
            if not isinstance(doc, dict) or doc.get("tier") not in rp.TIERS:
                raise Fail(f"{previous} tier out of enum")
            return {"tier": doc["tier"], "stage": doc.get("stage")}
    if handoff:
        return {"tier": handoff["toTier"], "stage": handoff["stage"]}
    return {"tier": None, "stage": None}


def lane_tier_of(handoff):
    """The lower lane's tier from its name (sdlc-green -> green), else None."""
    if not handoff:
        return None
    for tier in rp.TIERS:
        if tier in handoff["fromLane"]:
            return tier
    return None


def build(ctx, signals, paths, extra_inputs):
    policy = ctx["policy"]
    floors = rp.path_floors(policy, paths, ctx["repo"])
    owners, co_floors = [], []
    if ctx["codeowners_rules"] is not None:
        owners, co_floors = rp.codeowner_floors(policy, ctx["codeowners_rules"], paths)
    floors = sorted(floors + co_floors, key=lambda f: f["id"])
    score = sum(s["points"] for s in signals)
    mech_tier = rp.tier_max(rp.tier_from_score(score, policy["thresholds"]), *[f["floor"] for f in floors])
    agent = ctx["agent"]
    prior = ctx["prior"]
    tier = rp.tier_max(mech_tier, agent["tier"], prior["tier"], ctx["override"])
    lane = ctx["lane_tier"]
    escalate = bool(lane and rp.tier_gt(tier, lane))
    handoff = ctx["handoff"]
    inputs = {"briefSha256": ctx["brief_sha"], "allowlistSha256": None, "diffSha256": None,
              "codeownersSha256": ctx["codeowners_sha"], "profileSha256": ctx["profile_sha"],
              "policySha256": ctx["policy_sha"], "overlaySha256": ctx["overlay_sha"],
              "baseCommit": ctx["base"], "head": None}
    inputs.update(extra_inputs)
    return {
        "schema": rp.SCHEMA_SCORE,
        "scoringVersion": SCORING_VERSION,
        "policyVersion": policy["version"],
        "overlayVersion": policy["overlayVersion"],
        "stage": ctx["stage"],
        "laneTier": lane,
        "mechanical": {"tier": mech_tier, "score": score, "signals": signals + floors_to_signals(floors)},
        "agent": agent,
        "prior": prior,
        "override": ctx["override"],
        "tier": tier,
        "floors": [{"id": f["id"], "reason": f["reason"], "paths": f["paths"]} for f in floors],
        "codeowners": {"present": ctx["codeowners_rules"] is not None, "ownersTouched": owners,
                       "floorsApplied": [f["id"] for f in co_floors]},
        "inputs": inputs,
        "escalation": {"required": escalate, "toTier": tier if escalate else None},
        "escalated": handoff is not None,
        "escalatedFrom": lane_tier_of(handoff),
        "handoffRunId": handoff["runId"] if handoff else None,
    }


def trajectory_line(doc):
    return {"at": sl.utc_now(), "stage": doc["stage"], "tier": doc["tier"], "score": doc["mechanical"]["score"],
            "floors": [f["id"] for f in doc["floors"]], "mechanical": doc["mechanical"]["tier"],
            "agent": doc["agent"]["tier"], "prior": doc["prior"]["tier"], "override": doc["override"],
            "laneTier": doc["laneTier"], "escalate": doc["escalation"]["required"],
            "escalatedFrom": doc["escalatedFrom"], "handoffRunId": doc["handoffRunId"]}


def context(args):
    ad = os.path.abspath(args.artifacts)
    if not os.path.isdir(ad):
        raise Fail(f"artifacts dir missing: {args.artifacts}")
    params = read_json_optional(ad, "params.json")
    if params is None:
        raise Fail("params.json missing")
    if not isinstance(params, dict):
        raise Fail("params.json is not an object")
    try:
        profile = sl.read_json(args.profile)
    except (OSError, ValueError) as exc:
        raise Fail(f"profile unreadable: {exc}")
    if not isinstance(profile, dict):
        raise Fail("profile is not an object")
    try:
        policy = rp.load_policy(args.policy, profile, args.overlay)
    except rp.RiskPolicyError as exc:
        raise Fail(f"policy: {exc}")
    brief_path = args.brief or params.get("spec")
    if not isinstance(brief_path, str) or not os.path.isfile(brief_path):
        raise Fail("brief missing: params.json has no readable spec path and no --brief given")
    brief = read_text(brief_path, "brief")
    root = os.path.abspath(args.repo_root)
    if not os.path.isdir(root):
        raise Fail(f"repo root missing: {args.repo_root}")
    base = args.base
    if base is None and args.stage == "impl":
        head_file = os.path.join(ad, "bootstrap-head.txt")
        if os.path.isfile(head_file):
            base = read_text(head_file, "bootstrap-head.txt").strip() or None
    if base is not None and git(root, "cat-file", "-e", f"{base}^{{commit}}").returncode != 0:
        raise Fail(f"base commit not found: {base}")
    co_text, co_sha = codeowners_text(root, base)
    rules = None
    if co_text is not None:
        try:
            rules = rp.parse_codeowners(co_text)
        except rp.RiskPolicyError as exc:
            raise Fail(str(exc))
    handoff = read_handoff(args.handoff)
    caps = profile.get("capabilities") if isinstance(profile.get("capabilities"), dict) else {}
    repo = params.get("repo") if isinstance(params.get("repo"), str) and params.get("repo") else None
    repo = repo or caps.get("defaultRepo") or os.path.basename(root.rstrip(os.sep))
    return {
        "ad": ad, "stage": args.stage, "params": params, "profile": profile, "policy": policy,
        "brief": brief, "brief_sha": rp.sha256_text(brief), "root": root, "base": base, "repo": repo,
        "web_repo": caps.get("webRepo"),
        "profile_sha": rp.sha256_file(args.profile), "policy_sha": rp.sha256_file(args.policy or rp.DEFAULT_POLICY_PATH),
        "overlay_sha": rp.sha256_file(args.overlay) if args.overlay else None,
        "codeowners_rules": rules, "codeowners_sha": co_sha,
        "handoff": handoff, "override": args.override_tier, "lane_tier": args.lane_tier,
        "agent": {"tier": None, "rationale": None, "unknowns": []} if args.stage == "intake" else read_judgment(ad),
        "prior": prior_tier(ad, args.stage, handoff),
    }


STAGE_FUNCS = {"intake": lambda ctx: signals_intake(ctx) + ({},)}


def score(args):
    ctx = context(args)
    signals, paths, extra_inputs = STAGE_FUNCS[args.stage](ctx)
    return ctx, build(ctx, signals, paths, extra_inputs)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], epilog=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=rp.STAGES)
    ap.add_argument("--artifacts", required=True)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--repo-root", required=True)
    ap.add_argument("--base")
    ap.add_argument("--policy")
    ap.add_argument("--overlay")
    ap.add_argument("--handoff")
    ap.add_argument("--brief")
    ap.add_argument("--override-tier", choices=("yellow", "red"))
    ap.add_argument("--lane-tier", choices=rp.TIERS)
    args = ap.parse_args(argv)
    try:
        ctx, doc = score(args)
        sl.write_json_atomic(os.path.join(ctx["ad"], f"risk-{args.stage}.json"), doc)
        sl.append_jsonl(os.path.join(ctx["ad"], "risk-trajectory.jsonl"), trajectory_line(doc))
    except Fail as exc:
        print(f"RISK=FAIL {exc}")
        return 1
    except OSError as exc:
        print(f"RISK=FAIL {exc}")
        return 1
    floors = ",".join(f["id"] for f in doc["floors"]) or "none"
    print(f"RISK_TIER={doc['tier']} stage={doc['stage']} score={doc['mechanical']['score']} floors={floors}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Note `STAGE_FUNCS` is a dict so Task 5 adds `plan` and `impl` without touching `score()`. Each stage function returns `(signals, paths, extra_inputs)`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `$UVT setup.tests.test_risk_score -v`
Expected: all `Intake` tests PASS. If `test_brief_paths_ignore_urls_and_trailing_punctuation` fails, print `rs.brief_paths(...)` for the spec text and adjust `PATH_TOKEN_RE`'s look-behind; the contract is exactly `["apps/api/src/billing/", "libs/util/str.ts"]`.

- [ ] **Step 5: Commit**

```bash
git add setup/risk-score.py setup/tests/test_risk_score.py
git commit -m "feat(risk): risk-score.py, the intake stage

Scores the brief: task class, named paths, CODEOWNERS at the base commit,
profile protected areas; tier = max(mechanical, agent, prior, override);
fails closed with RISK=FAIL and writes nothing on failure.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Plan and impl stages

**Files:**
- Modify: `setup/risk-score.py`
- Test: `setup/tests/test_risk_score.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `setup/tests/test_risk_score.py` (before `if __name__ == "__main__":`):

```python
def gathered(name, file, callers):
    return {"name": name, "file": file, "d1_callers": callers, "risk": "LOW",
            "query_status": "GATHERED", "query_repo": "api", "query_target": name}


class Plan(Base):
    def baseline(self, files=None, impact=True):
        self.write(self.ad, "files-allowlist.json", files or ["apps/api/src/notes/notes.service.ts",
                                                              "apps/api/src/notes/notes.service.spec.ts"])
        if impact:
            self.write(self.ad, "impact.json", {"status": "GATHERED", "symbols": [
                gathered("NotesService.list", "apps/api/src/notes/notes.service.ts", ["a", "b"])]})
        self.write(self.ad, "triage.json", {"size": "S", "reasons": [], "hot_path_hits": [], "unknowns": []})

    def test_small_feature_is_green_and_records_allowlist_digest(self):
        self.baseline()
        last = self.assert_tier(self.run_cli("plan"), "green", "plan")
        d = self.doc("plan")
        self.assertEqual(d["inputs"]["allowlistSha256"],
                         rp.sha256_bytes(rp.canonical_bytes(json.loads((self.ad / "files-allowlist.json").read_text()))))
        ids = {s["id"]: s for s in d["mechanical"]["signals"]}
        self.assertEqual((ids["files"]["value"], ids["files"]["points"]), (1, 0))
        self.assertEqual((ids["test-files"]["value"], ids["test-files"]["points"]), (1, 0))
        self.assertEqual((ids["d1-callers"]["value"], ids["d1-callers"]["points"]), (2, 0))
        self.assertEqual((ids["impact"]["value"], ids["impact"]["points"]), ("GATHERED", 0))
        self.assertEqual((ids["triage"]["value"], ids["triage"]["points"]), ("S", 0))
        self.assertEqual(ids["coverage"]["value"], "no probes")
        self.assertTrue(last.endswith("floors=none"))

    def test_points_push_a_big_plan_to_yellow(self):
        files = [f"apps/api/src/notes/f{i}.ts" for i in range(7)]
        self.baseline(files=files)
        self.assert_tier(self.run_cli("plan"), "yellow", "plan")
        d = self.doc("plan")
        pts = POLICY["points"]
        expected = pts["taskClass"]["feature"] + pts["filesOverMax"] + pts["perExtraFile"] * (7 - POLICY["sizeThresholds"]["max_files"])
        self.assertEqual(d["mechanical"]["score"], expected)
        self.assertEqual(d["floors"], [])

    def test_floors_dominate_points(self):
        self.baseline(files=["docs/auth/README.md"])
        self.write_spec("# T\n\nKind: docs\n")
        self.assert_tier(self.run_cli("plan"), "red", "plan")
        d = self.doc("plan")
        self.assertEqual(d["mechanical"]["score"], 0)
        self.assertEqual([f["id"] for f in d["floors"]], ["sensitive-domain:auth"])

    def test_lockfile_is_yellow_migration_red_public_contract_yellow_factory_red(self):
        for files, tier, fid in ((["bun.lock"], "yellow", "lockfile"),
                                 (["libs/data-access/src/lib/rds/migrations/0007.ts"], "red", "migration"),
                                 (["app/services/api-client.d.ts"], "yellow", "public-contract"),
                                 ([".github/workflows/ci.yml"], "red", "factory-control")):
            self.baseline(files=files)
            self.write_spec("# T\n\nKind: chore\n")
            self.assert_tier(self.run_cli("plan"), tier, "plan")
            self.assertIn(fid, [f["id"] for f in self.doc("plan")["floors"]])

    def test_profile_protected_area_with_repo_prefix(self):
        self.baseline(files=["apps/integration-service/handler.ts"])
        self.assert_tier(self.run_cli("plan"), "red", "plan")
        self.assertEqual([f["id"] for f in self.doc("plan")["floors"]], ["protected:integration"])

    def test_web_allowlist_is_tagged_with_the_web_repo(self):
        self.baseline()
        self.write(self.ad, "web-files-allowlist.json", ["app/services/api-client.d.ts"])
        self.assert_tier(self.run_cli("plan"), "yellow", "plan")
        d = self.doc("plan")
        self.assertEqual(d["floors"][0]["paths"], ["web-app/app/services/api-client.d.ts"])

    def test_missing_allowlist_fails_closed(self):
        self.assert_fail(self.run_cli("plan"), "files-allowlist.json")

    def test_bad_allowlist_entries_fail_closed(self):
        for bad in ([], ["/abs/path.ts"], ["../escape.ts"], [""], "not a list"):
            self.write(self.ad, "files-allowlist.json", bad)
            self.assert_fail(self.run_cli("plan"), "files-allowlist.json")

    def test_impact_missing_or_unavailable_adds_unknown_points(self):
        self.baseline(impact=False)
        self.assert_tier(self.run_cli("plan"), "yellow", "plan")
        ids = {s["id"]: s for s in self.doc("plan")["mechanical"]["signals"]}
        self.assertEqual((ids["impact"]["value"], ids["impact"]["points"]), ("missing", POLICY["points"]["impactMissing"]))
        self.write(self.ad, "impact.json", {"status": "UNAVAILABLE", "symbols": []})
        self.assert_tier(self.run_cli("plan"), "yellow", "plan")
        ids = {s["id"]: s for s in self.doc("plan")["mechanical"]["signals"]}
        self.assertEqual(ids["impact"]["points"], POLICY["points"]["impactUnavailable"])
        self.write(self.ad, "impact.json", {"status": "NOPE"})
        self.assert_fail(self.run_cli("plan"), "impact.json")

    def test_callers_and_chain_links_over_max_add_points(self):
        self.baseline()
        many = [f"c{i}" for i in range(POLICY["sizeThresholds"]["max_d1_callers"] + 1)]
        self.write(self.ad, "impact.json", {"status": "GATHERED", "symbols": [gathered("X", "apps/api/src/notes/x.ts", many)]})
        self.write(self.ad, "causal-chain.json", {"links": [{"n": i} for i in range(POLICY["sizeThresholds"]["max_chain_links"] + 1)]})
        self.assert_tier(self.run_cli("plan"), "yellow", "plan")
        ids = {s["id"]: s for s in self.doc("plan")["mechanical"]["signals"]}
        self.assertEqual(ids["d1-callers"]["points"], POLICY["points"]["callersOverMax"])
        self.assertEqual(ids["chain-links"]["points"], POLICY["points"]["chainLinksOverMax"])

    def test_triage_L_adds_points_and_bad_triage_fails(self):
        self.baseline()
        self.write(self.ad, "triage.json", {"size": "L"})
        self.assert_tier(self.run_cli("plan"), "yellow", "plan")
        self.write(self.ad, "triage.json", {"size": "XL"})
        self.assert_fail(self.run_cli("plan"), "triage.json")

    def test_agent_judgment_joins_the_max_and_malformed_fails(self):
        self.baseline()
        self.write(self.ad, "risk-judgment.json", {"schema": rp.SCHEMA_JUDGMENT, "tier": "red",
                                                   "rationale": "touches the billing webhook path indirectly", "unknowns": ["retry semantics"]})
        self.assert_tier(self.run_cli("plan"), "red", "plan")
        d = self.doc("plan")
        self.assertEqual(d["agent"]["tier"], "red")
        self.assertEqual(d["mechanical"]["tier"], "green")  # disagreement recorded, not resolved
        self.write(self.ad, "risk-judgment.json", {"schema": rp.SCHEMA_JUDGMENT, "tier": "red"})
        self.assert_fail(self.run_cli("plan"), "risk-judgment.json")

    def test_prior_stage_tier_never_lowers(self):
        self.write_spec("# T\n\nKind: docs\n\nEdit apps/api/src/auth/a.md.\n")
        self.assert_tier(self.run_cli("intake"), "red", "intake")
        self.write_spec("# T\n\nKind: docs\n")
        self.baseline(files=["docs/a.md"])
        self.assert_tier(self.run_cli("plan"), "red", "plan")
        d = self.doc("plan")
        self.assertEqual(d["prior"], {"tier": "red", "stage": "intake"})
        self.assertEqual(d["mechanical"]["tier"], "green")

    def test_handoff_sets_prior_and_escalated_from(self):
        self.baseline()
        handoff = self.tmp / "escalation.json"
        handoff.write_text(json.dumps({"schema": rp.SCHEMA_ESCALATION, "fromLane": "sdlc-green", "toTier": "yellow",
                                       "stage": "plan", "runId": "run-123"}), encoding="utf-8")
        self.assert_tier(self.run_cli("plan", "--handoff", str(handoff)), "yellow", "plan")
        d = self.doc("plan")
        self.assertEqual(d["prior"], {"tier": "yellow", "stage": "plan"})
        self.assertEqual((d["escalated"], d["escalatedFrom"], d["handoffRunId"]), (True, "green", "run-123"))
        self.assertEqual(self.trajectory()[-1]["handoffRunId"], "run-123")
        handoff.write_text(json.dumps({"schema": "nope"}), encoding="utf-8")
        self.assert_fail(self.run_cli("plan", "--handoff", str(handoff)), "handoff")


class Impl(Base):
    def setUp(self):
        super().setUp()
        (self.ad / "bootstrap-head.txt").write_text(self.base + "\n", encoding="utf-8")
        self.write(self.ad, "impact.json", {"status": "GATHERED", "symbols": [
            gathered("NotesService.list", "apps/api/src/notes/notes.service.ts", ["a"])]})

    def test_diff_since_bootstrap_head_is_the_footprint(self):
        self.commit_file("apps/api/src/notes/notes.service.ts", "x")
        self.commit_file("apps/api/src/notes/notes.service.spec.ts", "t")
        self.assert_tier(self.run_cli("impl"), "green", "impl")
        d = self.doc("impl")
        ids = {s["id"]: s for s in d["mechanical"]["signals"]}
        self.assertEqual(ids["files"]["value"], 1)
        self.assertEqual(ids["test-files"]["value"], 1)
        diff = git(self.root, "diff", "--name-status", f"{self.base}..HEAD")
        self.assertEqual(d["inputs"]["diffSha256"], rp.sha256_text(diff + "\n"))
        self.assertEqual(d["inputs"]["head"], git(self.root, "rev-parse", "HEAD"))
        self.assertEqual(d["inputs"]["baseCommit"], self.base)
        self.assertIsNone(d["inputs"]["allowlistSha256"])

    def test_changed_diff_changes_the_digest_and_can_raise_the_tier(self):
        self.commit_file("docs/a.md", "x")
        self.assert_tier(self.run_cli("impl"), "green", "impl")
        first = self.doc("impl")["inputs"]["diffSha256"]
        self.commit_file("apps/api/src/auth/guard.ts", "y")
        self.assert_tier(self.run_cli("impl"), "red", "impl")
        d = self.doc("impl")
        self.assertNotEqual(first, d["inputs"]["diffSha256"])
        self.assertEqual([f["id"] for f in d["floors"]], ["sensitive-domain:auth"])

    def test_deleted_and_renamed_paths_count(self):
        self.commit_file("apps/api/src/billing/old.ts", "x")
        base = git(self.root, "rev-parse", "HEAD")
        (self.ad / "bootstrap-head.txt").write_text(base + "\n", encoding="utf-8")
        os.remove(self.root / "apps/api/src/billing/old.ts")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "rm")
        self.assert_tier(self.run_cli("impl"), "red", "impl")
        self.assertEqual(self.doc("impl")["floors"][0]["paths"], ["apps/api/src/billing/old.ts"])

    def test_missing_base_fails_closed(self):
        os.remove(self.ad / "bootstrap-head.txt")
        self.assert_fail(self.run_cli("impl"), "base")

    def test_unreadable_diff_fails_closed(self):
        self.assert_fail(self.run_cli("impl", "--base", "0" * 40), "base commit")
        self.assert_fail(self.run_cli("impl", "--repo-root", str(self.tmp / "not-a-repo")), "repo root")

    def test_prior_from_plan_and_triage_post(self):
        self.write(self.ad, "risk-plan.json", {"schema": rp.SCHEMA_SCORE, "stage": "plan", "tier": "yellow"})
        self.write(self.ad, "triage-post.json", {"size": "M"})
        self.commit_file("docs/a.md", "x")
        self.assert_tier(self.run_cli("impl"), "yellow", "impl")
        d = self.doc("impl")
        self.assertEqual(d["prior"], {"tier": "yellow", "stage": "plan"})
        ids = {s["id"]: s for s in d["mechanical"]["signals"]}
        self.assertEqual((ids["triage"]["value"], ids["triage"]["points"]), ("M", POLICY["points"]["triage"]["M"]))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$UVT setup.tests.test_risk_score.Plan setup.tests.test_risk_score.Impl -v`
Expected: `KeyError: 'plan'` from `STAGE_FUNCS` (the stage is not registered yet).

- [ ] **Step 3: Add the plan and impl stage functions to `setup/risk-score.py`**

Insert after `signals_intake` (before `# --- assembly`):

```python
# --- shared plan/impl signals ------------------------------------------------
def read_allowlist(ad, name, required):
    doc = read_json_optional(ad, name)
    if doc is None:
        if required:
            raise Fail(f"{name} missing")
        return None
    if not isinstance(doc, list) or (required and not doc):
        raise Fail(f"{name} is not a non-empty list")
    try:
        return [canonical_path(e, name) for e in doc]
    except Fail as exc:
        raise Fail(f"{name}: {exc}")


def read_triage(ad, name):
    doc = read_json_optional(ad, name)
    if doc is None:
        return None
    size = doc.get("size") if isinstance(doc, dict) else None
    if size not in ("S", "M", "L"):
        raise Fail(f"{name} size not in S|M|L")
    return size


def read_impact(ad):
    """(status, d1_callers). Absent -> ("missing", 0). Malformed -> FAIL."""
    doc = read_json_optional(ad, "impact.json")
    if doc is None:
        return "missing", 0
    status = doc.get("status") if isinstance(doc, dict) else None
    if status not in ("GATHERED", "UNAVAILABLE", "SKIPPED"):
        raise Fail("impact.json status not in GATHERED|UNAVAILABLE|SKIPPED")
    syms = doc.get("symbols")
    if not isinstance(syms, list):
        raise Fail("impact.json symbols is not a list")
    callers = 0
    for i, s in enumerate(syms):
        if not isinstance(s, dict) or not isinstance(s.get("d1_callers"), list):
            raise Fail(f"impact.json symbols[{i}] needs a d1_callers list")
        callers += len(s["d1_callers"])
    return status, callers


def read_chain_links(ad):
    doc = read_json_optional(ad, "causal-chain.json")
    if doc is None:
        return None
    links = doc.get("links") if isinstance(doc, dict) else None
    if not isinstance(links, list):
        raise Fail("causal-chain.json links is not a list")
    return len(links)


def size_signals(ctx, paths, triage_name):
    pol = ctx["policy"]
    pts, sizes = pol["points"], pol["sizeThresholds"]
    code = [p for p in paths if not TEST_RE.search(p)]
    tests = [p for p in paths if TEST_RE.search(p)]
    kind, evidence = brief_kind(ctx["brief"])
    signals = [{"id": "task-class", "value": kind, "points": pts["taskClass"][kind], "floor": None,
                "source": "mechanical", "evidence": evidence}]
    over = len(code) - sizes["max_files"]
    signals.append({"id": "files", "value": len(code), "points": pts["filesOverMax"] + pts["perExtraFile"] * over if over > 0 else 0,
                    "floor": None, "source": "mechanical", "evidence": f"{len(code)}/{sizes['max_files']} non-test files"})
    signals.append({"id": "test-files", "value": len(tests), "points": pts["testFilesOverMax"] if len(tests) > sizes["max_test_files"] else 0,
                    "floor": None, "source": "mechanical", "evidence": f"{len(tests)}/{sizes['max_test_files']} test files"})
    status, callers = read_impact(ctx["ad"])
    impact_points = {"missing": pts["impactMissing"], "UNAVAILABLE": pts["impactUnavailable"]}.get(status, 0)
    signals.append({"id": "impact", "value": status, "points": impact_points, "floor": None,
                    "source": "mechanical", "evidence": "impact.json status"})
    signals.append({"id": "d1-callers", "value": callers, "points": pts["callersOverMax"] if callers > sizes["max_d1_callers"] else 0,
                    "floor": None, "source": "mechanical", "evidence": f"{callers}/{sizes['max_d1_callers']} first-degree callers"})
    links = read_chain_links(ctx["ad"])
    if links is not None:
        signals.append({"id": "chain-links", "value": links, "points": pts["chainLinksOverMax"] if links > sizes["max_chain_links"] else 0,
                        "floor": None, "source": "mechanical", "evidence": f"{links}/{sizes['max_chain_links']} causal links"})
    size = read_triage(ctx["ad"], triage_name)
    if size is not None:
        signals.append({"id": "triage", "value": size, "points": pts["triage"][size], "floor": None,
                        "source": "mechanical", "evidence": f"{triage_name} size"})
    evidence = ctx["profile"].get("evidence") if isinstance(ctx["profile"].get("evidence"), dict) else None
    probes = (evidence or {}).get("behavioral") or []
    if not probes:
        signals.append({"id": "coverage", "value": "no probes", "points": 0, "floor": None,
                        "source": "mechanical", "evidence": "profile has no evidence.behavioral probes (Slice 2)"})
    else:
        covered = [p for p in code if any(rp.match_any(pr.get("covers") or [], p, ctx["repo"]) for pr in probes)]
        uncovered = sorted(set(code) - set(covered))
        signals.append({"id": "coverage", "value": uncovered, "points": pts["unknownCoverage"] if uncovered else 0,
                        "floor": None, "source": "mechanical", "evidence": f"{len(uncovered)} path(s) no probe covers"})
    return signals


# --- stage: plan -------------------------------------------------------------
def signals_plan(ctx):
    allow = read_allowlist(ctx["ad"], "files-allowlist.json", required=True)
    raw = sl.read_json(os.path.join(ctx["ad"], "files-allowlist.json"))
    paths = list(allow)
    web = read_allowlist(ctx["ad"], "web-files-allowlist.json", required=False)
    if web and ctx["web_repo"]:
        paths += [f"{ctx['web_repo']}/{p}" for p in web]
    signals = size_signals(ctx, paths, "triage.json")
    return signals, paths, {"allowlistSha256": rp.sha256_bytes(rp.canonical_bytes(raw))}


# --- stage: impl -------------------------------------------------------------
def signals_impl(ctx):
    if not ctx["base"]:
        raise Fail("impl needs a base commit: --base or bootstrap-head.txt")
    r = git(ctx["root"], "diff", "--name-status", f"{ctx['base']}..HEAD")
    if r.returncode != 0:
        raise Fail(f"git diff failed: {r.stderr.strip()}")
    diff_text = r.stdout if r.stdout.endswith("\n") else r.stdout + "\n"
    paths = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        # R100\told\tnew and C\told\tnew list both sides; every other status lists one path
        for p in parts[1:]:
            paths.append(canonical_path(p, "diff"))
    paths = sorted(set(paths))
    head = git(ctx["root"], "rev-parse", "HEAD")
    if head.returncode != 0:
        raise Fail("git rev-parse HEAD failed")
    signals = size_signals(ctx, paths, "triage-post.json")
    return signals, paths, {"diffSha256": rp.sha256_text(diff_text), "head": head.stdout.strip()}
```

Then replace the `STAGE_FUNCS` line with:

```python
STAGE_FUNCS = {"intake": lambda ctx: signals_intake(ctx) + ({},), "plan": signals_plan, "impl": signals_impl}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `$UVT setup.tests.test_risk_score -v`
Expected: all PASS. Then the 3.9 check:

```bash
/usr/bin/python3 -m unittest setup.tests.test_risk_score setup.tests.test_risk_policy 2>&1 | tail -3
```
Expected: `OK` (the system interpreter runs the scorer the way a workflow node will).

- [ ] **Step 5: Commit**

```bash
git add setup/risk-score.py setup/tests/test_risk_score.py
git commit -m "feat(risk): plan and impl stages for risk-score.py

plan reads the allowlists, impact, triage, chain links and the agent
judgment; impl reads git diff --name-status from bootstrap-head.txt and
hashes it; both share the size signals and the coverage stub.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Profile schemas gain `risk`, Goodword seeded from `hot_paths`

**Files:**
- Modify: `profiles/project-profile.v1.schema.json`, `profiles/project-profile.v2.schema.json`
- Modify: `profiles/goodword/project.v1.json`
- Test: `setup/tests/test_risk_policy.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `setup/tests/test_risk_policy.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `$UVT setup.tests.test_risk_policy.GoodwordSeed -v`
Expected: `KeyError: 'risk'`.

- [ ] **Step 3: Add the `risk` sub-schema to both profile schemas**

Insert this property into `"properties"` of **both** `profiles/project-profile.v1.schema.json` and `profiles/project-profile.v2.schema.json` (after `"capabilities"`; do not add `risk` to `required`):

```json
    "risk": {
      "type": "object",
      "description": "Risk-tier inputs merged over setup/risk-policy.json. Floors here can only add; setup/risk_policy.py refuses anything that weakens a default floor.",
      "properties": {
        "protectedAreas": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "paths": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1},
              "floor": {"enum": ["green", "yellow", "red"]},
              "reason": {"type": "string", "minLength": 1}
            },
            "required": ["paths", "floor", "reason"],
            "additionalProperties": false
          }
        },
        "codeowners": {
          "type": "object",
          "properties": {
            "ownerFloors": {"type": "object", "additionalProperties": {"enum": ["green", "yellow", "red"]}},
            "defaultOwnedFloor": {"enum": ["green", "yellow", "red"]}
          },
          "additionalProperties": false
        },
        "sensitiveDomains": {
          "type": "object",
          "properties": {
            "extraPaths": {"type": "array", "items": {"type": "string", "minLength": 1}}
          },
          "additionalProperties": false
        },
        "publicContract": {
          "type": "object",
          "properties": {"paths": {"type": "array", "items": {"type": "string", "minLength": 1}}},
          "additionalProperties": false
        },
        "sideEffects": {
          "type": "object",
          "properties": {"paths": {"type": "array", "items": {"type": "string", "minLength": 1}}},
          "additionalProperties": false
        },
        "thresholds": {
          "type": "object",
          "properties": {"yellow": {"type": "integer", "minimum": 0}, "red": {"type": "integer", "minimum": 0}},
          "additionalProperties": false
        }
      },
      "additionalProperties": false
    }
```

- [ ] **Step 4: Seed `profiles/goodword/project.v1.json`**

Add a top-level `"risk"` key after `"capabilities"`:

```json
  "risk": {
    "protectedAreas": [
      {"paths": ["api/libs/data-access/src/lib/rds/migrations/", "api/libs/data-access/src/lib/rds/entities/",
                 "api/libs/data-access/src/lib/rds/baseline/"], "floor": "red", "reason": "schema"},
      {"paths": ["api/apps/api/src/auth/", "api/apps/api/src/oauth/"], "floor": "red", "reason": "auth"},
      {"paths": ["api/apps/api/src/billing/"], "floor": "red", "reason": "billing"},
      {"paths": ["api/apps/api/src/global-search/"], "floor": "red", "reason": "global-search"},
      {"paths": ["api/infra/"], "floor": "red", "reason": "infra"},
      {"paths": ["api/apps/integration-service/", "api/apps/bridge-service/"], "floor": "red", "reason": "integration"},
      {"paths": ["web-app/app/services/api-client.d.ts"], "floor": "yellow", "reason": "generated-api-client"}
    ],
    "codeowners": {"defaultOwnedFloor": "yellow", "ownerFloors": {}}
  }
```

- [ ] **Step 5: Run the tests and the profile checks that already cover this file**

```bash
$UVT setup.tests.test_risk_policy setup.tests.test_profile_bind setup.tests.test_repo_aware_params setup.tests.test_materialize_guidance setup.tests.test_generic_graph_admission -v 2>&1 | tail -5
bash setup/profile-preflight.sh --help >/dev/null 2>&1; grep -n "schema" setup/profile-preflight.sh | head -5
```
Expected: `OK` from the unit modules. If `profile-preflight.sh` validates the goodword profile against the v1 schema with a JSON-schema tool, run it the way `package.sh` does (search `package.sh` for `profile-preflight`) and confirm it still passes; the `risk` property is now declared, so a closed schema accepts it.

- [ ] **Step 6: Commit**

```bash
git add profiles/project-profile.v1.schema.json profiles/project-profile.v2.schema.json profiles/goodword/project.v1.json setup/tests/test_risk_policy.py
git commit -m "feat(risk): optional profile.risk block; seed Goodword from hot_paths

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Trace digest `risk` block

**Files:**
- Modify: `setup/trace-digest.py`
- Test: `setup/tests/test_trace_digest.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `setup/tests/test_trace_digest.py` (before `if __name__ == "__main__":`; keep the existing `write` helper):

```python
class Risk(Base):
    def row(self, stage, tier, score=0, floors=(), **extra):
        base = {"at": "2026-10-01T00:00:00Z", "stage": stage, "tier": tier, "score": score, "floors": list(floors),
                "mechanical": tier, "agent": None, "prior": None, "override": None, "laneTier": None,
                "escalate": False, "escalatedFrom": None, "handoffRunId": None}
        base.update(extra)
        return json.dumps(base)

    def test_absent_trajectory_is_null(self):
        self.assertIsNone(self.digest()["risk"])

    def test_trajectory_summarised_with_review_and_gates(self):
        write(self.ad, "risk-trajectory.jsonl", "\n".join([
            self.row("intake", "green", 15), self.row("plan", "yellow", 32, laneTier="green", escalate=True),
            self.row("plan", "yellow", 32, handoffRunId="run-1"), self.row("impl", "red", 40, ["sensitive-domain:auth"])]) + "\n")
        write(self.ad, "round.txt", "2\n")
        write(self.ad, "round-1/fixer-result.json", self.fixer(applied=(("a", "P1"), ("b", "P2"))))
        write(self.ad, "round-2/fixer-result.json", self.fixer(applied=(("c", "P2"),)))
        write(self.ad, "node-exit-gate.out", "EXIT_GATE=PASS\n")
        write(self.ad, "node-merge-gate.out", "MERGE_GATE=PASS\n")
        write(self.ad, "pr-url.txt", "https://example.invalid/pr/9\n")
        write(self.ad, "delivery.json", {"mode": "draft", "autoMerge": False, "prUrl": "https://example.invalid/pr/9"})
        r = self.digest()["risk"]
        self.assertEqual(r, {
            "intake": "green", "plan": "yellow", "impl": "red", "final": "red",
            "escalatedAt": "plan", "escalationRunId": "run-1",
            "floors": ["sensitive-domain:auth"],
            "reviewSeverities": {"P0": 0, "P1": 1, "P2": 2, "P3": 0},
            "fixerRounds": 2, "exitGate": True, "mergeGate": True,
            "delivery": {"mode": "draft", "autoMerge": False, "prUrl": "https://example.invalid/pr/9"},
        })

    def test_partial_trajectory_leaves_nulls(self):
        write(self.ad, "risk-trajectory.jsonl", self.row("intake", "yellow") + "\n")
        r = self.digest()["risk"]
        self.assertEqual((r["intake"], r["plan"], r["impl"], r["final"]), ("yellow", None, None, "yellow"))
        self.assertEqual((r["escalatedAt"], r["escalationRunId"], r["mergeGate"], r["exitGate"]), (None, None, None, None))
        self.assertEqual(r["delivery"], {"mode": None, "autoMerge": None, "prUrl": None})

    def test_corrupt_trajectory_or_delivery_is_fail(self):
        write(self.ad, "risk-trajectory.jsonl", "{not json\n")
        with self.assertRaises(td.Fail) as cm:
            self.digest()
        self.assertIn("risk-trajectory.jsonl", str(cm.exception))
        write(self.ad, "risk-trajectory.jsonl", self.row("intake", "green") + "\n")
        write(self.ad, "delivery.json", "{oops")
        with self.assertRaises(td.Fail):
            self.digest()

    def test_risk_block_has_no_absolute_paths_and_typed_line_unchanged(self):
        write(self.ad, "risk-trajectory.jsonl", self.row("intake", "green") + "\n")
        r = run([str(self.ad)])
        self.assertEqual(r.stdout.strip(), "TRACE_DIGEST=OK run=run-abc lane=unknown terminal=incomplete rounds=0 score_inputs=0")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `$UVT setup.tests.test_trace_digest.Risk -v`
Expected: `KeyError: 'risk'`.

- [ ] **Step 3: Add the `risk` block to `setup/trace-digest.py`**

Add to the module docstring, after the `deslop:` paragraph:

```
risk:  null unless risk-trajectory.jsonl exists (setup/risk-score.py). Per
       stage the LAST line's tier; final = the last line overall; escalatedAt =
       the first stage whose line carries escalate: true; escalationRunId =
       the handoffRunId of any line (the lower run of an escalation pair);
       floors = the last line's floor ids; reviewSeverities sums the fixer's
       applied findings by severity across rounds; exitGate / mergeGate are
       the pass flags of node-exit-gate.out / node-merge-gate.out (null when
       the node never ran); delivery is delivery.json {mode, autoMerge, prUrl}
       when present (the ship node writes it from Slice 3 on), else nulls.
```

Add this section function (after `_waivers`):

```python
RISK_STAGES = ("intake", "plan", "impl")


def _risk(ad, review, typed):
    path = os.path.join(ad, "risk-trajectory.jsonl")
    if not os.path.isfile(path):
        return None
    try:
        rows = sl.read_jsonl(path)
    except (OSError, ValueError) as exc:
        raise Fail(f"risk-trajectory.jsonl is not JSONL: {exc}")
    by_stage, last, escalated_at, run_id = {}, None, None, None
    for row in rows:
        if not isinstance(row, dict) or row.get("stage") not in RISK_STAGES or row.get("tier") not in ("green", "yellow", "red"):
            raise Fail("risk-trajectory.jsonl line without a valid stage and tier")
        by_stage[row["stage"]] = row
        last = row
        if escalated_at is None and row.get("escalate") is True:
            escalated_at = row["stage"]
        if run_id is None and isinstance(row.get("handoffRunId"), str) and row["handoffRunId"]:
            run_id = row["handoffRunId"]
    severities = {s: 0 for s in SEVERITIES}
    for r in review["per_round"]:
        for s in SEVERITIES:
            severities[s] += r["applied_by_severity"][s]
    delivery = _read_json(os.path.join(ad, "delivery.json"), "delivery.json")
    if delivery is not None and not isinstance(delivery, dict):
        raise Fail("delivery.json is not an object")
    delivery = delivery or {}
    nodes = typed["nodes"]
    return {
        "intake": (by_stage.get("intake") or {}).get("tier"),
        "plan": (by_stage.get("plan") or {}).get("tier"),
        "impl": (by_stage.get("impl") or {}).get("tier"),
        "final": last["tier"] if last else None,
        "escalatedAt": escalated_at,
        "escalationRunId": run_id,
        "floors": [f for f in (last.get("floors") or []) if isinstance(f, str)] if last else [],
        "reviewSeverities": severities,
        "fixerRounds": review["rounds"],
        "exitGate": (nodes.get("exit-gate") or {}).get("pass"),
        "mergeGate": (nodes.get("merge-gate") or {}).get("pass"),
        "delivery": {"mode": delivery.get("mode") if isinstance(delivery.get("mode"), str) else None,
                     "autoMerge": delivery.get("autoMerge") if isinstance(delivery.get("autoMerge"), bool) else None,
                     "prUrl": delivery.get("prUrl") if isinstance(delivery.get("prUrl"), str) else None},
    }
```

In `digest()`, add the key after `"rca": rca,`:

```python
        "risk": _risk(ad, review, typed),
```

- [ ] **Step 4: Run the full digest tests and the ingest consumer**

Run: `$UVT setup.tests.test_trace_digest setup.tests.test_skill_score -v 2>&1 | tail -4`
Expected: `OK`. The `Cli.test_shipped_file_carries_no_machine_paths` import whitelist still holds because no new import was added.

- [ ] **Step 5: Commit**

```bash
git add setup/trace-digest.py setup/tests/test_trace_digest.py
git commit -m "feat(risk): trace digest carries a risk block from risk-trajectory.jsonl

Per-stage tiers, final tier, escalation stage and run link, floors, review
severities, exit and merge gate flags, and delivery when delivery.json
exists. Absent facts stay null; the ingest pointer needs no new writer.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Package the new files and pin them in the packaging test

**Files:**
- Modify: `setup/package.sh` (payload list, near the `setup/lite-envelope.json` lines)
- Modify: `setup/tests/test_setup_scripts_are_packaged.py`

- [ ] **Step 1: Write the failing test**

Append to the `SetupScriptsArePackagedTest` class in `setup/tests/test_setup_scripts_are_packaged.py`:

```python
    def test_risk_scorer_is_shipped(self):
        # risk_policy.py is imported, not called, so the reference regex cannot
        # see it; pin it with the scorer and the policy file it reads.
        entries = manifest_entries()
        for name in ("risk-score.py", "risk_policy.py"):
            self.assertIn(name, entries, name)
        self.assertIn("  setup/risk-policy.json\n", MANIFEST.read_text(encoding="utf-8"))
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `$UVT setup.tests.test_setup_scripts_are_packaged.SetupScriptsArePackagedTest.test_risk_scorer_is_shipped -v`
Expected: FAIL, `'risk-score.py' not found in {...}`.

- [ ] **Step 3: Add the three files to the payload list in `setup/package.sh`**

Directly before the line `  setup/lite-envelope.json` add:

```
  # Risk-tiered lanes (Slice 1): the scorer, its pure helpers, and the default
  # policy. risk-policy.json is the successor of lite-envelope.json.
  setup/risk-score.py
  setup/risk_policy.py
  setup/risk-policy.json
```

- [ ] **Step 4: Run the packaging tests and a syntax check**

```bash
$UVT setup.tests.test_setup_scripts_are_packaged -v 2>&1 | tail -3
bash -n setup/package.sh && echo SYNTAX_OK
```
Expected: `OK` and `SYNTAX_OK`.

- [ ] **Step 5: Commit**

```bash
git add setup/package.sh setup/tests/test_setup_scripts_are_packaged.py
git commit -m "chore(package): ship risk-score.py, risk_policy.py and risk-policy.json

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: Spec fixtures from the verification section, full-suite comparison, ship

**Files:**
- Test: `setup/tests/test_risk_score.py` (append)

- [ ] **Step 1: Write the end-to-end fixture tests the spec's verification step 3 names**

Append to `setup/tests/test_risk_score.py`:

```python
class SpecFixtures(Base):
    """Verification step 3 of the design spec, as executable fixtures."""

    def test_docs_only_spec_is_green(self):
        self.write_spec("# Clarify the README\n\nKind: docs\n\nUpdate README.md and docs/setup.md.\n")
        self.assert_tier(self.run_cli("intake"), "green", "intake")

    def test_spec_touching_auth_is_red_with_the_named_floor(self):
        self.write_spec("# Session refresh\n\nKind: feature\n\nChange apps/api/src/auth/session.service.ts.\n")
        last = self.assert_tier(self.run_cli("intake"), "red", "intake")
        self.assertTrue(last.endswith("floors=sensitive-domain:auth"), last)

    def test_same_with_codeowners_mapping_security_populates_owners_touched(self):
        self.commit_file("CODEOWNERS", "/apps/api/src/auth/  @org/security\n")
        self.write_spec("# Session refresh\n\nKind: feature\n\nChange apps/api/src/auth/session.service.ts.\n")
        self.assert_tier(self.run_cli("intake", "--base", git(self.root, "rev-parse", "HEAD")), "red", "intake")
        d = self.doc("intake")
        self.assertEqual(d["codeowners"]["ownersTouched"], ["@org/security"])
        self.assertIn("codeowners:@org/security", [f["id"] for f in d["floors"]])

    def test_lockfile_bump_is_yellow(self):
        self.write_spec("# Bump deps\n\nKind: chore\n\nRegenerate bun.lock.\n")
        last = self.assert_tier(self.run_cli("intake"), "yellow", "intake")
        self.assertTrue(last.endswith("floors=lockfile"), last)
```

- [ ] **Step 2: Run them**

Run: `$UVT setup.tests.test_risk_score.SpecFixtures -v`
Expected: PASS without code changes (every behaviour is already covered by Tasks 4 and 5; this class pins the spec wording).

- [ ] **Step 3: Commit**

```bash
git add setup/risk-score.py setup/tests/test_risk_score.py
git commit -m "test(risk): the spec's scorer fixtures as tests

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

- [ ] **Step 4: Full suite, compared per module against a clean worktree**

The suite has ~144 known environmental failures on this machine (memory `archon-test-suite-baseline`). Compare per module:

```bash
cd /Users/eduardopicazo/orca/workspaces/Archon/sdlc-risk-tiers
uv run --offline --no-project --python 3.13.9 --with pyyaml==6.0.3 python -m unittest discover -s setup/tests 2>&1 | grep -E "^(FAIL|ERROR):" | sed -E 's/ \(.*//' | sort > /private/tmp/claude-501/-Users-eduardopicazo-Documents-Archon/e038b9b6-9b63-4abb-a191-526d77ee9494/scratchpad/after.txt
git stash list >/dev/null; git worktree add -q /private/tmp/claude-501/-Users-eduardopicazo-Documents-Archon/e038b9b6-9b63-4abb-a191-526d77ee9494/scratchpad/clean origin/main
cd /private/tmp/claude-501/-Users-eduardopicazo-Documents-Archon/e038b9b6-9b63-4abb-a191-526d77ee9494/scratchpad/clean
uv run --offline --no-project --python 3.13.9 --with pyyaml==6.0.3 python -m unittest discover -s setup/tests 2>&1 | grep -E "^(FAIL|ERROR):" | sed -E 's/ \(.*//' | sort > ../before.txt
cd /Users/eduardopicazo/orca/workspaces/Archon/sdlc-risk-tiers
git worktree remove --force /private/tmp/claude-501/-Users-eduardopicazo-Documents-Archon/e038b9b6-9b63-4abb-a191-526d77ee9494/scratchpad/clean
comm -13 /private/tmp/claude-501/-Users-eduardopicazo-Documents-Archon/e038b9b6-9b63-4abb-a191-526d77ee9494/scratchpad/before.txt /private/tmp/claude-501/-Users-eduardopicazo-Documents-Archon/e038b9b6-9b63-4abb-a191-526d77ee9494/scratchpad/after.txt
```
Expected: `comm` prints nothing (no failure that is new on this branch). Any line printed is a regression to fix before shipping.

- [ ] **Step 5: Package drift gates still pass**

```bash
bash setup/package.sh 2>&1 | grep -E "LITE_DRIFT|CODEX_DRIFT|LANE_DOCTRINE|PROFILE|FAIL" | head
```
Expected: `LITE_DRIFT=OK`, `CODEX_DRIFT=OK`, `LANE_DOCTRINE=OK` lines and no `FAIL` (Slice 1 touches no lane YAML, so drift cannot change; this proves the new payload entries resolve).

- [ ] **Step 6: Ship through no-mistakes**

```bash
git remote -v | grep -q no-mistakes || no-mistakes init
git push no-mistakes Pibomeister/sdlc-risk-tiers
no-mistakes status
```
Expected: no-mistakes runs review, test, lint and docs, then pushes to origin and opens the PR for Slice 1. Watch with `no-mistakes status` until it reports the PR URL. Never `git push origin`.

---

## Self-review against the spec (Slice 1)

- **`setup/risk-score.py` CLI and typed lines** → Tasks 4, 5. `--brief` and `--lane-tier` are additions, documented in the module docstring.
- **`setup/risk-policy.json` fields** (version, scoringVersion, thresholds, taskClassPoints → `points.taskClass`, sizeThresholds, sensitiveDomains, factoryControl, reversibility, publicContract, repro_command_allow) → Task 1. `points.*` names differ from the spec's `taskClassPoints` because the file groups every weight under `points` for the Slice 5 calibrator; the spec's semantics are kept.
- **`setup/risk_policy.py`** load/merge, floor invariants, anchored-prefix globs, CODEOWNERS parser (last match wins, gitignore globs, absent → `present: false`) → Tasks 1–3.
- **Inputs per stage** table → Task 4 (intake), Task 5 (plan, impl). `causal-chain.json` is the chain-links source and is optional on feature lanes.
- **Output contract** `archon.risk-score.v1` → Task 4 `build()`. Fields beyond the spec: `laneTier`, `override`, `escalation`, `handoffRunId`.
- **Rules**: max across sources, fail closed, agent disagreement recorded not resolved → Task 4 `build()` and `Plan.test_agent_judgment_joins_the_max_and_malformed_fails`.
- **Profile extension** `risk {protectedAreas, codeowners, sensitiveDomains.extraPaths, publicContract, sideEffects, thresholds}` → Task 6 (schemas v1/v2 now; v3 inherits in Slice 2). Goodword seeded from `hot_paths` → Task 6.
- **Ledger pointer**: `trace-digest.py` `risk` block with the spec's keys (`intake, plan, impl, final, escalatedAt, escalationRunId, reviewSeverities, fixerRounds, exitGate, mergeGate, delivery`) plus `floors` → Task 7. No new library writer. `DIGEST_VERSION` unchanged (additive key; `test_skill_score` builds version-1 digests).
- **Tests listed in the spec**: floors dominate (`Plan.test_floors_dominate_points`, `Intake.test_brief_naming_an_auth_path_is_red_by_floor`); sensitive domains without CODEOWNERS (`SpecFixtures`); owner floors + ownersTouched; malformed CODEOWNERS → FAIL; max rule and override never lowers; fail closed on missing allowlist / unreadable diff / bad profile; digest binding (same inputs → identical bytes, changed diff → changed sha); factory-control red, lockfile yellow, migration red, public contract yellow; unknown coverage stubbed as "no probes".
- **Placeholder scan**: no TBD/TODO; every code step is complete; `STAGE_FUNCS` is defined in Task 4 and extended in Task 5 with the exact replacement line.
- **Name consistency**: `rp.load_policy / load_defaults / assert_invariants / match_rule / match_any / sensitive_domain / path_floors / parse_codeowners / codeowners_owners / codeowner_floors / tier_max / tier_gt / tier_from_score / sha256_text / sha256_file / sha256_bytes / canonical_bytes` are used with the same signatures in Tasks 1–5 and 9. `signals_intake(ctx)` returns `(signals, paths)`; the lambda in `STAGE_FUNCS` appends `({},)`; `signals_plan/impl` return three values. `brief_paths(text, policy)` is defined and called with two arguments in Task 4.
