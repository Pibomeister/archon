#!/usr/bin/env bash
# Run-level watchdog for codex lanes (limitation follow-up: maxBudgetUsd is
# inert under codex and per-node timeout never kills a stalled AI node, so a
# codex run's only in-engine brakes are loop caps and the ChatGPT quota).
#
# Usage: codex-watchdog.sh <run-id-prefix> --wall-minutes N [--max-total-tokens M]
#                          [--interval-s S] [--db PATH] [--codex-home DIR]
#
# Polls the run every S seconds (default 30). While the run is `running`:
#   - wall clock past N minutes            -> trip
#   - summed session tokens past M         -> trip (via setup/codex-usage.py)
# On trip: kills the detached archon launcher's whole process group (taking its
# codex children with it), prints `WATCHDOG=TRIPPED reason=... run=...`, exit 2.
# The run lands `failed` and resumes with setup/resume.sh like any typed stop.
# When the run leaves `running` on its own: `WATCHDOG=RUN_<STATUS>`, exit 0.
#
# Test seam: WATCHDOG_KILL_CMD overrides the kill (tests point it at a stub);
# never set it in real use.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
RUN="${1:?usage: codex-watchdog.sh <run-id-prefix> --wall-minutes N [--max-total-tokens M]}"; shift
WALL_MIN=""; MAX_TOK=""; INTERVAL=30
DB="$HOME/.archon/archon.db"
CHOME="${CODEX_HOME:-$HOME/.archon/codex-home}"
while [ $# -gt 0 ]; do
  case "$1" in
    --wall-minutes) WALL_MIN="$2"; shift 2 ;;
    --max-total-tokens) MAX_TOK="$2"; shift 2 ;;
    --interval-s) INTERVAL="$2"; shift 2 ;;
    --db) DB="$2"; shift 2 ;;
    --codex-home) CHOME="$2"; shift 2 ;;
    *) echo "WATCHDOG=FAIL unknown arg $1"; exit 1 ;;
  esac
done
[ -n "$WALL_MIN" ] || { echo "WATCHDOG=FAIL --wall-minutes is required"; exit 1; }

row() { sqlite3 "$DB" "SELECT status || '|' || id || '|' || user_message FROM remote_agent_workflow_runs WHERE id LIKE '$RUN%' ORDER BY started_at DESC LIMIT 1"; }
R="$(row)"
[ -n "$R" ] || { echo "WATCHDOG=FAIL no run matching $RUN"; exit 1; }
RUN_ID="$(printf '%s' "$R" | cut -d'|' -f2)"
MSG="$(printf '%s' "$R" | cut -d'|' -f3-)"
START_EPOCH="$(date +%s)"
DEADLINE=$(( START_EPOCH + WALL_MIN * 60 ))

tokens_now() { # prints a number, or ERR when accounting is unavailable
  python3 "$HERE/codex-usage.py" "$RUN_ID" --db "$DB" --codex-home "$CHOME" --json 2>/dev/null \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['total_tokens'])" 2>/dev/null || echo ERR
}

kill_run() { # kill the detached launcher's process group (codex children included)
  if [ -n "${WATCHDOG_KILL_CMD:-}" ]; then $WATCHDOG_KILL_CMD "$RUN_ID"; return; fi
  local pid
  pid="$(pgrep -f "archon workflow run .*$(basename "$MSG")" | head -1)"
  if [ -n "$pid" ]; then
    local pgid
    pgid="$(ps -o pgid= -p "$pid" | tr -d ' ')"
    [ -n "$pgid" ] && kill -TERM -- "-$pgid" 2>/dev/null
    sleep 5
    [ -n "$pgid" ] && kill -KILL -- "-$pgid" 2>/dev/null
  else
    echo "WATCHDOG=WARN launcher process not found (already gone?)"
  fi
}

while true; do
  S="$(row | cut -d'|' -f1)"
  if [ "$S" != "running" ]; then
    echo "WATCHDOG=RUN_$(printf '%s' "$S" | tr '[:lower:]' '[:upper:]')"
    exit 0
  fi
  NOW="$(date +%s)"
  if [ "$NOW" -ge "$DEADLINE" ]; then
    kill_run
    echo "WATCHDOG=TRIPPED reason=wall run=${RUN_ID:0:8} elapsed_s=$(( NOW - START_EPOCH )) budget_min=$WALL_MIN"
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
      echo "WATCHDOG=TRIPPED reason=tokens run=${RUN_ID:0:8} tokens=$T cap=$MAX_TOK"
      exit 2
    fi
  fi
  sleep "$INTERVAL"
done
