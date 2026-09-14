# ENG-3866 risk-delta pilot boundary

The delta runtime, controller-produced state, independent session binding,
verification receipts, and guarded source-successor path are implemented.
`risk-delta-v1` is not qualified and must not dispatch a production review yet.
The comparator's successful synthetic unit fixtures are not measured token
savings or delivery approvals. Qualified activation selects an explicitly
journaled successor while preserving the original captured source bytes.

Offline verification passed 196 feature-controller tests, 63 review-helper and
review-workflow tests, and 72 launcher tests. Python compilation, shell syntax,
and diff whitespace checks passed. Independent code review found an aggregate
qualification-fixture bypass; it was fixed and re-reviewed without remaining
blocking findings. The prearm recovery fixture now includes the required
private Sol/medium config so it reaches the control paths it tests.

Remaining work includes historical fixture/cost reconstruction, actual replay
measurements and qualification, historical coverage
import with truthful provenance, and API/MCP delivery. No live pilot approval
is implied by the offline checks.

## Executable delta loop

`review_delta_workflow.py` changes only the captured review-loop subtree. New
`delta-*` node identities prevent legacy completed node outputs from skipping
the new preparation/gates on resume. Conditional reviewers execute in batches
of at most three; each has a trusted bash checkpoint immediately afterward.
The wrapper records each actual Codex `thread.started` event in a private signed
session receipt. Reviewer-written identity labels cannot supply that authority.

`review_delta_runtime.py prepare` chooses the newest fully completed segment
head, with open findings retained. It writes that exact base/head diff to the
round artifact. Interrupted same-candidate/context assignments reuse completed
checkpoints. `converge` cannot emit the convergence promise for an unreviewed
head, unresolved mandatory findings, or missing/stale verification evidence.
If legacy review summaries exist but coverage has not been imported, preparation
stops with `HISTORICAL_COVERAGE_IMPORT_REQUIRED`. It does not silently restart a
full review from an empty new ledger.

`review_verification.py` runs approved commands in the exact worktree and retains
private signed receipts. Successful command receipts are reused only for the
same candidate, argv, installed dependencies, configuration, and environment.
Failed attempts retain separate output and are never reused as passing checks.
The controller refreshes protected review inputs and receipt expectations while
preserving completed coverage and the findings ledger. Worker permissions keep
review state and checkpoints read-only; trusted gates update them.

Current verification: 204 feature tests, 90 review tests, and 72 launcher tests
passed. The final policy-amendment suite passed 16 tests after the conservative
scope correction. Python compilation, shell syntax, and diff whitespace checks
passed. Independent focused review resolved the controller-state, session
provenance, receipt-import, and specialist-coverage findings. The live API
worktree remains clean at `c99b307a9506202d86c82ebac4f895f59efa0d0a`.

## Preserved run evidence

Read-only inspection on 2026-09-14 identified the target Codex chain as
`db76629f29455fb41f7ae880d6a5492f`, current run
`174a229b-0866-482f-bb33-3c5cce536793` (`failed`). The API worktree remains at
`c99b307a9506202d86c82ebac4f895f59efa0d0a`. Other ENG-3866 chains also exist;
they are not substitutes for this run.

The private ledger's last recorded usage is 350,394,151 total tokens against a
350,000,000 ceiling. Input is 348,662,969, including 333,235,327 cached tokens;
output is 1,731,182. Cached input is already included in input. Active usage is
33,166 seconds against 36,000. No allowance, approval, run artifact, worktree,
captured workflow, or ledger was changed by the offline harness work.

Captured source provenance is in the SQLite run metadata's `workflow_source`,
with digest `9abd5a9d245ca200a85c5a00bcfea98f7f1d117757b235e38900c773715cb423`.
The controller must validate the captured manifest/files, not trust a digest
supplied by the operator without comparison to the run's actual metadata.

