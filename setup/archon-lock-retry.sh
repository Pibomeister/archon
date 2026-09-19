#!/usr/bin/env bash
# archon-lock-retry.sh -- run one archon run-state control, retrying SQLITE_BUSY.
#
# archon 0.10.1 opens ~/.archon/archon.db with a hard-coded
# `PRAGMA busy_timeout = 5000` (the only busy_timeout in the binary; no env or
# config knob), but a larger timeout would not help: its SQLite adapter's
# withTransaction() issues a plain deferred `BEGIN`, reads the run row, then
# UPDATEs it. In WAL mode that read->write upgrade returns SQLITE_BUSY at once,
# without calling the busy handler, whenever another process holds the write
# lock or committed since the read. Several running workflows insert
# hook_activity events many times a second, so a control command fails in
# milliseconds ("database is locked"), either before it touched the run (e.g.
# "failed to load prior run state") or after it recorded a gate decision
# ("Rejected but failed to resume workflow ..."). Each attempt is an independent
# draw against that write duty cycle, so many short jittered retries work where
# a few long backoffs do not.
#
# Re-issuing the command is safe only in the first case, so an attempt repeats
# only when ALL of these hold:
#   - the command exited non-zero,
#   - its output contains "database is locked",
#   - the NAMED run's whole row (status, timestamps, metadata) is identical to
#     before the attempt. Other rows are ignored on purpose: concurrent runs of
#     the same lane bump their own last_activity_at.
# An unreadable row counts as changed, so a failed lookup never retries.
#
# A recorded approve/reject/respond whose own resume hit the lock changes the
# row (approval metadata), so the decision is never re-issued. It is continued
# with resume.sh instead, which retries under the same rule -- but only while the
# run is still `paused`. archon prints the same "Approved but failed to resume"
# prefix when the resumed WORKFLOW fails hours later; that run is `failed`, and
# re-running it here would bypass the operator and the watchdog.
#
# For RESUME, a retry also requires that the named run is still the newest
# resumable run of its lane (resume.sh's pre-check, repeated per attempt), since
# every attempt is a fresh `archon workflow resume`.
#
# Usage: archon-lock-retry.sh <LABEL> <run-id> -- <command...>
# Stdout/stderr of the command pass through unchanged. Each retry prints
#   <LABEL>_DB_LOCKED attempt=<n> of=<max> sleep_ms=<ms> run=<8>
# on stdout. Exits with the last command's exit code.
# Env: ARCHON_DB                      (default ~/.archon/archon.db)
#      ARCHON_LOCK_RETRY_ATTEMPTS     total attempts (default 60)
#      ARCHON_LOCK_RETRY_MIN_MS / ARCHON_LOCK_RETRY_MAX_MS
#                                     jittered sleep between attempts (default 500..2000)
#      ARCHON_LOCK_RETRY_DEADLINE_S   no new attempt after this many seconds (default 600)
# Invalid values fall back to the defaults. Only the last 64 KiB of output are
# inspected, so a lock message from early in a long run cannot trigger a retry.
set -uo pipefail

LABEL="$1"
RUN_ID="$2"
shift 2
[ "${1:-}" = "--" ] && shift

DB="${ARCHON_DB:-$HOME/.archon/archon.db}"
int_or() { case "$1" in "" | *[!0-9]*) printf '%s' "$2" ;; *) printf '%s' "$1" ;; esac; }
MAX="$(int_or "${ARCHON_LOCK_RETRY_ATTEMPTS:-}" 60)"
MIN_MS="$(int_or "${ARCHON_LOCK_RETRY_MIN_MS:-}" 500)"
MAX_MS="$(int_or "${ARCHON_LOCK_RETRY_MAX_MS:-}" 2000)"
DEADLINE_S="$(int_or "${ARCHON_LOCK_RETRY_DEADLINE_S:-}" 600)"
[ "$MAX_MS" -ge "$MIN_MS" ] || MAX_MS="$MIN_MS"
CAP="$(mktemp)"
trap 'rm -f "$CAP"' EXIT

