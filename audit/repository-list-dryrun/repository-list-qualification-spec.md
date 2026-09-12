# Repository-list Archon qualification fixture

This is a qualification-only Archon trial for `--scope api,goodword-mcp`. It must not implement ENG-3866, change product behavior, touch production systems, require deployed services, publish packages, open PRs, or rely on upstream merges. The intended result is a locally verified two-repository candidate that proves the repository-list controller can plan once, approve once, execute repository stages in dependency order, pass local handoffs by candidate revision, and run joint integration against disposable local worktrees.

## Goal

Add a tiny local-only test contract in `api` and a tiny local-only consumer test in `goodword-mcp`. The API stage produces a JSON fixture that represents an interface artifact. The MCP stage depends on that artifact conceptually and adds unit coverage for the expected contract shape. The joint integration stage must consume the exact local candidate revisions through `run-joint-integration.py` detached worktrees and run only local commands.

## Non-goals and hard boundaries

- Do not implement, modify, or prepare ENG-3866 product behavior.
- Do not edit production source paths outside the test-only files listed below.
- Do not add dependencies.
- Locked profile install commands may read package registries if the stock workflow needs to install existing dependencies. Runtime tests and integration commands must not call external networks.
- Do not call production APIs, deployed APIs, AWS, Linear, GitHub, package publication, or PR creation.
- Do not create a new API route or unauthenticated GET endpoint.
- Do not write `smoke-probe.json`; this fixture does not add a route. The stock API smoke may run its existing local boot check only.
- Do not expand scope beyond `api` and `goodword-mcp`.

## Required repository scope

Selected repositories are exactly:

1. `api`
2. `goodword-mcp`

Treat the list order as presentation order only. The approved plan must make `goodword-mcp` depend on `api` because MCP consumes the local contract shape produced by the API candidate.

## Required API stage

Allowed API files are exactly:

- `libs/utils/src/lib/__tests__/fixtures/repository-list-qualification-contract.json`
- `libs/utils/src/lib/__tests__/repository-list-qualification-contract.spec.ts`

Create `libs/utils/src/lib/__tests__/fixtures/repository-list-qualification-contract.json` with this exact contract shape, allowing only harmless formatting differences:

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

Create `libs/utils/src/lib/__tests__/repository-list-qualification-contract.spec.ts` to load that JSON fixture from the repository and assert every field above, including at least three assertions. The test must be deterministic, local, and must not import application services, connect to databases, read environment secrets, or call the network.

API verification commands must be exactly:

```bash
bun run typecheck
bun run lint
bun run test -- libs/utils/src/lib/__tests__/repository-list-qualification-contract.spec.ts
```

## Required goodword-mcp stage

Allowed MCP files are exactly:

- `tests/repository-list-qualification-consumer.unit.test.ts`

Create `tests/repository-list-qualification-consumer.unit.test.ts` as a local unit test that reads the actual upstream API candidate contract file. The test must fail if `ARCHON_REPO_API_WORKTREE` is missing, if `libs/utils/src/lib/__tests__/fixtures/repository-list-qualification-contract.json` is absent under that worktree, or if the file is malformed. It must load that JSON file at runtime and assert the schema string `goodword.repository-list-qualification.v1`, the phrase `archon repository-list qualification only`, producer `api`, consumer `goodword-mcp`, `network: none`, `production: none`, and at least three local assertions. Do not define an inline copy of the input object as the subject under test; inline expected scalar literals are allowed only as expected values. It must include the string `repository-list-qualification-contract.json` so the integration script can prove it is covering the intended interface artifact. It must not call the network, load secrets, or use e2e/smoke fixtures.

MCP verification commands must be exactly:

```bash
mise x node@20 -- pnpm exec tsc --noEmit
mise x node@20 -- env NODE_OPTIONS=--experimental-vm-modules pnpm exec jest --testPathIgnorePatterns '\.(e2e|smoke)\.test\.ts$' --testPathPatterns tests/repository-list-qualification-consumer.unit.test.ts
```

## Required joint plan artifact

The planning run must write `joint-plan.json` using schema `archon.joint-feature-plan.v1`. The plan must include this repository set and stage shape:

- `repositories`: `["api", "goodword-mcp"]`
- `dependency_order`: `["api", "goodword-mcp"]`
- `contracts`: one contract with producer `api`, consumer `goodword-mcp`, artifact `libs/utils/src/lib/__tests__/fixtures/repository-list-qualification-contract.json`, and a description stating it is a local qualification-only interface fixture.
- `stages.api.depends_on`: `[]`
- `stages.goodword-mcp.depends_on`: `["api"]`
- Each stage's `files_allowlist`, `test_patterns`, and `verification` must match the allowed files and commands above.

The plan must also include one integration scenario:

- `name`: `local-api-mcp-qualification-contract`
- `uses`: `["api", "goodword-mcp"]`
- `commands`: `["bash /Users/eduardopicazo/Documents/Workspace/Goodword/.archon/audit/repository-list-dryrun/run-repository-list-qualification-integration-v2.sh"]`
- `expected_tests`:
  - `API candidate exposes the qualification-only local contract fixture`
  - `MCP candidate reads the actual upstream API contract JSON through ARCHON_REPO_API_WORKTREE`
  - `Integration used detached local candidate worktrees exported by run-joint-integration.py`
- `expected_artifacts`: `["repository-list-qualification-integration.json"]`
- `command_timeout_seconds`: `1800`

The integration command must print `ARCHON_INTEGRATION_TESTS=3` only after it validates both candidate worktrees and runs both unit test selections.

## Acceptance criteria

The run is acceptable only if it finishes as `locally_verified` with publication held. Required evidence:

- One approved joint plan for exactly `api` and `goodword-mcp`.
- API stage candidate commit containing only the two allowed API files.
- MCP stage candidate commit containing only the one allowed MCP file.
- MCP stage starts only after the API stage is verified.
- Integration evidence records the exact candidate heads for both repositories.
- `integration-evidence.json` reports status `passed`, `tests_passed` at least `3`, and candidate heads matching the verified stage candidates.

If any command wants production credentials, a deployed URL, package publication, GitHub, Linear, or additional repository scope, stop and report a blocked qualification plan instead of continuing.
