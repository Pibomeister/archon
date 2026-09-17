#!/usr/bin/env python3
"""Name WHY an AI node left no output, for the gate that finds it missing.

Run f07acb10: the Claude `ralplan` node dispatched Explore agents that went to
the background, polled them with ScheduleWakeup, and ended its turn. Archon
logged dag.node_result_with_live_background_tasks, waited, then recorded the
node COMPLETED -- and the operator saw only `SNAPSHOT=FAIL no plan.md` one node
later, which reads like a planner that forgot a file and suggests a resume that
can never help (completed AI nodes do not re-run).

The engine-level prevention is CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1 in
.archon/.env (archon loads it for every run started from the Goodword root).
This helper is the belt: it reads the node's recorded tool calls from archon.db
and prints one typed line the failing gate echoes before its own FAIL:

  NODE_BACKGROUNDED_NO_OUTPUT node=<id> run=<run> evidence=<tools> missing=<files>
      the node waited on background work; its outputs were never written
  NODE_NO_OUTPUT node=<id> run=<run> missing=<files>
      no background evidence (or no event record): an ordinary missing artifact

Always exits 0 -- it is a diagnosis, never the gate. Usage:
  node-no-output.py <artifacts-dir> <node-id> <missing-file> [...]
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

# Tools that only make sense while something runs in the background.
BACKGROUND_TOOLS = {"ScheduleWakeup", "Monitor", "ListAgents", "TaskOutput"}


def evidence(db, run_id, node):
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT data FROM remote_agent_workflow_events WHERE workflow_run_id = ? "
            "AND step_name = ? AND event_type = 'tool_called'", (run_id, node)).fetchall()
    except sqlite3.Error:
        return None
    found = set()
    for (data,) in rows:
        try:
            call = json.loads(data or "{}")
        except ValueError:
            continue
        name = call.get("tool_name")
        tool_input = call.get("tool_input") if isinstance(call.get("tool_input"), dict) else {}
        if name in BACKGROUND_TOOLS:
            found.add(name)
        elif tool_input.get("run_in_background") is True:
            found.add(f"{name}(run_in_background)")
    return sorted(found)


def main(argv):
    if len(argv) < 3:
        print("usage: node-no-output.py <artifacts-dir> <node-id> <missing-file> [...]")
        return 0
    ad, node, missing = Path(argv[0]), argv[1], ",".join(argv[2:])
    run_id = ad.resolve().name
    db = os.environ.get("ARCHON_DB") or str(Path.home() / ".archon" / "archon.db")
    found = evidence(db, run_id, node) if Path(db).is_file() else None
    if found:
        print(f"NODE_BACKGROUNDED_NO_OUTPUT node={node} run={run_id} evidence={'+'.join(found)} "
              f"missing={missing} (the node ended its turn waiting on background work, so its "
              f"outputs were never written; a plain resume cannot re-run a completed AI node. "
              f"Planning run of a chain: python3 .archon/setup/archon-run.py feature-replan {run_id} "
              f"--chain <chain-id> (claude) or --token <token> (codex); otherwise relaunch)")
    else:
        print(f"NODE_NO_OUTPUT node={node} run={run_id} missing={missing}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
