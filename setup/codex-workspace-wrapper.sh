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

if [ "${1:-}" = "exec" ]; then
  shift
  PROMPT="$(cat)"
  ARTIFACTS_DIR="$(printf '%s' "$PROMPT" | python3 -c '
import os, re, sys
base = sys.argv[1].rstrip("/")
text = sys.stdin.read()
run_id = r"(?:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|[0-9a-f]{32})"
paths = sorted(set(re.findall(re.escape(base) + "/" + run_id + r"(?![0-9a-f-])", text)))
bound = os.environ.get("CODEX_RUN_ARTIFACTS")
if bound:
    paths = sorted(set(paths + [bound]))
if len(paths) > 1:
    print("CODEX_WRAPPER=FAIL prompt names multiple run artifact roots", file=sys.stderr)
    raise SystemExit(2)
print(paths[0] if paths else "")
' "$ARTIFACTS_BASE")"
  args=()
  WORKTREE="$PWD"
  SKIP_GIT_CHECK=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --sandbox|-s)
        [ $# -ge 2 ] || { echo "CODEX_WRAPPER=FAIL --sandbox missing value" >&2; exit 2; }
        shift 2 # Archon v0.8.0 forces danger-full-access; replace, never retain.
        ;;
      --sandbox=*) shift ;;
      --cd|-C)
        [ $# -ge 2 ] || { echo "CODEX_WRAPPER=FAIL $1 missing value" >&2; exit 2; }
        WORKTREE="$2"
        shift 2
        ;;
      --cd=*) WORKTREE="${1#*=}"; shift ;;
      --add-dir)
        [ $# -ge 2 ] || { echo "CODEX_WRAPPER=FAIL $1 missing value" >&2; exit 2; }
        shift 2
        ;;
      --add-dir=*) shift ;;
      --skip-git-repo-check) SKIP_GIT_CHECK=1; shift ;;
      --config|-c)
        [ $# -ge 2 ] || { echo "CODEX_WRAPPER=FAIL --config missing value" >&2; exit 2; }
        case "$2" in
          sandbox_*=*|sandbox_workspace_write.*|default_permissions=*|permissions=*|permissions.*) shift 2 ;;
          *) args+=("$1" "$2"); shift 2 ;;
        esac
        ;;
      --config=*|-c?*)
        echo "CODEX_WRAPPER=FAIL attached config override is unsupported" >&2
        exit 2
        ;;
      --worktree|--ephemeral|--profile|-p|--profile=*)
        echo "CODEX_WRAPPER=FAIL option conflicts with pinned workspace or session accounting: $1" >&2
        exit 2
        ;;
      --dangerously-bypass-approvals-and-sandbox)
        echo "CODEX_WRAPPER=FAIL bypass flag from adapter" >&2
        exit 2
        ;;
      *) args+=("$1"); shift ;;
    esac
  done
  if [ "${ARCHON_FEATURE_SCOPE:-}" = repositories ]; then
    normalized_args=()
    stripped_resume=0
    value_for=""
    i=0
    while [ "$i" -lt "${#args[@]}" ]; do
      token="${args[$i]}"
      if [ -n "$value_for" ]; then
        if [ "$value_for" = "--enable" ] && [ "$token" = "multi_agent" ]; then
          echo "CODEX_WRAPPER=FAIL native multi-agent cannot be enabled inside a guarded repository chain" >&2
          exit 2
        fi
        if { [ "$value_for" = "--config" ] || [ "$value_for" = "-c" ]; } && [[ "$token" =~ ^(features\.)?multi_agent[[:space:]]*= ]]; then
          echo "CODEX_WRAPPER=FAIL native multi-agent config cannot be changed inside a guarded repository chain" >&2
          exit 2
        fi
        normalized_args+=("$token")
        value_for=""
        i=$((i + 1))
        continue
      fi
      case "$token" in
        --model|-m|--config|-c|--output-schema|--color|--approval-policy|--image|-i|--enable|--disable|--thread-source|--output-last-message|-o|--local-provider)
          normalized_args+=("$token")
          value_for="$token"
          i=$((i + 1))
          ;;
        --model=*|--output-schema=*|--color=*|--approval-policy=*)
          normalized_args+=("$token")
          i=$((i + 1))
          ;;
        resume)
          if [ "$stripped_resume" -ne 0 ]; then
            echo "CODEX_WRAPPER=FAIL multiple Codex session selectors are unsupported" >&2
            exit 2
          fi
          next=$((i + 1))
          if [ "$next" -ge "${#args[@]}" ]; then
            echo "CODEX_WRAPPER=FAIL Codex resume selector missing session id" >&2
            exit 2
          fi
          selector="${args[$next]}"
          if [[ ! "$selector" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; then
            echo "CODEX_WRAPPER=FAIL unsupported Codex resume session id" >&2
            exit 2
          fi
          stripped_resume=1
          i=$((i + 2))
          ;;
        fork|--resume|--resume=*|-r|resume=*|--last|--all)
          echo "CODEX_WRAPPER=FAIL unsupported Codex session selector: $token" >&2
          exit 2
          ;;
        --enable=multi_agent)
          echo "CODEX_WRAPPER=FAIL native multi-agent cannot be enabled inside a guarded repository chain" >&2
          exit 2
          ;;
        *)
          if [[ "$token" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; then
            echo "CODEX_WRAPPER=FAIL unsupported bare Codex session selector" >&2
            exit 2
          fi
          normalized_args+=("$token")
          i=$((i + 1))
          ;;
      esac
    done
    args=("${normalized_args[@]}")
  fi
  PINNED_MODEL="${ARCHON_CODEX_PINNED_MODEL:-}"
  PINNED_REASONING_EFFORT="${ARCHON_CODEX_PINNED_REASONING_EFFORT:-}"
  if [ "${ARCHON_FEATURE_SCOPE:-}" = repositories ]; then
    value_for=""
    for token in "${args[@]}"; do
      if [ -n "$value_for" ]; then
        case "$value_for" in
          --model|-m)
            if [ -n "$PINNED_MODEL" ] && [ "$token" != "$PINNED_MODEL" ]; then
              echo "CODEX_WRAPPER=FAIL repository model override $token does not match trusted chain model $PINNED_MODEL" >&2
              exit 2
            fi
            PINNED_MODEL="$token"
            ;;
          --config|-c)
            case "$token" in
              model_reasoning_effort=*)
                requested_effort="${token#*=}"
                requested_effort="${requested_effort%\"}"
                requested_effort="${requested_effort#\"}"
                if [ -n "$PINNED_REASONING_EFFORT" ] && [ "$requested_effort" != "$PINNED_REASONING_EFFORT" ]; then
                  echo "CODEX_WRAPPER=FAIL repository reasoning effort override $requested_effort does not match trusted chain effort $PINNED_REASONING_EFFORT" >&2
                  exit 2
                fi
                PINNED_REASONING_EFFORT="$requested_effort"
                ;;
            esac
            ;;
        esac
        value_for=""
        continue
      fi
      case "$token" in
        --model|-m|--config|-c) value_for="$token" ;;
        --model=*)
          requested_model="${token#*=}"
          if [ -n "$PINNED_MODEL" ] && [ "$requested_model" != "$PINNED_MODEL" ]; then
            echo "CODEX_WRAPPER=FAIL repository model override $requested_model does not match trusted chain model $PINNED_MODEL" >&2
            exit 2
          fi
          PINNED_MODEL="$requested_model"
          ;;
      esac
    done
    export ARCHON_CODEX_PINNED_MODEL="$PINNED_MODEL"
    export ARCHON_CODEX_PINNED_REASONING_EFFORT="$PINNED_REASONING_EFFORT"
  fi
  WORKTREE="$(python3 - "$ROOT" "$WORKTREE" "$ARTIFACTS_DIR" "$ARTIFACTS_BASE" <<'PY_WORKTREE'
