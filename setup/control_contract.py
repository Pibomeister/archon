#!/usr/bin/env python3
"""Shared private-control serialization and HMAC verification primitives."""
from __future__ import annotations

import base64
import binascii
import errno
import hashlib
import hmac
import json
import os
import secrets
import stat
from pathlib import Path
from typing import Any


class ControlContractError(ValueError):
    pass


MAX_APPROVAL_SNAPSHOT_FILES = 32
MAX_APPROVAL_SNAPSHOT_FILE_BYTES = 1024 * 1024
MAX_APPROVAL_SNAPSHOT_TOTAL_BYTES = 4 * 1024 * 1024
HEX64 = set("0123456789abcdef")


def canonical_bytes(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def hmac_sha256(secret: object, data: Any) -> str:
    if not isinstance(secret, str) or len(secret) < 32:
        raise ControlContractError("chain state is missing a valid private secret")
    return hmac.new(secret.encode("utf-8"), canonical_bytes(data), hashlib.sha256).hexdigest()


def secure_read_json(path: Path, *, require_owner: bool = True) -> dict:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
        info = os.fstat(fd)
        bad_owner = require_owner and info.st_uid != os.getuid()
        if not stat.S_ISREG(info.st_mode) or bad_owner or info.st_mode & 0o077:
            os.close(fd)
            raise ControlContractError(f"private state must be an owned mode-0600 regular file: {path}")
        with os.fdopen(fd, encoding="utf-8") as fh:
            value = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise ControlContractError(f"private state unavailable or malformed at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ControlContractError(f"private state must be a JSON object: {path}")
    return value


def secure_write_json(path: Path, data: dict, *, replace: bool = True) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        with os.fdopen(os.open(temporary, flags, 0o600), "wb") as stream:
            stream.write((json.dumps(data, indent=2) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o600)
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except FileExistsError as exc:
        raise ControlContractError(f"private state already exists: {path}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def secure_create_json(path: Path, data: dict) -> None:
    secure_write_json(path, data, replace=False)


def seal_chain_state(state: dict) -> dict:
    payload = {k: v for k, v in state.items() if k != "state_mac"}
    payload["state_mac"] = hmac_sha256(payload.get("chain_secret"), payload)
    return payload


def verify_chain_state(state: dict, *, chain_id: str | None = None) -> dict:
    if chain_id is not None and state.get("logical_chain_id") != chain_id:
        raise ControlContractError("chain id mismatch")
    secret = state.get("chain_secret")
    mac = state.get("state_mac")
    if not isinstance(mac, str):
        raise ControlContractError("chain state is not HMAC sealed")
    payload = {k: v for k, v in state.items() if k != "state_mac"}
    if not hmac.compare_digest(mac, hmac_sha256(secret, payload)):
        raise ControlContractError("chain state MAC mismatch")
    return state


def _str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ControlContractError(f"{name} must be a non-empty string")
    return value


def _obj(value: Any, name: str) -> dict:
    if not isinstance(value, dict) or not value:
        raise ControlContractError(f"{name} must be a non-empty object")
    return value


def _hex64(value: Any, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(ch not in HEX64 for ch in value):
        raise ControlContractError(f"{name} must be a sha256 hex digest")
    return value


def _provider(value: Any) -> str:
    provider = _str(value, "provider")
    if provider not in {"claude", "codex"}:
        raise ControlContractError("provider must be claude or codex")
    return provider


def _context(value: Any) -> dict:
    return {
        _str(key, "context digest key"): _hex64(digest, f"context digest {key}")
        for key, digest in _obj(value, "context digests").items()
    }


def _allowlist(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ControlContractError("artifact allowlist must be a non-empty list")
    if len(value) > MAX_APPROVAL_SNAPSHOT_FILES:
        raise ControlContractError("artifact allowlist has too many files")
    seen = set()
    for rel in value:
        rel = _str(rel, "artifact allowlist entry")
        parts = rel.split("/")
        if rel.startswith("/") or "\\" in rel or "" in parts or "." in parts or ".." in parts:
            raise ControlContractError(f"artifact path must be relative without traversal: {rel}")
        if rel in seen:
            raise ControlContractError(f"duplicate artifact allowlist entry: {rel}")
        seen.add(rel)
    return value


def _dir_open(path_or_name, *, dir_fd=None, display="artifact root") -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path_or_name, flags, dir_fd=dir_fd)
    except OSError as exc:
        raise ControlContractError(f"{display} must be a non-symlink directory") from exc


def _read_artifact(root: Path, relative: str, max_file_bytes: int) -> dict:
    parts, dir_fd = relative.split("/"), _dir_open(root)
    try:
        for depth, part in enumerate(parts[:-1], start=1):
            next_fd = _dir_open(part, dir_fd=dir_fd, display="/".join(parts[:depth]))
            os.close(dir_fd)
            dir_fd = next_fd
        fd = -1
        try:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise ControlContractError(f"artifact path must not contain a symlink: {relative}") from exc
            raise ControlContractError(f"artifact unavailable: {relative}") from exc
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise ControlContractError(f"artifact path must be a regular file: {relative}")
            if before.st_nlink != 1:
                raise ControlContractError(f"artifact path must not be a hardlink: {relative}")
            if before.st_size <= 0:
                raise ControlContractError(f"artifact path must not be empty: {relative}")
            if before.st_size > max_file_bytes:
                raise ControlContractError(f"artifact is too large: {relative}")
            with os.fdopen(fd, "rb", closefd=True) as fh:
                fd = -1
                data = fh.read(max_file_bytes + 1)
            after = os.stat(parts[-1], dir_fd=dir_fd, follow_symlinks=False)
        finally:
            if fd >= 0:
                os.close(fd)
    finally:
        os.close(dir_fd)
    if (before.st_dev, before.st_ino, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_mtime_ns):
        raise ControlContractError(f"artifact changed while reading: {relative}")
    if len(data) != before.st_size or len(data) > max_file_bytes:
        raise ControlContractError(f"artifact changed size or is too large: {relative}")
    return {
        "path": relative,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "content_b64": base64.b64encode(data).decode("ascii"),
    }


def _plain(value: dict, *drop: str) -> dict:
    return {k: v for k, v in value.items() if k not in drop}


def _digest(value: dict) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _snapshot_files(snapshot: dict) -> list[dict]:
    files = snapshot.get("files")
    if not isinstance(files, list) or not files or len(files) > MAX_APPROVAL_SNAPSHOT_FILES:
        raise ControlContractError("approval snapshot files are malformed")
    seen, total = set(), 0
    for item in files:
        if not isinstance(item, dict):
            raise ControlContractError("approval snapshot file entry is malformed")
        path = _allowlist([item.get("path")])[0]
        if path in seen:
            raise ControlContractError(f"duplicate approval snapshot file: {path}")
        seen.add(path)
        _hex64(item.get("sha256"), "artifact digest")
        if type(item.get("size")) is not int or not 0 < item["size"] <= MAX_APPROVAL_SNAPSHOT_FILE_BYTES:
            raise ControlContractError("approval snapshot file size is malformed")
        try:
            data = base64.b64decode(item.get("content_b64", ""), validate=True)
        except (binascii.Error, TypeError) as exc:
            raise ControlContractError(f"approval snapshot content is malformed: {path}") from exc
        if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ControlContractError(f"approval snapshot content mismatch: {path}")
        total += len(data)
    if type(snapshot.get("total_bytes")) is not int or total > MAX_APPROVAL_SNAPSHOT_TOTAL_BYTES or total != snapshot.get("total_bytes"):
        raise ControlContractError("approval snapshot byte accounting mismatch")
    return files


def _seal(body: dict, digest_key: str, secret: str) -> dict:
    sealed = dict(body)
    sealed[digest_key] = _digest(body)
    sealed["authority_mac"] = hmac_sha256(secret, sealed)
    return sealed


def capture_approval_snapshot(
    out_path: Path,
    *,
    artifacts_dir: Path,
    allowlist: list[str],
    run_id: str,
    gate_id: str,
    native_pause_identity: dict,
    provider: str,
    context_digests: dict,
    controller_secret: str,
    max_file_bytes: int = MAX_APPROVAL_SNAPSHOT_FILE_BYTES,
    max_total_bytes: int = MAX_APPROVAL_SNAPSHOT_TOTAL_BYTES,
) -> dict:
    """Caller must pause workers first; this records private bytes, not a lock."""
    if type(max_file_bytes) is not int or not 0 < max_file_bytes <= MAX_APPROVAL_SNAPSHOT_FILE_BYTES:
        raise ControlContractError("invalid approval snapshot file limit")
    if type(max_total_bytes) is not int or not 0 < max_total_bytes <= MAX_APPROVAL_SNAPSHOT_TOTAL_BYTES:
        raise ControlContractError("invalid approval snapshot total limit")
    files = []
    total = 0
    for relative in _allowlist(allowlist):
        item = _read_artifact(artifacts_dir, relative, max_file_bytes)
        total += item["size"]
        if total > max_total_bytes:
            raise ControlContractError("approval snapshot is too large")
        files.append(item)
    body = {
        "schema_version": 1,
        "kind": "approval-snapshot",
        "run_id": _str(run_id, "run id"),
        "gate_id": _str(gate_id, "gate id"),
        "provider": _provider(provider),
        "native_pause_identity": _obj(native_pause_identity, "native pause identity"),
        "context_digests": _context(context_digests),
        "files": files,
        "total_bytes": total,
    }
    snapshot = _seal(body, "snapshot_digest", controller_secret)
    secure_create_json(out_path, snapshot)
    return snapshot


def verify_approval_snapshot(
    snapshot: Path | dict,
    *,
    controller_secret: str,
    expected_snapshot_digest: str | None = None,
) -> dict:
    value = secure_read_json(snapshot) if isinstance(snapshot, Path) else snapshot
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value.get("schema_version") != 1 or value.get("kind") != "approval-snapshot":
        raise ControlContractError("approval snapshot has an unsupported schema")
    digest = _hex64(value.get("snapshot_digest"), "snapshot digest")
    if expected_snapshot_digest is not None and digest != expected_snapshot_digest:
        raise ControlContractError("snapshot digest mismatch")
    if _digest(_plain(value, "snapshot_digest", "authority_mac")) != digest:
        raise ControlContractError("approval snapshot digest mismatch")
    if not hmac.compare_digest(_hex64(value.get("authority_mac"), "authority MAC"), hmac_sha256(controller_secret, _plain(value, "authority_mac"))):
        raise ControlContractError("approval snapshot MAC mismatch")
    _str(value.get("run_id"), "run id")
    _str(value.get("gate_id"), "gate id")
    _provider(value.get("provider"))
    _obj(value.get("native_pause_identity"), "native pause identity")
    _context(value.get("context_digests"))
    _snapshot_files(value)
    return value


def _check_drift(snapshot: dict, artifacts_dir: Path) -> None:
    for item in _snapshot_files(snapshot):
        current = _read_artifact(artifacts_dir, item["path"], max(item["size"], MAX_APPROVAL_SNAPSHOT_FILE_BYTES))
        if current["sha256"] != item["sha256"] or current["size"] != item["size"]:
            raise ControlContractError(f"state drift for approved artifact: {item['path']}")


def _decision(snapshot: dict, decision: Any, operator_id: Any) -> dict:
    decision = _str(decision, "operator decision")
    if decision not in {"approved", "rejected"}:
        raise ControlContractError("operator decision must be approved or rejected")
    return {
        "schema_version": 1,
        "kind": "operator-decision",
        "snapshot_digest": snapshot["snapshot_digest"],
        "run_id": snapshot["run_id"],
        "gate_id": snapshot["gate_id"],
        "provider": snapshot["provider"],
        "native_pause_identity": snapshot["native_pause_identity"],
        "context_digests": snapshot["context_digests"],
        "artifact_hashes": [
            {"path": item["path"], "sha256": item["sha256"], "size": item["size"]}
            for item in snapshot["files"]
        ],
        "decision": decision,
        "operator_id": _str(operator_id, "operator id"),
    }


def _verify_decision(value: dict, snapshot: dict, controller_secret: str) -> dict:
    body = _plain(value, "decision_digest", "authority_mac") if isinstance(value, dict) else {}
    if type(body.get("schema_version")) is not int or body.get("schema_version") != 1 or body.get("kind") != "operator-decision":
        raise ControlContractError("operator decision has an unsupported schema")
    if _digest(body) != _hex64(value.get("decision_digest"), "decision digest"):
        raise ControlContractError("operator decision digest mismatch")
    if not hmac.compare_digest(_hex64(value.get("authority_mac"), "authority MAC"), hmac_sha256(controller_secret, _plain(value, "authority_mac"))):
        raise ControlContractError("operator decision MAC mismatch")
    if body != _decision(snapshot, value.get("decision"), value.get("operator_id")):
        raise ControlContractError("operator decision is not bound to the approval snapshot")
    return value


def record_operator_decision(
    out_path: Path,
    *,
    snapshot_path: Path,
    expected_snapshot_digest: str,
    decision: str,
    operator_id: str,
    controller_secret: str,
    artifacts_dir: Path | None = None,
) -> dict:
    snapshot = verify_approval_snapshot(snapshot_path, controller_secret=controller_secret, expected_snapshot_digest=expected_snapshot_digest)
    if artifacts_dir is not None:
        _check_drift(snapshot, artifacts_dir)
    sealed = _seal(_decision(snapshot, decision, operator_id), "decision_digest", controller_secret)
    try:
        secure_create_json(out_path, sealed)
    except ControlContractError as exc:
        if not isinstance(exc.__cause__, FileExistsError):
            raise
        existing = validate_operator_decision(
            out_path, snapshot_path=snapshot_path,
            expected_snapshot_digest=expected_snapshot_digest,
            controller_secret=controller_secret, artifacts_dir=artifacts_dir,
        )
        if existing != sealed:
            raise ControlContractError("conflicting operator decision already exists") from exc
        return existing
    return sealed


def validate_operator_decision(
    decision_path: Path,
    *,
    snapshot_path: Path,
    expected_snapshot_digest: str,
    controller_secret: str,
    artifacts_dir: Path | None = None,
) -> dict:
    snapshot = verify_approval_snapshot(snapshot_path, controller_secret=controller_secret, expected_snapshot_digest=expected_snapshot_digest)
    if artifacts_dir is not None:
        _check_drift(snapshot, artifacts_dir)
    return _verify_decision(secure_read_json(decision_path), snapshot, controller_secret)
