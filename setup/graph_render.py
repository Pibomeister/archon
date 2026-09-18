#!/usr/bin/env python3
"""Render archon.workflow-graph.v1 as mermaid. Topology uses node ids only."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def mermaid(graph: dict) -> str:
    lines = ["flowchart TD"]
    ids = {n["id"] for n in graph.get("nodes") or []}
    for node in graph.get("nodes") or []:
        nid = node["id"]
        kind = node.get("kind") or "other"
        lines.append(f"  {nid}[\"{nid} ({kind})\"]")
        for dep in node.get("depends_on") or []:
            if dep in ids:
                lines.append(f"  {dep} --> {nid}")
    return "\n".join(lines) + "\n"


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.stderr.write("usage: graph_render.py <graph.json>\n")
        return 2
    graph = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    sys.stdout.write(mermaid(graph))
    return 0


if __name__ == "__main__":
    sys.exit(main())
