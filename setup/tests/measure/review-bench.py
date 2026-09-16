#!/usr/bin/env python3
"""Measure one review prompt's cost on one fixed candidate, outside the lane.

Item 2 of the two-hour plan picks the review topology by measurement, not by
argument. The thing being measured has to be the prompt body that will ship and
nothing else: no engine, no loop, no resume, no other node's latency folded in.
So this harness takes a prompt FILE, seeds the round files the prompt reads,
stages the skills the way archon-run.py does, spawns the provider
non-interactively against a fixed worktree, and times spawn -> the envelope's
`Review complete` line.

Timing is read from the streamed events rather than from process exit: the
prompt's last action is a `mark review-done` call that happens after the
envelope, and a review that took 8 minutes to produce an envelope did not cost
9 because a marker write followed it. Total wall is recorded alongside so the
gap is visible rather than assumed.

`{{SETUP}}` in the prompt is substituted with a scratch setup directory laid out
so `{{SETUP}}/review-contract.md` is the real contract (symlinked, so its digest
is the shipping one) and `{{SETUP}}/../vendor/` is the real vendor tree. The
scratch directory also holds a `round-state.py` that records the two marks and
nothing else: the lane's helper is another worker's file, and a bench that
depended on it would measure their delivery date rather than this prompt.

Usage:
  review-bench.py --prompt setup/prompts/review-trio.md \\
      --worktree /path/to/candidate --base <sha> --head <sha> \\
      --label api-trio --runs 3 [--scope full|verify] [--model sonnet] \\
      [--timeout-s 900] [--budget-usd 8] [--skill ce-code-review] \\
      [--plan <plan.md>] [--joint-plan <joint-plan.json>] \\
      [--allowlist <files-allowlist.json>] [--findings <ledger.json>] \\
      [--out setup/tests/measure/runs]

Prints one `BENCH` line per run and a `BENCH_SUMMARY` line with p50/p90, and
writes <out>/<label>/run-<k>.json plus the raw envelope per run.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ARCHON = Path(__file__).resolve().parents[3]
GOODWORD = ARCHON.parent
SEVERITY = ("P0", "P1", "P2", "P3")
# `[P1] Section: ... — title (lens, confidence 75)` and `**P1**: title`, the two
# shapes the CE markdown envelope uses. JSON findings are read separately.
MD_FINDING = re.compile(r"^\s*(?:[-*]\s*)?[\[*`]*\b(P[0-3])\b[\]*`:]*\s*(.+?)\s*$", re.M)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def git(worktree, *args):
    p = run(["/usr/bin/git", "-C", str(worktree), *args])
    if p.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed in {worktree}: {p.stderr.strip()}")
    return p.stdout.strip()


def stage_skills(worktree, skills):
    """Copy the staged Claude skills into the candidate the way archon-run.py
    stages them at the project root, and keep them out of `git status` so the
    reviewer does not read its own tooling as part of the candidate."""
    if not skills:
        return
    dest_root = Path(worktree) / ".claude" / "skills"
    dest_root.mkdir(parents=True, exist_ok=True)
    for skill in skills:
        src = GOODWORD / ".claude" / "skills" / skill
        if not (src / "SKILL.md").is_file():
            raise SystemExit(f"staged skill missing: {src}/SKILL.md")
        dest = dest_root / skill
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
    gitdir = Path(git(worktree, "rev-parse", "--git-dir"))
    if not gitdir.is_absolute():
        gitdir = Path(worktree) / gitdir
    exclude = gitdir / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    text = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    if ".claude/" not in text:
        exclude.write_text(text + "\n.claude/\n", encoding="utf-8")


SCHEMA = Path("vendor/ce-skills/3.2.0/ce-code-review/references/findings-schema.json")


def scratch_setup(root):
    """`<root>/setup` with the contract, the findings schema and a marker-only
    round-state.py, so every `{{SETUP}}`-relative path in a prompt resolves
    without leaving this directory.

    Copies, not symlinks, and `root` belongs outside every checkout. A symlink
    into the real tree is a path the session follows: measured 2026-09-16, the
    first trio run resolved `{{SETUP}}` back into the .archon worktree, spent
    four minutes reading this harness's own source, and billed it to the review.
    A bench whose subject can read the bench is measuring the wrong thing.
    """
    setup = root / "setup"
    setup.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ARCHON / "setup" / "review-contract.md",
                    setup / "review-contract.md")
    schema = root / SCHEMA
    schema.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ARCHON / SCHEMA, schema)
    (setup / "round-state.py").write_text(
        "#!/usr/bin/env python3\n"
        "# Bench stub: record the mark and its timestamp. The lane's helper does\n"
        "# far more; none of it is what this bench measures.\n"
        "import json, sys, time, pathlib\n"
        "ad = pathlib.Path(sys.argv[1])\n"
        "args = sys.argv[2:]\n"
        "ad.mkdir(parents=True, exist_ok=True)\n"
        "with open(ad / 'marks.jsonl', 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps({'t': time.time(), 'args': args}) + '\\n')\n"
        "print('MARK ' + ' '.join(args))\n",
        encoding="utf-8")
    return setup


def review_id(head, base, scope, contract_digest, plan_digest, allowlist_digest):
    return hashlib.sha256("|".join(
        [head, base, scope, contract_digest, plan_digest, allowlist_digest]
    ).encode("utf-8")).hexdigest()


def digest(path):
    if path and Path(path).is_file():
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    return hashlib.sha256(b"").hexdigest()


def seed_artifacts(ad, a, head):
    """The round files the prompts read. Round is always 1: the bench measures
    one review, and a round number the prompt never compares against anything
    would be theatre."""
    ad.mkdir(parents=True, exist_ok=True)
    rd = ad / "round-1"
    rd.mkdir(exist_ok=True)
    (ad / "round.txt").write_text("1\n", encoding="utf-8")
    (ad / "params.json").write_text(json.dumps({"worktree": str(a.worktree)}), encoding="utf-8")
    (ad / "bootstrap-head.txt").write_text(a.base + "\n", encoding="utf-8")
    for name, src in (("plan.md", a.plan), ("joint-plan.json", a.joint_plan),
                      ("files-allowlist.json", a.allowlist)):
        if src:
            shutil.copyfile(src, ad / name)
    contract = digest(ARCHON / "setup" / "review-contract.md")
    rid = review_id(head, a.base, a.scope, contract,
                    digest(a.plan), digest(a.allowlist))
    (rd / "review-input.json").write_text(json.dumps({
        "review_head": head, "review_tree": "", "base": a.base, "scope": a.scope,
        "contract_digest": contract, "plan_digest": digest(a.plan),
        "allowlist_digest": digest(a.allowlist), "attempt": 1, "id": rid,
    }, indent=2), encoding="utf-8")
    (rd / "review-scope.txt").write_text(a.scope + "\n", encoding="utf-8")
    (rd / "review-base.txt").write_text(a.base + "\n", encoding="utf-8")
    (rd / "pre-head.txt").write_text(head + "\n", encoding="utf-8")
    if a.findings:
        shutil.copyfile(a.findings, rd / "review-findings.json")
    return rid


def emitted_review_complete(ev):
    """`Review complete` as the model's own output, not as an echo of the prompt.

    Every prompt here QUOTES the footer it must emit, and the session's first
    stream event is the prompt. Matching the phrase anywhere in the stream timed
    the echo and reported a 12-minute review as 0.2 minutes. Only assistant text
    counts, and only as a line of its own.
    """
    if ev.get("type") != "assistant":
        return False
    content = (ev.get("message") or {}).get("content") or []
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "text":
            if re.search(r"(?m)^\s*Review complete\s*$", block.get("text", "")):
                return True
    return False


def spawn(prompt, a, cwd, env, timeout_s, envelope_path, err_path):
    """Run the provider and return (seconds to the envelope, wall, result, events).

    The stream is read line by line so a run that stalls is visible while it
    stalls, not 15 minutes later.
    """
    cmd = [a.claude_bin, "-p", prompt, "--model", a.model,
           "--output-format", "stream-json", "--verbose",
           "--permission-mode", "bypassPermissions"]
    t0, t0_wall = time.monotonic(), time.time()
    stream_at = None
    events = []
    result = None
    # stderr goes to a file, not a second pipe: a child that fills a 64 KB
    # stderr buffer blocks writing it while this loop is still draining stdout,
    # and the two wait on each other for the rest of the run.
    errf = open(err_path, "w+", encoding="utf-8")
    p = subprocess.Popen(cmd, cwd=str(cwd), env=env, stdout=subprocess.PIPE,
                         stderr=errf, text=True, bufsize=1)
    # The deadline lives in a watchdog, not in the read loop: a session that
    # stalls emits no lines, so a timeout checked per line is a timeout that
    # never fires. A killed process closes the pipe and ends the loop.
    killed = threading.Event()

    def watchdog():
        if not killed.wait(timeout_s):
            killed.set()
            p.kill()

    threading.Thread(target=watchdog, daemon=True).start()
    try:
        for line in p.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            events.append(ev)
            if ev.get("type") == "result":
                result = ev
            if stream_at is None and emitted_review_complete(ev):
                stream_at = time.monotonic() - t0
                print(f"    .. envelope emitted at {stream_at / 60:.1f} min", flush=True)
        p.wait(timeout=60)
    finally:
        was_killed = killed.is_set()
        killed.set()
    wall = time.monotonic() - t0
    if was_killed:
        return None, wall, {"timeout": True}, events
    errf.flush()
    errf.seek(0)
    stderr = errf.read()
    errf.close()
    if result is None and p.returncode != 0:
        result = {"is_error": True, "stderr": stderr[-2000:]}
    # The envelope file's mtime is when the review finished, independent of how
    # the model chose to narrate afterwards. The stream signal is the fallback
    # for a session that relayed the envelope without writing the file.
    at = None
    if envelope_path.is_file() and envelope_path.stat().st_size > 0:
        at = envelope_path.stat().st_mtime - t0_wall
    if at is None or at <= 0:
        at = stream_at
    return at, wall, result or {}, events


def envelope_text(ad, result):
    """The envelope file the prompt was told to write; the final result text is
    the fallback, because a session that failed to write the file still billed
    the time and its output is the only evidence of what it did."""
    f = ad / "round-1" / "review-envelope.txt"
    if f.is_file() and f.stat().st_size > 0:
        return f.read_text(encoding="utf-8", errors="replace")
    return str(result.get("result", ""))


def findings_in(text):
    """Every finding the envelope states, as (severity, title), from both the
    JSON shape and the CE markdown shape. Both are collected because the two
    modes emit different ones and parity has to compare the same population."""
    out = []
    dec = json.JSONDecoder()

    def take(f):
        if isinstance(f, dict) and f.get("severity") in SEVERITY and f.get("title"):
            out.append((f["severity"], str(f["title"])[:160],
                        str(f.get("finding_id", ""))))

    # Both container shapes occur and neither is wrong: the schema's wrapper
    # object, and the bare array a session emits when it relays only the
    # findings list. Scanning for `{` alone found the wrapper and missed the
    # array entirely, which read as "this mode found nothing".
    for m in re.finditer(r"[\{\[]", text):
        try:
            obj, _ = dec.raw_decode(text, m.start())
        except ValueError:
            continue
        if isinstance(obj, list):
            for f in obj:
                take(f)
            continue
        if not isinstance(obj, dict):
            continue
        if isinstance(obj.get("findings"), list):
            for f in obj["findings"]:
                take(f)
        take(obj)
    for sev, rest in MD_FINDING.findall(text):
        rest = rest.strip()
        # A bare severity token on its own line, or a table row, is not a finding.
        if len(rest) > 15 and not rest.startswith("|"):
            out.append((sev, rest[:160], ""))
    seen, uniq = set(), []
    for sev, title, fid in out:
        k = (sev, title.casefold())
        if k not in seen:
            seen.add(k)
            uniq.append({"severity": sev, "title": title, "finding_id": fid})
    return uniq


def pct(values, q):
    """Nearest-rank percentile. Three runs do not support interpolation, and an
    interpolated p90 of three samples would read as more precision than it is."""
    if not values:
        return None
    s = sorted(values)
    i = max(0, min(len(s) - 1, int(-(-q * len(s) // 100)) - 1))
    return s[i]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--worktree", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--label", required=True)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--scope", default="full", choices=("full", "verify"))
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--timeout-s", type=int, default=900)
    ap.add_argument("--budget-usd", type=float, default=8.0)
    ap.add_argument("--skill", action="append", default=[],
                    help="staged Claude skill the prompt needs; repeatable")
    ap.add_argument("--plan")
    ap.add_argument("--joint-plan")
    ap.add_argument("--allowlist")
    ap.add_argument("--findings", help="ledger for a verify run")
    ap.add_argument("--claude-bin", default=shutil.which("claude") or "claude")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "runs"))
    a = ap.parse_args()

    wt = Path(a.worktree).resolve()
    head = git(wt, "rev-parse", a.head)
    before = git(wt, "rev-parse", "HEAD") + "|" + git(wt, "status", "--porcelain")
    stage_skills(wt, a.skill)
    body = Path(a.prompt).read_text(encoding="utf-8")

    outdir = Path(a.out) / a.label
    outdir.mkdir(parents=True, exist_ok=True)
    print(f"BENCH_START label={a.label} prompt={Path(a.prompt).name} model={a.model} "
          f"runs={a.runs} scope={a.scope} worktree={wt} base={a.base[:10]} head={head[:10]}",
          flush=True)

    rows = []
    for k in range(1, a.runs + 1):
        scratch = Path(a.out) / a.label / f"scratch-{k}"
        if scratch.exists():
            shutil.rmtree(scratch)
        setup = scratch_setup(scratch)
        ad = scratch / "artifacts"
        rid = seed_artifacts(ad, a, head)
        prompt = body.replace("{{SETUP}}", str(setup))
        env = dict(os.environ, ARTIFACTS_DIR=str(ad))
        print(f"  run {k}/{a.runs} id={rid[:12]} ...", flush=True)
        secs, wall, result, events = spawn(prompt, a, wt, env, a.timeout_s,
                                           ad / "round-1" / "review-envelope.txt",
                                           outdir / f"run-{k}-stderr.txt")
        text = envelope_text(ad, result)
        after = git(wt, "rev-parse", "HEAD") + "|" + git(wt, "status", "--porcelain")
        fnd = findings_in(text)
        row = {
            "label": a.label, "run": k, "scope": a.scope, "model": a.model,
            "prompt": str(a.prompt), "review_id": rid,
            "seconds_to_envelope": secs, "wall_seconds": wall,
            "minutes": (secs / 60) if secs else None,
            "timed_out": bool(result.get("timeout")),
            "is_error": bool(result.get("is_error")),
            "cost_usd": result.get("total_cost_usd"),
            "num_turns": result.get("num_turns"),
            "envelope_bytes": len(text),
            "has_footer": bool(re.search(r"(?m)^Review complete\s*$", text)),
            "input_line_matches": f"Input: {rid}" in text,
            "head_line_matches": f"Head: {head}" in text,
            "verdict": (lambda m: m.group(1) if m else "")(re.search(
                r"Verdict:\s*[*_`]*\s*(Ready to merge|Ready with fixes|Not ready)", text)),
            "tree_moved": after != before,
            "findings": fnd,
            "p0p1": [f for f in fnd if f["severity"] in ("P0", "P1")],
            "marks": [json.loads(l) for l in
                      (ad / "marks.jsonl").read_text(encoding="utf-8").splitlines()]
            if (ad / "marks.jsonl").is_file() else [],
        }
        rows.append(row)
        (outdir / f"run-{k}.json").write_text(json.dumps(row, indent=2), encoding="utf-8")
        (outdir / f"run-{k}-envelope.txt").write_text(text, encoding="utf-8")
        (outdir / f"run-{k}-events.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events[-400:]), encoding="utf-8")
        print(f"BENCH label={a.label} run={k} "
              f"min={row['minutes'] if row['minutes'] is None else round(row['minutes'], 2)} "
              f"wall_min={round(wall / 60, 2)} verdict=[{row['verdict']}] "
              f"footer={row['has_footer']} input_ok={row['input_line_matches']} "
              f"head_ok={row['head_line_matches']} tree_moved={row['tree_moved']} "
              f"findings={len(fnd)} p0p1={len(row['p0p1'])} "
              f"cost={row['cost_usd']}", flush=True)
        for f in row["p0p1"]:
            print(f"    {f['severity']} {f['title']}", flush=True)

    mins = [r["minutes"] for r in rows if r["minutes"]]
    p50, p90 = pct(mins, 50), pct(mins, 90)
    summary = {"label": a.label, "runs": len(rows), "completed": len(mins),
               "p50_min": p50, "p90_min": p90, "minutes": mins,
               "prompt": str(a.prompt), "scope": a.scope, "worktree": str(wt),
               "base": a.base, "head": head, "model": a.model,
               # Recorded, not enforced: the lane's engine applies maxBudgetUsd,
               # the provider CLI has no equivalent flag. The measured
               # `cost_usd` per run is what says whether the budget holds.
               "budget_usd_configured": a.budget_usd,
               "timeout_s": a.timeout_s}
    (outdir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"BENCH_SUMMARY label={a.label} completed={len(mins)}/{len(rows)} "
          f"p50={None if p50 is None else round(p50, 2)} "
          f"p90={None if p90 is None else round(p90, 2)} "
          f"runs_min={[round(m, 2) for m in mins]}", flush=True)
    return 0 if mins else 1


if __name__ == "__main__":
    sys.exit(main())
