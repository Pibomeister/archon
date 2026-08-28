#!/usr/bin/env bash
# M4.1 — package the Archon layer for the team gist.
# Builds .archon/dist/ from the live tree: reverse-templates this machine's root
# into the GOODWORD_ROOT placeholder, excludes dev smokes (wrap-*, lg-probe),
# runs a fail-closed secret gate and a round-trip identity check, then (with
# --publish) creates or updates the secret gist. Gists reject directories, so the
# payload ships flat: .archon/setup/foo.sh -> archon__setup__foo.sh.
set -euo pipefail

ARCHON="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$(dirname "$ARCHON")"
DIST="$ARCHON/dist"
GIST_ID_FILE="$ARCHON/.gist-id"
# Split literal: this file ships in the payload; the round-trip render-back must
# not rewrite this assignment.
PLACEHOLDER='{{GOODWORD''_ROOT}}'
PUBLISH=no
[ "${1:-}" = "--publish" ] && PUBLISH=yes

# Absolute grep/head (see install.sh for the shadowing workaround this preserves).
# The secret gate runs grep inside `if` conditions, where `set -e` does NOT fire —
# an unresolvable binary would return 127 and the gate would pass having scanned
# nothing. Resolve once, fail loudly.
resolve_bin() { # $1 = name -> absolute path on stdout, rc 1 if unresolvable
  local n="$1" p
  for p in "/usr/bin/$n" "/bin/$n"; do
    [ -x "$p" ] && { printf '%s' "$p"; return 0; }
  done
  p="$(command -v "$n" 2>/dev/null || true)"
  case "$p" in /*) [ -x "$p" ] && { printf '%s' "$p"; return 0; } ;; esac
  return 1
}
GREP="$(resolve_bin grep)" || { echo "PACKAGE=FAIL cannot resolve a grep binary on this host"; exit 1; }
HEAD="$(resolve_bin head)" || { echo "PACKAGE=FAIL cannot resolve a head binary on this host"; exit 1; }

# Payload manifest: everything a teammate's .archon/ tree needs, nothing else.
# register-probe ships (it IS the documented one-time folder registration);
# wrap-* and lg-probe are dev scaffolding pinned to this machine's fixtures.
# backfill.yaml is deliberately held back until its own clean trial passes —
# do not add it here without that evidence.
MANIFEST=(
  config.yaml
  RUNBOOK.md
  workflows/babysit.yaml
  workflows/bugfix.yaml
  workflows/bugfix-smoke-deployed.yaml
  workflows/cleanup.yaml
  workflows/full-sdlc-api.yaml
  workflows/full-sdlc-web.yaml
  workflows/register-probe.yaml
  setup/allowlist.json
  setup/bind-repo.py
  setup/check-fixer-result.py
  setup/check-scope.py
  setup/check-slop.py
  setup/detach.py
  setup/gist-README.md
  setup/install.sh
  setup/negcontrol.sh
  setup/package.sh
  setup/params-env.sh
  setup/parse-critique.py
  setup/parse-review-envelope.py
  setup/plan-shape.sh
  setup/rca-shape.sh
  setup/resolve-params.sh
  setup/run-repro.sh
  setup/selective-genapi-patch.py
  setup/stage-skills.sh
  setup/strip-premise-answers.py
  setup/thread-lane.py
  setup/update-waivers.py
  setup/write-review-summary.py
  # Claude skills staged into <root>/.claude/skills by stage-skills.sh. The flat
  # gist name is archon__skills__<name>__SKILL.md, so NO skill file or directory
  # name may contain a double underscore — install.sh un-flattens on '__'.
  skills/archon-install/SKILL.md
  skills/archon-sdlc/SKILL.md
  templates/implement-node.md
  templates/repo-conventions-api.md
  templates/repo-conventions-web.md
)

FIXTURE="$ROOT/.omc/research/toy-feature-spec.md"

for f in "${MANIFEST[@]}"; do
  test -f "$ARCHON/$f" || { echo "PACKAGE=FAIL manifest file missing: .archon/$f"; exit 1; }
done
test -f "$FIXTURE" || { echo "PACKAGE=FAIL toy fixture missing: $FIXTURE"; exit 1; }

# --- Reverse check: every setup/ script a manifest workflow references must ---
# itself be in MANIFEST, or a teammate's install ships a workflow that calls a
# script that never arrived. Only manifest workflow YAMLs are scanned.
# grep's exit code drives the fail path directly (rc>=2 fails closed) rather
# than living inside an `if` condition, for the same reason the secret gate
# above resolves its own grep binary: `if grep ...` is invisible to `set -e`.
echo "--- reverse check (workflow -> setup script coverage) ---"
REVERSE_FAIL=0
for f in "${MANIFEST[@]}"; do
  case "$f" in workflows/*.yaml) ;; *) continue ;; esac
  REFS="$("$GREP" -ohE 'setup/[A-Za-z0-9_.-]+' "$ARCHON/$f" | sort -u)" && grc=0 || grc=$?
  if [ "$grc" -ge 2 ]; then
    echo "PACKAGE=FAIL grep errored scanning $f for setup/ references (fail-closed)"
    REVERSE_FAIL=1
    continue
  fi
  while IFS= read -r ref; do
    test -n "$ref" || continue
    IN_MANIFEST=no
    for m in "${MANIFEST[@]}"; do
      [ "$m" = "$ref" ] && { IN_MANIFEST=yes; break; }
    done
    if [ "$IN_MANIFEST" = no ]; then
      echo "PACKAGE=FAIL workflow references unpackaged script $ref (from $f)"
      REVERSE_FAIL=1
    fi
  done <<< "$REFS"
done
[ "$REVERSE_FAIL" = 0 ] || { echo "PACKAGE=FAIL reverse check — see unpackaged-script lines above"; exit 1; }
echo "REVERSE_CHECK=OK"

# --- VERSION: date-serial, bumped when repackaging on the same day -------------
TODAY="$(date +%Y.%m.%d)"
SERIAL=1
if [ -f "$ARCHON/VERSION" ]; then
  PREV="$(cat "$ARCHON/VERSION")"
  case "$PREV" in
    "$TODAY"-*) SERIAL=$(( ${PREV##*-} + 1 )) ;;
  esac
fi
VERSION="$TODAY-$SERIAL"
printf '%s\n' "$VERSION" > "$ARCHON/VERSION"
echo "VERSION=$VERSION"

# --- Build payload (templated) + flat gist layout ------------------------------
rm -rf "$DIST"
mkdir -p "$DIST/payload" "$DIST/gist"

template() { # $1 src, $2 dest: reverse-template ROOT -> placeholder
  python3 - "$1" "$2" "$ROOT" "$PLACEHOLDER" <<'PY'
import sys
src, dest, root, ph = sys.argv[1:5]
data = open(src, encoding="utf-8").read()
open(dest, "w", encoding="utf-8").write(data.replace(root, ph))
PY
}

for f in "${MANIFEST[@]}"; do
  mkdir -p "$DIST/payload/$(dirname "$f")"
  template "$ARCHON/$f" "$DIST/payload/$f"
  flat="archon__${f//\//__}"
  cp "$DIST/payload/$f" "$DIST/gist/$flat"
done
# Vendored CE skills (validated 3.2.0 snapshot): stage-skills.sh's fallback when
# no cached CE version carries the headless contract (upstream restructured the
# skills after 3.2.0). Shipped whole; the '__' flattening rule applies, so guard.
test -d "$ARCHON/vendor/ce-skills" || { echo "PACKAGE=FAIL vendor/ce-skills missing — the install fallback would be hollow"; exit 1; }
while IFS= read -r vf; do
  rel="${vf#"$ARCHON"/}"
  case "$rel" in *__*) echo "PACKAGE=FAIL vendored path contains '__' (breaks flattening): $rel"; exit 1 ;; esac
  mkdir -p "$DIST/payload/$(dirname "$rel")"
  template "$vf" "$DIST/payload/$rel"
  flat="archon__${rel//\//__}"
  cp "$DIST/payload/$rel" "$DIST/gist/$flat"
done < <(find "$ARCHON/vendor/ce-skills" -type f | sort)
printf '%s\n' "$VERSION" > "$DIST/payload/VERSION"

# Top-level gist files: entry point, docs, allowlist, fixture, VERSION.
cp "$DIST/payload/setup/install.sh" "$DIST/gist/install.sh"
cp "$DIST/payload/setup/gist-README.md" "$DIST/gist/README.md"
cp "$DIST/payload/setup/allowlist.json" "$DIST/gist/allowlist.json"
cp "$FIXTURE" "$DIST/gist/toy-feature-spec.md"
printf '%s\n' "$VERSION" > "$DIST/gist/VERSION"
if [ -f "$GIST_ID_FILE" ]; then
  GID="$(cat "$GIST_ID_FILE")"
  python3 - "$DIST/gist/README.md" "$GID" <<'PY'
import sys
p, gid = sys.argv[1:3]
d = open(p, encoding="utf-8").read()
open(p, "w", encoding="utf-8").write(d.replace("GIST_ID_HERE", gid))
PY
fi

# --- Secret gate (fail-closed) -------------------------------------------------
echo "--- secret gate ---"
GATE_FAIL=0
# No grep in the condition: a `find | grep -q` in an `if` is invisible to set -e,
# so a broken grep would report "no leak" having scanned nothing.
LEAKED="$(find "$DIST/gist" -name 'settings.local.json')"
if [ -n "$LEAKED" ]; then
  echo "SECRET_GATE=FAIL settings.local.json must never ship: $LEAKED"; GATE_FAIL=1
fi
# Pattern literals are split ('sk-''ant') so this file, which ships in the
# payload being scanned, cannot match its own gate.
PATTERNS=(
  'sk-''ant'
  'AKIA''[0-9A-Z]{16}'
  'ASIA''[0-9A-Z]{16}'
  'ghp_''[A-Za-z0-9]{20,}'
  'gho_''[A-Za-z0-9]{20,}'
  'github_''pat_'
  '-----BEGIN'' [A-Z ]*PRIVATE KEY'
  'api''[_-]?key *= *[A-Za-z0-9]'
  '[A-Za-z0-9+/=]''{60,}'
  '/Use''rs/'
)
for pat in "${PATTERNS[@]}"; do
  hits="$("$GREP" -rInE -e "$pat" "$DIST/gist")" && rc=0 || rc=$?
  if [ $rc -eq 0 ]; then
    echo "SECRET_GATE=FAIL pattern '$pat' matched:"; echo "$hits" | "$HEAD" -5; GATE_FAIL=1
  elif [ $rc -ge 2 ]; then
    echo "SECRET_GATE=FAIL grep errored on pattern '$pat' (fail-closed)"; GATE_FAIL=1
  fi
done
[ "$GATE_FAIL" = 0 ] || { echo "PACKAGE=FAIL secret gate — refusing to publish"; exit 1; }
echo "SECRET_GATE=OK"

# --- Round-trip identity: render payload back with this root, diff vs live ----
echo "--- round-trip check ---"
RT="$(mktemp -d)"
trap 'rm -rf "$RT"' EXIT
RT_FAIL=0
for f in "${MANIFEST[@]}"; do
  mkdir -p "$RT/$(dirname "$f")"
  python3 - "$DIST/payload/$f" "$RT/$f" "$ROOT" "$PLACEHOLDER" <<'PY'
import sys
src, dest, root, ph = sys.argv[1:5]
data = open(src, encoding="utf-8").read()
open(dest, "w", encoding="utf-8").write(data.replace(ph, root))
PY
  if ! diff -q "$RT/$f" "$ARCHON/$f" >/dev/null; then
    echo "ROUNDTRIP=FAIL $f differs after render-back"; RT_FAIL=1
  fi
done
[ "$RT_FAIL" = 0 ] || { echo "PACKAGE=FAIL round-trip identity"; exit 1; }
echo "ROUNDTRIP=OK (${#MANIFEST[@]} files)"

echo "PACKAGE=OK dist ready at $DIST/gist"
[ "$PUBLISH" = yes ] || { echo "Dry build only — re-run with --publish to push the gist."; exit 0; }

# --- Publish: create once, then update via the gist's git remote ---------------
if [ ! -f "$GIST_ID_FILE" ]; then
  # gh gist create has no --secret flag: secret IS the default (only --public exists).
  URL="$(cd "$DIST/gist" && gh gist create --desc "Goodword Archon SDLC setup ($VERSION)" ./*)"
  GID="${URL##*/}"
  printf '%s\n' "$GID" > "$GIST_ID_FILE"
  echo "PUBLISH=OK created secret gist $URL"
  echo "NOTE: README still shows GIST_ID_HERE on this first publish — re-run with --publish to bake the id in."
else
  GID="$(cat "$GIST_ID_FILE")"
  CLONE="$(mktemp -d)/gist"
  # HTTPS + gh credentials: ssh to gist.github.com is unconfigured on most machines.
  git clone -q -c credential.helper='!gh auth git-credential' \
    "https://gist.github.com/$GID.git" "$CLONE"
  find "$CLONE" -maxdepth 1 -type f -delete
  cp "$DIST/gist/"* "$CLONE/"
  git -C "$CLONE" add -A
  if git -C "$CLONE" diff --cached --quiet; then
    echo "PUBLISH=OK gist already up to date ($VERSION)"
  else
    git -C "$CLONE" commit -q -m "Release $VERSION"
    git -C "$CLONE" push -q
    echo "PUBLISH=OK updated gist https://gist.github.com/$GID to $VERSION"
  fi
  rm -rf "$(dirname "$CLONE")"
fi
