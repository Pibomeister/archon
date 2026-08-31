#!/usr/bin/env bash
# M1.1 skill staging. Node sessions do NOT load installed plugins (M0.2, falsified
# premise); the proven remediation is a project-scope symlink of each used CE skill
# from the plugin cache, plus a `skills:` declaration on the node.
# Version policy: validated baseline 3.2.0 first, then newer versions >= the
# floor, then the vendored snapshot. The hard gate is a SUPPORTED REVIEW
# CONTRACT (old headless envelope or new agent-JSON — see contract_ok below);
# the review gates parse both return shapes via parse-review-envelope.py.
set -euo pipefail

ROOT="${1:?usage: stage-skills.sh <goodword-root>}"
test -d "$ROOT" || { echo "FAIL: root does not exist: $ROOT"; exit 1; }

CE_BASE="$HOME/.claude/plugins/cache/compound-engineering-plugin/compound-engineering"
CE_MIN="3.2.0"
CE_VALIDATED="3.2.0"
VENDOR="$ROOT/.archon/vendor/ce-skills/$CE_VALIDATED"

# Candidate list, best-first: cache versions >= the floor (newest first), then
# the vendored snapshot shipped with this payload. A candidate qualifies only
# when BOTH skills exist AND carry the headless contract markers — so a newer
# CE that restructured the skills (observed upstream: >= ~3.19 dropped
# mode:headless entirely) is skipped with a note instead of breaking installs.
CANDIDATES="$(python3 - "$CE_BASE" "$CE_MIN" <<'PY'
import os, re, sys
base, floor = sys.argv[1], sys.argv[2]
def key(v):
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", v)
    return tuple(map(int, m.groups())) if m else None
fl = key(floor)
try:
    entries = os.listdir(base)
except OSError:
    entries = []
cands = sorted(
    ((key(d), d) for d in entries
     if key(d) and key(d) >= fl and os.path.isdir(os.path.join(base, d, "skills"))),
    reverse=True,
)
# Validated baseline first: an unattended pipeline runs what was validated;
# newer versions are tried only when the baseline is absent.
ordered = [v for _, v in cands if v == floor] + [v for _, v in cands if v != floor]
for v in ordered:
    print(f"{v} {os.path.join(base, v, 'skills')}")
PY
)"
# Dual contract. Old (3.2.0): markers live in SKILL.md. New (>= ~3.19):
# ce-code-review's mode:headless (alias for mode:agent) lives in
# references/modes-and-output.md and the verdict field in
# references/finish-review.md; ce-doc-review's alias lives in
# references/modes.md. The review gates parse BOTH return shapes.
code_contract_ok() { # $1 = skills dir
  test -f "$1/ce-code-review/SKILL.md" || return 1
  { grep -q 'mode:headless' "$1/ce-code-review/SKILL.md" && grep -q '"verdict"' "$1/ce-code-review/SKILL.md"; } && return 0
  grep -q 'mode:headless' "$1/ce-code-review/references/modes-and-output.md" 2>/dev/null     && grep -q '"verdict"' "$1/ce-code-review/references/finish-review.md" 2>/dev/null
}
doc_contract_ok() { # $1 = skills dir
  test -f "$1/ce-doc-review/SKILL.md" || return 1
  grep -q 'mode:headless' "$1/ce-doc-review/SKILL.md" && return 0
  grep -q 'mode:headless' "$1/ce-doc-review/references/modes.md" 2>/dev/null
}
contract_ok() { # $1 = skills dir: both skills present with a supported contract
  code_contract_ok "$1" && doc_contract_ok "$1"
}
CE=""
CE_VER=""
# Preference order: the validated baseline from the cache, then the VENDORED
# validated snapshot, then newer cache versions whose contract holds. Teammates
# run the contract this pipeline was validated against; a newer CE's agent-JSON
# contract is accepted only when nothing validated is available.
while IFS=' ' read -r ver dir; do
  [ -n "$ver" ] || continue
  if [ "$ver" = "$CE_VALIDATED" ] && contract_ok "$dir"; then
    CE="$dir"; CE_VER="$ver"; break
  fi
done <<EOF_CANDS
$CANDIDATES
EOF_CANDS
if [ -z "$CE" ] && contract_ok "$VENDOR"; then
  CE="$VENDOR"; CE_VER="$CE_VALIDATED-vendored"
  echo "NOTE: validated baseline not in the plugin cache; staging the vendored $CE_VALIDATED snapshot shipped with this payload"
