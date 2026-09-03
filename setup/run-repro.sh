#!/usr/bin/env bash
# THE single repro-execution point for the bugfix lane: red-gate, green-check,
# exit-gate, and negcontrol all run the repro through this script, so "the
# repro test" means exactly the same command everywhere. Reads
# failing-test.json from the artifacts dir, runs its command in the worktree
# under the repo's toolchain, writes full output to <outfile>, prints a tail
# for the node log, and exits with the test command's exit code. The LAST
# line printed is always REPRO_EXIT=<code>. Infra failures (missing files)
# exit 97 so callers can tell them from a passing (0) or failing (1+) test.
# Usage: run-repro.sh <worktree> <artifacts-dir> <outfile>
set -uo pipefail
WT="${1:?usage: run-repro.sh <worktree> <artifacts-dir> <outfile>}"
AD="${2:?usage: run-repro.sh <worktree> <artifacts-dir> <outfile>}"
OUT="${3:?usage: run-repro.sh <worktree> <artifacts-dir> <outfile>}"
FT="$AD/failing-test.json"
if [ ! -f "$FT" ]; then echo "REPRO=FAIL missing $FT" | tee "$OUT"; echo "REPRO_EXIT=97"; exit 97; fi
if [ ! -d "$WT" ]; then echo "REPRO=FAIL missing worktree $WT" | tee "$OUT"; echo "REPRO_EXIT=97"; exit 97; fi
REPO=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1], encoding='utf-8'))['repo'])" "$FT") || { echo "REPRO_EXIT=97"; exit 97; }
CMD=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1], encoding='utf-8'))['command'])" "$FT") || { echo "REPRO_EXIT=97"; exit 97; }
KIND=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1], encoding='utf-8'))['kind'])" "$FT") || { echo "REPRO_EXIT=97"; exit 97; }
if [ "$KIND" = integration ]; then
  echo "REPRO_NOTE=integration mutex: this run owns ports 54322/8001 machine-globally (a concurrent 'down -v' destroys this run's DB)"
  # Integration worktrees intentionally do not copy the ignored credentialed
  # .env.e2e file. Resolve the main checkout through the shared git dir and
  # export its existing harness environment for every RED/GREEN/negcontrol run.
  COMMON=$(git -C "$WT" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) \
    || { echo "REPRO=FAIL cannot resolve integration git common dir" | tee "$OUT"; echo "REPRO_EXIT=97"; exit 97; }
  BASE=$(dirname "$COMMON")
  E2E_ENV="$BASE/.env.e2e"
  if [ ! -f "$E2E_ENV" ]; then
    echo "REPRO=FAIL missing integration env $E2E_ENV" | tee "$OUT"
    echo "REPRO_EXIT=97"
    exit 97
  fi
  set -a
  # shellcheck disable=SC1090 -- protected main-checkout harness file.
  source "$E2E_ENV"
  set +a
fi
cd "$WT"
CODE=0
if [ "$REPO" = web-app ]; then
  # mise shims do not apply in bare execs — pin node 20 explicitly.
  mise x node@20 -- bash -c "$CMD" > "$OUT" 2>&1 || CODE=$?
else
  bash -c "$CMD" > "$OUT" 2>&1 || CODE=$?
fi
tail -40 "$OUT"
echo "REPRO_EXIT=$CODE"
exit "$CODE"
