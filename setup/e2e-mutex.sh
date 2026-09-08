#!/usr/bin/env bash
# Host mutex for the e2e docker stack (api/local-env-compose.e2e.yaml).
#
# Usage: e2e-mutex.sh acquire <owner-artifacts-dir>
#        e2e-mutex.sh release <owner-artifacts-dir>
#
# The compose file pins a project name (goodword-e2e), container names
# (postgres-db-e2e, dynamodb-local-e2e), named volumes and host ports 54322/8001.
# `up -d --wait` is idempotent, so a second run does NOT error: it attaches to
# the SAME Postgres and then runs its own migrations and seed over the first
# run's rows. Per-run isolation would mean editing that compose file, whose other
# consumer is the api integration suite -- so this serializes the phase LOUDLY
# instead, which is the failure mode we can afford.
#
# The lock is an atomic mkdir. It is deliberately NOT a blocking wait: the smoke
# stack outlives its node and stays up across a human approval gate that can take
# hours, so blocking would hang a run behind a person. A collision is a typed
# stop naming the owner and the one command that clears it.
set -uo pipefail

OP="${1:?usage: e2e-mutex.sh <acquire|release> <owner-artifacts-dir>}"
AD="${2:?usage: e2e-mutex.sh <acquire|release> <owner-artifacts-dir>}"
LOCK="${ARCHON_E2E_LOCK:-$HOME/.archon/control/e2e-stack.lock}"

case "$OP" in
  acquire)
    mkdir -p "$(dirname "$LOCK")"
    if mkdir "$LOCK" 2>/dev/null; then
      printf '%s\n' "$AD" > "$LOCK/owner"
      echo "E2E_MUTEX=ACQUIRED owner=$AD"
      exit 0
    fi
    OWNER=$(cat "$LOCK/owner" 2>/dev/null || echo "<unknown>")
    if [ "$OWNER" = "$AD" ]; then
      echo "E2E_MUTEX=HELD owner=$AD (re-entrant: this run already owns it)"
      exit 0
    fi
    echo "E2E_MUTEX=FAIL the e2e stack (54322/8001, project goodword-e2e) is held by another run"
    echo "  owner artifacts: $OWNER"
    echo "  That run's migrations and seed own the shared database; starting a second"
    echo "  stack over it would mutate its rows with no error. Let the owner reach its"
    echo "  smoke gate and approve it (teardown releases this), or release by hand:"
    echo "    rm -rf $LOCK"
    exit 1
    ;;
  release)
    OWNER=$(cat "$LOCK/owner" 2>/dev/null || echo "")
    if [ ! -d "$LOCK" ]; then
      echo "E2E_MUTEX=NOOP not held"
      exit 0
    fi
    if [ -n "$OWNER" ] && [ "$OWNER" != "$AD" ]; then
      echo "E2E_MUTEX=NOOP held by another run ($OWNER) - not releasing someone else's lock"
      exit 0
    fi
    rm -rf "$LOCK"
    echo "E2E_MUTEX=RELEASED owner=$AD"
    exit 0
    ;;
  *)
    echo "E2E_MUTEX=FAIL unknown op [$OP] (expected acquire or release)"
    exit 1
    ;;
esac
