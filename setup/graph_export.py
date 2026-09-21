#!/usr/bin/env python3
"""Export a compiled Archon workflow YAML as archon.workflow-graph.v1 JSON.

Node ids stay the YAML ids. Project identity is metadata, never a node.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

SCHEMA = "archon.workflow-graph.v1"


def _kind(node: dict) -> str:
    if node.get("loop") or node.get("loop_group"):
        return "loop"
    if node.get("approval"):
        return "approval"
    if node.get("prompt"):
        return "prompt"
    if node.get("bash"):
        return "bash"
    if node.get("script") or node.get("command"):
        return "script"
    return "other"


def _walk(nodes, parent=None):
    for node in nodes or []:
        if not isinstance(node, dict) or not node.get("id"):
            continue
        yield node, parent
        for key in ("loop_group", "loop", "body"):
            inner = node.get(key)
            if isinstance(inner, dict):
                yield from _walk(inner.get("nodes"), parent=node["id"])
            elif isinstance(inner, list):
                yield from _walk(inner, parent=node["id"])


def export_workflow(path, profile_id=None) -> dict:
    path = Path(path)
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    name = doc.get("name") or path.stem
    nodes = []
    seen = set()
    for node, parent in _walk(doc.get("nodes")):
        nid = node["id"]
        if nid in seen:
            continue
        seen.add(nid)
        depends = node.get("depends_on") or []
        if isinstance(depends, str):
            depends = [depends]
        entry = {
            "id": nid,
            "kind": _kind(node),
            "depends_on": list(depends),
        }
        if parent:
            entry["parent"] = parent
        when = node.get("when")
        if when:
            entry["when"] = when
        nodes.append(entry)
    graph = {
        "schema": SCHEMA,
        "workflow": name,
        "nodes": nodes,
    }
    if profile_id:
        graph["profileId"] = profile_id
    return graph


def write(graph: dict, artifacts) -> Path:
    artifacts = Path(artifacts)
    artifacts.mkdir(parents=True, exist_ok=True)
    dest = artifacts / "graph.json"
    dest.write_text(json.dumps(graph, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return dest


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.stderr.write("usage: graph_export.py <workflow.yaml> [artifacts-dir] [profile-id]\n")
        return 2
    path = argv[0]
    artifacts = argv[1] if len(argv) > 1 else None
    profile_id = argv[2] if len(argv) > 2 else None
    graph = export_workflow(path, profile_id=profile_id)
    if artifacts:
        write(graph, artifacts)
        sys.stdout.write(f"GRAPH=OK workflow={graph['workflow']} nodes={len(graph['nodes'])}\n")
    else:
        sys.stdout.write(json.dumps(graph, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
