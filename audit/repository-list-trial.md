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

### 2026-09-12: authorized shared-budget retry preflight

The operator authorized a **100,000,000 total-token ceiling**, including the
30,154,003 tokens already consumed, for the same chain and run above. The
240-active-minute ceiling still includes 5,031 consumed seconds. Review may use
eight total rounds while retaining round four and all findings; no findings or
human approval requirements are waived. The stopping milestone is the reviewed
joint plan packet, before product implementation.

Read-only preflight confirmed the specification SHA-256 remains
`aefdbc3dd38d1ef13112eb5e573890ccf207b2d0144e393fa7f0cec0925827de`;
API remains at `c13a1f484737842fdce76501fca20107c1ace5aa` and MCP at
`a2e63d9f8f00db188a3142ca4ea727aba806db9d`. Both chain worktrees are clean.
The run is failed, its launcher/watchdog process groups have no members, and
the latest operator authority validates. The captured workflow digest remains
`3e00720cbcaf2565804b25eac6556425837c72929d9d86f530c5f739f5843e3f`;
the captured critic and dedicated runtime use Sol, with medium runtime effort.
Private pre-amendment evidence preserves the entire ledger for comparison.

The guarded amendment applied successfully. Chain, ledger, ledger usage summary,
private control, and public control all show 100,000,000 tokens. Historical run
bindings, active intervals, aggregate/session token high-water marks, 30,154,003
consumed tokens, and 5,031 consumed seconds are unchanged. Remaining allowance
before restart: 69,845,997 tokens and 9,369 active seconds. Approval and candidate
handoffs remain absent. `plan-round-cap.txt` is now eight; the counter stayed four
until normal resume advanced it to five. The same run resumed on Sol/medium with
the captured source retained, and the controller supervisor emitted updated
forecasts. The initial forecast is sufficient, with low confidence.

Harness verification: 125 feature tests, 59 launcher tests, 19 control tests,
16 watchdog tests, 10 package-manifest tests, and Python compilation passed.
Amendment cases include all six persisted-write interruption boundaries with
nonzero usage, actual concurrent requests, and surviving process-group members
after leader exit. The real supervisor/accounting/forecast path is exercised.
Packaged helpers, skill, and runbook match the source in both payload and gist
layouts. Dry packaging still stops at the pre-existing all-zero example-hash
secret gate; it was not weakened, and nothing was published.
Continuations now refresh their allowances from chain state under its lock and
retain those values in prearm-failure recovery, preventing a continuation that
read the old allowance before amendment from restoring that stale ceiling.

Final retry outcome: **blocked before joint approval**. The captured workflow
allows three loop iterations per invocation, so the first resume completed
rounds five through seven and stopped at that invocation limit. A second guarded
resume of the same run executed the already-authorized eighth round. Its critic
returned three P1 findings; the reviser reported all three applied and none
declined, then the gate stopped with **`PLAN_ROUND_CAP round=8 cap=8`**. The latest
revisions have not received independent acceptance, so there is no current
approval-ready packet. No ninth round was attempted and no findings were waived.

The final review topics are canonical links inconsistent with the public route's
Postgres prerequisites, genuine `/me` subscription/authentication denials being
lost through MCP normalization/gating, and executable GSI readiness before the API
accepts traffic. The final revision adds orphan-metadata repair rules, the MCP
subscription gate and tests, and candidate-owned pre-listen index readiness.
Those proposed resolutions remain subject to independent review.

Settled accounting after stopped-process verification: **45,486,481 / 100,000,000
tokens** and **7,451 / 14,400 active seconds**, across the same two runs and 14
recorded sessions. Remaining: **54,513,519 tokens and 6,949 seconds (115m 49s)**.
Both hard limits are clear. Launcher and watchdog groups have exited, both
product worktrees remain clean at the original baselines, the specification and
captured workflow/model bindings are unchanged, approval is absent, and every
implementation stage remains pending. There is no `locally_verified` delivery.

Current evidence is under
`~/.archon/workspaces/_local/Goodword/artifacts/runs/b7909dbc-8d91-41f0-94c6-0e28755a2907/`:
`plan.md`, `joint-plan.json`, `plan-round-8/critique.json`,
`plan-round-8/revision.json`, and `plan-round-8/converge.txt`.
The current operator token validates against the protected
`/tmp/archon-eng3866-budget-retry-round8.log`; its value is not copied here.
Further review beyond eight requires new authorization. Product implementation
still requires the ticket-specific reviewed joint approval; neither another cap
increase nor acceptance of findings is authorized by this retry.

