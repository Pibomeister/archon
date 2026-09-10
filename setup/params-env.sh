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
PARAMS=$(python3 - "$P" <<'PY_PARAMS'
import json, shlex, sys

try:
    with open(sys.argv[1], encoding="utf-8") as source:
        data = json.load(source)
    if not isinstance(data, dict):
        raise ValueError("params must be an object")
    values = {}
    for name, key in (("SPEC", "spec"), ("SLUG", "slug"), ("BR", "branch"), ("WT", "worktree")):
        value = data.get(key)
        if not isinstance(value, str) or not value or "\0" in value:
            raise ValueError("invalid " + key)
        values[name] = value
    for name, key in (("APIPORT", "api_port"), ("WEBPORT", "web_port")):
        value = data.get(key, "")
        if value != "" and (type(value) is not int or not 1 <= value <= 65535):
            raise ValueError("invalid " + key)
        values[name] = str(value)
    repo = data["repo"] if "repo" in data else "api"
    if not isinstance(repo, str) or not repo or "\0" in repo:
        raise ValueError("invalid repo")
    values["REPO_NAME"] = repo
except (OSError, UnicodeError, ValueError) as exc:
    print("PARAMS_ENV=FAIL " + str(exc), file=sys.stderr)
    raise SystemExit(1)

for name, value in values.items():
    print(f"{name}={shlex.quote(value)}")
PY_PARAMS
) || {
  printf '%s\n' "echo 'PARAMS_ENV=FAIL invalid or unreadable params.json' >&2" 'exit 1'
  exit 0
}
eval "$PARAMS"
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
printf '%s\n' "$PARAMS" "$PROFILE"
