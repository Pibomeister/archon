#!/usr/bin/env bash
# resume.sh -- guarded wrapper around `archon workflow resume <run-id>`.
#
# The CLI validates the run id you name (it must be failed or paused) and then
# discards it: it calls the executor with {resume:true} and no id, and the
# executor re-selects the run to resume with
#
#   SELECT * FROM remote_agent_workflow_runs
#    WHERE workflow_name = ?1
#      AND working_path  = ?2
#      AND (status IN ('failed','paused')
#           OR (status = 'running'
#               AND (last_activity_at IS NULL
#                    OR last_activity_at < datetime('now','-' || ?3 || ' days'))))
#    ORDER BY started_at DESC
#    LIMIT 1                                     -- ?3 = 1 (day)
#
# The grace period is ONE day, not three. In the bundle the clause is built by
# `Dw$(Y, 3)` -> `nowMinusDays(3)` -> `datetime('now','-' || $3 || ' days')`: the
# 3 is the SQL *placeholder index*, not a day count. $3 is bound to `Fw$`, and
# `Fw$ = 1`. setup/tests/test_resume_guard.py pins this with a run idle for two
# days, which must block.
#
# so the *newest* resumable run of the (workflow_name, working_path) lane wins,
# not the one you named. Resuming ab6ea8aa on 2026-08-29 executed 607fa834, a
# different bug report in the same lane that had failed 83 minutes later.
#
# This wrapper computes that same selection itself, refuses when it differs from
# the run you named, and afterwards verifies from the database that the named
# run -- and only the named run -- actually moved.
#
# Usage:  setup/resume.sh <run-id-or-prefix> [extra archon args...]
# Env:    ARCHON_DB   path to the archon sqlite db (default ~/.archon/archon.db)
#
# Every exit prints exactly one RESUME=<verdict> line on stdout.
set -euo pipefail

# The stale-'running' grace period baked into the CLI (Fw$ = 1 in the bundle).
STALE_DAYS=1  # archon 0.8.0 findResumableRun: Dw$(Y,3) binds placeholder $3 = Fw$ = 1 day, not "3 days"

ARCHON_DB="${ARCHON_DB:-$HOME/.archon/archon.db}"

if [ "$#" -lt 1 ]; then
  echo "RESUME=REFUSED reason=usage"
  echo "usage: resume.sh <run-id-or-prefix> [archon args...]" >&2
  exit 1
fi

PREFIX="$1"
shift

if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "RESUME=REFUSED reason=no-sqlite3"
  exit 1
fi

if ! command -v archon >/dev/null 2>&1; then
  echo "RESUME=REFUSED reason=no-archon"
  exit 1
fi

if [ ! -f "$ARCHON_DB" ]; then
  echo "RESUME=REFUSED reason=no-db db=$ARCHON_DB"
  exit 1
fi

# Same shape the CLI's own prefix resolver accepts; also keeps the prefix safe
# to interpolate into the LIKE below.
case "$PREFIX" in
  "" | *[!0-9a-fA-F-]*)
    echo "RESUME=REFUSED reason=bad-id-format id=$PREFIX"
    exit 1
    ;;
esac

q() { sqlite3 -noheader -separator '|' "$ARCHON_DB" "$1"; }
sqlq() { printf '%s' "$1" | sed "s/'/''/g"; }
short() { printf '%s' "${1:0:8}"; }

MATCHES="$(q "SELECT id FROM remote_agent_workflow_runs WHERE id LIKE '${PREFIX}%' ORDER BY id;")"
N_MATCHES="$(printf '%s\n' "$MATCHES" | awk 'NF{n++} END{print n+0}')"

if [ "$N_MATCHES" -eq 0 ]; then
  echo "RESUME=REFUSED reason=not-found id=$PREFIX"
  exit 1
fi

if [ "$N_MATCHES" -gt 1 ]; then
  echo "RESUME=REFUSED ambiguous prefix=$PREFIX matches=$N_MATCHES"
  printf '%s\n' "$MATCHES" | sed 's/^/  /'
  exit 1
fi

RUN_ID="$MATCHES"

