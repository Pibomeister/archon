# Archon v2 review loop — knowledge transfer

Written 2026-09-24 against `main` `9beaa04` for whoever takes the two-repository
convergence work forward. It says what v2 is, where each piece lives, how to
operate and recover a chain, what was measured, and what is still open. It does
not restate the operator contract: `RUNBOOK.md` §3c is the typed-line reference
for the loop and `docs/operator-recovery.md` is the recovery contract for chain
stops; where this file and either of those disagree, they win.

Shipped as GoodwordTeam/archon PR #9 (merged 2026-09-21, fast-forward to
`2761f7a`), on top of v1's PR #8. The plan it executed is
`docs/archon-v2-plan.md` (Codex cross-model consensus, revision 8, shipped alongside
this file).

## 1. What v2 is, in one paragraph

v1 measured the ENG-3866 two-repo chain and found the review loop was the cost:
187 of 306 active minutes were review, five of thirteen review invocations were
replays after infrastructure stops, and the fixer widened symbols the spec had
pinned. v2 makes each activity in a round durable and identity-bound, so a
resume re-runs only the activity that was interrupted; replaces "re-review the
whole diff after every fix" with a `verify` round that dispositions the ledger
and scans the repair's regression perimeter; requires positive closure (every
P0/P1 that ever entered the ledger must be `closed` by a reviewer citing a line
at HEAD); and freezes pinned symbol bodies mechanically on the staged index. The
review topology itself was measured (bounded trio, capped `ce-code-review`) and
kept unchanged: `mode:ce` runs the full `ce-code-review` skill under an
execution contract.

## 2. Map of the code

| Piece | Files | Tests |
|---|---|---|
| Durable round protocol: identity, marks, gates, converge decision table | `setup/round-state.py` (`pre`, `gate`, `fix-plan`, `commit-fixer`, `converge`, `exit-check`, `mark`, `reject-review`, `id`) | `setup/tests/test_round_state.py`, injection replay `setup/tests/injection/review-loop-replay.sh` |
| Closure ledger, residuals, waiver compatibility | `setup/ledger.py` | `setup/tests/test_ledger.py` |
| Review mode selection (`full` / `verify`) | `setup/review-mode.py` | `setup/tests/test_review_mode.py` |
| Pin guard on the staged index | `setup/pin-guard.py` | `setup/tests/test_pin_guard.py` |
| Review execution contract and prompts | `setup/review-contract.md`, `setup/prompts/review-ce.md`, `review-verify.md` (measured-and-rejected `review-trio.md`, `review-capped.md` kept for the record), embedded into the lane by `setup/embed-prompts.py` (`REVIEW_MODE = "ce"`) | `test_review_prompt_shape.py`, `test_prompt_embedding.py`, `test_v2_prompt_contract.py`, `test_review_sizing.py` |
| The lane | `workflows/full-sdlc-api.yaml` review-loop (`round-pre → review → review-gate → fix-plan → fixer → commit-fixer → converge`), `exit-gate`; derived `full-sdlc-api-lite.yaml`, `*-codex.yaml` via `derive-lite.py api` and `derive-codex.py --all`; doctrine lock `setup/lane-doctrine.lock.json` | `test_node_review_loop_shape.py`, `test_node_review_gate.py`, `test_node_stress.py`, `test_lane_doctrine.py` |
| Chain control: first-failure reopen, per-stage scope/pin amendment, timing counters, acceptance | `setup/feature_chain.py`, `setup/archon-run.py` (`feature-reopen --verify-only`, `feature-pin-amend`, `feature-scope-amend`, `feature-budget-update`), `setup/chain-acceptance.py` | `test_feature_chain_v2.py`, `test_feature_scope_amend.py`, `test_verify_only_reopen.py`, `test_chain_acceptance.py` |
| Cross-repo findings, joint plan validation (pin coverage) | `setup/cross-repo-keys.py`, `setup/validate-joint-plan.py` | `test_joint_plan_coverage.py`, `test_node_exit_gate_cross_repo.py` |
| Measurement harness and results | `setup/tests/measure/review-bench.py`, `setup/tests/measure/RESULTS.md` | — |
| SQLite lock retries for control commands | `setup/archon-lock-retry.sh` (used by `resume.sh` and `gate-approve.sh`) | `test_archon_lock_retry.py` |

