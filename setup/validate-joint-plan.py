#!/usr/bin/env python3
"""Validate the optional joint repository feature-planning artifact."""
import json
from pathlib import Path
import re
import sys


KNOWN_REPOS = {"api", "goodword-mcp", "web-app"}
SCHEMA = "archon.joint-feature-plan.v1"
PATHISH = re.compile(r"(^/)|(\.\.)|[/\\]|(^~)")
NEGATIVE_SCENARIO = re.compile(r"(?i)(denied|reject|forbidden|unauthori[sz]ed|403|negative)")


def fail(message: str) -> None:
    print("JOINT_PLAN=FAIL " + message)
    raise SystemExit(1)


def load_json(path: Path, label: str):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"{label} unreadable or malformed: {exc}")


def validate_repo_name(repo: object, label: str) -> str:
    if not isinstance(repo, str) or not repo.strip():
        fail(f"{label} must be a non-empty repository name")
    if repo != repo.strip() or PATHISH.search(repo):
        fail(f"{label} is not a canonical repository name: {repo!r}")
    if repo not in KNOWN_REPOS:
        fail(f"{label} is unknown: {repo}")
    return repo


def selected_repositories(params: dict) -> list[str]:
    for key in ("repositories", "repository_scope"):
        value = params.get(key)
        if value is None:
            continue
        if not isinstance(value, list):
            fail(f"params.json {key} must be a list")
        repos = []
        seen = set()
        for i, raw in enumerate(value):
            repo = validate_repo_name(raw, f"params.json {key}[{i}]")
            if repo in seen:
                fail(f"params.json {key} duplicates repository {repo}")
            seen.add(repo)
            repos.append(repo)
        return repos
    return []


def stage_map(doc: dict) -> dict:
    stages = doc.get("stages")
    if isinstance(stages, dict):
        return stages
    if isinstance(stages, list):
        mapped = {}
        for i, stage in enumerate(stages):
            if not isinstance(stage, dict):
                fail(f"joint-plan.json stages[{i}] must be an object")
            repo = validate_repo_name(stage.get("repo"), f"joint-plan.json stages[{i}].repo")
            if repo in mapped:
                fail(f"joint-plan.json stages duplicates repository {repo}")
            body = dict(stage)
            body.pop("repo", None)
            mapped[repo] = body
        return mapped
    fail("joint-plan.json stages must be an object keyed by repository")


def ordered_repos(repos: list[str], stages: dict) -> list[str]:
    remaining = set(repos)
    done: set[str] = set()
    order: list[str] = []
    while remaining:
        ready = sorted(
            repo for repo in remaining
            if set(stages[repo].get("depends_on", [])).issubset(done)
        )
        if not ready:
            fail("dependency cycle among selected repositories")
        repo = ready[0]
        remaining.remove(repo)
        done.add(repo)
        order.append(repo)
    return order


def require_string_list(body: dict, key: str, label: str) -> list[str]:
    value = body.get(key)
    if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
        fail(f"{label} must declare {key}")
    return value


def validate_stage(repo: str, body: object, selected: set[str]) -> None:
    if not isinstance(body, dict):
        fail(f"stage for {repo} must be an object")
    deps = body.get("depends_on", [])
    if not isinstance(deps, list) or not all(isinstance(x, str) for x in deps):
        fail(f"stage for {repo} has malformed depends_on")
    outside = sorted(set(deps) - selected)
    if outside:
        fail(f"stage for {repo} depends on outside-scope repositories: {','.join(outside)}")
    if repo in deps:
        fail(f"stage for {repo} depends on itself")
    require_string_list(body, "files_allowlist", f"stage for {repo}")
    require_string_list(body, "test_patterns", f"stage for {repo}")
    require_string_list(body, "verification", f"stage for {repo}")


def declared_columns(audit: object, label: str) -> set[tuple[str, str]]:
    columns = audit.get("columns") if isinstance(audit, dict) else None
    if not isinstance(columns, list) or not all(
        isinstance(c, dict) and all(isinstance(c.get(k), str) and c[k].strip() for k in ("table", "column"))
        for c in columns
    ):
        fail(f"{label} must be {{\"columns\": [{{\"table\", \"column\", \"reason\"}}]}}")
    return {(c["table"], c["column"]) for c in columns}


