#!/usr/bin/env bash
# Prove a non-blocking search-quality failure is pre-existing by replaying the
# same lane with every post-RED tracked change temporarily restored to RED.
set -euo pipefail
WT="${1:?usage: eval-quality-differential.sh <worktree> <red-sha> <artifacts-dir> <fix-log>}"
RED="${2:?usage: eval-quality-differential.sh <worktree> <red-sha> <artifacts-dir> <fix-log>}"
AD="${3:?usage: eval-quality-differential.sh <worktree> <red-sha> <artifacts-dir> <fix-log>}"
FIX_LOG="${4:?usage: eval-quality-differential.sh <worktree> <red-sha> <artifacts-dir> <fix-log>}"
BASE_LOG="$AD/eval-ai-baseline.log"
PRE=$(git -C "$WT" rev-parse HEAD)
test -z "$(git -C "$WT" status --porcelain | grep -vE '^\?\? \.env$|^\?\? \.env\.e2e$|^ M pnpm-lock\.yaml$')" \
  || { echo "EVAL_DIFFERENTIAL=FAIL dirty tree"; exit 1; }
FILES=()
while IFS= read -r file; do
  test -n "$file" && FILES[${#FILES[@]}]="$file"
done < <(git -C "$WT" diff --name-only "$RED".."$PRE")
test "${#FILES[@]}" -gt 0 || { echo "EVAL_DIFFERENTIAL=FAIL no post-RED files"; exit 1; }
restore() {
  git -C "$WT" checkout "$PRE" -- "${FILES[@]}" >/dev/null 2>&1 || true
}
trap restore EXIT
git -C "$WT" checkout "$RED" -- "${FILES[@]}"
(
  cd "$WT"
  bunx jest --config jest.ai.config.js \
    apps/api-e2e/src/eval/search-corpus/search-eval.ai.spec.ts
) > "$BASE_LOG" 2>&1 || true
restore
trap - EXIT
test "$(git -C "$WT" rev-parse HEAD)" = "$PRE"
test -z "$(git -C "$WT" status --porcelain | grep -vE '^\?\? \.env$|^\?\? \.env\.e2e$|^ M pnpm-lock\.yaml$')" \
  || { echo "EVAL_DIFFERENTIAL=FAIL restore left dirty tree"; exit 1; }
python3 - "$FIX_LOG" "$BASE_LOG" <<'PY'
import re, sys

def signatures(path):
    text = open(path, encoding="utf-8", errors="replace").read()
    values = set(re.findall(r'no recorded fixture for namespace="search-eval-rerank" key=[0-9a-f]+', text))
    values.update(re.findall(r'\[search-eval quality\] regressions: [^\n]+', text))
    return sorted(values)

fix = signatures(sys.argv[1])
base = signatures(sys.argv[2])
if not fix or fix != base:
    print(f"EVAL_DIFFERENTIAL=FAIL fix={fix} baseline={base}")
    raise SystemExit(1)
print(f"EVAL_DIFFERENTIAL=PASS pre-existing-signatures={len(fix)}")
PY