Everything the loop decides on lives in `<artifacts>/round-N/` as files written
`tmp` + `mv` by `round-state.py`; node outputs are never read back as state.
The files, in order: `review-input.json` → `review-envelope.txt` → `review.ok`
(the only downstream authorization) → `repair-start.json` → `repair.json` →
`fixer.ok` → `decision.json`. `RUNBOOK.md` §3c has the table and every typed
line.

## 3. Operating a two-repository chain

All commands from the Goodword root. `feature` and `feature-advance` supervise
the run until its next gate or terminal state, so run them in the background
or in a second shell.

```bash
# launch (planning run first; pauses at plan-gate)
DISABLE_OMC=1 python3 .archon/setup/archon-run.py feature --provider claude \
  --scope api,goodword-mcp /abs/path/to/spec.md

# approve the plan (records the approval, then resumes the run detached)
archon workflow approve <planning-run-id> --detach
# if it prints "Approved but failed to resume … database is locked":
bash .archon/setup/resume.sh <planning-run-id>        # foreground: lock retries apply

# dispatch the next stage / integration once the current run is terminal
DISABLE_OMC=1 python3 .archon/setup/archon-run.py feature-advance --chain <chain-id>

# resume a stopped stage after fixing what the typed line said
bash .archon/setup/resume.sh <run-id>

# acceptance report once LOCALLY_VERIFIED (PASS / PARTIAL with clauses)
python3 .archon/setup/chain-acceptance.py <chain-id>
```

State: chain json in `~/.archon/control/codex-lite/feature-chains-v2/<chain>.json`
(MAC-signed; never hand-edit) plus `<chain>-chain-timing.json`; run artifacts in
`~/.archon/workspaces/_local/Goodword/artifacts/runs/<run>/`; runs and events
in `~/.archon/archon.db` (`remote_agent_workflow_runs`,
`remote_agent_workflow_events`); logs in `~/.archon/logs/`; stage worktrees in
`api/.worktrees/<slug>-<chain8>` and `goodword-mcp/.worktrees/<slug>-<chain8>`.

Operator levers, all guarded and all recorded in chain state:

| Situation | Lever |
|---|---|
| `PIN_BREACH` / `PIN_CONFLICT` on a symbol the spec itself orders changed | `archon-run.py feature-pin-amend <run> --chain <id> --symbol <s> --allowed-change "<text>" --reason "<r>"`, then resume. Invalidates the round's review (intended). |
| `COMMIT_SCOPE=QUARANTINED` / `COMMIT_SCOPE=FAIL` (a file outside the allowlist) | `archon-run.py feature-scope-amend <run> --chain <id> --add-file <path> --reason "<r>"`, then resume. The only writer that updates both allowlist copies and re-signs; per-stage since `2761f7a`. Editing `files-allowlist.json` by hand is `ALLOWLIST_DRIFT=FAIL` at the next commit node. |
| `ROUND_CAP_REACHED` | Raise `round-cap.txt` in the run artifacts, or write `accept-residuals.txt` (human act; first line is the reason and is echoed by `EXIT_GATE`). Acceptance buys the cap round only: converge counts every non-regressed P0/P1 as accepted (`CLOSURE_ACCEPTED`) on a ready verdict; past the cap `ROUND_CAP_EXCEEDED` stops regardless. |
| `NOT_READY_WITHOUT_BLOCKER` | `round-state.py reject-review <artifacts> --reason "<r>"`, then resume (`ROUND_REUSE … reason=rejected`). |
| Stage failed on its first implement run (`STAGE_FAILED`) | `feature-reopen --chain <id> --repo <repo> --reason "<r>"`, `--verify-only` when the candidate is already committed. Reopen dispatches live source; resume replays the run's captured YAML. |
| `CHAIN_BUDGET=EXCEEDED active=<s> cap=7200` before a dispatch | `feature-budget-update … --chain <id> --total-active-minutes <n> --reason "<r>"`. Active seconds are summed from node events, gate waits excluded. |
| `GATE_5_input_matches=FAIL` | `round-state.py id <artifacts>` prints the identity the round expects; diff against the envelope's `Input:`. On a run dispatched before 2026-09-18, reopen rather than resume (its snapshot has the clobber defect). |