import json
import hashlib
import hmac
import os
import re
import stat
import sys
from pathlib import Path

def private_feature_worktree(artifacts):
    chain_id = os.environ.get("ARCHON_FEATURE_CHAIN_ID", "")
    if not re.fullmatch(r"[0-9a-f]{24,64}", chain_id):
        raise ValueError("invalid repository chain identity")
    directory = Path(os.environ["ARCHON_CONTROL_DIR"]) / "feature-chains-v2"
    path = directory / (chain_id + ".json")
    for item in (directory.parent, directory, path):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("repository chain authority is not private")
    state = json.loads(path.read_text())
    body = {k: v for k, v in state.items() if k not in {"state_mac", "approval_mac", "receipt_mac"}}
    expected = hmac.new(state["chain_secret"].encode(), json.dumps(body, sort_keys=True,
                        separators=(",", ":")).encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, state.get("state_mac", "")):
        raise ValueError("repository chain authority MAC mismatch")
    if state.get("schema_version") != 2 or state.get("logical_chain_id") != chain_id:
        raise ValueError("repository chain authority schema mismatch")
    current = state["current_run"]
    if artifacts is None or current["run_id"] != artifacts.name:
        raise ValueError("repository chain run does not match artifacts")
    phase = current["phase"]
    if phase in {"planning", "integration"}:
        return artifacts
    if phase not in {"implement", "verify"} or not state.get("approval"):
        raise ValueError("repository execution has no joint approval")
    repo = current["repo"]
    selected = Path(state["worktrees"][repo]["worktree"]).resolve(strict=True)
    if str(selected) not in current["write_roots"]:
        raise ValueError("repository execution worktree authority mismatch")
    return selected

