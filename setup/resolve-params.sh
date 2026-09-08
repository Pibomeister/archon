#!/usr/bin/env bash
# Run-parameter derivation — the single point where a run's identity comes from.
# Usage: resolve-params.sh <goodword-root> <arguments> <artifacts-dir> [api-port-base] [web-port-base]
# <arguments> is the CLI run message (env ARGUMENTS in nodes): the ABSOLUTE path
# to the feature spec. Empty is a hard failure — a run with no spec has no
# identity; the toy dry-run passes its spec path explicitly like any other run.
# Deterministic: same message, same params — that is what makes resume and
# adopt-if-exists coherent across processes.
#
# The optional port bases are the lane's OWN literals (4124/3124 for bugfix, and
# so on). They are the only place those numbers still appear in a lane, which is
# what keeps derive-lite.py's port map -- a plain string substitution -- working.
# port-alloc.sh turns a base into a per-run port; without bases, params.json
# simply carries no ports and the lane has no smoke stack.
set -euo pipefail

ROOT="${1:?usage: resolve-params.sh <root> <arguments> <artifacts-dir> [api-port-base] [web-port-base]}"
ARGS="${2-}"
AD="${3:?usage: resolve-params.sh <root> <arguments> <artifacts-dir> [api-port-base] [web-port-base]}"
APIBASE="${4-}"
WEBBASE="${5-}"

SPEC="$ARGS"
test -n "$SPEC" || { echo "PARAMS=FAIL no spec path in run message — invoke as: archon workflow run full-sdlc-api \"/abs/path/to/spec.md\""; exit 1; }
case "$SPEC" in
  /*) : ;;
  *) echo "PARAMS=FAIL spec path must be absolute, got: $SPEC"; exit 1 ;;
esac
test -f "$SPEC" || { echo "PARAMS=FAIL spec file missing: $SPEC"; exit 1; }

SLUG=$(basename "$SPEC" | sed 's/\.[^.]*$//' | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//' | cut -c1-60)
test -n "$SLUG" || { echo "PARAMS=FAIL empty slug from spec: $SPEC"; exit 1; }

APIPORT=""; WEBPORT=""
if [ -n "$APIBASE" ]; then
  APIPORT=$(bash "$ROOT/.archon/setup/port-alloc.sh" "$APIBASE" "$SLUG") || { echo "PARAMS=FAIL cannot allocate api port from base $APIBASE"; exit 1; }
fi
if [ -n "$WEBBASE" ]; then
  WEBPORT=$(bash "$ROOT/.archon/setup/port-alloc.sh" "$WEBBASE" "$SLUG") || { echo "PARAMS=FAIL cannot allocate web port from base $WEBBASE"; exit 1; }
fi

python3 - "$AD/params.json" "$SPEC" "$SLUG" "$ROOT" "$APIPORT" "$WEBPORT" <<'PY'
import json, sys
out, spec, slug, root, api_port, web_port = sys.argv[1:7]
params = {
    "spec": spec,
    "slug": slug,
    "branch": f"archon/{slug}",
    "worktree": f"{root}/api/.worktrees/{slug}",
}
if api_port:
    params["api_port"] = int(api_port)
if web_port:
    params["web_port"] = int(web_port)
json.dump(params, open(out, "w", encoding="utf-8"), indent=2)
PY
echo "PARAMS=OK spec=$SPEC slug=$SLUG branch=archon/$SLUG api_port=${APIPORT:-none} web_port=${WEBPORT:-none}"
