#!/usr/bin/env bash
# Legacy command/stdout-based backfill execution cannot authorize writes.
set -uo pipefail
AD="${1:?usage: armed-exec.sh <artifacts_dir> apply}"
MODE="${2:?usage: armed-exec.sh <artifacts_dir> apply}"
if [ "$MODE" != apply ]; then
  echo "ARMED_EXEC=FAIL unknown mode $MODE"
  exit 10
fi
if [ -f "$AD/KILL_SWITCH" ]; then
  echo "KILL_SWITCH=ENGAGED refusing to execute"
  exit 3
fi
echo "LEGACY_BACKFILL_EXECUTION=DISABLED proposal-v2 and hardened controller executor required"
exit 10
