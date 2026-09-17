#!/usr/bin/env python3
"""Compile one run's artifacts tree into a typed, portable digest.

  trace-digest.py <artifacts-dir> [--out <path>]

Writes archon.trace-digest.v1 (schema fixed; skill-score.py and the
skill-evolve agents read it) to --out atomically; without --out nothing is
written and only the typed line is printed. Absent facts are null/0/[], never
inferred. Prose artifacts are listed in files_present, never embedded. The only
absolute path in the document is artifacts_dir.

Typed last line:
  TRACE_DIGEST=OK run=<id> lane=<lane> terminal=<terminal> rounds=<n> score_inputs=<k>   exit 0
  TRACE_DIGEST=FAIL <reason>                                                            exit 1
score_inputs counts the non-zero score components of setup/skill-score.py.

FAIL: missing directory, missing or non-object params.json, any JSON artifact
this digest reads that exists but does not parse. Absent optional artifacts are
not failures.

Terminal classification, in this order (first match wins):
  no_change  feature-result.json outcome == "NO_CHANGE", or
             no-change-closure-intent.json exists (setup/no-change-closure.py)
  completed  a completion marker exists: non-empty pr-url.txt, or
             feature-result.json verification.status == "passed"
             (setup/write-local-candidate.py), or node-kb-capture-gate.out ends
             with a pass-class typed line (kb-capture is the last gated node of
             every lane)
  failed     any typed source ends with a fail-class line (typed.fail_terminals
             is non-empty) and no completion marker exists
  incomplete otherwise (the run stopped without a typed verdict)

Typed sources: every node-<id>.out (id = the node) plus every
<round-dir>/converge.txt (id = "<round-dir>/converge"); the converge nodes tee
their discriminator into the round directory instead of a node file, and that
is where NO_PROGRESS / ROUND_CAP_REACHED / RCA_PLAN_* live on a real run.

repo:  params.repo when present; else web-app when any node-web-*.out exists
       (the web lane's params carry no repo key); else api.
lane:  rca.md present -> bugfix; else plan.md present -> feature; else unknown.
plan:  the planning loop of the lane: plan-round-* (feature) or rca-round-*
       (bugfix), whichever prefix has a counter or a round directory; `prefix`
       records which. critique_blocking counts critique.json findings with
       severity P0/P1 at confidence >= 75 across rounds (the gate's own rule).
review.exit: KEY of the last round's converge line (CONVERGED, NO_PROGRESS,
       ROUND_CAP_REACHED, ...), else null.
reraised: applied findings of round N whose normalised key (setup/review-yield.py
       _key: casefold, strip non [a-z0-9 ], collapse whitespace, 80 chars) was
       already applied in an earlier round.
deslop: rounds = deslop-round.txt else count of deslop-round-N dirs;
       dirty_rounds = deslop-dirty.txt else count of DESLOP=DIRTY lines;
       slop_fail = any "DESLOP_GATE=FAIL slop" line.
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skill_library as sl  # noqa: E402

DIGEST_VERSION = 1
SEVERITIES = ("P0", "P1", "P2", "P3")
ROUND_DIR_RE = re.compile(r"^(round|plan-round|rca-round|deslop-round)-(\d+)$")
NODE_OUT_RE = re.compile(r"^node-(.+)\.out$")
FILES_PRESENT_CAP = 400
SKIP_DIRS = ("traces", "kb")
KEY_LEN = 80
LOOP_FAILURE_TOKENS = {"NO_PROGRESS", "FIXER_BLOCKED", "ROUND_CAP_REACHED", "DESLOP_ROUND_CAP",
                       "PLAN_NO_PROGRESS", "PLAN_ROUND_CAP", "SCOPE_BREACH"}


class Fail(Exception):
    pass


# --- readers ----------------------------------------------------------------
def _key(text):
    """Identical to setup/review-yield.py::_key and update-waivers.py."""
    s = re.sub(r"[^a-z0-9\s]", "", str(text).casefold())
    return re.sub(r"\s+", " ", s).strip()[:KEY_LEN]


def _read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _read_json(path, rel):
    """dict/list or None when absent. Present-but-unparseable is FAIL."""
    if not os.path.isfile(path):
        return None
    try:
        return sl.read_json(path)
    except (OSError, ValueError) as exc:
        raise Fail(f"{rel} is not JSON: {exc}")


def _read_int(path):
    text = _read_text(path)
    if text is None:
        return None
    text = text.strip()
    return int(text) if text.isdigit() else None


def _round_dirs(ad, prefix):
    out = []
    for name in os.listdir(ad):
        m = ROUND_DIR_RE.match(name)
        if m and m.group(1) == prefix and os.path.isdir(os.path.join(ad, name)):
            out.append((int(m.group(2)), name))
    return [name for _, name in sorted(out)]


def _last_typed_of(path):
    text = _read_text(path)
    return sl.last_typed(text) if text is not None else None


def _typed_key(last):
    return last[0] if last else None


# --- sections ---------------------------------------------------------------
def _repo(ad, params):
    repo = params.get("repo")
    if isinstance(repo, str) and repo:
        return repo
    if any(n.startswith("node-web-") and n.endswith(".out") for n in os.listdir(ad)):
        return "web-app"
    return "api"


def _lane(ad):
    if os.path.isfile(os.path.join(ad, "rca.md")):
        return "bugfix"
    if os.path.isfile(os.path.join(ad, "plan.md")):
        return "feature"
    return "unknown"


def _severity_counts(entries):
    counts = {s: 0 for s in SEVERITIES}
    for e in entries:
        sev = e.get("severity") if isinstance(e, dict) else None
        # The fixer prompt: an omitted severity reads as P0.
        counts[sev if sev in counts else "P0"] += 1
    return counts


def _review(ad):
    rounds_counter = _read_int(os.path.join(ad, "round.txt"))
    dirs = _round_dirs(ad, "round")
    rounds = rounds_counter if rounds_counter is not None else len(dirs)
    per_round, seen_keys = [], set()
    for name in dirs:
        n = int(name.split("-")[1])
        summary = _read_json(os.path.join(ad, name, "review-summary.json"), f"{name}/review-summary.json") or {}
        fixer = _read_json(os.path.join(ad, name, "fixer-result.json"), f"{name}/fixer-result.json") or {}
        applied = fixer.get("applied") if isinstance(fixer.get("applied"), list) else []
        keys = [_key(e.get("finding", "") if isinstance(e, dict) else e) for e in applied]
        reraised = sum(1 for k in keys if k and k in seen_keys)
        seen_keys.update(k for k in keys if k)
        last = _last_typed_of(os.path.join(ad, name, "converge.txt"))
        per_round.append({
            "round": n,
            "verdict": summary.get("verdict") if isinstance(summary.get("verdict"), str) else None,
            "residual_count": summary.get("residual_count") if isinstance(summary.get("residual_count"), int) else 0,
            "degraded": bool(summary.get("degraded", False)),
            "applied": len(applied),
            "applied_by_severity": _severity_counts(applied),
            "incomplete": len(fixer.get("incomplete") or []),
            "advisory": len(fixer.get("advisory") or []),
            "failed": len(fixer.get("failed") or []),
            "reraised": reraised,
            "converge_line": last[2] if last else None,
        })
    exit_key = None
    for r in reversed(per_round):
        if r["converge_line"]:
            exit_key = sl.parse_typed_lines(r["converge_line"])[0][0]
            break
    return {"rounds": rounds, "exit": exit_key, "per_round": per_round}


def _plan(ad, lane):
    prefixes = ("rca-round", "plan-round") if lane == "bugfix" else ("plan-round", "rca-round")
    prefix = None
    for p in prefixes:
        if os.path.isfile(os.path.join(ad, f"{p}.txt")) or _round_dirs(ad, p):
            prefix = p
            break
    if prefix is None:
        return {"prefix": None, "rounds": 0, "exit": None, "critique_blocking": 0}
    counter = _read_int(os.path.join(ad, f"{prefix}.txt"))
    dirs = _round_dirs(ad, prefix)
    blocking, exit_key = 0, None
    for name in dirs:
        crit = _read_json(os.path.join(ad, name, "critique.json"), f"{name}/critique.json") or {}
        for f in crit.get("findings") or []:
            if not isinstance(f, dict):
                continue
            conf = f.get("confidence")
            if f.get("severity") in ("P0", "P1") and isinstance(conf, int) and conf >= 75:
                blocking += 1
        last = _last_typed_of(os.path.join(ad, name, "converge.txt"))
        if last:
            exit_key = last[0]
    return {"prefix": prefix, "rounds": counter if counter is not None else len(dirs),
            "exit": exit_key, "critique_blocking": blocking}


def _typed(ad):
    sources = []
    for name in sorted(os.listdir(ad)):
        m = NODE_OUT_RE.match(name)
        if m and os.path.isfile(os.path.join(ad, name)):
            sources.append((m.group(1), os.path.join(ad, name)))
        elif ROUND_DIR_RE.match(name) and os.path.isfile(os.path.join(ad, name, "converge.txt")):
            sources.append((f"{name}/converge", os.path.join(ad, name, "converge.txt")))
    nodes, fail_terminals, fail_tokens, warn = {}, [], {}, 0
    dirty_rounds, slop_fail = 0, False
    for nid, path in sorted(sources):
        text = _read_text(path) or ""
        lines = sl.parse_typed_lines(text)
        for key, val, line in lines:
            if key in sl.FAIL_TOKENS:
                fail_tokens[key] = fail_tokens.get(key, 0) + 1
            if val == "WARN":
                warn += 1
            if key == "DESLOP" and val == "DIRTY":
                dirty_rounds += 1
            if line.startswith("DESLOP_GATE=FAIL slop"):
                slop_fail = True
        last = lines[-1] if lines else None
        cls = sl.classify_typed(*last) if last else None
        nodes[nid] = {"last": last[2] if last else None,
                      "pass": True if cls == "pass" else False if cls == "fail" else None}
        if cls == "fail":
            fail_terminals.append(last[0])
    return ({"fail_terminals": fail_terminals, "fail_tokens": fail_tokens,
             "warn_lines": warn, "nodes": nodes},
            dirty_rounds, slop_fail)


def _deslop(ad, dirty_seen, slop_fail):
    counter = _read_int(os.path.join(ad, "deslop-round.txt"))
    rounds = counter if counter is not None else len(_round_dirs(ad, "deslop-round"))
    dirty = _read_int(os.path.join(ad, "deslop-dirty.txt"))
    return {"rounds": rounds, "dirty_rounds": dirty if dirty is not None else dirty_seen,
            "slop_fail": slop_fail}


def _waivers(ad):
    doc = _read_json(os.path.join(ad, "waivers.json"), "waivers.json") or {}
    entries = doc.get("entries") if isinstance(doc.get("entries"), list) else []
    findings = [e.get("finding") for e in entries if isinstance(e, dict) and isinstance(e.get("finding"), str)]
    return {"count": len(entries), "findings": findings}


def _files_present(ad):
    out = []
    for name in sorted(os.listdir(ad)):
        full = os.path.join(ad, name)
        if os.path.isfile(full) and not name.endswith(".tar"):
            out.append(name)
        elif os.path.isdir(full) and ROUND_DIR_RE.match(name):
            for dirpath, dirnames, filenames in os.walk(full):
                dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
                for fn in sorted(filenames):
                    if fn.endswith(".tar"):
                        continue
                    out.append(os.path.relpath(os.path.join(dirpath, fn), ad))
    return sorted(out)[:FILES_PRESENT_CAP]


def _terminal(ad, feature_result, typed):
    if isinstance(feature_result, dict) and feature_result.get("outcome") == "NO_CHANGE":
        return "no_change"
    if os.path.isfile(os.path.join(ad, "no-change-closure-intent.json")):
        return "no_change"
    pr = (_read_text(os.path.join(ad, "pr-url.txt")) or "").strip()
    verification = feature_result.get("verification") if isinstance(feature_result, dict) else None
    kb = typed["nodes"].get("kb-capture-gate") or {}
    if pr or (isinstance(verification, dict) and verification.get("status") == "passed") or kb.get("pass") is True:
        return "completed"
    if typed["fail_terminals"]:
        return "failed"
    return "incomplete"


def score_components(d):
    """The score inputs of setup/skill-score.py SCORE_VERSION 1, unweighted.
    Kept here only to count non-zero components for the typed line."""
    pr = d["review"]["per_round"]
    tokens = d["typed"]["fail_tokens"]
    loop = sum(c for t, c in tokens.items() if t in LOOP_FAILURE_TOKENS or t.startswith("RCA_PLAN_"))
    other = [k for k in d["typed"]["fail_terminals"]
             if not (k in LOOP_FAILURE_TOKENS or k.startswith("RCA_PLAN_"))]
    return {
        "review_rounds_beyond_first": max(0, d["review"]["rounds"] - 1),
        "applied_findings": sum(sum(r["applied_by_severity"].values()) for r in pr),
        "fixer_incomplete": sum(r["incomplete"] for r in pr),
        "reraised": sum(r["reraised"] for r in pr),
        "waivers": d["waivers"]["count"],
        "deslop_dirty_rounds": d["deslop"]["dirty_rounds"],
        "plan_rounds_beyond_first": max(0, d["plan"]["rounds"] - 1),
        "loop_failure_tokens": loop,
        "other_fail_terminals": len(other),
        "terminal_failed": 1 if d["terminal"] == "failed" else 0,
    }


# --- digest -----------------------------------------------------------------
def digest(artifacts_dir):
    ad = os.path.abspath(artifacts_dir)
    if not os.path.isdir(ad):
        raise Fail(f"artifacts dir missing: {artifacts_dir}")
    params = _read_json(os.path.join(ad, "params.json"), "params.json")
    if params is None:
        raise Fail("params.json missing")
    if not isinstance(params, dict):
        raise Fail("params.json is not an object")
    lane = _lane(ad)
    typed, dirty_seen, slop_fail = _typed(ad)
    feature_result = _read_json(os.path.join(ad, "feature-result.json"), "feature-result.json")
    review = _review(ad)
    plan = _plan(ad, lane)
    terminal = _terminal(ad, feature_result, typed)
    failed_at = None
    if terminal == "failed":
        failed_at = sorted(n for n, v in typed["nodes"].items() if v["pass"] is False)[0]
    spec = params.get("spec")
    pr_url = (_read_text(os.path.join(ad, "pr-url.txt")) or "").strip() or None
    rca = None
    if lane == "bugfix":
        rca = {"present": True, "rounds": plan["rounds"] if plan["prefix"] == "rca-round" else 0,
               "exit": plan["exit"] if plan["prefix"] == "rca-round" else None,
               "critique_blocking": plan["critique_blocking"] if plan["prefix"] == "rca-round" else 0}
    doc = {
        "schema": sl.SCHEMA_DIGEST,
        "digest_version": DIGEST_VERSION,
        "run_id": os.path.basename(ad.rstrip(os.sep)),
        "artifacts_dir": ad,
        "repo": _repo(ad, params),
        "lane": lane,
        "spec": os.path.basename(spec) if isinstance(spec, str) and spec else None,
        "slug": params.get("slug") if isinstance(params.get("slug"), str) else None,
        "branch": params.get("branch") if isinstance(params.get("branch"), str) else None,
        "terminal": terminal,
        "failed_at": failed_at,
        "outcome": feature_result.get("outcome") if isinstance(feature_result, dict)
        and isinstance(feature_result.get("outcome"), str) else None,
        "pr_url": pr_url,
        "plan": plan,
        "review": review,
        "deslop": _deslop(ad, dirty_seen, slop_fail),
        "waivers": _waivers(ad),
        "typed": typed,
        "rca": rca,
        "skills_staged": _read_json(os.path.join(ad, "skills-staged.json"), "skills-staged.json"),
        "files_present": _files_present(ad),
    }
    return doc


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("artifacts_dir")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    try:
        doc = digest(args.artifacts_dir)
        if args.out:
            sl.write_json_atomic(args.out, doc)
    except Fail as exc:
        print(f"TRACE_DIGEST=FAIL {exc}")
        return 1
    except OSError as exc:
        print(f"TRACE_DIGEST=FAIL {exc}")
        return 1
    k = sum(1 for v in score_components(doc).values() if v)
    print(f"TRACE_DIGEST=OK run={doc['run_id']} lane={doc['lane']} terminal={doc['terminal']} "
          f"rounds={doc['review']['rounds']} score_inputs={k}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