Traps met live, each cost time:

- macOS has no `timeout`; a `timeout 300 python3 …` line is a silent no-op.
- `resume.sh --detach` returns before the SQLite lock error surfaces, so the
  retry wrapper never fires; resume in the foreground when other runs are live.
- A run snapshots its YAML under `<artifacts>/workflow-source/` but calls
  `setup/*.py` by absolute path, so fast-forwarding `.archon` main under a live
  run changes that run's scripts mid-flight. Coordinate with any other session
  dispatching runs before moving main.
- Preflight exact-matches `ARCHON_OTP_TEST_EMAIL` / `ARCHON_OTP_SECOND_EMAIL`
  against `OTP_TEST_WHITELIST_EMAILS` in `api/.env`, while the api honours
  `@domain` entries; add the literal address.
- The shared Claude subscription's weekly window is the real budget: at 100%
  every model node dies with `rate_limit … resets <time>` and the run needs one
  resume after the reset. Nothing is lost; every round is committed as it lands.

## 4. What was measured

**Topology gate** (`setup/tests/measure/RESULTS.md`, three runs per cell on the
two v1 round-2 candidates): trio p50/p90 api 14.4/15.0 min, mcp 11.3/15.8;
capped-CE api 13.7/14.7, mcp 11.4/12.0; verify 4.2 min. Neither topology
re-found the v1 round-1 P1 on either candidate (parity 0/12) and verify missed
the v1 round-4 P1. Decision: no topology change. The acceptance formula
`84 + 3d + 2v` re-derived to about 136 active minutes; 120 was not claimed.

**Live run**, chain `f725df60` (ENG-3866 v2, api + goodword-mcp, claude),
LOCALLY_VERIFIED, not published:

| Phase | Active | Rounds | Notes |
|---|---|---|---|
| planning | 97.7 min | 2 critic rounds | docreview 34, ralplan 23, critic 19, revise 9 |
| api stage | 57.0 min | 2 (`full`, `verify`) | `PIN_OK pins=15`, fixer changed 0 pinned bodies |
| goodword-mcp stage | 144.7 min | 6 | converged by `CLOSURE_ACCEPTED` on one deferred P1; ~35 min lost to a stray `.env.example` edit and one weekly-limit stop |
| integration | 50 s | 1 attempt, 2/2 | first try |

`CHAIN_TIMING reviews=9/2 rounds=8 review_duplicates=0 fixers=10
fixer_duplicates=0`. Against v1's chain `3460c074`: api rounds 7 → 2, review
replays 5 → 0, integration first-try both; mcp rounds 4 → 6 because that stage's
snapshot predates the acceptance fix (on the fixed lane the cap ends it at 4).
Acceptance: PARTIAL (accept-residuals, waivers, deferred P2/P3 on both stages).

**Where the time goes now.** 300 active minutes against the 136 gate, and the
loop is not the driver: planning is 97 min on its own and each review
invocation averaged about 19 min against the 14.4 measured in the gate.

## 5. Defects found live and fixed (all with tests and negative controls)

| Commit | Defect |
|---|---|
| `4de6f85` | `review-gate` copied the session's narration over the reviewer's marked envelope, erasing the footer: `GATE_5 envelope_input=[]` on every round. Now the node output only fills a missing envelope. |
| `63e7af6` | Pin guard could not resolve the spec's vocabulary (`Class.member`, DTO classes, NestJS routes, whole files) and checked the other stage's pins against this worktree: 15/15 `PIN_UNRESOLVED`. Also: commit-fixer compared against the stage baseline, so a spec-ordered change to a pinned method breached on every fixer round; it now compares against the round's pre-head. |
| `7c7cc01` | `accept-residuals.txt` removed the cap and a deferred P1 could never close: an unbounded loop (chain `1f7a896a`, six rounds). Acceptance now buys the cap round only and closes non-regressed blockers at converge. |
| `455004d`, `c2018f7` | No re-dispatch path for a stage whose first implement run failed (patch from the MCP roadmap session). Since extended by `STAGE_FAILED` + `RECOVERY=` in `62b6f4f`. |
| `2761f7a` | Scope and pin amendments refused once any stage in the chain had verified; now keyed per stage. |

