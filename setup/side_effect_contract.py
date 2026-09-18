#!/usr/bin/env python3
"""Named settlement for workflow nodes that mutate remotes, GitHub, compose,
or publication. This is the workflow-layer G3 contract: resume/replay must
not treat these as ordinary bash. Generated *-codex and *-grok twins are
excluded; they inherit the parent node ids."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml

PATTERNS = {
    "compose": re.compile(r"docker compose"),
    "gh_pr_ready": re.compile(r"gh pr ready"),
    "gh_pr_comment": re.compile(r"gh pr comment"),
    "gh_pr_create": re.compile(r"gh pr create"),
    "git_push": re.compile(r"git push"),
    "publish": re.compile(r"SHIP=DISABLED|feature-publish"),
}


@dataclass(frozen=True)
class Entry:
    workflow: str
    node_id: str
    kind: str
    settlement: str


ENTRIES: tuple[Entry, ...] = (
    Entry("babysit.yaml", "api-watch", "gh_pr_ready", "logged"),
    Entry("babysit.yaml", "web-watch", "gh_pr_ready", "logged"),
    Entry("bugfix.yaml", "exit-gate", "compose", "e2e-mutex"),
    Entry("bugfix.yaml", "smoke-stack", "compose", "e2e-mutex"),
    Entry("bugfix.yaml", "ship", "publish", "disabled"),
    Entry("bugfix-lite.yaml", "exit-gate", "compose", "e2e-mutex"),
    Entry("bugfix-lite.yaml", "ship", "publish", "disabled"),
    Entry("bugfix-smoke-deployed.yaml", "pr-comment", "gh_pr_comment", "logged"),
    Entry("full-sdlc-api.yaml", "ship", "publish", "disabled"),
    Entry("full-sdlc-api-lite.yaml", "ship", "publish", "disabled"),
    Entry("full-sdlc-web.yaml", "ship", "publish", "disabled"),
    Entry("wrap-ship.yaml", "ship", "publish", "disabled"),
)


def walk(nodes: Iterable[dict] | None):
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        yield n
        lg = n.get("loop_group")
        if isinstance(lg, dict):
            yield from walk(lg.get("nodes"))


def detect(workflows_dir: Path) -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    for path in sorted(workflows_dir.glob("*.yaml")):
        if path.name.endswith("-codex.yaml") or path.name.endswith("-grok.yaml"):
            continue
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        for n in walk(doc.get("nodes")):
            body = "\n".join(
                part for part in (n.get("bash"), n.get("prompt"), n.get("script") and str(n.get("script")))
                if part
            )
            for kind, pat in PATTERNS.items():
                if pat.search(body):
                    found.append((path.name, n.get("id") or "", kind))
    return found


def unregistered(workflows_dir: Path) -> list[tuple[str, str, str]]:
    registered = {(e.workflow, e.node_id, e.kind) for e in ENTRIES}
    return [hit for hit in detect(workflows_dir) if hit not in registered]


def stale_entries(workflows_dir: Path) -> list[tuple[str, str, str]]:
    present = set(detect(workflows_dir))
    return [(e.workflow, e.node_id, e.kind) for e in ENTRIES if (e.workflow, e.node_id, e.kind) not in present]
