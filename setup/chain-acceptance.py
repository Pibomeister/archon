#!/usr/bin/env python3
"""The "no findings remain" predicate for a repository-list chain.

It lived at the Goodword root's .omc/scripts/, outside .archon, which meant the
reviewers of every Archon PR could not see the thing that decides whether a live
run passed. It lives here now.

  chain-acceptance.py <chain-id>

Clauses, each checked per stage unless noted:
  1  no accept-residuals.txt and no yield-stop.txt (a human bypass was not used)
  2  waivers.md carries no headings (no finding was waived)
  3  no `failed`, `incomplete`, `cross_repo` or `pin_conflict` fixer partition in
     any round, and no residuals.json disposition
  4  the final converge.txt says CONVERGED round=N and nothing else
  5  smoke-result.txt starts SMOKE=PASS
  6  chain-wide: integration evidence `passed`, and every acceptance_criteria id
     covered by a scenario

Output contract:
  ACCEPTANCE=PASS
  ACCEPTANCE=PARTIAL failures=<n> residual_only=<true|false>
with one `FAIL <repo>: clause<k> …` line per failure. `residual_only=true` means
every failure named is a residuals.json disposition -- a P2/P3 the round
deliberately deferred, pinned or waived into residuals -- which is the only
PARTIAL the live gate tolerates. Anything else is a real failure wearing the
same line.
"""
import json
import os
import re
import sys
from pathlib import Path

CONTROL = Path(os.environ.get("ARCHON_CHAIN_CONTROL_DIR")
               or Path.home() / ".archon/control/codex-lite/feature-chains-v2")
# The fixer partitions that mean work is unfinished, blocked, or owned elsewhere.
BLOCKING_PARTITIONS = ("failed", "incomplete", "cross_repo", "pin_conflict")
RESIDUAL_MARKER = "residuals.json disposition"


def rounds(artifacts):
    return sorted((p for p in artifacts.glob("round-*") if p.is_dir()),
                  key=lambda p: int(p.name.split("-")[1]))


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def residual_dispositions(artifacts):
    """P2/P3 entries the run carried out instead of closing.

    Read tolerantly: a list of entries, or an object with a `residuals` list, or
    counts keyed by state. Each shape reduces to state -> count.
    """
    doc = read_json(artifacts / "residuals.json")
    if doc is None:
        return {}
    entries = doc if isinstance(doc, list) else doc.get("residuals") if isinstance(doc, dict) else None
    if entries is None and isinstance(doc, dict):
        return {k: v for k, v in doc.items() if isinstance(v, int) and v}
    counts = {}
    for entry in entries or []:
        state = str(entry.get("state", "unknown")) if isinstance(entry, dict) else "unknown"
        counts[state] = counts.get(state, 0) + 1
    return counts


def check_stage(repo, artifacts, fails):
    for name in ("accept-residuals.txt", "yield-stop.txt"):
        if (artifacts / name).exists():
            fails.append(f"{repo}: clause1 {name} present")
    waivers = artifacts / "waivers.md"
    if waivers.exists() and any(line.startswith("## ")
                                for line in waivers.read_text(encoding="utf-8").splitlines()):
        fails.append(f"{repo}: clause2 waivers.md has entries")
    stage_rounds = rounds(artifacts)
    for round_dir in stage_rounds:
        result = read_json(round_dir / "fixer-result.json")
        if not isinstance(result, dict):
            continue
        for key in BLOCKING_PARTITIONS:
            if result.get(key):
                fails.append(f"{repo}: clause3 {round_dir.name} {key} non-empty")
    for state, count in sorted(residual_dispositions(artifacts).items()):
        fails.append(f"{repo}: clause3 {RESIDUAL_MARKER} {state}={count}")
    converge = ""
    if stage_rounds and (stage_rounds[-1] / "converge.txt").exists():
        converge = (stage_rounds[-1] / "converge.txt").read_text(encoding="utf-8")
    if not re.search(r"^CONVERGED round=\d+$", converge, re.M):
        last = converge.strip().splitlines()[-1] if converge.strip() else "missing"
        fails.append(f"{repo}: clause4 final converge not clean: {last}")
    smoke_path = artifacts / "smoke-result.txt"
    smoke = smoke_path.read_text(encoding="utf-8").strip() if smoke_path.exists() else ""
    if not smoke.startswith("SMOKE=PASS"):
        fails.append(f"{repo}: clause5 smoke={smoke!r}")
    if repo == "goodword-mcp" and "mcp-boot=ok favicon=200 unauth-mcp=401" not in smoke:
        fails.append(f"{repo}: clause5 mcp smoke line unexpected: {smoke!r}")


def check_chain(state, fails):
    evidence = (state.get("integration") or {}).get("integration_evidence") or {}
    if evidence.get("status") != "passed":
        fails.append("clause6 integration not passed")
    plan = state.get("approved_plan") or {}
    declared = {c.get("id") for c in plan.get("acceptance_criteria", [])}
    covered = {cid for scenario in plan.get("integration", {}).get("scenarios", [])
               for cid in scenario.get("covers", [])}
    if declared - covered:
        fails.append(f"clause6 uncovered criteria: {sorted(declared - covered)}")
    if not declared:
        fails.append("clause6 plan declares no acceptance_criteria")


def main(chain_id):
    state = json.loads((CONTROL / f"{chain_id}.json").read_text(encoding="utf-8"))
    fails = []
    stages = {repo: Path(handoff["artifacts"])
              for repo, handoff in state.get("candidate_handoffs", {}).items()}
    for repo, artifacts in stages.items():
        check_stage(repo, artifacts, fails)
    check_chain(state, fails)
    print(f"status={state.get('status')} stages={list(stages)}")
    for line in fails:
        print("FAIL", line)
    if not fails:
        print("ACCEPTANCE=PASS")
        return 0
    residual_only = all(RESIDUAL_MARKER in line for line in fails)
    print(f"ACCEPTANCE=PARTIAL failures={len(fails)} "
          f"residual_only={'true' if residual_only else 'false'}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
