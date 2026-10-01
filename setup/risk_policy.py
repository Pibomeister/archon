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

Only the sensitiveDomains and factoryControl floors are invariant:
publicContract, sideEffects, codeowners and protectedAreas floors may be set
lower by a layer on purpose, since those sections encode judgment calls
rather than hard floors. Overlay-version monotonicity (rejecting an overlay
applied out of order) is enforced by the Slice 5 admit step, not here at
load time.

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
LAYER_KEYS = ("thresholds", "points", "protectedAreas", "sensitiveDomains", "factoryControl",
              "publicContract", "sideEffects", "codeowners", "autoMerge")


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
    tc = points.get("taskClass")
    if not isinstance(tc, dict) or set(tc) != set(TASK_CLASSES) or any(not _is_nonneg_int(v) for v in tc.values()):
        raise RiskPolicyError("risk-policy.json points.taskClass must map exactly the task classes "
                              "to non-negative integers")
    tr = points.get("triage")
    if not isinstance(tr, dict) or set(tr) != {"S", "M", "L"} or any(not _is_nonneg_int(v) for v in tr.values()):
        raise RiskPolicyError("risk-policy.json points.triage must map exactly S, M, L to non-negative integers")
    for key in _REQUIRED_POINTS:
        if key in ("taskClass", "triage"):
            continue
        if not _is_nonneg_int(points.get(key)):
            raise RiskPolicyError(f"risk-policy.json points.{key} must be a non-negative integer")
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
    for name, section in doc["reversibility"].items():
        if not isinstance(section, dict):
            raise RiskPolicyError(f"risk-policy.json reversibility.{name} must be an object")
        _check_tier(section.get("floor"), f"reversibility.{name}.floor")
        _check_str_list(section.get("paths"), f"reversibility.{name}.paths")
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


def _check_points(points: Any, label: str) -> dict:
    """A layer's points block: a subset of _REQUIRED_POINTS, each value typed.

    Unlike load_defaults (which requires every point key and an exact
    taskClass/triage key set), a layer may touch only the keys it means to
    change; taskClass and triage are merged key-by-key, so a layer may add or
    override a single class without restating the rest."""
    if not isinstance(points, dict) or any(k not in _REQUIRED_POINTS for k in points):
        raise RiskPolicyError(f"{label} must map known point keys")
    if "taskClass" in points:
        tc = points["taskClass"]
        if not isinstance(tc, dict) or any(k not in TASK_CLASSES for k in tc) or \
                any(not _is_nonneg_int(v) for v in tc.values()):
            raise RiskPolicyError(f"{label}.taskClass must map known task classes to non-negative integers")
    if "triage" in points:
        tr = points["triage"]
        if not isinstance(tr, dict) or any(k not in ("S", "M", "L") for k in tr) or \
                any(not _is_nonneg_int(v) for v in tr.values()):
            raise RiskPolicyError(f"{label}.triage must map S, M, L to non-negative integers")
    for key, value in points.items():
        if key in ("taskClass", "triage"):
            continue
        if not _is_nonneg_int(value):
            raise RiskPolicyError(f"{label}.{key} must be a non-negative integer")
    return points


def _check_layer(layer: Any, label: str) -> dict:
    """A profile.risk block or an overlay: every key optional, each typed.

    Returns a deep copy so the merged document never aliases the caller's
    profile or overlay dicts."""
    if not isinstance(layer, dict):
        raise RiskPolicyError(f"{label} must be an object")
    for k in layer:
        if k not in LAYER_KEYS:
            raise RiskPolicyError(f"{label}: unknown key {k}")
    if "thresholds" in layer:
        th = layer["thresholds"]
        if not isinstance(th, dict):
            raise RiskPolicyError(f"{label}.thresholds must be an object")
        for k, v in th.items():
            if k not in ("yellow", "red") or not _is_nonneg_int(v):
                raise RiskPolicyError(f"{label}.thresholds.{k} must be a non-negative integer")
    if "points" in layer:
        _check_points(layer["points"], f"{label}.points")
    if "protectedAreas" in layer:
        _check_areas(layer["protectedAreas"], f"{label}.protectedAreas")
    for key in ("sensitiveDomains", "factoryControl", "publicContract", "sideEffects", "codeowners"):
        if key in layer and not isinstance(layer[key], dict):
            raise RiskPolicyError(f"{label}.{key} must be an object")
    for key in ("sensitiveDomains", "factoryControl", "publicContract", "sideEffects"):
        section = layer.get(key) or {}
        if "floor" in section:
            _check_tier(section["floor"], f"{label}.{key}.floor")
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
    if "autoMerge" in layer and layer["autoMerge"] is not False:
        raise RiskPolicyError(f"{label}.autoMerge: risk policy cannot enable auto-merge")
    return json.loads(json.dumps(layer))


