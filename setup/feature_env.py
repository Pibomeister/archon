#!/usr/bin/env python3
"""ARCHON_FEATURE_* reads with a params.json fallback.

A plain `archon workflow resume` does not carry the launcher's ARCHON_FEATURE_*
env (observed 2026-09-13, run ffd88016), so every gate keyed on that env
silently read "not a repository run" and took the wrong branch.
trusted-local-candidate.sh:8-13 fixed that inline for itself; this is the same
fallback for the other readers, written once so the paths cannot drift.

params.json is the durable copy: feature_chain.params_payload() writes it right
after the run row exists, and it survives resume because it is a file.

Usage (python): from feature_env import feature_env
                feature_env("ARCHON_FEATURE_SCOPE", artifacts=artifacts_dir)
Usage (shell):  . "$SETUP/feature-env.sh" <artifacts-dir>
"""
from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

# params.json key per env var, exactly as feature_chain.params_payload() writes
# them and as archon-run.py's chain_env() derives them.
KEYS = {
    "ARCHON_FEATURE_SCOPE": "feature_scope",
    "ARCHON_FEATURE_PHASE": "feature_phase",
    "ARCHON_FEATURE_CHAIN_ID": "logical_chain_id",
    "ARCHON_FEATURE_RUN_ID": "run_id",
}


def _artifacts_dir(artifacts: object) -> Path | None:
    """The run's artifacts dir: the caller's, else whatever bound it into env.

    CODEX_RUN_ARTIFACTS is the only handle a Codex hook has — it runs with no
    argv of its own.
    """
    if artifacts:
        return Path(str(artifacts))
    bound = os.environ.get("CODEX_RUN_ARTIFACTS") or os.environ.get("ARTIFACTS_DIR")
    return Path(bound) if bound else None


def params(artifacts: object = None) -> dict:
    """The run's params.json, or {} when there is none to read.

    Unreadable is the same as absent on purpose: this is a fallback, and a
    reader whose env is also empty must still reach its own typed failure
    rather than die here with a traceback.
    """
    directory = _artifacts_dir(artifacts)
    if directory is None:
        return {}
    try:
        loaded = json.loads((directory / "params.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def feature_env(name: str, default: str | None = None, artifacts: object = None) -> str | None:
    """env first, params.json second, the caller's default last."""
    value = os.environ.get(name)
    if value:
        return value
    value = params(artifacts).get(KEYS.get(name, name))
    return value if isinstance(value, str) and value else default


def feature_env_all(artifacts: object = None) -> dict[str, str]:
    resolved = params(artifacts)
    out = {}
    for name, key in KEYS.items():
        value = os.environ.get(name) or resolved.get(key)
        if isinstance(value, str) and value:
            out[name] = value
    return out


if __name__ == "__main__":
    for key, val in feature_env_all(sys.argv[1] if len(sys.argv) > 1 else None).items():
        print(f"export {key}={shlex.quote(val)}")
