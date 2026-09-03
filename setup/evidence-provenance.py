#!/usr/bin/env python3
"""Bounded, freshness-aware, controller-sealed bugfix evidence provenance."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import stat
from pathlib import Path

import control_contract

STATUSES = {"complete", "degraded", "timed-out", "unavailable"}
MAX = 1024 * 1024
SENSITIVE_PATTERNS = (
    (re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"), "EMAIL"),
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b"), "ID"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "SECRET"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"), "SECRET"),
    (re.compile(r"(?i)\b(password|passwd|token|secret|api[_-]?key)\s*[:=]\s*[^\s,;]+"), "SECRET"),
)


def fail(message):
    print(f"EVIDENCE_PROVENANCE=FAIL {message}")
    raise SystemExit(1)


def safe(root, relative, required=True):
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        fail("unsafe evidence path")
    target = root / path
    if not target.exists() and not required:
        return target
    try:
        info = target.lstat()
    except OSError as exc:
        fail(f"evidence unavailable: {exc}")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        fail("evidence must be regular non-symlink file")
    if info.st_size > MAX:
        fail("evidence exceeds byte limit")
    try:
        target.resolve().relative_to(root.resolve())
    except ValueError:
        fail("evidence escapes artifacts root")
    return target


def iso(value=None):
    now = dt.datetime.fromisoformat(value.replace("Z", "+00:00")) if value else dt.datetime.now(dt.timezone.utc)
    return now.astimezone(dt.timezone.utc)


def load_manifest(path):
    try:
        value = json.loads(path.read_text()) if path.exists() else {"schema_version": 2, "sources": []}
    except Exception as exc:
        fail(f"manifest unreadable: {exc}")
    if value.get("schema_version") != 2 or not isinstance(value.get("sources"), list):
        fail("manifest schema invalid")
    return value


def write_manifest(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def redact(raw):
    text = raw.decode("utf-8", errors="replace")
    count = 0
    for pattern, label in SENSITIVE_PATTERNS:
        def replacement(match):
            nonlocal count
            count += 1
            digest = hashlib.sha256(match.group(0).encode()).hexdigest()[:12]
            return f"[REDACTED_{label}:{digest}]"
        text = pattern.sub(replacement, text)
    return text.encode("utf-8"), count


def validate_manifest(root, document, now=None):
    current_time = iso(now)
    for entry in document["sources"]:
        if entry.get("status") not in STATUSES:
            fail("evidence status out of enum")
        complete = entry.get("status") == "complete"
        if entry.get("completeness") != ("complete" if complete else "incomplete"):
            fail("evidence completeness/status mismatch")
        if entry.get("supports_negative") and not complete:
            fail("tool failure cannot be negative product evidence")
        file_name = entry.get("file")
        if file_name:
            path = safe(root, file_name)
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != entry.get("output_sha256"):
                fail(f"evidence output mutated after collection: {entry.get('source')}")
        if entry.get("expires_at") and iso(entry["expires_at"]) <= current_time and file_name:
            fail(f"evidence expired but raw file remains: {entry.get('source')}")
    return document


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    record = sub.add_parser("record")
    record.add_argument("--artifacts", type=Path, required=True)
    record.add_argument("--source", required=True)
    record.add_argument("--status", choices=sorted(STATUSES), required=True)
    record.add_argument("--file")
    record.add_argument("--query")
    record.add_argument("--baseline")
    record.add_argument("--provider", default="local")
    record.add_argument("--tool", default="unknown")
    record.add_argument("--duration-ms", type=int, default=0)
    record.add_argument("--now")
    record.add_argument("--evidence-kind", choices=("class", "occurrence"), default="class")
    record.add_argument("--entity-watermark")
    record.add_argument("--occurrence-window")
    record.add_argument("--supports-negative", action="store_true")
    cleanup = sub.add_parser("cleanup")
    cleanup.add_argument("--artifacts", type=Path, required=True)
    cleanup.add_argument("--now")
    validate = sub.add_parser("validate")
    validate.add_argument("--artifacts", type=Path, required=True)
    validate.add_argument("--now")
    validate.add_argument("--require-source")
    validate.add_argument("--require-file")
    for action in ("seal", "verify"):
        command = sub.add_parser(action)
        command.add_argument("--artifacts", type=Path, required=True)
        command.add_argument("--chain-state", type=Path, required=True)
        command.add_argument("--out", type=Path, required=True)
        command.add_argument("--now")
    args = parser.parse_args()
    root = args.artifacts.resolve()
    manifest = root / "evidence-provenance.json"
    document = load_manifest(manifest)

    if args.action == "record":
        now = iso(args.now)
        complete = args.status == "complete"
        if complete and not args.file:
            fail("complete evidence requires a bounded output file")
        if args.supports_negative and not complete:
            fail("tool failure cannot be negative product evidence")
        if args.evidence_kind == "occurrence":
            try:
                window = json.loads(args.occurrence_window or "null")
            except json.JSONDecodeError:
                fail("occurrence window must be JSON")
            if (not isinstance(window, dict) or not window.get("start") or not window.get("end")
                    or not args.entity_watermark or not args.query or not args.baseline):
                fail("occurrence evidence requires window, entity/version watermark, query, and baseline")
        entry = {
            "source": args.source,
            "status": args.status,
            "completeness": "complete" if complete else "incomplete",
            "provider": args.provider,
            "tool": args.tool,
            "collected_at": now.isoformat().replace("+00:00", "Z"),
            "expires_at": (now + dt.timedelta(days=30)).isoformat().replace("+00:00", "Z"),
            "duration_ms": args.duration_ms,
            "query_sha256": hashlib.sha256((args.query or "").encode()).hexdigest(),
            "baseline": args.baseline,
            "file": args.file,
            "output_sha256": None,
            "bytes": 0,
            "evidence_kind": args.evidence_kind,
            "entity_watermark": args.entity_watermark,
            "occurrence_window": window if args.evidence_kind == "occurrence" else None,
            "occurrence_attribution_valid": complete,
            "supports_negative": bool(args.supports_negative),
        }
        if args.file:
            path = safe(root, args.file)
            raw = path.read_bytes()
            redacted, redaction_count = redact(raw)
            if redacted != raw:
                temporary = path.with_suffix(path.suffix + ".redacted.tmp")
                temporary.write_bytes(redacted)
                os.replace(temporary, path)
            entry["raw_sha256"] = hashlib.sha256(raw).hexdigest()
            entry["output_sha256"] = hashlib.sha256(redacted).hexdigest()
            entry["bytes"] = len(redacted)
            entry["redaction_count"] = redaction_count
        previous = [item for item in document["sources"] if item.get("source") == args.source]
        if args.evidence_kind == "occurrence" and previous:
            for item in previous:
                if item.get("entity_watermark") != args.entity_watermark:
                    item["occurrence_attribution_valid"] = False
                    item["invalidated_at"] = entry["collected_at"]
            document["sources"].append(entry)
        else:
            document["sources"] = [item for item in document["sources"] if item.get("source") != args.source]
            document["sources"].append(entry)
        write_manifest(manifest, document)
        print(f"EVIDENCE_PROVENANCE=OK source={args.source} status={args.status}")
        return

    if args.action == "cleanup":
        now = iso(args.now)
        removed = []
        for entry in document["sources"]:
            if not entry.get("file") or not entry.get("expires_at"):
                continue
            if iso(entry["expires_at"]) <= now:
                path = safe(root, entry["file"], required=False)
                if path.exists():
                    path.unlink()
                    removed.append(entry["file"])
                entry["file"] = None
                entry["bytes"] = 0
                entry["expired_at"] = now.isoformat().replace("+00:00", "Z")
                entry["occurrence_attribution_valid"] = False
        write_manifest(manifest, document)
        print(f"EVIDENCE_CLEANUP=OK removed={len(removed)}")
        return

    validate_manifest(root, document, args.now)
    if args.action == "validate":
        if args.require_source:
            matches = [entry for entry in document["sources"] if entry.get("source") == args.require_source]
            if not matches:
                fail(f"required evidence source missing: {args.require_source}")
            if args.require_file and not any(entry.get("file") == args.require_file for entry in matches):
                fail(f"required evidence source file mismatch: {args.require_source}")
        print(f"EVIDENCE_PROVENANCE=VALID sources={len(document['sources'])}")
        return

    try:
        state = control_contract.verify_chain_state(control_contract.secure_read_json(args.chain_state))
    except control_contract.ControlContractError as exc:
        fail(str(exc))
    manifest_hash = hashlib.sha256(control_contract.canonical_bytes(document)).hexdigest()
    if args.action == "seal":
        seal = {
            "schema_version": 1,
            "authority": "controller",
            "logical_chain_id": state["logical_chain_id"],
            "run_id": state["current_run_id"],
            "manifest_hash": manifest_hash,
            "source_count": len(document["sources"]),
        }
        seal["authority_mac"] = control_contract.hmac_sha256(state["chain_secret"], seal)
        control_contract.secure_write_json(args.out, seal)
        print(f"EVIDENCE_PROVENANCE=SEALED hash={manifest_hash}")
        return
    try:
        seal = control_contract.secure_read_json(args.out)
        authority_mac = seal.pop("authority_mac")
    except (control_contract.ControlContractError, KeyError) as exc:
        fail(str(exc))
    expected = control_contract.hmac_sha256(state["chain_secret"], seal)
    if (not hmac.compare_digest(authority_mac, expected)
            or seal.get("logical_chain_id") != state.get("logical_chain_id")
            or seal.get("run_id") != state.get("current_run_id")
            or seal.get("manifest_hash") != manifest_hash):
        fail("evidence controller seal is stale or invalid")
    print(f"EVIDENCE_PROVENANCE=VERIFIED hash={manifest_hash}")


if __name__ == "__main__":
    main()
