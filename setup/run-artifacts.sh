#!/usr/bin/env bash
# Print a run's artifacts directory.
#
# Usage: run-artifacts.sh <run-id-or-prefix>
#
# The path used to be hardcoded as ~/.archon/workspaces/_folder/goodword/... --
# the layout of a FOLDER project. Since RUNBOOK 5a the Goodword root is a repo
# project so runs get one worktree (and one path lock) each, and archon puts
# their artifacts under ~/.archon/workspaces/_local/<Project>/artifacts/runs
# instead. Two consumers had the old literal baked in, and both fail in ways
# that cost a human real time: gate-approve.sh hard-exits before approving, and
# resume.sh silently restores NO chain env and then dies at the first
# attestation node after paying for the RCA again.
#
# The run row's own output_root is the authority. The literals below are only a
# fallback for a DB that predates the column being populated.
set -uo pipefail
RUN="${1:?usage: run-artifacts.sh <run-id-or-prefix>}"
DB="${ARCHON_DB:-$HOME/.archon/archon.db}"

case "$RUN" in
  "" | *[!0-9a-fA-F-]*) echo "RUN_ARTIFACTS=FAIL bad-id-format [$RUN]" >&2; exit 1 ;;
esac

ROOTS=()
if [ -f "$DB" ] && command -v sqlite3 >/dev/null 2>&1; then
  OUT=$(sqlite3 "file:$DB?mode=ro" \
    "SELECT COALESCE(output_root,'') FROM remote_agent_workflow_runs WHERE id LIKE '${RUN}%' ORDER BY started_at DESC LIMIT 1;" 2>/dev/null || true)
  [ -n "$OUT" ] && ROOTS+=("$OUT/artifacts/runs")
fi
[ -n "${ARCHON_ARTIFACTS_ROOT-}" ] && ROOTS+=("$ARCHON_ARTIFACTS_ROOT")
ROOTS+=("$HOME/.archon/workspaces/_local/Goodword/artifacts/runs")
ROOTS+=("$HOME/.archon/workspaces/_folder/goodword/artifacts/runs")

for root in "${ROOTS[@]}"; do
  for d in "$root/$RUN"*; do
    [ -d "$d" ] || continue
    printf '%s\n' "$d"
    exit 0
  done
done
echo "RUN_ARTIFACTS=FAIL no artifacts dir for $RUN (looked in: ${ROOTS[*]})" >&2
exit 1
