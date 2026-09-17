#!/usr/bin/env python3
"""Run an approved joint-plan integration matrix against local candidate commits."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time


COMMIT_RE = re.compile(r"[0-9a-f]{40}")
DIGEST_RE = re.compile(r"[0-9a-f]{64}")
KNOWN_REPOS = {"api", "goodword-mcp", "web-app"}
PATHISH = re.compile(r"(^/)|(\.\.)|[/\\]|(^~)")
TEST_COUNT_RE = re.compile(r"^ARCHON_INTEGRATION_TESTS=(\d+)\s*$", re.M)
ENV_REF_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")
BARE_ARCHON_REF_RE = re.compile(r"\$(?!\{)(ARCHON_[A-Z0-9_]+)")
PLAN_SCHEMA = "archon.joint-feature-plan.v1"
SCHEMA = "archon.joint-candidates.v1"
RESULT = "joint-integration-result.json"
EVIDENCE = "integration-evidence.json"
FEATURE_BUDGET = Path(__file__).resolve().parent / "feature-budget.py"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from feature_env import feature_env_all  # noqa: E402
from candidate_env import CandidateEnvError, prepare as prepare_candidate_env  # noqa: E402


def fail(message: str) -> None:
    print("JOINT_INTEGRATION=FAIL " + message)
    raise SystemExit(1)


def load_json(path: Path, label: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"{label} unreadable or malformed: {exc}")
    if not isinstance(data, dict):
        fail(f"{label} must be an object")
    return data


def canonical_digest(data: object) -> str:
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        fail(f"git {' '.join(args)} failed in {repo}: {result.stderr.strip()}")
    return result.stdout.strip()


def safe_env_name(repo: str, suffix: str) -> str:
    return "ARCHON_REPO_" + re.sub(r"[^A-Z0-9]", "_", repo.upper()) + "_" + suffix


def selected_repos(plan: dict) -> list[str]:
    if plan.get("schema") != PLAN_SCHEMA:
        fail(f"joint-plan.json schema must be {PLAN_SCHEMA}")
    repos = plan.get("repositories")
    if not isinstance(repos, list) or not repos or not all(isinstance(x, str) for x in repos):
        fail("joint-plan.json must name at least one repository")
    seen = set()
    for index, repo in enumerate(repos):
        if not repo.strip():
            fail(f"joint-plan.json repositories[{index}] must be non-empty")
        if repo != repo.strip() or PATHISH.search(repo):
            fail(f"joint-plan.json repositories[{index}] is not a canonical repository name: {repo!r}")
        if repo not in KNOWN_REPOS:
            fail(f"joint-plan.json repositories[{index}] is unknown: {repo}")
        if repo in seen:
            fail(f"joint-plan.json repositories duplicates repository {repo}")
        seen.add(repo)
    return repos


def candidate_rows(candidates: dict, repos: list[str], plan: dict) -> tuple[dict, str]:
    if candidates.get("schema") != SCHEMA:
        fail(f"candidate revisions schema must be {SCHEMA}")
    plan_digest = candidates.get("plan_digest")
    if not isinstance(plan_digest, str) or not DIGEST_RE.fullmatch(plan_digest):
        fail("candidate revisions must carry plan_digest")
    if plan_digest != canonical_digest(plan):
        fail("candidate revisions plan_digest does not match joint-plan.json")
    approved = candidates.get("approved_plan_digest")
    if not isinstance(approved, str) or not DIGEST_RE.fullmatch(approved):
        fail("candidate revisions must carry approved_plan_digest")
    if approved != plan_digest:
        fail("candidate revisions approved_plan_digest does not match plan_digest")
    heads = candidates.get("candidate_heads")
    if not isinstance(heads, dict):
        fail("candidate revisions must carry candidate_heads")
    rows = candidates.get("repositories")
    if not isinstance(rows, dict):
        fail("candidate revisions must carry repositories object")
    missing = sorted(set(repos) - set(rows))
    extra = sorted(set(rows) - set(repos))
    if missing or extra:
        fail(f"candidate repositories mismatch missing={missing} extra={extra}")
    for repo in repos:
        row = rows[repo]
        if not isinstance(row, dict):
            fail(f"candidate row for {repo} must be an object")
        source = row.get("source_worktree")
        commit = row.get("commit")
        if not isinstance(source, str) or not source:
            fail(f"candidate row for {repo} must carry source_worktree")
        if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
            fail(f"candidate row for {repo} must carry a 40-character commit")
        declared = heads.get(repo)
        if declared != commit:
            fail(f"candidate row for {repo} does not match candidate_heads")
    return rows, plan_digest


def create_worktrees(rows: dict, repos: list[str], root: Path) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=False)
    worktrees: dict[str, Path] = {}
    try:
        for repo in repos:
            source = Path(rows[repo]["source_worktree"])
            commit = rows[repo]["commit"]
            actual = git(source, "rev-parse", "--verify", commit + "^{commit}")
            if actual != commit:
                fail(f"candidate commit for {repo} is not exact")
            target = root / repo
            worktrees[repo] = target
            git(source, "worktree", "add", "--detach", str(target), commit)
            checked_out = git(target, "rev-parse", "HEAD")
            if checked_out != commit:
                fail(f"candidate worktree for {repo} did not checkout exact commit")
    except BaseException:
        remove_worktrees(rows, worktrees)
        shutil.rmtree(root, ignore_errors=True)
        raise
    return worktrees


def prepare_environments(rows: dict, worktrees: dict[str, Path]) -> dict:
    """Make each candidate runnable by its repository's own commands before any
    scenario runs, so plans never carry bootstrap scripts of their own."""
    prepared = {}
    for repo, worktree in worktrees.items():
        try:
            prepared[repo] = prepare_candidate_env(repo, worktree, Path(rows[repo]["source_worktree"]))
        except CandidateEnvError as exc:
            fail(f"CANDIDATE_ENV=FAIL {exc}")
        print(f"CANDIDATE_ENV=PASS repo={repo} {json.dumps(prepared[repo], sort_keys=True)}")
    return prepared


def integration_scenarios(plan: dict) -> list[dict]:
    integration = plan.get("integration")
    if not isinstance(integration, dict):
        fail("joint-plan.json integration must be an object")
    scenarios = integration.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        fail("joint-plan.json integration.scenarios must be non-empty")
    return scenarios


def validate_expected_artifact(artifacts: Path, scenario_name: str, relative: str) -> dict:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        fail(f"integration scenario {scenario_name} expected_artifacts contains unsafe path: {relative!r}")
    resolved = artifacts / path
    if not resolved.is_file() or resolved.stat().st_size == 0:
        return {"path": relative, "exists": False, "bytes": 0}
    return {
        "path": relative,
        "exists": True,
        "bytes": resolved.stat().st_size,
        "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
    }


def scenario_tests(scenario: dict, name: str) -> list[str]:
    tests = scenario.get("expected_tests")
    if not isinstance(tests, list) or not tests or not all(isinstance(test, str) and test.strip() for test in tests):
        fail(f"integration scenario {name} must declare non-empty expected_tests")
    return tests


def scenario_artifacts(scenario: dict, name: str) -> list[str]:
    paths = scenario.get("expected_artifacts", [])
    if not isinstance(paths, list) or not all(isinstance(path, str) and path.strip() for path in paths):
        fail(f"integration scenario {name} has malformed expected_artifacts")
    return paths


def scenario_timeout(scenario: dict, name: str) -> int:
    value = scenario.get("command_timeout_seconds", 1800)
    if not isinstance(value, int) or value < 1 or value > 1800:
        fail(f"integration scenario {name} has invalid command_timeout_seconds")
    return value


def reported_tests(output: str) -> int:
    return sum(int(match.group(1)) for match in TEST_COUNT_RE.finditer(output))


def kill_process_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        return


def process_fingerprint(pid: int) -> str | None:
    result = subprocess.run(
        ["ps", "-o", "lstart=", "-o", "command=", "-p", str(pid)],
        capture_output=True,
        encoding="utf-8",
    )
    value = result.stdout.strip()
    return value or None


def feature_run_id(artifacts: Path, env: dict) -> str | None:
    for key in ("ARCHON_FEATURE_RUN_ID", "WORKFLOW_ID", "WORKFLOW_RUN_ID"):
        value = env.get(key)
        if isinstance(value, str) and value and re.fullmatch(r"[0-9a-fA-F-]{8,36}", value):
            return value
    name = artifacts.name
    return name if re.fullmatch(r"[0-9a-fA-F-]{8,36}", name) else None


def register_group(artifacts: Path, env: dict, pgid: int, label: str) -> bool:
    chain_id = env.get("ARCHON_FEATURE_CHAIN_ID")
    control_dir = env.get("ARCHON_CONTROL_DIR")
    run_id = feature_run_id(artifacts, env)
    if not chain_id or not control_dir or not run_id:
        return False
    result = subprocess.run(
        [
            sys.executable, str(FEATURE_BUDGET), "--control-dir", control_dir,
            "register-group", "--chain-id", chain_id, "--run-id", run_id,
            "--pgid", str(pgid), "--fingerprint", "@current", "--label", label,
        ],
        capture_output=True,
        encoding="utf-8",
        env=env,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stdout + result.stderr).strip() or "feature budget group registration failed")
    return True


def unregister_group(env: dict, pgid: int) -> None:
    chain_id = env.get("ARCHON_FEATURE_CHAIN_ID")
    control_dir = env.get("ARCHON_CONTROL_DIR")
    if not chain_id or not control_dir:
        return
    subprocess.run(
        [
            sys.executable, str(FEATURE_BUDGET), "--control-dir", control_dir,
            "unregister-group", "--chain-id", chain_id, "--pgid", str(pgid),
        ],
        capture_output=True,
        encoding="utf-8",
        env=env,
        timeout=30,
    )


def run_shell_command(command: str, artifacts: Path, env: dict, timeout_seconds: int, label: str) -> tuple[str, int | None, bool, int, bool]:
    return run_supervised_command(
        artifacts=artifacts,
        env=dict(env, ARCHON_JOINT_COMMAND=command),
        timeout_seconds=timeout_seconds,
        label=label,
        supervisor_arg="--supervise-command",
    )


def run_structured_command(argv: list[str], cwd: Path, artifacts: Path, env: dict, timeout_seconds: int, label: str) -> tuple[str, int | None, bool, int, bool]:
    return run_supervised_command(
        artifacts=artifacts,
        env=dict(env, ARCHON_JOINT_ARGV=json.dumps(argv), ARCHON_JOINT_CWD=str(cwd)),
        timeout_seconds=timeout_seconds,
        label=label,
        supervisor_arg="--supervise-argv",
    )


def run_supervised_command(
    artifacts: Path,
    env: dict,
    timeout_seconds: int,
    label: str,
    supervisor_arg: str,
) -> tuple[str, int | None, bool, int, bool]:
    read_fd, write_fd = os.pipe()
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), supervisor_arg, str(read_fd)],
        cwd=artifacts,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
        pass_fds=(read_fd,),
    )
    os.close(read_fd)
    registered = False
    try:
        registered = register_group(artifacts, env, proc.pid, label)
        os.write(write_fd, b"go\n")
    except BaseException:
        kill_process_group(proc.pid)
        raise
    finally:
        os.close(write_fd)
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                kill_process_group(proc.pid)
                output, _ = proc.communicate()
                return output + f"\nJOINT_INTEGRATION_TIMEOUT={timeout_seconds}\n", None, True, proc.pid, registered
            try:
                output, _ = proc.communicate(timeout=min(0.1, remaining))
                return output, proc.returncode, False, proc.pid, registered
            except subprocess.TimeoutExpired:
                if proc.poll() is not None and proc.returncode != 0:
                    kill_process_group(proc.pid)
                    output, _ = proc.communicate()
                    return output, proc.returncode, False, proc.pid, registered
    finally:
        unregister_group(env, proc.pid)


def supervise_command(gate_fd: int) -> int:
    command = os.environ["ARCHON_JOINT_COMMAND"]
    return supervise_child(gate_fd, ["/bin/bash", "-c", command], None)


def supervise_argv(gate_fd: int) -> int:
    try:
        argv = json.loads(os.environ["ARCHON_JOINT_ARGV"])
    except (KeyError, json.JSONDecodeError):
        return 70
    cwd = os.environ.get("ARCHON_JOINT_CWD")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
        return 70
    if not isinstance(cwd, str) or not cwd:
        return 70
    return supervise_child(gate_fd, argv, cwd)


def supervise_child(gate_fd: int, argv: list[str], cwd: str | None) -> int:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        os.read(gate_fd, 1)
    except OSError:
        return 70
    try:
        os.close(gate_fd)
    except OSError:
        pass
    child = subprocess.Popen(
        argv,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    while True:
        rc = child.poll()
        if rc is not None:
            deadline = time.time() + 2
            while time.time() < deadline:
                try:
                    members = [
                        int(pid)
                        for pid in subprocess.check_output(
                            ["pgrep", "-g", str(os.getpgrp())],
                            stderr=subprocess.DEVNULL,
                            text=True,
                        ).split()
                        if int(pid) != os.getpid()
                    ]
                except (subprocess.CalledProcessError, ValueError):
                    members = []
                if not members:
                    break
                time.sleep(0.05)
            return int(rc)
        time.sleep(0.05)


def repo_env_refs(worktrees: dict[str, Path]) -> dict[str, str]:
    return {
        safe_env_name(repo, suffix): repo
        for repo in worktrees
        for suffix in ("WORKTREE", "COMMIT", "SOURCE_WORKTREE")
    }


def allowed_env_refs(env: dict, worktrees: dict[str, Path]) -> dict[str, str]:
    known = repo_env_refs(worktrees)
    return {
        key: repo for key, repo in known.items()
        if key in env
    }


def scenario_uses(scenario: dict, worktrees: dict[str, Path]) -> set[str]:
    uses = scenario.get("uses")
    if not isinstance(uses, list):
        return set(worktrees)
    return {repo for repo in uses if isinstance(repo, str)}


def expand_argv_refs(argv: list[str], env: dict, name: str, command_index: int, scenario: dict, worktrees: dict[str, Path]) -> list[str]:
    allowed = allowed_env_refs(env, worktrees)
    uses = scenario_uses(scenario, worktrees)

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in allowed:
            fail(f"integration scenario {name} command {command_index} has unknown environment reference: ${{{key}}}")
        repo = allowed[key]
        if repo not in uses:
            fail(f"integration scenario {name} command {command_index} references repository outside uses: {repo}")
        return env[key]

    expanded = []
    for value in argv:
        bare = BARE_ARCHON_REF_RE.search(value)
        if bare:
            fail(f"integration scenario {name} command {command_index} has unsupported bare environment reference: ${bare.group(1)}")
        expanded.append(ENV_REF_RE.sub(replace, value))
    return expanded


def structured_command_details(command: dict, scenario: dict, name: str, command_index: int, worktrees: dict[str, Path], env: dict) -> tuple[str, list[str], Path]:
    if set(command) != {"repo", "argv"}:
        fail(f"integration scenario {name} command {command_index} must contain exactly repo and argv")
    repo = command["repo"]
    argv = command["argv"]
    if not isinstance(repo, str) or repo not in worktrees:
        fail(f"integration scenario {name} command {command_index} uses unknown repository: {repo!r}")
    uses = scenario.get("uses")
    if isinstance(uses, list) and repo not in uses:
        fail(f"integration scenario {name} command {command_index} repo is not listed in uses: {repo}")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item and "\x00" not in item for item in argv):
        fail(f"integration scenario {name} command {command_index} has invalid argv")
    return repo, expand_argv_refs(argv, env, name, command_index, scenario, worktrees), worktrees[repo]


def run_commands(artifacts: Path, plan: dict, worktrees: dict[str, Path], candidates: dict) -> tuple[list[dict], list[dict]]:
    env = dict(os.environ)
    # A plain `archon workflow resume` drops the launcher's chain env, and
    # without ARCHON_FEATURE_CHAIN_ID every command below runs unregistered
    # with the feature budget. params.json is the durable copy.
    env.update(feature_env_all(artifacts))
    env["ARCHON_INTEGRATION_ROOT"] = str(artifacts / "joint-integration-worktrees")
    # Concurrent chains each get their own smoke port in params.json; without
    # exporting it every joint e2e booted on the script's fixed default and the
    # second chain died with "port already in use".
    try:
        api_port = json.loads((artifacts / "params.json").read_text(encoding="utf-8")).get("api_port")
    except (OSError, ValueError):
        api_port = None
    if isinstance(api_port, int):
        env["ARCHON_API_PORT"] = str(api_port)
    for repo, worktree in worktrees.items():
        env[safe_env_name(repo, "WORKTREE")] = str(worktree)
        env[safe_env_name(repo, "COMMIT")] = candidates[repo]["commit"]
        env[safe_env_name(repo, "SOURCE_WORKTREE")] = candidates[repo]["source_worktree"]
    command_rows = []
    test_rows = []
    for scenario in integration_scenarios(plan):
        name = scenario.get("name")
        commands = scenario.get("commands")
        if not isinstance(name, str) or not name.strip():
            fail("integration scenario missing name")
        name = name.strip()
        tests = scenario_tests(scenario, name)
        expected_artifacts = scenario_artifacts(scenario, name)
        timeout_seconds = scenario_timeout(scenario, name)
        if not isinstance(commands, list) or not commands:
            fail(f"integration scenario {name} has no commands")
        scenario_commands = []
        for index, command in enumerate(commands, start=1):
            command_kind = "shell"
            command_record = command
            cwd_record = str(artifacts)
            if isinstance(command, str):
                if not command.strip():
                    fail(f"integration scenario {name} has an empty command")
                runner = lambda: run_shell_command(command, artifacts, env, timeout_seconds, f"integration:{name}:{index}")
            elif isinstance(command, dict):
                command_kind = "argv"
                repo, argv, cwd = structured_command_details(command, scenario, name, index, worktrees, env)
                command_record = {"repo": repo, "argv": argv}
                cwd_record = str(cwd)
                runner = lambda: run_structured_command(argv, cwd, artifacts, env, timeout_seconds, f"integration:{name}:{index}")
            else:
                fail(f"integration scenario {name} has an empty command")
            log = artifacts / f"joint-integration-{len(command_rows) + 1}.log"
            try:
                output, returncode, timed_out, launcher_pgid, registered_group = runner()
            except RuntimeError as exc:
                output = f"JOINT_INTEGRATION_REGISTRATION=FAIL {exc}\n"
                returncode = 1
                timed_out = False
                launcher_pgid = 0
                registered_group = False
            row = {
                "scenario": name,
                "index": index,
                "command": command_record,
                "command_kind": command_kind,
                "cwd": cwd_record,
                "returncode": returncode,
                "timed_out": timed_out,
                "launcher_pgid": launcher_pgid,
                "registered_group": registered_group,
                "reported_tests": reported_tests(output),
                "log": str(log),
            }
            log.write_text(output, encoding="utf-8")
            command_rows.append(row)
            scenario_commands.append(len(command_rows) - 1)
            if row["returncode"] != 0:
                test_rows.append({"name": name, "status": "failed", "expected_tests": tests, "commands": scenario_commands, "artifacts": []})
                return command_rows, test_rows
        artifacts_evidence = [validate_expected_artifact(artifacts, name, path) for path in expected_artifacts]
        missing = [row for row in artifacts_evidence if not row.get("exists")]
        reported = sum(command_rows[index]["reported_tests"] for index in scenario_commands)
        underreported = reported < len(tests)
        test_rows.append({
            "name": name,
            "status": "failed" if missing or underreported else "passed",
            "expected_tests": tests,
            "reported_tests": reported,
            "commands": scenario_commands,
            "artifacts": artifacts_evidence,
        })
        if missing or underreported:
            return command_rows, test_rows
    return command_rows, test_rows


def remove_worktrees(rows: dict, worktrees: dict[str, Path]) -> None:
    for repo, worktree in worktrees.items():
        source = Path(rows[repo]["source_worktree"])
        if source.exists():
            subprocess.run(
                ["git", "-C", str(source), "worktree", "remove", "--force", str(worktree)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            subprocess.run(
                ["git", "-C", str(source), "worktree", "prune"],
                capture_output=True,
                text=True,
                timeout=60,
            )


def candidate_revisions(rows: dict, repos: list[str]) -> dict:
    return {repo: rows[repo]["commit"] for repo in repos}


def write_result(artifacts: Path, status: str, rows: list[dict], tests: list[dict], repos: list[str], candidates: dict, plan_digest: str, environments: dict) -> None:
    counters = {
        "repositories": len(repos),
        "scenarios": len(tests),
        "commands": len(rows),
        "expected_tests": sum(len(row.get("expected_tests", [])) for row in tests),
        "reported_tests": sum(row.get("reported_tests", 0) for row in tests),
        "tests_passed": sum(row.get("reported_tests", 0) for row in tests if row.get("status") == "passed"),
        "passed_scenarios": sum(1 for row in tests if row.get("status") == "passed"),
        "failed_commands": sum(1 for row in rows if row.get("returncode") != 0),
        "timed_out_commands": sum(1 for row in rows if row.get("timed_out")),
    }
    result = {
        "schema": "archon.joint-integration-result.v1",
        "status": status,
        "plan_digest": plan_digest,
        "approved_plan_digest": plan_digest,
        "candidate_heads": candidate_revisions(candidates, repos),
        "candidate_revisions": candidate_revisions(candidates, repos),
        "candidate_environments": environments,
        "repositories": repos,
        "counters": counters,
        "tests": tests,
        "commands": rows,
    }
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    (artifacts / RESULT).write_text(payload, encoding="utf-8")
    (artifacts / EVIDENCE).write_text(payload, encoding="utf-8")


def run(artifacts: Path) -> int:
    for stale in (RESULT, EVIDENCE):
        (artifacts / stale).unlink(missing_ok=True)
    plan = load_json(artifacts / "joint-plan.json", "joint-plan.json")
    candidates = load_json(artifacts / "candidate-revisions.json", "candidate-revisions.json")
    repos = selected_repos(plan)
    rows, plan_digest = candidate_rows(candidates, repos, plan)
    root = artifacts / "joint-integration-worktrees"
    if root.exists():
        fail("joint integration worktree root already exists")
    worktrees: dict[str, Path] = {}
    command_rows: list[dict] = []
    tests: list[dict] = []
    try:
        worktrees = create_worktrees(rows, repos, root)
        environments = prepare_environments(rows, worktrees)
        command_rows, tests = run_commands(artifacts, plan, worktrees, rows)
        status = (
            "passed"
            if command_rows
            and tests
            and all(row["returncode"] == 0 for row in command_rows)
            and all(row["status"] == "passed" and row["expected_tests"] for row in tests)
            else "failed"
        )
        write_result(artifacts, status, command_rows, tests, repos, rows, plan_digest, environments)
        if status != "passed":
            fail("approved integration command failed")
    finally:
        remove_worktrees(rows, worktrees)
        shutil.rmtree(root, ignore_errors=True)
    print("JOINT_INTEGRATION=PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", required=True, type=Path)
    args = parser.parse_args()
    return run(args.artifacts.resolve())


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--supervise-command":
        raise SystemExit(supervise_command(int(sys.argv[2])))
    if len(sys.argv) == 3 and sys.argv[1] == "--supervise-argv":
        raise SystemExit(supervise_argv(int(sys.argv[2])))
    raise SystemExit(main())
