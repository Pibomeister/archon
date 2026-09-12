#!/usr/bin/env bash
# Run-parameter derivation — the single point where a run's identity comes from,
# including WHICH REPOSITORY the run targets.
# Usage: resolve-params.sh <goodword-root> <arguments> <artifacts-dir> [api-port-base] [web-port-base] [--allow <csv>]
# <arguments> is the CLI run message (env ARGUMENTS in nodes): the ABSOLUTE path
# to the feature spec. Empty is a hard failure — a run with no spec has no
# identity; the toy dry-run passes its spec path explicitly like any other run.
# Deterministic: same message, same params — that is what makes resume and
# adopt-if-exists coherent across processes.
#
# The optional port bases are the lane's OWN literals (4124/3124 for bugfix, and
# so on). They are the only place those numbers still appear in a lane, which is
# what keeps derive-lite.py's port map -- a plain string substitution -- working.
# port-alloc.sh turns a base into a per-run port; without bases, params.json
# simply carries no ports and the lane has no smoke stack. A base is also
# IGNORED when the resolved repo declares no smoke stack, so a repo with no
# server never allocates a port it cannot use.
#
# --allow <csv> is what the LANE supports and gates NEW selection only. It is
# deliberately not applied to an already-bound run: bugfix.yaml passes no
# --allow, and bind-repo.py rewrites its params to repo=web-app after the RCA
# gate, so re-applying the default would reject every resumed web-app bugfix.
set -euo pipefail

# --allow is trailing so positionals 1-5 keep their existing meaning for every
# current caller; strip it before binding them.
ALLOW="api"
ARGV=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --allow) ALLOW="${2:?--allow needs a comma-separated repo list}"; shift 2 ;;
    *) ARGV+=("$1"); shift ;;
  esac
done
set -- "${ARGV[@]+"${ARGV[@]}"}"

ROOT="${1:?usage: resolve-params.sh <root> <arguments> <artifacts-dir> [api-port-base] [web-port-base] [--allow <csv>]}"
ARGS="${2-}"
AD="${3:?usage: resolve-params.sh <root> <arguments> <artifacts-dir> [api-port-base] [web-port-base] [--allow <csv>]}"
APIBASE="${4-}"
WEBBASE="${5-}"

SPEC="$ARGS"
test -n "$SPEC" || { echo "PARAMS=FAIL no spec path in run message — invoke as: archon workflow run full-sdlc-api \"/abs/path/to/spec.md\""; exit 1; }
case "$SPEC" in
  /*) : ;;
  *) echo "PARAMS=FAIL spec path must be absolute, got: $SPEC"; exit 1 ;;
esac
test -f "$SPEC" || { echo "PARAMS=FAIL spec file missing: $SPEC"; exit 1; }

SLUG=$(basename "$SPEC" | sed 's/\.[^.]*$//' | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//' | cut -c1-60)
test -n "$SLUG" || { echo "PARAMS=FAIL empty slug from spec: $SPEC"; exit 1; }

# ---- repo selection: the four-step sequence, implemented once ----------------
# 1. An existing params.json with a "repo" is the EXISTING BINDING.
# 2. An explicit ARCHON_REPO that CONFLICTS with it is a typed stop, never a
#    silent rebind.
# 3. A binding with ARCHON_REPO unset OR EQUAL is adopted, skipping --allow.
#    "or equal" is not cosmetic: without it, naming the repo you are already
#    bound to would fail step 3 and then be rejected by a default --allow=api,
#    so being explicit would break a run that staying silent would not.
# 4. Only with NO binding does --allow gate ${ARCHON_REPO:-api}.
BOUND=""
if [ -f "$AD/params.json" ]; then
  BOUND=$(python3 -c "
import json, sys
try:
    print(json.load(open(sys.argv[1], encoding='utf-8')).get('repo') or '')
except Exception:
    print('')
" "$AD/params.json")
fi
WANT="${ARCHON_REPO-}"
FEATURE_SCOPE="${ARCHON_FEATURE_SCOPE-}"
FEATURE_PHASE="${ARCHON_FEATURE_PHASE-}"
# Repository-list chains: the launcher's controller writes params.json right
# after the run row exists. A Claude lane starts detached, so its preflight can
# reach this point first; wait for the binding instead of deriving a rival one.
if [ "$FEATURE_SCOPE" = repositories ]; then
  WAIT="${ARCHON_PARAMS_WAIT_SECONDS:-60}"
  while [ ! -f "$AD/params.json" ] && [ "$WAIT" -gt 0 ]; do
    sleep 1; WAIT=$((WAIT - 1))
  done
  if [ ! -f "$AD/params.json" ]; then
    echo "PARAMS=FAIL missing controller params for repository-list phase=${FEATURE_PHASE:-unknown} (launch through archon-run.py feature)"
    exit 1
  fi
  echo "PARAMS=OK adopted repository feature params phase=${FEATURE_PHASE:-unknown}"
  exit 0
fi
if [ -n "$BOUND" ]; then
  if [ -n "$WANT" ] && [ "$WANT" != "$BOUND" ]; then
    echo "PARAMS=FAIL REPO_CONFLICT run is bound to $BOUND but ARCHON_REPO=$WANT (rebinding a live run is not supported; start a new run or clear the artifacts dir)"
    exit 1
  fi
  REPO="$BOUND"
  SELECTION="adopted"
else
  REPO="${WANT:-api}"
  SELECTION="new"
  case ",$ALLOW," in
    *",$REPO,"*) : ;;
    *) echo "PARAMS=FAIL repo $REPO not supported by this lane (allowed: $ALLOW)"; exit 1 ;;
  esac
fi

# Capture-and-check: `eval "$(...)"` alone swallows a non-zero exit, so a helper
# that dies with empty stdout would silently leave every profile value unset.
PROFILE=$(bash "$ROOT/.archon/setup/repo-profile.sh" "$REPO") || { echo "PARAMS=FAIL repo-profile.sh failed for repo $REPO"; exit 1; }
eval "$PROFILE"

APIPORT=""; WEBPORT=""
if [ -n "$HAS_SMOKE" ]; then
  if [ -n "$APIBASE" ]; then
    APIPORT=$(bash "$ROOT/.archon/setup/port-alloc.sh" "$APIBASE" "$SLUG") || { echo "PARAMS=FAIL cannot allocate api port from base $APIBASE"; exit 1; }
  fi
  if [ -n "$WEBBASE" ]; then
    WEBPORT=$(bash "$ROOT/.archon/setup/port-alloc.sh" "$WEBBASE" "$SLUG") || { echo "PARAMS=FAIL cannot allocate web port from base $WEBBASE"; exit 1; }
  fi
fi

python3 - "$AD/params.json" "$SPEC" "$SLUG" "$ROOT" "$APIPORT" "$WEBPORT" "$REPO" <<'PY'
import json, sys
out, spec, slug, root, api_port, web_port, repo = sys.argv[1:8]
params = {
    "spec": spec,
    "slug": slug,
    "branch": f"archon/{slug}",
    "repo": repo,
    "worktree": f"{root}/{repo}/.worktrees/{slug}",
}
if api_port:
    params["api_port"] = int(api_port)
if web_port:
    params["web_port"] = int(web_port)
json.dump(params, open(out, "w", encoding="utf-8"), indent=2)
PY
echo "PARAMS=OK spec=$SPEC slug=$SLUG repo=$REPO ($SELECTION) branch=archon/$SLUG api_port=${APIPORT:-none} web_port=${WEBPORT:-none}"
