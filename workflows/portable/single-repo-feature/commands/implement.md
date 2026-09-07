Implement the single approved unit in plan.json under ARTIFACTS_DIR. Read the same
run-context.json, brief.md, policy-context.json and frozen knowledge files used by
the planning roles. binding.repositoryRoot is the ONLY source checkout to modify.

Preserve the existing W3 boundary: change only plan.files, do not refactor adjacent
code, edit the plan/context/counters, create a branch/worktree, commit, push, deploy,
merge, or write to any external knowledge repository. Those actions are owned by
mechanical workflow steps and the operator. Native API/backend/mobile scope is
excluded from this frontend pilot.

On a recovery round, read review.json and verification.json from the prior round,
then fix only the reported problem within the original allowlist. If a valid fix
requires broader scope, explain that constraint; do not widen the plan. If the
requested work already satisfies the plan, report that accurately and set the
optional structured field `"category": "already-satisfied"`. Use
`"changes-produced"` when you changed the work product, `"blocked"` when you
cannot proceed inside the approved plan, and `"inconclusive"` when the evidence is
insufficient. This category is advisory: mechanical verification decides whether
the workflow can close without a PR.

Use the package manager and scripts in the captured profile. The mechanical
verification node will execute them again and records real exit codes. Return a
structured summary; do not claim a test succeeded without its actual output.

For profile v2, use its explicit source recipe and repository facts. A Bun engine workspace or Python/shell workflow pack does not imply Next or a packageManager field. Do not invent missing metadata.
