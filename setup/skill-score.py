#!/usr/bin/env python3
"""Run scoring and the candidate validation window of the skills library.

The score is a deterministic cost of one run's trace digest (archon.trace-digest.v1,
written by setup/trace-digest.py): lower is better. Every weight lives in WEIGHTS
and SCORE_VERSION changes whenever WEIGHTS or the component rules change, so a
window that mixes versions is inconclusive rather than silently compared.

  skill-score.py score <digest.json>
  skill-score.py ingest <repo> <digest.json> --lib <dir> --evolve-run <id> [--out <json>]
  skill-score.py is-ingested <repo> <run-id> --lib <dir>
  skill-score.py window <repo> --lib <dir>

Typed lines (the last one is the discriminator):
  SKILL_SCORE=OK run=.. score=.. eligible=yes|no reason=..            (score)
  SCORE_COMPONENT name=.. value=.. weight=..                           one per non-zero component
  SKILL_SCORE=OK run=.. score=.. eligible=.. window=none|open(n/K)|closed:accept|closed:rollback|closed:inconclusive
  SKILL_WINDOW=ACCEPT skill=.. baseline=.. candidate=..                when a window closes
  SKILL_WINDOW=ROLLBACK skill=.. reason=tie|worse|score_version_mismatch|no_baseline baseline=.. candidate=..
  SKILL_SCORE=FAIL already ingested run=..                             exit 2
  SKILL_SCORE=FAIL <reason>                                            exit 1
  SKILL_INGESTED=YES|NO run=..                                         exit 0 either way
  SKILL_WINDOW=NONE | SKILL_WINDOW=OPEN skill=.. runs=n/K baseline=..

Eligibility: completed runs are scored; failed runs are scored only when they
reached the review loop (round.txt present), otherwise they are ingested with
reason failed_before_review; no_change and incomplete runs are ingested and
never scored. A run counts toward the open candidate's window only when the
run staged that candidate (name, status candidate and sha256 all equal to the
index at ingest time) and the run is eligible.

Window rule (strict, asymmetric): when candidate_runs reaches window_size the
candidate is accepted iff mean(candidate) < mean(baseline) - min_improvement.
A tie rolls back (reason=tie). An empty baseline or a score_version that
differs between the index and any ledger row in the window is inconclusive
and rolls back (reason=no_baseline | score_version_mismatch). Baselines are
frozen at admission by skill-admit.py via baseline_runs() below.

This helper never commits; the calling bash node does.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skill_library as sl  # noqa: E402

SCORE_VERSION = 1
SCHEMA_SUMMARY = "archon.skill-score.v1"

# One weight per component. A change here is a SCORE_VERSION bump.
WEIGHTS = {
    "review_rounds_extra": 10,
    "applied_p0": 8,
    "applied_p1": 4,
    "applied_p2": 2,
    "applied_p3": 1,
    "fixer_incomplete": 3,
    "reraised": 3,
    "waivers": 1,
    "deslop_dirty_rounds": 5,
    "plan_rounds_extra": 5,
    "loop_failure_tokens": 20,
    "other_fail_terminals": 10,
    "terminal_failed": 50,
}

LOOP_FAILURE_TOKENS = {
    "NO_PROGRESS", "FIXER_BLOCKED", "ROUND_CAP_REACHED", "DESLOP_ROUND_CAP",
    "PLAN_NO_PROGRESS", "PLAN_ROUND_CAP", "SCOPE_BREACH",
}
LOOP_FAILURE_PREFIX = "RCA_PLAN_"

TERMINALS = ("completed", "no_change", "failed", "incomplete")


class ScoreError(Exception):
    pass


def is_loop_failure_token(token):
    return token in LOOP_FAILURE_TOKENS or str(token).startswith(LOOP_FAILURE_PREFIX)


def _int(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def components(digest):
    """Raw component counts of one digest, before weighting. Every key of
    WEIGHTS is present so a missing input reads as 0, never as an error."""
    review = _dict(digest.get("review"))
    rounds = _list(review.get("per_round"))
    sev = {"P0": 0, "P1": 0, "P2": 0, "P3": 0}
    incomplete = reraised = 0
    for r in rounds:
        r = _dict(r)
        by = _dict(r.get("applied_by_severity"))
        for k in sev:
            sev[k] += _int(by.get(k))
        incomplete += _int(r.get("incomplete"))
        reraised += _int(r.get("reraised"))
    typed = _dict(digest.get("typed"))
    tokens = _dict(typed.get("fail_tokens"))
    loop = sum(_int(n) for t, n in tokens.items() if is_loop_failure_token(t))
    terminals = [t for t in _list(typed.get("fail_terminals")) if isinstance(t, str)]
    other = sum(1 for t in terminals if not is_loop_failure_token(t))
    return {
        "review_rounds_extra": max(0, _int(review.get("rounds")) - 1),
        "applied_p0": sev["P0"],
        "applied_p1": sev["P1"],
        "applied_p2": sev["P2"],
        "applied_p3": sev["P3"],
        "fixer_incomplete": incomplete,
        "reraised": reraised,
        "waivers": _int(_dict(digest.get("waivers")).get("count")),
        "deslop_dirty_rounds": _int(_dict(digest.get("deslop")).get("dirty_rounds")),
        "plan_rounds_extra": max(0, _int(_dict(digest.get("plan")).get("rounds")) - 1),
        "loop_failure_tokens": loop,
        "other_fail_terminals": other,
        "terminal_failed": 1 if digest.get("terminal") == "failed" else 0,
    }


def score(digest):
    """(score, components). Deterministic; lower is better."""
    comp = components(digest)
    total = sum(comp[k] * WEIGHTS[k] for k in WEIGHTS)
    return float(total), comp


def eligibility(digest):
    """(eligible, reason) per the digest's terminal."""
    terminal = digest.get("terminal")
    if terminal == "completed":
        return True, "completed"
    if terminal == "failed":
        if "round.txt" in _list(digest.get("files_present")):
            return True, "failed_in_review"
        return False, "failed_before_review"
    if terminal == "no_change":
        return False, "no_change"
    if terminal == "incomplete":
        return False, "incomplete"
    return False, f"unknown_terminal:{terminal}"


