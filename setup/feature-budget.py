#!/usr/bin/env python3
"""Private shared-budget ledger for repository-list Codex feature chains.

This helper is intentionally separate from feature chain state.  Watchdogs
update active intervals frequently, while controller feature state is HMAC
sealed around plan and handoff authority.  Keeping budget state in its own
private file avoids controller-state MAC races and gives every stage one shared
wall/token cap.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import glob
import hashlib
import json
import os
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import control_contract  # noqa: E402


CHAIN_ID_RE = re.compile(r"[0-9a-f]{24,64}", re.I)
RUN_ID_RE = re.compile(
    r"(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{8,36})",
    re.I,
)
DEFAULT_WALL_MINUTES = 240
DEFAULT_MAX_TOTAL_TOKENS = 30_000_000


class BudgetError(ValueError):
    pass


def fail(reason: str) -> None:
    print(f"FEATURE_BUDGET=FAIL {reason}", file=sys.stderr)
    raise SystemExit(1)


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_control_dir(control_dir: Path) -> None:
    try:
        control_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = control_dir.lstat()
    except OSError as exc:
        raise BudgetError(f"private control directory unavailable: {exc}") from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise BudgetError(f"private control directory must be an owned mode-0700 directory: {control_dir}")


def budget_dir(control_dir: Path) -> Path:
    ensure_control_dir(control_dir)
    path = control_dir / "feature-budgets"
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise BudgetError(f"feature budget directory must be owned mode-0700: {path}")
    return path


def validate_chain_id(chain_id: str) -> str:
    if not CHAIN_ID_RE.fullmatch(chain_id):
        raise BudgetError(f"bad feature chain id: {chain_id}")
    return chain_id.lower()


def validate_run_id(run_id: str) -> str:
    if not RUN_ID_RE.fullmatch(run_id):
        raise BudgetError(f"bad run id: {run_id}")
    return run_id.lower()


def budget_path(control_dir: Path, chain_id: str) -> Path:
    return budget_dir(control_dir) / f"{validate_chain_id(chain_id)}.json"


@contextlib.contextmanager
def locked_budget(control_dir: Path, chain_id: str):
    directory = budget_dir(control_dir)
    lock_path = directory / f"{validate_chain_id(chain_id)}.lock"
    with open(lock_path, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield budget_path(control_dir, chain_id)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def read_budget(path: Path) -> dict[str, Any]:
    try:
        return control_contract.secure_read_json(path)
    except control_contract.ControlContractError as exc:
        raise BudgetError(str(exc)) from exc


def write_budget(path: Path, state: dict[str, Any]) -> None:
    try:
        control_contract.secure_write_json(path, state)
    except control_contract.ControlContractError as exc:
        raise BudgetError(str(exc)) from exc


def process_fingerprint(pid: int) -> str | None:
    result = subprocess.run(
        ["ps", "-o", "lstart=", "-o", "command=", "-p", str(pid)],
        capture_output=True,
        encoding="utf-8",
    )
    value = result.stdout.strip()
    return value or None


def process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def validate_group_identity(pgid: int, fingerprint: str) -> None:
    if not isinstance(pgid, int) or pgid <= 1 or pgid == os.getpgrp():
        raise BudgetError(f"refusing unsafe process group {pgid}")
    try:
        if os.getpgid(pgid) != pgid:
            raise BudgetError(f"process group {pgid} leader is not the group leader")
    except ProcessLookupError as exc:
        raise BudgetError(f"process group {pgid} is not alive") from exc
    if not isinstance(fingerprint, str) or not fingerprint.strip():
        raise BudgetError("process group fingerprint is required")
    current = process_fingerprint(pgid)
    if current != fingerprint:
        raise BudgetError(f"process group {pgid} fingerprint mismatch")


def terminate_group(pgid: int, fingerprint: str, wait_s: float) -> str:
    try:
        validate_group_identity(pgid, fingerprint)
    except BudgetError:
        return "refused-fingerprint"
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return "already-exited"
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if not process_group_exists(pgid):
            return "term-exited"
        time.sleep(0.05)
    if process_fingerprint(pgid) != fingerprint:
        return "refused-kill-fingerprint"
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return "term-exited"
    deadline = time.time() + min(wait_s, 2.0)
    while time.time() < deadline:
        if not process_group_exists(pgid):
            return "term+kill"
        time.sleep(0.05)
    return "survived-sigkill"


def require_positive_int(value: int, name: str) -> int:
    if not isinstance(value, int) or value <= 0:
        raise BudgetError(f"{name} must be a positive integer")
    return value


def command_init(args: argparse.Namespace) -> None:
    wall_minutes = require_positive_int(args.wall_minutes, "wall minutes")
    max_total_tokens = require_positive_int(args.max_total_tokens, "max total tokens")
    with locked_budget(args.control_dir, args.chain_id) as path:
        if path.exists() and not args.replace:
            state = read_budget(path)
            if (
                state.get("wall_seconds") != wall_minutes * 60
                or state.get("max_total_tokens") != max_total_tokens
            ):
                raise BudgetError("feature budget already exists with different limits")
            print(f"FEATURE_BUDGET=READY chain={args.chain_id} existing=true")
            return
        state = {
            "schema_version": 1,
            "kind": "archon-feature-shared-budget",
            "logical_chain_id": validate_chain_id(args.chain_id),
            "wall_seconds": wall_minutes * 60,
            "max_total_tokens": max_total_tokens,
            "runs": [],
            "active_intervals": [],
            "token_high_water": {
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            },
            "session_token_high_water": {},
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        write_budget(path, state)
    print(f"FEATURE_BUDGET=READY chain={args.chain_id} existing=false")


def find_run(state: dict[str, Any], run_id: str) -> dict[str, Any] | None:
    for run in state.get("runs", []):
        if run.get("run_id") == run_id:
            return run
    return None


def relative_session_path(codex_home: Path, session_file: Path) -> str:
    resolved_home = codex_home.resolve()
    resolved_file = session_file.resolve()
    try:
        rel = resolved_file.relative_to(resolved_home)
    except ValueError as exc:
        raise BudgetError(f"session file must live under codex home: {session_file}") from exc
    if session_file.is_dir():
        raise BudgetError(f"session file is a directory: {session_file}")
    return rel.as_posix()


def validate_session_id(session_id: str) -> str:
    if not isinstance(session_id, str) or not session_id.strip():
        raise BudgetError("empty Codex session id")
    value = session_id.strip()
    if "/" in value or "\\" in value or value in {".", ".."}:
        raise BudgetError(f"bad session id: {session_id}")
    return value


def ensure_run(state: dict[str, Any], run_id: str, stage: str | None = None) -> dict[str, Any]:
    run = find_run(state, run_id)
    if run is None:
        run = {
            "run_id": run_id,
            "stage": stage,
            "session_files": [],
            "session_ids": [],
            "bound_at": utc_now(),
        }
        state.setdefault("runs", []).append(run)
    if "session_files" not in run:
        run["session_files"] = []
    if "session_ids" not in run:
        run["session_ids"] = []
    run["stage"] = stage or run.get("stage")
    run["updated_at"] = utc_now()
    return run


def add_session_files(run: dict[str, Any], session_files: list[str]) -> int:
    existing = set(run.get("session_files", []))
    added = 0
    for session_file in session_files:
        if session_file not in existing:
            run.setdefault("session_files", []).append(session_file)
            existing.add(session_file)
            added += 1
    return added


def add_session_ids(run: dict[str, Any], session_ids: list[str]) -> int:
    existing = set(run.get("session_ids", []))
    added = 0
    for session_id in session_ids:
        if session_id not in existing:
            run.setdefault("session_ids", []).append(session_id)
            existing.add(session_id)
            added += 1
    return added


def command_bind_run(args: argparse.Namespace) -> None:
    run_id = validate_run_id(args.run_id)
    session_files = [relative_session_path(args.codex_home, item) for item in args.session_file]
    session_ids = [validate_session_id(item) for item in args.session_id]
    with locked_budget(args.control_dir, args.chain_id) as path:
        state = read_budget(path)
        run = ensure_run(state, run_id, args.stage)
        file_count = add_session_files(run, session_files)
        id_count = add_session_ids(run, session_ids)
        state["updated_at"] = utc_now()
        write_budget(path, state)
    print(
        f"FEATURE_BUDGET=BOUND chain={args.chain_id} run={run_id[:8]} "
        f"session_files={file_count} session_ids={id_count}"
    )


def command_bind_session(args: argparse.Namespace) -> None:
    run_id = validate_run_id(args.run_id)
    session_id = validate_session_id(args.session_id)
    with locked_budget(args.control_dir, args.chain_id) as path:
        state = read_budget(path)
        run = ensure_run(state, run_id, args.stage)
        added = add_session_ids(run, [session_id])
        state["updated_at"] = utc_now()
        write_budget(path, state)
    print(f"FEATURE_BUDGET=SESSION chain={args.chain_id} run={run_id[:8]} added={added}")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError as exc:
        raise BudgetError(f"evidence file cannot be read: {path}: {exc}") from exc
    return h.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def non_negative_int(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise BudgetError(f"{name} must be a non-negative integer")
    return value


def normalize_model(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BudgetError("provider usage model is missing")
    return re.sub(r"\[[^\]]+\]\s*$", "", value.strip())


def event_row_by_id(db: Path, event_id: str) -> dict[str, Any]:
    if not isinstance(event_id, str) or not event_id.strip():
        raise BudgetError("provider usage event id is required")
    try:
        con = sqlite3.connect(db)
        try:
            if not table_exists(con, "remote_agent_workflow_events"):
                raise BudgetError("workflow events table is unavailable")
            columns = [row[1] for row in con.execute("PRAGMA table_info(remote_agent_workflow_events)").fetchall()]
            if "id" not in columns:
                raise BudgetError("workflow events table has no event id column")
            row = con.execute(
                f"SELECT {', '.join(columns)} FROM remote_agent_workflow_events WHERE id = ?",
                (event_id,),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise BudgetError(f"workflow event lookup failed: {exc}") from exc
    if row is None:
        raise BudgetError("provider usage event was not found")
    return dict(zip(columns, row))


def event_tool_ids(db: Path, run_id: str) -> set[str]:
    try:
        con = sqlite3.connect(db)
        try:
            if not table_exists(con, "remote_agent_workflow_events"):
                raise BudgetError("workflow events table is unavailable")
            columns = {row[1] for row in con.execute("PRAGMA table_info(remote_agent_workflow_events)").fetchall()}
            if "workflow_run_id" not in columns or "data" not in columns:
                return set()
            rows = con.execute(
                "SELECT data FROM remote_agent_workflow_events "
                "WHERE workflow_run_id = ? AND event_type IN ('tool_called', 'tool_completed')",
                (run_id,),
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise BudgetError(f"workflow tool ownership lookup failed: {exc}") from exc
    ids: set[str] = set()
    for (raw,) in rows:
        try:
            data = json.loads(raw) if isinstance(raw, str) and raw else {}
        except ValueError:
            continue
        for key in ("tool_call_id", "toolUseID", "tool_use_id"):
            value = data.get(key) if isinstance(data, dict) else None
            if isinstance(value, str) and value:
                ids.add(value)
    return ids


def parse_provider_event(db: Path, event_id: str, run_id: str) -> dict[str, Any]:
    row = event_row_by_id(db, event_id)
    if row.get("workflow_run_id") != run_id:
        raise BudgetError("provider usage event does not belong to the registered run")
    if row.get("event_type") != "node_completed":
        raise BudgetError("provider usage event must be a completed node event")
    raw = row.get("data")
    if not isinstance(raw, str) or not raw:
        raise BudgetError("provider usage event has no JSON data")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise BudgetError("provider usage event data is malformed") from exc
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        raise BudgetError("provider usage event is missing token totals")
    inclusive_input = non_negative_int(tokens.get("input"), "event input tokens")
    output_tokens = non_negative_int(tokens.get("output"), "event output tokens")
    cache_read = non_negative_int(tokens.get("cacheRead"), "event cache read tokens")
    cache_write = non_negative_int(tokens.get("cacheWrite"), "event cache write tokens")
    uncached_input = inclusive_input - cache_read - cache_write
    if uncached_input < 0:
        raise BudgetError("provider usage event cache tokens exceed inclusive input")
    model_usage = data.get("model_usage")
    if not isinstance(model_usage, dict):
        raise BudgetError("provider usage event is missing model usage")
    model = normalize_model(model_usage.get("resolved"))
    provider = "claude" if model.startswith("claude-") else "unknown"
    if provider == "unknown":
        raise BudgetError("provider usage event model is not a supported provider model")
    return {
        "event_id": event_id,
        "event_sha256": sha256_text(raw),
        "run_id": run_id,
        "node": row.get("step_name") or row.get("node_name"),
        "provider": provider,
        "model": model,
        "event_model": model_usage.get("resolved"),
        "provider_usage": {
            "uncached_input_tokens": uncached_input,
            "cache_creation_input_tokens": cache_write,
            "cache_read_input_tokens": cache_read,
            "inclusive_input_tokens": inclusive_input,
            "output_tokens": output_tokens,
            "total_tokens": inclusive_input + output_tokens,
        },
        "usage": {
            "input_tokens": inclusive_input,
            "cached_input_tokens": cache_read,
            "output_tokens": output_tokens,
            "total_tokens": inclusive_input + output_tokens,
        },
    }


def normalized_provider_usage(raw: dict[str, int]) -> dict[str, int]:
    return {
        "input_tokens": raw["inclusive_input_tokens"],
        "cached_input_tokens": raw["cache_read_input_tokens"],
        "output_tokens": raw["output_tokens"],
        "total_tokens": raw["total_tokens"],
    }


def raw_provider_usage(raw: dict[str, int]) -> dict[str, int]:
    return {
        "uncached_input_tokens": raw["input_tokens"],
        "cache_creation_input_tokens": raw["cache_creation_input_tokens"],
        "cache_read_input_tokens": raw["cache_read_input_tokens"],
        "inclusive_input_tokens": raw["input_tokens"] + raw["cache_creation_input_tokens"] + raw["cache_read_input_tokens"],
        "output_tokens": raw["output_tokens"],
        "total_tokens": raw["total_tokens"],
    }


def transcript_tool_ids_from_content(content: Any) -> tuple[set[str], set[str]]:
    tool_ids: set[str] = set()
    tool_names: set[str] = set()
    if not isinstance(content, list):
        return tool_ids, tool_names
    for item in content:
        if not isinstance(item, dict):
            continue
        value = item.get("id") or item.get("tool_use_id")
        if isinstance(value, str) and value.startswith("toolu_"):
            tool_ids.add(value)
        name = item.get("name")
        if isinstance(name, str):
            tool_names.add(name)
    return tool_ids, tool_names


def parse_provider_transcript(path: Path) -> dict[str, Any]:
    transcript_sha = sha256_file(path)
    usage_by_message: dict[str, dict[str, int]] = {}
    usage_rows = 0
    tool_ids: set[str] = set()
    tool_names: set[str] = set()
    models: set[str] = set()
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    data = json.loads(line)
                except ValueError as exc:
                    raise BudgetError("provider transcript contains malformed JSONL") from exc
                message = data.get("message")
                if not isinstance(message, dict):
                    continue
                model = message.get("model")
                if isinstance(model, str) and model.strip():
                    models.add(normalize_model(model))
                found_ids, found_names = transcript_tool_ids_from_content(message.get("content"))
                tool_ids.update(found_ids)
                tool_names.update(found_names)
                usage = message.get("usage")
                message_id = message.get("id")
                if not isinstance(usage, dict):
                    continue
                usage_rows += 1
                if not isinstance(message_id, str) or not message_id.strip():
                    raise BudgetError("provider transcript usage row is missing a message id")
                item = {
                    "input_tokens": non_negative_int(usage.get("input_tokens", 0), "transcript input tokens"),
                    "cache_creation_input_tokens": non_negative_int(
                        usage.get("cache_creation_input_tokens", 0), "transcript cache creation tokens"
                    ),
                    "cache_read_input_tokens": non_negative_int(
                        usage.get("cache_read_input_tokens", 0), "transcript cache read tokens"
                    ),
                    "output_tokens": non_negative_int(usage.get("output_tokens", 0), "transcript output tokens"),
                }
                if message_id in usage_by_message and usage_by_message[message_id] != item:
                    raise BudgetError("provider transcript has conflicting duplicate usage rows")
                usage_by_message[message_id] = item
    except OSError as exc:
        raise BudgetError(f"provider transcript cannot be read: {path}: {exc}") from exc
    if not usage_by_message:
        raise BudgetError("provider transcript has no usage rows")
    if tool_names.intersection({"Agent", "Task"}):
        raise BudgetError("provider transcript contains unsupported child-agent tool usage")
    totals = {"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
              "output_tokens": 0}
    for usage in usage_by_message.values():
        totals["input_tokens"] += usage["input_tokens"]
        totals["cache_creation_input_tokens"] += usage["cache_creation_input_tokens"]
        totals["cache_read_input_tokens"] += usage["cache_read_input_tokens"]
        totals["output_tokens"] += usage["output_tokens"]
    provider_usage = raw_provider_usage({
        **totals,
        "total_tokens": totals["input_tokens"] + totals["cache_creation_input_tokens"]
        + totals["cache_read_input_tokens"] + totals["output_tokens"],
    })
    return {
        "transcript_sha256": transcript_sha,
        "model": next(iter(models)) if len(models) == 1 else None,
        "models": sorted(models),
        "usage_rows": usage_rows,
        "unique_message_ids": len(usage_by_message),
        "tool_use_ids": sorted(tool_ids),
        "provider_usage": provider_usage,
        "usage": normalized_provider_usage(provider_usage),
    }


def provider_receipt_totals(state: dict[str, Any]) -> dict[str, int]:
    totals = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    for receipt in state.get("provider_usage_receipts", []):
        if not isinstance(receipt, dict):
            raise BudgetError("provider usage receipt is malformed")
        usage = receipt.get("usage")
        if not isinstance(usage, dict):
            raise BudgetError("provider usage receipt is missing usage")
        totals["input_tokens"] += non_negative_int(usage.get("input_tokens", 0), "receipt input tokens")
        totals["cached_input_tokens"] += non_negative_int(usage.get("cached_input_tokens", 0), "receipt cached input tokens")
        totals["output_tokens"] += non_negative_int(usage.get("output_tokens", 0), "receipt output tokens")
        totals["total_tokens"] += non_negative_int(usage.get("total_tokens", 0), "receipt total tokens")
    return totals


def validate_provider_receipt(db: Path, run_id: str, event_id: str, transcript: Path) -> dict[str, Any]:
    event = parse_provider_event(db, event_id, run_id)
    transcript_usage = parse_provider_transcript(transcript)
    if transcript_usage["model"] != event["model"]:
        raise BudgetError("provider transcript model does not match the completed event")
    if transcript_usage["provider_usage"] != event["provider_usage"]:
        raise BudgetError("provider transcript usage does not match the completed event")
    owned_tool_ids = event_tool_ids(db, run_id)
    transcript_tool_ids = set(transcript_usage["tool_use_ids"])
    if not transcript_tool_ids.issubset(owned_tool_ids):
        raise BudgetError("provider transcript includes tool calls not owned by the registered run")
    return {
        "kind": "provider-usage-receipt",
        "run_id": run_id,
        "provider": event["provider"],
        "model": event["model"],
        "node": event["node"],
        "event_id": event_id,
        "event_sha256": event["event_sha256"],
        "transcript_path": str(transcript),
        "transcript_sha256": transcript_usage["transcript_sha256"],
        "usage_rows": transcript_usage["usage_rows"],
        "unique_message_ids": transcript_usage["unique_message_ids"],
        "tool_use_ids": len(transcript_usage["tool_use_ids"]),
        "provider_usage": transcript_usage["provider_usage"],
        "usage": transcript_usage["usage"],
        "recorded_at": utc_now(),
    }


def command_account_provider_usage(args: argparse.Namespace) -> None:
    run_id = validate_run_id(args.run_id)
    with locked_budget(args.control_dir, args.chain_id) as path:
        state = read_budget(path)
        if find_run(state, run_id) is None:
            raise BudgetError("provider usage run is not registered in this ledger")
        receipt = validate_provider_receipt(args.db, run_id, args.event_id, args.transcript)
        receipts = state.setdefault("provider_usage_receipts", [])
        for existing in receipts:
            if existing.get("event_id") == receipt["event_id"]:
                if (
                    existing.get("transcript_sha256") == receipt["transcript_sha256"]
                    and existing.get("usage") == receipt["usage"]
                    and existing.get("run_id") == receipt["run_id"]
                ):
                    print(f"FEATURE_BUDGET=PROVIDER_USAGE chain={args.chain_id} run={run_id[:8]} existing=true")
                    return
                raise BudgetError("provider usage event was already recorded with different evidence")
            if existing.get("transcript_sha256") == receipt["transcript_sha256"]:
                raise BudgetError("provider usage transcript was already recorded for a different event")
        receipts.append(receipt)
        state["updated_at"] = utc_now()
        write_budget(path, state)
    print(
        f"FEATURE_BUDGET=PROVIDER_USAGE chain={args.chain_id} run={run_id[:8]} "
        f"provider={receipt['provider']} total={receipt['usage']['total_tokens']} existing=false"
    )


def table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def db_session_ids(db: Path, run_id: str) -> list[str]:
    try:
        con = sqlite3.connect(db)
        try:
            if not table_exists(con, "remote_agent_workflow_run_node_sessions"):
                raise BudgetError("workflow run node session table is unavailable")
            rows = con.execute(
                "SELECT provider_session_id FROM remote_agent_workflow_run_node_sessions "
                "WHERE workflow_run_id = ? AND provider = ? ORDER BY node_id",
                (run_id, "codex"),
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise BudgetError(f"workflow session lookup failed: {exc}") from exc
    session_ids = []
    for (session_id,) in rows:
        try:
            session_ids.append(validate_session_id(session_id))
        except BudgetError as exc:
            raise BudgetError(f"run {run_id[:8]} has malformed Codex session id") from exc
    return session_ids


def read_session_meta_payload(path: Path) -> dict[str, Any] | None:
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise BudgetError(f"session file must be an owned regular non-symlink file: {path}")
        with path.open(encoding="utf-8") as fh:
            first = fh.readline()
    except OSError as exc:
        raise BudgetError(f"session file cannot be read: {path}: {exc}") from exc
    try:
        data = json.loads(first)
    except ValueError:
        return None
    if data.get("type") != "session_meta":
        return None
    payload = data.get("payload")
    return payload if isinstance(payload, dict) else None


def session_parent_id(payload: dict[str, Any]) -> str | None:
    source = payload.get("source")
    if not isinstance(source, dict):
        return None
    subagent = source.get("subagent")
    if not isinstance(subagent, dict):
        return None
    spawn = subagent.get("thread_spawn")
    if not isinstance(spawn, dict):
        return None
    parent = spawn.get("parent_thread_id")
    if not isinstance(parent, str) or not parent.strip():
        return None
    return validate_session_id(parent)


def session_meta_id(payload: dict[str, Any]) -> str | None:
    session_id = payload.get("id")
    if not isinstance(session_id, str) or payload.get("session_id") != session_id:
        return None
    return validate_session_id(session_id)


def discover_descendant_session_ids(codex_home: Path, root_ids: list[str]) -> list[str]:
    known = {validate_session_id(item) for item in root_ids}
    if not known:
        return []
    candidates = sorted(glob.glob(str(codex_home / "sessions" / "*" / "*" / "*" / "*.jsonl")))
    discovered: set[str] = set()
    changed = True
    while changed:
        changed = False
        for item in candidates:
            payload = read_session_meta_payload(Path(item))
            if payload is None:
                continue
            session_id = session_meta_id(payload)
            if session_id is None or session_id in known:
                continue
            parent_id = session_parent_id(payload)
            if parent_id not in known:
                continue
            known.add(session_id)
            discovered.add(session_id)
            changed = True
    return sorted(discovered)


def resolve_session_ids(run: dict[str, Any], codex_home: Path) -> int:
    session_ids = run.get("session_ids", [])
    if not isinstance(session_ids, list):
        raise BudgetError("budget run entry has malformed session_ids")
    descendant_ids = discover_descendant_session_ids(codex_home, session_ids)
    add_session_ids(run, descendant_ids)
    resolved = []
    for session_id in run.get("session_ids", []):
        resolved.append(relative_session_path(codex_home, discover_session_file(codex_home, validate_session_id(session_id))))
    return add_session_files(run, resolved)


def parse_db_ts(value: Any) -> int | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1) + "+00:00"
    try:
        return int(datetime.datetime.fromisoformat(text).timestamp())
    except ValueError:
        return None


def run_stop_epoch(db: Path, run_id: str, start_epoch: int) -> int | None:
    terminal = {"workflow_paused", "approval_requested", "workflow_failed", "workflow_completed", "workflow_cancelled"}
    try:
        con = sqlite3.connect(db)
        try:
            if not table_exists(con, "remote_agent_workflow_events"):
                return None
            columns = {row[1] for row in con.execute("PRAGMA table_info(remote_agent_workflow_events)").fetchall()}
            event_col = "event_type" if "event_type" in columns else None
            created_col = "created_at" if "created_at" in columns else None
            if event_col is None or created_col is None:
                return None
            rows = con.execute(
                f"SELECT {event_col}, {created_col} FROM remote_agent_workflow_events "
                "WHERE workflow_run_id = ? ORDER BY created_at",
                (run_id,),
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise BudgetError(f"workflow stop lookup failed: {exc}") from exc
    for event_type, created_at in rows:
        if str(event_type) not in terminal:
            continue
        epoch = parse_db_ts(created_at)
        if epoch is not None and epoch >= start_epoch:
            return epoch
    return None


def recover_open_intervals(state: dict[str, Any], db: Path | None) -> None:
    if db is None:
        return
    for interval in state.get("active_intervals", []):
        if interval.get("end_epoch") is not None:
            continue
        run_id = interval.get("run_id")
        start = int(interval.get("start_epoch", 0) or 0)
        if not isinstance(run_id, str) or start <= 0:
            continue
        stop = run_stop_epoch(db, run_id, start)
        if stop is None:
            continue
        interval["end_epoch"] = stop
        interval["ended_at"] = utc_now()
        interval["recovered_from_event"] = True


def run_has_codex_activity(db: Path, run_id: str) -> bool:
    try:
        con = sqlite3.connect(db)
        try:
            if not table_exists(con, "remote_agent_workflow_events"):
                raise BudgetError("workflow events table is unavailable")
            columns = {row[1] for row in con.execute("PRAGMA table_info(remote_agent_workflow_events)").fetchall()}
            data_col = "data" if "data" in columns else "payload" if "payload" in columns else None
            event_col = "event_type" if "event_type" in columns else None
            step_col = "step_name" if "step_name" in columns else "node_name" if "node_name" in columns else None
            if data_col is None:
                raise BudgetError("workflow events table has no JSON payload column")
            select_cols = [data_col]
            if event_col:
                select_cols.append(event_col)
            if step_col:
                select_cols.append(step_col)
            rows = con.execute(
                f"SELECT {', '.join(select_cols)} FROM remote_agent_workflow_events WHERE workflow_run_id = ?",
                (run_id,),
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise BudgetError(f"workflow activity lookup failed: {exc}") from exc
    for row in rows:
        raw = row[0]
        try:
            data = json.loads(raw) if isinstance(raw, str) and raw else {}
        except ValueError:
            data = {}
        event_type = str(row[1] if len(row) > 1 else "").lower()
        # Stock emits node_started before it even invokes the Codex wrapper.
        # Exact sessions are registered by the wrapper at thread.started;
        # pre-provider failures must not become unaccountable model usage.
        if event_type == "tool_called" or (event_type == "node_completed" and isinstance(data, dict) and "tokens" in data):
            return True
    return False


def refresh_recorded_sessions(state: dict[str, Any], db: Path | None, codex_home: Path, require_sessions: bool) -> None:
    for run in state.get("runs", []):
        run_id = run.get("run_id")
        if not isinstance(run_id, str):
            raise BudgetError("budget run entry is missing run_id")
        db_ids = db_session_ids(db, run_id) if db is not None else []
        id_count = add_session_ids(run, db_ids)
        file_count = resolve_session_ids(run, codex_home)
        if not run.get("session_files") and db is not None and require_sessions and run_has_codex_activity(db, run_id):
            raise BudgetError(f"run {run_id[:8]} has Codex activity but no recorded Codex session")
        if id_count or file_count:
            run["sessions_refreshed_at"] = utc_now()


def command_active_start(args: argparse.Namespace) -> None:
    run_id = validate_run_id(args.run_id)
    now = int(args.now if args.now is not None else time.time())
    with locked_budget(args.control_dir, args.chain_id) as path:
        state = read_budget(path)
        require_no_pending_amendment(state)
        recover_open_intervals(state, args.db)
        refresh_recorded_sessions(state, args.db, args.codex_home, True)
        summary = usage_summary(state, args.codex_home, now, True)
        if summary["wall_exhausted"]:
            raise BudgetError("shared wall budget is exhausted before dispatch")
        if summary["tokens_exhausted"]:
            raise BudgetError("shared token budget is exhausted before dispatch")
        for interval in state.get("active_intervals", []):
            if interval.get("end_epoch") is None:
                if interval.get("run_id") == run_id:
                    print(f"FEATURE_BUDGET=ACTIVE chain={args.chain_id} run={run_id[:8]} already=true")
                    return
                raise BudgetError("another active interval is already open")
        state.setdefault("active_intervals", []).append({
            "run_id": run_id,
            "start_epoch": now,
            "end_epoch": None,
            "started_at": utc_now(),
        })
        state["updated_at"] = utc_now()
        write_budget(path, state)
    print(f"FEATURE_BUDGET=ACTIVE chain={args.chain_id} run={run_id[:8]} already=false")


def require_no_pending_amendment(state: dict[str, Any]) -> None:
    pending = state.get("pending_amendment")
    if isinstance(pending, dict):
        raise BudgetError("budget amendment is incomplete; retry feature-budget-update before dispatch")


def require_no_live_execution(state: dict[str, Any]) -> None:
    for interval in state.get("active_intervals", []):
        if interval.get("end_epoch") is None:
            raise BudgetError("cannot amend budget while an active interval is open")
    for group in state.get("process_groups", []):
        if group.get("unregistered_at") is None and group.get("terminated_at") is None:
            raise BudgetError("cannot amend budget while a controlled process group is registered")


def command_amend_limit(args: argparse.Namespace) -> None:
    total_tokens = require_positive_int(args.total_tokens, "total tokens")
    total_active_minutes = getattr(args, "total_active_minutes", None)
    wall_seconds_target = None
    if total_active_minutes is not None:
        wall_seconds_target = require_positive_int(total_active_minutes, "total active minutes") * 60
    amendment_id = args.amendment_id.strip()
    if not re.fullmatch(r"[0-9a-f]{64}", amendment_id):
        raise BudgetError("amendment id must be a sha256 hex digest")
    with locked_budget(args.control_dir, args.chain_id) as path:
        state = read_budget(path)
        applied = state.setdefault("amendments", [])
        for item in applied:
            if item.get("amendment_id") != amendment_id:
                continue
            if item.get("total_tokens") != total_tokens or item.get("wall_seconds") != wall_seconds_target:
                raise BudgetError("amendment id was already used for a different allowance")
            print(f"FEATURE_BUDGET=AMENDED chain={args.chain_id} total_tokens={total_tokens} existing=true")
            return
        pending = state.get("pending_amendment")
        if isinstance(pending, dict):
            if (pending.get("amendment_id") != amendment_id
                    or pending.get("total_tokens") != total_tokens
                    or pending.get("wall_seconds") != wall_seconds_target):
                raise BudgetError("a different budget amendment is incomplete")
        else:
            require_no_live_execution(state)
            current = state.get("max_total_tokens")
            current_wall = state.get("wall_seconds")
            if type(current) is not int or current <= 0:
                raise BudgetError("current token limit is malformed")
            if type(current_wall) is not int or current_wall <= 0:
                raise BudgetError("current active time limit is malformed")
            if total_tokens < current:
                raise BudgetError("budget amendment may only increase the token ceiling")
            if wall_seconds_target is not None and wall_seconds_target < current_wall:
                raise BudgetError("budget amendment may only increase the active time ceiling")
            tokens_increase = total_tokens > current
            wall_increase = wall_seconds_target is not None and wall_seconds_target > current_wall
            if not tokens_increase and not wall_increase:
                raise BudgetError("budget amendment must increase at least one ceiling")
            state["pending_amendment"] = {
                "amendment_id": amendment_id,
                "from_total_tokens": current,
                "from_wall_seconds": current_wall,
                "total_tokens": total_tokens,
                "wall_seconds": wall_seconds_target,
                "reason": args.reason,
                "started_at": utc_now(),
            }
            state["updated_at"] = utc_now()
            write_budget(path, state)
        pending = state["pending_amendment"]
        state["max_total_tokens"] = total_tokens
        if pending.get("wall_seconds") is not None:
            state["wall_seconds"] = pending["wall_seconds"]
        last_usage = state.get("last_usage")
        if isinstance(last_usage, dict):
            last_usage["max_total_tokens"] = total_tokens
            total = int(last_usage.get("total_tokens", 0) or 0)
            last_usage["tokens_exhausted"] = total >= total_tokens
            if pending.get("wall_seconds") is not None:
                last_usage["wall_seconds"] = pending["wall_seconds"]
                active = int(last_usage.get("active_seconds", 0) or 0)
                last_usage["wall_exhausted"] = active >= pending["wall_seconds"]
        applied.append({
            **pending,
            "applied_at": utc_now(),
        })
        state.pop("pending_amendment", None)
        state["updated_at"] = utc_now()
        write_budget(path, state)
    print(f"FEATURE_BUDGET=AMENDED chain={args.chain_id} total_tokens={total_tokens} existing=false")


def command_active_stop(args: argparse.Namespace) -> None:
    run_id = validate_run_id(args.run_id)
    now = int(args.now if args.now is not None else time.time())
    stopped = 0
    with locked_budget(args.control_dir, args.chain_id) as path:
        state = read_budget(path)
        for interval in state.get("active_intervals", []):
            if interval.get("run_id") == run_id and interval.get("end_epoch") is None:
                if now < int(interval.get("start_epoch", now)):
                    raise BudgetError("active interval cannot end before it starts")
                interval["end_epoch"] = now
                interval["ended_at"] = utc_now()
                stopped += 1
        state["updated_at"] = utc_now()
        write_budget(path, state)
    print(f"FEATURE_BUDGET=STOPPED chain={args.chain_id} run={run_id[:8]} intervals={stopped}")


def command_register_group(args: argparse.Namespace) -> None:
    run_id = validate_run_id(args.run_id)
    pgid = require_positive_int(args.pgid, "pgid")
    fingerprint = args.fingerprint
    if fingerprint == "@current":
        fingerprint = process_fingerprint(pgid)
        if fingerprint is None:
            raise BudgetError(f"process group {pgid} is not alive")
    validate_group_identity(pgid, fingerprint)
    with locked_budget(args.control_dir, args.chain_id) as path:
        state = read_budget(path)
        groups = state.setdefault("process_groups", [])
        for group in groups:
            if group.get("pgid") == pgid and group.get("unregistered_at") is None and group.get("terminated_at") is None:
                raise BudgetError(f"process group {pgid} is already registered")
        groups.append({
            "run_id": run_id,
            "pgid": pgid,
            "fingerprint": fingerprint,
            "label": args.label,
            "registered_at": utc_now(),
        })
        state["updated_at"] = utc_now()
        write_budget(path, state)
    print(f"FEATURE_BUDGET=GROUP_REGISTERED chain={args.chain_id} run={run_id[:8]} pgid={pgid}")


def command_unregister_group(args: argparse.Namespace) -> None:
    pgid = require_positive_int(args.pgid, "pgid")
    count = 0
    with locked_budget(args.control_dir, args.chain_id) as path:
        state = read_budget(path)
        for group in state.get("process_groups", []):
            if group.get("pgid") == pgid and group.get("unregistered_at") is None and group.get("terminated_at") is None:
                group["unregistered_at"] = utc_now()
                count += 1
        state["updated_at"] = utc_now()
        write_budget(path, state)
    print(f"FEATURE_BUDGET=GROUP_UNREGISTERED chain={args.chain_id} pgid={pgid} count={count}")


def command_terminate_groups(args: argparse.Namespace) -> None:
    run_id = validate_run_id(args.run_id) if args.run_id else None
    rows: list[dict[str, Any]] = []
    with locked_budget(args.control_dir, args.chain_id) as path:
        state = read_budget(path)
        for group in state.get("process_groups", []):
            if group.get("unregistered_at") is not None or group.get("terminated_at") is not None:
                continue
            if run_id is not None and group.get("run_id") != run_id:
                continue
            pgid = group.get("pgid")
            fingerprint = group.get("fingerprint")
            if not isinstance(pgid, int) or not isinstance(fingerprint, str):
                result = "malformed-registration"
            else:
                result = terminate_group(pgid, fingerprint, args.wait_seconds)
            group["terminated_at"] = utc_now()
            group["terminate_result"] = result
            rows.append({"pgid": pgid, "label": group.get("label"), "result": result})
        state["updated_at"] = utc_now()
        write_budget(path, state)
    if args.json:
        print(json.dumps({"terminated": rows}, sort_keys=True))
        return
    print(f"FEATURE_BUDGET=GROUPS_TERMINATED chain={args.chain_id} count={len(rows)}")


def session_usage(path: Path) -> dict[str, int]:
    usage = None
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if '"token_count"' not in line:
                    continue
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                info = (data.get("payload") or {}).get("info") or {}
                current = info.get("total_token_usage")
                if isinstance(current, dict):
                    usage = current
    except OSError as exc:
        raise BudgetError(f"session accounting unavailable for {path}: {exc}") from exc
    if usage is None:
        return {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    return {
        "input_tokens": int(usage.get("input_tokens", 0) or 0),
        "cached_input_tokens": int(usage.get("cached_input_tokens", 0) or 0),
        "output_tokens": int(usage.get("output_tokens", 0) or 0),
        "total_tokens": int(usage.get("total_tokens", 0) or 0),
    }


def active_seconds(state: dict[str, Any], now: int) -> int:
    total = 0
    for interval in state.get("active_intervals", []):
        start = int(interval.get("start_epoch", 0) or 0)
        end_value = interval.get("end_epoch")
        end = now if end_value is None else int(end_value)
        if end < start:
            raise BudgetError("active interval has negative duration")
        total += end - start
    return total


def usage_summary(state: dict[str, Any], codex_home: Path, now: int, require_sessions: bool) -> dict[str, Any]:
    seen: set[str] = set()
    totals = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    prior_high_water = state.get("token_high_water")
    if not isinstance(prior_high_water, dict):
        prior_high_water = {}
    session_high_water = state.setdefault("session_token_high_water", {})
    runs = state.get("runs", [])
    for run in runs:
        session_files = run.get("session_files", [])
        for rel in session_files:
            if not isinstance(rel, str) or rel.startswith("/") or ".." in rel.split("/"):
                raise BudgetError(f"invalid recorded session path: {rel}")
            if rel in seen:
                continue
            seen.add(rel)
            item = session_usage(codex_home / rel)
            prior = session_high_water.get(rel, {}) if isinstance(session_high_water.get(rel), dict) else {}
            effective = {}
            for key in totals:
                value = max(int(prior.get(key, 0) or 0), int(item[key]))
                effective[key] = value
                totals[key] += value
            session_high_water[rel] = effective
    for key in totals:
        totals[key] = max(totals[key], int(prior_high_water.get(key, 0) or 0))
    state["token_high_water"] = dict(totals)
    receipt_totals = provider_receipt_totals(state)
    reported = {key: totals[key] + receipt_totals[key] for key in totals}
    used_seconds = active_seconds(state, now)
    wall_seconds = state.get("wall_seconds")
    token_cap = state.get("max_total_tokens")
    if type(wall_seconds) is not int or wall_seconds <= 0 or type(token_cap) is not int or token_cap <= 0:
        raise BudgetError("shared wall/token limits must be positive integers")
    return {
        "chain": state.get("logical_chain_id"),
        "runs": len(runs),
        "sessions": len(seen),
        "provider_receipts": len(state.get("provider_usage_receipts", [])),
        **reported,
        "active_seconds": used_seconds,
        "wall_seconds": wall_seconds,
        "max_total_tokens": token_cap,
        "wall_exhausted": used_seconds >= wall_seconds,
        "tokens_exhausted": reported["total_tokens"] >= token_cap,
    }


def command_usage(args: argparse.Namespace) -> None:
    now = int(args.now if args.now is not None else time.time())
    with locked_budget(args.control_dir, args.chain_id) as path:
        state = read_budget(path)
        recover_open_intervals(state, args.db)
        refresh_recorded_sessions(state, args.db, args.codex_home, args.require_sessions)
        summary = usage_summary(state, args.codex_home, now, args.require_sessions)
        state["updated_at"] = utc_now()
        state["last_usage"] = summary
        write_budget(path, state)
    if args.json:
        print(json.dumps(summary, sort_keys=True))
        return
    print(
        f"FEATURE_BUDGET chain={str(summary['chain'])[:8]} runs={summary['runs']} "
        f"sessions={summary['sessions']} active_s={summary['active_seconds']} "
        f"wall_s={summary['wall_seconds']} total={summary['total_tokens']} "
        f"cap={summary['max_total_tokens']}"
    )


def discover_session_file(codex_home: Path, session_id: str) -> Path:
    validate_session_id(session_id)
    candidates = sorted(glob.glob(str(codex_home / "sessions" / "*" / "*" / "*" / f"*{session_id}.jsonl")))
    matches = [Path(item) for item in candidates if session_file_has_id(Path(item), session_id)]
    if len(matches) != 1:
        raise BudgetError(f"expected one Codex session file for {session_id}, found {len(matches)}")
    return matches[0]


def session_file_has_id(path: Path, session_id: str) -> bool:
    payload = read_session_meta_payload(path)
    return payload is not None and payload.get("id") == session_id and payload.get("session_id") == session_id


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--control-dir", type=Path, required=True)
    ap.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME", Path.home() / ".archon/codex-home")))
    sub = ap.add_subparsers(dest="action", required=True)

    init = sub.add_parser("init")
    init.add_argument("--chain-id", required=True)
    init.add_argument("--wall-minutes", type=int, default=DEFAULT_WALL_MINUTES)
    init.add_argument("--max-total-tokens", type=int, default=DEFAULT_MAX_TOTAL_TOKENS)
    init.add_argument("--replace", action="store_true")

    bind = sub.add_parser("bind-run")
    bind.add_argument("--chain-id", required=True)
    bind.add_argument("--run-id", required=True)
    bind.add_argument("--stage")
    bind.add_argument("--session-file", type=Path, action="append", default=[])
    bind.add_argument("--session-id", action="append", default=[])

    bind_session = sub.add_parser("bind-session")
    bind_session.add_argument("--chain-id", required=True)
    bind_session.add_argument("--run-id", required=True)
    bind_session.add_argument("--session-id", required=True)
    bind_session.add_argument("--stage")

    start = sub.add_parser("active-start")
    start.add_argument("--chain-id", required=True)
    start.add_argument("--run-id", required=True)
    start.add_argument("--db", type=Path)
    start.add_argument("--now", type=int)

    stop = sub.add_parser("active-stop")
    stop.add_argument("--chain-id", required=True)
    stop.add_argument("--run-id", required=True)
    stop.add_argument("--now", type=int)

    register_group = sub.add_parser("register-group")
    register_group.add_argument("--chain-id", required=True)
    register_group.add_argument("--run-id", required=True)
    register_group.add_argument("--pgid", type=int, required=True)
    register_group.add_argument("--fingerprint", required=True)
    register_group.add_argument("--label", default="integration")

    unregister_group = sub.add_parser("unregister-group")
    unregister_group.add_argument("--chain-id", required=True)
    unregister_group.add_argument("--pgid", type=int, required=True)

    terminate_groups = sub.add_parser("terminate-groups")
    terminate_groups.add_argument("--chain-id", required=True)
    terminate_groups.add_argument("--run-id")
    terminate_groups.add_argument("--wait-seconds", type=float, default=5.0)
    terminate_groups.add_argument("--json", action="store_true")

    usage = sub.add_parser("usage")
    usage.add_argument("--chain-id", required=True)
    usage.add_argument("--db", type=Path)
    usage.add_argument("--now", type=int)
    usage.add_argument("--require-sessions", action="store_true")
    usage.add_argument("--json", action="store_true")
    account = sub.add_parser("account-provider-usage")
    account.add_argument("--chain-id", required=True)
    account.add_argument("--run-id", required=True)
    account.add_argument("--db", type=Path, required=True)
    account.add_argument("--event-id", required=True)
    account.add_argument("--transcript", type=Path, required=True)
    amend = sub.add_parser("amend-limit")
    amend.add_argument("--chain-id", required=True)
    amend.add_argument("--total-tokens", type=int, required=True)
    amend.add_argument("--total-active-minutes", type=int)
    amend.add_argument("--amendment-id", required=True)
    amend.add_argument("--reason", required=True)
    return ap


def main() -> None:
    args = parser().parse_args()
    try:
        {
            "init": command_init,
            "bind-run": command_bind_run,
            "bind-session": command_bind_session,
            "active-start": command_active_start,
            "active-stop": command_active_stop,
            "register-group": command_register_group,
            "unregister-group": command_unregister_group,
            "terminate-groups": command_terminate_groups,
            "usage": command_usage,
            "account-provider-usage": command_account_provider_usage,
            "amend-limit": command_amend_limit,
        }[args.action](args)
    except BudgetError as exc:
        fail(str(exc))


if __name__ == "__main__":
    main()
