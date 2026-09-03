#!/usr/bin/env python3
"""Shared private-control serialization and HMAC verification primitives."""
from __future__ import annotations

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


def canonical_bytes(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def hmac_sha256(secret: str, data: Any) -> str:
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


def secure_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(temporary, flags, 0o600)
    try:
        os.write(fd, (json.dumps(data, indent=2) + "\n").encode())
        os.fsync(fd)
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    os.replace(temporary, path)


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
