# Supervised repository-list qualification trial

Date: 2026-09-11

## Verdict

`READY_TO_LAUNCH_TO_HUMAN_GATE`, not launched. The disposable qualification spec, expected joint-plan skeleton, and local integration script are prepared for a supervised Sol/medium repository-list run over `api` and `goodword-mcp`. No paid AI, approval, production access, push, PR, package publication, Linear write, or ENG-3866 implementation was run while preparing this artifact.

## Trial objective

Exercise the stock generated `full-sdlc-api-codex` repository-list path with one joint plan and two repository stages:

1. Planning reads `api` and `goodword-mcp`, writes one joint plan, and pauses at the human approval gate.
2. After human approval only, API creates a local qualification-only JSON contract fixture plus a unit test.
3. MCP depends on the API stage and creates a local unit test for the same contract shape.
4. Joint integration consumes the exact local candidate worktrees exported by `run-joint-integration.py`, runs the two unit test selections, writes evidence, and prints `ARCHON_INTEGRATION_TESTS=3`.

This is not a product feature. It is a harness qualification run.

## Prepared artifacts

- Spec to launch: `.archon/audit/repository-list-dryrun/repository-list-qualification-spec.md`
- Expected plan skeleton: `.archon/audit/repository-list-dryrun/repository-list-qualification-joint-plan.example.json`
- Integration command script: `.archon/audit/repository-list-dryrun/run-repository-list-qualification-integration.sh`

Validation already run:

```text
python3 .archon/setup/validate-joint-plan.py <temp-artifacts>
JOINT_PLAN=PASS repositories=api,goodword-mcp order=api,goodword-mcp

bash -n .archon/audit/repository-list-dryrun/run-repository-list-qualification-integration.sh
# exit 0
```

Local Codex model evidence immediately before preparation:

```text
model_reasoning_effort = "medium"
model = "gpt-5.6-sol"
```

Recheck those two lines immediately before launch because they are local mutable configuration.

## Exact launch command, held

Run only when the current root tests/checks are green and a human operator is ready to supervise the planning pause. This launches paid Codex work.

```bash
cd /Users/eduardopicazo/Documents/Workspace/Goodword
DISABLE_OMC=1 python3 .archon/setup/archon-run.py \
  --wall-minutes 240 \
  --max-total-tokens 30000000 \
  feature \
  --provider codex \
  --scope api,goodword-mcp \
  /Users/eduardopicazo/Documents/Workspace/Goodword/.archon/audit/repository-list-dryrun/repository-list-qualification-spec.md
```

The global budget flags are intentionally before the `feature` subcommand. `archon-run.py feature --help` shows `--wall-minutes` and `--max-total-tokens` are global parser options, not feature-subparser options.

## Human gate path

At the first planning pause, render the plan packet and stop. Do not approve automatically. The human should inspect at least these fields before approval:

- `joint-plan.json` repositories are exactly `["api", "goodword-mcp"]`.
- `dependency_order` is `["api", "goodword-mcp"]`.
- `contracts[0]` has producer `api`, consumer `goodword-mcp`, and artifact `scripts/__tests__/fixtures/repository-list-qualification-contract.json`.
- API `files_allowlist` contains only:
  - `scripts/__tests__/fixtures/repository-list-qualification-contract.json`
  - `scripts/__tests__/repository-list-qualification-contract.spec.ts`
- MCP `files_allowlist` contains only:
  - `tests/repository-list-qualification-consumer.unit.test.ts`
- Integration command is exactly:
  - `bash /Users/eduardopicazo/Documents/Workspace/Goodword/.archon/audit/repository-list-dryrun/run-repository-list-qualification-integration.sh`
- No `smoke-probe.json` is proposed because this trial adds no route.
- No production, deployment, package publishing, PR, GitHub, Linear, AWS, or ENG-3866 product work is proposed.
- Locked profile install commands may read package registries if the stock workflow needs existing dependencies; runtime tests and integration commands must not call external networks.

Only after human approval should the operator use the token from the latest `CODEX_LITE_RUN=STARTED` line:

```bash
DISABLE_OMC=1 python3 .archon/setup/archon-run.py approve <run-id> --token <CONTROL_TOKEN_FROM_LAST_LAUNCH>
```

Agents must not run `approve`, `reject`, or `abandon` themselves.

## Expected implementation files

API candidate may touch only:

```text
api/scripts/__tests__/fixtures/repository-list-qualification-contract.json
api/scripts/__tests__/repository-list-qualification-contract.spec.ts
```

MCP candidate may touch only:

```text
goodword-mcp/tests/repository-list-qualification-consumer.unit.test.ts
```

That MCP test must read the API candidate file from `process.env.ARCHON_REPO_API_WORKTREE + "/scripts/__tests__/fixtures/repository-list-qualification-contract.json"` and fail closed when the env var or file is missing.

The API JSON contract must be local-only and contain:

```json
{
  "schema": "goodword.repository-list-qualification.v1",
  "producer": "api",
  "consumer": "goodword-mcp",
  "purpose": "archon repository-list qualification only",
  "network": "none",
  "production": "none",
  "assertions": [
    "joint planning owns both selected repositories",
    "downstream work consumes a local upstream candidate artifact",
    "integration verification runs against detached local candidate worktrees"
  ]
}
```

## Expected verification commands

These commands match `.archon/setup/repo-profile.sh` for the selected repositories.

API:

```bash
bun run typecheck
bun run lint
bun run test -- scripts/__tests__/repository-list-qualification-contract.spec.ts
```

MCP:

```bash
mise x node@20 -- pnpm exec tsc --noEmit
mise x node@20 -- env NODE_OPTIONS=--experimental-vm-modules pnpm exec jest --testPathIgnorePatterns '\.(e2e|smoke)\.test\.ts$' --testPathPatterns tests/repository-list-qualification-consumer.unit.test.ts
```

Joint integration:

```bash
bash /Users/eduardopicazo/Documents/Workspace/Goodword/.archon/audit/repository-list-dryrun/run-repository-list-qualification-integration.sh
```

The integration script expects `run-joint-integration.py` to provide these environment variables for detached local candidate worktrees:

```text
ARCHON_REPO_API_WORKTREE
ARCHON_REPO_API_COMMIT
ARCHON_REPO_API_SOURCE_WORKTREE
ARCHON_REPO_GOODWORD_MCP_WORKTREE
ARCHON_REPO_GOODWORD_MCP_COMMIT
ARCHON_REPO_GOODWORD_MCP_SOURCE_WORKTREE
```

The `SOURCE_WORKTREE` variables are used only to symlink existing `node_modules` into disposable detached worktrees when available, avoiding install/network work during integration.

It writes `repository-list-qualification-integration.json` into the integration artifacts directory and prints `ARCHON_INTEGRATION_TESTS=3` only after:

1. The API candidate contract file exists and has the required local-only fields.
2. The MCP candidate test file exists, reads the actual upstream API JSON through `ARCHON_REPO_API_WORKTREE`, and references the intended contract file.
3. The detached candidate worktree HEADs match the exported candidate commit variables.
4. The API and MCP unit selections pass in the detached candidate worktrees.

## Smoke and network safety

`api` declares `HAS_SMOKE=1`, so the full API DAG may still run its stock local boot smoke. This fixture must not add `smoke-probe.json`; without a new route, there is no extra probe. If stock local smoke fails because local boot dependencies are unavailable, treat that as a qualification environment failure, not as product verification success. Do not replace it with a production URL or deployed API.

`goodword-mcp` declares no smoke and the MCP test command explicitly ignores `.e2e.test.ts` and `.smoke.test.ts` suites.

## Success condition

The supervised trial passes only if final state is `locally_verified` with publication held and `integration-evidence.json` reports:

- `status: "passed"`
- `counters.tests_passed >= 3`
- `candidate_heads.api` equals the verified API stage candidate head
- `candidate_heads.goodword-mcp` equals the verified MCP stage candidate head
- `publication: held` appears in the final repository-list receipt

Any failed run, skipped verification, missing artifact, zero-test success, publication attempt, scope expansion, or production dependency is a failed qualification.

### 2026-09-11: executable contracts and supervised ENG-3866 refinement

The ENG-3866 planning run `ae226aa6-a51e-4798-8d45-1427ec5ea281`
reached its review cap without approval. Its plan exposed general harness gaps:
interface exports had no execution hook, integration commands guessed cwd and
candidate variables, and client error normalization was not traced in review.

The shared harness now supports approved `export.argv` into
`ARCHON_INTERFACE_OUTPUT_DIR`, copies/hashes declared outputs, rejects stale
outputs and candidate mutations, and executes structured integration `{repo,argv}`
in exact disposable candidate worktrees under the existing process containment.
New/replanned chains enforce that contract from private state; untouched legacy
chains preserve their original shell semantics. Joint contracts are included in
review snapshots and mutation checks. Planner/critic guidance now checks export
lifecycle and end-to-end client transformations.

`feature-replan` retains copied, hashed prior plan/review evidence in private
state and supplies a read-only `prior-planning-evidence.json`; dispatch retries
retain generation and budget. Tests cover legacy history, stale controls,
parameter downgrade attempts, and approval binding.

Settled verification: controller 34 tests, integration 18, candidate/export 15,
repository params/validator 68, wrapper 11, budget 15, pre-arm recovery 4, launcher
integration 2, plan shape 11, plan accept 4, fullstack 18, lite derivation 16,
Codex derivation 19. Workflow validation has no errors; generated twins match.
Package copies match source, but the existing secret gate still rejects the
all-zero hashes in machine-binding.example.json. No publication occurred.

Supervised Sol/medium successor: `b7909dbc-8d91-41f0-94c6-0e28755a2907`,
same chain `db76629f29455fb41f7ae880d6a5492f`, same worktrees and shared limits.
At restart the ledger retained 2,507 active seconds and 14,817,413 tokens.
The successor read prior evidence and entered joint planning. This entry does
not claim approval, implementation, integration completion, or qualification.

Final supervised outcome: the successor completed three revision rounds (3, 4,
and 3 findings applied respectively), then resumed the same run for a fourth
independent review within the unchanged shared cap. Round four was stopped by
`WATCHDOG=TRIPPED reason=shared-tokens`: 30,154,003 cumulative tokens against
30,000,000, across 2 runs and 10 recorded sessions, with 5,031 active seconds.
The small overshoot occurred between accounting observations. The exact launcher
process group exited on TERM; no group members remained. Workflow status is
`failed`, approval is absent, candidate handoffs are empty, and both product
worktrees are clean. No product implementation or local integration occurred.
The final plan edits are preserved but have not passed independent review.

Review also repeatedly confused attached planning worktrees with integration
fixtures. The shared validator now reports its runtime guarantee explicitly,
and the parent critic prompt points to `run-joint-integration.py` for provenance
semantics. Structured commands already execute in detached candidate fixtures;
no run-specific plan edits were used to waive this check. Generated twins and
packaged source copies were refreshed and parity checked after this clarification.

Continuation requires an explicitly authorized increase to the chain's shared
token ceiling; do not reset the ledger or create an unrelated replacement chain
as a workaround. The latest operator token remains in its protected launch log,
not this audit document. No publication or external writes occurred.