def validate_reader_audits(repos: list[str], stages: dict, params: dict, artifacts: Path) -> None:
    """stages.<repo>.reader_audit is each stage's own reader-audit obligation.
    The planning run's reader-audit.json is anchored on ONE repository, so a
    stage that inherits it audits another repository's columns and proves
    nothing. All or none: a plan without any (every plan approved before this
    field existed) passes with a WARN, and feature_chain has each non-anchor
    stage derive its own audit from its diff instead of inheriting the anchor's."""
    present = [repo for repo in repos if "reader_audit" in stages[repo]]
    if not present:
        if len(repos) > 1:
            print("JOINT_PLAN=WARN no stages.<repo>.reader_audit (legacy plan): "
                  "non-anchor stages derive their reader audit from their own diff")
        return
    for repo in repos:
        if repo not in present:
            fail(f"stage for {repo} must declare reader_audit (every stage declares one when any does)")
        declared_columns(stages[repo]["reader_audit"], f"stages.{repo}.reader_audit")
    # The packet renders reader-audit.json (anchor) and web-reader-audit.json;
    # the stages consume the joint plan. They must say the same thing.
    mirrors = [(params.get("repo"), "reader-audit.json")]
    if params.get("repo") != "web-app":
        mirrors.append(("web-app", "web-reader-audit.json"))
    for repo, name in mirrors:
        if repo in stages and (artifacts / name).exists():
            planned = declared_columns(load_json(artifacts / name, name), name)
            if planned != declared_columns(stages[repo]["reader_audit"], f"stages.{repo}.reader_audit"):
                fail(f"{name} columns differ from stages.{repo}.reader_audit")


def validate_contracts(doc: dict) -> None:
    contracts = doc.get("contracts")
    if not isinstance(contracts, list):
        fail("joint-plan.json contracts must be a list")
    for i, contract in enumerate(contracts):
        if not isinstance(contract, dict):
            fail(f"joint-plan.json contracts[{i}] must be an object")
        for key in ("producer", "consumer", "artifact", "description"):
            if not isinstance(contract.get(key), str) or not contract[key].strip():
                fail(f"joint-plan.json contracts[{i}] must declare {key}")
        for key in ("producer", "consumer"):
            validate_repo_name(contract[key], f"contracts[{i}].{key}")
            if contract[key] not in doc["repositories"]:
                fail(f"contracts[{i}].{key} is outside selected repositories")
        if contract["producer"] != contract["consumer"] and contract["producer"] not in stage_map(doc)[contract["consumer"]].get("depends_on", []):
            fail(f"contracts[{i}] producer must be an owned dependency of its consumer")
        artifact = Path(contract["artifact"])
        if artifact.is_absolute() or ".." in artifact.parts or not artifact.parts:
            fail(f"contracts[{i}] artifact must be relative to its producer repository")
        # The producer stage copies this file out of its worktree, so prose such as
        # "GET /x -> { y }" is not an artifact; it must be a plain repository path.
        if not re.fullmatch(r"[A-Za-z0-9_./@+-]+", contract["artifact"]):
            fail(f"contracts[{i}] artifact must be a repository-relative file path, not prose: {contract['artifact']!r}")
        if "export" in contract:
            export = contract["export"]
            if not isinstance(export, dict) or set(export) != {"argv"}:
                fail(f"contracts[{i}].export must declare only argv")
            validate_argv(export["argv"], f"contracts[{i}].export.argv")


def validate_argv(argv: object, label: str) -> None:
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x and "\x00" not in x for x in argv):
        fail(f"{label} must be a non-empty argv list")


def validate_command(command: object, selected: set[str], label: str, require_executable: bool = False) -> None:
    if isinstance(command, str) and command.strip() and not require_executable:
        return  # Persisted v1 shell commands retain their execution semantics.
    if not isinstance(command, dict) or set(command) != {"repo", "argv"}:
        expected = "{repo, argv}" if require_executable else "a shell string or {repo, argv}"
        fail(f"{label} must be {expected}")
    if not isinstance(command["repo"], str) or command["repo"] not in selected:
        fail(f"{label}.repo must name a selected repository")
    validate_argv(command["argv"], f"{label}.argv")
    allowed = {"ARCHON_REPO_" + repo.upper().replace("-", "_") + suffix
               for repo in selected for suffix in ("_WORKTREE", "_COMMIT", "_SOURCE_WORKTREE")}
    for arg in command["argv"]:
        if re.search(r"\$(ARCHON_[A-Za-z0-9_]+)", arg):
            fail(f"{label} unsupported bare environment reference; use ${{ARCHON_REPO_<REPO>_<FIELD>}}")
        for name in re.findall(r"\$\{([^}]+)\}", arg):
            if name not in allowed:
                fail(f"{label} unknown environment reference: {name}")


def validate_integration(doc: dict, selected: set[str], require_executable: bool = False) -> None:
    integration = doc.get("integration")
    if not isinstance(integration, dict):
        fail("joint-plan.json integration must be an object")
    scenarios = integration.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        fail("joint-plan.json integration.scenarios must be a non-empty list")
    for i, scenario in enumerate(scenarios):
        label = f"integration.scenarios[{i}]"
        if not isinstance(scenario, dict) or not isinstance(scenario.get("name"), str) or not scenario["name"].strip():
            fail(f"{label} must name a scenario")
        uses = scenario.get("uses", [])
        commands = scenario.get("commands")
        expected_tests = scenario.get("expected_tests")
        if not isinstance(uses, list) or not all(isinstance(x, str) and x.strip() for x in uses):
            fail(f"{label} must declare uses")
        outside = sorted(set(uses) - selected)
        if outside:
            fail(f"{label} uses outside-scope repositories: {','.join(outside)}")
        if not isinstance(commands, list) or not commands:
            fail(f"{label} must declare commands")
        for j, command in enumerate(commands):
            validate_command(command, set(uses), f"{label}.commands[{j}]", require_executable)
        if not isinstance(expected_tests, list) or not all(isinstance(x, str) and x.strip() for x in expected_tests):
            fail(f"{label} must declare expected_tests")
        artifacts = scenario.get("expected_artifacts", [])
        if not isinstance(artifacts, list) or not all(isinstance(x, str) and x.strip() for x in artifacts):
            fail(f"{label} has malformed expected_artifacts")


