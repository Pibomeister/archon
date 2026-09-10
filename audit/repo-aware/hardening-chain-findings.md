# Why ENG-3866 cannot run through archon today

Investigated 2026-09-09. Conclusion: the blocker is an **explicitly unimplemented feature** in the
hardening runtime fork, not configuration, and not anything to do with the repo-awareness work.

## The chain

```
.archon workflow layer declares hardened.required + controller_action   (commit 229090a, 2026-09-08)
  -> requires the archon-engine fork                    RESOLVED (built, gated, installed)
  -> fork requires execContext kind='container' profile='hardened'
  -> container isolation is folder-project-only
  -> hardened container seeding rejects nested repositories            <-- TERMINATES HERE
```

## The terminating condition, verbatim from the runtime

```
Hardened controller committed seed cannot infer nested repository inputs.
Multi-repo folder roots require controller-declared pinned repo inputs, which are not wired yet.
```

`packages/core/src/workflows/hardened-controller.ts:1879-1880` — walking from the seed root, **any
`.git` at a path other than the root throws**. Only `node_modules`, `.archon` and `.cache` are
skipped.

The Goodword root has **62 nested repositories at depth 1** (`api`, `web-app`, `mobile-app`,
`goodword-mcp`, `goodword-kb`, and every `wt-*` / `api-*` worktree sibling). A multi-repo root is
what Goodword *is*; the check can never pass for it.

## What this rules out

Re-registering Goodword as a folder project — the option that looked like the next step — **would not
have helped**. Proven on a scratch folder project with an isolated `ARCHON_DB`, so the real
registration and the 152 run rows referencing it were never touched.

## Supporting findings (still true, still useful)

1. **The root `.git` is a synthetic workaround.** One commit, `8a1d6d4 "archon shell root"`, **zero
   tracked files**, remote `~/.archon/shells/goodword-origin.git`. It exists so a multi-repo
   directory passes archon's git gate.
2. **Folder-kind is the documented intent**, and the registration contradicts it.
   `.archon/config.yaml:1` says "folder project"; `packages/cli/src/commands/workflow.ts:1605`
   describes folder projects as exactly this shape — cwd at the root, sees every child repo,
   per-service git is the agent's job. The registry says `kind=repo`.
3. **Today nodes run in an empty worktree** — repo-kind makes archon create a worktree of the empty
   shell repo under `~/.archon/workspaces/_local/Goodword/worktrees/`. Harmless only because the lane
   uses absolute paths and its own `<repo>/.worktrees/<slug>`; all three `$PWD` consumers `cd "$WT"`
   first.
4. **`kind` is immutable.** `updateCodebase` accepts only `default_cwd`, `repository_url`,
   `default_branch`. Changing it means delete-and-re-register.
5. **Re-registration would cost little** — `codebase_id ... ON DELETE SET NULL`, so 152 runs would
   keep their rows. Moot given the terminating condition.
6. **No released archon can run this workflow layer.** v0.8.0 fails to load 12 of 44 workflows;
   v0.10.1 fails 13 (its key list gained `wait`, not `controller_action`). Only the fork loads all 44.

## Runtime state left on this machine

- `Goodword/archon-engine` — the fork at `hardening/controller-boundary` (`6c073de`), installed deps,
  `type-check` clean, `bun run test` 0 failures, `hardened-controller.test.ts` 66/66.
- `~/.local/bin/archon` — now the fork binary, built from that tree. It loads all 44 workflows
  (`errorCount:0`); the released build cannot load this layer at all.
- `~/.local/bin/archon.release-v0.8.0.bak` — the previous released binary, restorable.
- `archon-runner:0.8.0` / `:latest` docker images, built per the RUNBOOK.
- The registration was **not** changed. `archon.db` was **not** modified.

## What would unblock it

Implement controller-declared pinned repo inputs for multi-repo folder roots in the fork — the
feature its own error message names. That is a runtime change in `hardened-controller.ts`, and it is
the frontier of the hardening rollout the RUNBOOK's header already calls "not admitted".

Until then no archon lane can run against the Goodword root, `bugfix` and `full-sdlc-web` included.
The last completed `full-sdlc-api` run was 2026-08-29, which predates the hardening checkpoint.
