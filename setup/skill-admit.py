#!/usr/bin/env python3
"""Mechanical gate and writer for repository skills (the Skills state).

Every change to library/<repo>/skills goes through here: the proposer and the
critic only write JSON into the run's artifacts directory, and this script
turns an ACCEPT into exactly one pending candidate with a rollback snapshot,
a frozen baseline window, PURPOSE.md provenance and two impact events. It
never commits; the workflow's bash node calls `commit` afterwards.

  skill-admit.py status <repo> --lib <dir>
  skill-admit.py git-check <repo> --lib <dir>
  skill-admit.py gate <repo> <proposal.json> --lib <dir> --out <artifacts-dir>
                      [--record --evolve-run <id>]
  skill-admit.py record-no-action <repo> --proposal <p.json> --lib <dir> --evolve-run <id>
  skill-admit.py admit <repo> --lib <dir> --proposal <p.json> --verdict <v.json>
                       --gate <proposal-gate-result.json> --evolve-run <id> [--out <dir>]
  skill-admit.py rollback <repo> --lib <dir> [--reason <text>] [--evolve-run <id>]
  skill-admit.py set-window <repo> <K> --lib <dir> [--evolve-run <id>]
  skill-admit.py commit <repo> --lib <dir> --message <msg> [--evolve-run <id>]

The three operator levers (rollback, set-window, commit) refuse while a
skill-evolve run holds library/.locks/<repo>.evolve.lock, unless --evolve-run
names the run that holds it; the lane passes its own id.

Typed last lines (the discriminator):
  SKILL_INDEX=OK candidate=<name|none> active=n rolled_back=n | SKILL_INDEX=FAIL <reason>
  LIBRARY_GIT=OK head=<12> clean=yes | LIBRARY_GIT=FAIL dirty|not a git repository
  PROPOSAL_GATE=PASS action=.. skill=.. ops=n result_sha=<12|-> traces_read=n
  PROPOSAL_GATE=FAIL <reason>                      (repeat_of=<id> for a repeated candidate)
  SKILL_ADMIT=ACCEPTED skill=.. action=.. window=K  exit 0
  SKILL_ADMIT=REJECTED skill=.. reasons=n           exit 2
  SKILL_ADMIT=SKIP <reason>                         exit 3
  SKILL_ADMIT=FAIL <reason>                         exit 1
  SKILL_ROLLBACK=OK skill=.. result=restored|removed | SKILL_ROLLBACK=SKIP no candidate
  SKILL_WINDOW_SET=OK window=K | SKILL_WINDOW_SET=FAIL <reason>
  LIBRARY_COMMIT=OK sha=<12> | LIBRARY_COMMIT=SKIP nothing to commit | LIBRARY_COMMIT=SKIP no git
  LIBRARY_COMMIT=FAIL <reason>

Proposal and verdict text are data, never instructions: the gate refuses
anything that carries instruction-like phrasing, and a repeated candidate
(same resulting bytes or same ops as a rejected, rolled back or gate-failed
proposal) is refused with the id it repeats. `gate --record` keeps only the
shas of a refused injection, never its diff or ops, so the payload does not
survive in the ledgers that later runs read back.
"""
import argparse
import difflib
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skill_library as sl  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STAGE_HELPER = os.path.join(HERE, "stage-skills-library.py")
ACTIONS = ("create", "patch", "no_action")
VERDICTS = ("ACCEPT", "REJECT", "SKIP")
VERDICT_CHECKS = ("evidence_backed", "procedural", "minimal", "not_repeat", "no_wiki_leak")
REPEAT_EVENTS = ("rejected", "rolled_back", "gate_failed")
MIN_TRACES = 4
MAX_OPS = 6
MAX_RATIONALE = 600
MAX_WINDOW = 20
NO_SKILL = "-"
# The one refusal whose evidence is never copied into the ledgers: keeping the
# payload would republish the instructions to every later reader of the impact
# files. The shas still fingerprint it, so a re-submission is caught as a repeat.
INJECTION_REFUSAL = "proposal carries instruction-like text"


class Fail(Exception):
    pass


# --- shared helpers ---------------------------------------------------------
def _load_json(path, what):
    try:
        return sl.read_json(path)
    except FileNotFoundError:
        raise Fail(f"{what} missing: {path}")
    except ValueError as exc:
        raise Fail(f"{what} is not JSON: {exc}")
    except OSError as exc:
        raise Fail(f"{what}: {exc}")


