#!/usr/bin/env python3
"""Provider-neutral Archon launcher and guarded Codex control surface.

Every execution is detached with an exact process group, resolves one exact Archon
run, and starts the external wall/token watchdog before returning to the operator.
The generated Codex workflows' one-time always-run guard rejects raw execution,
resumption, and gate-release invocations.  ``bugfix --provider`` performs the
conservative static/lite-envelope routing shared by Claude and Codex.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import pwd
import re
import secrets
import signal
import shutil
import contextlib
import socket
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import NoReturn, Any, Iterator


USER_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
DEFAULT_CONTROL_DIR = USER_HOME / ".archon" / "control" / "codex-lite"
SELF_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SELF_DIR))
import control_contract as control_contract  # noqa: E402
PRIVATE_INSTALL = SELF_DIR / "codex-lite-install.json"
if PRIVATE_INSTALL.is_file():
    try:
        _install = json.loads(PRIVATE_INSTALL.read_text(encoding="utf-8"))
        ARCHON_DIR = Path(_install["archon_dir"])
        ROOT = Path(_install["root"])
    except Exception as exc:
        raise SystemExit(f"CODEX_LITE_RUN=FAIL private installation metadata invalid: {exc}")
    SETUP = SELF_DIR
else:
    ARCHON_DIR = SELF_DIR.parent
    ROOT = ARCHON_DIR.parent
    SETUP = ARCHON_DIR / "setup"
WORKSPACE_WRAPPER = SETUP / "codex-workspace-wrapper.sh"
CODEX_LANES = {
    "full-sdlc-api-lite-codex": (90, 8_000_000),
    "bugfix-lite-codex": (90, 8_000_000),
    "bugfix-codex": (240, 30_000_000),
}
LANES = set(CODEX_LANES)
ID_RE = re.compile(r"[0-9a-f]{8,32}", re.I)
RUN_ID_RE = re.compile(r'"workflowRunId"\s*:\s*"([0-9a-f]{32})"')
CONTROL_TOKEN_PLACEHOLDER = "CONTROL_TOKEN_FROM_LAST_LAUNCH"
COMMIT_RE = re.compile(r"[0-9a-f]{40}", re.I)
CHAIN_ID_RE = re.compile(r"[0-9a-f]{24,64}", re.I)
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
GATE_STATUSES = {"paused"}
DEFAULT_GITNEXUS_INDEX = USER_HOME / ".archon" / "gitnexus" / "api-main"
CONTINUATION_ARTIFACTS = (
    "symptoms.json", "symptoms.seal.json", "evidence-manifest.json",
    "causal-chain.json", "hypotheses.json", "proof-assessment.json",
    "chain-verify.json", "chain-assessment.json", "experiment.json",
    "experiment-result.json", "experiment-assessment.json", "proof-recovery.json",
    "rca.md", "failed-fix.json", "failed-patch.diff", "failed-untracked.json",
)
MAX_RECOVERY_SUCCESSORS = 2



def fail(reason: str) -> NoReturn:
    print(f"CODEX_LITE_RUN=FAIL {reason}")
    raise SystemExit(1)


def resolve_run(db: Path, prefix: str) -> dict:
    if not ID_RE.fullmatch(prefix):
        fail(f"bad-id-format [{prefix}]")
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, workflow_name, user_message, status, output_root "
        "FROM remote_agent_workflow_runs WHERE id LIKE ? ORDER BY started_at DESC LIMIT 3",
        (prefix.lower() + "%",),
    ).fetchall()
    con.close()
    if not rows:
        fail(f"no run matching {prefix}")
    if len(rows) != 1:
        fail(f"ambiguous prefix={prefix} matches={len(rows)}")
    row = dict(rows[0])
    if row["workflow_name"] not in LANES:
        fail(f"run {row['id'][:8]} is {row['workflow_name']}, not a guarded Codex lane")
    return row


def pinned_gitnexus_runner(index_path: Path) -> Path:
    """Resolve the exact analyzer artifact recorded by the current index."""
    try:
        meta = json.loads((index_path / ".gitnexus" / "meta.json").read_text(encoding="utf-8"))
        invoked = meta["runnerIdentity"]["invokedArtifact"]
        runner = Path(invoked["path"])
        expected_digest = invoked["digest"]
        info = runner.lstat()
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        fail(f"pinned GitNexus analyzer artifact unavailable: {exc}")
    if not runner.is_absolute() or stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        fail("pinned GitNexus analyzer artifact must be an absolute regular non-symlink file")
    actual_digest = "sha256:" + hashlib.sha256(runner.read_bytes()).hexdigest()
    if not isinstance(expected_digest, str) or not hmac.compare_digest(actual_digest, expected_digest):
        fail("pinned GitNexus analyzer artifact digest mismatch")
    return runner


def _gitnexus_unavailable(reason: str) -> dict:
    safe = re.sub(r"[^A-Za-z0-9_./:=,+ -]", "_", reason).strip() or "unknown"
    return {"status": "UNAVAILABLE", "reason": safe[:240]}



def optional_pinned_gitnexus_runner(index_path: Path) -> tuple[Path | None, str | None]:
    try:
        meta = json.loads((index_path / ".gitnexus" / "meta.json").read_text(encoding="utf-8"))
        invoked = meta["runnerIdentity"]["invokedArtifact"]
        runner = Path(invoked["path"])
        expected_digest = invoked["digest"]
        info = runner.lstat()
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return None, f"pinned-runner-unavailable {exc}"
    if not runner.is_absolute() or stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return None, "pinned-runner-not-absolute-regular-file"
    actual_digest = "sha256:" + hashlib.sha256(runner.read_bytes()).hexdigest()
    if not isinstance(expected_digest, str) or not hmac.compare_digest(actual_digest, expected_digest):
        return None, "pinned-runner-digest-mismatch"
    return runner, None

def assess_gitnexus_environment(root: Path, codex_home: Path, registry_path: Path,
                                baseline: dict | None = None) -> dict:
    """Best-effort GitNexus readiness check.

    GitNexus is evidence acceleration, not control authority. Missing MCP,
    missing registry entries, stale indexes, or analyzer drift must degrade to
    explicit unavailable evidence so workflows can continue using repo-local
    inspection. A configured MCP server is only accepted when it points at the
    protected dispatcher; otherwise the launcher fails because using an
    unpinned graph would weaken provenance.
    """
    try:
        config_text = (codex_home / "config.toml").read_text(encoding="utf-8")
    except OSError:
        config_text = ""
    mcp_block = re.search(r"(?ms)^\[mcp_servers\.gitnexus\]\s*$\n(.*?)(?=^\[|\Z)", config_text)
    expected_dispatcher = str((SETUP / "gitnexus-mcp-dispatch.py").resolve())
    if mcp_block:
        body = mcp_block.group(1)
        if (not re.search(r'(?m)^command\s*=\s*["\']python3["\']\s*$', body)
                or expected_dispatcher not in body):
            fail(f"gitnexus MCP is configured but does not use stable dispatcher: python3 {expected_dispatcher}")
    else:
        return _gitnexus_unavailable("mcp-not-configured")

    index_path = DEFAULT_GITNEXUS_INDEX
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return _gitnexus_unavailable(f"registry-unreadable {exc}")
    if not isinstance(registry, list):
        return _gitnexus_unavailable("registry-not-list")
    api_entries = [entry for entry in registry if isinstance(entry, dict) and entry.get("name") == "api"]
    if len(api_entries) != 1:
        return _gitnexus_unavailable(f"api-entry-count={len(api_entries)}")
    api_entry = api_entries[0]
    if Path(api_entry.get("path", "")) != index_path or not Path(api_entry.get("storagePath", "")).is_dir():
        return _gitnexus_unavailable(f"pinned-api-index-missing path={index_path}")

    baseline_commit = None
    if isinstance(baseline, dict):
        baseline_commit = ((baseline.get("gitnexus") or {}).get("commit")
                           or (baseline.get("commits") or {}).get("api"))
    if baseline_commit is not None and not COMMIT_RE.fullmatch(str(baseline_commit)):
        fail(f"stored api baseline commit is invalid: {baseline_commit}")
    if baseline_commit is None:
        fetched = subprocess.run(
            ["git", "-C", str(root / "api"), "fetch", "origin", "main", "--quiet"],
            capture_output=True,
            encoding="utf-8",
        )
        if fetched.returncode != 0:
            return _gitnexus_unavailable(f"cannot-refresh-api-origin-main {fetched.stderr.strip()}")
        current = subprocess.run(
            ["git", "-C", str(root / "api"), "rev-parse", "origin/main"],
            capture_output=True,
            encoding="utf-8",
        )
        if current.returncode != 0:
            return _gitnexus_unavailable(f"cannot-resolve-api-origin-main {current.stderr.strip()}")
        expected_commit = current.stdout.strip()
        expected_label = "origin/main"
    else:
        expected_commit = str(baseline_commit)
        expected_label = "stored-run-baseline"

    worktree = subprocess.run(
        ["git", "-C", str(index_path), "rev-parse", "HEAD"],
        capture_output=True,
        encoding="utf-8",
    )
    if worktree.returncode != 0 or worktree.stdout.strip() != expected_commit:
        return _gitnexus_unavailable(
            f"worktree-stale actual={worktree.stdout.strip()} expected-{expected_label}={expected_commit}"
        )
    if api_entry.get("lastCommit") != expected_commit:
        return _gitnexus_unavailable(
            f"index-stale actual={api_entry.get('lastCommit')} expected-{expected_label}={expected_commit}"
        )
    analyzer_runner, runner_error = optional_pinned_gitnexus_runner(index_path)
    if analyzer_runner is None:
        return _gitnexus_unavailable(runner_error or "pinned-runner-unavailable")
    index_status = subprocess.run(
        ["node", str(analyzer_runner), "status"],
        cwd=index_path, capture_output=True, encoding="utf-8",
    )
    if index_status.returncode != 0 or "Status: ✅ up-to-date" not in index_status.stdout:
        return _gitnexus_unavailable("analyzer-runtime-stale")
    return {"status": "AVAILABLE", "reason": "pinned-api-index-current", "index": str(index_path), "commit": expected_commit}


def ensure_environment(root: Path, codex_home: Path, registry_path: Path,
                       lane: str | None = None, baseline: dict | None = None,
                       *, check_ports: bool = True) -> None:
    if os.environ.get("CODEX_LITE_SKIP_ENV_CHECKS") == "1":
        return
    for tool in (os.environ.get("ARCHON_BIN", "archon"), "codex", "sqlite3", "git", "gh", "bun", "node", "pnpm", "mise"):
        if not (Path(tool).is_file() if "/" in tool else shutil.which(tool)):
            fail(f"required tool not found: {tool}")
    for command, label in ((["gh", "auth", "status"], "GitHub auth"),
                           (["mise", "x", "node@20", "--", "node", "-v"], "Node 20 via mise")):
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode != 0:
            fail(f"{label} is not ready")
    for required in (root / "api" / ".git", root / "web-app" / ".git",
                     root / "api" / ".env", root / "web-app" / ".env"):
        if not required.exists():
            fail(f"required repository/config path missing: {required}")
    if check_ports:
        lane_ports = {
            "full-sdlc-api-lite-codex": (4125,),
            "bugfix-lite-codex": (4126, 3126),
            "bugfix-codex": (4124, 3124),
        }
        for port in lane_ports.get(lane, (4124, 4125, 4126, 3124, 3126)):
            sock = socket.socket()
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                fail(f"lite lane port is busy: {port}")
            finally:
                sock.close()
    auth = codex_home / "auth.json"
    try:
        data = json.loads(auth.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(f"Codex auth unreadable at {auth}: {exc}")
    tokens = data.get("tokens") or {}
    if data.get("auth_mode") != "chatgpt" or not tokens.get("access_token") or not tokens.get("refresh_token"):
        fail(f"Codex home is not a complete ChatGPT login: CODEX_HOME={codex_home} codex login")
    if auth.stat().st_mode & 0o077:
        fail(f"Codex auth permissions must be 0600: chmod 600 {auth}")

    try:
        config_text = (codex_home / "config.toml").read_text(encoding="utf-8")
    except OSError as exc:
        fail(f"Codex config unreadable at {codex_home}/config.toml: {exc}")
    if not re.search(r'(?m)^sandbox_mode\s*=\s*["\']workspace-write["\']\s*$', config_text):
        fail("dedicated Codex config must set sandbox_mode = \"workspace-write\"; danger-full-access breaks the control boundary")

    for skill in ("ce-code-review", "ce-doc-review"):
        if not (root / ".agents" / "skills" / skill / "SKILL.md").is_file():
            fail(f"staged Codex skill missing: {root}/.agents/skills/{skill}/SKILL.md")
        private_skill = codex_home / "skills" / skill / "SKILL.md"
        if not private_skill.is_file():
            fail(f"dedicated-home Codex skill missing: {private_skill}")

    gitnexus = assess_gitnexus_environment(root, codex_home, registry_path, baseline)
    if gitnexus["status"] == "AVAILABLE":
        print(f"GITNEXUS=AVAILABLE index={gitnexus['index']} commit={gitnexus['commit']}")
    else:
        print(f"GITNEXUS=UNAVAILABLE reason={gitnexus['reason']}")

def detached(log: Path, command: list[str], env: dict[str, str], supervise: bool = True) -> tuple[int, int]:
    detach = Path(os.environ.get("CODEX_LITE_DETACH", SETUP / "detach.py"))
    detach_args = [sys.executable, str(detach)]
    if supervise:
        detach_args.append("--supervise")
    result = subprocess.run(
        [*detach_args, str(log), str(ROOT), *command],
        capture_output=True,
        encoding="utf-8",
        env=env,
    )
    if result.returncode != 0:
        fail(f"detach failed: {(result.stdout + result.stderr).strip()}")
    pid_match = re.search(r"DETACHED_PID=(\d+)", result.stdout)
    pgid_match = re.search(r"DETACHED_PGID=(\d+)", result.stdout)
    if not pid_match or not pgid_match:
        fail(f"detach returned no exact pid/pgid: {(result.stdout + result.stderr).strip()}")
    return int(pid_match.group(1)), int(pgid_match.group(1))


def wait_for_run_id(log: Path, pid: int, timeout_s: int = 60) -> str:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
        match = RUN_ID_RE.search(text)
        if match:
            return match.group(1)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            tail = "\n".join(text.splitlines()[-15:])
            fail(f"Archon exited before creating a run; log tail:\n{tail}")
        time.sleep(0.1)
    fail(f"timed out waiting for workflowRunId in {log}")


def command_for(action: str, target: str, reason: str | None = None) -> list[str]:
    archon = os.environ.get("ARCHON_BIN", "archon")
    if action == "run":
        lane, spec = target.split("\0", 1)
        return [archon, "workflow", "run", lane, spec]
    if action == "resume":
        return ["bash", str(SETUP / "resume.sh"), target]
    if action == "approve":
        return [archon, "workflow", "approve", target]
    if action == "reject":
        return [archon, "workflow", "reject", target, reason or ""]
    if action == "abandon":
        return [archon, "workflow", "abandon", target, "--json"]
    fail(f"unknown action {action}")


def watchdog_command(run_id: str, pgid: int, fingerprint: str, wall: int,
                     tokens: int, db: Path, codex_home: Path,
                     arm_file: Path, chain_id: str | None = None) -> list[str]:
    script = Path(os.environ.get("CODEX_LITE_WATCHDOG", SETUP / "codex-watchdog.sh"))
    cmd = [
        "bash", str(script), run_id,
        "--wall-minutes", str(wall),
        "--max-total-tokens", str(tokens),
        "--launcher-pgid", str(pgid),
        "--launcher-fingerprint", fingerprint,
        "--await-running",
        "--arm-file", str(arm_file),
        "--db", str(db),
        "--codex-home", str(codex_home),
    ]
    if chain_id:
        cmd.extend(["--chain-id", chain_id])
    return cmd


def control_artifact_path(row: dict) -> Path:
    ad = Path(row["output_root"]) / "artifacts" / "runs" / row["id"]
    ad.mkdir(parents=True, exist_ok=True)
    return ad / "codex-lite-control.json"


def ensure_control_dir(control_dir: Path) -> None:
    try:
        control_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = control_dir.lstat()
    except OSError as exc:
        fail(f"private control directory unavailable at {control_dir}: {exc}")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        fail(f"private control path is not a real directory: {control_dir}")
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        fail(f"private control directory must be owned by this user and mode 0700: {control_dir}")
    if os.environ.get("CODEX_LITE_SKIP_ENV_CHECKS") != "1":
        try:
            control_dir.resolve().relative_to(ROOT.resolve())
        except ValueError:
            pass
        else:
            fail("private control directory must be outside the AI-writable workspace")


def validate_control_location(control_dir: Path) -> None:
    if (os.environ.get("CODEX_LITE_SKIP_ENV_CHECKS") != "1"
            and control_dir.resolve() != DEFAULT_CONTROL_DIR.resolve()):
        fail(f"production control state is fixed at {DEFAULT_CONTROL_DIR}")
    ensure_control_dir(control_dir)


def install_private_codex_wrapper(control_dir: Path) -> Path:
    ensure_control_dir(control_dir)
    try:
        payload = WORKSPACE_WRAPPER.read_bytes()
    except OSError as exc:
        fail(f"Codex sandbox wrapper unreadable at {WORKSPACE_WRAPPER}: {exc}")
    target = control_dir / "codex-workspace-wrapper.sh"
    temporary = control_dir / f".codex-wrapper.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(temporary, flags, 0o500)
    try:
        os.write(fd, payload)
        os.fsync(fd)
        os.fchmod(fd, 0o500)
    finally:
        os.close(fd)
    os.replace(temporary, target)
    return target


def stage_private_codex_skills(root: Path, codex_home: Path) -> None:
    skills_dir = codex_home / "skills"
    skills_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    for skill in ("ce-code-review", "ce-doc-review"):
        source = root / ".agents" / "skills" / skill
        if not (source / "SKILL.md").is_file():
            fail(f"staged Codex skill missing: {source}/SKILL.md")
        resolved = source.resolve()
        try:
            resolved.relative_to(root.resolve())
        except ValueError:
            pass
        else:
            fail(f"Codex skill source must be outside its writable roots: {resolved}")
        target = skills_dir / skill
        if target.is_symlink() and target.resolve() == resolved:
            continue
        if target.exists() or target.is_symlink():
            fail(f"Codex home skill path conflicts with trusted source: {target}")
        target.symlink_to(resolved, target_is_directory=True)


def secure_write_json(path: Path, data: dict) -> None:
    try:
        control_contract.secure_write_json(path, data)
    except control_contract.ControlContractError as exc:
        fail(str(exc))


def secure_read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return control_contract.secure_read_json(path)
    except control_contract.ControlContractError as exc:
        fail(str(exc))


def _canonical_json_bytes(data: Any) -> bytes:
    return control_contract.canonical_bytes(data)


def _hmac_sha256(secret: str, data: Any) -> str:
    try:
        return control_contract.hmac_sha256(secret, data)
    except control_contract.ControlContractError as exc:
        fail(str(exc))


def chain_state_dir(control_dir: Path) -> Path:
    ensure_control_dir(control_dir)
    path = control_dir / "bugfix-chains"
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        fail(f"private bugfix chain directory must be owned mode-0700 real directory: {path}")
    return path


def chain_state_path(control_dir: Path, chain_id: str) -> Path:
    if not CHAIN_ID_RE.fullmatch(chain_id):
        fail(f"bad-chain-id-format [{chain_id}]")
    return chain_state_dir(control_dir) / f"{chain_id}.json"


def seal_chain_state(state: dict) -> dict:
    try:
        return control_contract.seal_chain_state(state)
    except control_contract.ControlContractError as exc:
        fail(str(exc))


def verify_chain_state(state: dict) -> dict:
    try:
        return control_contract.verify_chain_state(state)
    except control_contract.ControlContractError as exc:
        fail(str(exc))


def read_chain_state(control_dir: Path, chain_id: str) -> dict:
    path = chain_state_path(control_dir, chain_id)
    try:
        data = secure_read_json(path)
    except (ValueError, json.JSONDecodeError) as exc:
        fail(f"private bugfix chain state malformed at {path}: {exc}")
    if not data:
        fail(f"private bugfix chain state is missing for {chain_id}")
    if data.get("logical_chain_id") != chain_id:
        fail(f"private bugfix chain state id mismatch at {path}")
    return verify_chain_state(data)


def write_chain_state(control_dir: Path, state: dict) -> dict:
    state = seal_chain_state(state)
    secure_write_json(chain_state_path(control_dir, state["logical_chain_id"]), state)
    return state


def git_commit_or_fail(repo: Path, label: str, ref: str = "origin/main") -> str:
    if ref == "origin/main":
        fetched = subprocess.run(
            ["git", "-C", str(repo), "fetch", "--quiet", "origin", "main"],
            capture_output=True, encoding="utf-8",
        )
        if fetched.returncode != 0:
            fail(f"cannot refresh {label} baseline at {repo}: {(fetched.stderr or fetched.stdout).strip()}")
    result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", f"{ref}^{{commit}}"], capture_output=True, encoding="utf-8")
    if result.returncode != 0 or not COMMIT_RE.fullmatch(result.stdout.strip()):
        fail(f"cannot capture {label} baseline commit at {repo}: {(result.stderr or result.stdout).strip()}")
    return result.stdout.strip()


def capture_bugfix_baseline(root: Path) -> dict:
    commits = {
        "api": git_commit_or_fail(root / "api", "api"),
        "web-app": git_commit_or_fail(root / "web-app", "web-app"),
    }
    baseline = {
        "commits": commits,
        "gitnexus": {
            "repo": "api",
            "index_path": str(DEFAULT_GITNEXUS_INDEX),
            "commit": commits["api"],
        },
    }
    baseline["sha256"] = hashlib.sha256(_canonical_json_bytes(baseline)).hexdigest()
    return baseline


def start_bugfix_chain(control_dir: Path, provider: str, report: Path, baseline: dict) -> dict:
    report_hash = hashlib.sha256(report.read_bytes()).hexdigest()
    state = {
        "logical_chain_id": secrets.token_hex(16),
        "chain_secret": secrets.token_urlsafe(48),
        "provider": provider,
        "report": str(report),
        "report_sha256": report_hash,
        "root_run_id": None,
        "current_run_id": None,
        "runs": [],
        "counters": {"successors": 0, "recovery_successors": 0, "proof_rounds": 0, "causal_fix_failures": 0},
        "baseline": baseline,
        "continuation_seeds": {},
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    return write_chain_state(control_dir, state)


def public_chain_receipt(state: dict, row: dict, transition_reason: str, parent_run_id: str | None, sequence: int) -> dict:
    return {
        "logical_chain_id": state["logical_chain_id"],
        "provider": state["provider"],
        "report": state["report"],
        "report_sha256": state["report_sha256"],
        "root_run_id": state.get("root_run_id") or row["id"],
        "parent_run_id": parent_run_id,
        "current_run_id": row["id"],
        "sequence": sequence,
        "transition_reason": transition_reason,
        "baseline": state["baseline"],
        "root_source_ids": state.get("root_source_ids", []),
        "ledger_root_hash": state.get("ledger_root_hash"),
        "ledger_revision_hash": state.get("ledger_revision_hash"),
        "counters": {k: state.get("counters", {}).get(k, 0) for k in ("successors", "recovery_successors", "proof_rounds", "causal_fix_failures")},
    }


def write_public_chain_receipt(row: dict, receipt: dict) -> Path:
    target_dir = artifact_dir(row)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "bugfix-chain.json"
    tmp = target.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(target)
    return target


def record_chain_run(control_dir: Path, state: dict, row: dict, transition_reason: str, parent_run_id: str | None) -> dict:
    sequence = len(state.get("runs", []))
    if state.get("root_run_id") is None:
        state["root_run_id"] = row["id"]
    state["current_run_id"] = row["id"]
    receipt = public_chain_receipt(state, row, transition_reason, parent_run_id, sequence)
    state.setdefault("runs", []).append({
        "run_id": row["id"],
        "workflow_name": row.get("workflow_name"),
        "sequence": sequence,
        "parent_run_id": parent_run_id,
        "transition_reason": transition_reason,
        "receipt_sha256": hashlib.sha256(_canonical_json_bytes(receipt)).hexdigest(),
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    write_public_chain_receipt(row, receipt)
    return write_chain_state(control_dir, state)


def adopt_run_ledger(control_dir: Path, state: dict, row: dict) -> dict:
    """Bind the controller-owned chain to the intake-sealed root symptom set."""
    ad = artifact_dir(row)
    try:
        ledger = json.loads((ad / "symptoms.json").read_text(encoding="utf-8"))
        receipt = json.loads((ad / "bugfix-chain.json").read_text(encoding="utf-8"))
        source_ids = [item["id"] for item in ledger["source_symptoms"]]
        root_hash = ledger["ledger_root_hash"]
        revision_hash = ledger["ledger_revision_hash"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        fail(f"cannot bind successor to sealed symptom ledger for run {row['id'][:8]}: {exc}")
    if not source_ids or not isinstance(root_hash, str) or not isinstance(revision_hash, str):
        fail("sealed symptom ledger is missing source identities or hashes")
    for key, value in (("root_source_ids", source_ids), ("ledger_root_hash", root_hash)):
        existing = state.get(key)
        if existing not in (None, [], value):
            fail(f"successor symptom lineage conflicts with private chain {key}")
        if receipt.get(key) != value:
            fail(f"public chain receipt is not bound to sealed ledger {key}")
        state[key] = value
    state["ledger_revision_hash"] = revision_hash
    return write_chain_state(control_dir, state)


def continuation_bundle_path(control_dir: Path, chain_id: str, nonce: str) -> Path:
    suffix = hashlib.sha256(nonce.encode()).hexdigest()
    return chain_state_dir(control_dir) / f"{chain_id}-{suffix}.bundle.json"


def prepare_continuation_bundle(control_dir: Path, state: dict, row: dict, seed: dict) -> Path:
    documents = {}
    ad = artifact_dir(row)
    for name in CONTINUATION_ARTIFACTS:
        path = ad / name
        if not path.is_file() or path.is_symlink():
            continue
        raw = path.read_bytes()
        documents[name] = {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "data_b64": base64.b64encode(raw).decode("ascii"),
        }
    if "symptoms.json" not in documents:
        fail("continuation bundle requires a sealed symptom ledger")
    bundle = {
        "schema_version": 1,
        "logical_chain_id": state["logical_chain_id"],
        "parent_run_id": row["id"],
        "provider": state["provider"],
        "report_sha256": state["report_sha256"],
        "baseline_sha256": state["baseline"]["sha256"],
        "seed_nonce_sha256": hashlib.sha256(seed["nonce"].encode()).hexdigest(),
        "causal_fix_failures": int(state.get("counters", {}).get("causal_fix_failures", 0)),
        "documents": documents,
    }
    bundle["bundle_mac"] = _hmac_sha256(state["chain_secret"], bundle)
    path = continuation_bundle_path(control_dir, state["logical_chain_id"], seed["nonce"])
    secure_write_json(path, bundle)
    return path


def import_continuation_bundle(args: argparse.Namespace) -> None:
    chain_id = os.environ.get("ARCHON_BUGFIX_CHAIN_ID")
    nonce = os.environ.get("ARCHON_BUGFIX_CONTINUATION_SEED")
    if not chain_id or not nonce:
        fail("continuation import requires protected chain and seed environment")
    state = read_chain_state(args.control_dir, chain_id)
    path = continuation_bundle_path(args.control_dir, chain_id, nonce)
    bundle = secure_read_json(path)
    if not bundle:
        fail("continuation bundle is missing")
    mac = bundle.get("bundle_mac")
    payload = {k: v for k, v in bundle.items() if k != "bundle_mac"}
    if not isinstance(mac, str) or not hmac.compare_digest(mac, _hmac_sha256(state["chain_secret"], payload)):
        fail("continuation bundle failed its controller MAC")
    expected = {
        "logical_chain_id": chain_id,
        "provider": state["provider"],
        "report_sha256": state["report_sha256"],
        "baseline_sha256": state["baseline"]["sha256"],
        "seed_nonce_sha256": hashlib.sha256(nonce.encode()).hexdigest(),
    }
    if any(bundle.get(k) != v for k, v in expected.items()):
        fail("continuation bundle identity mismatch")
    documents = bundle.get("documents")
    if not isinstance(documents, dict):
        fail("continuation bundle documents are malformed")
    continuation_dir = args.artifacts.resolve() / "continuation"
    continuation_dir.mkdir(parents=True, exist_ok=True)
    decoded = {}
    for name, entry in documents.items():
        if name not in CONTINUATION_ARTIFACTS or not isinstance(entry, dict):
            fail(f"continuation bundle contains unexpected document: {name}")
        try:
            raw = base64.b64decode(entry["data_b64"], validate=True)
        except Exception as exc:
            fail(f"continuation document encoding invalid for {name}: {exc}")
        if hashlib.sha256(raw).hexdigest() != entry.get("sha256"):
            fail(f"continuation document hash mismatch: {name}")
        decoded[name] = raw
        (continuation_dir / name).write_bytes(raw)
    if args.finalize_ledger:
        try:
            ledger = json.loads(decoded["symptoms.json"].decode("utf-8"))
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            fail(f"continuation ledger invalid: {exc}")
        ledger["previous_revision_hash"] = ledger.get("ledger_revision_hash")
        ledger["revision"] = int(ledger.get("revision", 0)) + 1
        ledger["ledger_revision_hash"] = None
        ledger["ledger_revision_hash"] = hashlib.sha256(_canonical_json_bytes(ledger)).hexdigest()
        secure_target = args.artifacts.resolve() / "symptoms.json"
        temporary = secure_target.with_suffix(".tmp")
        temporary.write_text(json.dumps(ledger, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, secure_target)
    context = {
        "schema_version": 1,
        "parent_run_id": bundle["parent_run_id"],
        "inherited_documents": sorted(documents),
        "finalized_ledger": bool(args.finalize_ledger),
        "causal_fix_failures": int(bundle.get("causal_fix_failures", 0)),
    }
    target = args.artifacts.resolve() / "continuation-context.json"
    target.write_text(json.dumps(context, indent=2) + "\n", encoding="utf-8")
    print(
        f"ARCHON_BUGFIX_CONTINUATION=IMPORTED parent={bundle['parent_run_id'][:8]} "
        f"documents={len(documents)} finalized_ledger={str(bool(args.finalize_ledger)).lower()}"
    )


def create_continuation_seed(control_dir: Path, state: dict, parent_run_id: str, transition_type: str, successor_budget: int = 1) -> tuple[dict, dict]:
    if state.get("current_run_id") != parent_run_id:
        fail("continuation parent must be the current run in the bugfix chain")
    if successor_budget != 1:
        fail("continuation seeds are single-use and require successor_budget=1")
    failures = int(state.get("counters", {}).get("causal_fix_failures", 0))
    if failures >= 3:
        receipt = state.get("architecture_review_receipt")
        if transition_type != "architecture-approved" or not isinstance(receipt, dict) or receipt.get("approved") is not True:
            fail("three causal fix failures block continuation without architecture approval")
    is_recovery = transition_type not in {"lite-envelope-full", "architecture-approved"}
    if is_recovery:
        consumed = int(state.get("counters", {}).get("recovery_successors", 0))
        outstanding = sum(
            1 for entry in state.get("continuation_seeds", {}).values()
            if not entry.get("consumed")
            and (entry.get("seed") or {}).get("transition_type") not in {"lite-envelope-full", "architecture-approved"}
        )
        if consumed + outstanding >= MAX_RECOVERY_SUCCESSORS:
            fail(f"CHAIN_CAP_REACHED recovery_successors={consumed} max={MAX_RECOVERY_SUCCESSORS}")
    nonce = secrets.token_urlsafe(32)
    seed = {
        "nonce": nonce,
        "logical_chain_id": state["logical_chain_id"],
        "root_run_id": state.get("root_run_id"),
        "parent_run_id": parent_run_id,
        "provider": state["provider"],
        "report_sha256": state["report_sha256"],
        "baseline_sha256": state["baseline"]["sha256"],
        "transition_type": transition_type,
        "successor_budget": successor_budget,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    seed["seed_mac"] = _hmac_sha256(state["chain_secret"], seed)
    state.setdefault("continuation_seeds", {})[hashlib.sha256(nonce.encode()).hexdigest()] = {
        "seed": seed,
        "consumed": False,
    }
    state.setdefault("counters", {}).setdefault("successors", 0)
    state = write_chain_state(control_dir, state)
    return state, seed


def consume_continuation_seed(control_dir: Path, chain_id: str, nonce: str, expected_provider: str) -> dict:
    state = read_chain_state(control_dir, chain_id)
    key = hashlib.sha256(nonce.encode()).hexdigest()
    entry = state.get("continuation_seeds", {}).get(key)
    if not entry or entry.get("consumed"):
        fail("continuation seed is missing, already consumed, or not single-use")
    seed = entry.get("seed")
    if not isinstance(seed, dict) or seed.get("provider") != expected_provider or seed.get("logical_chain_id") != chain_id:
        fail("continuation seed does not match requested provider/chain")
    expected_mac = seed.get("seed_mac")
    payload = {k: v for k, v in seed.items() if k != "seed_mac"}
    if not isinstance(expected_mac, str) or not hmac.compare_digest(expected_mac, _hmac_sha256(state["chain_secret"], payload)):
        fail("continuation seed failed its private MAC check")
    if state.get("baseline", {}).get("sha256") != seed.get("baseline_sha256"):
        fail("continuation seed baseline no longer matches chain state")
    state.setdefault("counters", {})["successors"] = int(state.get("counters", {}).get("successors", 0)) + 1
    if seed.get("transition_type") not in {"lite-envelope-full", "architecture-approved"}:
        state["counters"]["recovery_successors"] = int(state["counters"].get("recovery_successors", 0)) + 1
    entry["consumed"] = True
    entry["consumed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state["continuation_seeds"][key] = entry
    return write_chain_state(control_dir, state)


@contextlib.contextmanager
def temporary_env(updates: dict[str, str]) -> Iterator[None]:
    old = {k: os.environ.get(k) for k in updates}
    os.environ.update(updates)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def chain_env(state: dict, seed: dict | None = None) -> dict[str, str]:
    baseline = state.get("baseline", {})
    gitnexus = baseline.get("gitnexus", {}) if isinstance(baseline, dict) else {}
    env = {
        "ARCHON_BUGFIX_CHAIN_ID": state["logical_chain_id"],
        "ARCHON_BUGFIX_CHAIN_STATE": str(chain_state_path(Path(os.environ.get("ARCHON_CONTROL_DIR", DEFAULT_CONTROL_DIR)), state["logical_chain_id"])),
        "ARCHON_GITNEXUS_INDEX": str(gitnexus.get("index_path") or DEFAULT_GITNEXUS_INDEX),
        "ARCHON_GITNEXUS_COMMIT": str(gitnexus.get("commit") or baseline.get("commits", {}).get("api", "")),
        "ARCHON_API_BASELINE": str(baseline.get("commits", {}).get("api", "")),
        "ARCHON_WEB_BASELINE": str(baseline.get("commits", {}).get("web-app", "")),
        "ARCHON_ATTESTATION_DIR": str(Path(os.environ.get("ARCHON_CONTROL_DIR", DEFAULT_CONTROL_DIR)) / "attestations"),
    }
    if seed is not None:
        env["ARCHON_BUGFIX_CONTINUATION_SEED"] = seed["nonce"]
    return env

def control_state_path(row: dict, control_dir: Path) -> Path:
    ensure_control_dir(control_dir)
    return control_dir / f"{row['id']}.json"


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def authority_mac(token: str, control: dict) -> str:
    fields = {
        key: control.get(key) for key in (
            "run", "launcher_pgid", "launcher_fingerprint",
            "watchdog_pgid", "watchdog_fingerprint",
        )
    }
    payload = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(token.encode(), payload, hashlib.sha256).hexdigest()


def create_guard_file(control_dir: Path) -> Path:
    ensure_control_dir(control_dir)
    nonce = secrets.token_hex(32)
    path = control_dir / f"guard-{nonce}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, f"codex-lite-one-time-guard:{nonce}\n".encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    return path


def write_control_records(row: dict, control_dir: Path, control_token: str,
                          action: str, launcher_pid: int, launcher_pgid: int,
                          launcher_fingerprint: str, workflow_log: Path,
                          watchdog_log: Path, watchdog_pid: int,
                          watchdog_pgid: int, watchdog_fingerprint: str,
                          arm_file: Path, armed: bool, wall: int,
                          tokens: int) -> None:
    bugfix_chain_id = os.environ.get("ARCHON_BUGFIX_CHAIN_ID")
    private = {
        "action": action,
        "run": row["id"],
        "control_token_hash": token_digest(control_token),
        "launcher_pid": launcher_pid,
        "launcher_pgid": launcher_pgid,
        "launcher_fingerprint": launcher_fingerprint,
        "watchdog_pid": watchdog_pid,
        "watchdog_pgid": watchdog_pgid,
        "watchdog_fingerprint": watchdog_fingerprint,
        "watchdog_armed": armed,
        "watchdog_arm_file": str(arm_file),
        "wall_minutes": wall,
        "max_total_tokens": tokens,
        "workflow_log": str(workflow_log),
        "watchdog_log": str(watchdog_log),
    }
    if bugfix_chain_id:
        private["bugfix_chain"] = {
            "logical_chain_id": bugfix_chain_id,
            "chain_state_path": os.environ.get("ARCHON_BUGFIX_CHAIN_STATE"),
            "baseline_commits": {
                "api": os.environ.get("ARCHON_GITNEXUS_COMMIT"),
            },
        }
    private["authority_mac"] = authority_mac(control_token, private)
    secure_write_json(control_state_path(row, control_dir), private)
    public = {
        "action": action,
        "run": row["id"],
        "watchdog_armed": armed,
        "wall_minutes": wall,
        "max_total_tokens": tokens,
        "workflow_log": str(workflow_log),
        "watchdog_log": str(watchdog_log),
        "authority": "informational-only; private launcher state controls signals",
    }
    if bugfix_chain_id:
        public["logical_chain_id"] = bugfix_chain_id
    target = control_artifact_path(row)
    temporary = target.with_suffix(f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(public, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)


def status_for_run(db: Path, run_id: str) -> str:
    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT status FROM remote_agent_workflow_runs WHERE id = ?", (run_id,)
    ).fetchone()
    con.close()
    return str(row[0]) if row else "MISSING"


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def process_fingerprint(pid: int) -> str | None:
    result = subprocess.run(
        ["ps", "-o", "lstart=", "-o", "command=", "-p", str(pid)],
        capture_output=True,
        encoding="utf-8",
    )
    value = result.stdout.strip()
    return value or None


def terminate_group(pgid: int, wait_s: float = 5.0,
                    expected_fingerprint: str | None = None) -> None:
    if pgid <= 1 or pgid == os.getpgrp():
        fail(f"refusing unsafe process group {pgid}")
    try:
        if os.getpgid(pgid) != pgid:
            fail(f"refusing reused process group {pgid}: leader is not the group leader")
    except ProcessLookupError:
        return
    if expected_fingerprint is not None:
        current_fingerprint = process_fingerprint(pgid)
        if current_fingerprint != expected_fingerprint:
            fail(f"refusing reused process group {pgid}: process fingerprint changed")
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if not process_group_exists(pgid):
            return
        time.sleep(0.05)
    if expected_fingerprint is not None and process_fingerprint(pgid) != expected_fingerprint:
        fail(f"refusing SIGKILL for process group {pgid}: supervisor fingerprint changed")
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return
    deadline = time.time() + min(wait_s, 2.0)
    while time.time() < deadline and process_group_exists(pgid):
        time.sleep(0.05)
    if process_group_exists(pgid):
        fail(f"process group {pgid} survived SIGKILL")


def wait_for_watchdog_arm(arm_file: Path, watchdog_pgid: int, row: dict,
                          launcher_pgid: int, db: Path, watchdog_log: Path,
                          timeout_s: int = 15) -> str:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if arm_file.is_file():
            contents = arm_file.read_text(encoding="utf-8", errors="replace")
            if f"run={row['id']}" in contents and f"launcher_pgid={launcher_pgid}" in contents:
                return "armed"
            fail(f"watchdog arm file did not match run/process group: {arm_file}")
        if not process_exists(watchdog_pgid):
            status = status_for_run(db, row["id"])
            if status in {"completed", "cancelled"} and not process_exists(launcher_pgid):
                return status
            text = watchdog_log.read_text(encoding="utf-8", errors="replace") if watchdog_log.is_file() else ""
            tail = "\n".join(text.splitlines()[-15:])
            fail(f"watchdog exited before arming; run status={status}; log tail:\n{tail}")
        time.sleep(0.1)
    fail(f"timed out waiting for watchdog arming: {arm_file}")


def wait_for_guard_consumed(guard_file: Path, row: dict, db: Path,
                            timeout_s: int = 15) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not guard_file.exists():
            return
        if status_for_run(db, row["id"]) in {"failed", "completed", "cancelled", "paused"}:
            fail(f"one-time control guard was not consumed before status={status_for_run(db, row['id'])}")
        time.sleep(0.1)
    fail(f"timed out waiting for one-time control guard consumption: {guard_file}")


def read_control_state(row: dict, control_dir: Path) -> dict | None:
    path = control_state_path(row, control_dir)
    try:
        data = secure_read_json(path)
    except (ValueError, json.JSONDecodeError) as exc:
        fail(f"private control state malformed at {path}: {exc}")
    if data is None:
        return None
    if data.get("run") != row["id"]:
        fail(f"private control state run mismatch at {path}")
    return data


def require_control_token(row: dict, control_dir: Path, token: str | None) -> dict:
    if not token or token == CONTROL_TOKEN_PLACEHOLDER:
        fail("control action requires --token from the previous STARTED line")
    control = read_control_state(row, control_dir)
    if not control:
        fail("private control state is missing for this run")
    expected = control.get("control_token_hash")
    if not isinstance(expected, str) or not hmac.compare_digest(expected, token_digest(token)):
        fail("control token does not match this run")
    expected_mac = control.get("authority_mac")
    if not isinstance(expected_mac, str) or not hmac.compare_digest(expected_mac, authority_mac(token, control)):
        fail("private control authority was modified after launch")
    return control


def stop_controlled_processes(row: dict, control_dir: Path) -> None:
    # A paused/failed command and its watchdog have already exited. Killing a
    # historical PID here would create a reuse hazard; only a currently
    # running row can own live controlled groups.
    if row["status"] != "running":
        return
    control = read_control_state(row, control_dir)
    if not control:
        fail("running Codex lite run has no private control state; refusing an unscoped kill")
    launcher_pgid = control.get("launcher_pgid")
    launcher_fingerprint = control.get("launcher_fingerprint")
    if (not isinstance(launcher_pgid, int) or not isinstance(launcher_fingerprint, str)
            or not launcher_fingerprint):
        fail("private control state has invalid launcher PGID/fingerprint")
    # The workflow group is authoritative and must be stopped even if the
    # ancillary watcher died and its old PID was reused.
    terminate_group(launcher_pgid, expected_fingerprint=launcher_fingerprint)

    watchdog_pgid = control.get("watchdog_pgid")
    watchdog_fingerprint = control.get("watchdog_fingerprint")
    if isinstance(watchdog_pgid, int) and isinstance(watchdog_fingerprint, str):
        if process_fingerprint(watchdog_pgid) == watchdog_fingerprint:
            terminate_group(watchdog_pgid, expected_fingerprint=watchdog_fingerprint)
        elif process_exists(watchdog_pgid):
            print(f"CODEX_LITE_RUN=WARN watcher PGID {watchdog_pgid} was reused; not signalling it")


def cleanup_after_rejection(row: dict, previous_control: dict | None, db: Path) -> None:
    """Release resources retained by a paused run after rejection."""
    if isinstance(previous_control, dict):
        pgid = previous_control.get("launcher_pgid")
        fingerprint = previous_control.get("launcher_fingerprint")
        if isinstance(pgid, int) and isinstance(fingerprint, str):
            if process_fingerprint(pgid) == fingerprint:
                terminate_group(pgid, expected_fingerprint=fingerprint)

    event = latest_run_event(db, row["id"])
    if gate_name_from_event(event) != "smoke-approval":
        return
    smoke_web_dir = artifact_dir(row) / "smoke-stack" / "web-dir.txt"
    try:
        recorded = Path(smoke_web_dir.read_text(encoding="utf-8").strip()).resolve()
    except OSError:
        return
    expected = (ROOT / "web-app" / ".worktrees" / "bugfix-smoke").resolve()
    if recorded != expected or not recorded.is_dir():
        return
    removed = subprocess.run(
        ["git", "-C", str(ROOT / "web-app"), "worktree", "remove", "--force", str(recorded)],
        capture_output=True, encoding="utf-8",
    )
    if removed.returncode != 0:
        print(f"CODEX_LITE_RUN=WARN rejected smoke worktree cleanup failed: {removed.stderr.strip()}")
        return
    subprocess.run(["git", "-C", str(ROOT / "web-app"), "worktree", "prune"], check=False)


def abandon_if_orphaned(row: dict | None, db: Path, env: dict[str, str]) -> None:
    if not row or status_for_run(db, row["id"]) != "running":
        return
    result = subprocess.run(
        command_for("abandon", row["id"]), cwd=ROOT, env=env,
        capture_output=True, encoding="utf-8",
    )
    final_status = status_for_run(db, row["id"])
    if result.returncode != 0 or final_status != "cancelled":
        detail = (result.stdout + result.stderr).strip()
        print(
            f"CODEX_LITE_RUN=CLEANUP_FAIL run={row['id'][:8]} "
            f"status={final_status} detail={detail}",
            file=sys.stderr,
        )


def redact_control_tokens(text: str) -> str:
    text = re.sub(r"control_token=[^\s]+", "control_token=<redacted>", text)
    text = re.sub(r"--token\s+[^\s]+", "--token <redacted>", text)
    text = re.sub(r"CONTROL_TOKEN_FROM_LAST_LAUNCH", "<operator-held-token>", text)
    return text


def run_row_by_id(db: Path, run_id: str) -> dict | None:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT id, workflow_name, user_message, status, output_root FROM remote_agent_workflow_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    con.close()
    return dict(row) if row else None


def latest_run_event(db: Path, run_id: str) -> dict:
    try:
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        columns = {r[1] for r in con.execute("PRAGMA table_info(remote_agent_workflow_events)").fetchall()}
        if not columns:
            con.close(); return {}
        order_parts = [f"{name} DESC" for name in ("event_order", "created_at") if name in columns]
        order_parts.append("rowid DESC")
        order = ", ".join(order_parts)
        approval_filter = " AND event_type = 'approval_requested'" if "event_type" in columns else ""
        row = con.execute(
            f"SELECT * FROM remote_agent_workflow_events WHERE workflow_run_id = ?{approval_filter} ORDER BY {order} LIMIT 1",
            (run_id,),
        ).fetchone()
        if row is None and approval_filter:
            row = con.execute(
                f"SELECT * FROM remote_agent_workflow_events WHERE workflow_run_id = ? ORDER BY {order} LIMIT 1",
                (run_id,),
            ).fetchone()
        con.close()
        return dict(row) if row else {}
    except sqlite3.Error:
        return {}


def gate_name_from_event(event: dict) -> str:
    # Archon stores the approval node in ``step_name``.  Older adapters and
    # synthetic fixtures used the other aliases, so accept all of them rather
    # than making supervision depend on one event serialization.
    for key in ("step_name", "node_name", "node", "gate", "name"):
        val = event.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    for key in ("payload", "data", "message"):
        val = event.get(key)
        if isinstance(val, str):
            try:
                parsed = json.loads(val)
            except json.JSONDecodeError:
                parsed = {}
            if isinstance(parsed, dict):
                for nested in ("step_name", "node_name", "node", "gate", "name"):
                    nv = parsed.get(nested)
                    if isinstance(nv, str) and nv.strip():
                        return nv.strip()
    return "unknown-gate"


def enrich_gate_handoff(row: dict, result: dict) -> dict:
    """Attach public, decision-relevant evidence to a paused-run handoff."""
    ad = artifact_dir(row)
    result["artifacts"] = str(ad)
    packet_names = {
        "rca-approval": "rca-review.html",
        "smoke-approval": "smoke-matrix.html",
        "plan-gate": "plan-review.html",
    }
    packet = ad / packet_names.get(str(result.get("gate")), "")
    if packet.name and packet.is_file():
        result["packet"] = str(packet)
    try:
        chain = json.loads((ad / "bugfix-chain.json").read_text(encoding="utf-8"))
        result["chain"] = chain.get("logical_chain_id")
    except (OSError, json.JSONDecodeError):
        pass
    try:
        classification = json.loads((ad / "fix-classification.json").read_text(encoding="utf-8"))
        result["classification"] = classification.get("implementation_result")
        result["ticket"] = classification.get("ticket_disposition")
        result["open_symptoms"] = ",".join(classification.get("open_effective_ids") or []) or "none"
        closure_allowed = classification.get("ticket_closure_allowed") is True
    except (OSError, json.JSONDecodeError):
        closure_allowed = True
    product_failures: list[str] = []
    nonpassing_smoke: list[str] = []
    try:
        matrix = json.loads((ad / "smoke-matrix.json").read_text(encoding="utf-8"))
        for smoke_row in matrix.get("rows", []):
            if smoke_row.get("kind") != "auto" or smoke_row.get("result") == "pass":
                continue
            row_id = str(smoke_row.get("id") or smoke_row.get("spec_title") or "unnamed")
            nonpassing_smoke.append(row_id)
            if smoke_row.get("failure_class") == "product":
                product_failures.append(row_id)
    except (OSError, json.JSONDecodeError):
        pass
    if product_failures:
        result["product_failures"] = ",".join(product_failures)
        result["recommended_action"] = "reject-product-failure"
    elif not closure_allowed and not (ad / "accept-residuals.txt").is_file():
        result["recommended_action"] = "reject-or-human-residual-decision"
    elif nonpassing_smoke:
        result["nonpassing_smoke"] = ",".join(nonpassing_smoke)
        result["recommended_action"] = "inspect-unverified-smoke"
    else:
        result["recommended_action"] = "human-review"
    return result


def supervise_exact_run(db: Path, run_id: str, timeout_s: int, interval_s: float = 2.0) -> dict:
    deadline = time.time() + timeout_s
    while True:
        row = run_row_by_id(db, run_id)
        if not row:
            return {"state": "missing", "run": run_id, "status": "MISSING"}
        status = row["status"]
        if status in GATE_STATUSES:
            event = latest_run_event(db, run_id)
            return enrich_gate_handoff(row, {
                "state": "gate", "run": row["id"], "status": status,
                "lane": row["workflow_name"], "gate": gate_name_from_event(event),
            })
        if status in TERMINAL_STATUSES:
            return {"state": "terminal", "run": row["id"], "status": status, "lane": row["workflow_name"]}
        if time.time() >= deadline:
            return {"state": "handoff", "run": row["id"], "status": status, "lane": row["workflow_name"], "reason": "timeout"}
        time.sleep(interval_s)


def recovery_successor_required(row: dict) -> bool:
    try:
        recovery = json.loads((artifact_dir(row) / "proof-recovery.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return recovery.get("state") == "RECOVERY_SUCCESSOR_REQUIRED"


def failed_fix_recovery_required(row: dict) -> bool:
    attempts = sorted(artifact_dir(row).glob("attempt-*/green.json"))
    if not attempts:
        return False
    try:
        green = json.loads(attempts[-1].read_text(encoding="utf-8"))
        converge = attempts[-1].with_name("converge.txt").read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        return False
    return green.get("green") is False and "FIX_ATTEMPT_FAILED" in converge


def seal_failed_fix_evidence(row: dict, failure_number: int, state: dict) -> None:
    ad = artifact_dir(row)
    attempts = sorted(ad.glob("attempt-*/green.json"))
    if not attempts:
        fail("failed-fix recovery has no green assessment")
    green = json.loads(attempts[-1].read_text(encoding="utf-8"))
    try:
        params = json.loads((ad / "params.json").read_text(encoding="utf-8"))
        worktree = Path(params["worktree"])
        repo = params["repo"]
        baseline_sha = state["baseline"]["commits"][repo]
        allowlist = json.loads((ad / "files-allowlist.json").read_text(encoding="utf-8"))
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        fail(f"cannot seal failed-fix inputs: {exc}")
    if not isinstance(allowlist, list) or not all(isinstance(x, str) and x for x in allowlist):
        fail("failed-fix allowlist is malformed")
    result = subprocess.run(
        ["git", "-C", str(worktree), "diff", "--binary", baseline_sha, "--"],
        capture_output=True,
    )
    if result.returncode != 0:
        fail(f"failed-fix tracked patch capture failed: {result.stderr.decode(errors='replace')}")
    patch = result.stdout
    if len(patch) > 10 * 1024 * 1024:
        fail("failed-fix tracked patch exceeds 10 MiB evidence bound")
    status = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain=v1", "-z"],
        capture_output=True,
    )
    if status.returncode != 0:
        fail("failed-fix status capture failed")
    untracked = {}
    total_untracked = 0
    for record in status.stdout.split(b"\0"):
        if not record or not record.startswith(b"?? "):
            continue
        try:
            relative = record[3:].decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            fail("failed-fix untracked path is not valid UTF-8")
        if relative not in allowlist:
            fail(f"failed-fix untracked path is outside allowlist: {relative}")
        path = worktree / relative
        info = path.lstat()
        if path.is_symlink() or not path.is_file():
            fail(f"failed-fix untracked evidence must be a regular file: {relative}")
        raw = path.read_bytes()
        total_untracked += len(raw)
        if len(raw) > 1024 * 1024 or total_untracked > 5 * 1024 * 1024:
            fail("failed-fix untracked evidence exceeds size bound")
        untracked[relative] = {
            "mode": info.st_mode & 0o777,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "data_b64": base64.b64encode(raw).decode("ascii"),
        }
    (ad / "failed-patch.diff").write_bytes(patch)
    (ad / "failed-untracked.json").write_text(json.dumps(untracked, indent=2) + "\n", encoding="utf-8")
    evidence = {
        "schema_version": 1,
        "failure_number": failure_number,
        "green_assessment": green,
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
        "untracked_sha256": hashlib.sha256((ad / "failed-untracked.json").read_bytes()).hexdigest(),
        "porcelain_sha256": hashlib.sha256(status.stdout).hexdigest(),
        "source_attempt": attempts[-1].parent.name,
    }
    (ad / "failed-fix.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    if hashlib.sha256((ad / "failed-patch.diff").read_bytes()).hexdigest() != evidence["patch_sha256"]:
        fail("failed-fix tracked patch seal verification failed")


def reset_failed_worktree(row: dict) -> None:
    ad = artifact_dir(row)
    try:
        params = json.loads((ad / "params.json").read_text(encoding="utf-8"))
        repo = params["repo"]
        worktree = Path(params["worktree"])
        branch = params["branch"]
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        fail(f"cannot reset failed fix worktree safely: {exc}")
    base = ROOT / repo
    if worktree.exists():
        removed = subprocess.run(
            ["git", "-C", str(base), "worktree", "remove", "--force", str(worktree)],
            capture_output=True, encoding="utf-8",
        )
        if removed.returncode != 0:
            fail(f"cannot remove sealed failed-fix worktree: {removed.stderr.strip()}")
    branch_exists = subprocess.run(
        ["git", "-C", str(base), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]
    )
    if branch_exists.returncode == 0:
        deleted = subprocess.run(
            ["git", "-C", str(base), "branch", "-D", branch],
            capture_output=True, encoding="utf-8",
        )
        if deleted.returncode != 0:
            fail(f"cannot delete sealed failed-fix branch: {deleted.stderr.strip()}")


def supervise_command(args: argparse.Namespace) -> None:
    row = resolve_any_run(args.db, args.run_id, {"bugfix", "bugfix-lite", "bugfix-codex", "bugfix-lite-codex", *LANES})
    result = supervise_exact_run(args.db, row["id"], args.timeout_seconds, args.interval_s)
    result["control_token"] = "operator-held-not-persisted"
    if args.handoff_file:
        args.handoff_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.handoff_file.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(redact_control_tokens(json.dumps(result, indent=2, sort_keys=True) + "\n"), encoding="utf-8")
        tmp.replace(args.handoff_file)
    print("ARCHON_SUPERVISE=" + result["state"].upper() + " " + redact_control_tokens(" ".join(
        f"{k}={v}" for k, v in result.items() if k != "control_token"
    )))


def emit_successor_seed(args: argparse.Namespace) -> None:
    row = resolve_any_run(args.db, args.run_id, {"bugfix", "bugfix-lite", "bugfix-codex", "bugfix-lite-codex", *LANES})
    chain_id = args.chain_id
    if chain_id is None:
        ad = artifact_dir(row) / "bugfix-chain.json"
        try:
            chain_id = json.loads(ad.read_text(encoding="utf-8"))["logical_chain_id"]
        except Exception as exc:
            fail(f"cannot infer bugfix chain id for successor seed: {exc}")
    state = read_chain_state(args.control_dir, chain_id)
    if state.get("provider") != args.provider:
        fail("successor seed provider must match the stored bugfix chain provider")
    state = adopt_run_ledger(args.control_dir, state, row)
    state, seed = create_continuation_seed(args.control_dir, state, row["id"], args.transition_type, args.successor_budget)
    prepare_continuation_bundle(args.control_dir, state, row, seed)
    lane = "bugfix-codex" if args.provider == "codex" else "bugfix"
    command = (
        f"python3 {Path(__file__).resolve()} --control-dir {args.control_dir} bugfix "
        f"--provider {args.provider} --chain-id {state['logical_chain_id']} "
        f"--continuation-seed {seed['nonce']} {state['report']}"
    )
    print(
        f"ARCHON_BUGFIX_SUCCESSOR=SEED chain={state['logical_chain_id']} parent={row['id'][:8]} "
        f"transition={args.transition_type} seed={seed['nonce']} lane={lane} command={command}"
    )


def issue_architecture_challenge(control_dir: Path, state: dict, run_id: str) -> tuple[dict, str]:
    token = secrets.token_urlsafe(32)
    state["architecture_challenge"] = {
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "logical_chain_id": state["logical_chain_id"],
        "run_id": run_id,
        "failure_count": int(state.get("counters", {}).get("causal_fix_failures", 0)),
        "used": False,
        "issued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    return write_chain_state(control_dir, state), token


def approve_architecture_successor(args: argparse.Namespace) -> None:
    row = resolve_any_run(args.db, args.run_id, {"bugfix", "bugfix-lite", "bugfix-codex", "bugfix-lite-codex", *LANES})
    chain_id = args.chain_id
    if chain_id is None:
        try:
            chain_id = json.loads((artifact_dir(row) / "bugfix-chain.json").read_text())["logical_chain_id"]
        except Exception as exc:
            fail(f"cannot infer bugfix chain id for architecture approval: {exc}")
    state = read_chain_state(args.control_dir, chain_id)
    if state.get("provider") != args.provider:
        fail("architecture approval cannot change provider")
    failures = int(state.get("counters", {}).get("causal_fix_failures", 0))
    if failures < 3:
        fail("architecture approval is only valid after three causal fix failures")
    challenge = state.get("architecture_challenge")
    if (not isinstance(challenge, dict) or challenge.get("used") is not False
            or challenge.get("logical_chain_id") != chain_id
            or challenge.get("run_id") != row["id"]
            or challenge.get("failure_count") != failures
            or not hmac.compare_digest(
                str(challenge.get("token_sha256", "")), hashlib.sha256(args.token.encode()).hexdigest()
            )):
        fail("architecture approval token is invalid, stale, or already used")
    challenge["used"] = True
    challenge["used_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state["architecture_challenge"] = challenge
    state["architecture_review_receipt"] = {
        "approved": True,
        "reviewed_by": args.reviewed_by,
        "reason": args.reason,
        "failure_count": failures,
        "approved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    state = write_chain_state(args.control_dir, state)
    state = adopt_run_ledger(args.control_dir, state, row)
    state, seed = create_continuation_seed(
        args.control_dir, state, row["id"], "architecture-approved", 1
    )
    prepare_continuation_bundle(args.control_dir, state, row, seed)
    command = (
        f"python3 {Path(__file__).resolve()} --control-dir {args.control_dir} bugfix "
        f"--provider {args.provider} --chain-id {chain_id} "
        f"--continuation-seed {seed['nonce']} {state['report']}"
    )
    print(
        f"ARCHON_BUGFIX_ARCHITECTURE=APPROVED chain={chain_id} failures={failures} "
        f"seed={seed['nonce']} command={command}"
    )


def static_bugfix_route(report: Path) -> tuple[str, str]:
    """Return (lane_kind, reason) without interpreting ticket prose loosely.

    Lite eligibility requires one explicit repository and one allow-listed,
    single-line command in the ``## Repro`` section followed by observed text.
    Everything ambiguous goes to the full intake lane, where unknowns are a
    supported input rather than a launcher error.
    """
    text = report.read_text(encoding="utf-8", errors="replace")
    repo_values = {
        match.group(1).lower()
        for match in re.finditer(
            r"(?im)^\s*(?:repository|repo)\s*:\s*`?(api|web-app)`?\s*$", text
        )
    }
    if len(repo_values) != 1:
        return "full", "static-missing-single-repository"

    section = re.search(
        r"(?ims)^##\s+Repro\s*$\n(.*?)(?=^##\s+|\Z)", text
    )
    if not section:
        return "full", "static-missing-repro"
    fences = list(re.finditer(r"(?ms)^```[^\n]*\n(.*?)^```\s*$", section.group(1)))
    if len(fences) != 1:
        return "full", "static-repro-fence-count"
    command_lines = [line.strip() for line in fences[0].group(1).splitlines() if line.strip()]
    if len(command_lines) != 1:
        return "full", "static-repro-command-count"
    try:
        envelope = json.loads((SETUP / "lite-envelope.json").read_text(encoding="utf-8"))
        allowed = envelope["repro_command_allow"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return "full", "static-envelope-config-invalid"
    command = command_lines[0]
    if re.search(r"[;&|$`<>]", command):
        return "full", "static-repro-command-unsafe"
    if not any(command.startswith(prefix) for prefix in allowed):
        return "full", "static-repro-command-not-allowed"
    observed = section.group(1)[fences[0].end():].strip()
    if not observed:
        return "full", "static-missing-observed-output"
    return "lite", "static-lite-candidate"


def artifact_dir(row: dict) -> Path:
    return Path(row["output_root"]) / "artifacts" / "runs" / row["id"]


def write_routing_receipt(row: dict, receipt: dict) -> Path:
    target_dir = artifact_dir(row)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "bugfix-routing-receipt.json"
    temporary = target.with_suffix(f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(target)
    return target


def wait_for_pre_envelope(row: dict, db: Path, timeout_s: int) -> tuple[str, str]:
    """Wait for the authoritative typed pre-envelope, never infer from failure."""
    envelope = artifact_dir(row) / "envelope-pre.txt"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if envelope.is_file():
            lines = [line.strip() for line in envelope.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines() if line.startswith("ROUTE=")]
            if lines == ["ROUTE=LITE"]:
                return "LITE", lines[0]
            if len(lines) == 1 and re.fullmatch(r"ROUTE=FULL reason=[a-z_]+(?:,[a-z_]+)*", lines[0]):
                reasons = lines[0].split("=", 2)[2].split(",")
                if "malformed" in reasons:
                    fail("lite pre-envelope is malformed; refusing automatic fallback")
                # The workflow must have stopped on this exact routing gate.
                status = status_for_run(db, row["id"])
                if status == "failed":
                    return "FULL", lines[0]
            elif lines:
                fail("lite pre-envelope has an invalid or duplicate ROUTE line")
        status = status_for_run(db, row["id"])
        if status in {"failed", "cancelled", "completed"}:
            fail(f"lite run ended status={status} without an authoritative pre-envelope route")
        time.sleep(0.25)
    fail(f"timed out waiting for authoritative lite pre-envelope for {row['id'][:8]}")


def run_claude_lane(lane: str, report: Path, db: Path) -> dict:
    log_dir = Path(os.environ.get("ARCHON_RUN_LOG_DIR", Path.home() / ".archon" / "logs"))
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    log = log_dir / f"archon-{lane}-{int(time.time())}-{os.getpid()}.log"
    env = dict(os.environ, DISABLE_OMC="1", ARCHON_DB=str(db))
    pid, _ = detached(log, command_for("run", f"{lane}\0{report}"), env)
    run_id = wait_for_run_id(log, pid)
    row = resolve_any_run(db, run_id, {lane})
    if Path(row["user_message"]).resolve() != report.resolve():
        fail("created Claude run does not match requested report")
    return row


def resolve_any_run(db: Path, prefix: str, lanes: set[str]) -> dict:
    if not ID_RE.fullmatch(prefix):
        fail(f"bad-id-format [{prefix}]")
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, workflow_name, user_message, status, output_root "
        "FROM remote_agent_workflow_runs WHERE id LIKE ? ORDER BY started_at DESC LIMIT 3",
        (prefix.lower() + "%",),
    ).fetchall()
    con.close()
    if len(rows) != 1:
        fail(f"expected one run matching {prefix}, found {len(rows)}")
    row = dict(rows[0])
    if row["workflow_name"] not in lanes:
        fail(f"run {row['id'][:8]} is unexpected lane {row['workflow_name']}")
    return row


def invoke_codex_lane(args: argparse.Namespace, lane: str, report: Path) -> dict:
    wall, tokens = CODEX_LANES[lane]
    command = [
        sys.executable, str(Path(__file__).resolve()),
        "--wall-minutes", str(wall), "--max-total-tokens", str(tokens),
        "--db", str(args.db), "--codex-home", str(args.codex_home),
        "--registry", str(args.registry), "--control-dir", str(args.control_dir),
        "run", lane, str(report),
    ]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, encoding="utf-8")
    if result.returncode != 0:
        fail((result.stdout + result.stderr).strip())
    control_line = re.search(r"CODEX_LITE_RUN=STARTED[^\n]*", result.stdout)
    match = re.search(r"CODEX_LITE_RUN=STARTED[^\n]*\brun=([0-9a-f]{8,32})", result.stdout)
    if not match:
        control_line = re.search(r"CODEX_LITE_RUN=FINISHED[^\n]*", result.stdout)
        match = re.search(r"CODEX_LITE_RUN=FINISHED[^\n]*\brun=([0-9a-f]{8,32})", result.stdout)
    if not match:
        fail("guarded Codex launcher returned no run id")
    row = resolve_any_run(args.db, match.group(1), {lane})
    row["_control_line"] = control_line.group(0)
    return row


def adaptive_bugfix(args: argparse.Namespace) -> None:
    report = Path(args.report)
    if not report.is_absolute() or not report.is_file():
        fail("bugfix report must be an existing absolute path")
    report = report.resolve()
    lane_map = {
        "claude": {"lite": "bugfix-lite", "full": "bugfix"},
        "codex": {"lite": "bugfix-lite-codex", "full": "bugfix-codex"},
    }
    continuing = bool(getattr(args, "chain_id", None) or getattr(args, "continuation_seed", None))
    if bool(getattr(args, "chain_id", None)) != bool(getattr(args, "continuation_seed", None)):
        fail("bugfix continuation requires both --chain-id and --continuation-seed")
    initial_parent = None
    initial_seed = None
    if continuing:
        state = read_chain_state(args.control_dir, args.chain_id)
        if state.get("provider") != args.provider:
            fail("bugfix continuation cannot change provider")
        if state.get("report_sha256") != hashlib.sha256(report.read_bytes()).hexdigest():
            fail("bugfix continuation report does not match the logical chain")
        key = hashlib.sha256(args.continuation_seed.encode()).hexdigest()
        entry = state.get("continuation_seeds", {}).get(key) or {}
        initial_seed = entry.get("seed")
        if not isinstance(initial_seed, dict):
            fail("bugfix continuation seed is not registered in the logical chain")
        initial_parent = state.get("current_run_id")
        state = consume_continuation_seed(
            args.control_dir, state["logical_chain_id"], args.continuation_seed, args.provider
        )
        static_lane, static_reason = "full", str(initial_seed.get("transition_type") or "guarded-continuation")
    else:
        static_lane, static_reason = static_bugfix_route(report)
        baseline = capture_bugfix_baseline(ROOT)
        state = start_bugfix_chain(args.control_dir, args.provider, report, baseline)
    os.environ["ARCHON_CONTROL_DIR"] = str(args.control_dir)

    def launch(kind: str, transition_reason: str, parent_run_id: str | None = None, seed: dict | None = None) -> dict:
        lane = lane_map[args.provider][kind]
        env_updates = chain_env(state, seed)
        with temporary_env(env_updates):
            row = (invoke_codex_lane(args, lane, report) if args.provider == "codex"
                   else run_claude_lane(lane, report, args.db))
        return row

    discarded = None
    envelope_result = None
    routed_by = static_reason
    active = launch(static_lane, static_reason, initial_parent, initial_seed)
    state = record_chain_run(args.control_dir, state, active, static_reason, initial_parent)
    if static_lane == "lite":
        route, envelope_result = wait_for_pre_envelope(
            active, args.db, int(os.environ.get("ARCHON_BUGFIX_ROUTE_TIMEOUT", "1800"))
        )
        if route == "FULL":
            discarded = active["id"]
            state = adopt_run_ledger(args.control_dir, state, active)
            state, seed = create_continuation_seed(args.control_dir, state, active["id"], "lite-envelope-full", 1)
            state = consume_continuation_seed(args.control_dir, state["logical_chain_id"], seed["nonce"], args.provider)
            prepare_continuation_bundle(args.control_dir, state, active, seed)
            active = launch("full", "lite-envelope-full", discarded, seed)
            state = record_chain_run(args.control_dir, state, active, "lite-envelope-full", discarded)
            routed_by = "lite-envelope-full"

    status = status_for_run(args.db, active["id"])
    if getattr(args, "no_watch", False) and status in {"failed", "cancelled"}:
        fail(f"selected {active['workflow_name']} run did not remain active")

    receipt = {
        "provider": args.provider,
        "report": str(report),
        "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
        "logical_chain_id": state["logical_chain_id"],
        "root_run_id": state.get("root_run_id"),
        "parent_run_id": discarded or initial_parent,
        "sequence": len(state.get("runs", [])) - 1,
        "baseline_commits": state.get("baseline", {}).get("commits", {}),
        "baseline_sha256": state.get("baseline", {}).get("sha256"),
        "static_decision": static_lane,
        "static_reason": static_reason,
        "envelope_result": envelope_result,
        "discarded_lite_run_id": discarded,
        "active_run_id": active["id"],
        "active_lane": active["workflow_name"],
        "routed_by": routed_by,
    }
    write_routing_receipt(active, receipt)
    if args.provider == "codex" and active.get("_control_line"):
        print(active["_control_line"])
    print(
        f"ARCHON_BUGFIX=STARTED provider={args.provider} lane={active['workflow_name']} "
        f"run={active['id'][:8]} chain={state['logical_chain_id']} routed_by={routed_by}"
    )
    if not getattr(args, "no_watch", False) and status != "MISSING":
        while True:
            result = supervise_exact_run(args.db, active["id"], getattr(args, "watch_timeout_seconds", 86400), 2.0)
            print("ARCHON_BUGFIX_SUPERVISION=" + result["state"].upper() + " " + redact_control_tokens(" ".join(
                f"{k}={v}" for k, v in result.items()
            )))
            if result.get("state") != "terminal":
                break
            proof_recovery = recovery_successor_required(active)
            fix_recovery = failed_fix_recovery_required(active)
            if not proof_recovery and not fix_recovery:
                break
            state = adopt_run_ledger(args.control_dir, state, active)
            transition = "proof-conflict-recovery"
            if proof_recovery:
                proof_rounds = int(state.get("counters", {}).get("proof_rounds", 0)) + 1
                state.setdefault("counters", {})["proof_rounds"] = proof_rounds
                state = write_chain_state(args.control_dir, state)
                if proof_rounds > MAX_RECOVERY_SUCCESSORS:
                    print(
                        f"CHAIN_CAP_REACHED chain={state['logical_chain_id']} "
                        f"proof_rounds={proof_rounds} recovery_successors={MAX_RECOVERY_SUCCESSORS}"
                    )
                    break
            if fix_recovery:
                failures = int(state.get("counters", {}).get("causal_fix_failures", 0)) + 1
                state.setdefault("counters", {})["causal_fix_failures"] = failures
                state = write_chain_state(args.control_dir, state)
                seal_failed_fix_evidence(active, failures, state)
                reset_failed_worktree(active)
                if failures >= 3:
                    state, architecture_token = issue_architecture_challenge(
                        args.control_dir, state, active["id"]
                    )
                    print(
                        f"ARCHITECTURE_SUSPECT chain={state['logical_chain_id']} "
                        f"causal_fix_failures={failures} guarded_architecture_receipt=required "
                        f"architecture_token={architecture_token} command=python3 {Path(__file__).resolve()} "
                        f"bugfix-architecture-approve {active['id']} --provider {args.provider} "
                        f"--reviewed-by '<human>' --reason '<architecture review>' --token {architecture_token}"
                    )
                    break
                transition = "failed-fix-investigation"
            parent = active["id"]
            state, seed = create_continuation_seed(
                args.control_dir, state, parent, transition, 1
            )
            state = consume_continuation_seed(
                args.control_dir, state["logical_chain_id"], seed["nonce"], args.provider
            )
            prepare_continuation_bundle(args.control_dir, state, active, seed)
            active = launch("full", transition, parent, seed)
            state = record_chain_run(
                args.control_dir, state, active, transition, parent
            )
            if args.provider == "codex" and active.get("_control_line"):
                print(active["_control_line"])
            write_routing_receipt(active, {
                **receipt,
                "parent_run_id": parent,
                "sequence": len(state.get("runs", [])) - 1,
                "discarded_lite_run_id": discarded,
                "active_run_id": active["id"],
                "active_lane": active["workflow_name"],
                "routed_by": transition,
            })
            print(
                f"ARCHON_BUGFIX=STARTED provider={args.provider} lane={active['workflow_name']} "
                f"run={active['id'][:8]} chain={state['logical_chain_id']} "
                f"routed_by={transition}"
            )



def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--wall-minutes", type=int)
    ap.add_argument("--max-total-tokens", type=int)
    ap.add_argument("--db", type=Path, default=Path.home() / ".archon" / "archon.db")
    ap.add_argument("--codex-home", type=Path,
                    default=Path(os.environ.get("CODEX_HOME", Path.home() / ".archon" / "codex-home")))
    ap.add_argument("--registry", type=Path, default=Path.home() / ".gitnexus" / "registry.json")
    ap.add_argument("--control-dir", type=Path,
                    default=Path(os.environ.get("ARCHON_CONTROL_DIR", DEFAULT_CONTROL_DIR)))
    sub = ap.add_subparsers(dest="action", required=True)
    run = sub.add_parser("run")
    run.add_argument("lane", choices=sorted(LANES))
    run.add_argument("spec")
    for action in ("resume", "approve", "abandon"):
        p = sub.add_parser(action)
        p.add_argument("run_id")
        p.add_argument("--token", required=True)
    reject = sub.add_parser("reject")
    reject.add_argument("run_id")
    reject.add_argument("reason")
    reject.add_argument("--token", required=True)
    bugfix = sub.add_parser("bugfix")
    bugfix.add_argument("--provider", choices=("claude", "codex"), required=True)
    bugfix.add_argument("--no-watch", action="store_true")
    bugfix.add_argument("--watch-timeout-seconds", type=int, default=86400)
    bugfix.add_argument("--chain-id")
    bugfix.add_argument("--continuation-seed")
    bugfix.add_argument("report")
    supervise = sub.add_parser("supervise")
    supervise.add_argument("run_id")
    supervise.add_argument("--timeout-seconds", type=int, default=86400)
    supervise.add_argument("--interval-s", type=float, default=2.0)
    supervise.add_argument("--handoff-file", type=Path)
    successor = sub.add_parser("bugfix-successor-seed")
    successor.add_argument("run_id")
    successor.add_argument("--provider", choices=("claude", "codex"), required=True)
    successor.add_argument("--transition-type", required=True)
    successor.add_argument("--successor-budget", type=int, default=1)
    successor.add_argument("--chain-id")
    continuation = sub.add_parser("import-continuation")
    continuation.add_argument("--artifacts", type=Path, required=True)
    continuation.add_argument("--finalize-ledger", action="store_true")
    architecture = sub.add_parser("bugfix-architecture-approve")
    architecture.add_argument("run_id")
    architecture.add_argument("--provider", choices=("claude", "codex"), required=True)
    architecture.add_argument("--reviewed-by", required=True)
    architecture.add_argument("--reason", required=True)
    architecture.add_argument("--token", required=True)
    architecture.add_argument("--chain-id")
    sub.add_parser("check")
    return ap


def main() -> None:
    args = parser().parse_args()
    if args.action == "bugfix":
        adaptive_bugfix(args)
        return
    if args.action == "supervise":
        supervise_command(args)
        return
    if args.action == "bugfix-successor-seed":
        validate_control_location(args.control_dir)
        emit_successor_seed(args)
        return
    if args.action == "import-continuation":
        validate_control_location(args.control_dir)
        import_continuation_bundle(args)
        return
    if args.action == "bugfix-architecture-approve":
        validate_control_location(args.control_dir)
        approve_architecture_successor(args)
        return
    if args.action == "run":
        default_wall, default_tokens = CODEX_LANES[args.lane]
        args.wall_minutes = default_wall if args.wall_minutes is None else args.wall_minutes
        args.max_total_tokens = default_tokens if args.max_total_tokens is None else args.max_total_tokens
    elif args.action not in {"check"}:
        # Continuations inherit the limits authenticated in private run state.
        preview = resolve_run(args.db, args.run_id)
        control = require_control_token(preview, args.control_dir, args.token)
        args.wall_minutes = control.get("wall_minutes") if args.wall_minutes is None else args.wall_minutes
        args.max_total_tokens = control.get("max_total_tokens") if args.max_total_tokens is None else args.max_total_tokens
    else:
        args.wall_minutes = 90 if args.wall_minutes is None else args.wall_minutes
        args.max_total_tokens = 8_000_000 if args.max_total_tokens is None else args.max_total_tokens
    if not isinstance(args.wall_minutes, int) or not isinstance(args.max_total_tokens, int) or args.wall_minutes <= 0 or args.max_total_tokens <= 0:
        fail("wall/token budgets must be positive")
    validate_control_location(args.control_dir)
    if args.action == "check":
        install_private_codex_wrapper(args.control_dir)
        stage_private_codex_skills(ROOT, args.codex_home)
        ensure_environment(ROOT, args.codex_home, args.registry)
        print("CODEX_LITE_RUN=READY lanes=" + ",".join(sorted(LANES)))
        return

    row = None
    previous_control = None
    bugfix_control = None
    if args.action == "run":
        raw_spec = Path(args.spec)
        if not raw_spec.is_absolute() or not raw_spec.is_file():
            fail("run spec must be an existing absolute path")
        target = f"{args.lane}\0{raw_spec.resolve()}"
    else:
        row = resolve_run(args.db, args.run_id)
        allowed = {
            "resume": {"failed"},
            "approve": {"paused"},
            "reject": {"paused"},
            "abandon": {"running", "paused", "failed"},
        }[args.action]
        if row["status"] not in allowed:
            fail(f"{args.action} requires status {sorted(allowed)}, got {row['status']}")
        previous_control = require_control_token(row, args.control_dir, args.token)
        bugfix_control = previous_control.get("bugfix_chain") if isinstance(previous_control, dict) else None
        if (args.action != "abandon" and not isinstance(bugfix_control, dict)
                and row.get("workflow_name") in {
            "bugfix", "bugfix-lite", "bugfix-codex", "bugfix-lite-codex"
        }):
            public_chain_path = artifact_dir(row) / "bugfix-chain.json"
            try:
                public_chain = json.loads(public_chain_path.read_text(encoding="utf-8"))
                bugfix_control = {"logical_chain_id": public_chain["logical_chain_id"]}
            except FileNotFoundError:
                bugfix_control = None  # Legacy/synthetic guarded runs predate v2 lineage.
            except (OSError, KeyError, json.JSONDecodeError) as exc:
                fail(f"bugfix continuation cannot recover chain identity: {exc}")
        if isinstance(bugfix_control, dict) and bugfix_control.get("logical_chain_id"):
            state = read_chain_state(args.control_dir, bugfix_control["logical_chain_id"])
            if state.get("current_run_id") != row["id"]:
                fail("stored bugfix chain does not name the resumed run as current")
            os.environ["ARCHON_CONTROL_DIR"] = str(args.control_dir)
            os.environ.update(chain_env(state))
        target = row["id"]

    # Abandon is the emergency stop. It must not be blocked by expired auth,
    # busy dev ports, or a stale index, all of which are start-time checks.
    if args.action not in {"abandon", "reject"}:
        stage_private_codex_skills(ROOT, args.codex_home)
        lane = args.lane if args.action == "run" else row["workflow_name"]
        stored_baseline = None
        if isinstance(bugfix_control, dict):
            chain_id = bugfix_control.get("logical_chain_id")
            if chain_id:
                stored_baseline = read_chain_state(args.control_dir, chain_id).get("baseline")
        # A paused gate may deliberately keep its smoke stack live. Only a
        # fresh run needs to acquire free lane ports. A failed-run resume may
        # deliberately retain its own smoke stack; node-level resource gates
        # remain authoritative for any later acquisition.
        ensure_environment(
            ROOT, args.codex_home, args.registry, lane, stored_baseline,
            check_ports=args.action == "run",
        )
        private_codex_wrapper = install_private_codex_wrapper(args.control_dir)
    elif args.action == "reject":
        # Rejection is a human stop signal, not a new execution environment.
        # Authenticate its control token and retain the enforced Codex binary,
        # but never make stopping a bad run depend on ports, repo tooling,
        # provider auth freshness, or an evidence index.
        private_codex_wrapper = install_private_codex_wrapper(args.control_dir)
    else:
        private_codex_wrapper = None

    next_control_token = secrets.token_urlsafe(32) if args.action != "abandon" else None
    guard_file = create_guard_file(args.control_dir) if args.action != "abandon" else None
    env = dict(os.environ)
    env.update({
        "ARCHON_DB": str(args.db),
        "DISABLE_OMC": "1",
        "CODEX_HOME": str(args.codex_home),
    })
    if guard_file is not None:
        env["ARCHON_CODEX_LITE_GUARD_FILE"] = str(guard_file)
    if private_codex_wrapper is not None:
        env["CODEX_BIN_PATH"] = str(private_codex_wrapper)
        env["CODEX_REAL_BIN"] = str(Path(shutil.which("codex") or "codex").resolve())
        env["CODEX_WORKSPACE_ROOT"] = str(ROOT)
        env["CODEX_ARTIFACTS_BASE"] = str(
            USER_HOME / ".archon" / "workspaces" / "_folder" / "goodword" / "artifacts" / "runs"
        )
    log_dir = Path(os.environ.get("CODEX_LITE_LOG_DIR", Path.home() / ".archon" / "logs"))
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = f"{int(time.time())}-{os.getpid()}"
    workflow_log = log_dir / f"codex-lite-{args.action}-{stamp}.log"
    command = command_for(args.action, target, getattr(args, "reason", None))

    if args.action == "abandon":
        stop_controlled_processes(row, args.control_dir)
        result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, encoding="utf-8")
        if result.returncode != 0:
            fail((result.stdout + result.stderr).strip())
        final_status = status_for_run(args.db, row["id"])
        if final_status != "cancelled":
            fail(f"abandon returned success but run status is {final_status}")
        print(f"CODEX_LITE_RUN=ABANDONED run={row['id'][:8]}")
        return

    launcher_pid = launcher_pgid = watchdog_pid = watchdog_pgid = None
    launcher_fingerprint = watchdog_fingerprint = None
    try:
        launcher_pid, launcher_pgid = detached(workflow_log, command, env)
        launcher_fingerprint = process_fingerprint(launcher_pgid)
        if not launcher_fingerprint:
            fail("launcher supervisor exited before its process fingerprint was captured")
        if args.action == "run":
            run_id = wait_for_run_id(workflow_log, launcher_pid)
            row = resolve_run(args.db, run_id)
            if row["workflow_name"] != args.lane or Path(row["user_message"]).resolve() != raw_spec.resolve():
                fail("created run does not match requested lane/spec")

        watchdog_log = log_dir / f"codex-watchdog-{row['id']}-{stamp}.log"
        arm_file = log_dir / f"codex-watchdog-{row['id']}-{stamp}.armed"
        try:
            arm_file.unlink()
        except FileNotFoundError:
            pass
        watchdog_pid, watchdog_pgid = detached(
            watchdog_log,
            watchdog_command(row["id"], launcher_pgid, launcher_fingerprint,
                             args.wall_minutes,
                             args.max_total_tokens, args.db, args.codex_home,
                             arm_file, os.environ.get("ARCHON_BUGFIX_CHAIN_ID")),
            env,
        )
        watchdog_fingerprint = process_fingerprint(watchdog_pgid)
        if not watchdog_fingerprint:
            fail("watchdog supervisor exited before its process fingerprint was captured")
        write_control_records(
            row, args.control_dir, next_control_token, args.action,
            launcher_pid, launcher_pgid, launcher_fingerprint,
            workflow_log, watchdog_log, watchdog_pid, watchdog_pgid,
            watchdog_fingerprint, arm_file, False,
            args.wall_minutes, args.max_total_tokens,
        )
        arm_status = wait_for_watchdog_arm(
            arm_file, watchdog_pgid, row, launcher_pgid, args.db, watchdog_log
        )
        if arm_status != "armed":
            write_control_records(
                row, args.control_dir, next_control_token, args.action,
                launcher_pid, launcher_pgid, launcher_fingerprint,
                workflow_log, watchdog_log, watchdog_pid, watchdog_pgid,
                watchdog_fingerprint, arm_file, False,
                args.wall_minutes, args.max_total_tokens,
            )
            if arm_status == "completed":
                wait_for_guard_consumed(guard_file, row, args.db)
            elif guard_file is not None:
                try:
                    guard_file.unlink()
                except FileNotFoundError:
                    pass
            if args.action == "reject":
                cleanup_after_rejection(row, previous_control, args.db)
            print(
                f"CODEX_LITE_RUN=FINISHED action={args.action} run={row['id'][:8]} "
                f"status={arm_status} log={workflow_log} watchdog_log={watchdog_log}"
            )
            return
        wait_for_guard_consumed(guard_file, row, args.db)
        write_control_records(
            row, args.control_dir, next_control_token, args.action,
            launcher_pid, launcher_pgid, launcher_fingerprint,
            workflow_log, watchdog_log, watchdog_pid, watchdog_pgid,
            watchdog_fingerprint, arm_file, True,
            args.wall_minutes, args.max_total_tokens,
        )
    except BaseException:
        for label, pgid, fingerprint in (
            ("watchdog", watchdog_pgid, watchdog_fingerprint),
            ("launcher", launcher_pgid, launcher_fingerprint),
        ):
            if pgid is None:
                continue
            try:
                terminate_group(pgid, expected_fingerprint=fingerprint)
            except BaseException as cleanup_exc:
                print(
                    f"CODEX_LITE_RUN=CLEANUP_FAIL process={label} pgid={pgid} "
                    f"detail={cleanup_exc}", file=sys.stderr,
                )
        abandon_if_orphaned(row, args.db, env)
        # A continuation rotates its token before AI work can begin. If the
        # arming/guard handshake fails before the replacement token is printed,
        # restore the last operator-known capability instead of stranding the
        # paused/failed run behind an undisclosed hash.
        if previous_control is not None:
            secure_write_json(control_state_path(row, args.control_dir), previous_control)
        if guard_file is not None:
            try:
                guard_file.unlink()
            except FileNotFoundError:
                pass
        raise
    print(
        f"CODEX_LITE_RUN=STARTED action={args.action} run={row['id'][:8]} "
        f"launcher_pid={launcher_pid} launcher_pgid={launcher_pgid} "
        f"watchdog_pid={watchdog_pid} control_token={next_control_token} "
        f"log={workflow_log} watchdog_log={watchdog_log}"
    )


if __name__ == "__main__":
    os.umask(0o077)
    def _interrupt(signum, _frame):
        raise KeyboardInterrupt(f"received signal {signum}")
    signal.signal(signal.SIGINT, _interrupt)
    signal.signal(signal.SIGTERM, _interrupt)
    main()
