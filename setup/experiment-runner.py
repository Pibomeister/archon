#!/usr/bin/env python3
"""Bounded experiment adapters for Archon bugfix RCA.

This is deliberately small: it executes only typed unit-test, repo-read, and
prod-sql experiment specs. It never accepts free-form shell strings.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn

REPOS = {"api", "web-app"}
ADAPTERS = {"unit-test", "repo-read", "prod-sql"}
MAX_TIMEOUT_SECONDS = 120
MAX_ARGC = 24
MAX_ARG_BYTES = 4096
MAX_OUTPUT_BYTES = 128 * 1024
MAX_READ_BYTES = 256 * 1024


class RunnerError(ValueError):
    pass


def fail(message: str) -> NoReturn:
    print(f"EXPERIMENT_RUNNER=FAIL {message}")
    raise SystemExit(1)


def load_spec(path: Path) -> dict[str, Any]:
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunnerError(f"cannot read spec: {exc}") from exc
    if not isinstance(spec, dict):
        raise RunnerError("spec must be an object")
    adapter = spec.get("adapter")
    if adapter not in ADAPTERS:
        raise RunnerError("adapter must be one of unit-test, repo-read, prod-sql")
    if "command" in spec and isinstance(spec["command"], str):
        raise RunnerError("free-form shell command rejected; use argv")
    return spec


def safe_repo(root: Path, repo: Any) -> Path:
    if repo not in REPOS:
        raise RunnerError("repo must be api or web-app")
    path = (root / repo).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise RunnerError("repo escapes root") from exc
    if not path.is_dir():
        raise RunnerError(f"repo directory missing: {repo}")
    return path


def safe_child(root: Path, relative: str, *, must_exist: bool = True) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise RunnerError(f"unsafe path: {relative}")
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise RunnerError(f"path escapes root: {relative}") from exc
    if must_exist and not path.is_file():
        raise RunnerError(f"path missing: {relative}")
    return path


def bounded_argv(value: Any) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(x, str) and x for x in value):
        raise RunnerError("argv must be a non-empty string list")
    if len(value) > MAX_ARGC:
        raise RunnerError("argv too long")
    if sum(len(x.encode("utf-8")) for x in value) > MAX_ARG_BYTES:
        raise RunnerError("argv byte length too large")
    if value[0] not in {"bun", "pnpm", "npm"}:
        raise RunnerError("unit-test argv must start with bun, pnpm, or npm")
    return value


def sanitized_env(artifacts: Path) -> dict[str, str]:
    keep = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "")}
    keep["TMPDIR"] = str(artifacts)
    keep["NO_PROXY"] = "*"
    keep["HTTP_PROXY"] = ""
    keep["HTTPS_PROXY"] = ""
    keep["ALL_PROXY"] = ""
    return keep


def sandboxed_argv(argv: list[str], repo_root: Path, artifacts: Path) -> list[str]:
    sandbox_exec = shutil.which("sandbox-exec")
    if not sandbox_exec:
        raise RunnerError("unit-test execution requires an OS sandbox (sandbox-exec unavailable)")
    profile = artifacts / "experiment.sb"
    profile.write_text(
        "(version 1)\n"
        "(allow default)\n"
        "(deny network*)\n"
        f"(deny file-write* (subpath {json.dumps(str(repo_root.resolve()))}))\n",
        encoding="utf-8",
    )
    return [sandbox_exec, "-f", str(profile), "--", *argv]


def run_argv(
    argv: list[str], cwd: Path, artifacts: Path, timeout: int, *, dry_run: bool,
    sandbox_repo_root: Path | None = None,
) -> dict[str, Any]:
    if dry_run:
        return {"status": "dry-run", "argv": argv, "cwd": str(cwd)}
    effective_argv = sandboxed_argv(argv, sandbox_repo_root, artifacts) if sandbox_repo_root else argv
    proc = subprocess.Popen(
        effective_argv,
        cwd=str(cwd),
        env=sanitized_env(artifacts),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        stdout, _ = proc.communicate(timeout=timeout)
        output = stdout[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
        return {
            "status": "exit",
            "returncode": proc.returncode,
            "output": output,
            "truncated": len(stdout) > MAX_OUTPUT_BYTES,
        }
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        stdout, _ = proc.communicate()
        output = stdout[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
        return {
            "status": "timeout",
            "returncode": None,
            "output": output,
            "truncated": len(stdout) > MAX_OUTPUT_BYTES,
        }


def validate_sql(sql: Any) -> str:
    if not isinstance(sql, str) or not sql.strip():
        raise RunnerError("sql must be non-empty")
    stripped = sql.strip().rstrip(";")
    if ";" in stripped:
        raise RunnerError("prod-sql accepts one statement only")
    if not re.match(r"(?is)^(select|with)\b", stripped):
        raise RunnerError("prod-sql must start with SELECT/WITH")
    if re.search(r"(?i)\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|vacuum)\b", stripped):
        raise RunnerError("prod-sql write/DDL keyword rejected")
    return stripped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        spec = load_spec(args.spec)
        artifacts = args.artifacts.resolve()
        artifacts.mkdir(parents=True, exist_ok=True)
        timeout = spec.get("timeout_seconds", 30)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1 or timeout > MAX_TIMEOUT_SECONDS:
            raise RunnerError("timeout_seconds out of bounds")
        adapter = spec["adapter"]
        if adapter == "unit-test":
            repo = safe_repo(args.repo_root, spec.get("repo"))
            result = run_argv(
                bounded_argv(spec.get("argv")), repo, artifacts, timeout,
                dry_run=args.dry_run, sandbox_repo_root=args.repo_root.resolve(),
            )
        elif adapter == "repo-read":
            repo = safe_repo(args.repo_root, spec.get("repo"))
            paths = spec.get("paths")
            if not isinstance(paths, list) or not paths or len(paths) > 10:
                raise RunnerError("repo-read paths must be 1..10")
            files = []
            for rel in paths:
                path = safe_child(repo, rel)
                data = path.read_bytes()
                files.append({"path": rel, "bytes": len(data), "truncated": len(data) > MAX_READ_BYTES, "content": data[:MAX_READ_BYTES].decode("utf-8", errors="replace")})
            result = {"status": "read", "files": files}
        else:
            sql = validate_sql(spec.get("sql"))
            wrapper = os.environ.get("ARCHON_PROD_SQL_WRAPPER")
            if not wrapper:
                raise RunnerError("ARCHON_PROD_SQL_WRAPPER required for prod-sql")
            result = run_argv([wrapper, sql], Path.cwd(), artifacts, timeout, dry_run=args.dry_run)
        out = {"schema_version": 1, "adapter": adapter, "result": result}
        target = artifacts / "experiment-result.json"
        target.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
        print(f"EXPERIMENT_RUNNER=OK adapter={adapter} status={result.get('status')}")
    except RunnerError as exc:
        fail(str(exc))


if __name__ == "__main__":
    main()