# working_path is read last so any '|' inside it lands in the trailing field.
IFS='|' read -r WORKFLOW STATUS WORKING_PATH <<EOF
$(q "SELECT workflow_name, status, COALESCE(working_path,'') FROM remote_agent_workflow_runs WHERE id = '$RUN_ID';")
EOF

case "$STATUS" in
  failed | paused) ;;
  *)
    echo "RESUME=REFUSED status=$STATUS named=$(short "$RUN_ID")"
    exit 1
    ;;
esac

if [ -z "$WORKING_PATH" ]; then
  echo "RESUME=REFUSED reason=no-working-path named=$(short "$RUN_ID")"
  exit 1
fi

LANE="workflow_name = '$(sqlq "$WORKFLOW")' AND working_path = '$(sqlq "$WORKING_PATH")'"
RESUMABLE="(status IN ('failed','paused') OR (status = 'running' AND (last_activity_at IS NULL OR last_activity_at < datetime('now','-' || $STALE_DAYS || ' days'))))"

# `ORDER BY started_at DESC LIMIT 1` with no tiebreaker: if two resumable runs of
# the lane share the newest started_at, which one the CLI picks is undefined, so
# the guard cannot promise anything.
TIES="$(q "SELECT COUNT(*) FROM remote_agent_workflow_runs WHERE $LANE AND $RESUMABLE AND started_at = (SELECT MAX(started_at) FROM remote_agent_workflow_runs WHERE $LANE AND $RESUMABLE);")"
if [ "${TIES:-0}" -gt 1 ]; then
  echo "RESUME=REFUSED named=$(short "$RUN_ID") reason=started-at-tie tied=$TIES"
  echo "  Abandon the runs you do not want, then retry:"
  q "SELECT id FROM remote_agent_workflow_runs WHERE $LANE AND $RESUMABLE AND started_at = (SELECT MAX(started_at) FROM remote_agent_workflow_runs WHERE $LANE AND $RESUMABLE) ORDER BY id;" | sed 's/^/    archon workflow abandon /'
  exit 1
fi

WOULD_RESUME="$(q "SELECT id FROM remote_agent_workflow_runs WHERE $LANE AND $RESUMABLE ORDER BY started_at DESC, id ASC LIMIT 1;")"

if [ "$WOULD_RESUME" != "$RUN_ID" ]; then
  echo "RESUME=REFUSED would_resume=$(short "$WOULD_RESUME") named=$(short "$RUN_ID") reason=newer-resumable-run-of-lane"
  echo "  archon workflow abandon $WOULD_RESUME"
  exit 1
fi

# A row counts as moved when any of (status, started_at, last_activity_at)
# changes -- resumeWorkflowRun rewrites all three, and last_activity_at alone has
# only one-second resolution.
snapshot() {
  q "SELECT id || ' ' || status || ' ' || COALESCE(started_at,'') || ' ' || COALESCE(last_activity_at,'') FROM remote_agent_workflow_runs WHERE $LANE ORDER BY id;"
}

BEFORE="$(snapshot)"

set +e
DISABLE_OMC=1 archon workflow resume "$RUN_ID" "$@" </dev/null
ARCHON_RC=$?
set -e

AFTER="$(snapshot)"

MOVED="$(comm -13 <(printf '%s\n' "$BEFORE") <(printf '%s\n' "$AFTER") | awk 'NF{print $1}')"

NAMED_MOVED=0
OTHER_MOVED=""
for id in $MOVED; do
  if [ "$id" = "$RUN_ID" ]; then
    NAMED_MOVED=1
  else
    OTHER_MOVED="$id"
  fi
done

if [ -n "$OTHER_MOVED" ]; then
  echo "RESUME=WRONG_RUN named=$(short "$RUN_ID") moved=$(short "$OTHER_MOVED") archon_rc=$ARCHON_RC"
  exit 1
fi

if [ "$NAMED_MOVED" -eq 0 ]; then
  echo "RESUME=WRONG_RUN named=$(short "$RUN_ID") moved=none archon_rc=$ARCHON_RC"
  exit 1
fi

echo "RESUME=OK run=$(short "$RUN_ID") archon_rc=$ARCHON_RC"
exit "$ARCHON_RC"
