# Repository-list feature qualification

Status: implementation verified by automated checks; supervised trial is paused at the joint human approval gate. Post-approval repository/integration qualification remains pending.

## Implemented behavior

- Codex repository scopes use registered names, canonical aliases, and one versioned private chain. Legacy scalar commands and persisted API/web chains retain their readers.
- Every selected repository has its own worktree and pinned baseline before joint planning. Planning workers can write only run artifacts; routing files are read-only in their sandbox. After approval, workers receive one assigned worktree and read-only approved plans, tests, and dependency inputs.
- One approval binds specification bytes, repository scope, baselines, the joint plan, supporting planning artifacts, dependencies, interfaces, and integration scenarios.
- Repository stages use the existing feature DAG and repository profiles. Trusted final verification requires actual positive JSON test counts and exact interface artifact hashes. Consumers receive exact local predecessor revisions.
- Integration creates disposable candidate worktrees, validates the approved matrix, and records revisions, commands, test counts, and expected artifacts. Its subprocess groups are registered in the private budget ledger before execution.
- A shared 240-active-minute / 30-million-token ledger covers all phases and retries. Exact session IDs and native subagent ancestry replace time-window attribution. Approval waits do not consume active time. Exhaustion is checked before dispatch and arming.
- Failed starts preserve control authority; duplicate actions and dispatches are serialized. `feature-replan` creates a guarded successor for terminal, unapproved planning without replacing the chain, worktrees, or budget.
- Only a verified combined result becomes `locally_verified`; publication remains held.

## Automated evidence

- The settled full setup run passed 1,283 tests. Ten existing optional PostgreSQL / qualified-engine tests were explicitly excluded because their required external environments were unavailable. All feature tests were included. Subsequent live-trial repairs have focused regression coverage.
- Launcher, wrapper, private state, approval drift, topological dispatch, failed-start recovery, budget accounting, JSON test reports, and disposable integration each have focused tests.
- Real sandbox probes deny worker writes to routing and approved artifacts while allowing planning documents before approval.
- Stock `archon validate workflows` accepts both full Codex feature DAGs. Existing deprecated legacy approval/loop warnings remain.
- Stock DAG dry-runs using the actual planning route (`bootstrap=no`) reach `plan-gate` without implementation. Repository and integration modes use separate paths.
- Lite and Codex generation checks pass. Python compilation and shell syntax checks pass. The TS-focused deslop scanner found no applicable JS/TS files in this harness change.
- CodeRabbit completed two reviews. Valid findings were repaired; the second review's sole pattern-stdin finding was fixed. The suggestion to remove explicit shell failure exits was rejected because a surrounding `||` disables implicit `errexit`; the checks now run in a subshell and retain explicit exits.

Logs from this session:

- `/tmp/archon-repository-settled-suite.log`
- `/tmp/archon-repository-coderabbit.log`
- `/tmp/archon-repository-coderabbit-recheck.log`
- `/tmp/archon-stock-planning-actual-routing.log`
- `/tmp/archon-stock-implement-settled.log`
- `/tmp/archon-stock-integration-settled.log`

## Supervised Sol/medium trial

Spec: `repository-list-dryrun/repository-list-qualification-spec.md`.

Chain: `f473a1dcbc741a49c11ac34a2257e5cc`.

Current planning run: `c5d765e7-a873-401e-93c7-b2415ef99ac1` (generation 2).

Prior planning run: `ab9de194-c258-49b3-a71f-c3af3f6f02c4`. This exposed two harness defects before approval: a missing worker control-directory environment variable, and a skipped-bootstrap dependency that incorrectly skipped the plan node. Both were repaired and tested. Successor planning retains the same chain and cumulative accounting.

The next unapproved planning attempt, `6c43d7bc-0b2e-4c7a-9de9-c0d87b8e498a`, correctly stopped at its review cap because the qualification fixture was outside Jest roots. The original input is archived as `repository-list-dryrun/repository-list-qualification-spec-v1.md`; corrected v2 fixture discovery passed the real Jest `--listTests` command in a disposable worktree. Generation 2 retains the same selected worktrees and budget.

Actual Codex session context confirms `gpt-5.6-sol` with `medium` effort. Both selected worktrees remain clean at their pinned baselines, with no approval present. All active budget intervals are closed for the approval wait. Control tokens are operator-held and are not included here.

Private operator logs:

- `/tmp/archon-repository-live-trial.log`
- `/tmp/archon-repository-live-resume.log`
- `/tmp/archon-repository-live-replan.log`
- `/tmp/archon-repository-live-replan-v2.log`

## Remaining qualification and existing packaging limitation

The live trial must reach its joint gate, receive human approval, complete both repository stages, and pass the local integration matrix before this feature is called qualified. ENG-3866 stays deferred.

A dry package build passes manifest, generation, and lane-doctrine checks but stops at the existing secret scan: `profiles/machine-binding.example.json` contains two all-zero SHA-256 placeholders. Those same placeholders are present in the pre-change HEAD. The secret gate was not weakened, and nothing was published.

No production access, Linear writes, pushes, PR publication, or ENG-3866 implementation was performed.

## Current human gate

Packet: `/Users/eduardopicazo/.archon/workspaces/_local/Goodword/artifacts/runs/c5d765e7-a873-401e-93c7-b2415ef99ac1/plan-review.html`.

The plan critic accepted round 3 with no findings or final-round declines. Premise verification is empty (no declared premises); caller impact is explicitly SKIPPED. Document review applied no edits and left four P1 notes: restating assertion literals in the plan, binding the external verifier script digest, specifying an executable local `/health` check, and recording detached-worktree provenance. These require human review; no approval was issued and the trial is not locally verified. The assertion literals are already supplied by the specification, so that note is primarily plan clarity.

The packaged `archon-install`, `archon-linear`, and `archon-sdlc` skills now document repository scopes, provider-specific controls, joint approval, local candidates, shared Codex budgets, recovery, qualification, and publication hold. Their payload/gist copies and staged links were verified, with 22 contract/staging/manifest tests passing.
