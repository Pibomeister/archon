#!/usr/bin/env python3
"""Materialize project guidance into a run's artifacts.

The control graph never inlines stack commands or host paths. Preflight (or a
node immediately after it) copies the Archon project's playbook and conventions
into artifacts/project-guidance.md. Prompt nodes read that file.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path


class GuidanceError(SystemExit):
    def __init__(self, detail):
        super().__init__(f"GUIDANCE=FAIL {detail}")


def _read(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise GuidanceError(f"refusing non-regular file: {path}")
    return path.read_text(encoding="utf-8")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pack_root(profile: dict, layer: Path) -> Path:
    guidance = profile.get("guidance") or {}
    rel = guidance.get("root")
    if not rel or not isinstance(rel, str):
        raise GuidanceError("guidance.root missing")
    if rel.startswith("/") or ".." in Path(rel).parts:
        raise GuidanceError(f"guidance.root must be a relative pack path: {rel}")
    root = (layer / rel).resolve()
    try:
        root.relative_to(layer.resolve())
    except ValueError:
        raise GuidanceError(f"guidance.root escapes the layer: {rel}")
    if not root.is_dir():
        raise GuidanceError(f"guidance pack missing: {rel}")
    return root


def materialize(profile: dict, layer, artifacts) -> dict:
    layer = Path(layer)
    artifacts = Path(artifacts)
    artifacts.mkdir(parents=True, exist_ok=True)
    project_id = profile.get("projectId") or "project:unknown"
    guidance = profile.get("guidance")
    if not guidance:
        text = (
            f"# Project guidance\n\n"
            f"profile: {project_id}\n\n"
            "No project guidance declared. Do not invent stack commands.\n"
        )
        (artifacts / "project-guidance.md").write_text(text, encoding="utf-8")
        (artifacts / "project-guidance.digest.txt").write_text(_sha256(text) + "\n", encoding="utf-8")
        return {"status": "EMPTY", "profileId": project_id, "digest": _sha256(text)}

    if not isinstance(guidance, dict):
        raise GuidanceError("guidance must be an object")
    pack = _pack_root(profile, layer)
    parts = [f"# Project guidance\n\nprofile: {project_id}\npack: {guidance['root']}\n"]
    playbook = guidance.get("playbook")
    if playbook:
        path = pack / playbook
        if not path.is_file():
            raise GuidanceError(f"playbook missing: {playbook}")
        parts.append("## Playbook\n")
        parts.append(_read(path).rstrip() + "\n")
    for rel in guidance.get("conventions") or []:
        if not isinstance(rel, str) or not rel or rel.startswith("/") or ".." in Path(rel).parts:
            raise GuidanceError(f"invalid conventions path: {rel!r}")
        path = pack / rel
        if not path.is_file():
            raise GuidanceError(f"conventions missing: {rel}")
        parts.append(f"## Conventions ({rel})\n")
        parts.append(_read(path).rstrip() + "\n")
    envelope_rel = guidance.get("envelopePath")
    if envelope_rel:
        path = pack / envelope_rel
        if not path.is_file():
            raise GuidanceError(f"envelope missing: {envelope_rel}")
        dest = artifacts / "lite-envelope.json"
        dest.write_text(_read(path), encoding="utf-8")
        parts.append(f"## Envelope\nCopied to lite-envelope.json from {envelope_rel}.\n")
        try:
            envelope = json.loads(dest.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise GuidanceError(f"envelope is not JSON: {exc}")
        hot = envelope.get("hot_paths") or []
        if hot:
            parts.append("hot_paths:")
            parts.extend(f"- {item}" for item in hot)
            parts.append("")
    debug = guidance.get("debug") or []
    if debug:
        parts.append("## Debug\n")
        for item in debug:
            if not isinstance(item, dict) or not item.get("id") or not item.get("cli") or not item.get("note"):
                raise GuidanceError("debug entries need id, cli, note")
            parts.append(f"- {item['id']}: use `{item['cli']}` — {item['note']}")
        parts.append("")
    text = "\n".join(parts).rstrip() + "\n"
    host_home = "Documents/Workspace/" + "Goodword"
    if host_home in text or re.search(r"/Use" + r"rs/[A-Za-z0-9]", text):
        raise GuidanceError("materialized guidance contains a host path")
    digest = _sha256(text)
    (artifacts / "project-guidance.md").write_text(text, encoding="utf-8")
    (artifacts / "project-guidance.digest.txt").write_text(digest + "\n", encoding="utf-8")
    return {"status": "GATHERED", "profileId": project_id, "digest": digest, "pack": str(pack)}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 3:
        sys.stderr.write("usage: materialize_guidance.py <profile.json> <layer-root> <artifacts-dir>\n")
        return 2
    profile_path, layer, artifacts = argv
    try:
        profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))
        result = materialize(profile, layer, artifacts)
    except GuidanceError as exc:
        sys.stderr.write(str(exc) + "\n")
        return 1
    sys.stdout.write(f"GUIDANCE={result['status']} profile={result['profileId']} digest={result['digest']}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
