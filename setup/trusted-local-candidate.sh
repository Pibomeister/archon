#!/usr/bin/env bash
# Final trusted local candidate verifier for repository feature stages.
set -euo pipefail
AD="${1:?usage: trusted-local-candidate.sh <artifacts-dir>}"
SETUP="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
eval "$(bash "$SETUP/params-env.sh" "$AD/params.json")"
# A plain `archon workflow resume` does not carry the launcher's ARCHON_FEATURE_* env
# (observed 2026-09-13, run ffd88016); params.json is the durable copy of both values.
if [ -z "${ARCHON_FEATURE_SCOPE-}" ]; then
  ARCHON_FEATURE_SCOPE=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("feature_scope",""))' "$AD/params.json")
  ARCHON_FEATURE_PHASE=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("feature_phase",""))' "$AD/params.json")
  export ARCHON_FEATURE_SCOPE ARCHON_FEATURE_PHASE
fi
test "${ARCHON_FEATURE_SCOPE-}" = repositories || { echo "LOCAL_CANDIDATE=FAIL missing repository feature scope"; exit 1; }
test "${ARCHON_FEATURE_PHASE-}" = implement || { echo "LOCAL_CANDIDATE=FAIL phase is not implement"; exit 1; }
cd "$WT"
BASE=$(cat "$AD/bootstrap-head.txt")
CANDIDATE_COMMITS=$(git rev-list --count "$BASE"..HEAD)
if ! test -s "$AD/commit-msg.txt"; then
  echo "CANDIDATE_SQUASH=SKIP no commit-msg.txt"
elif test "$CANDIDATE_COMMITS" -eq 0; then
  echo "CANDIDATE_SQUASH=SKIP no commits"
elif test "$CANDIDATE_COMMITS" -eq 1 \
  && test "$(git log -1 --format=%s)" = "$(head -n 1 "$AD/commit-msg.txt")"; then
  echo "CANDIDATE_SQUASH=SKIP already squashed"
else
  TREE_BEFORE=$(git rev-parse 'HEAD^{tree}')
  git reset --soft "$BASE"
  git commit -q -F "$AD/commit-msg.txt"
  test "$(git rev-parse 'HEAD^{tree}')" = "$TREE_BEFORE" || { echo "CANDIDATE_SQUASH=FAIL tree changed"; exit 1; }
  echo "CANDIDATE_SQUASH=OK commits=1 head=$(git rev-parse HEAD)"
fi
CANDIDATE_HEAD=$(git rev-parse HEAD)
# A --verify-only reopen re-verifies a hand fix that is already in this worktree.
# An unchanged head means the hand fix never landed, so there is nothing to verify.
VERIFY_ONLY=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("feature_verify_only",""))' "$AD/params.json")
if [ "$VERIFY_ONLY" = yes ]; then
  PREVIOUS_HEAD=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("feature_previous_head",""))' "$AD/params.json")
  test "$PREVIOUS_HEAD" != "$CANDIDATE_HEAD" || { echo "REOPEN=NOOP head=$CANDIDATE_HEAD"; exit 1; }
fi
python3 "$SETUP/check-scope.py" "$AD/files-allowlist.json" "$WT" "$BASE" \
  || { echo "LOCAL_CANDIDATE=FAIL scope breach"; exit 1; }
VERIFY_LOG="$AD/local-candidate-verify.log"
: > "$VERIFY_LOG"
(
  echo "LOCAL_CANDIDATE_VERIFY typecheck"
  "${CMD_TYPECHECK[@]}" || exit 1
  if [ "${#CMD_LINT[@]}" -gt 0 ]; then
    echo "LOCAL_CANDIDATE_VERIFY lint"
    "${CMD_LINT[@]}" || exit 1
  else
    echo "LOCAL_CANDIDATE_VERIFY lint=not_applicable repo=$REPO"
  fi
  python3 -c "import json,sys; [print(p) for p in json.load(open(sys.argv[1]))['test_patterns']]" \
    "$AD/verify.json" > "$AD/local-candidate-patterns.txt" || exit 1
  test -s "$AD/local-candidate-patterns.txt"
) >> "$VERIFY_LOG" 2>&1 || { cat "$VERIFY_LOG"; echo "LOCAL_CANDIDATE=FAIL pre-test verification"; exit 1; }
INDEX=0
while IFS= read -r PAT <&3; do
  test -n "$PAT" || continue
  INDEX=$((INDEX + 1))
  REPORT="$AD/local-candidate-test-$INDEX.json"
  rm -f "$REPORT"
  {
    echo "LOCAL_CANDIDATE_VERIFY unit pattern=$PAT report=$REPORT"
    case "$REPO" in
      api|goodword-mcp)
        "${CMD_TEST[@]}" "$PAT" --json --outputFile "$REPORT" </dev/null 3<&-
        ;;
      web-app)
        "${CMD_TEST[@]}" "$PAT" --reporter=json --outputFile "$REPORT" </dev/null 3<&-
        ;;
      *)
        echo "LOCAL_CANDIDATE_VERIFY unsupported repo=$REPO"
        exit 1
        ;;
    esac
  } >> "$VERIFY_LOG" 2>&1 || { cat "$VERIFY_LOG"; echo "LOCAL_CANDIDATE=FAIL unit pattern=$PAT"; exit 1; }
done 3< "$AD/local-candidate-patterns.txt"
test "$INDEX" -gt 0 || { echo "LOCAL_CANDIDATE=FAIL no test patterns recorded"; exit 1; }
test -s "$AD/smoke-result.txt" || { echo "LOCAL_CANDIDATE=FAIL missing smoke result"; exit 1; }
INTERFACE_OUTPUT_DIR="$AD/interface-export"
rm -rf "$INTERFACE_OUTPUT_DIR"
mkdir -p "$INTERFACE_OUTPUT_DIR"
ARCHON_INTERFACE_OUTPUT_DIR="$INTERFACE_OUTPUT_DIR" \
  python3 "$SETUP/write-local-candidate.py" --run-interface-exports \
  "$AD" "$REPO" "$WT" "$INTERFACE_OUTPUT_DIR" \
  || { echo "LOCAL_CANDIDATE=FAIL interface export"; exit 1; }
test -z "$(git status --porcelain --untracked-files=all)" || { echo "LOCAL_CANDIDATE=FAIL dirty worktree"; git status --porcelain --untracked-files=all; exit 1; }
test "$(git rev-parse HEAD)" = "$CANDIDATE_HEAD" || { echo "LOCAL_CANDIDATE=FAIL candidate head changed during export"; exit 1; }
ARCHON_INTERFACE_OUTPUT_DIR="$INTERFACE_OUTPUT_DIR" \
  python3 "$SETUP/write-local-candidate.py" \
  "$AD" "$REPO" "$BASE" "$CANDIDATE_HEAD"
echo "LOCAL_CANDIDATE=PASS repo=$REPO head=$CANDIDATE_HEAD publication=held"