fi
if [ -z "$CE" ]; then
  while IFS=' ' read -r ver dir; do
    [ -n "$ver" ] || continue
    if contract_ok "$dir"; then
      CE="$dir"; CE_VER="$ver"
      echo "NOTE: staging CE $ver under its newer (agent-JSON) contract — no validated 3.2.0 skills available anywhere"
      break
    fi
    echo "NOTE: CE $ver present but its skills carry no supported review contract — skipping"
  done <<EOF_CANDS2
$CANDIDATES
EOF_CANDS2
fi
if [ -z "$CE" ]; then
  echo "FAIL: no compound-engineering skills with the headless contract found (checked cache >= $CE_MIN under $CE_BASE and vendor $VENDOR)"
  echo "      Newer CE versions dropped the contract this pipeline depends on — report to the maintainer (pipeline update required); do not edit skills to force a pass."
  exit 1
fi
if [ "$CE_VER" = "$CE_VALIDATED" ]; then
  echo "CE_VERSION=$CE_VER (validated baseline)"
else
  echo "CE_VERSION=$CE_VER (contract markers verified)"
fi
DEST="$ROOT/.claude/skills"
mkdir -p "$DEST"

for s in ce-code-review ce-doc-review; do
  ln -sfn "$CE/$s" "$DEST/$s"
  test -f "$DEST/$s/SKILL.md" || { echo "FAIL: staged link does not resolve: $DEST/$s"; exit 1; }
  echo "STAGED: $s -> $(readlink "$DEST/$s")"
done

# Path-local preflight assertions: the staged links must satisfy a supported
# contract end-to-end (belt on top of the source-side candidate check).
contract_ok "$DEST" || { echo "FAIL: staged skills lost the review contract (CE $CE_VER) — report to the maintainer, do not downgrade"; exit 1; }
echo "PREFLIGHT_TOKENS=OK contract=$([ -f "$DEST/ce-code-review/references/modes-and-output.md" ] && grep -q 'mode:headless' "$DEST/ce-code-review/SKILL.md" && echo headless || { grep -q 'mode:headless' "$DEST/ce-code-review/SKILL.md" && echo headless || echo agent-json; })"

# Skills this layer ships itself: how to install the stack (archon-install) and
# how to drive/supervise a lane (archon-sdlc). Same symlink seam and the same
# fail-loud discipline as the CE loop above — a broken link is a setup bug, not a
# degraded mode. These are OPERATOR-session skills; workflow nodes declare only
# ce-code-review / ce-doc-review and must never load them.
for s in archon-install archon-sdlc; do
  SRC="$ROOT/.archon/skills/$s"
  test -f "$SRC/SKILL.md" || { echo "FAIL: shipped skill missing: $SRC/SKILL.md"; exit 1; }
  ln -sfn "$SRC" "$DEST/$s"
  test -f "$DEST/$s/SKILL.md" || { echo "FAIL: staged link does not resolve: $DEST/$s"; exit 1; }
  echo "STAGED: $s -> $(readlink "$DEST/$s")"
done

# Codex skill staging (S1, codex-provider twins): codex discovers skills from
# <root>/.agents/skills, so the SAME CE sources staged into .claude/skills above
# are linked there too. Same fail-loud discipline. A pre-existing REAL directory
# at the path is removed first: `ln -sfn` into an existing dir nests the link
# inside it instead of replacing it, which would leave SKILL.md unresolvable at
# the path the codex twins' preflight asserts.
ADEST="$ROOT/.agents/skills"
mkdir -p "$ADEST" || { echo "FAIL: cannot create $ADEST"; exit 1; }
for s in ce-code-review ce-doc-review; do
  if [ -e "$ADEST/$s" ] && [ ! -L "$ADEST/$s" ]; then rm -rf "$ADEST/$s"; fi
  ln -sfn "$CE/$s" "$ADEST/$s" || { echo "FAIL: cannot stage codex link $ADEST/$s"; exit 1; }
  test -f "$ADEST/$s/SKILL.md" || { echo "FAIL: staged codex link does not resolve: $ADEST/$s"; exit 1; }
  echo "STAGED_CODEX: $s -> $(readlink "$ADEST/$s")"
done
# Validation sweep covers the codex path end-to-end, same as the .claude path.
contract_ok "$ADEST" || { echo "FAIL: codex-staged skills lost the review contract (CE $CE_VER) — report to the maintainer, do not downgrade"; exit 1; }
echo "PREFLIGHT_CODEX_TOKENS=OK"