try:
    root = Path(sys.argv[1]).resolve(strict=True)
    selected = sys.argv[2]
    artifacts = None
    if sys.argv[3]:
        artifacts = Path(sys.argv[3]).resolve(strict=True)
        if artifacts.parent != Path(sys.argv[4]).resolve(strict=True):
            raise ValueError("run artifacts must be directly inside the artifacts base")
        if os.environ.get("ARCHON_FEATURE_SCOPE") != "repositories":
            params = json.loads((artifacts / "params.json").read_text())
            selected = params["worktree"]
            if not isinstance(selected, str) or not Path(selected).is_absolute():
                raise ValueError("recorded worktree must be absolute")
    if os.environ.get("ARCHON_FEATURE_SCOPE") == "repositories":
        selected = private_feature_worktree(artifacts)
        if selected == artifacts:
            print(selected)
            raise SystemExit(0)
        selected = str(selected)
    if not isinstance(selected, str) or not selected or "\n" in selected:
        raise ValueError("invalid worktree")
    worktree = Path(selected).resolve()
    if worktree == root or not worktree.is_relative_to(root):
        raise ValueError("worktree must be inside the workspace, not its root")
    if not worktree.exists() and artifacts is not None:
        if (artifacts / "bootstrap-head.txt").exists():
            raise ValueError("selected worktree missing after bootstrap")
        # Research nodes precede bootstrap and write only run evidence.
        worktree = artifacts
    elif not worktree.is_dir() or not (worktree / ".git").exists():
        raise ValueError("selected worktree has no Git metadata")
    print(worktree)
except (OSError, ValueError, KeyError, TypeError) as exc:
    print("CODEX_WRAPPER=FAIL " + str(exc), file=sys.stderr)
    raise SystemExit(2)
PY_WORKTREE
)"
  PERMISSIONS="$(python3 - "$ARTIFACTS_DIR" <<'PY_PERMISSIONS'
import json
import os
import sys
from pathlib import Path

home = Path.home()
control = Path(os.environ.get("ARCHON_CONTROL_DIR", home / ".archon/control/codex-lite"))
denied = [control, home / ".archon/logs", home / ".archon/archon.db",
          home / ".archon/archon.db-wal", home / ".archon/archon.db-shm"]
rules = ",".join(json.dumps(str(path.resolve())) + '= "deny"' for path in denied)
if os.environ.get("ARCHON_FEATURE_SCOPE") == "repositories":
    artifacts = Path(sys.argv[1]).resolve()
    frozen = ["params.json", "worktrees.json", "bootstrap-head.txt", "feature-chain-request.json",
              "prior-planning-evidence.json", "budget-forecast.json", "AGENTS.md"]
    if os.environ.get("ARCHON_FEATURE_PHASE") != "planning":
        frozen += ["joint-plan.json", "plan.md", "files-allowlist.json", "verify.json",
                   "candidate-inputs.json", "candidate-revisions.json", "premises.json",
                   "reader-audit.json", "web-premises.json", "web-reader-audit.json",
                   "browser-evidence.json", "browser-evidence.sha256", "smoke-probe.json"]
    rules += "," + ",".join(json.dumps(str(artifacts / name)) + '= "read"' for name in frozen)
