#!/usr/bin/env bash
# Bind the Archon project pack, check its required tools/KB, then derive params.
# Usage: profile-preflight.sh <api-port-base> <workflow.yaml> [web-port-base] [--web] [--checkouts]
#
# Port bases stay lane literals so derive-lite.py's substitution still works.
# Stack CLIs, KB path, GitNexus index, and allowed repos come from the profile.
set -euo pipefail
: "${ARCHON_LAYER:?PREFLIGHT=FAIL ARCHON_LAYER unset}"
: "${PROJECT_ROOT:?PREFLIGHT=FAIL PROJECT_ROOT unset}"
: "${ARTIFACTS_DIR:?PREFLIGHT=FAIL ARTIFACTS_DIR unset}"
APIBASE="${1:?usage: profile-preflight.sh <api-port-base> <workflow.yaml> [web-port-base] [--web] [--checkouts]}"
WORKFLOW_YAML="${2:?usage: profile-preflight.sh <api-port-base> <workflow.yaml> [web-port-base] [--web] [--checkouts]}"
shift 2
WEBBASE=""
RESOLVER=params
CHECKOUTS=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --web) RESOLVER=web; shift ;;
    --checkouts) CHECKOUTS=1; shift ;;
    *) WEBBASE="$1"; shift ;;
  esac
done

PROFILE="${ARCHON_PROJECT_PROFILE:-$ARCHON_LAYER/profiles/goodword/project.v1.json}"
test -f "$PROFILE" || { echo "PREFLIGHT=FAIL profile missing: $PROFILE"; exit 1; }

python3 "$ARCHON_LAYER/setup/materialize_guidance.py" "$PROFILE" "$ARCHON_LAYER" "$ARTIFACTS_DIR" || exit 1
python3 "$ARCHON_LAYER/setup/profile_bind.py" "$PROFILE" "$ARCHON_LAYER" "$ARTIFACTS_DIR" || exit 1
# shellcheck disable=SC1091
source "$ARTIFACTS_DIR/profile-runtime.sh"

python3 - "$ARTIFACTS_DIR/profile-runtime.json" <<'PY'
import json, shutil, sys
from pathlib import Path
runtime = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
fails, warns = [], []
for item in runtime.get("requiredTools") or []:
    name = item["id"]
    if shutil.which(name):
        continue
    if item.get("fail"):
        fails.append(name)
    else:
        warns.append(name)
        print(f"PREFLIGHT_WARN {name} unavailable - evidence/runtime integrations may degrade; workflow control continues")
if fails:
    print("PREFLIGHT=FAIL missing required tool(s): " + ",".join(fails))
    raise SystemExit(1)
print("PREFLIGHT_TOOLS=OK")
PY
NEED_GH=$(python3 - "$ARTIFACTS_DIR/profile-runtime.json" <<'PY'
import json, sys
from pathlib import Path
runtime = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print("1" if any(t.get("id") == "gh" and t.get("fail") for t in runtime.get("requiredTools") or []) else "0")
PY
)
if [ "$NEED_GH" = 1 ] && [ "${ARCHON_FEATURE_SCOPE-}" != "repositories" ]; then
  gh auth status >/dev/null 2>&1 || { echo "PREFLIGHT=FAIL gh unauthenticated"; exit 1; }
fi

if [ "$KB_REQUIRED" = 1 ]; then
  test -n "$KB_ROOT_REL" || { echo "PREFLIGHT=FAIL knowledge.required but knowledge.root empty"; exit 1; }
  test -d "$PROJECT_ROOT/$KB_ROOT_REL/wiki" || { echo "PREFLIGHT=FAIL knowledge base missing"; exit 1; }
  git -C "$PROJECT_ROOT/$KB_ROOT_REL" status --porcelain | sort > "$ARTIFACTS_DIR/kb-pre-porcelain.txt"
else
  : > "$ARTIFACTS_DIR/kb-pre-porcelain.txt"
  echo "PREFLIGHT_KB=SKIP (profile does not require a sibling knowledge base)"
fi

if [ "$RESOLVER" = web ]; then
  bash "$ARCHON_LAYER/setup/resolve-web-params.sh" \
    "$PROJECT_ROOT" "${ARGUMENTS-}" "$ARTIFACTS_DIR" "$APIBASE" "$WEBBASE"
else
  ALLOW_ARGS=()
  if [ -n "$ALLOWED_REPOS" ]; then
    ALLOW_ARGS=(--allow "$ALLOWED_REPOS")
  fi
  export ARCHON_REPO="${ARCHON_REPO:-$DEFAULT_REPO}"
  bash "$ARCHON_LAYER/setup/resolve-params.sh" \
    "$PROJECT_ROOT" "${ARGUMENTS-}" "$ARTIFACTS_DIR" "$APIBASE" "$WEBBASE" \
    "${ALLOW_ARGS[@]}"
fi
eval "$(bash "$ARCHON_LAYER/setup/params-env.sh" "$ARTIFACTS_DIR/params.json")"

if [ "$PROFILE_LAYOUT" = single ]; then
  test -d "$PROJECT_ROOT/.git" || { echo "PREFLIGHT=FAIL project root is not a git checkout"; exit 1; }
elif [ "$CHECKOUTS" = 1 ] && [ -n "${REQUIRED_CHECKOUTS-}" ]; then
  IFS=',' read -r -a _repos <<< "$REQUIRED_CHECKOUTS"
  for r in "${_repos[@]}"; do
    test -d "$PROJECT_ROOT/$r/.git" || { echo "PREFLIGHT=FAIL $r repo missing"; exit 1; }
  done
elif [ "$RESOLVER" = web ]; then
  WEB_DIR="$PROJECT_ROOT/${WEB_REPO:-web-app}"
  if [ "$PROFILE_LAYOUT" = single ]; then
    WEB_DIR="$PROJECT_ROOT"
  fi
  test -d "$WEB_DIR/.git" || { echo "PREFLIGHT=FAIL web repo missing"; exit 1; }
else
  test -d "$PROJECT_ROOT/$REPO/.git" || { echo "PREFLIGHT=FAIL $REPO repo missing"; exit 1; }
fi
if [ -n "$WEBBASE" ] && [ -n "${HAS_SMOKE-}" ]; then
  test -n "${APIPORT-}" && test -n "${WEBPORT-}" || { echo "PREFLIGHT=FAIL params.json carries no smoke ports"; exit 1; }
  echo "PREFLIGHT_PORTS api=$APIPORT web=$WEBPORT (per-run, from bases $APIBASE/$WEBBASE)"
elif [ -n "${HAS_SMOKE-}" ]; then
  test -n "${APIPORT-}" || { echo "PREFLIGHT=FAIL params.json carries no smoke port"; exit 1; }
  echo "PREFLIGHT_PORTS api=$APIPORT (per-run, from base $APIBASE)"
else
  echo "PREFLIGHT_PORTS none ($REPO declares no smoke stack)"
fi
if [ "$RESOLVER" = web ] && [ "${ARCHON_FEATURE_SCOPE-fullstack}" = fullstack ]; then
  APIWT=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('api_worktree') or '')" "$ARTIFACTS_DIR/params.json")
  test -d "$APIWT/.git" || { echo "PREFLIGHT=FAIL api handoff worktree missing"; exit 1; }
fi

python3 "$ARCHON_LAYER/setup/graph_export.py" "$ARCHON_LAYER/workflows/$WORKFLOW_YAML" "$ARTIFACTS_DIR" || exit 1
echo "PREFLIGHT_PROFILE=$PROFILE_ID layout=$PROFILE_LAYOUT"