def staged_entries(digest):
    """[{name, status, sha256}] from the digest's skills_staged block."""
    out = []
    for s in _list(_dict(digest.get("skills_staged")).get("skills")):
        s = _dict(s)
        out.append({"name": s.get("name"), "status": s.get("status"), "sha256": s.get("sha256")})
    return out


def attribution(index, digest):
    """The candidate this run counts toward, or None. All three of name,
    status and sha256 must equal the index entry at ingest time."""
    name = sl.one_candidate(index)
    if name is None:
        return None
    entry = index["skills"][name]
    for s in staged_entries(digest):
        if s["name"] == name and s["status"] == "candidate" and s["sha256"] == entry.get("sha256"):
            return name
    return None


def baseline_runs(lib_root, repo, window_size, before_iso=None):
    """The frozen baseline for a new candidate: the last `window_size` ledger
    rows that are eligible and carry a non-null score, in ledger order
    (append order, which is ingest order; ties on ingested_at keep file
    order). When before_iso is given only rows with ingested_at < before_iso
    count. Returns [{"run_id", "score"}]."""
    rows = sl.read_raw_ledger(lib_root, repo)
    keep = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict) or not row.get("eligible"):
            continue
        sc = row.get("score")
        if isinstance(sc, bool) or not isinstance(sc, (int, float)):
            continue
        at = row.get("ingested_at") or ""
        if before_iso is not None and not at < before_iso:
            continue
        keep.append((at, i, {"run_id": row.get("run_id"), "score": float(sc)}))
    keep.sort(key=lambda t: (t[0], t[1]))
    return [k[2] for k in keep[-int(window_size):]] if window_size > 0 else []


def _mean(runs):
    vals = [float(r["score"]) for r in runs if isinstance(r, dict) and r.get("score") is not None]
    return sum(vals) / len(vals) if vals else None


def _fmt(value):
    return "none" if value is None else f"{value:.2f}"


def _ledger_rows_by_id(lib_root, repo):
    out = {}
    for row in sl.read_raw_ledger(lib_root, repo):
        if isinstance(row, dict) and row.get("run_id"):
            out.setdefault(row["run_id"], row)
    return out


def is_ingested(lib_root, repo, run_id):
    return run_id in _ledger_rows_by_id(lib_root, repo)


def _load_digest(path):
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise ScoreError(f"cannot read digest: {exc}")
    try:
        digest = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ScoreError(f"digest is not JSON: {exc}")
    if not isinstance(digest, dict):
        raise ScoreError("digest is not an object")
    if digest.get("schema") != sl.SCHEMA_DIGEST:
        raise ScoreError(f"digest schema {digest.get('schema')!r} != {sl.SCHEMA_DIGEST}")
    run_id = digest.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip() or "/" in run_id:
        raise ScoreError("digest run_id missing or invalid")
    if digest.get("terminal") not in TERMINALS:
        raise ScoreError(f"digest terminal {digest.get('terminal')!r} not in {TERMINALS}")
    return digest, sl.sha256_bytes(data)


