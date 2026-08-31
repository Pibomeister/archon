#!/usr/bin/env python3
"""Per-run token accounting for codex lanes (limitation follow-up: the codex
adapter has no costControl, so quota burn is otherwise invisible).

Usage: codex-usage.py <run-id-prefix> [--codex-home DIR] [--db PATH] [--json]

Reads the run's start/end from archon's sqlite, then sums the FINAL
token_count event of every session rollout the dedicated codex home wrote
inside that window (each workflow node = its own session/thread, so the sum
over sessions is the run's burn — valid because the home is dedicated and
one-run-per-root serializes lanes).

Prints one typed line:
  CODEX_USAGE run=<id> sessions=N input=... cached=... output=... total=... wall_s=... rate_used_pct=<last observed weekly %>
"""
import argparse
import datetime
import glob
import json
import os
import sqlite3
import sys


def parse_ts(s):
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def run_window(db, run_prefix):
    """Window = the run's own event stream (min/max created_at). The runs row is
    NOT usable: a resume rewrites started_at, so a resumed run's row covers only
    the last process and under-counts every earlier session."""
    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT id FROM remote_agent_workflow_runs WHERE id LIKE ? ORDER BY started_at DESC LIMIT 1",
        (run_prefix + "%",),
    ).fetchone()
    if not row:
        con.close()
        raise SystemExit(f"CODEX_USAGE=FAIL no run matching {run_prefix!r}")
    ev = con.execute(
        "SELECT min(created_at), max(created_at) FROM remote_agent_workflow_events WHERE workflow_run_id = ?",
        (row[0],),
    ).fetchone()
    con.close()
    if not ev or not ev[0]:
        raise SystemExit(f"CODEX_USAGE=FAIL run {row[0][:8]} has no recorded events")
    start = parse_ts(ev[0]).replace(tzinfo=datetime.timezone.utc)
    end = parse_ts(ev[1]).replace(tzinfo=datetime.timezone.utc)
    return row[0], start, end


def session_files(home, start, end, slack_s=120):
    files = glob.glob(os.path.join(home, "sessions", "*", "*", "*", "*.jsonl"))
    out = []
    for f in files:
        m = datetime.datetime.fromtimestamp(os.path.getmtime(f), tz=datetime.timezone.utc)
        if start - datetime.timedelta(seconds=slack_s) <= m <= end + datetime.timedelta(seconds=slack_s):
            out.append(f)
    return sorted(out)


def last_usage(path):
    """Final cumulative token usage + last seen rate-limit percent in one rollout."""
    usage, pct = None, None
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if '"token_count"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            info = (d.get("payload") or {}).get("info") or {}
            u = info.get("total_token_usage")
            if u:
                usage = u
            rl = (d.get("payload") or {}).get("rate_limits") or {}
            p = (rl.get("primary") or {}).get("used_percent")
            if p is not None:
                pct = p
    return usage, pct


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_prefix")
    ap.add_argument("--codex-home", default=os.environ.get("CODEX_HOME", os.path.expanduser("~/.archon/codex-home")))
    ap.add_argument("--db", default=os.path.expanduser("~/.archon/archon.db"))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    run_id, start, end = run_window(a.db, a.run_prefix)
    tot = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    pct = None
    files = session_files(a.codex_home, start, end)
    for f in files:
        u, p = last_usage(f)
        if u:
            for k in tot:
                tot[k] += u.get(k, 0)
        if p is not None:
            pct = p
    wall = int((end - start).total_seconds())
    if a.json:
        print(json.dumps({"run": run_id, "sessions": len(files), **tot, "wall_s": wall, "rate_used_pct": pct}))
    else:
        print(f"CODEX_USAGE run={run_id[:8]} sessions={len(files)} input={tot['input_tokens']} "
              f"cached={tot['cached_input_tokens']} output={tot['output_tokens']} total={tot['total_tokens']} "
              f"wall_s={wall} rate_used_pct={pct if pct is not None else 'n/a'}")


if __name__ == "__main__":
    main()
