# Sourced, never executed: restores the ARCHON_FEATURE_* chain env from
# <artifacts-dir>/params.json when a plain `archon workflow resume` dropped what
# the launcher exported. feature_env.py holds the key map, so the shell and
# python readers cannot disagree about where a value comes from.
#
# A no-op when the env is already set or params.json is absent, so a reader with
# neither still reaches its own typed failure.
#
# Usage: . "$SETUP/feature-env.sh" "$ARTIFACTS_DIR"
# The artifacts dir MUST be passed explicitly: a sourced script with no
# arguments inherits the caller's positional parameters.
if [ -n "${1-}" ] && [ -f "$1/params.json" ]; then
  eval "$(python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/feature_env.py" "$1")"
fi
