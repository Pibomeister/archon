#!/usr/bin/env python3
"""Write the final feature-result.json for a locally verified repository stage."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def fail(message: str) -> None:
    print("LOCAL_CANDIDATE=FAIL " + message)
    raise SystemExit(1)


def load_json(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"{label} unreadable or malformed: {exc}")
    if not isinstance(value, dict):
        fail(f"{label} must be an object")
    return value


def require_int(value: object, label: str, path: Path) -> int:
    if type(value) is not int:
        fail(f"test report {path.name} missing integer {label}")
    return value


def passed_from_report(report: dict, path: Path) -> int:
    if report.get("success") is not True:
        fail(f"test report did not declare success true: {path.name}")
    failed_tests = require_int(report.get("numFailedTests"), "numFailedTests", path)
    failed_suites = require_int(report.get("numFailedTestSuites"), "numFailedTestSuites", path)
    if failed_tests or failed_suites:
        fail(f"test report has failures: {path.name}")
    passed = require_int(report.get("numPassedTests"), "numPassedTests", path)
    if passed <= 0:
        fail(f"test report has zero passed tests: {path.name}")
    return passed


def safe_relative(value: str, label: str) -> Path:
    if "\x00" in value:
        fail(f"unsafe {label}: contains NUL byte")
    rel = Path(value)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        fail(f"unsafe {label}: {value}")
    return rel


def safe_argv(argv: object, label: str) -> list[str]:
    if not isinstance(argv, list) or not argv:
        fail(f"{label} must be a non-empty string list")
    for item in argv:
        if not isinstance(item, str) or not item:
            fail(f"{label} must be a non-empty string list")
        if "\x00" in item:
            fail(f"{label} must not contain NUL bytes")
    return argv


def declared_contract_artifacts(artifacts: Path, repo: str) -> list[dict]:
    plan_path = artifacts / "joint-plan.json"
    if not plan_path.is_file():
        return []
    plan = load_json(plan_path, "joint-plan.json")
    contracts = plan.get("contracts", [])
    if not isinstance(contracts, list):
        fail("joint-plan.json contracts must be a list")
    declared = []
    for index, contract in enumerate(contracts):
        if not isinstance(contract, dict):
            fail(f"joint-plan.json contracts[{index}] must be an object")
        if contract.get("producer") != repo:
            continue
        artifact = contract.get("artifact")
        if not isinstance(artifact, str) or not artifact.strip():
            fail(f"joint-plan.json contracts[{index}] must declare artifact")
        export = contract.get("export")
        if export is not None:
            if not isinstance(export, dict) or set(export) != {"argv"}:
                fail(f"joint-plan.json contracts[{index}].export must declare only argv")
            safe_argv(export.get("argv"), f"joint-plan.json contracts[{index}].export.argv")
        declared.append({"artifact": artifact, "export": export, "index": index})
    return declared


def ensure_under(path: Path, root: Path, label: str) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        fail(f"{label} escapes allowed root: {path}")


def interface_source_root(contract: dict, worktree: Path, export_dir: Path | None) -> Path:
    if contract.get("export") is None:
        return worktree
    if export_dir is None:
        fail("declared interface export has no output directory")
    return export_dir


def copy_declared_interfaces(artifacts: Path, repo: str, worktree: Path) -> list[dict]:
    rows = []
    export_dir_value = os.environ.get("ARCHON_INTERFACE_OUTPUT_DIR")
    export_dir = Path(export_dir_value) if export_dir_value else None
    for contract in declared_contract_artifacts(artifacts, repo):
        artifact = contract["artifact"]
        rel = safe_relative(artifact, "interface artifact path")
        source_root = interface_source_root(contract, worktree, export_dir)
        source = source_root / rel
        if not source.is_file() or source.stat().st_size <= 0:
            fail(f"declared interface artifact missing or empty: {artifact}")
        ensure_under(source, source_root, "interface source")
        target_rel = Path("interface") / rel
        target = artifacts / target_rel
        ensure_under(target.parent, artifacts, "interface target")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            target.unlink()
        data = source.read_bytes()
        target.write_bytes(data)
        rows.append({
            "path": target_rel.as_posix(),
            "source_path": rel.as_posix(),
            "source_kind": "export" if contract.get("export") is not None else "worktree",
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
        })
    return rows


def run_interface_exports(artifacts: Path, repo: str, worktree: Path, output_dir: Path) -> int:
    exports = [item for item in declared_contract_artifacts(artifacts, repo) if item.get("export") is not None]
    if not exports:
        return 0
    ensure_under(output_dir, artifacts, "interface export output")
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = artifacts / "interface-export.log"
    env = dict(os.environ)
    env["ARCHON_INTERFACE_OUTPUT_DIR"] = str(output_dir)
    with log_path.open("a", encoding="utf-8") as log:
        for item in exports:
            argv = item["export"]["argv"]
            log.write(f"INTERFACE_EXPORT artifact={item['artifact']} argv={json.dumps(argv)}\n")
            log.flush()
            completed = subprocess.run(argv, cwd=worktree, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
            if completed.returncode != 0:
                fail(f"interface export failed for {item['artifact']}")
            rel = safe_relative(item["artifact"], "interface artifact path")
            source = output_dir / rel
            ensure_under(source, output_dir, "interface source")
            if not source.is_file() or source.stat().st_size <= 0:
                fail(f"interface export did not produce declared artifact: {item['artifact']}")
    return 0


def interface_rows(artifacts: Path, existing: dict, repo: str, worktree: Path) -> list[dict]:
    declared = copy_declared_interfaces(artifacts, repo, worktree)
    if declared:
        return declared
    rows = []
    for item in existing.get("interface_artifacts", []):
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            fail("interface_artifacts entries must carry path")
        rel = safe_relative(item["path"], "interface artifact path")
        full = artifacts / rel
        if not full.is_file() or full.stat().st_size <= 0:
            fail(f"interface artifact missing or empty: {item['path']}")
        row = dict(item)
        row["sha256"] = hashlib.sha256(full.read_bytes()).hexdigest()
        row["bytes"] = full.stat().st_size
        rows.append(row)
    return rows


def main() -> int:
    if len(sys.argv) == 6 and sys.argv[1] == "--run-interface-exports":
        artifacts = Path(sys.argv[2])
        repo = sys.argv[3]
        worktree = Path(sys.argv[4])
        output_dir = Path(sys.argv[5])
        return run_interface_exports(artifacts, repo, worktree, output_dir)
    if len(sys.argv) != 5:
        fail("usage: write-local-candidate.py <artifacts-dir> <repo> <baseline> <head>")
    artifacts = Path(sys.argv[1])
    repo, baseline, head = sys.argv[2:5]
    result_path = artifacts / "feature-result.json"
    existing = load_json(result_path, "feature-result.json") if result_path.exists() else {}
    params = load_json(artifacts / "params.json", "params.json")
    worktree_value = params.get("worktree")
    if not isinstance(worktree_value, str) or not worktree_value:
        fail("params.json must carry worktree")
    worktree = Path(worktree_value)
    patterns_path = artifacts / "local-candidate-patterns.txt"
    try:
        patterns = [line.strip() for line in patterns_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError as exc:
        fail(f"local candidate patterns unavailable: {exc}")
    if not patterns:
        fail("local candidate has no test patterns")
    reports = sorted(artifacts.glob("local-candidate-test-*.json"))
    if len(reports) != len(patterns):
        fail("test report count does not match test patterns")
    passed_tests = sum(passed_from_report(load_json(path, path.name), path) for path in reports)
    if passed_tests <= 0:
        fail("zero tests passed")
    smoke_text = (artifacts / "smoke-result.txt").read_text(encoding="utf-8").strip()
    if smoke_text.startswith("SMOKE=PASS"):
        smoke_status = "passed"
    elif smoke_text.startswith("SMOKE=NOT_APPLICABLE") and repo == "goodword-mcp":
        smoke_status = "not_applicable"
    else:
        fail("smoke result is not acceptable for local candidate")
    evidence = [
        {"name": "typecheck", "status": "passed", "log": "local-candidate-verify.log"},
        {"name": "unit", "status": "passed", "log": "local-candidate-verify.log", "tests_passed": passed_tests, "patterns": patterns},
        {"name": "smoke", "status": smoke_status, "log": "smoke-result.txt"},
    ]
    if repo == "api":
        evidence.insert(1, {"name": "lint", "status": "passed", "log": "local-candidate-verify.log"})
    payload = {
        "schema": "archon.repository-stage-result.v1",
        "outcome": existing.get("outcome", "CHANGED"),
        "repo": repo,
        "basis": "trusted local candidate after final repository verification",
        "baseline": baseline,
        "head": head,
        "candidate_head": head,
        "publication": "held",
        "verification": {"status": "passed", "tests_passed": passed_tests},
        "verification_evidence": evidence,
        "interface_artifacts": interface_rows(artifacts, existing, repo, worktree),
        "pr_url": None,
    }
    result_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