Round six covers base `c13a1f484737842fdce76501fca20107c1ace5aa` through
`797b5b98bc89a6453ea143b90e41316fe17bd259`: 3,955 production lines and 6,330
total changed lines. Its gate passed and its verdict was Not ready, with three
blockers. Its envelope reports sequential orchestrator fallback after child
dispatch failed. Retain that actual provenance; do not invent separate reviewer
identities or silently convert it into the new policy's two-reviewer baseline.
Importing historical coverage requires an explicit provenance assessment.
Wrong-model and interrupted reviews remain evidence, never approvals.

The latest repair commit fixes malformed canonical-claim fallback disclosure,
recipient resurrection from an unlocked snapshot, and reconciliation audit-ID
reuse. Independent closure and defect-specific committed regression evidence
are still required.

## Qualification evidence to collect

Historical run artifacts live under
`~/.archon/workspaces/_local/Goodword/artifacts/runs/174a229b-0866-482f-bb33-3c5cce536793/`.

| Fixture | Historical evidence |
|---|---|
| Malformed claim disclosure | `round-6/review-envelope.txt`, sharedlinks repository |
| Recipient resurrection | `round-6/review-envelope.txt`, group service |
| Reconciliation audit-ID reuse | `round-6/review-envelope.txt`, local DynamoDB bootstrap |
| Missing IAM permission | `round-4/review-envelope.txt`, ConditionCheckItem permission |
| Incorrect index types | Approved plan and DynamoDB readiness verification; locate and seal the defective snapshot |
| HTTP-contract mismatch | `round-1/fixer-result.json`, POST 201 string response contract |

The run artifacts inspected do not contain matched per-repair cost receipts.
Reconstruct them from exact session/event provenance before comparing policies.
Aggregate chain usage or phase forecasts cannot stand in for matched repair
costs. Each benchmark pair must retain the same snapshot, completed independent
reviews, model/effort, raw output, required responsibilities, known blocker IDs,
and provider accounting receipt. All six replay blockers must be detected and
gross repeated-review tokens must fall by at least 60%.

## Authorized bounded allowance (applied)

The user authorized total ceilings of **400,000,000 tokens and 840 active minutes**
on 2026-09-14, reiterating that repairs must receive delta reviews. These
totals include all consumed usage; they leave at most 49,605,849 tokens and
287 minutes 14 seconds at the last recorded usage. Refresh accounting before
any guarded amendment. This is a bounded estimate, not a promise of completion.
The operator token was recovered from the prior round-six launcher log and
validated against both the current token hash and authority MAC. The guarded
budget amendment applied both ceilings on 2026-09-14. Chain, budget-ledger, and
run-control records agree; consumed usage remains 350,394,151 tokens and 33,166
active seconds. The token value is not included in this document.

The delta policy is registered as unqualified, with predecessor capture
preserved and the run still stopped. Registration exposed and fixed the actual
capture layout: `project/.archon/workflows`, not `project/workflows`. Historical
coverage import and measured qualification remain required before activation.

The concrete next API review span is
`797b5b98bc89a6453ea143b90e41316fe17bd259..c99b307a9506202d86c82ebac4f895f59efa0d0a`:
28 added and 25 removed lines across `group.service.ts`, `sharedlinks.repo.ts`,
and `init-dynamodb-local.js`. Retain the completed round-six review as historical
baseline evidence even though it contains findings. Independently validate its
provenance and fill only demonstrated coverage gaps. Review the new disclosure,
recipient, and reconciliation behavior with security and data/reliability lenses.
Open findings alone never reset the review base to `origin/main`.

| Remaining work | Maximum tokens |
|---|---:|
| Historical receipt reconstruction and matched qualification | 10,000,000 |
| API closure/regressions, verification, OpenAPI handoff | 10,000,000 |
| MCP implementation and independent review | 20,000,000 |
| Exact-candidate integration and draft PR preparation | 5,000,000 |
| Diagnosis/retry reserve | 4,605,849 |

The existing low-confidence phase forecast estimated 10–30M remaining before
qualification was introduced. The proposal adds a bounded qualification lane
and reserve. Stop if qualification misses a blocker or the 60% threshold,
required evidence cannot be recovered, the allowance is exhausted, or two
repair attempts make no progress on one finding. Do not lower acceptance or
extend resources automatically. API → MCP order and `locally_verified` before
publication remain mandatory.
