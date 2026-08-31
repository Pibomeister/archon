#!/usr/bin/env bash
# Plan artifact shape checks, factored out of full-sdlc-api.yaml's
# plan-snapshot node so plan-converge (U1's plan-loop) can re-run the same
# checks on every ACCEPT round without duplicating the logic. Verifies
# plan.md's five headings, verify.json/files-allowlist.json/reader-audit.json
# shape, and — when the spec declares premises — that every premises.json
# evidence quote is cited verbatim in the worktree (same cited() helper as
# plan-snapshot, verbatim).
# Usage: plan-shape.sh <artifacts-dir> <worktree> <spec-path>
set -euo pipefail
AD="${1:?usage: plan-shape.sh <artifacts-dir> <worktree> <spec-path>}"
WT="${2:?usage: plan-shape.sh <artifacts-dir> <worktree> <spec-path>}"
SPEC="${3:?usage: plan-shape.sh <artifacts-dir> <worktree> <spec-path>}"

test -s "$AD/plan.md" || { echo "PLAN_SHAPE=FAIL no plan.md"; exit 1; }
for h in "## Goal" "## Files" "## Approach" "## Test scenarios" "## Verification"; do
  grep -q "^$h" "$AD/plan.md" || { echo "PLAN_SHAPE=FAIL missing $h"; exit 1; }
done
python3 -c "import json,sys; p=json.load(open(sys.argv[1]))['test_patterns']; assert isinstance(p,list) and p and all(isinstance(x,str) and x.strip() for x in p)" "$AD/verify.json" || { echo "PLAN_SHAPE=FAIL verify.json missing or empty"; exit 1; }
python3 -c "import json,sys; a=json.load(open(sys.argv[1])); assert isinstance(a,list) and a and all(isinstance(x,str) and x.strip() for x in a)" "$AD/files-allowlist.json" || { echo "PLAN_SHAPE=FAIL files-allowlist.json missing or empty"; exit 1; }
python3 -c "import json,sys; c=json.load(open(sys.argv[1]))['columns']; assert isinstance(c,list)" "$AD/reader-audit.json" || { echo "PLAN_SHAPE=FAIL reader-audit.json missing or malformed"; exit 1; }

if grep -q '^## Premises to verify' "$SPEC"; then
  python3 - "$AD/premises.json" "$WT" <<'PY' || { echo "PLAN_SHAPE=FAIL premises.json missing, empty, or uncited"; exit 1; }
import json, os, re, subprocess, sys
prem = json.load(open(sys.argv[1]))
assert isinstance(prem, list) and prem, "empty premises list though spec declares premises"
def cited(q, path):
    if subprocess.run(["grep", "-qF", q, path]).returncode == 0:
        return True
    # Verbatim modulo formatting: Prettier wraps statements across lines,
    # so a quote that is one line in the plan may be split in the source.
    # Collapse whitespace runs on both sides before the substring check.
    try:
        content = open(path, encoding="utf-8", errors="replace").read()
    except Exception:
        return False
    return bool(content) and " ".join(q.split()) in " ".join(content.split())
for p in prem:
    ev = p.get("evidence") or []
    assert ev, f"premise {p.get('id')} has no evidence"
    ok = False
    for e in ev:
        # a "path:N" / "path:N-M" suffix is a formatting habit, not a different file
        f = os.path.join(sys.argv[2], re.sub(r":[0-9]+(?:-[0-9]+)?$", "", e["file"]))
        if os.path.isfile(f) and cited(e["quote"], f):
            ok = True
            break
    assert ok, f"premise {p.get('id')}: no evidence quote found verbatim in the worktree"
PY
fi
echo "PLAN_SHAPE=OK"
