#!/usr/bin/env bash
# Host mutex for the e2e docker stack (api/local-env-compose.e2e.yaml).
#
# Usage: e2e-mutex.sh acquire <owner-artifacts-dir>
#        e2e-mutex.sh wait    <owner-artifacts-dir>
#        e2e-mutex.sh release <owner-artifacts-dir>
#        e2e-mutex.sh run     <owner-artifacts-dir> -- <command...>
#
# The compose file pins a project name (goodword-e2e), container names
# (postgres-db-e2e, dynamodb-local-e2e), named volumes and host ports 54322/8001.
# `up -d --wait` is idempotent, so a second run does NOT error: it attaches to
# the SAME Postgres and then runs its own migrations and seed over the first
# run's rows. And the api integration suite's jest globalSetup runs
# `docker-compose -f local-env-compose.e2e.yaml down -v` before its own `up`, so
# any `bun run test:integration` destroys whatever stack another run is using.
# Per-run isolation would mean editing that compose file, whose other consumer is
# the api integration suite -- so this serializes the phase LOUDLY instead.
#
# The lock is an atomic mkdir, holding `owner` (the artifacts dir) and, for the
# wait/run ops, `pid` (the process that holds the stack while it runs).
#
# acquire: NON-blocking. The bugfix smoke stack outlives its node and stays up
#   across a human approval gate that can take hours, so blocking there would
#   hang a run behind a person. A collision is a typed stop naming the owner.
# wait: BOUNDED blocking acquire for automated phases (joint integration, a
#   wrapped test:integration), where two chains overlapping is normal. Polls with
#   jitter; prints E2E_MUTEX=WAITING at most once a minute and
#   E2E_MUTEX=FAIL timeout after ARCHON_E2E_WAIT_SECONDS (default 1800).
# run: wait, run the command, release on every exit path (only when this call
#   took the lock; a re-entrant HELD leaves the outer holder's lock alone).
#
# Stale-owner recovery (acquire and wait): a lock is reclaimed only when its
# owner's run (the artifacts dir basename) is completed/failed/cancelled in
# archon.db AND its recorded pid, if any, is gone. An owner archon.db does not
# know, an unreadable db, or a live pid is never stolen.
set -uo pipefail

OP="${1:?usage: e2e-mutex.sh <acquire|wait|release|run> <owner-artifacts-dir> [-- command...]}"
AD="${2:?usage: e2e-mutex.sh <acquire|wait|release|run> <owner-artifacts-dir> [-- command...]}"
LOCK="${ARCHON_E2E_LOCK:-$HOME/.archon/control/e2e-stack.lock}"
DB="${ARCHON_DB:-$HOME/.archon/archon.db}"
WAIT_S="${ARCHON_E2E_WAIT_SECONDS:-1800}"
POLL_S="${ARCHON_E2E_POLL_SECONDS:-5}"
OWNER=""
TOOK=""

run_status() { # $1 = run id; prints its status, or nothing when unknown/unreadable
  python3 - "$DB" "$1" <<'PY' 2>/dev/null
import sqlite3, sys, time
db, run = sys.argv[1], sys.argv[2]
for attempt in range(5):
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
        row = con.execute("select status from remote_agent_workflow_runs where id = ?", (run,)).fetchone()
        con.close()
        print(row[0] if row else "")
        break
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc) and "busy" not in str(exc):
            break
        time.sleep(0.2 * (attempt + 1))
PY
}

reclaim_if_stale() {
  [ -n "$OWNER" ] || return 1
  local pid status stale
  pid=$(cat "$LOCK/pid" 2>/dev/null || true)
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then return 1; fi
  status=$(run_status "$(basename "$OWNER")")
  case "$status" in completed|failed|cancelled) ;; *) return 1 ;; esac
  stale="$LOCK.stale.$$"
  mv "$LOCK" "$stale" 2>/dev/null || return 1
  # ponytail: read-then-rename race; a lock re-taken between the owner read and
  # the mv is put back. A per-lock generation token would close it fully.
  if [ "$(cat "$stale/owner" 2>/dev/null)" != "$OWNER" ]; then
    mv "$stale" "$LOCK" 2>/dev/null || true
    return 1
  fi
  rm -rf "$stale"
  echo "E2E_MUTEX=RECLAIMED stale owner=$OWNER status=$status"
}

