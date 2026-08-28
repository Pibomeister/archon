# Production implementation-node prompt skeleton (effects-table §8a — ce-work Tier-3 replacement)

Substitution points: {U_ID}, {PLAN_PATH}, {UNIT_BLOCK}, {ABS_WORKTREE_PATH}, {REPO_CONVENTIONS} (inline the matching repo-conventions-*.md verbatim).

```
Implement implementation unit {U_ID} from the plan at {PLAN_PATH}.

The working tree is at this absolute path (you are NOT cwd'd there; cd for every command):
  {ABS_WORKTREE_PATH}

<unit>
{UNIT_BLOCK — VERBATIM Goal / Files / Approach / Execution note / Patterns to follow /
 Test scenarios / Verification block, copied from the plan}
</unit>

Scope: change only the files listed under Files. Do not refactor adjacent code.
Do not edit the plan file. Do not create commits — a later node commits.
Do not create branches or worktrees — you are already on the correct branch.
If the unit's work already exists and satisfies Verification, say so and stop;
do not reimplement.

{REPO_CONVENTIONS}

Done when: every item under Verification is satisfied and the gate commands exit 0.
Report the exact commands you ran and their exit status.
This is an unattended pipeline invocation; ask no questions.
```

Shell-owned gate (the DAG re-runs these; never trust the model's self-report):
api → `bun run typecheck` && `bun run lint` && `bun run test "<touched patterns>"`
web → `pnpm typecheck` && `pnpm lint` && `pnpm test`
