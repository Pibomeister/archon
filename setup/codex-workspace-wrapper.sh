#!/usr/bin/env bash
# Archon v0.8.0's Codex adapter forces danger-full-access and does not expose
# the provider sandbox field. The guarded lite launcher installs this wrapper
# outside the workspace and points CODEX_BIN_PATH at it, making workspace-write
# an argv-level invariant for every spawned Codex node.
set -euo pipefail
REAL="${CODEX_REAL_BIN:?CODEX_REAL_BIN is required}"
ROOT="${CODEX_WORKSPACE_ROOT:?CODEX_WORKSPACE_ROOT is required}"
ARTIFACTS_BASE="${CODEX_ARTIFACTS_BASE:?CODEX_ARTIFACTS_BASE is required}"
[ -x "$REAL" ] || { echo "CODEX_WRAPPER=FAIL real binary is not executable: $REAL" >&2; exit 126; }
[ -d "$ROOT/api/.git" ] || { echo "CODEX_WRAPPER=FAIL api repo missing under $ROOT" >&2; exit 2; }
[ -d "$ROOT/web-app/.git" ] || { echo "CODEX_WRAPPER=FAIL web repo missing under $ROOT" >&2; exit 2; }

if [ "${1:-}" = "exec" ]; then
  shift
  PROMPT="$(cat)"
  ARTIFACTS_DIR="$(printf '%s' "$PROMPT" | python3 -c '
import re, sys
base = sys.argv[1].rstrip("/")
text = sys.stdin.read()
paths = sorted(set(re.findall(re.escape(base) + r"/[0-9a-f]{32}", text)))
if len(paths) > 1:
    print("CODEX_WRAPPER=FAIL prompt names multiple run artifact roots", file=sys.stderr)
    raise SystemExit(2)
print(paths[0] if paths else "")
' "$ARTIFACTS_BASE")"
  args=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --sandbox)
        [ $# -ge 2 ] || { echo "CODEX_WRAPPER=FAIL --sandbox missing value" >&2; exit 2; }
        shift 2 # Archon v0.8.0 forces danger-full-access; replace, never retain.
        ;;
      --cd|--add-dir)
        [ $# -ge 2 ] || { echo "CODEX_WRAPPER=FAIL $1 missing value" >&2; exit 2; }
        shift 2 # Replace the broad Goodword root with the two writable repos.
        ;;
      --config)
        [ $# -ge 2 ] || { echo "CODEX_WRAPPER=FAIL --config missing value" >&2; exit 2; }
        case "$2" in
          sandbox_workspace_write.network_access=*) shift 2 ;;
          *) args+=("$1" "$2"); shift 2 ;;
        esac
        ;;
      --dangerously-bypass-approvals-and-sandbox)
        echo "CODEX_WRAPPER=FAIL bypass flag from adapter" >&2
        exit 2
        ;;
      *) args+=("$1"); shift ;;
    esac
  done
  forced=(exec --sandbox workspace-write --cd "$ROOT/api" --add-dir "$ROOT/web-app"
    --config sandbox_workspace_write.network_access=false)
  [ -z "$ARTIFACTS_DIR" ] || forced+=(--add-dir "$ARTIFACTS_DIR")
  exec "$REAL" "${forced[@]}" "${args[@]}" <<< "$PROMPT"
fi

exec "$REAL" "$@"