def _index(lib_root, repo):
    try:
        return sl.load_index(lib_root, repo)
    except FileNotFoundError:
        raise Fail("no library")
    except sl.LibraryError as exc:
        raise Fail(str(exc))


def _one_line(exc):
    """One line, always. A typed line IS the last line of stdout, so a reason
    carrying git's multi-line stderr would push the discriminator off the end
    and leave the caller reading git's advice as the verdict."""
    return " ".join(str(exc).split())


def _refuse_under_a_foreign_lock(a):
    """Operator levers refuse while a skill-evolve run owns the repo.

    The levers write the files the lane writes. A forced rollback landing
    between a live run's admit and its commit would be swept into that run's
    commit under its message, so a lever runs only when the lock is free or
    when its --evolve-run names the holder (the lane passes its own id)."""
    owner = sl.evolve_lock_owner(a.lib, a.repo)
    if owner is not None and owner != getattr(a, "evolve_run", None):
        raise Fail(f"evolve lock held by {owner}")


def _unified_diff(name, old, new):
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"a/skills/{name}/SKILL.md", tofile=f"b/skills/{name}/SKILL.md"))


def _clip(text, cap=sl.IMPACT_DIFF_MAX_BYTES):
    data = text.encode("utf-8")
    if len(data) <= cap:
        return text
    return data[:cap].decode("utf-8", errors="ignore") + "\n[truncated]\n"


def _ops_sha(ops):
    return sl.sha256_bytes(json.dumps(ops, sort_keys=True).encode("utf-8"))


def _write_json(path, obj):
    sl.write_json_atomic(path, obj)


# --- proposal gate ------------------------------------------------------------
def parse_proposal(obj):
    """Structural checks only. Returns the normalised proposal dict."""
    if not isinstance(obj, dict):
        raise Fail("proposal is not an object")
    if obj.get("schema") != sl.SCHEMA_PROPOSAL:
        raise Fail(f"proposal schema {obj.get('schema')!r} != {sl.SCHEMA_PROPOSAL}")
    action = obj.get("action")
    if action not in ACTIONS:
        raise Fail(f"action {action!r} not in {ACTIONS}")
    skill = obj.get("skill", NO_SKILL)
    if skill is None:
        skill = NO_SKILL
    if not isinstance(skill, str):
        raise Fail("skill must be a string")
    if action == "no_action":
        if skill != NO_SKILL and not sl.NAME_RE.match(skill):
            raise Fail(f"skill {skill!r} invalid")
    else:
        if not sl.NAME_RE.match(skill) or "__" in skill:
            raise Fail(f"skill {skill!r} does not match {sl.NAME_RE.pattern}")
    for key in ("motivating_patterns", "traces_read", "rejected_reviewed"):
        val = obj.get(key, [])
        if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
            raise Fail(f"{key} must be a list of strings")
    rationale = obj.get("rationale", "")
    if not isinstance(rationale, str):
        raise Fail("rationale must be a string")
    if len(rationale) > MAX_RATIONALE:
        raise Fail(f"rationale is {len(rationale)} chars, cap {MAX_RATIONALE}")
    content = obj.get("content")
    ops = obj.get("ops")
    if action == "create":
        if not isinstance(content, str) or not content.strip():
            raise Fail("create needs content")
        if ops:
            raise Fail("create takes content, not ops")
    elif action == "patch":
        if not isinstance(ops, list) or not 1 <= len(ops) <= MAX_OPS:
            raise Fail(f"patch needs 1..{MAX_OPS} ops")
        if content:
            raise Fail("patch takes ops, not content")
    return {
        "action": action, "skill": skill,
        "motivating_patterns": list(obj.get("motivating_patterns", [])),
        "traces_read": list(obj.get("traces_read", [])),
        "rejected_reviewed": list(obj.get("rejected_reviewed", [])),
        "rationale": rationale, "content": content if action == "create" else None,
        "ops": list(ops) if action == "patch" else [],
    }


