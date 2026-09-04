#!/usr/bin/env bash
# Refuse to run a query against anything but the prod READ REPLICA.
# Two independent assertions, because either one alone is defeatable: the
# credentials secret could be repointed (host check catches it) and a host
# could be renamed (pg_is_in_recovery catches it). Callers export PG* first.
# Usage: bash assert-ro.sh   ->   RO_GUARD=OK | RO_GUARD=FAIL <typed reason>
set -uo pipefail
case "${PGHOST:-}" in
  *-ro-*) ;;
  *) echo "RO_GUARD=FAIL host-not-read-replica"; exit 1 ;;
esac
RECOVERY=$(psql -X -t -A -c 'SELECT pg_is_in_recovery()' 2>&1) || {
  echo "RO_GUARD=FAIL recovery-check-failed"; exit 1
}
test "$(printf '%s' "$RECOVERY" | tr -d '[:space:]')" = t || {
  echo "RO_GUARD=FAIL not-in-recovery"; exit 1
}
echo "RO_GUARD=OK"