Harness files changed for this retry: `setup/archon-run.py`,
`setup/feature_chain.py`, `setup/feature-budget.py`, `setup/control_contract.py`,
`setup/tests/test_archon_run.py`, `setup/tests/test_feature_budget.py`,
`setup/tests/test_feature_budget_update.py`, `setup/tests/test_feature_chain_v2.py`,
`setup/tests/test_feature_prearm_recovery.py`, `skills/archon-sdlc/SKILL.md`,
`RUNBOOK.md`, `VERSION`, and this audit. The implementation reuses the existing
controller, ledger, lock, and secure serialization helpers. Stock Archon is
unchanged; generated payload/gist helpers are refreshed with no publication.

### 2026-09-12: autonomous continuation through PR creation

After the eight-round stop, the user explicitly directed: “continue until the PR
is created” and requested autonomous completion of the whole run. This supersedes
the prior stop-at-eight, stop-at-human-plan-gate, and publication-hold instructions
for ENG-3866. Planning must still pass review, implementation must follow its
sealed plan, and local integration must pass before publication. Findings are not
automatically accepted. Production access and merging remain outside scope.

The same stopped run resumes from round eight with its existing revisions. The
operational review cap is now twelve; further review batches may continue under
this instruction while actual shared accounting remains enforced. The current
100M-token and 240-active-minute hard limits are unchanged. The controller's normal
approval and publication commands will be used after their prerequisite checks,
under this explicit user direction, rather than editing approval evidence.

During continued review, official AWS documentation disproved the proposed
`dynamodb:TransactWriteItems` IAM action. Cited supplemental facts were supplied
through the run-local `AGENTS.md`, protected read-only by the wrapper; round twelve
then raised the invalid permission premise through the normal critic contract.
The same review corrected a projected-read capacity claim and required retained
integration evidence rather than a count alone. No production account operation
was used to obtain these facts.

Session inspection also found a general isolation defect: the adapter resumed
one Codex conversation for impact, critic, and reviser nodes. The 11:02:55 session
`01a09692-7455-7b03-9e1f-d232497c3f68` contains three critic/reviser pairs. Thus
earlier claims of independent review are limited: only the first critic after a
fresh invocation lacked that invocation's reviser history.

The repository-list wrapper now strips the adapter's supported `resume <uuid>`
selector, preserves model/effort and other option values, and refuses ambiguous
or history-forking selectors. Every node continues from artifacts in a fresh
conversation and remains in the same accounting ledger. Sixteen wrapper tests
and a separate code review pass. The private wrapper was installed atomically;
the live round-twelve reviser is now session
`01a096bc-5db4-78b2-9d81-594dd11fd125`, separate from critic session
`01a096b2-c39f-7442-ab24-16e0f8a7dbd6`. Both record `gpt-5.6-sol` / `medium`.
Stock Archon and the captured workflow source are unchanged.

### Autonomous goal resource stop

The user-authorized autonomous continuation proceeded through round eighteen,
using guarded resumes when the captured workflow exhausted its three iterations
per invocation. The operational round cap is twenty. Independent fresh sessions
and cited operator evidence corrected invalid IAM and row-lock premises, restored
read-only MCP reuse, and kept unsupported mixed-binary deployment guarantees
outside the candidate-local verification claim. An architecture audit did not
establish a lower-total-scope replacement that also solved legacy discovery, so
the current design was retained.

The controller supervisor then enforced the original active-time ceiling:
`FEATURE_CHAIN=FAIL feature shared wall budget is exhausted`. Forecast warnings
had continued normally; this stop was actual exhaustion. Settled usage is
**82,903,204 / 100,000,000 tokens** and **14,403 / 14,400 active seconds**, across
two runs and 39 sessions. The three-second observation/containment overrun is
recorded, not reset. Both controlled process groups exited. Both product worktrees
remain clean at their original baselines; approval and implementation are absent,
and no PR has been created.

Round eighteen's critique is complete, but its revision was interrupted before
`revision.json` was produced. Remaining review topics: reserved-group denial in
later readers, preserving the limited expired-share response, and the recipient
authorization boundary for invite redemption. Current plan SHA-256:
`00902e317c8f20ae12d2ebfd14ff691d6d876296a5eea1c760cf4f9d9419b6c4`;
joint-plan SHA-256:
`8c754440b4e2279c45b6d92abc8011b9e41cded1d73332569dc28b07394b406c`.

The resource question requesting authority for up to 200M total tokens and 480
active minutes remains unanswered. No increase was applied. A guarded optional
`--total-active-minutes` extension is prepared in an isolated, reviewed patch at
`/tmp/archon-active-amend.U5yVTE/optional-active-minutes-amendment.patch`.
It preserves old token-only amendment IDs and all consumed intervals; 134 feature
tests pass in its isolated tree, and review is clear after a legacy-idempotency
fix. It is not installed in the live launcher. Apply and verify it only after
the required resource authorization, then amend the stopped chain through the
guarded command and resume the same run.

