#!/usr/bin/env python3
"""Resolve the Archon layer root (this pack). Fail-closed.

The layer is the directory that contains setup/resolve-params.sh. YAML bash
must receive ARCHON_LAYER from the engine, archon-run.py, or tests. Python
helpers may discover it from this file's location when the env is unset.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

MARKER = Path("setup") / "resolve-params.sh"


class LayerRootError(SystemExit):
    def __init__(self, detail):
        super().__init__(f"LAYER_ROOT=FAIL {detail}")


def layer_root(env=None) -> Path:
    env = os.environ if env is None else env
    raw = (env.get("ARCHON_LAYER") or "").strip()
    if raw:
        root = Path(raw).expanduser().resolve()
        if not (root / MARKER).is_file():
            raise LayerRootError(f"ARCHON_LAYER does not contain {MARKER}: {root}")
        return root
    here = Path(__file__).resolve().parent.parent
    if (here / MARKER).is_file():
        return here
    raise LayerRootError("ARCHON_LAYER unset and this file is not inside a layer")


def main(argv=None):
    try:
        sys.stdout.write(str(layer_root()) + "\n")
    except LayerRootError as exc:
        sys.stderr.write(str(exc) + "\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
