#!/usr/bin/env bash
# Local-only integration check for the repository-list Archon qualification run.
# This script is called by run-joint-integration.py from ARTIFACTS_DIR after it
# creates detached candidate worktrees and exports ARCHON_REPO_* variables.
set -euo pipefail

: "${ARCHON_REPO_API_WORKTREE:?missing api candidate worktree}"
: "${ARCHON_REPO_GOODWORD_MCP_WORKTREE:?missing goodword-mcp candidate worktree}"
: "${ARCHON_REPO_API_COMMIT:?missing api candidate commit}"
: "${ARCHON_REPO_GOODWORD_MCP_COMMIT:?missing goodword-mcp candidate commit}"

API_WT="$ARCHON_REPO_API_WORKTREE"
MCP_WT="$ARCHON_REPO_GOODWORD_MCP_WORKTREE"
API_SOURCE_WT="${ARCHON_REPO_API_SOURCE_WORKTREE:-}"
MCP_SOURCE_WT="${ARCHON_REPO_GOODWORD_MCP_SOURCE_WORKTREE:-}"
CONTRACT="$API_WT/scripts/__tests__/fixtures/repository-list-qualification-contract.json"
MCP_TEST="$MCP_WT/tests/repository-list-qualification-consumer.unit.test.ts"
EVIDENCE="$PWD/repository-list-qualification-integration.json"

reuse_node_modules() {
  local source_wt="$1"
  local candidate_wt="$2"
  if [ -n "$source_wt" ] && [ -d "$source_wt/node_modules" ] && [ ! -e "$candidate_wt/node_modules" ]; then
    ln -s "$source_wt/node_modules" "$candidate_wt/node_modules"
  fi
}

reuse_node_modules "$API_SOURCE_WT" "$API_WT"
reuse_node_modules "$MCP_SOURCE_WT" "$MCP_WT"

python3 - "$CONTRACT" "$MCP_TEST" "$ARCHON_REPO_API_COMMIT" "$ARCHON_REPO_GOODWORD_MCP_COMMIT" "$EVIDENCE" "$API_WT" "$MCP_WT" <<'PY'
import json
import hashlib
import subprocess
import sys
from pathlib import Path

contract_path = Path(sys.argv[1])
mcp_test_path = Path(sys.argv[2])
api_commit = sys.argv[3]
mcp_commit = sys.argv[4]
evidence_path = Path(sys.argv[5])
api_worktree = Path(sys.argv[6])
mcp_worktree = Path(sys.argv[7])

def git_head(worktree: Path) -> str:
    result = subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD"], text=True, capture_output=True)
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or f"cannot read git HEAD for {worktree}")
    return result.stdout.strip()

if git_head(api_worktree) != api_commit:
    raise SystemExit("API candidate HEAD does not match ARCHON_REPO_API_COMMIT")
if git_head(mcp_worktree) != mcp_commit:
    raise SystemExit("MCP candidate HEAD does not match ARCHON_REPO_GOODWORD_MCP_COMMIT")

if not contract_path.is_file():
    raise SystemExit(f"missing API qualification contract: {contract_path}")
contract = json.loads(contract_path.read_text(encoding="utf-8"))
expected = {
    "schema": "goodword.repository-list-qualification.v1",
    "producer": "api",
    "consumer": "goodword-mcp",
    "purpose": "archon repository-list qualification only",
    "network": "none",
    "production": "none",
}
for key, value in expected.items():
    if contract.get(key) != value:
        raise SystemExit(f"contract[{key!r}]={contract.get(key)!r}, expected {value!r}")
assertions = contract.get("assertions")
if not isinstance(assertions, list) or len(assertions) < 3:
    raise SystemExit("contract assertions must contain at least three local assertions")

if not mcp_test_path.is_file():
    raise SystemExit(f"missing MCP consumer qualification test: {mcp_test_path}")
mcp_test = mcp_test_path.read_text(encoding="utf-8")
for needle in (
    "ARCHON_REPO_API_WORKTREE",
    "repository-list-qualification-contract.json",
    "goodword.repository-list-qualification.v1",
    "archon repository-list qualification only",
):
    if needle not in mcp_test:
        raise SystemExit(f"MCP consumer test does not reference {needle!r}")

evidence = {
    "schema": "goodword.repository-list-qualification.integration.v1",
    "api_commit": api_commit,
    "goodword_mcp_commit": mcp_commit,
    "contract_sha256": hashlib.sha256(contract_path.read_bytes()).hexdigest(),
    "mcp_test_sha256": hashlib.sha256(mcp_test_path.read_bytes()).hexdigest(),
    "assertions": [
        "API candidate exposes the qualification-only local contract fixture",
        "MCP candidate reads the actual upstream API contract JSON through ARCHON_REPO_API_WORKTREE",
        "Integration used detached local candidate worktrees exported by run-joint-integration.py",
    ],
}
evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

(
  cd "$API_WT"
  bun run test -- scripts/__tests__/repository-list-qualification-contract.spec.ts
)

(
  cd "$MCP_WT"
  mise x node@20 -- env NODE_OPTIONS=--experimental-vm-modules pnpm exec jest \
    --testPathIgnorePatterns '\.(e2e|smoke)\.test\.ts$' \
    --testPathPatterns tests/repository-list-qualification-consumer.unit.test.ts
)

printf 'ARCHON_INTEGRATION_TESTS=3\n'
