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
import posixpath
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import risk_policy as rp  # noqa: E402
import skill_library as sl  # noqa: E402

SCORING_VERSION = 1
TEST_RE = re.compile(r"(/__tests__/|(^|/)tests?/|(^|/)test_[^/]+\.py$|_test\.(go|py)$|\.(spec|test|int\.spec|e2e\.spec)\.[cm]?[jt]sx?$)")
PATH_TOKEN_RE = re.compile(r"(?<![\w/\\:.])[/\\]?[A-Za-z0-9_.@-]+(?:[/\\][A-Za-z0-9_.@-]+)+[/\\]?")
BARE_NAME_RE = re.compile(r"(?<![\w/\\])[A-Za-z0-9_.-]+")
KIND_LINE_RE = re.compile(r"^\s*Kind:\s*([A-Za-z][\w-]*)\s*$", re.M)
KIND_HEADING_RE = re.compile(r"^## Kind\s*$\n+\s*([A-Za-z][\w-]*)", re.M)


class Fail(Exception):
    pass


# --- small readers -----------------------------------------------------------
def read_text(path, label):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except (OSError, ValueError) as exc:  # a non-UTF-8 file raises UnicodeDecodeError (a ValueError)
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


def canonical_path(entry, label, soft=False):
    """Repo-relative, normalised: interior runs of 2+ slashes collapsed to
    one, repeated leading './' segments dropped, interior '..' segments
    resolved (apps/../apps/x -> apps/x) via posixpath.normpath with a
    trailing slash carried across that resolution. An unresolved '..'
    segment, an empty result, or a still-absolute entry FAILs.

    soft=True is for free-form prose (a brief): a backslash is converted to
    '/' and one leading '/' is stripped before those checks, and any
    resulting problem is dropped instead of raised (return None), so one odd
    token does not sink the whole score. soft=False (the default) is for
    machine-produced, already-structured input (files-allowlist.json, a git
    diff path): a backslash or any leading '/' there means an upstream node
    is broken, so both FAIL immediately rather than being silently
    repaired."""
    def bad(msg):
        if soft:
            return None
        raise Fail(f"{label} {msg}: {entry}")
    if not isinstance(entry, str) or not entry.strip():
        return bad("entry is not a non-empty string")
    raw = entry.strip()
    if "\\" in raw:
        if not soft:
            return bad("entry must use forward slashes")
        raw = raw.replace("\\", "/")
    if raw.startswith("/"):
        if not soft:
            return bad("entry must be repo-relative")
        raw = raw[1:]
        if raw.startswith("/"):
            return bad("entry must be repo-relative")
    e = re.sub(r"/{2,}", "/", raw)
    while e.startswith("./"):
        e = re.sub(r"/{2,}", "/", e[2:])
    # posixpath.normpath resolves interior ".." segments (apps/../apps/x ->
    # apps/x) instead of letting one sink the whole entry; it also strips a
    # trailing slash, so that is carried over separately when present.
    had_slash = e.endswith("/") and e != "/"
    normed = posixpath.normpath(e)
    e = "" if normed == "." else (normed + "/" if had_slash else normed)
    if e.startswith("/") or ".." in e.split("/"):
        return bad("entry escapes the repo")
    if e in ("", "."):
        return bad("entry is empty after normalisation")
    return e


def git(root, *args):
    try:
        r = subprocess.run(["git", "-C", root, *args], capture_output=True, encoding="utf-8")
    except (OSError, ValueError) as exc:  # non-UTF-8 git output raises UnicodeDecodeError (a ValueError)
        raise Fail(f"git {' '.join(args)} failed: {exc}")
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
    """Repo-relative paths the brief names: multi-segment tokens containing
    '/' or '\\' (URL schemes stripped first, trailing sentence punctuation
    stripped per token), each canonicalised via canonical_path(soft=True) --
    a leading './' or one leading '/' is normalised away, and a token that
    still escapes the repo or is still absolute after that is dropped rather
    than failing the whole brief -- plus bare lockfile and manifest names
    from the policy ("bump bun.lock") that are not already part of a longer
    path token (so "apps/web/package.json" does not also add a bare
    "package.json" hit), de-duplicated, sorted."""
    text = re.sub(r"\w+://\S+", " ", text)
    out = set()
    for tok in PATH_TOKEN_RE.findall(text):
        tok = tok.rstrip(".,;:)")
        canon = canonical_path(tok, "brief-path", soft=True)
        if canon is not None:
            out.add(canon)
    rev = policy["reversibility"]
    names = {n for key in ("lockfiles", "manifests") for n in rev[key]["paths"] if "/" not in n and "*" not in n}
    for tok in BARE_NAME_RE.findall(text):
        clean = tok.rstrip(".,;:)")
        if clean in names:
            out.add(clean)
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
    """Assemble every input build() needs for one stage.

    A relative params.json "spec" path resolves against --repo-root, not the
    process's current working directory, since the node that writes
    params.json rarely runs with the worktree as its cwd. An explicit
    --brief is used exactly as given (left relative to the cwd), since that
    is a direct CLI override."""
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
    root = os.path.abspath(args.repo_root)
    if not os.path.isdir(root):
        raise Fail(f"repo root missing: {args.repo_root}")
    brief_path = args.brief or params.get("spec")
    if not isinstance(brief_path, str) or not brief_path.strip():
        raise Fail("brief missing: params.json has no readable spec path and no --brief given")
    if not args.brief and not os.path.isabs(brief_path):
        brief_path = os.path.join(root, brief_path)
    if not os.path.isfile(brief_path):
        raise Fail("brief missing: params.json has no readable spec path and no --brief given")
    brief = read_text(brief_path, "brief")
    if not brief.strip():
        raise Fail("brief is empty")
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


STAGE_FUNCS = {"intake": lambda ctx: signals_intake(ctx) + ({},), "plan": signals_plan, "impl": signals_impl}


def score(args):
    ctx = context(args)
    signals, paths, extra_inputs = STAGE_FUNCS[args.stage](ctx)
    return ctx, build(ctx, signals, paths, extra_inputs)


class RiskArgumentParser(argparse.ArgumentParser):
    """A usage error (missing or invalid argument) must end with the same
    typed last line as every other failure mode, not argparse's default
    usage dump on stderr and exit code 2."""
    def error(self, message):
        print(f"RISK=FAIL usage: {message}")
        raise SystemExit(1)


def main(argv=None):
    ap = RiskArgumentParser(description=__doc__.split("\n")[0], epilog=__doc__,
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
        # Trajectory first: it is append-only and cheap to retry, so if it
        # fails nothing (not even a partial risk-<stage>.json) is left behind.
        sl.append_jsonl(os.path.join(ctx["ad"], "risk-trajectory.jsonl"), trajectory_line(doc))
        sl.write_json_atomic(os.path.join(ctx["ad"], f"risk-{args.stage}.json"), doc)
    except Fail as exc:
        print(f"RISK=FAIL {exc}")
        return 1
    except OSError as exc:
        print(f"RISK=FAIL {exc}")
        return 1
    except Exception as exc:  # last-resort fail-closed guard: never let a raw traceback be the last line
        print(f"RISK=FAIL internal: {type(exc).__name__}: {exc}")
        return 1
    floors = ",".join(f["id"] for f in doc["floors"]) or "none"
    print(f"RISK_TIER={doc['tier']} stage={doc['stage']} score={doc['mechanical']['score']} floors={floors}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
