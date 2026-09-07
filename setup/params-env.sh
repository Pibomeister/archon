#!/usr/bin/env bash
# Print shell assignments for a run's params.json, safely quoted.
# Usage inside a node:  eval "$(bash <abs>/params-env.sh "$ARTIFACTS_DIR/params.json")"
# Defines: SPEC, SLUG, BR, WT, and (when the lane allocated them) APIPORT/WEBPORT.
# APIPORT/WEBPORT are ALWAYS defined -- empty when the lane has no smoke stack --
# so `set -u` consumers can test them without a default expansion.
set -euo pipefail
P="${1:?usage: params-env.sh <params.json>}"
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
