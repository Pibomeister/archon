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
# so the *newest* resumable run of the (workflow_name, working_path) lane wins,
# not the one you named. On 2026-08-29 `archon workflow resume ab6ea8aa` executed
# 607fa834 instead -- a different bug report in the same lane, whose last
# node_failed was at 23:21:36, 16 seconds before the resume landed at 23:21:52.
#
# The grace period is ONE day, not three. In the bundle the clause is built by
# `Dw$(Y, 3)` -> `nowMinusDays(3)` -> `datetime('now','-' || $3 || ' days')`: the
# 3 is the SQL *placeholder index*, not a day count. $3 is bound to `Fw$`, and
# `Fw$ = 1`. setup/tests/test_resume_guard.py pins this with a run idle for two
# days, which must block.
#
# This wrapper computes that same selection itself, refuses when it differs from
# the run you named, and afterwards verifies from the database that the named
# run -- and only the named run -- actually moved.
#
# archon runs with stdin closed (`</dev/null`), per RUNBOOK section 1: the lane
# must never block waiting on a human at a terminal. Approval gates are resolved
# out of band with `archon workflow approve` / `archon workflow reject`, never by
# typing into this process.
#
# Usage:  setup/resume.sh <run-id-or-prefix> [extra archon args...]
# Env:    ARCHON_DB   path to the archon sqlite db (default ~/.archon/archon.db)
#
# Every exit prints exactly one RESUME= line on stdout, guaranteed by an EXIT
# trap. Verdicts:
#   RESUME=OK run=<8> archon_rc=0            the named run resumed and finished clean
#   RESUME=RAN run=<8> archon_rc=<n>         the named run resumed; workflow ended non-zero
#   RESUME=REFUSED ...                       the guard blocked; archon never ran
#   RESUME=NOT_EXECUTED named=<8> ...        archon failed before touching any run
#   RESUME=WRONG_RUN named=<8> moved=<8>,..  a different run of the lane moved
#   RESUME=ERROR stage=<s> reason=<r>        the guard itself could not complete
# Any verdict may carry `appeared=<8>,<8>`: rows that showed up in the lane
# during the resume. The CLI cannot create a run while resuming (resumeWorkflowRun
# only UPDATEs), so those belong to another operator and never change the verdict.
set -euo pipefail

# The stale-'running' grace period baked into the CLI (Fw$ = 1 in the bundle).
STALE_DAYS=1  # archon 0.8.0 findResumableRun: Dw$(Y,3) binds placeholder $3 = Fw$ = 1 day, not "3 days"

ARCHON_DB="${ARCHON_DB:-$HOME/.archon/archon.db}"

STAGE=init
ARCHON_RC=""
VERDICT_PRINTED=0
QOUT=""

verdict() {
  VERDICT_PRINTED=1
  printf '%s\n' "$1"
}

# Present only once archon has actually run, so a pre-archon error cannot imply
# that something was executed.
rc_suffix() {
  if [ -n "$ARCHON_RC" ]; then printf ' archon_rc=%s' "$ARCHON_RC"; fi
  return 0
}

# Guard failures must be distinguishable from archon's own exit code.
guard_error_rc() {
  if [ -n "$ARCHON_RC" ] && [ "$ARCHON_RC" -eq 90 ]; then printf '91'; else printf '90'; fi
}

fail() {
  verdict "RESUME=ERROR stage=$STAGE reason=$1$(rc_suffix)"
  exit "$(guard_error_rc)"
}

# Backstop: set -e, an unhandled signal, or any path that forgot to print must
# still leave exactly one typed line behind.
on_exit() {
  local rc=$?
  if [ "$VERDICT_PRINTED" -eq 0 ]; then
    printf 'RESUME=ERROR stage=%s reason=unexpected-exit rc=%s%s\n' "$STAGE" "$rc" "$(rc_suffix)"
    exit "$(guard_error_rc)"
  fi
}
trap on_exit EXIT

# Never let a control character in a stored value break the one-line contract.
sanitize() { printf '%s' "$1" | LC_ALL=C tr -c '[:print:]' '?'; }
sqlq() { printf '%s' "$1" | sed "s/'/''/g"; }
short() { printf '%s' "${1:0:8}"; }

# Sets QOUT. Runs in the parent shell -- a sqlite failure inside a command
# substitution would otherwise exit silently under set -e.
qq() {
  if ! QOUT="$(sqlite3 -noheader -separator '|' "$ARCHON_DB" "$1" 2>/dev/null)"; then
    fail "sqlite-failed"
  fi
}

