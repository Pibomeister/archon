"""Derive only the review loop of a captured repository-list workflow."""
from __future__ import annotations

import copy
from pathlib import Path
import shlex


class DeltaWorkflowError(ValueError):
    pass


def _command(setup: Path, action: str) -> str:
    helper = shlex.quote(str(setup / "review_delta_runtime.py"))
    return f'set -euo pipefail\npython3 {helper} {action} --artifacts "$ARTIFACTS_DIR"\n'


def _review_prompt(slot: int, setup: Path) -> str:
    return f"""ARCHON_RISK_DELTA_SLOT={slot}
Review assignment {slot} under risk-delta-v1. Work read-only.
Read $ARTIFACTS_DIR/current-review.json and only your assignment's bounded brief.
The brief supplies the exact worktree, candidate base/head, diff, responsibilities,
open finding IDs, invariants, and evidence references. Verify HEAD equals the
brief candidate before reviewing. Never substitute origin/main or bootstrap-head
for the brief's base after repairs. Completed initial coverage may have findings.

Invoke the existing ce-code-review skill with its explicit supported arguments:
mode:report-only base:<brief.candidate.base> plan:<brief.approved_plan>
The assigned responsibilities replace default persona selection for this call.
Perform this assignment in this independent session; do not launch subreviewers,
fix code, rerun shared mechanical checks, or reread the entire original diff.
Inspect affected callers/readers/contracts and any changed risk boundaries.
Missing impact evidence requires a bounded caller/reader trace. Request scope
expansion with concrete evidence if impact is unbounded or a baseline is invalid;
never claim coverage for source you did not inspect.

Use Sol/medium. Keep source findings' explicit IDs, invariant, paths, severity,
evidence and owning repository. Do not drop an unresolved source blocker because
validation is malformed or a similar title was previously reported. Independent
closure must name the repair commit and committed defect regression evidence.
Retain raw output immediately at the assignment's response path in current-review.json,
using the response schema in that brief. A malformed response gets one format-only
repair from its original bytes; persistent malformed output remains unresolved.
The following trusted checkpoint node validates and retains this response.
Do not call private-controller helpers from this worker session or alter another
assignment's records. Report completion only after saving the raw response.
"""


FIXER_PROMPT = """ARCHON_RISK_DELTA_ROLE=fixer
Repair only mandatory open findings in the current risk-delta-v1
ledger and repair packets referenced by $ARTIFACTS_DIR/current-review.json.
Use the exact worktree from params.json. Before editing, validate packet paths
against files-allowlist.json, including required test, contract and configuration
counterparts. Missing authority is scope-amendment-required, never advisory.
Preserve cross-repository ownership and return it through cross_repo records.
Every behavioral repair needs a committed regression exercising that defect.
Temporary probes supplement committed regressions. Do not change the approved
design or API-to-MCP order. No commits or branches; commit-fixer owns commits.
P3 is advisory unless an explicit project gate makes it mandatory. P0-P2 may not
be waived due to scope, environment failure, prior existence, or low finding count.
Use prior repair attempts by finding ID. Two attempts without progress require
bounded diagnosis, not another full review. Fixers never close their own findings.
Write the existing round-<N>/fixer-result.json contract (applied, failed, advisory,
incomplete, cross_repo), retaining finding IDs, severity, action and regression
evidence. With no mandatory open findings write empty arrays and change nothing.
End with FIXER_DONE only after saving the result. All changed code will receive
independent delta review; no yield-stop or residual shortcut can bypass it.
"""


def transform(workflow: dict, setup: Path) -> dict:
    if workflow.get("provider") != "codex":
        raise DeltaWorkflowError("risk-delta-v1 requires a captured Codex workflow")
    output = copy.deepcopy(workflow)
    loops = [node for node in output.get("nodes", []) if node.get("id") == "review-loop"]
    if len(loops) != 1:
        raise DeltaWorkflowError("captured workflow needs exactly one review-loop")
    loop = loops[0].get("loop_group")
    if not isinstance(loop, dict):
        raise DeltaWorkflowError("captured review-loop must be a loop group")
    nodes = loop.get("nodes", [])
    expected = ["round-pre", "review", "review-gate", "commit-fixes", "fixer", "commit-fixer", "converge"]
    if [node.get("id") for node in nodes] != expected:
        raise DeltaWorkflowError("captured review-loop shape changed; refuse an unbounded rewrite")
    commit = copy.deepcopy(nodes[5])
    transformed = [{"id": "round-pre", "timeout": 120000, "bash": _command(setup, "prepare")}]
    for slot in range(1, 10):
        dependencies = ["round-pre"] if slot <= 3 else [f"checkpoint-{previous}" for previous in range(((slot - 1) // 3 - 1) * 3 + 1, ((slot - 1) // 3) * 3 + 1)]
        transformed.append({"id": f"review-{slot}", "depends_on": dependencies,
                            "when": f"$round-pre.review_{slot} == 'yes'",
                            "trigger_rule": "all_done", "timeout": 1800000,
                            "prompt": _review_prompt(slot, setup)})
        transformed.append({"id": f"checkpoint-{slot}", "depends_on": [f"review-{slot}"],
                            "when": f"$round-pre.review_{slot} == 'yes'", "trigger_rule": "all_done",
                            "timeout": 120000, "bash": _command(setup, f"complete --slot {slot}")})
    transformed.append({"id": "review-gate", "depends_on": [f"checkpoint-{slot}" for slot in range(1, 10)],
                        "trigger_rule": "all_done", "timeout": 300000, "bash": _command(setup, "complete")})
    transformed.append(copy.deepcopy(nodes[3]))
    transformed.append({"id": "fixer", "depends_on": ["commit-fixes"], "timeout": nodes[4].get("timeout", 1800000),
                        "prompt": FIXER_PROMPT})
    transformed.extend([commit, {"id": "converge", "depends_on": ["commit-fixer"], "timeout": 300000,
                                 "bash": _command(setup, "converge")}])
    verification = "set -euo pipefail\npython3 " + shlex.quote(str(setup / "review_verification.py")) + ' --artifacts "$ARTIFACTS_DIR"\n'
    transformed.insert(0, {"id": "verify-before", "timeout": 5400000, "bash": verification})
    transformed[1]["depends_on"] = ["verify-before"]
    transformed.insert(-1, {"id": "verify-after", "depends_on": ["commit-fixer"], "timeout": 5400000, "bash": verification})
    transformed[-1]["depends_on"] = ["verify-after"]
    # New node identities prevent a stopped legacy round from reusing outputs
    # produced by its completed preflight/gates under a different review policy.
    for node in transformed:
        node["id"] = "delta-" + node["id"]
        if "depends_on" in node:
            node["depends_on"] = ["delta-" + dependency for dependency in node["depends_on"]]
        if "when" in node:
            node["when"] = node["when"].replace("$round-pre.", "$delta-round-pre.")
    loop["nodes"] = transformed
    return output
