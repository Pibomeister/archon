#!/usr/bin/env bash
# Print shell assignments for a run's params.json, safely quoted.
# Usage inside a node:  eval "$(bash <abs>/params-env.sh "$ARTIFACTS_DIR/params.json")"
# Defines: SPEC, SLUG, BR, WT, and (when the lane allocated them) APIPORT/WEBPORT.
# APIPORT/WEBPORT are ALWAYS defined -- empty when the lane has no smoke stack --
# so `set -u` consumers can test them without a default expansion.
#
# Also delegates to repo-profile.sh, so post-params nodes get REPO and the
# repository's toolchain (CMD_INSTALL[], CMD_TYPECHECK[], CMD_LINT[], CMD_TEST[],
# ENV_SRC, HAS_SMOKE, HAS_BROWSER, IMPACT_INDEX) from the SAME definition
# resolve-params.sh used before params.json existed. A params.json with no
# "repo" key -- one written before repo-awareness, or a resumed legacy run --
# resolves to api, which is what every such run targeted.
#
# Failure propagation: this script's own callers use `eval "$(params-env.sh ...)"`,
# which swallows a non-zero exit. So it CAPTURES the helper (rather than
# blind-evaling it) and, on any failure, emits an eval-able failure of its own —
# measured to stop the caller with exit 1 for both a helper that prints an error
# and a helper that dies with empty stdout.
set -euo pipefail
P="${1:?usage: params-env.sh <params.json>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
test -f "$P" || { echo "echo 'PARAMS_ENV=FAIL missing $P'; exit 1"; exit 0; }
python3 - "$P" <<'PY'
import json, shlex, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
print(f"SPEC={shlex.quote(d['spec'])}")
print(f"SLUG={shlex.quote(d['slug'])}")
print(f"BR={shlex.quote(d['branch'])}")
print(f"WT={shlex.quote(d['worktree'])}")
print(f"APIPORT={d.get('api_port', '')}")
print(f"WEBPORT={d.get('web_port', '')}")
PY

REPO_NAME=$(python3 -c "
import json, sys
print(json.load(open(sys.argv[1], encoding='utf-8')).get('repo') or 'api')
" "$P")
PROFILE=$(bash "$HERE/repo-profile.sh" "$REPO_NAME") || {
  # Same rule as repo-profile.sh: the repo name comes from a params.json this
  # script did not write, so it is quoted before it becomes shell, and `exit 1`
  # is emitted on its own line where no `#` in the payload can reach it.
  python3 -c "
import shlex, sys
print('echo ' + shlex.quote('PARAMS_ENV=FAIL repo-profile.sh failed for repo ' + sys.argv[1]) + ' >&2')
print('exit 1')
" "$REPO_NAME"
  exit 0
}
printf '%s\n' "$PROFILE"