# Newline/space separated id list -> "aaaaaaaa,bbbbbbbb".
join_short() {
  printf '%s\n' "$1" | tr ' ' '\n' | awk 'NF{printf "%s%s", (n++ ? "," : ""), substr($0, 1, 8)}'
}

if [ "$#" -lt 1 ]; then
  verdict "RESUME=REFUSED reason=usage"
  echo "usage: resume.sh <run-id-or-prefix> [archon args...]" >&2
  exit 1
fi

PREFIX="$1"
shift

if ! command -v sqlite3 >/dev/null 2>&1; then
  verdict "RESUME=REFUSED reason=no-sqlite3"
  exit 1
fi

if ! command -v archon >/dev/null 2>&1; then
  verdict "RESUME=REFUSED reason=no-archon"
  exit 1
fi

if [ ! -f "$ARCHON_DB" ]; then
  verdict "RESUME=REFUSED reason=no-db db=$(sanitize "$ARCHON_DB")"
  exit 1
fi

# Same shape the CLI's own prefix resolver accepts; also keeps the prefix safe
# to interpolate into the LIKE below.
case "$PREFIX" in
  "" | *[!0-9a-fA-F-]*)
    verdict "RESUME=REFUSED reason=bad-id-format id=$(sanitize "$PREFIX")"
    exit 1
    ;;
esac

STAGE=resolve

qq "SELECT id FROM remote_agent_workflow_runs WHERE id LIKE '${PREFIX}%' ORDER BY id;"
MATCHES="$QOUT"
N_MATCHES="$(printf '%s\n' "$MATCHES" | awk 'NF{n++} END{print n+0}')"

if [ "$N_MATCHES" -eq 0 ]; then
  verdict "RESUME=REFUSED reason=not-found id=$(sanitize "$PREFIX")"
  exit 1
fi

if [ "$N_MATCHES" -gt 1 ]; then
  verdict "RESUME=REFUSED ambiguous prefix=$(sanitize "$PREFIX") matches=$N_MATCHES"
  printf '%s\n' "$MATCHES" | sed 's/^/  /'
  exit 1
fi

RUN_ID="$MATCHES"

# Read each field with its own query and a '#' sentinel: a newline inside
# working_path would truncate a line-based `read`, and a truncated path silently
# becomes a different lane.
qq "SELECT status || '#' FROM remote_agent_workflow_runs WHERE id = '$RUN_ID';"
STATUS="${QOUT%\#}"
qq "SELECT workflow_name || '#' FROM remote_agent_workflow_runs WHERE id = '$RUN_ID';"
WORKFLOW="${QOUT%\#}"
qq "SELECT COALESCE(working_path,'') || '#' FROM remote_agent_workflow_runs WHERE id = '$RUN_ID';"
WORKING_PATH="${QOUT%\#}"

case "$STATUS" in
  failed | paused) ;;
  *)
    verdict "RESUME=REFUSED status=$(sanitize "$STATUS") named=$(short "$RUN_ID")"
    exit 1
    ;;
esac

if [ -z "$WORKING_PATH" ]; then
  verdict "RESUME=REFUSED reason=no-working-path named=$(short "$RUN_ID")"
  exit 1
fi

STAGE=select

LANE="workflow_name = '$(sqlq "$WORKFLOW")' AND working_path = '$(sqlq "$WORKING_PATH")'"
RESUMABLE="(status IN ('failed','paused') OR (status = 'running' AND (last_activity_at IS NULL OR last_activity_at < datetime('now','-' || $STALE_DAYS || ' days'))))"
NEWEST="started_at = (SELECT MAX(started_at) FROM remote_agent_workflow_runs WHERE $LANE AND $RESUMABLE)"

# `ORDER BY started_at DESC LIMIT 1` with no tiebreaker: if two resumable runs of
# the lane share the newest started_at, which one the CLI picks is undefined, so
# the guard cannot promise anything.
qq "SELECT COUNT(*) FROM remote_agent_workflow_runs WHERE $LANE AND $RESUMABLE AND $NEWEST;"
TIES="$QOUT"
if [ "${TIES:-0}" -gt 1 ]; then
  verdict "RESUME=REFUSED named=$(short "$RUN_ID") reason=started-at-tie tied=$TIES"
  echo "  lane: $(sanitize "$WORKFLOW") at $(sanitize "$WORKING_PATH")"
  echo "  Abandon the runs you do not want, then retry:"
  qq "SELECT id FROM remote_agent_workflow_runs WHERE $LANE AND $RESUMABLE AND $NEWEST ORDER BY id;"
  printf '%s\n' "$QOUT" | sed 's/^/    archon workflow abandon /'
  exit 1