try_acquire() { # $1 = holder pid ("" for none). 0 = ours (TOOK=yes|held), 1 = someone else's
  mkdir -p "$(dirname "$LOCK")"
  if mkdir "$LOCK" 2>/dev/null; then
    printf '%s\n' "$AD" > "$LOCK/owner"
    if [ -n "$1" ]; then printf '%s\n' "$1" > "$LOCK/pid"; fi
    TOOK=yes
    echo "E2E_MUTEX=ACQUIRED owner=$AD"
    return 0
  fi
  OWNER=$(cat "$LOCK/owner" 2>/dev/null || true)
  if [ "$OWNER" = "$AD" ]; then
    TOOK=held
    echo "E2E_MUTEX=HELD owner=$AD (re-entrant: this run already owns it)"
    return 0
  fi
  reclaim_if_stale && try_acquire "$1" && return 0
  return 1
}

wait_acquire() { # $1 = holder pid
  local start now waited last=-60
  start=$(date +%s)
  while :; do
    try_acquire "$1" && return 0
    now=$(date +%s); waited=$((now - start))
    if [ $((waited - last)) -ge 60 ]; then
      echo "E2E_MUTEX=WAITING owner=${OWNER:-<unknown>} waited=${waited}s"
      last=$waited
    fi
    if [ "$waited" -ge "$WAIT_S" ]; then
      echo "E2E_MUTEX=FAIL timeout waited=${waited}s limit=${WAIT_S}s owner=${OWNER:-<unknown>}"
      echo "  The owner is still live (or not a run archon.db knows), so its lock was not"
      echo "  reclaimed. Retry once it finishes, or release by hand once it is gone:"
      echo "    rm -rf $LOCK"
      return 1
    fi
    sleep "$(awk -v p="$POLL_S" -v r="$RANDOM" 'BEGIN { printf "%.2f", p * (1 + r / 32767) }')"
  done
}

release_lock() {
  if [ ! -d "$LOCK" ]; then
    echo "E2E_MUTEX=NOOP not held"
    return 0
  fi
  OWNER=$(cat "$LOCK/owner" 2>/dev/null || echo "")
  if [ -n "$OWNER" ] && [ "$OWNER" != "$AD" ]; then
    echo "E2E_MUTEX=NOOP held by another run ($OWNER) - not releasing someone else's lock"
    return 0
  fi
  rm -rf "$LOCK"
  echo "E2E_MUTEX=RELEASED owner=$AD"
}

case "$OP" in
  acquire)
    try_acquire "" && exit 0
    echo "E2E_MUTEX=FAIL the e2e stack (54322/8001, project goodword-e2e) is held by another run"
    echo "  owner artifacts: ${OWNER:-<unknown>}"
    echo "  That run's migrations and seed own the shared database; starting a second"
    echo "  stack over it would mutate its rows with no error. Let the owner reach its"
    echo "  smoke gate and approve it (teardown releases this), or release by hand:"
    echo "    rm -rf $LOCK"
    exit 1
    ;;
  wait)
    # The caller (e.g. run-joint-integration.py) holds the stack, not this shell.
    wait_acquire "$PPID"
    exit $?
    ;;
  release)
    release_lock
    exit 0
    ;;
  run)
    shift 2
    if [ "${1-}" = "--" ]; then shift; fi
    if [ "$#" -eq 0 ]; then echo "E2E_MUTEX=FAIL run needs a command after --"; exit 2; fi
    wait_acquire "$$" || exit 1
    if [ "$TOOK" = yes ]; then trap 'release_lock' EXIT; fi
    trap 'exit 143' TERM
    trap 'exit 130' INT
    "$@"
    exit $?
    ;;
  *)
    echo "E2E_MUTEX=FAIL unknown op [$OP] (expected acquire, wait, release or run)"
    exit 1
    ;;
esac