def close_window(lib_root, repo, index, name, evolve_run_id, at=None):
    """Decide the open window of candidate `name` and apply accept/rollback.
    Returns a dict describing the decision. The caller saves the index."""
    at = at or sl.utc_now()
    entry = index["skills"][name]
    scoring = entry["scoring"]
    baseline = _list(scoring.get("baseline_runs"))
    candidate = _list(scoring.get("candidate_runs"))
    ledger = _ledger_rows_by_id(lib_root, repo)
    versions = set()
    for r in baseline + candidate:
        row = ledger.get(_dict(r).get("run_id"))
        if row is not None:
            versions.add(row.get("score_version"))
    b_mean, c_mean = _mean(baseline), _mean(candidate)
    details = {
        "window_size": scoring.get("window_size"),
        "min_improvement": index.get("min_improvement"),
        "baseline_runs": [r.get("run_id") for r in baseline],
        "candidate_runs": [r.get("run_id") for r in candidate],
        "baseline_mean": b_mean,
        "candidate_mean": c_mean,
        "score_version": index.get("score_version"),
    }
    decision = reason = None
    if versions and versions != {index.get("score_version")}:
        decision, reason = "inconclusive", "score_version_mismatch"
    elif b_mean is None:
        decision, reason = "inconclusive", "no_baseline"
    elif c_mean < b_mean - float(index.get("min_improvement") or 0.0):
        decision, reason = "accept", "better"
    elif c_mean == b_mean - float(index.get("min_improvement") or 0.0):
        decision, reason = "rollback", "tie"
    else:
        decision, reason = "rollback", "worse"
    if decision == "accept":
        sl.accept_candidate(lib_root, repo, index, name, at=at)
        sl.record_impact(lib_root, repo, "accepted", name, entry.get("proposal"), evolve_run_id,
                         reason=reason, details=details, at=at)
        sl.append_log(lib_root, repo,
                      f"skill {name} accepted: window of {len(candidate)} runs, candidate mean "
                      f"{_fmt(c_mean)} < baseline mean {_fmt(b_mean)}", at=at)
    else:
        restored = sl.rollback_candidate(lib_root, repo, index, name, reason, at=at)
        details["restored"] = restored
        sl.record_impact(lib_root, repo, "rolled_back", name, entry.get("proposal"), evolve_run_id,
                         reason=reason, details=details, at=at)
        sl.append_log(lib_root, repo,
                      f"skill {name} rolled back ({reason}, {restored}): candidate mean "
                      f"{_fmt(c_mean)}, baseline mean {_fmt(b_mean)}", at=at)
    return {"decision": decision, "reason": reason, "skill": name,
            "baseline_mean": b_mean, "candidate_mean": c_mean, "runs": len(candidate),
            "window_size": scoring.get("window_size")}


def ingest(lib_root, repo, digest_path, evolve_run_id, at=None):
    """Append the run to the raw ledger, attribute it to the open candidate
    when it staged that candidate, and close the window when it fills.
    Returns the summary dict. Raises ScoreError (exit 1) or reports an
    already-ingested run via the 'already' key (exit 2)."""
    at = at or sl.utc_now()
    digest, digest_sha = _load_digest(digest_path)
    run_id = digest["run_id"]
    try:
        p = sl.paths(lib_root, repo)
        index = sl.load_index(lib_root, repo)
    except FileNotFoundError:
        raise ScoreError(f"no skills index for {repo}")
    except sl.LibraryError as exc:
        raise ScoreError(str(exc))
    total, comp = score(digest)
    eligible, reason = eligibility(digest)
    summary = {
        "schema": SCHEMA_SUMMARY, "run_id": run_id, "repo": repo, "score": total,
        "score_version": SCORE_VERSION, "components": comp, "eligible": eligible,
        "eligibility_reason": reason, "attributed_to": None,
        "window": {"state": "none"}, "already_ingested": False,
    }
    if is_ingested(lib_root, repo, run_id):
        summary["already_ingested"] = True
        return summary
    attributed = attribution(index, digest) if eligible else None
    row = {
        "schema": sl.SCHEMA_RAW, "run_id": run_id,
        "artifacts_dir": digest.get("artifacts_dir"), "lane": digest.get("lane"),
        "terminal": digest.get("terminal"), "outcome": digest.get("outcome"),
        "eligible": eligible, "eligibility_reason": reason,
        "score": total if eligible else None, "score_version": SCORE_VERSION,
        "skills_staged": staged_entries(digest), "attributed_to": attributed,
        "library_head": sl.git_head(p["repo_dir"]), "digest_sha256": digest_sha,
        "evolve_run_id": evolve_run_id, "ingested_at": at,
    }
    sl.append_jsonl(p["raw_ledger"], row)
    summary["ledger_row"] = row
    summary["attributed_to"] = attributed
    candidate = sl.one_candidate(index)
    if candidate is not None:
        entry = index["skills"][candidate]
        scoring = entry.setdefault("scoring", {})
        scoring.setdefault("window_size", index.get("window_size"))
        scoring.setdefault("baseline_runs", [])
        runs = scoring.setdefault("candidate_runs", [])
        if attributed == candidate:
            runs.append({"run_id": run_id, "score": total})
        k = int(scoring.get("window_size") or index.get("window_size"))
        if len(runs) >= k:
            closed = close_window(lib_root, repo, index, candidate, evolve_run_id, at=at)
            closed["state"] = "closed:" + closed["decision"]
            summary["window"] = closed
        else:
            summary["window"] = {"state": f"open({len(runs)}/{k})", "skill": candidate,
                                 "runs": len(runs), "window_size": k,
                                 "baseline_mean": _mean(scoring.get("baseline_runs"))}
    try:
        sl.save_index(lib_root, repo, index)
    except sl.LibraryError as exc:
        raise ScoreError(str(exc))
    return summary