The latest valid operator token is in the protected
`/tmp/archon-eng3866-autonomous-review18.log`; its value is not copied here.
This is the first goal-turn observation of the resource-authorization blocker.
The PR goal remains unfinished.

Second goal-turn resource audit: the run is still failed, both process groups
are absent, the 14,400-second ceiling is unchanged/exhausted, and no resource
authorization has arrived. The reviewed optional-active-minute extension has now
been installed in the live launcher as a safe preparation step; no amendment
command was executed and no private allowance changed. Its public CLI exposes
`--total-active-minutes`, preserving token-only amendment identity. The run can
continue only after the pending resource decision. This observation does not
claim PR creation or completed delivery.
Verification in the live tree passed 134 feature tests, Python compilation,
CLI help, and diff checks. Updated launcher/controller/ledger/skill/runbook
payload and gist copies match source; full packaging still encounters the same
pre-existing all-zero example-hash secret-scan failure. Live allowances and
usage were rechecked: 100M tokens, 240 minutes, 82,903,204 consumed tokens and
14,403 consumed seconds, unchanged by installing the command extension.

Third consecutive goal-turn resource audit confirmed the same terminal run,
absent launcher/watchdog groups, exhausted unchanged 14,400-second ceiling, no
approval, pending implementation stages, and no publication. No resource
authorization arrived. The goal is blocked on that specific user decision;
the PR objective is not complete. The extension is installed and verified, so
the next authorized action is the guarded ceiling amendment, not more tooling
preparation or a chain reset.

### Authorized resource extension and resumed PR goal

The user answered the pending resource question: **“Allow up to 200M / 480
minutes if needed.”** The guarded amendment was applied with those total ceilings
and verified against a private pre-amendment ledger snapshot. All run bindings,
active intervals, and aggregate/session high-water marks are unchanged. Consumed
usage remains 82,903,204 tokens and 14,403 seconds; remaining allowance is
117,096,796 tokens and 14,397 seconds (239m 57s). Chain, ledger, usage summary,
private control, and public control agree on the new ceilings.

The same run resumes using `/tmp/archon-eng3866-authorized-200m-review.log` for
protected operator output. Round eighteen's interrupted critique is explicitly
identified in the run-local operator evidence so subsequent review must assess
its still-open findings. The resource blocker is resolved; no further routine
planning/implementation/publication authorization is required for this ticket.


### Joint plan accepted; provider-hook incident contained

Planning round 21 accepted with zero findings, including explicit disposition
of the interrupted reader, expired-response, and invite-redemption findings.
Final doc review required no edits. Operator inspection identified one missing
source allowance for the already-required IntroRequestService caller changes.
Guarded rejection produced only that source allowance and its matching plan
mapping, plus the rendered packet. Joint-plan validation passes and both product
worktrees remain clean at their pinned baselines.

The stock rejection hook unexpectedly used Claude instead of Codex/Sol. It had
already completed when authenticated containment checked its process groups;
no process was killed. Its completed event
`d2f8f63a-2b7d-4023-9209-d56851b28e02` and exact session transcript
`174ef717-b145-4f30-b861-f79bbc4c226c` independently report 977,526 tokens:
964,761 inclusive input (290 uncached, 123,768 cache creation, 840,703 cache read)
and 12,765 output. The 39 usage rows deduplicate to ten assistant messages;
all 26 tool IDs belong to this run, with no child-agent invocation evidence.
Transcript SHA-256:
`db6edea1cb2d9deab0dca430f096f66330e1dac9cc58714e63c71a476b53338e`.
The existing active interval already includes the hook duration.

Recovery adds a guarded, independently reconciled private provider receipt and
an unexpected-provider denial executable. Stock approval-hook scoped provider
and model keys are ignored by the validator, so those keys were not added.
Captured workflow source and stock Archon remain unchanged. The current token
is held only in protected `/tmp/archon-eng3866-plan-contract-correction2.log`.
The run remains paused pending receipt verification and guarded approval under
the user's autonomous-through-PR authorization.


Provider recovery passed independent code review and focused tests (62 launcher,
23 ledger, 13 amendment/controller, 11 estimate/receipt CLI). Guarded live receipt
import was repeated: exactly one receipt persisted, all historical ledger fields
unchanged during import. The immediate refreshed accounting added exactly
977,526 tokens. Normal interval recovery finalized 30 previously unrefreshed
seconds; no receipt time was added. Before approval the total was 96,885,117
tokens and 17,436 active seconds, under 200M/28,800s.
The corrected joint plan passed structural validation and its user-authorized
guarded approval succeeded. The same chain automatically dispatched API stage
`174a229b-0866-482f-bb33-3c5cce536793`; implementation is now running. New stage
sessions naturally extend the preserved session ledger. Protected operator log:
`/tmp/archon-eng3866-approved-implementation.log`.