def validate_negative_scenario(scenarios: list) -> None:
    # ponytail: regex over planner prose; upgrade to a typed scenario.kind field if planners game it.
    for scenario in scenarios:
        haystack = [scenario.get("name", "")] + list(scenario.get("expected_tests", []))
        if any(NEGATIVE_SCENARIO.search(text) for text in haystack):
            return
    fail("integration has no negative scenario")


def validate_acceptance_coverage(doc: dict, scenarios: list) -> None:
    criteria = doc.get("acceptance_criteria")
    if not isinstance(criteria, list) or not all(
        isinstance(c, dict) and isinstance(c.get("id"), str) and c["id"].strip() for c in criteria
    ):
        fail("joint-plan.json acceptance_criteria must be a list of {id, text}")
    declared = [c["id"] for c in criteria]
    covered: set[str] = set()
    for scenario in scenarios:
        for cid in scenario.get("covers", []):
            if cid not in declared:
                fail(f"unknown covered criterion {cid}")
            covered.add(cid)
    for cid in declared:
        if cid not in covered:
            fail(f"acceptance criterion {cid} is uncovered")


def main() -> int:
    if len(sys.argv) != 2:
        fail("usage: validate-joint-plan.py <artifacts-dir>")
    artifacts = Path(sys.argv[1])
    params = load_json(artifacts / "params.json", "params.json")
    if not isinstance(params, dict):
        fail("params.json must be an object")
    repos = selected_repositories(params)
    if not repos:
        print("JOINT_PLAN=SKIPPED legacy-scalar-params")
        return 0
    doc = load_json(artifacts / "joint-plan.json", "joint-plan.json")
    if not isinstance(doc, dict):
        fail("joint-plan.json must be an object")
    if "schema" in doc and doc.get("schema") != SCHEMA:
        fail(f"joint-plan.json schema must be {SCHEMA}")
    declared = doc.get("repositories")
    if declared != repos:
        fail("joint-plan.json repositories must match params.json order exactly")
    stages = stage_map(doc)
    selected = set(repos)
    if set(stages) != selected:
        fail("joint-plan.json stages must match selected repositories exactly")
    for repo in repos:
        validate_stage(repo, stages[repo], selected)
    # verify.json is the planning run's mirror of the anchor repo's shell gate,
    # but the stage run is seeded from stages.<repo>.test_patterns (feature_chain
    # dispatch), so a critic-driven fix that lands only in verify.json ships a
    # stage that fails gate-tests. Chain 228a0717 did exactly that on
    # 2026-09-15: verify.json held the unit pattern, the sealed stage still held
    # an .int.spec pattern the unit jest config ignores.
    anchor = params.get("repo")
    verify_path = artifacts / "verify.json"
    if anchor in stages and verify_path.exists():
        verify = load_json(verify_path, "verify.json")
        mirrored = verify.get("test_patterns") if isinstance(verify, dict) else None
        if mirrored != stages[anchor]["test_patterns"]:
            fail(f"verify.json test_patterns differ from stages.{anchor}.test_patterns")
    validate_reader_audits(repos, stages, params, artifacts)
    expected_order = ordered_repos(repos, stages)
    if "dependency_order" in doc and doc.get("dependency_order") != expected_order:
        fail("joint-plan.json dependency_order must be stable topological order")
    validate_contracts(doc)
    validate_integration(doc, selected, params.get("executable_plan_contract") == 1)
    scenarios = doc["integration"]["scenarios"]
    if "acceptance_criteria" in doc:
        # Rule 1 is gated on acceptance_criteria's presence too: persisted plans from before
        # this feature (e.g. chain 2205cded's) predate both fields and must keep passing.
        validate_negative_scenario(scenarios)
        validate_acceptance_coverage(doc, scenarios)
    else:
        print("JOINT_PLAN=WARN no acceptance_criteria (legacy plan)")
    print(f"JOINT_PLAN=PASS repositories={','.join(repos)} order={','.join(expected_order)}")
    if params.get("executable_plan_contract") == 1:
        print("JOINT_EXECUTION=structured argv runs in its repo's disposable detached candidate worktree; "
              "ARCHON_REPO_*_WORKTREE identifies that fixture, not the attached planning worktree. "
              "run-joint-integration.py creates and checks these fixtures before invoking commands.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
