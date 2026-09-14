# risk-delta-v1 repository-list review policy

This repository-owned policy is opt-in through the guarded controller. Qualified
activation selects a journaled review-loop successor and retains the original
captured source intact. Registration cannot dispatch an unqualified policy.
Sol (`gpt-5.6-sol`) and medium effort remain required.
The approved product plan, API → MCP dependency order, stage allowances,
exact-candidate integration, counters, and consumed resources remain binding.

## Review work

The workflow uses `review_delta_runtime.py prepare` to select the last completed
review head and retain a bounded brief for each assignment.
Invoke the existing ce-code-review skill with the supported arguments
`mode:report-only base:<brief.candidate.base> plan:<approved-plan-path>`.
Supply the brief as explicit caller instructions. The brief's responsibility
assignment replaces overlapping default persona selection for this invocation.
Keep the original skill and globally installed plugin unchanged.

Review only the assigned responsibilities, the actual diff, open finding IDs,
invariants, and the affected callers/readers/contracts. Read evidence by its
retained path. Request additional repository context whenever impact cannot be
bounded. Missing impact tooling requires a caller/reader trace. A change to an
invariant, public contract, permission boundary, shared dependency, or subsystem
requires full review of the affected subsystem. Invalid baseline evidence or
unbounded impact requires a full candidate review.

The initial full review needs separate correctness/contracts and
testing/maintainability/standards reviewers. Assign security, data/reliability,
performance, and CLI responsibilities when their risks are present. Ordinary
repairs need an independent repair reviewer plus changed risk specialists;
cross-store repairs always include data/reliability. Assign each responsibility
once; launch at most three reviewers concurrently, using further batches when
needed. Use fresh sessions with bounded briefs, not full-conversation forks.
Every production change requires review by someone other than its author.

Save each raw response immediately; its trusted checkpoint node normalizes and
retains it before the next reviewer batch. Every blocker
must retain an explicit immutable ID through normalization and ledger updates.
One format-only repair attempt may use the original response; a second malformed
result blocks the assignment and remains unresolved. Validator failure, similar
titles, small finding counts, and elapsed rounds never remove a blocker.
Checkpoint each completed coverage record independently. Preserve incomplete
attempts and existing counters; resume only unfinished or invalidated work.

## Repairs and verification

Construct a repair packet with the finding, production files, committed
regressions, and required contract/configuration counterparts. Validate every
path against the approved stage allowance before editing. Missing paths produce
a precise scope-amendment request. Preserve cross-repository ownership and use
the existing chain controls. After two attempts without progress on one finding,
stop that repair for bounded diagnosis.

A behavioral repair requires committed evidence exercising that defect.
Temporary probes supplement regressions. Run small real DynamoDB,
HTTP-contract, and transaction-order probes before expensive review when those
boundaries change. Classify sandbox/infrastructure failures separately from
application failures; retain trusted local verification evidence for the exact
candidate. No skipped or failed required check passes a gate.

Centralize required mechanical commands. Reuse a receipt only when source tree,
command, dependencies, configuration, environment, and retained output match.
Run cleanup once and then on changed lines. Treat P3 style suggestions as
advisory unless they violate an explicit project gate.

Converge only with contiguous full-baseline plus delta coverage through the final
candidate, all required responsibilities, zero unresolved mandatory findings,
independent closure and regression evidence, and current successful required
verification receipts. A full baseline may contain open findings. Never use
`yield-stop`, baseline test counts, or a human residual shortcut to approve
unreviewed repairs under this policy. Existing integration gates still run.

For future planning, independently review the complete plan once, then changed
requirements and unresolved findings. Reopen settled decisions only for new
evidence or changed requirements. Do not restart ENG-3866's accepted plan.

## Qualification and activation boundary

Run the offline helper tests first. Retain actual historical repair snapshots,
raw reviews and provider usage receipts for the six ENG-3866 replay defects.
`review_qualification.py` checks matched snapshot hashes, required coverage,
known blocker accounting, and at least 60% reduction in repeated-review gross
tokens. Cached input remains counted once. Retain cached/uncached input, output,
active time, repeated reads, new validated defects, and reopened findings.
Synthetic unit fixtures are not pilot qualification evidence.

Qualification also requires interruption, malformed-output, authority, stale
receipt, dependency drift, model-dispatch and amendment-crash negative tests.
Missing evidence or a missed blocker fails qualification; do not relax the
threshold. The exhausted 350M-token ceiling cannot be extended automatically.
The stopped pilot remains stopped until a bounded allowance is explicitly
authorized and qualification succeeds. Publication still requires the existing
`locally_verified` exact-candidate integration receipt.