print('permissions={archon-worker={extends=":workspace",filesystem={' + rules +
      '},network={enabled=false}}}')
PY_PERMISSIONS
)"
  forced=(exec --cd "$WORKTREE" --config 'default_permissions="archon-worker"'
    --config "$PERMISSIONS")
  if [ "${ARCHON_FEATURE_SCOPE:-}" = repositories ]; then
    forced+=(--disable multi_agent)
  fi
  if [ "$SKIP_GIT_CHECK" -eq 1 ] || [ ! -e "$WORKTREE/.git" ]; then
    forced+=(--skip-git-repo-check)
  fi
  [ -z "$ARTIFACTS_DIR" ] || forced+=(--add-dir "$ARTIFACTS_DIR")
  # Codex launches stdio MCP servers with a core-only environment (HOME, PATH,
  # SHELL, TERM, TMPDIR, USER, LANG, LOGNAME), so the gitnexus dispatcher never
  # inherits its pinned index/commit and exits before the handshake. Forward the
  # pin as an argv-level MCP env override, keeping the chain binding whenever
  # this node runs inside a bugfix chain.
  if [ -n "${ARCHON_GITNEXUS_INDEX:-}" ] && [ -n "${ARCHON_GITNEXUS_COMMIT:-}" ]; then
    forced+=(--config "mcp_servers.gitnexus.env.ARCHON_GITNEXUS_INDEX=\"$ARCHON_GITNEXUS_INDEX\""
      --config "mcp_servers.gitnexus.env.ARCHON_GITNEXUS_COMMIT=\"$ARCHON_GITNEXUS_COMMIT\"")
    if [ -n "${ARCHON_BUGFIX_CHAIN_ID:-}" ] && [ -n "${ARCHON_BUGFIX_CHAIN_STATE:-}" ]; then
      forced+=(--config "mcp_servers.gitnexus.env.ARCHON_BUGFIX_CHAIN_ID=\"$ARCHON_BUGFIX_CHAIN_ID\""
        --config "mcp_servers.gitnexus.env.ARCHON_BUGFIX_CHAIN_STATE=\"$ARCHON_BUGFIX_CHAIN_STATE\"")
    fi
  fi
  if [ "${ARCHON_FEATURE_SCOPE:-}" = repositories ]; then
    : "${ARCHON_FEATURE_BUDGET_SCRIPT:?repository chains require exact session recording}"
    exec python3 -c '
import json, os, subprocess, sys

recorder, run_id, binary = sys.argv[1:4]
child = subprocess.Popen([binary, *sys.argv[4:]], stdin=sys.stdin, stdout=subprocess.PIPE,
                         text=True, encoding="utf-8", errors="replace", bufsize=1)
try:
    for line in child.stdout:
        try:
            event = json.loads(line)
        except ValueError:
            event = None
        if isinstance(event, dict) and event.get("type") == "thread.started":
            session_id = event.get("thread_id")
            if not isinstance(session_id, str) or not session_id:
                raise ValueError("Codex thread.started has no session identity")
            registered = subprocess.run([
                sys.executable, recorder, "--control-dir", os.environ["ARCHON_CONTROL_DIR"],
                "--codex-home", os.environ["CODEX_HOME"], "bind-session",
                "--chain-id", os.environ["ARCHON_FEATURE_CHAIN_ID"], "--run-id", run_id,
                "--session-id", session_id,
            ], capture_output=True, text=True, timeout=30)
            if registered.returncode:
                raise ValueError("exact Codex session registration failed: " + registered.stderr.strip())
        sys.stdout.write(line)
        sys.stdout.flush()
    raise SystemExit(child.wait())
except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
    child.terminate()
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
    print("CODEX_WRAPPER=FAIL " + str(exc), file=sys.stderr)
    raise SystemExit(2)
' "$ARCHON_FEATURE_BUDGET_SCRIPT" "$(basename "$ARTIFACTS_DIR")" "$REAL" "${forced[@]}" "${args[@]}" <<< "$PROMPT"
  fi
  exec "$REAL" "${forced[@]}" "${args[@]}" <<< "$PROMPT"
fi

exec "$REAL" "$@"