named_row() {
  case "$RUN_ID" in
    "" | *[!0-9a-fA-F-]*) printf 'unknown-%s%s' "$RANDOM" "$RANDOM"; return ;;
  esac
  sqlite3 -readonly -cmd '.timeout 5000' "$DB" \
    "SELECT * FROM remote_agent_workflow_runs WHERE id LIKE '${RUN_ID}%' ORDER BY id;" 2>/dev/null \
    || printf 'unreadable-%s%s' "$RANDOM" "$RANDOM"
}

# One scalar from the named run's row; empty when unreadable.
named_scalar() {
  case "$RUN_ID" in "" | *[!0-9a-fA-F-]*) return ;; esac
  sqlite3 -readonly -cmd '.timeout 5000' "$DB" "$1" 2>/dev/null || true
}

# The run `archon workflow resume` would select for the named run's lane
# (same clause as resume.sh). Empty when unreadable.
lane_pick() {
  named_scalar "SELECT o.id FROM remote_agent_workflow_runs n JOIN remote_agent_workflow_runs o
     ON o.workflow_name = n.workflow_name AND o.working_path = n.working_path
     WHERE n.id LIKE '${RUN_ID}%'
       AND (o.status IN ('failed','paused')
            OR (o.status = 'running' AND (o.last_activity_at IS NULL
                OR o.last_activity_at < datetime('now','-1 days'))))
     ORDER BY o.started_at DESC, o.id ASC LIMIT 1;"
}

tail_has() { tail -c 65536 "$CAP" | grep -Eq "$1"; }

attempt=1
while :; do
  before="$(named_row)"
  : >"$CAP"
  # stdout -> stdout, stderr -> stderr, both also appended to $CAP.
  { { "$@" 2>&1 1>&3 3>&- | tee -a "$CAP" >&2; exit "${PIPESTATUS[0]}"; } 3>&1 | tee -a "$CAP"; }
  rc="${PIPESTATUS[0]}"
  if [ "$rc" -eq 0 ] || ! tail_has 'database is locked'; then
    exit "$rc"
  fi
  if [ "$(named_row)" != "$before" ]; then
    if [ "$LABEL" != RESUME ] \
      && tail_has "(Approved|Rejected|Response recorded) but failed to resume workflow '[^']*': .*database is locked" \
      && [ "$(named_scalar "SELECT status FROM remote_agent_workflow_runs WHERE id LIKE '${RUN_ID}%';")" = paused ]; then
      echo "${LABEL}_DB_LOCKED recorded=yes continue=resume.sh run=${RUN_ID:0:8}"
      rm -f "$CAP"
      exec bash "$(dirname "$0")/resume.sh" "$RUN_ID" </dev/null
    fi
    exit "$rc"
  fi
  wait_ms=$((MIN_MS + RANDOM % (MAX_MS - MIN_MS + 1)))
  if [ "$attempt" -ge "$MAX" ] || [ $((SECONDS + wait_ms / 1000)) -ge "$DEADLINE_S" ]; then
    exit "$rc"
  fi
  pick="$(lane_pick)"
  if [ "$LABEL" = RESUME ] && { [ -z "$pick" ] || [ "$pick" != "$(named_scalar "SELECT id FROM remote_agent_workflow_runs WHERE id LIKE '${RUN_ID}%';")" ]; }; then
    echo "RESUME_DB_LOCKED stop=lane-selection-changed run=${RUN_ID:0:8}"
    exit "$rc"
  fi
  echo "${LABEL}_DB_LOCKED attempt=$attempt of=$MAX sleep_ms=$wait_ms run=${RUN_ID:0:8}"
  sleep "$((wait_ms / 1000)).$(printf '%03d' $((wait_ms % 1000)))"
  attempt=$((attempt + 1))
done