The operator-recovery series that followed (`62b6f4f`, `6728939`, `83fb297`,
VERSION 2026.09.18-1) turned three more residuals into typed stops: quarantine
prints `COMMIT_SCOPE=QUARANTINED` + `RECOVERY=` instead of a silent
`COMMIT_SCOPE=OK`; `files-allowlist.json` is a projection and hand edits are
`ALLOWLIST_DRIFT=FAIL`; `CHAIN_BUDGET=EXCEEDED` stops the next dispatch.

## 6. Still open

Harness:

- Reader-audit gate classifies the change's own new guard as an affected reader
  when the plan's `reader_audit` already declares the consequence (api stage,
  `groups.reserved`); a plan-declared consequence should not fail the stage.
- Preflight OTP check should honour `@domain` whitelist entries the way the api
  does.
- `chain-acceptance.py` clause 4 reads `CONVERGED round=N (residuals accepted: …)`
  as "final converge not clean".
- The fixer prompt does not carry the allowlist; a doc-only sibling edit still
  costs a round (now a typed stop, no longer silent).
- Pin extractor is regex; tree-sitter is the documented upgrade. Route pins on a
  new endpoint are `PIN_NEW` at commit-impl and frozen from the first fixer
  round; whole-file pins on allowlisted files are `PIN_UNENFORCEABLE` and rely
  on the reviewer.
- Verify-mode recall missed a P1 in the gate; the pin guard is a scanner, not a
  proof. The risk-delta pilot's `review_delta_workflow.py` refuses the v2 loop
  shape (typed; its test re-pointed at `bugfix-codex`) and must re-capture.

Product, from the run: concurrent or retried `share_group` calls mint a
duplicate untracked public link (deferred P1 `21c0f940bb23`); needs an
idempotent share-link mint in the api. Chain `f725df60`'s candidates are still
in their worktrees, unpublished.

## 7. Next targets, in order of measured payoff

1. **Planning (97 min).** `docreview` alone is 34 min at `maxBudgetUsd 12`,
   `ralplan` 23 min, two critic rounds 28 min. A bounded docreview prompt was
   built during v2 and deleted unmeasured; measure it with the same
   `review-bench.py` discipline (three runs, parity against the v1 findings)
   before shipping. Cutting planning to 40 min brings the chain to about 240.
2. **Per-review wall time (19 min live vs 14.4 in the gate).** The gate ran on
   the v1 round-2 candidates; the live rounds reviewed larger first-round diffs
   and the mcp stage's full rounds re-reviewed after verify found new P3s. Log
   persona count and production-line churn per round (the `Coverage` section
   the prompt asks for) and check the sizing rule is being applied.
3. **Rounds that find nothing blocking.** The mcp stage spent two full rounds
   after verify rounds surfaced P3-only findings (`verify-found-new-blocker`
   fires on any new finding the ledger keeps). Consider letting a verify round
   that finds only P2/P3 progress to `verify` again rather than `full`.
4. The harness items in §6, cheapest first: clause 4 parser, preflight domain
   rule, reader-audit self-guard.

## 8. Verification recipe for the next change

Run from `.archon` (a worktree under the Goodword root; the suite is
location-sensitive): the unit suite in four foreground chunks of about 38
modules (a single background run is OOM-killed on this machine), expecting only
the six baseline failures (`test_backfill_disposable_postgres_matrix` ×4,
`test_bugfix_supervision` ×2); `NODE_STRESS=100` on edited nodes; the injection
replay; `derive-lite.py api` → `derive-codex.py --all` → `lane-doctrine.py check`
without `update`; `embed-prompts.py --check`. Every guard gets a negative
control by reverting exactly that guard. `setup/run-tests.py` is the
fail-closed discovery runner the fork's gate uses.
