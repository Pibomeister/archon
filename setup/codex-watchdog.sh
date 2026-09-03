#!/usr/bin/env bash
# Run-level watchdog for codex lanes (limitation follow-up: maxBudgetUsd is
# inert under codex and per-node timeout never kills a stalled AI node, so a
# codex run's only in-engine brakes are loop caps and the ChatGPT quota).
#
# Usage: codex-watchdog.sh <run-id-or-unique-prefix> --wall-minutes N
#                          [--max-total-tokens M] [--launcher-pgid N]
#                          [--launcher-fingerprint STRING]
#                          [--interval-s S] [--await-running --arm-file PATH]
#                          [--db PATH] [--codex-home DIR] [--chain-id ID]
#
# Polls the run every S seconds (default 30). While the run is `running`:
#   - wall clock past N minutes            -> trip
#   - summed session tokens past M         -> trip (via setup/codex-usage.py)
# On trip: kills the detached archon launcher's whole process group (taking its
# codex children with it), prints `WATCHDOG=TRIPPED reason=... run=...`, exit 2.
# If SIGTERM does not move the run out of `running`, the watchdog abandons that
# orphan so its worktree lock cannot survive the budget stop.
# When the run leaves `running` on its own: `WATCHDOG=RUN_<STATUS>`, exit 0.
#
# Test seam: WATCHDOG_KILL_CMD overrides the kill (tests point it at a stub);
# never set it in real use.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
RUN="${1:?usage: codex-watchdog.sh <run-id-prefix> --wall-minutes N [--max-total-tokens M]}"; shift
WALL_MIN=""; MAX_TOK=""; INTERVAL=30; LAUNCHER_PGID=""
LAUNCHER_FINGERPRINT=""
AWAIT_RUNNING=0; ARM_FILE=""
DB="$HOME/.archon/archon.db"
CHOME="${CODEX_HOME:-$HOME/.archon/codex-home}"
CHAIN_ID=""
while [ $# -gt 0 ]; do
  case "$1" in
    --wall-minutes) WALL_MIN="$2"; shift 2 ;;
    --max-total-tokens) MAX_TOK="$2"; shift 2 ;;
    --launcher-pgid) LAUNCHER_PGID="$2"; shift 2 ;;
    --launcher-fingerprint) LAUNCHER_FINGERPRINT="$2"; shift 2 ;;
    --await-running) AWAIT_RUNNING=1; shift ;;
    --arm-file) ARM_FILE="$2"; shift 2 ;;
    --interval-s) INTERVAL="$2"; shift 2 ;;
    --db) DB="$2"; shift 2 ;;
    --codex-home) CHOME="$2"; shift 2 ;;
    --chain-id) CHAIN_ID="$2"; shift 2 ;;
    *) echo "WATCHDOG=FAIL unknown arg $1"; exit 1 ;;
  esac
done
[ -n "$WALL_MIN" ] || { echo "WATCHDOG=FAIL --wall-minutes is required"; exit 1; }
case "$WALL_MIN" in ''|*[!0-9]*) echo "WATCHDOG=FAIL --wall-minutes must be a non-negative integer"; exit 1 ;; esac
case "$INTERVAL" in ''|*[!0-9]*) echo "WATCHDOG=FAIL --interval-s must be a non-negative integer"; exit 1 ;; esac
if [ -n "$MAX_TOK" ]; then case "$MAX_TOK" in *[!0-9]*|'') echo "WATCHDOG=FAIL --max-total-tokens must be an integer"; exit 1 ;; esac; fi
if [ -n "$LAUNCHER_PGID" ]; then case "$LAUNCHER_PGID" in *[!0-9]*|'') echo "WATCHDOG=FAIL --launcher-pgid must be an integer"; exit 1 ;; esac; fi
if [ -z "${WATCHDOG_KILL_CMD:-}" ] && { [ -z "$LAUNCHER_PGID" ] || [ -z "$LAUNCHER_FINGERPRINT" ]; }; then
  echo "WATCHDOG=FAIL --launcher-pgid and --launcher-fingerprint are required outside tests (use codex-lite-run.py)"
  exit 1
fi
if [ "$AWAIT_RUNNING" -eq 1 ] && { [ -z "$LAUNCHER_PGID" ] || [ -z "$LAUNCHER_FINGERPRINT" ] || [ -z "$ARM_FILE" ]; }; then
  echo "WATCHDOG=FAIL --await-running requires --launcher-pgid, --launcher-fingerprint, and --arm-file"
  exit 1
fi

R="$(python3 - "$DB" "$RUN" <<'PY'
import re, sqlite3, sys
db, prefix = sys.argv[1:]
if not re.fullmatch(r"[0-9a-fA-F]{8,32}", prefix):
    print(f"WATCHDOG=FAIL bad-id-format [{prefix}]")
    raise SystemExit(1)
con = sqlite3.connect(db)
rows = con.execute(
    "SELECT status, id FROM remote_agent_workflow_runs WHERE id LIKE ? ORDER BY started_at DESC LIMIT 3",
    (prefix.lower() + "%",),
).fetchall()
con.close()
if not rows:
    print(f"WATCHDOG=FAIL no run matching {prefix}")
    raise SystemExit(1)
if len(rows) != 1:
    print(f"WATCHDOG=FAIL ambiguous prefix={prefix} matches={len(rows)}")
    raise SystemExit(1)
print(f"{rows[0][0]}|{rows[0][1]}")
PY
)" || { printf '%s\n' "$R"; exit 1; }
RUN_ID="$(printf '%s' "$R" | cut -d'|' -f2)"
START_EPOCH="$(date +%s)"
DEADLINE=$(( START_EPOCH + WALL_MIN * 60 ))

