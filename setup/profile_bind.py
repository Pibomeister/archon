#!/usr/bin/env python3
"""Bind a project profile into run artifacts for bash nodes and prompt nodes.

Writes profile-runtime.json and profile-runtime.sh. Does not invent stack
commands: required tools, KB root, GitNexus index, layout, and repo toolchains
come from the profile pack.
"""
from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path


class ProfileBindError(SystemExit):
    def __init__(self, detail):
        super().__init__(f"PROFILE_BIND=FAIL {detail}")


def _read_json(path: Path):
    if path.is_symlink() or not path.is_file():
        raise ProfileBindError(f"refusing non-regular file: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProfileBindError(f"not JSON: {path}: {exc}") from exc


def _repos_from_pack(profile: dict, layer: Path) -> dict:
    guidance = profile.get("guidance") or {}
    rel = guidance.get("root")
    if not rel or not isinstance(rel, str):
        return {}
    path = (layer / rel / "repos.json").resolve()
    try:
        path.relative_to(layer.resolve())
    except ValueError:
        raise ProfileBindError(f"repos.json escapes the layer: {rel}")
    if not path.is_file():
        return {}
    data = _read_json(path)
    profiles = data.get("profiles")
    if not isinstance(profiles, dict):
        raise ProfileBindError("repos.json profiles must be an object")
    return profiles


def check_tools(tools, present):
    """Return (hard_fails, warns) given requiredTools and a set of command names."""
    fails, warns = [], []
    for item in tools or []:
        if not isinstance(item, dict) or not item.get("id"):
            raise ProfileBindError("requiredTools entries need id and fail")
        name = item["id"]
        hard = bool(item.get("fail"))
        if name not in present:
            (fails if hard else warns).append(name)
    return fails, warns


def bind(profile: dict, layer, project_root, artifacts) -> dict:
    layer = Path(layer)
    project_root = Path(project_root)
    artifacts = Path(artifacts)
    artifacts.mkdir(parents=True, exist_ok=True)
    capabilities = profile.get("capabilities") or {}
    if capabilities and not isinstance(capabilities, dict):
        raise ProfileBindError("capabilities must be an object")
    knowledge = profile.get("knowledge") or {}
    if knowledge and not isinstance(knowledge, dict):
        raise ProfileBindError("knowledge must be an object")
    kb_rel = knowledge.get("root") or ""
    if kb_rel and (str(kb_rel).startswith("/") or ".." in Path(str(kb_rel)).parts):
        raise ProfileBindError(f"knowledge.root must be a relative sibling: {kb_rel}")
    kb_required = bool(knowledge.get("required", bool(kb_rel)))
    allowed = capabilities.get("allowedRepos") or []
    if not isinstance(allowed, list) or not all(isinstance(x, str) and x for x in allowed):
        raise ProfileBindError("allowedRepos must be a list of non-empty strings")
    tools = capabilities.get("requiredTools") or []
    layout = capabilities.get("layout") or "siblings"
    if layout not in ("siblings", "single"):
        raise ProfileBindError(f"unknown layout {layout}")
    default_repo = capabilities.get("defaultRepo") or ("fluxkeep" if layout == "single" else "api")
    web_repo = capabilities.get("webRepo") or ""
    checkouts = capabilities.get("requiredCheckouts") or []
    if not isinstance(checkouts, list) or not all(isinstance(x, str) and x for x in checkouts):
        raise ProfileBindError("requiredCheckouts must be a list of non-empty strings")
    repos = _repos_from_pack(profile, layer)
    runtime = {
        "profileId": profile.get("projectId") or "project:unknown",
        "layout": layout,
        "defaultRepo": default_repo,
        "webRepo": web_repo,
        "requiredCheckouts": checkouts,
        "allowedRepos": allowed,
        "kbRootRel": kb_rel,
        "kbRequired": kb_required,
        "gitnexusRepo": capabilities.get("gitnexusRepo") or "",
        "impactUnavailable": capabilities.get("impactUnavailable") or "route-full",
        "generatedApiSync": bool(capabilities.get("generatedApiSync")),
        "browserUat": bool(capabilities.get("browserUat")),
        "requiredTools": tools,
        "repos": repos,
        "projectRoot": str(project_root),
    }
    (artifacts / "profile-runtime.json").write_text(
        json.dumps(runtime, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    sh = [
        f"PROFILE_ID={shlex.quote(runtime['profileId'])}",
        f"PROFILE_LAYOUT={shlex.quote(layout)}",
        f"DEFAULT_REPO={shlex.quote(default_repo)}",
        f"ALLOWED_REPOS={shlex.quote(','.join(allowed))}",
        f"WEB_REPO={shlex.quote(web_repo)}",
        f"REQUIRED_CHECKOUTS={shlex.quote(','.join(checkouts))}",
        f"KB_ROOT_REL={shlex.quote(kb_rel)}",
        f"KB_REQUIRED={'1' if kb_required else '0'}",
        f"GITNEXUS_REPO={shlex.quote(runtime['gitnexusRepo'])}",
        f"IMPACT_UNAVAILABLE={shlex.quote(runtime['impactUnavailable'])}",
        f"GENERATED_API_SYNC={'1' if runtime['generatedApiSync'] else '0'}",
        f"BROWSER_UAT={'1' if runtime['browserUat'] else '0'}",
    ]
    (artifacts / "profile-runtime.sh").write_text("\n".join(sh) + "\n", encoding="utf-8")
    return runtime


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 3:
        sys.stderr.write("usage: profile_bind.py <profile.json> <layer-root> <artifacts-dir>\n")
        return 2
    profile_path, layer, artifacts = argv
    try:
        profile = _read_json(Path(profile_path))
        project_root = Path(layer).parent
        import os
        raw = (os.environ.get("PROJECT_ROOT") or "").strip()
        if raw:
            project_root = Path(raw)
        runtime = bind(profile, layer, project_root, artifacts)
    except ProfileBindError as exc:
        sys.stderr.write(str(exc) + "\n")
        return 1
    sys.stdout.write(
        f"PROFILE_BIND=OK profile={runtime['profileId']} layout={runtime['layout']}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