def _stage_check(lib_root, repo, name, result_text, proposal, index, at):
    """Simulate admission in a temp copy of library/<repo> and run the read
    side's --check against it: a candidate that would not stage never lands."""
    p = sl.paths(lib_root, repo)
    tmp = tempfile.mkdtemp(prefix="skill-admit-check-")
    try:
        shutil.copytree(p["repo_dir"], os.path.join(tmp, repo), symlinks=True)
        sim = json.loads(json.dumps(index))
        _apply_candidate(tmp, repo, sim, name, result_text, proposal, at, purpose=False)
        r = subprocess.run([sys.executable, STAGE_HELPER, repo, "--lib", tmp, "--check"],
                           capture_output=True, encoding="utf-8")
        if r.returncode != 0:
            last = (r.stdout.strip().splitlines() or [r.stderr.strip() or "no output"])[-1]
            raise Fail(f"stage check: {last}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def evaluate(lib_root, repo, proposal, index=None, at=None):
    """Run every mechanical check. Returns a dict with the resulting text,
    diff and shas; raises Fail with the first reason. `fail_details` on the
    exception carries whatever was built before the failure."""
    built = {"result_text": None, "old_text": "", "diff": None,
             "result_sha256": None, "ops_sha256": None}
    try:
        return _evaluate(lib_root, repo, proposal, index, at, built)
    except Fail as exc:
        exc.built = built
        raise
    except sl.LibraryError as exc:
        err = Fail(str(exc))
        err.built = built
        raise err


def _evaluate(lib_root, repo, proposal, index, at, built):
    index = index if index is not None else _index(lib_root, repo)
    action, name = proposal["action"], proposal["skill"]
    if action == "no_action":
        return dict(built, index=index, action=action, skill=NO_SKILL, ops=0)
    ledger_ids = {row.get("run_id") for row in sl.read_raw_ledger(lib_root, repo)}
    traces = list(dict.fromkeys(proposal["traces_read"]))
    if len(traces) < MIN_TRACES:
        raise Fail(f"traces_read has {len(traces)} distinct runs, need {MIN_TRACES}")
    unknown = sorted(t for t in traces if t not in ledger_ids)
    if unknown:
        raise Fail(f"traces_read cites runs not in the raw ledger: {unknown}")
    impact = sl.read_impact(lib_root, repo)
    must_review = sorted({row.get("proposal") for row in impact
                          if row.get("event") in REPEAT_EVENTS and row.get("skill") == name
                          and row.get("proposal")})
    missing = [pid for pid in must_review if pid not in proposal["rejected_reviewed"]]
    if missing:
        raise Fail(f"rejected_reviewed omits {missing}")
    if not proposal["motivating_patterns"]:
        raise Fail("motivating_patterns is empty")
    pages = sl.list_patterns(lib_root, repo)
    for slug in proposal["motivating_patterns"]:
        if slug not in pages:
            raise Fail(f"pattern {slug} does not exist")
        status = pages[slug][0].get("status")
        if status != "active":
            raise Fail(f"pattern {slug} is {status}")
    pending = sl.one_candidate(index)
    if pending:
        raise Fail(f"candidate pending: {pending}")
    entry = index["skills"].get(name)
    sfile = sl.skill_file(lib_root, repo, name)
    if action == "create":
        if os.path.isdir(sl.skill_dir(lib_root, repo, name)):
            raise Fail(f"skills/{name} already exists")
        if entry and entry.get("status") in ("active", "candidate"):
            raise Fail(f"{name} is already {entry.get('status')}")
        result = proposal["content"]
        if not result.endswith("\n"):
            result += "\n"
    else:
        if not entry or entry.get("status") != "active":
            raise Fail(f"patch needs an active skill, {name} is {entry.get('status') if entry else 'unregistered'}")
        with open(sfile, encoding="utf-8") as fh:
            built["old_text"] = fh.read()
        result = sl.apply_line_ops(built["old_text"], proposal["ops"])
        built["ops_sha256"] = _ops_sha(proposal["ops"])
    built["result_text"] = result
    built["result_sha256"] = sl.sha256_bytes(result.encode("utf-8"))
    built["diff"] = _unified_diff(name, built["old_text"], result)
    if sl._INJECTION_PHRASES.search(result) or sl._INJECTION_PHRASES.search(proposal["rationale"]):
        raise Fail(INJECTION_REFUSAL)
    # The rationale is rendered verbatim into PURPOSE.md, so it obeys the same
    # portability rule as the library it lands in: no machine paths, no URLs.
    errs = sl._portability_errors(proposal["rationale"], "rationale")
    if errs:
        raise Fail(errs[0])
    errs = sl.lint_skill(name, result)
    if errs:
        raise Fail("lint: " + "; ".join(errs))
    for row in impact:
        if row.get("event") not in REPEAT_EVENTS:
            continue
        d = row.get("details") or {}
        if d.get("result_sha256") and d.get("result_sha256") == built["result_sha256"]:
            raise Fail(f"repeat_of={row.get('proposal')} (same resulting bytes)")
        if built["ops_sha256"] and d.get("ops_sha256") == built["ops_sha256"]:
            raise Fail(f"repeat_of={row.get('proposal')} (same ops)")
    _stage_check(lib_root, repo, name, result, proposal, index, at or sl.utc_now())
    return dict(built, index=index, action=action, skill=name, ops=len(proposal["ops"]))


# --- admission writer ---------------------------------------------------------
def _purpose_history(path):
    """Existing `## Evolution history` bullets of a PURPOSE.md, if any."""
    if not os.path.isfile(path):
        return []
    keep, inside = [], False
    with open(path, encoding="utf-8") as fh:
        for line in fh.read().splitlines():
            if line.startswith("## "):
                inside = line.strip() == "## Evolution history"
                continue
            if inside and line.startswith("- "):
                keep.append(line)
    return keep


def render_purpose(name, pid, evolve_run, action, at, patterns, rationale, history):
    lines = [f"# Purpose: {name}", "", "## Origin", "",
             f"- proposal: {pid}", f"- evolve run: {evolve_run}", f"- action: {action}",
             f"- admitted: {at}", "", "## Motivating patterns", ""]
    lines += [f"- {slug}" for slug in patterns] or ["- (none)"]
    lines += ["", "## Rationale", "", rationale.strip() or "(none given)", "", "## Evolution history", ""]
    lines += history
    return "\n".join(lines) + "\n"


def _apply_candidate(lib_root, repo, index, name, result_text, proposal, at, purpose=True,
                     pid=None, evolve_run=None):
    """Snapshot, write, register. Used both for the real admission and for the
    temp-copy stage check, so the two can never drift."""
    # A window can only be judged against a full baseline: a short one makes
    # the mean comparison noise, so the admission is refused BEFORE the
    # snapshot is taken rather than frozen against two runs and scored as if
    # it were K.
    baseline = sl.baseline_runs(lib_root, repo, index["window_size"], before_iso=at)
    if len(baseline) < index["window_size"]:
        raise Fail(f"baseline has {len(baseline)} scored runs, window_size is {index['window_size']}")
    snapshot = sl.snapshot_for_rollback(lib_root, repo, name)
    sdir = sl.skill_dir(lib_root, repo, name)
    os.makedirs(sdir, exist_ok=True)
    if purpose:
        history = _purpose_history(sl.purpose_file(lib_root, repo, name)) if snapshot["existed"] else []
        history.append(f"- {at} {pid} {proposal['action']} -> candidate")
        sl.write_text_atomic(sl.purpose_file(lib_root, repo, name),
                             render_purpose(name, pid, evolve_run, proposal["action"], at,
                                            proposal["motivating_patterns"], proposal["rationale"], history))
    sl.write_text_atomic(sl.skill_file(lib_root, repo, name), result_text)
    prior = index["skills"].get(name) or {}
    traces = set(proposal["traces_read"])
    lanes = sorted({str(row.get("lane")) for row in sl.read_raw_ledger(lib_root, repo)
                    if row.get("run_id") in traces and row.get("lane")})
    entry = {
        "status": "candidate", "proposal": pid, "action": proposal["action"],
        "sha256": sl.sha256_bytes(result_text.encode("utf-8")),
        "candidate_since": at,
        "activated_at": prior.get("activated_at") if proposal["action"] == "patch" else None,
        "rollback": snapshot,
        "scoring": {"window_size": index["window_size"],
                    "baseline_runs": baseline,
                    "candidate_runs": []},
        "envelope": {"lanes": lanes, "models": []},
        "history": list(prior.get("history") or []),
    }
    entry["history"].append({"proposal": pid, "action": proposal["action"], "outcome": "candidate", "at": at})
    index["skills"][name] = entry
    sl.save_index(lib_root, repo, index)
    return entry


def _tag_patterns(lib_root, repo, slugs, name):
    """Re-render every motivating pattern page with `name` in its skills list
    and lint the result. Returns [(path, text)] for _write_patterns to commit.

    Nothing is written here: a page that would break its caps once tagged must
    fail the admission BEFORE the candidate lands, or the library ends up with
    a registered candidate whose motivating pages never mention it."""
    tagged = []
    for slug in slugs:
        path = sl.pattern_path(lib_root, repo, slug)
        with open(path, encoding="utf-8") as fh:
            meta, sections = sl.parse_pattern(fh.read())
        skills = list(meta.get("skills") or [])
        if name not in skills:
            skills.append(name)
        meta["skills"] = skills
        text = sl.render_pattern(meta, sections)
        errs = sl.lint_pattern(slug, text)
        if errs:
            raise Fail(f"pattern {slug} would not lint after tagging: {errs[0]}")
        tagged.append((path, text))
    return tagged


def _write_patterns(tagged):
    for path, text in tagged:
        sl.write_text_atomic(path, text)


def _event_details(ev, proposal, extra=None):
    d = {"action": proposal["action"], "result_sha256": ev.get("result_sha256"),
         "ops_sha256": ev.get("ops_sha256"), "ops": proposal["ops"],
         "motivating_patterns": proposal["motivating_patterns"]}
    if ev.get("diff"):
        d["diff"] = _clip(ev["diff"])
    d.update(extra or {})
    return d


# --- subcommands --------------------------------------------------------------
def cmd_status(a):
    if not sl.skeleton_present(a.lib, a.repo):
        raise Fail("no library")
    index = _index(a.lib, a.repo)
    skills = index["skills"]
    counts = {s: sum(1 for e in skills.values() if e.get("status") == s) for s in sl.STATUSES}
    print(f"SKILL_INDEX=OK candidate={sl.one_candidate(index) or 'none'} "
          f"active={counts['active']} rolled_back={counts['rolled_back']}")
    return 0


def cmd_git_check(a):
    p = sl.paths(a.lib, a.repo)
    top = sl.git_toplevel(p["repo_dir"]) or sl.git_toplevel(p["root"])
    if top is None:
        print("LIBRARY_GIT=FAIL not a git repository")
        return 1
    if sl.git_dirty(a.lib, a.repo):
        print("LIBRARY_GIT=FAIL dirty")
        return 1
    head = sl.git_head(p["repo_dir"]) or sl.git_head(p["root"]) or "none"
    print(f"LIBRARY_GIT=OK head={head[:12]} clean=yes")
    return 0


def cmd_gate(a):
    if not os.path.isdir(a.out):
        raise Fail(f"--out is not a directory: {a.out}")
    if a.record and not a.evolve_run:
        raise Fail("--record needs --evolve-run")
    result_path = os.path.join(a.out, "proposal-gate-result.json")
    for stale in ("candidate-SKILL.md", "candidate.diff"):
        if os.path.exists(os.path.join(a.out, stale)):
            os.remove(os.path.join(a.out, stale))
    raw = _load_json(a.proposal, "proposal")
    out = {"schema": "archon.proposal-gate-result.v1", "result": "FAIL", "action": None, "skill": None,
           "reason": "", "result_sha256": None, "ops_sha256": None, "traces_read": 0, "repeat_of": None}
    proposal = None
    try:
        proposal = parse_proposal(raw)
        out.update(action=proposal["action"], skill=proposal["skill"],
                   traces_read=len(set(proposal["traces_read"])))
        ev = evaluate(a.lib, a.repo, proposal)
    except Fail as exc:
        reason = str(exc)
        built = getattr(exc, "built", {}) or {}
        out.update(reason=reason, result_sha256=built.get("result_sha256"), ops_sha256=built.get("ops_sha256"))
        if reason.startswith("repeat_of="):
            out["repeat_of"] = reason.split("=", 1)[1].split()[0]
        _write_json(result_path, out)
        if a.record and proposal is not None:
            details = {"action": proposal["action"], "reason": reason, "ops": proposal["ops"],
                       "result_sha256": built.get("result_sha256"), "ops_sha256": built.get("ops_sha256")}
            if built.get("diff"):
                details["diff"] = _clip(built["diff"])
            if reason == INJECTION_REFUSAL:
                # Both the diff and the line ops carry the refused text verbatim,
                # and skill-impact.md renders every detail back out. Drop them and
                # keep the shas, which are what the repeat check reads.
                details.pop("diff", None)
                details.pop("ops", None)
            pid = sl.proposal_id(a.evolve_run)
            sl.record_impact(a.lib, a.repo, "gate_failed", proposal["skill"], pid, a.evolve_run,
                             reason=reason, details=details)
            sl.append_log(a.lib, a.repo, f"skill: gate failed for {proposal['skill']} ({pid}): {reason}")
        print(f"PROPOSAL_GATE=FAIL {reason}")
        return 1
    out.update(result="PASS", result_sha256=ev["result_sha256"], ops_sha256=ev["ops_sha256"])
    if ev["action"] != "no_action":
        sl.write_text_atomic(os.path.join(a.out, "candidate-SKILL.md"), ev["result_text"])
        sl.write_text_atomic(os.path.join(a.out, "candidate.diff"), ev["diff"])
    _write_json(result_path, out)
    sha = ev["result_sha256"][:12] if ev["result_sha256"] else NO_SKILL
    print(f"PROPOSAL_GATE=PASS action={ev['action']} skill={ev['skill']} ops={ev['ops']} "
          f"result_sha={sha} traces_read={out['traces_read']}")
    return 0


def cmd_record_no_action(a):
    proposal = parse_proposal(_load_json(a.proposal, "proposal"))
    if proposal["action"] != "no_action":
        raise Fail(f"proposal action is {proposal['action']}, not no_action")
    _index(a.lib, a.repo)
    pid = sl.proposal_id(a.evolve_run)
    sl.record_impact(a.lib, a.repo, "no_action", NO_SKILL, pid, a.evolve_run,
                     reason=proposal["rationale"][:MAX_RATIONALE],
                     details={"traces_read": proposal["traces_read"],
                              "motivating_patterns": proposal["motivating_patterns"]})
    sl.append_log(a.lib, a.repo, f"skill: no action ({pid})")
    print("SKILL_ADMIT=SKIP no_action")
    return 0


def parse_verdict(obj):
    if not isinstance(obj, dict):
        raise Fail("verdict is not an object")
    if obj.get("schema") != sl.SCHEMA_VERDICT:
        raise Fail(f"verdict schema {obj.get('schema')!r} != {sl.SCHEMA_VERDICT}")
    verdict = obj.get("verdict")
    if verdict not in VERDICTS:
        raise Fail(f"verdict {verdict!r} not in {VERDICTS}")
    reasons = obj.get("reasons", [])
    if not isinstance(reasons, list) or not all(isinstance(r, str) for r in reasons):
        raise Fail("reasons must be a list of strings")
    checks = obj.get("checks") if verdict != "SKIP" else (obj.get("checks") or {})
    if verdict != "SKIP":
        if not isinstance(checks, dict) or set(checks) != set(VERDICT_CHECKS) \
                or not all(isinstance(checks[k], bool) for k in VERDICT_CHECKS):
            raise Fail(f"checks must be exactly {list(VERDICT_CHECKS)} as booleans")
    return {"verdict": verdict, "reasons": list(reasons), "checks": dict(checks or {})}


def cmd_admit(a):
    pid = sl.proposal_id(a.evolve_run)
    gate = _load_json(a.gate, "gate result")
    summary_path = os.path.join(a.out, "skill-admit-result.json") if a.out else None

    def finish(line, code, **facts):
        if summary_path:
            _write_json(summary_path, dict({"schema": "archon.skill-admit-result.v1", "proposal": pid,
                                            "line": line, "exit": code}, **facts))
        print(line)
        return code

    if not isinstance(gate, dict) or gate.get("result") != "PASS":
        return finish("SKILL_ADMIT=SKIP gate did not pass", 3, outcome="skip")
    if gate.get("action") == "no_action":
        return finish("SKILL_ADMIT=SKIP no_action", 3, outcome="skip")
    proposal = parse_proposal(_load_json(a.proposal, "proposal"))
    verdict = parse_verdict(_load_json(a.verdict, "verdict"))
    if proposal["action"] == "no_action":
        return finish("SKILL_ADMIT=SKIP no_action", 3, outcome="skip")
    if verdict["verdict"] == "SKIP":
        return finish("SKILL_ADMIT=SKIP critic verdict SKIP", 3, outcome="skip")
    name = proposal["skill"]
    at = sl.utc_now()
    ev = evaluate(a.lib, a.repo, proposal, at=at)  # the library may have moved since the gate
    if gate.get("result_sha256") != ev["result_sha256"]:
        raise Fail("gate result no longer matches the proposal (library changed)")
    reasons = list(verdict["reasons"])
    accept = verdict["verdict"] == "ACCEPT"
    if accept and not all(verdict["checks"].get(k) is True for k in VERDICT_CHECKS):
        accept = False
        reasons.append("checks not all true")
    # Tag-first: render and lint the motivating pages before anything is
    # written, so a page that would break its caps once tagged refuses the
    # admission while the tree is still clean and the ledgers are still empty.
    tagged = _tag_patterns(a.lib, a.repo, proposal["motivating_patterns"], name) if accept else []
    sl.record_impact(a.lib, a.repo, "proposed", name, pid, a.evolve_run,
                     reason=proposal["rationale"],
                     details=_event_details(ev, proposal, {"traces_read": sorted(set(proposal["traces_read"]))}),
                     at=at)
    if not accept:
        sl.record_impact(a.lib, a.repo, "rejected", name, pid, a.evolve_run,
                         reason="; ".join(reasons) or "rejected by critic",
                         details=_event_details(ev, proposal, {"reasons": reasons}), at=at)
        sl.append_log(a.lib, a.repo, f"skill: rejected {proposal['action']} of {name} ({pid})", at=at)
        return finish(f"SKILL_ADMIT=REJECTED skill={name} reasons={len(reasons)}", 2,
                      outcome="rejected", skill=name, reasons=reasons)
    index = ev["index"]
    entry = _apply_candidate(a.lib, a.repo, index, name, ev["result_text"], proposal, at,
                             pid=pid, evolve_run=a.evolve_run)
    _write_patterns(tagged)
    sl.regenerate_index_md(a.lib, a.repo)
    sl.record_impact(a.lib, a.repo, "admitted", name, pid, a.evolve_run,
                     reason=f"candidate for {index['window_size']} runs",
                     details=_event_details(ev, proposal, {"baseline_runs": entry["scoring"]["baseline_runs"],
                                                           "envelope": entry["envelope"]}), at=at)
    sl.append_log(a.lib, a.repo, f"skill: admitted {proposal['action']} of {name} as candidate ({pid}), "
                                 f"window {index['window_size']}", at=at)
    return finish(f"SKILL_ADMIT=ACCEPTED skill={name} action={proposal['action']} window={index['window_size']}",
                  0, outcome="accepted", skill=name, action=proposal["action"],
                  window=index["window_size"], sha256=entry["sha256"])


def cmd_rollback(a):
    _refuse_under_a_foreign_lock(a)
    index = _index(a.lib, a.repo)
    name = sl.one_candidate(index)
    if not name:
        print("SKILL_ROLLBACK=SKIP no candidate")
        return 0
    at = sl.utc_now()
    entry = index["skills"][name]
    prior_sha, pid = entry.get("sha256"), entry.get("proposal")
    reason = a.reason or "forced by operator"
    outcome = sl.rollback_candidate(a.lib, a.repo, index, name, reason, at=at)
    sl.save_index(a.lib, a.repo, index)
    sl.record_impact(a.lib, a.repo, "forced_rollback", name, pid, a.evolve_run or NO_SKILL, reason=reason,
                     details={"result_sha256": prior_sha, "restored": outcome, "action": entry.get("action")},
                     at=at)
    sl.append_log(a.lib, a.repo, f"skill: forced rollback of {name} ({pid}): {outcome}; {reason}", at=at)
    print(f"SKILL_ROLLBACK=OK skill={name} result={outcome}")
    return 0


def cmd_set_window(a):
    _refuse_under_a_foreign_lock(a)
    if not 1 <= a.k <= MAX_WINDOW:
        raise Fail(f"window must be 1..{MAX_WINDOW}")
    index = _index(a.lib, a.repo)
    pending = sl.one_candidate(index)
    if pending:
        print(f"SKILL_WINDOW_SET=FAIL candidate pending: {pending}")
        return 1
    old = index["window_size"]
    index["window_size"] = a.k
    sl.save_index(a.lib, a.repo, index)
    sl.record_impact(a.lib, a.repo, "window_reset", NO_SKILL, NO_SKILL, NO_SKILL,
                     reason="operator set-window", details={"from": old, "to": a.k})
    sl.append_log(a.lib, a.repo, f"skill: window {old} -> {a.k}")
    print(f"SKILL_WINDOW_SET=OK window={a.k}")
    return 0


def cmd_commit(a):
    _refuse_under_a_foreign_lock(a)
    p = sl.paths(a.lib, a.repo)
    if sl.git_toplevel(p["root"]) is None:
        print("LIBRARY_COMMIT=SKIP no git")
        return 0
    if not sl.git_dirty(a.lib, a.repo):
        print("LIBRARY_COMMIT=SKIP nothing to commit")
        return 0
    # A commit is the last chance to stop an invalid registry from becoming
    # history: validate what is on disk before staging anything. Nothing is
    # added and the tree stays dirty, so the operator can discard the partial
    # write with the recipe in RUNBOOK section 17.
    _index(a.lib, a.repo)
    try:
        sha = sl.git_commit(a.lib, a.repo, a.message)
    except sl.LibraryError as exc:
        print(f"LIBRARY_COMMIT=FAIL {_one_line(exc)}")
        return 1
    if sha is None:
        print("LIBRARY_COMMIT=SKIP nothing to commit")
        return 0
    print(f"LIBRARY_COMMIT=OK sha={sha[:12]}")
    return 0


FAIL_KEY = {"status": "SKILL_INDEX", "git-check": "LIBRARY_GIT", "gate": "PROPOSAL_GATE",
            "record-no-action": "SKILL_ADMIT", "admit": "SKILL_ADMIT", "rollback": "SKILL_ROLLBACK",
            "set-window": "SKILL_WINDOW_SET", "commit": "LIBRARY_COMMIT"}


MORE = "Typed lines, exit codes and the gate rules: skill-admit.py --help"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], epilog=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="<command>")

    def parser(name, what):
        # The whole docstring -- typed lines, exit codes, gate rules -- is the
        # top-level epilog; a subcommand points at it rather than repeating it.
        return sub.add_parser(name, help=what, description=what, epilog=MORE,
                              formatter_class=argparse.RawDescriptionHelpFormatter)

    def common(sp, repo=True):
        if repo:
            sp.add_argument("repo", help="repository name, a directory under the library root")
        sp.add_argument("--lib", required=True, help="library root, the directory holding <repo>/")

    common(parser("status", "report the index: pending candidate, active and rolled back counts"))
    common(parser("git-check", "report the library repo's head and whether its tree is clean"))
    g = parser("gate", "run every mechanical check on a proposal; writes no library file")
    g.add_argument("repo", help="repository name, a directory under the library root")
    g.add_argument("proposal", help="the proposer's proposal.json")
    g.add_argument("--lib", required=True, help="library root, the directory holding <repo>/")
    g.add_argument("--out", required=True, help="artifacts directory for proposal-gate-result.json")
    g.add_argument("--record", action="store_true",
                   help="record a refusal in the ledgers so the proposal cannot be re-submitted")
    g.add_argument("--evolve-run", help="the skill-evolve run id, required by --record")
    n = parser("record-no-action", "record that a run proposed nothing, so the next run sees it")
    common(n)
    n.add_argument("--proposal", required=True, help="the proposal.json carrying action=none")
    n.add_argument("--evolve-run", required=True, help="the skill-evolve run id")
    m = parser("admit", "turn an ACCEPT verdict into exactly one pending candidate")
    common(m)
    m.add_argument("--proposal", required=True, help="the proposer's proposal.json")
    m.add_argument("--verdict", required=True, help="the critic's verdict.json")
    m.add_argument("--gate", required=True, help="proposal-gate-result.json from the gate step")
    m.add_argument("--evolve-run", required=True, help="the skill-evolve run id")
    m.add_argument("--out", help="artifacts directory for skill-admit-result.json")
    r = parser("rollback", "operator lever: undo the pending candidate, restoring the snapshot")
    common(r)
    r.add_argument("--reason", help="why it was rolled back; stored in the index and the ledgers")
    r.add_argument("--evolve-run", help="the run id holding the evolve lock, when one is held")
    w = parser("set-window", "operator lever: set K, the scoring window for the NEXT candidate")
    w.add_argument("repo", help="repository name, a directory under the library root")
    w.add_argument("k", type=int, help="window size in runs; refused while a candidate is pending")
    w.add_argument("--lib", required=True, help="library root, the directory holding <repo>/")
    w.add_argument("--evolve-run", help="the run id holding the evolve lock, when one is held")
    c = parser("commit", "operator lever: commit the library tree this script left behind")
    common(c)
    c.add_argument("--message", required=True, help="the commit message")
    c.add_argument("--evolve-run", help="the run id holding the evolve lock, when one is held")
    a = ap.parse_args(argv)
    fn = {"status": cmd_status, "git-check": cmd_git_check, "gate": cmd_gate,
          "record-no-action": cmd_record_no_action, "admit": cmd_admit, "rollback": cmd_rollback,
          "set-window": cmd_set_window, "commit": cmd_commit}[a.cmd]
    try:
        sl.check_repo_name(a.repo)
        return fn(a)
    except (Fail, sl.LibraryError) as exc:
        print(f"{FAIL_KEY[a.cmd]}=FAIL {_one_line(exc)}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
