#!/usr/bin/env bash
# Run-parameter derivation — the single point where a run's identity comes from.
# Usage: resolve-params.sh <goodword-root> <arguments> <artifacts-dir>
# <arguments> is the CLI run message (env ARGUMENTS in nodes): the ABSOLUTE path
# to the feature spec. Empty is a hard failure — a run with no spec has no
# identity; the toy dry-run passes its spec path explicitly like any other run.
# Deterministic: same message, same params — that is what makes resume and
# adopt-if-exists coherent across processes.
set -euo pipefail

ROOT="${1:?usage: resolve-params.sh <root> <arguments> <artifacts-dir>}"
ARGS="${2-}"
AD="${3:?usage: resolve-params.sh <root> <arguments> <artifacts-dir>}"

SPEC="$ARGS"
test -n "$SPEC" || { echo "PARAMS=FAIL no spec path in run message — invoke as: archon workflow run full-sdlc-api \"/abs/path/to/spec.md\""; exit 1; }
case "$SPEC" in
  /*) : ;;
  *) echo "PARAMS=FAIL spec path must be absolute, got: $SPEC"; exit 1 ;;
esac
test -f "$SPEC" || { echo "PARAMS=FAIL spec file missing: $SPEC"; exit 1; }

SLUG=$(basename "$SPEC" | sed 's/\.[^.]*$//' | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//' | cut -c1-60)
test -n "$SLUG" || { echo "PARAMS=FAIL empty slug from spec: $SPEC"; exit 1; }

python3 - "$AD/params.json" "$SPEC" "$SLUG" "$ROOT" <<'PY'
import json, sys
out, spec, slug, root = sys.argv[1:5]
json.dump({
    "spec": spec,
    "slug": slug,
    "branch": f"archon/{slug}",
    "worktree": f"{root}/api/.worktrees/{slug}",
}, open(out, "w", encoding="utf-8"), indent=2)
PY
echo "PARAMS=OK spec=$SPEC slug=$SLUG branch=archon/$SLUG"