status_now() {
  python3 - "$DB" "$RUN_ID" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
row = con.execute("SELECT status FROM remote_agent_workflow_runs WHERE id = ?", (sys.argv[2],)).fetchone()
con.close()
print(row[0] if row else "MISSING")
PY
}

tokens_now() { # prints a number, or ERR when accounting is unavailable
  python3 "$HERE/codex-usage.py" "$RUN_ID" --db "$DB" --codex-home "$CHOME" --json 2>/dev/null \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['total_tokens'])" 2>/dev/null || echo ERR
}

current_fingerprint() {
  ps -o lstart= -o command= -p "$LAUNCHER_PGID" 2>/dev/null | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'
}

launcher_alive() { # supervisor is the stable process-group leader (PID == PGID)
  [ -n "$LAUNCHER_PGID" ] && kill -0 "$LAUNCHER_PGID" 2>/dev/null || return 1
  [ -z "$LAUNCHER_FINGERPRINT" ] || [ "$(current_fingerprint)" = "$LAUNCHER_FINGERPRINT" ]
}

kill_run() { # kill the detached launcher's process group (codex children included)
  if [ -n "${WATCHDOG_KILL_CMD:-}" ]; then "$WATCHDOG_KILL_CMD" "$RUN_ID"; KILL_RESULT="stubbed"; return; fi
  if ! launcher_alive; then KILL_RESULT="refused-fingerprint"; return; fi
  kill -TERM -- "-$LAUNCHER_PGID" 2>/dev/null || true
  sleep 5
  if launcher_alive; then
    kill -KILL -- "-$LAUNCHER_PGID" 2>/dev/null || true
    KILL_RESULT="term+kill"
  else
    KILL_RESULT="term-exited"
  fi
}

cleanup_orphan() {
  CLEANUP="$(status_now)"
  # Test kill stubs deliberately do not own a real Archon process or DB.
  [ -z "${WATCHDOG_KILL_CMD:-}" ] || return 0
  sleep 1
  CLEANUP="$(status_now)"
  if [ "$CLEANUP" = "running" ]; then
    if DISABLE_OMC=1 ARCHON_DB="$DB" "${ARCHON_BIN:-archon}" workflow abandon "$RUN_ID" --json \
      >/dev/null 2>&1; then
      CLEANUP="$(status_now)"
    else
      CLEANUP="abandon-failed:running"
    fi
  fi
}

arm_watchdog() {
  mkdir -p "$(dirname "$ARM_FILE")"
  tmp="${ARM_FILE}.tmp.$$"
  printf 'run=%s\nlauncher_pgid=%s\narmed_at=%s\n' \
    "$RUN_ID" "$LAUNCHER_PGID" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$tmp"
  mv "$tmp" "$ARM_FILE"
  echo "WATCHDOG=ARMED run=${RUN_ID:0:8} chain=${CHAIN_ID:-none} pgid=$LAUNCHER_PGID"
}

ARMED=0
KILL_RESULT="not-needed"

while true; do
  S="$(status_now)"
  if [ "$S" = "MISSING" ]; then echo "WATCHDOG=FAIL run disappeared id=$RUN_ID chain=${CHAIN_ID:-none}"; exit 1; fi
  if [ "$AWAIT_RUNNING" -eq 1 ] && [ "$ARMED" -eq 0 ]; then
    if [ "$S" = "running" ]; then
      if ! launcher_alive; then
        echo "WATCHDOG=FAIL launcher exited before arming run=${RUN_ID:0:8} status=$S"
        exit 1
      fi
      arm_watchdog
      ARMED=1
    elif launcher_alive; then
      # approve/reject/resume starts from paused/failed. Do not treat that
      # cached pre-command status as a completed watchdog run.
      sleep 0.1
      continue
    else
      echo "WATCHDOG=FAIL launcher exited before running run=${RUN_ID:0:8} status=$S"
      exit 1
    fi
  fi
  if [ "$S" != "running" ]; then
    echo "WATCHDOG=RUN_$(printf '%s' "$S" | tr '[:lower:]' '[:upper:]') chain=${CHAIN_ID:-none}"
    exit 0
  fi
  if [ "$ARMED" -eq 1 ] && ! launcher_alive; then
    cleanup_orphan
    echo "WATCHDOG=TRIPPED reason=launcher-exited run=${RUN_ID:0:8} chain=${CHAIN_ID:-none} cleanup=$CLEANUP"
    exit 2
  fi
  NOW="$(date +%s)"
  if [ "$NOW" -ge "$DEADLINE" ]; then
    kill_run
    cleanup_orphan
    echo "WATCHDOG=TRIPPED reason=wall run=${RUN_ID:0:8} chain=${CHAIN_ID:-none} elapsed_s=$(( NOW - START_EPOCH )) budget_min=$WALL_MIN kill=$KILL_RESULT cleanup=$CLEANUP"
    exit 2
  fi
  if [ -n "$MAX_TOK" ]; then
    T="$(tokens_now)"
    if [ "$T" = "ERR" ]; then
      # Accounting failure must not silently disable the cap (a gate that
      # scanned nothing) nor kill a healthy run: warn once, keep the wall cap.
      if [ -z "${WARNED_TOKENS:-}" ]; then
        echo "WATCHDOG=WARN token accounting unavailable - only the wall budget is enforced"
        WARNED_TOKENS=1
      fi
    elif [ "$T" -ge "$MAX_TOK" ] 2>/dev/null; then
      kill_run
      cleanup_orphan
      echo "WATCHDOG=TRIPPED reason=tokens run=${RUN_ID:0:8} tokens=$T cap=$MAX_TOK chain=${CHAIN_ID:-none} kill=$KILL_RESULT cleanup=$CLEANUP"
      exit 2
    fi
  fi
  sleep "$INTERVAL"
done
