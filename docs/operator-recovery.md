# Operator recovery (VERSION 2026.09.18-1)

Recipes for the stops that used to dump an operator into a plain session.
The decision procedure is `skills/archon-sdlc/SKILL.md`. Discriminators and
command shapes here are the contract; if a printed `RECOVERY=` line disagrees
with this file, the printed line wins (it is bound to that run).

Agents never write `accept-residuals.txt`, never edit `files-allowlist.json`,
never `approve`/`reject`/`abandon`, and never merge. They may **print** the
commands below with the run id and chain id filled in.

## When to use this

A repository-list feature (`archon-run.py feature --scope …`) stopped, and the
typed line is one of: `SCOPE_BREACH`, `COMMIT_SCOPE=QUARANTINED`,
`COMMIT_SCOPE=FAIL`, `ALLOWLIST_DRIFT=FAIL`, `STAGE_FAILED`,
`CHAIN_BUDGET=EXCEEDED`, `SMOKE=FAIL … class=infrastructure`,
`JOINT_E2E=FAIL class=infrastructure`.

## One allowlist

`files-allowlist.json` in the stage artifacts is a **projection** of
`stages.<repo>.plan.files_allowlist` in the MAC-signed chain state.
Editing the json is not an approval. On a repository-list stage the next
commit node compares the two and stops with `ALLOWLIST_DRIFT=FAIL`.

The only writer is:

```bash
# Codex (control token from the STARTED line)
python3 .archon/setup/archon-run.py feature-scope-amend <run-id> \
  --token <operator-token> --add-file <repo-relative-path> \
  --reason "Authorized scope recovery"

# Claude (no control token)
python3 .archon/setup/archon-run.py feature-scope-amend <run-id> \
  --chain <chain-id> --add-file <repo-relative-path> \
  --reason "Authorized scope recovery"
bash .archon/setup/resume.sh <run-id>
```

The path may be:

- already tracked in the stage worktree, or
- a new untracked file in the stage worktree, or
- a file under `<artifacts>/strays/` (the command restores it into the
  worktree, then allowlists it).

It must be a safe repository-relative path, not a symlink, owned by the
operator. Amend is add-only for the **current** stage. A verified predecessor
in a two-repo chain does not close the second stage. Integration and
publication still refuse.

## Quarantine is a stop

`check-scope.py --stage --quarantine` still **moves** a new file that shares
no stem with an allowlisted sibling, so the operator can retrieve it. It then
exits 1:

```
COMMIT_SCOPE=QUARANTINED file=<path> -> <artifacts>/strays/<path>
COMMIT_SCOPE=FAIL nothing staged
RECOVERY=mv <dest> <worktree>/<path> && python3 .archon/setup/archon-run.py feature-scope-amend …
```

Do not resume until the file is allowlisted or deleted. Resuming a round that
printed `COMMIT_SCOPE=OK` after a quarantine was the C4 compile-break (a
module the fixer created was gone, an import of it was not).

On a repository-list stage, stem-sibling auto-expansion is off. New files
always go through amend.

## First-attempt implement failure

A stage that fails on the first implement run (never verified) used to have
no re-dispatch: reopen required a verified handoff, advance was a no-op,
resume restored the captured (broken) workflow. `advance` now records
`stages.<repo>.status=failed` and prints:

```
ARCHON_FEATURE_REPOSITORY_CHAIN=STAGE_FAILED chain=<id> repo=<repo> status=failed
RECOVERY=python3 .archon/setup/archon-run.py feature-reopen --chain <id> --repo <repo> --reason "retry failed implement stage"
```

If the candidate is already committed in the worktree, add `--verify-only`.
If the failure was a harness defect in captured YAML (review-gate clobber
class), do **not** `resume.sh`: resume re-runs the snapshot that failed.
Reopen dispatches live source.

`feature-reopen` is also admitted for a previously reopened stage whose own
implement run then stopped.

## Envelope fill-if-missing

Review and doc-review gates write session output into `review-envelope.txt` /
`docreview-envelope.txt` only when that file is **empty**. A reviewer that
`mark`ed the envelope, then narrated, used to overwrite the footer the gate
requires. If you still see `GATE_5` / `envelope_input=[]` on a run dispatched
**before** VERSION 2026.09.18-1, reopen; do not resume.

## Infrastructure vs product

| Typed line | Class | Recovery |
|---|---|---|
| `SMOKE=FAIL api-boot exited before ready class=infrastructure` | process died | Restart local `postgres-db` / `dynamodb-local`, `resume.sh` |
| `SMOKE=FAIL api-docs-json code=<n> class=infrastructure reason=stack-down` | docker stack down | `docker start postgres-db dynamodb-local`, `resume.sh` |
| `SMOKE=FAIL api-docs-json code=<n>` (no `class=`) | product / unknown | Read the boot log; this is the feature unless the log says otherwise |
| `JOINT_E2E=FAIL class=infrastructure api candidate env` | missing/stale env | Root clone `.env` must exist; do not copy a stage-worktree `.env` by hand |
| `JOINT_E2E=FAIL class=infrastructure second-identity-unsubscribed code=401` | fixture | Insert an active `billing_subscriptions` row for the second OTP identity, mirroring the first |
| `JOINT_E2E=FAIL class=infrastructure api-boot exited before ready` | process / stack | Same as smoke stack-down |

Do not stop shared local containers from a node-scoped cleanup. Joint e2e
resolves `.env` through `candidate_env.py`: the git clone wins over a stale
copy in `api/.worktrees/*`.

## Active-time budget

`CHAIN_BUDGET=EXCEEDED active=<s> cap=<s>` on a chain that still has a stage
or integration to dispatch **stops** that dispatch. Wall-clock remains
advisory (human gates and outages). A chain that is already `locally_verified`
still only reports.

```bash
# Codex
python3 .archon/setup/archon-run.py feature-budget-update <run-id> \
  --token <operator-token> --total-tokens <authorized> --reason "…"

# Claude: same command with --chain <id> (no token). Active minutes are not
# replenished; the total includes prior usage.
```

## Stacked and scalar scopes

Do not fall back to a plain session because the ticket is mcp-only, web-only,
or stacked on an unmerged parent.

```bash
# mcp-only (HAS_SMOKE allocates APIPORT even when api is not selected)
python3 .archon/setup/archon-run.py feature --provider claude \
  --scope goodword-mcp /abs/spec.md

# stacked on a local parent commit (fetch the parent PR first)
python3 .archon/setup/archon-run.py feature --provider claude \
  --scope goodword-mcp --base goodword-mcp=<40-hex> /abs/spec.md

# web-only
python3 .archon/setup/archon-run.py feature --provider claude \
  --scope web-app /abs/spec.md
```

`--base` may be repeated per selected repo. Unknown repo, non-commit, or a sha
not present locally is a typed refuse. Approval binds the pinned baselines.

## Exit criteria

The stop is recovered when:

- the printed `RECOVERY=` command has been run by a human (or, for resume-safe
  transients, `bash .archon/setup/resume.sh <run-id>`), and
- the next typed line is not the same discriminator.

A captured-source harness defect is recovered only by reopen, never by resume.
