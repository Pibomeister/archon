#!/usr/bin/env bash
# Per-run smoke-port allocation.
#
# Usage: port-alloc.sh <base> <slug>
# Prints one free TCP port, or exits 1 with a typed line.
#
# The lock on concurrency used to be the port literals: every run of a lane
# bound the same port, so a second run of that lane died at preflight (or, worse
# for full-sdlc-api vs full-sdlc-web, both bound 4123 and each smoke check
# passed against whichever server answered). Ports now derive from the run's
# slug instead of from the lane.
#
# port = base + 10*k, k in 0..SLOTS-1, starting at k = hash(slug) % SLOTS and
# walking forward. The stride of 10 keeps lanes disjoint by construction: lane
# bases differ by less than 10 (4123 api, 4124 bugfix, 4125 api-lite, 4126
# bugfix-lite, 4127 web; 3124/3126/3127 for web servers), so two lanes can never
# land on the same port however their slugs hash. Deterministic while the slot
# is free -- same spec, same port, which is what keeps resume and the teardown
# sweep coherent -- and it walks only when something already holds the slot.
set -uo pipefail

BASE="${1:?usage: port-alloc.sh <base> <slug>}"
SLUG="${2:?usage: port-alloc.sh <base> <slug>}"
SLOTS=20

case "$BASE" in ''|*[!0-9]*) echo "PORT_ALLOC=FAIL base is not an integer: [$BASE]" >&2; exit 1 ;; esac

busy() { # $1 = port; 0 = something is listening, 1 = free, 2 = cannot tell
  local out=""
  if command -v lsof >/dev/null 2>&1; then
    out="$(lsof -ti "tcp:$1" -sTCP:LISTEN 2>/dev/null || true)"
  elif command -v ss >/dev/null 2>&1; then
    out="$(ss -ltnp "sport = :$1" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u || true)"
  elif command -v fuser >/dev/null 2>&1; then
    out="$(fuser -n tcp "$1" 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+$' || true)"
  else
    return 2
  fi
  [ -n "$out" ]
}

# Same digest the rest of the layer uses for slugs; cksum is POSIX and stable.
K=$(printf '%s' "$SLUG" | cksum | awk -v s="$SLOTS" '{print $1 % s}')

for i in $(seq 0 $((SLOTS - 1))); do
  SLOT=$(( (K + i) % SLOTS ))
  PORT=$(( BASE + 10 * SLOT ))
  busy "$PORT"
  case $? in
    1) printf '%s\n' "$PORT"; exit 0 ;;
    2) echo "PORT_ALLOC=FAIL no port-inspection tool (need lsof, ss, or fuser)" >&2; exit 1 ;;
  esac
done

echo "PORT_ALLOC=FAIL all $SLOTS slots from base $BASE are busy (stride 10) - tear down stale runs" >&2
exit 1