fi

qq "SELECT id FROM remote_agent_workflow_runs WHERE $LANE AND $RESUMABLE ORDER BY started_at DESC, id ASC LIMIT 1;"
WOULD_RESUME="$QOUT"

if [ "$WOULD_RESUME" != "$RUN_ID" ]; then
  verdict "RESUME=REFUSED would_resume=$(short "$WOULD_RESUME") named=$(short "$RUN_ID") reason=newer-resumable-run-of-lane"
  echo "  lane: $(sanitize "$WORKFLOW") at $(sanitize "$WORKING_PATH")"
  echo "  archon workflow abandon $WOULD_RESUME"
  exit 1
fi

# A row counts as moved when any of (status, started_at, last_activity_at)
# changes -- resumeWorkflowRun rewrites all three, and last_activity_at alone has
# only one-second resolution.
snapshot() {
  qq "SELECT id || ' ' || status || ' ' || COALESCE(started_at,'') || ' ' || COALESCE(last_activity_at,'') FROM remote_agent_workflow_runs WHERE $LANE ORDER BY id;"
}

STAGE=pre
snapshot
BEFORE="$QOUT"

set +e
DISABLE_OMC=1 archon workflow resume "$RUN_ID" "$@" </dev/null
ARCHON_RC=$?
set -e

STAGE=post
snapshot
AFTER="$QOUT"

# Only ids present in BEFORE can have *moved*. A row that appeared during the
# resume cannot be the CLI's doing -- resumeWorkflowRun only UPDATEs, and
# createWorkflowRun is not on the resume path -- so it belongs to another
# operator and is reported, never counted as a wrong run. Real precedent:
# c66c7cc0 was created 23:25:27, inside 607fa834's resume window.
CHANGED="$(awk 'NR==FNR { if (NF) b[$1] = $0; next } NF && ($1 in b) && $0 != b[$1] { print $1 }' \
  <(printf '%s\n' "$BEFORE") <(printf '%s\n' "$AFTER") | sort)"
APPEARED="$(awk 'NR==FNR { if (NF) b[$1] = 1; next } NF && !($1 in b) { print $1 }' \
  <(printf '%s\n' "$BEFORE") <(printf '%s\n' "$AFTER") | sort)"

APPEARED_SUFFIX=""
if [ -n "$APPEARED" ]; then
  APPEARED_SUFFIX=" appeared=$(join_short "$APPEARED")"
fi

NAMED_MOVED=0
OTHERS=""
for id in $CHANGED; do
  if [ "$id" = "$RUN_ID" ]; then
    NAMED_MOVED=1
  else
    OTHERS="${OTHERS}${OTHERS:+ }$id"
  fi
done

if [ -n "$OTHERS" ]; then
  verdict "RESUME=WRONG_RUN named=$(short "$RUN_ID") moved=$(join_short "$OTHERS") archon_rc=$ARCHON_RC$APPEARED_SUFFIX"
  exit 1
fi

if [ "$NAMED_MOVED" -eq 0 ]; then
  if [ "$ARCHON_RC" -ne 0 ]; then
    # archon exited non-zero without touching a single row: nothing ran.
    verdict "RESUME=NOT_EXECUTED named=$(short "$RUN_ID") archon_rc=$ARCHON_RC$APPEARED_SUFFIX"
  else
    # archon claimed success but moved nothing -- a silent no-op.
    verdict "RESUME=WRONG_RUN named=$(short "$RUN_ID") moved=none archon_rc=0$APPEARED_SUFFIX"
  fi
  exit 1
fi

if [ "$ARCHON_RC" -eq 0 ]; then
  verdict "RESUME=OK run=$(short "$RUN_ID") archon_rc=0$APPEARED_SUFFIX"
  exit 0
fi

# The guard held and the right run executed; the workflow itself ended non-zero.
verdict "RESUME=RAN run=$(short "$RUN_ID") archon_rc=$ARCHON_RC$APPEARED_SUFFIX"
exit 1