# --- CLI ----------------------------------------------------------------------
def cmd_score(args):
    try:
        digest, _ = _load_digest(args.digest)
    except ScoreError as exc:
        print(f"SKILL_SCORE=FAIL {exc}")
        return 1
    total, comp = score(digest)
    eligible, reason = eligibility(digest)
    for k in WEIGHTS:
        if comp[k]:
            print(f"SCORE_COMPONENT name={k} value={comp[k]} weight={WEIGHTS[k]}")
    print(f"SKILL_SCORE=OK run={digest['run_id']} score={_fmt(total)} "
          f"eligible={'yes' if eligible else 'no'} reason={reason}")
    return 0


def cmd_ingest(args):
    try:
        sl.check_repo_name(args.repo)
        summary = ingest(args.lib, args.repo, args.digest, args.evolve_run)
    except (ScoreError, sl.LibraryError) as exc:
        print(f"SKILL_SCORE=FAIL {exc}")
        return 1
    if args.out:
        sl.write_json_atomic(args.out, summary)
    if summary["already_ingested"]:
        print(f"SKILL_SCORE=FAIL already ingested run={summary['run_id']}")
        return 2
    w = summary["window"]
    if w["state"].startswith("closed:"):
        if w["decision"] == "accept":
            print(f"SKILL_WINDOW=ACCEPT skill={w['skill']} baseline={_fmt(w['baseline_mean'])} "
                  f"candidate={_fmt(w['candidate_mean'])}")
        else:
            print(f"SKILL_WINDOW=ROLLBACK skill={w['skill']} reason={w['reason']} "
                  f"baseline={_fmt(w['baseline_mean'])} candidate={_fmt(w['candidate_mean'])}")
    print(f"SKILL_SCORE=OK run={summary['run_id']} score={_fmt(summary['score'])} "
          f"eligible={'yes' if summary['eligible'] else 'no'} window={w['state']}")
    return 0


def cmd_is_ingested(args):
    try:
        sl.check_repo_name(args.repo)
        hit = is_ingested(args.lib, args.repo, args.run_id)
    except sl.LibraryError as exc:
        print(f"SKILL_SCORE=FAIL {exc}")
        return 1
    print(f"SKILL_INGESTED={'YES' if hit else 'NO'} run={args.run_id}")
    return 0


def cmd_window(args):
    try:
        sl.check_repo_name(args.repo)
        index = sl.load_index(args.lib, args.repo)
        name = sl.one_candidate(index)
    except FileNotFoundError:
        print(f"SKILL_SCORE=FAIL no skills index for {args.repo}")
        return 1
    except sl.LibraryError as exc:
        print(f"SKILL_SCORE=FAIL {exc}")
        return 1
    if name is None:
        print("SKILL_WINDOW=NONE")
        return 0
    scoring = _dict(index["skills"][name].get("scoring"))
    k = scoring.get("window_size") or index.get("window_size")
    runs = _list(scoring.get("candidate_runs"))
    print(f"SKILL_WINDOW=OPEN skill={name} runs={len(runs)}/{k} "
          f"baseline={_fmt(_mean(_list(scoring.get('baseline_runs'))))}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("score")
    s.add_argument("digest")
    s.set_defaults(fn=cmd_score)
    s = sub.add_parser("ingest")
    s.add_argument("repo")
    s.add_argument("digest")
    s.add_argument("--lib", required=True)
    s.add_argument("--evolve-run", required=True)
    s.add_argument("--out")
    s.set_defaults(fn=cmd_ingest)
    s = sub.add_parser("is-ingested")
    s.add_argument("repo")
    s.add_argument("run_id")
    s.add_argument("--lib", required=True)
    s.set_defaults(fn=cmd_is_ingested)
    s = sub.add_parser("window")
    s.add_argument("repo")
    s.add_argument("--lib", required=True)
    s.set_defaults(fn=cmd_window)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
