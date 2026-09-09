#!/usr/bin/env python3
"""Fail-closed read-only proposal-v2 validation for Archon backfills.

This helper cannot apply writes or render executable SQL. Instruments propose materialized record
mutations; they do not receive write credentials and they do not provide raw apply
commands. Production admission remains disabled in backfill.yaml until this executor
is wired through hardened controller dispatch and the database-backed matrix passes.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
LEGACY_WRITE_FIELDS = {"apply_command", "apply_sql"}
SCALAR_TYPES = (str, int, bool, type(None))
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
SUPPORTED_SQL_TYPE_RE = re.compile(r"(?:smallint|integer|boolean|text|uuid|character varying(?:\([1-9][0-9]*\))?)")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def fail(message: str) -> None:
    raise ValueError(message)


def require(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def require_ident(value: Any, label: str) -> str:
    require(isinstance(value, str) and bool(IDENT_RE.fullmatch(value)), f"{label} must be a simple identifier")
    return value


def require_digest(value: Any, label: str) -> str:
    require(isinstance(value, str) and bool(DIGEST_RE.fullmatch(value)), f"{label} must be sha256:<64 hex>")
    return value


def validate_canonical_subset(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            require(isinstance(key, str) and bool(re.fullmatch(r"[\x20-\x7e]*", key)), "canonical object keys must be printable ASCII")
            validate_canonical_subset(item)
    elif isinstance(value, list):
        for item in value:
            validate_canonical_subset(item)
    elif type(value) is int:
        require(abs(value) <= 9007199254740991, "canonical integer exceeds JSON safe range")
    else:
        require(value is None or type(value) in (str, bool), "value is outside the canonical subset")


def canonical_json_bytes(value: Any) -> bytes:
    validate_canonical_subset(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest_value(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def proposal_payload_for_digest(doc: dict[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(doc)
    payload.pop("proposal_digest", None)
    return payload


def validate_canonical_digests(doc: dict[str, Any]) -> None:
    expected = digest_value(proposal_payload_for_digest(doc))
    require(doc.get("proposal_digest") == expected, "proposal_digest mismatch")
    undo = doc.get("undo")
    require(isinstance(undo, dict), "undo is required")
    definition = undo.get("definition")
    require(definition is not None, "undo.definition is required")
    require(undo.get("definition_digest") == digest_value(definition), "undo.definition_digest mismatch")


def validate_instrument_binding(doc: dict[str, Any]) -> None:
    instrument = doc.get("instrument")
    require(isinstance(instrument, dict), "instrument immutable binding is required")
    require(instrument.get("repo") == "api", "instrument.repo must be api")
    require(isinstance(instrument.get("version"), str) and instrument["version"].strip(), "instrument.version is required")
    require(isinstance(instrument.get("source_files"), list) and bool(instrument["source_files"]), "instrument.source_files is required")
    for source in instrument["source_files"]:
        require(isinstance(source, str) and source.strip() and not source.startswith("/") and ".." not in Path(source).parts, "instrument.source_files must be repo-relative paths")
    for key in ("commit", "tree"):
        require(isinstance(instrument.get(key), str) and bool(re.fullmatch(r"[0-9a-f]{40}", instrument[key])), f"instrument.{key} must be a full git object id")

def reject_legacy_write_authority(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            require(key not in LEGACY_WRITE_FIELDS, f"legacy write authority rejected: {key}")
            reject_legacy_write_authority(nested)
    elif isinstance(value, list):
        for nested in value:
            reject_legacy_write_authority(nested)


def validate_identifier_set(value: Any, label: str) -> None:
    require(isinstance(value, list) and bool(value), f"{label} must be non-empty")
    for item in value:
        require_ident(item, label)
    require(len(set(value)) == len(value), f"{label} must be unique")


def validate_scalar_map(value: Any, label: str) -> dict[str, Any]:
    require(isinstance(value, dict) and bool(value), f"{label} must be a non-empty object")
    out: dict[str, Any] = {}
    for key, item in value.items():
        require_ident(key, f"{label} key")
        require(isinstance(item, SCALAR_TYPES), f"{label}.{key} must be a scalar from the canonical subset")
        require(not isinstance(item, int) or isinstance(item, bool) or abs(item) <= 9007199254740991, f"{label}.{key} integer exceeds JSON safe range")
        out[key] = item
    return out


def validate_target(doc: dict[str, Any]) -> None:
    target = doc.get("target")
    require(isinstance(target, dict), "target is required")
    identity = target.get("database_identity")
    require(isinstance(identity, dict) and bool(identity), "target.database_identity is required")
    for key in ("database", "role", "cluster"):
        require(isinstance(identity.get(key), str) and identity[key].strip(), f"target.database_identity.{key} is required")
    tables = target.get("tables")
    keys = target.get("key_columns")
    validate_identifier_set(tables, "target.tables")
    validate_identifier_set(keys, "target.key_columns")
    for table in tables:
        require_ident(table, "target table")
    for key in keys:
        require_ident(key, "target key column")
    metadata = target.get("schema_metadata")
    require(isinstance(metadata, dict) and bool(metadata), "target.schema_metadata is required")
    require(set(metadata.keys()) == set(tables), "target.schema_metadata must match target.tables exactly")


def validate_schema_effects(doc: dict[str, Any]) -> None:
    metadata = doc["target"]["schema_metadata"]
    for table, table_meta in metadata.items():
        require_ident(table, "schema_metadata table")
        require(isinstance(table_meta, dict), f"schema_metadata.{table} must be an object")
        validate_identifier_set(table_meta.get("columns"), f"schema_metadata.{table}.columns")
        column_types = table_meta.get("column_types")
        if column_types is not None:
            require(isinstance(column_types, dict), "column_types must be an object")
            for column in column_types:
                require_ident(column, "column_types key")
        for key in ("triggers", "cascades", "external_side_effects", "side_effects"):
            value = table_meta.get(key)
            if value is None or value is False or value == [] or value == {}:
                continue
            fail(f"unsupported schema effects for {table}: {key}")

def validate_bounds(doc: dict[str, Any]) -> None:
    bounds = doc.get("bounds")
    require(isinstance(bounds, dict), "bounds is required")
    require(set(bounds.keys()) == {"max_rows", "chunk_size"}, "bounds contains unsupported keys")
    max_rows = bounds.get("max_rows")
    chunk_size = bounds.get("chunk_size")
    require(isinstance(max_rows, int) and not isinstance(max_rows, bool) and max_rows > 0, "bounds.max_rows must be a positive integer")
    require(isinstance(chunk_size, int) and not isinstance(chunk_size, bool) and 0 < chunk_size <= min(max_rows, 500), "bounds.chunk_size must be in 1..max_rows")


def validate_typed_value(value: Any, sql_type: str, label: str, allow_null: bool) -> None:
    if value is None:
        require(allow_null, f"{label} cannot be null")
        return
    if sql_type in ("smallint", "integer"):
        lower, upper = (-32768, 32767) if sql_type == "smallint" else (-2147483648, 2147483647)
        valid = type(value) is int and lower <= value <= upper
    elif sql_type == "boolean":
        valid = type(value) is bool
    else:
        valid = isinstance(value, str)
        if sql_type == "uuid":
            valid = valid and bool(UUID_RE.fullmatch(value))
    require(valid, f"{label} value does not match approved type {sql_type}")


def validate_mutation_types(mutation: dict[str, Any], target: dict[str, Any]) -> None:
    metadata = target["schema_metadata"][mutation["table"]]
    types = metadata.get("column_types")
    require(isinstance(types, dict), "mutation column types are required")
    for group in ("key", "preconditions", "set"):
        for column, value in mutation[group].items():
            sql_type = types.get(column)
            require(isinstance(sql_type, str) and bool(SUPPORTED_SQL_TYPE_RE.fullmatch(sql_type)),
                    f"mutation column type missing or unsupported for {column}")
            validate_typed_value(value, sql_type, f"mutation {group}.{column}", group != "key")


def validate_mutation(value: Any, seen: set[str], target: dict[str, Any]) -> None:
    require(isinstance(value, dict), "mutation must be an object")
    op = value.get("operation_key")
    require(isinstance(op, str) and bool(ID_RE.fullmatch(op)), "mutation operation_key is invalid")
    require(op not in seen, f"duplicate operation_key {op}")
    seen.add(op)
    require_ident(value.get("table"), "mutation table")
    key = validate_scalar_map(value.get("key"), "mutation key")
    require(value["table"] in target["tables"], "mutation targets unapproved table")
    require(set(key) == set(target["key_columns"]), "mutation key columns mismatch")
    before = validate_scalar_map(value.get("preconditions"), "mutation preconditions")
    desired = validate_scalar_map(value.get("set"), "mutation set")
    allowed = set(target["schema_metadata"][value["table"]]["columns"])
    require(set(before).union(desired).issubset(allowed), "mutation uses unapproved column")
    require(not set(desired).intersection(target["key_columns"]), "mutation cannot change key columns")
    validate_mutation_types(value, target)


def validate_proposal(doc: dict[str, Any]) -> dict[str, Any]:
    require(isinstance(doc, dict), "proposal must be an object")
    reject_legacy_write_authority(doc)
    validate_canonical_subset(doc)
    require(doc.get("version") == "backfill.proposal.v2", "version must be backfill.proposal.v2")
    require(isinstance(doc.get("run_id"), str) and bool(ID_RE.fullmatch(doc["run_id"])), "run_id is invalid")
    require_digest(doc.get("proposal_digest"), "proposal_digest")
    validate_instrument_binding(doc)
    validate_target(doc)
    validate_schema_effects(doc)
    validate_bounds(doc)
    mutations = doc.get("mutations")
    require(isinstance(mutations, list) and bool(mutations), "mutations must materialize record identities")
    require(len(mutations) <= doc["bounds"]["max_rows"], "proposal exceeds approved row bound")
    seen: set[str] = set()
    identities: set[bytes] = set()
    for mutation in mutations:
        validate_mutation(mutation, seen, doc["target"])
        identity = canonical_json_bytes([mutation["table"], mutation["key"]])
        require(identity not in identities, "duplicate record identity")
        identities.add(identity)
    require(isinstance(doc.get("verification"), dict), "verification is required")
    undo = doc.get("undo")
    require(isinstance(undo, dict), "undo is required")
    require_digest(undo.get("definition_digest"), "undo.definition_digest")
    validate_canonical_digests(doc)
    return doc


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(data, dict), "proposal must be a JSON object")
    return data


def cmd_validate(path: Path) -> int:
    doc = validate_proposal(load_json(path))
    print(f"BACKFILL_PROPOSAL_V2=OK run_id={doc['run_id']} mutations={len(doc['mutations'])}")
    return 0


def cmd_render_sql(path: Path, output: Path, chunk_id: str) -> int:
    print("BACKFILL_TRANSACTION_SQL=DISABLED use the trusted parameterized executor")
    return 1


def cmd_apply_chunk(path: Path, artifacts: Path, chunk_id: str) -> int:
    print("BACKFILL_PRODUCTION_ADMISSION=DISABLED hardened controller executor required")
    return 1


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Archon backfill proposal-v2 transactional executor")
    sub = p.add_subparsers(dest="command", required=True)
    v = sub.add_parser("validate-plan")
    v.add_argument("proposal", type=Path)
    r = sub.add_parser("render-sql")
    r.add_argument("proposal", type=Path)
    r.add_argument("output", type=Path)
    r.add_argument("--chunk-id", default="chunk-1")
    a = sub.add_parser("apply-chunk")
    a.add_argument("proposal", type=Path)
    a.add_argument("artifacts", type=Path)
    a.add_argument("--chunk-id", default="chunk-1")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "validate-plan":
            return cmd_validate(args.proposal)
        if args.command == "render-sql":
            return cmd_render_sql(args.proposal, args.output, args.chunk_id)
        if args.command == "apply-chunk":
            return cmd_apply_chunk(args.proposal, args.artifacts, args.chunk_id)
    except ValueError as exc:
        print(f"BACKFILL_PROPOSAL_V2=FAIL {exc}")
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
