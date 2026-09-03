#!/usr/bin/env python3
"""No-shell GitNexus CLI fallback for Codex exec sessions without MCP tools."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

MAX_OUTPUT = 2 * 1024 * 1024
COMMAND_TIMEOUT_SECONDS = 120
MAX_QUERY_TERMS = 16
MAX_CONTEXTS = 3


def fail(message: str) -> None:
    print(f"GITNEXUS_CLI=FAIL {message}")
    raise SystemExit(1)


def query_text(artifacts: Path) -> str:
    report = (artifacts / "bug-report-normalized.md").read_text(encoding="utf-8", errors="replace")
    report = re.sub(r"<trace-context>.*?</trace-context>", "", report, flags=re.S | re.I)
    report = re.sub(r"https?://\S+", "", report)
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", report)
    stop = {"the", "and", "for", "with", "that", "this", "from", "report", "expected", "problem", "none", "provided"}
    unique = []
    for word in words:
        lower = word.lower()
        if lower not in stop and lower not in unique:
            unique.append(lower)
    return " ".join(unique[:MAX_QUERY_TERMS])


def run_json(argv: list[str]) -> dict:
    print(f"GITNEXUS_CLI=PROGRESS command={argv[-3] if len(argv) >= 3 else argv[0]}", flush=True)
    try:
        result = subprocess.run(argv, capture_output=True, timeout=COMMAND_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        fail(f"GitNexus command timed out after {COMMAND_TIMEOUT_SECONDS}s")
    if result.returncode != 0:
        fail((result.stderr or result.stdout)[:1000].decode(errors="replace"))
    if len(result.stdout) > MAX_OUTPUT:
        fail("GitNexus output exceeds 2 MiB bound")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        fail(f"GitNexus returned malformed JSON: {exc}")
    if not isinstance(value, dict):
        fail("GitNexus result must be an object")
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    artifacts = args.artifacts.resolve()
    index = Path(os.environ.get("ARCHON_GITNEXUS_INDEX", "")).resolve()
    commit = os.environ.get("ARCHON_GITNEXUS_COMMIT", "")
    if not index.is_dir() or not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        fail("protected index path/commit missing")
    dispatcher = Path(__file__).resolve().with_name("gitnexus-mcp-dispatch.py")
    checked = subprocess.run(["python3", str(dispatcher), "--check"], capture_output=True, text=True)
    if checked.returncode != 0:
        fail((checked.stderr or checked.stdout).strip())
    runner = index / ".gitnexus" / "run.cjs"
    query = query_text(artifacts)
    if not query:
        fail("report yielded no bounded query concepts")
    result = run_json(["node", str(runner), "query", query, "--repo", "api", "--limit", "5"])
    contexts = []
    names = []
    for item in result.get("definitions", []):
        name = item.get("name") if isinstance(item, dict) else None
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    for name in names[:MAX_CONTEXTS]:
        contexts.append({"name": name, "result": run_json([
            "node", str(runner), "context", name, "--repo", "api", "--limit", "10"
        ])})
    payload = {
        "schema_version": 1,
        "transport": "cli-fallback",
        "index": str(index),
        "commit": commit,
        "query": query,
        "query_result": result,
        "contexts": contexts,
    }
    target = artifacts / "evidence" / "gitnexus-cli.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"GITNEXUS_CLI=OK definitions={len(result.get('definitions', []))} contexts={len(contexts)}")


if __name__ == "__main__":
    main()