def _merge_layer(merged: dict, layer: dict) -> None:
    if "thresholds" in layer:
        merged["thresholds"] = dict(merged["thresholds"], **layer["thresholds"])
    if "points" in layer:
        target = merged.setdefault("points", {})
        for key, value in layer["points"].items():
            if key in ("taskClass", "triage"):
                sub = dict(target.get(key, {}))
                sub.update(value)
                target[key] = sub
            else:
                target[key] = value
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


def _require_floor_section(doc: dict, key: str, label: str) -> dict:
    section = doc.get(key)
    if not isinstance(section, dict):
        raise RiskPolicyError(f"{label} {key} must be an object")
    _check_tier(section.get("floor"), f"{label} {key}.floor")
    return section


def assert_invariants(merged: dict, defaults: dict) -> None:
    """The floors that no layer may weaken. Raised at load, proposal and admit.

    Shape-checks first so a malformed document (a hand-edited overlay, a
    Slice 5 proposal, an entry that never went through load_policy) raises a
    named RiskPolicyError instead of a KeyError or AttributeError."""
    if not isinstance(merged, dict):
        raise RiskPolicyError("merged policy document must be an object")
    if not isinstance(defaults, dict):
        raise RiskPolicyError("default policy document must be an object")
    merged_sd = _require_floor_section(merged, "sensitiveDomains", "merged")
    merged_fc = _require_floor_section(merged, "factoryControl", "merged")
    defaults_sd = _require_floor_section(defaults, "sensitiveDomains", "defaults")
    defaults_fc = _require_floor_section(defaults, "factoryControl", "defaults")
    for key, merged_section, defaults_section in (
        ("sensitiveDomains", merged_sd, defaults_sd),
        ("factoryControl", merged_fc, defaults_fc),
    ):
        if tier_gt(defaults_section["floor"], merged_section["floor"]):
            raise RiskPolicyError(f"{key} floor cannot be lowered below {defaults_section['floor']}")
    tokens = merged_sd.get("tokens")
    if not isinstance(tokens, dict) or any(not isinstance(v, list) for v in tokens.values()):
        raise RiskPolicyError("sensitiveDomains.tokens must be an object of lists")
    default_tokens = defaults_sd.get("tokens")
    if not isinstance(default_tokens, dict):
        raise RiskPolicyError("defaults sensitiveDomains.tokens must be an object")
    for name, words in default_tokens.items():
        _check_str_list(tokens.get(name, []), f"sensitiveDomains.tokens.{name}")
        kept = set(tokens.get(name, []))
        missing = sorted(set(words) - kept)
        if missing:
            raise RiskPolicyError(f"sensitiveDomains.tokens.{name} cannot drop {', '.join(missing)}")
    merged_paths = merged_fc.get("paths")
    if not isinstance(merged_paths, list):
        raise RiskPolicyError("factoryControl.paths must be a list")
    default_paths = defaults_fc.get("paths")
    if not isinstance(default_paths, list):
        raise RiskPolicyError("defaults factoryControl.paths must be a list")
    _check_str_list(merged_paths, "factoryControl.paths")
    missing = sorted(set(default_paths) - set(merged_paths))
    if missing:
        raise RiskPolicyError(f"factoryControl.paths cannot drop {', '.join(missing)}")
    if merged.get("autoMerge") not in (None, False):
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
