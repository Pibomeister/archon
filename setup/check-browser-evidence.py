#!/usr/bin/env python3
"""Check unsealed advisory browser observations; this cannot authorize release."""
from __future__ import annotations

import binascii
import hashlib
import struct
import zipfile
import json
import re
import sys
from pathlib import Path
from typing import Any

HEX40 = re.compile(r"^[0-9a-fA-F]{40}$")
VALID_ASSERTIONS = {"text", "selector", "url", "title", "testid", "click", "fill"}


def fail(message: str) -> None:
    raise SystemExit(f"UAT_GATE=FAIL {message}")


def load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"{label} unreadable: {exc}")
    if not isinstance(data, dict):
        fail(f"{label} must be an object")
    return data


def canonical_digest(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_png(raw: bytes, path: Path) -> None:
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        fail(f"screenshot evidence is not a PNG: {path}")
    offset = 8
    saw_ihdr = False
    while offset + 12 <= len(raw):
        length = struct.unpack(">I", raw[offset:offset + 4])[0]
        kind = raw[offset + 4:offset + 8]
        data_start = offset + 8
        data_end = data_start + length
        crc_end = data_end + 4
        if crc_end > len(raw):
            fail(f"screenshot PNG is truncated: {path}")
        crc_expected = struct.unpack(">I", raw[data_end:crc_end])[0]
        crc_actual = binascii.crc32(kind + raw[data_start:data_end]) & 0xFFFFFFFF
        if crc_actual != crc_expected:
            fail(f"screenshot PNG CRC mismatch: {path}")
        if kind == b"IHDR":
            if saw_ihdr or length != 13:
                fail(f"screenshot PNG IHDR malformed: {path}")
            width, height = struct.unpack(">II", raw[data_start:data_start + 8])
            if width <= 0 or height <= 0 or width > 10000 or height > 10000:
                fail(f"screenshot PNG dimensions out of bounds: {path}")
            saw_ihdr = True
        elif not saw_ihdr:
            fail(f"screenshot PNG missing leading IHDR: {path}")
        if kind == b"IEND":
            if crc_end != len(raw):
                fail(f"screenshot PNG has trailing bytes: {path}")
            return
        offset = crc_end
    fail(f"screenshot PNG missing IEND: {path}")


def validate_trace_zip(path: Path) -> None:
    if not zipfile.is_zipfile(path):
        fail(f"trace evidence is not a zip: {path}")
    try:
        with zipfile.ZipFile(path) as trace:
            names = trace.namelist()
    except zipfile.BadZipFile as exc:
        fail(f"trace evidence zip invalid: {path} ({exc})")
    if not names:
        fail(f"trace evidence zip is empty: {path}")


def file_digest(path: Path, evidence_root: Path, label: str) -> str:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(evidence_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        fail(f"evidence path escapes evidence root: {path} ({exc})")
    if path.is_symlink() or not path.is_file():
        fail(f"evidence file missing or unsafe: {path}")
    raw = path.read_bytes()
    if not raw:
        fail(f"evidence file is empty: {path}")
    if label == "screenshot":
        validate_png(raw, path)
    elif label == "trace":
        validate_trace_zip(path)
    return hashlib.sha256(raw).hexdigest()


def smoke_origins(path: Path) -> tuple[str, str]:
    values: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().rstrip("/")
    except OSError as exc:
        fail(f"smoke-urls unreadable: {exc}")
    web = values.get("web", "")
    api = values.get("api", "")
    if not web.startswith("http://localhost:") and not web.startswith("http://127.0.0.1:"):
        fail("smoke-urls web origin is not an isolated local origin")
    if not api.startswith("http://localhost:") and not api.startswith("http://127.0.0.1:"):
        fail("smoke-urls api origin is not an isolated local origin")
    return web, api


def required_entries(req: dict[str, Any]) -> dict[str, dict[str, Any]]:
    required = req.get("required")
    if not isinstance(required, list) or not required:
        fail("browser-evidence has no required criteria")
    out: dict[str, dict[str, Any]] = {}
    for item in required:
        if not isinstance(item, dict):
            fail("browser-evidence required entries must be objects")
        cid = item.get("id")
        criterion = item.get("criterion")
        path = item.get("path")
        assertions = item.get("assertions")
        if not isinstance(cid, str) or not cid:
            fail("browser-evidence required entries need ids")
        if cid in out:
            fail(f"duplicate required criterion: {cid}")
        if not isinstance(criterion, str) or not criterion.strip():
            fail(f"required criterion {cid} has no criterion text")
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
            fail(f"required criterion {cid} needs a same-origin path")
        if not isinstance(assertions, list) or not assertions:
            fail(f"required criterion {cid} needs frozen assertions")
        for assertion in assertions:
            validate_assertion(cid, assertion)
        out[cid] = item
    return out


def validate_assertion(cid: str, assertion: Any) -> None:
    if not isinstance(assertion, dict):
        fail(f"criterion {cid} assertion must be an object")
    kind = assertion.get("type")
    value = assertion.get("value")
    if kind not in VALID_ASSERTIONS:
        fail(f"criterion {cid} assertion type unsupported")
    if not isinstance(value, str) or not value:
        fail(f"criterion {cid} assertion needs a nonempty value")
    if kind == "fill" and (not isinstance(assertion.get("text"), str) or not assertion.get("text")):
        fail(f"criterion {cid} fill assertion needs text")


def validate_identity(receipt: dict[str, Any]) -> None:
    candidate = receipt.get("candidate")
    api = receipt.get("api")
    if not isinstance(candidate, dict) or not HEX40.fullmatch(str(candidate.get("commit", ""))):
        fail("receipt missing candidate commit identity")
    if not isinstance(candidate.get("tree"), str) or not HEX40.fullmatch(candidate["tree"]):
        fail("receipt missing candidate tree identity")
    if not isinstance(api, dict) or not HEX40.fullmatch(str(api.get("commit", ""))):
        fail("receipt missing API commit identity")
    if not isinstance(api.get("origin"), str) or not api["origin"]:
        fail("receipt missing API origin identity")


def validate_controller_state(receipt: dict[str, Any]) -> None:
    if receipt.get("controller_action") != "finalize-evidence":
        fail("receipt is not bound to finalize-evidence controller action")
    if "controller_seal" in receipt:
        fail("browser receipt cannot self-author controller authentication")
    if receipt.get("authority_state") != "unsealed-local-receipt":
        fail("receipt controller authority state is malformed")


def validate_receipt(req: dict[str, Any], receipt: dict[str, Any], smoke_path: Path, evidence_root: Path, approved_digest: str) -> int:
    required = required_entries(req)
    web_origin, api_origin = smoke_origins(smoke_path)
    if receipt.get("authority") != "controller-playwright" or receipt.get("runner") != "archon-browser-verifier":
        fail("browser receipt is not an advisory Playwright observation")
    if receipt.get("receipt_version") != 1:
        fail("unsupported browser receipt version")
    actual_digest = canonical_digest(req)
    if actual_digest != approved_digest:
        fail("browser-evidence.json does not match approved browser policy digest")
    if receipt.get("requirements_digest") != approved_digest:
        fail("browser receipt is stale for approved browser policy digest")
    if receipt.get("status") != "passed":
        fail("browser receipt did not pass")
    if receipt.get("origin") != web_origin:
        fail("browser receipt origin does not match smoke origin")
    validate_identity(receipt)
    if receipt["api"].get("origin") != api_origin:
        fail("browser receipt API origin does not match smoke origin")
    validate_controller_state(receipt)
    viewport = receipt.get("viewport")
    if not isinstance(viewport, dict) or not isinstance(viewport.get("width"), int) or not isinstance(viewport.get("height"), int):
        fail("browser receipt missing viewport")
    if not (1 <= viewport["width"] <= 10000 and 1 <= viewport["height"] <= 10000):
        fail("browser receipt viewport out of bounds")
    results = receipt.get("criteria")
    if not isinstance(results, list):
        fail("browser receipt criteria must be an array")
    seen: set[str] = set()
    for row in results:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            fail("browser receipt criteria entries need ids")
        cid = row["id"]
        if cid in seen:
            fail(f"duplicate executed criterion: {cid}")
        seen.add(cid)
        if cid not in required:
            fail(f"unexpected executed criterion: {cid}")
        if row.get("status") != "passed":
            fail(f"required criterion did not pass: {cid}")
        if row.get("path") != required[cid].get("path"):
            fail(f"criterion path changed: {cid}")
        assertions = row.get("assertions")
        if not isinstance(assertions, list) or len(assertions) != len(required[cid]["assertions"]):
            fail(f"criterion assertion count changed: {cid}")
        for index, assertion in enumerate(assertions):
            if assertion.get("status") != "passed":
                fail(f"criterion assertion did not pass: {cid}")
            expected = required[cid]["assertions"][index]
            if assertion.get("type") != expected.get("type") or assertion.get("value") != expected.get("value"):
                fail(f"criterion assertion changed: {cid}")
            if expected.get("type") == "fill" and assertion.get("text") != expected.get("text"):
                fail(f"criterion assertion changed: {cid}")
    missing = sorted(set(required) - seen)
    if missing:
        fail("missing required criteria: " + ",".join(missing))
    evidence = receipt.get("evidence")
    if not isinstance(evidence, dict):
        fail("browser receipt evidence must be an object")
    screenshots = evidence.get("screenshots")
    traces = evidence.get("traces")
    if not isinstance(screenshots, list) or not screenshots:
        fail("browser receipt has no screenshots")
    if not isinstance(traces, list) or not traces:
        fail("browser receipt has no traces")
    for label, entries in (("screenshot", screenshots), ("trace", traces)):
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not isinstance(entry.get("sha256"), str):
                fail(f"{label} evidence entry malformed")
            actual = file_digest(evidence_root / entry["path"], evidence_root, label)
            if actual != entry["sha256"]:
                fail(f"{label} evidence digest mismatch: {entry['path']}")
    return len(seen)


def load_approved_digest(req_path: Path, explicit_path: Path | None) -> str:
    path = explicit_path or req_path.with_suffix(".sha256")
    try:
        value = path.read_text(encoding="utf-8").strip().split()[0]
    except (OSError, IndexError) as exc:
        fail(f"approved browser policy digest unavailable: {exc}")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        fail("approved browser policy digest is malformed")
    return value.lower()


def main() -> int:
    if len(sys.argv) not in (4, 5):
        print("UAT_GATE=FAIL usage: check-browser-evidence.py <browser-evidence.json> <browser-receipt.json> <smoke-urls.txt> [approved-digest-file]")
        return 1
    req_path, receipt_path, smoke_path = map(Path, sys.argv[1:4])
    digest_path = Path(sys.argv[4]) if len(sys.argv) == 5 else None
    req = load_object(req_path, "browser-evidence")
    receipt = load_object(receipt_path, "browser-receipt")
    try:
        approved_digest = load_approved_digest(req_path, digest_path)
        count = validate_receipt(req, receipt, smoke_path, receipt_path.parent, approved_digest)
    except SystemExit as exc:
        print(str(exc))
        return 1
    print(f"BROWSER_EVIDENCE=ADVISORY criteria={count} authority=none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
