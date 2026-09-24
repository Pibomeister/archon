> Status: approved 2026-09-17 and executed as GoodwordTeam/archon PR #9 (merged 2026-09-21). The text below is the consensus plan as approved, unchanged; `docs/archon-v2-knowledge-transfer.md` records what was built, measured and left open.

# Archon: a two-repository ticket in ≤ 2 h active time (v2, revision 8)

Deliverable type: build-ready implementation plan for the `.archon` repo. Status: **pending approval** (Architect READY round 6; Critic ACCEPT-WITH-RESERVATIONS, both reservations
folded into revision 8).
Pinned revision: `.archon` `main` @ `789b2a1` (PR #8 merged as `dc3a845`, plus `789b2a1`). Every `file:line`
below is against that commit. Lane: `workflows/full-sdlc-api.yaml` (claude provider); `derive-lite.py` /
`derive-codex.py` regenerate the derived lanes and `lane-doctrine.py check` must pass without `update`.

Revision 7 answers the Critic (session 1): partial fixer work is owned via `repair-start.json` so an
interrupted fixer resumes instead of tripping the read-only guard; one acceptance contract for the two
topology outcomes; stale statements reconciled; baseline failures and the acceptance checker named;
`design_expanded` scoped to the current round. Revision 6 answered Architect round 5: downstream authorization is bound to the successfully gated envelope
generation (`review.ok.gen`), required by fix-plan, commit-fixer, converge and terminal replay, and
invalidated by a gate failure or a rejection. Revision 5 answered round 4 (unified reuse validation on both branches and on terminal replay;
no-change attestation without an empty commit; explicit review rejection marker; ledger merge inside the
recovered boundary; the read-only guard moved into `review-gate` against a tree snapshot). Revision 4 answered round 3 (session `01a0ac46`): one authoritative qualification bar and call
count; a completion-record lifecycle for the fixer (`repair.json` durable completion, `fixer.ok` attestation
after commit) with three written recovery traces; reuse of a complete-but-ungated envelope; pending-commit
reconciliation before the reuse decision; repair reuse bound to result digest, review identity and tree;
closure = verified `closed` only (cross-repo findings keep their existing non-waivable stop);
`review-summary.json` kept as the compatibility artifact; a real measurement harness; duplicate-based
acceptance. Probe 4 (2026-09-16): a completion promise is honoured only from the group's **last** node
(emitted from the first node, the group ran to `max_iterations`), so terminal replay lives in `converge`.

## What v1 (PR #8) measured on chain `3460c074`

Per-run node minutes from `remote_agent_workflow_events` (node sums equal the active segments to the minute;
orchestration gaps are 0):

| run | segment | review | fixer | implement | other nodes |
|---|---:|---:|---:|---:|---|
| planning `692005f7` | 44 | — | — | — | docreview 15.3, ralplan 9.3, plan-revise 7.8 (3 rounds), plan-critic 5.9 (3), plan-render 3.3, kb-recon 1.2, impact-probe 1.1 |
| api `8ddb9cce` | 156 | 108.5 (8 invocations) | 31.8 (7) | 6.6 | reader-audit 3.5, deslop 2.5, deslop-review 1.6, gates < 1 |
| mcp `01845af1` | 105 | 78.6 (5) | 10.9 (6) | 8.0 | reader-audit 2.8, deslop 1.9, deslop-review 1.6 |
| integration `08acff37` | 1 | | | | |
| **total** | **306** | **187** | **43** | **15** | **61** |

One review invocation: 8.8–19.7 min (mean 14.4) = ~1.5 min setup + ~9.5 min slowest of 6–13 persona
subagents + ~6.5 min validator pass and synthesis (artifact mtimes in
`/tmp/compound-engineering/ce-code-review/20260916-135214-a18cd4cd/`). Delta rounds were not cheaper
(10.4, 19.1). Five of the thirteen invocations were replays after a resume (OAuth expiry, provider stream
drop, reviewer ending its turn early, a `COMMIT_SCOPE=STRAY` stop, one parser bug since fixed):
`resume.sh:265` hands the run to the CLI, which restarts the `review-loop` group at `round-pre`
(RUNBOOK §4). Rounds 2–5 of the api stage reviewed code the round-1 fixer had added to
`GroupService.shareGroup`/`reshareGroup` against the spec's pin ("No other change to POST /group/share");
the planner's emitted pin rule kept only the managed-group sentence. Re-raise rate measured 0.

Probes (2026-09-16, throwaway `when-probe.yaml`, three runs): (1) `when:` on a node inside a `loop_group` is
honoured pre-invocation and reads a JSON field of a sibling's output, which must be a bare JSON object;
(2) a dependent of a `when`-skipped node is itself skipped (`trigger_rule`); (3) `trigger_rule: all_done`
on the dependent (as `babysit.yaml:76`) makes it run after a skipped or failed upstream. Per-node `retry:`
is not honoured (v1 probe).

## Budget: the 116-minute scenario

| Component | measured | scenario | how |
|---|---:|---:|---|
| planning: docreview | 15 | 5 | item 7; latency bar p90 ≤ 5 min in the measurement gate |
| planning: ralplan, critic/revise (≤ 3 rounds), render, recon | 29 | 29 | item 5 changes hand stops, not active minutes (no saving claimed) |
| stages: implement, reader-audit, deslop, gates, smoke, handoffs | 31 | 31 | unchanged |
| review, both stages | 187 | 32 | 5 invocations: 2 × (discovery 8 + verify 4) + 1 spare discovery 8; live gate: ≤ 5 across both stages, ≤ 3 in any one |
| fixer, both stages | 43 | 18 | one closure-scoped repair per stage (v1 round-1 fixers: 7.9 + 3.3) plus a ≤ 3-min verify-round repair each |
| integration | 1 | 1 | unchanged |
| **total active** | **306** | **116** | slack 4 min |

Every number in the scenario column except "unchanged" rows is an assumption until measured: review cost
(item 2's gate: discovery p90 ≤ 8, verify p90 ≤ 4 — the bar equals the scenario, so a qualified topology fits
the budget by construction), docreview cost (item 7's gate, p90 ≤ 5), invocation count (≤ 3 per stage, live
acceptance), fixer minutes (live acceptance). If the faster qualified topology's p90 exceeds the bar, the
plan does not claim 120 min: the acceptance contract under Options re-derives the live gate from the
measured p90s and the PR states it. Replays are inside the 187 and are removed by item 1, not budgeted
separately. Wall time (human gates) is reported by `CHAIN_TIMING`, not budgeted.

## Principles

1. A completed activity is never re-executed by a resume; only the interrupted one is.
2. One review round has a fixed, measured cost envelope; personas do not scale with anxiety.
3. Convergence is closure of a finite finding set with evidence bound to the final candidate SHA.
4. The fixer repairs what was cited; when a repair must add behaviour, it says so and the next round is a
   full discovery.
5. Every new stop is typed, documented in RUNBOOK §3, and tested with a negative control.

## Decision drivers

1. Review invocations are 61 % of active time; nothing else moves the total on its own.
2. Replays (5 of 13 invocations, ~72 min) are the highest-confidence saving and cost nothing in quality.
3. Fixer scope growth turned a 2-round loop into 7.

## Options considered

- **A. Lane-owned review trio (2 discovery + 1 validator) + closure convergence + pin guard + durable reuse.**
- **B. Capped ce-code-review (fixed 3 personas, validator kept) + closure convergence + pin guard + durable
  reuse.** Measured like A; expected ~10 min/discovery.

Acceptance contract for the topology gate, **one formula used everywhere**: with `d` = measured discovery
p90 and `v` = measured verify p90 (minutes), the scenario is `84 + 3d + 2v` (84 = the non-review rows of the
budget; 3 discovery + 2 verify calls). Among the modes that meet parity and verify recall, the one with the
lower `d` ships. At the bar (`d ≤ 8`, `v ≤ 4`) the scenario is ≤ 116 and the live gate is 7200 s active. Above
the bar the mode still ships, and the live gate is `(84 + 3d + 2v) × 60 + 240` s (the same 4-minute slack),
stated in the PR as the measured target (e.g. B at 10/4 → 122 min, gate 7560 s). If no mode meets parity,
stop at the gate and report.

- **C. Replay fix + scope fix only.** ~130 min of review remain. Rejected on arithmetic.
- **D. Auto-write `accept-residuals.txt` at the cap.** Makes the number, not the outcome. Refused.

Chosen: the execution contract (items 1, 3, 4, 6) is topology-agnostic and is built first; the two review
prompts (A, B) and the `verify` prompt are then measured standalone (item 2) by a harness that invokes the
prompt bodies directly on fixed candidates, outside the lane; the one that meets the parity bar with the
lower p90 is wired into the lane. The default expectation is A (Adversarial Review, arXiv 2608.18167:
three agents outperform five; Systima: fan-out 2.6–5.9× tokens, never faster), but the plan does not assume it.

## Items

### 1. Durable round checkpoints — A (yaml, lite overlay), C (`setup/round-state.py`)

**Revised DAG of `review-loop`** (`:2022`): `round-pre` → `review` (`when: $round-pre.review == 'run'`) →
`review-gate` (`trigger_rule: all_done`; includes the read-only guard) → `fix-plan` (unconditional bash,
JSON only) → `fixer` (`when: $fix-plan.fixer == 'run'`) → `commit-fixer` (`all_done`) → `converge`
(`all_done`). `commit-fixes` (`:2223`) is deleted. `fixer` depends on `fix-plan`, so no skip propagates into
it. Probes 1–4 are the engine evidence; RUNBOOK §4 records them.

**Round state** is a set of files under `round-N/`, each written atomically (`tmp` + `mv`) by a helper
`setup/round-state.py` (read/write/derive; the nodes call it, no node writes these files by hand):

| file | writer | content |
|---|---|---|
| `attempt.txt` | `round-pre` | monotonically increasing attempt id `k` for this round |
| `pre-head.txt` | `round-pre` (first attempt only) | HEAD when the round opened |
| `review-input.json` | `round-pre` when deciding `review: run` | `{review_head, review_tree, base, scope, contract_digest, plan_digest, allowlist_digest, attempt}`; `review_tree` = tree sha of the candidate at review start (`git add -A` on allowlist paths into a temporary index, `git write-tree`) |
| `review-envelope.txt` | the reviewer session, last action, via `round-state.py mark review-done` | envelope ending with `Review complete`, `Input: <id>`, `Head: <sha>`, `Verdict:` |
| `review.ok` | `review-gate` after every gate passed, including the read-only guard; **deleted** by `review-gate` on any gate failure and by a rejection | `{gen, id, guard: PASS}` where `gen` = the `attempt` recorded in `review-input.json` for the envelope that passed (the gated envelope generation) |
| `review-rejected.txt` | a human, or the RUNBOOK recipe `round-state.py reject-review --reason` | rejection marker; the envelope is renamed `review-envelope.rejected-<k>.txt` for diagnosis |
| `gate.txt` | `review-gate` | `PASS`/`FAIL` |
| `ledger.json` | `review-gate` (merge envelope), `commit-fixer` (merge fixer result) | item 3 |
| `fixer-result.json` | the fixer session (unchanged) | partitions |
| `repair-start.json` | the fixer session, **first** action, via `round-state.py mark repair-start` | `{attempt, review_gen}`: from this point the fixer owns the tree |
| `repair.json` | the fixer session, last action, via `round-state.py mark repair-done` (runs `git add -A` on allowlist paths, `git write-tree`) | durable completion: `{attempt, review_id, review_gen, result_sha256, tree}` |
| `post-fix.json` | `commit-fixer` | `{tree}` written before `git commit`; recovery marker only |
| `fixer.ok` | `commit-fixer`, after a successful commit or a validated no-change result | attestation `{attempt, review_id, review_gen, result_sha256, tree, head, committed}` |
| `decision.json` | `converge`, last action | `{result: converged|progressed|blocked, next_mode, reason, head, id, gen, tree, attempt}` |

`converge.txt` stays a diagnostic tee; it is never read as state.

**Identity.** `id = sha256(review_head | base | scope | contract_digest | plan_digest | allowlist_digest)`.
`round-pre` recomputes the *expected* id from the recorded `review_head` (immutable, from
`review-input.json`) and the **current** contract, approved-plan and allowlist digests; the envelope's
`Input:` must equal it. A `feature-pin-amend` or a contract change therefore invalidates a completed review;
a fixer commit does not. Repair identity is the tree sha, independent of the review id.

**`round-pre` decision procedure** (current round N from `round.txt`; the counter never advances here
except at step 1):
1. If `round-N/decision.json` exists and is bound to the current state (`decision.head` == HEAD and
   `decision.id` == expected id): `converged` → this attempt is a terminal replay: every node no-ops and
   `converge` (the group's last node, probe 4) re-emits the promise; `progressed` → open N+1 (`round.txt`, new
   dir, `pre-head` = HEAD, `attempt=1`, mode from `next_mode`); `blocked` → do not open; re-evaluate this
   round (the blocked reason's recovery is a hand action; a resume re-runs converge on current files). A
   decision that is not bound (HEAD or identity changed) is ignored and the round is re-evaluated.
2. Otherwise stay on round N, `attempt = k+1`.
0. Reconcile a pending commit: if `post-fix.json.tree` exists and `fixer.ok` does not, and `HEAD^{tree}`
   equals that tree, run the idempotent ledger merge from `fixer-result.json` (item 3) and then write
   `fixer.ok` from `repair.json` + HEAD (the commit happened; the marker was lost).
3. `review: reuse` iff a complete envelope exists (`Review complete`, verdict in enum, `Input:` equal to the
   expected id, `Head:` equal to the recorded `review_head`) and HEAD ∈ {`pre-head`, `fixer.ok.head`}. A
   complete envelope that was never gated is reused and gated now (no rediscovery). Else `review: run` with
   `reason ∈ {initial, interrupted, identity, head-moved, gate-failed, rejected}` and a fresh `review-input.json`;
   `rejected` when `review-rejected.txt` is present (the marker is consumed and the envelope renamed).
   `round-reclaim.sh` is consulted only when no envelope exists.
4. Base validation (item 6). Print the JSON line `{"round":N,"review":"run|reuse","attempt":k,"reason":"…"}`
   as the only stdout; `ROUND_REUSE round=N review=… attempt=k reason=…` to the tee file.

**`fix-plan`** (bash, JSON only), requires `gate.txt == PASS`; `id` = expected review id; `T` = current
tree (`git add -A` on allowlist paths into a temporary index, `git write-tree`). **Authorization**: `review.ok` must exist with `id == expected id` and `guard == PASS`; its `gen` is the
authorizing envelope generation `G`. Without it fix-plan prints `{"fixer":"unauthorized"}` (the fixer is
skipped) and commit-fixer / converge fail `REVIEW_UNAUTHORIZED round=N`. Common validation `V(rec)` for a
completion record `rec ∈ {repair.json, fixer.ok}`: `rec.review_id == id`, `rec.review_gen == G`,
`fixer-result.json` present, validates (`check-fixer-result.py`) and its sha equals `rec.result_sha256`, and
`T == rec.tree`. A repair made for an earlier envelope generation (before a rejection or a failed gate) is
therefore never reused, even when the review identity is unchanged.
- `{"fixer":"reuse-committed"}` iff `V(fixer.ok)` and `fixer.ok.head == HEAD` and `HEAD^{tree} == T`;
- `{"fixer":"reuse-uncommitted"}` iff `V(repair.json)` and no `fixer.ok`;
- `{"fixer":"run","reason":"interrupted"}` iff `repair-start.json` exists for `G` and no `repair.json`: the
  fixer resumes over its own partial tree (the fixer prompt already handles pre-existing edits, `:2294`);
- a completed record whose `review_id`/`review_gen` match but whose tree or result differs from the present
  state → `FIXER_TREE_DRIFT round=N` (exit 1; unaccounted edits are never silently re-run over);
- else `{"fixer":"run","reason":"initial"}`. A matching HEAD alone never substitutes for `V`; a repair made for a different
  review identity (after `feature-pin-amend`) is never reused.

**`commit-fixer`** (`all_done`): on `reuse-committed` it re-runs the idempotent ledger merge and exits.
Otherwise it requires `V(repair.json)` (the fixer's durable completion record); a fixer that failed or left
no `repair.json` → `FIXER_INCOMPLETE round=N attempt=k`. Then, in order: (1) the idempotent ledger merge
from `fixer-result.json` (item 3; keyed by finding id, never downgrading a `closed` entry whose evidence
`head` is at or after this repair); (2) the pin guard on the staged index (item 4); (3) if `T == HEAD^{tree}`
(a no-change result: `applied == 0`, today's `COMMITTED=NO` path at `:2323`) → no commit, `fixer.ok` with
`head = HEAD`, `committed: false`; else write `post-fix.json.tree`, `git commit`, then `fixer.ok` with the new
HEAD and `committed: true`. Every step is idempotent, so a kill anywhere is recovered by re-running the node
(steps 1–2) or by `round-pre` step 0 (after the commit).

Recovery traces (each is an injection test):
- *First successful repair:* `fix-plan: run` → fixer writes `fixer-result.json` and `repair.json` → commit-fixer
  validates `repair.json`, commits, writes `fixer.ok` → converge. No `fixer.ok` is required before the commit.
- *Kill after the envelope, before the gate:* round-pre finds a complete envelope with matching id and
  `Head:` == `review_head`, HEAD == `pre-head` → `review: reuse`; review-gate gates it, writes `review.ok`.
- *Kill after the commit, before `fixer.ok`:* round-pre step 0 finds `post-fix.json.tree` == `HEAD^{tree}`,
  re-runs the ledger merge, writes `fixer.ok`; step 3 sees HEAD == `fixer.ok.head` → `review: reuse`;
  fix-plan → `reuse-committed`; commit-fixer merges again (no-op); converge.
- *Kill between the ledger merge and the commit:* `repair.json` still validates → `reuse-uncommitted`;
  commit-fixer merges again (idempotent) and commits.
- *Old repair, new failed gate (negative):* valid `repair.json`/`fixer.ok` from generation 1; the review is
  rejected and re-run as generation 2; the new gate fails (degraded, `review.ok` deleted) → fix-plan
  `unauthorized`, commit-fixer and converge `REVIEW_UNAUTHORIZED`; no commit, no promise.
- *Fixer interrupted after an edit, before `repair.json`:* gate passes (repair-start present) → fix-plan
  `run reason=interrupted` → fixer completes → `repair.json` → commit → attestation. Negative control: drop
  the `repair-start` clause → `REVIEW_WROTE_TREE` blocks the resume.
- *Ready verify round with nothing to fix:* fixer writes an all-empty result and `repair.json` with
  `tree == HEAD^{tree}`; commit-fixer attests without a commit; converge row 8 → CONVERGED.

**Read-only guard** (inside `review-gate`, so it runs on fresh and reused envelopes alike): the current
tree `T` must equal `review-input.json.review_tree` (the snapshot taken before the reviewer started) unless
`repair-start.json` exists for generation `G` (the fixer owns the tree from its first action; partial or
complete repairs are then its provenance, not the reviewer's); anything else fails `REVIEW_WROTE_TREE
round=N files=<n>` and `review.ok` is not written. `round-pre` applies the same rule when it re-snapshots for
an interrupted review attempt: a new `review_tree` that differs from the previous attempt's for the same
generation, with no `repair-start.json`, is `REVIEW_WROTE_TREE` at `round-pre` (a reviewer that edited and was
interrupted cannot bake its edit into the next snapshot). The reviewer is read-only; v1's autofix path and
its unreviewed-edit residual go away.

**`converge`** (`all_done`): requires the authorization (`review.ok` for `G`) and `V(fixer.ok)` with
`head == HEAD` and `HEAD^{tree} == T` (a successful commit-fixer attestation on a clean candidate; a
pin-guard or commit failure leaves none) → else `REVIEW_UNAUTHORIZED` / `FIXER_ABSENT round=N` /
`FIXER_TREE_DRIFT`; then applies item 3's table and writes `decision.json` (bound to HEAD, id, `G` and tree)
last; on a `converged` decision bound to the current HEAD, id, `G` and tree it re-emits the promise (terminal
replay); an unbound decision is ignored. `review-gate` writes `review-summary.json` before its final checks
(`:2191`), so a readable Ready summary is never treated as authorization; only `review.ok` is.

- Lite overlay `setup/lite/api/review-loop.round-pre.bash.sh`: same JSON line, always `review: run`, and it
  writes `review-input.json` with the same constituents, so `GATE_5` applies to lite too (no SKIP path for
  gate 5; the old `GATE_4` SKIP path is deleted with `GATE_4`).
- Tests: `test_round_state.py` (helper: identity recompute with changed plan digest → mismatch; commit-recovery
  three cases). `test_node_round_pre_reuse.py` — (a) `review.ok` + id match + HEAD = pre-head → reuse;
  (b) HEAD = post-fix head → reuse; (c) HEAD elsewhere → run `head-moved`; (d) envelope without
  `Review complete` → run and reclaim consulted; (e) plan digest changed → run `identity`; (f) `decision.json`
  converged → promise replayed; (g) progressed → N+1 opened with `next_mode`. `test_node_fix_plan.py` —
  tree match → reuse; result valid but tree differs → `FIXER_TREE_DRIFT`; repair-start without repair.json →
  run `interrupted`; no review.ok → unauthorized. `test_node_commit_fixer.py` — the recovery traces above; no-change attestation; `FIXER_INCOMPLETE` without
  `repair.json`; `FIXER_TREE_DRIFT` on an edited allowlisted file after a commit. Injection sequence (bash driving the node bodies with a kill
  after each boundary: envelope written, gate passed, `repair.json` written, ledger merged, `post-fix.json`
  written, commit made, `fixer.ok` written, decision written): zero **duplicate** activities — a `review: run` for a round
  whose complete envelope matched the identity, or a `fixer: run` when `repair.json`/`fixer.ok` matched — while
  first invocations (`reason=initial`) and genuinely interrupted ones (`interrupted`) are permitted.
  Negative controls: drop the identity recompute → (e) reuses; compare the worktree instead of the tree
  sha → the tree-differs case reuses; drop step 0 → the commit-before-marker trace re-reviews.

### 2. Review cost: measure, then pick the topology — B
- Build `setup/review-contract.md` (versioned; digest in `review-input`) and a `mode:trio` review prompt
  (`:2062`, add-only where doctrine-locked lines exist; where the persona instructions must go, the node body
  is replaced and `lane-doctrine.py update` is run once with the diff shown in the PR): one evidence packet
  (`Base:`, `Head:`, scope, diff, allowlist, `pinned_decisions` verbatim, gate-tests output, ledger from the
  previous round), two parallel discovery subagents (sonnet) — `correctness-and-spec`, `risk` — returning the
  compact finding shape of `vendor/ce-skills/3.2.0/ce-code-review/references/findings-schema.json` plus
  `finding_id = sha1(normalized(file) + "|" + normalized(title))[:12]` (the schema does not require
  `symbol`), one `validator` subagent that checks each cited line and claim and may only drop or downgrade.
  Specialist add-on on a concrete trigger (paths matching `migrations/`, `auth`, `payments`).
  `maxBudgetUsd: 8`, `timeout: 720000`; a timeout is a failed attempt, never a verdict.
- Build `mode:capped` for option B: the existing ce-code-review invocation with a fixed persona set
  (`correctness`, `security`, `adversarial`) and the validator pass, everything else unchanged.
- **Measurement gate** — harness `setup/tests/measure/review-bench.py`: loads the lane YAML, takes the named
  prompt node's `prompt`, `model`, `skills`, `maxBudgetUsd`, `timeout` (the node harness's `runnable_body`
  in `setup/tests/nodes/extract.py:51` handles bash only, so this is a new extractor), stages the skills the
  way `archon-run.py` does, and invokes the provider non-interactively (`claude -p --model <m>
  --output-format json`, cwd = the fixed worktree, `ARTIFACTS_DIR` = a scratch dir with the round files
  pre-seeded), recording wall time from spawn to the envelope's `Review complete` line and collecting the
  envelope and per-subagent artifacts; p50/p90 over the runs; recorded in the PR before the winner is wired.
  Both modes, three runs each, on the two v1 candidates (api `e42b242..85b43eb` = round-1 diff; mcp `86b8e13..f193473`); record
  p50, p90, and finding parity against v1's round-1 findings on each. Verification recall: run each mode's
  `verify` pass on the api round-3 → round-4 repair diff and check it finds the round-4 P1 (public→invite-only
  link revocation). Bar: every v1 P0/P1 re-found on both candidates, verify recall hit, p90 ≤ 8 min
  (discovery) and ≤ 4 min (verify) — the bar under which the 2-hour claim stands; the shipping and gate rule is
  the acceptance contract under Options (lowest `d` among parity-qualified modes; gate from the formula); if neither meets
  parity, stop and report.
- `review-gate` (`:2144`): `GATE_5_input_matches=PASS|FAIL` (envelope `Input:` equals the expected id and
  `Head:` equals `review-input.json.review_head`; replaces v1's `GATE_4` `Scope:` string compare, ambiguous
  since `789b2a1` labels the bootstrap-head comparison `delta`). On PASS it writes `review.ok`.
- Verdict enum is unchanged (`Ready to merge | Ready with fixes | Not ready`); "Ready" below means either of the
  first two. `parse-review-envelope.py:24` untouched.
- Tests: gate fixture (input match pass/fail); prompt-shape test (exactly two discovery roles and one validator
  in `mode:trio`); negative control: drop the `Input:` compare → mismatch passes.

### 3. Verification rounds and closure convergence — B (mode), A (converge)
- `review-mode.py` (`setup/review-mode.py:30-70`) gains mode `verify`: chosen when round N-1's ledger has
  ≥ 1 finding in state `applied` and no applied finding is P0 and no applied entry has `design_expanded: true`.
  It writes `review-scope.txt` (`full | verify`, separate from `review-base.txt`), `review-base.txt` = round
  N-1's `pre-head.txt`, and `review-findings.json` = the ledger. `full` for round 1, after a P0, after a
  design-expanding repair, and when the previous round is unreadable. `delta` is retired for the claude lane
  (its cost was never lower); the lite lane's SKIP path is unchanged.
- `verify` runs the validator role alone: for each ledger finding, `closed | open | regressed` with a cited
  line; a bounded regression scan of the repair hunks; verdict `Ready` iff no P0/P1 is `open`/`regressed` and no
  new P0/P1. Target ≤ 4 min (measured in item 2).
- Ledger `round-N/ledger.json` (`setup/ledger.py`; it also keeps writing `review-summary.json` exactly as
  `write-review-summary.py:9` does today, because `exit-gate:2487` reads its `verdict`):
  entries `{id, severity, state, evidence, head, round}`, states
  `open | applied | closed | regressed | deferred | pinned | waived | filed | incomplete | pin_conflict`. Update
  boundaries: `review-gate` merges the envelope (new findings → `open`; in `verify`, each verified id →
  `closed | open | regressed` with the verifier's cited line and `head`); `commit-fixer` merges
  `fixer-result.json` (`applied | deferred | pinned | waived | filed | incomplete | pin_conflict`); the merge is
  idempotent (keyed by id and attempt) and never moves a `closed` entry back to `applied` when the closure
  evidence `head` is at or after the repair. Historical entries are
  never dropped; the ledger is copied forward into round N+1 by `round-pre`. IDs persist by `finding_id`.
- **Positive closure requirement:** every P0/P1 id that ever entered the ledger must be `closed` (verified by
  a `verify` round at `head` == current HEAD) before convergence. `applied` is not closure; `deferred`,
  `filed` and `pinned` are not closure for P0/P1: a `filed` (cross-repo) P0/P1 keeps today's non-waivable
  `CROSS_REPO_FINDING` stop (`:2378`, exit-gate `:2478`); a P0/P1 that needs a pinned symbol is
  `pin_conflict` (row 1). P2/P3 may carry `deferred | pinned | waived` dispositions into `residuals.json`; `filed` is reserved for the
  fixer's `cross_repo` partition and always blocks (row 1b), whatever the severity.
- `converge` (`:2328`), repository scope, decision table evaluated **in this order, first match wins**
  (legacy scalar lanes keep the `MOVED` rule):

  | # | condition | result → `decision.json` |
  |---|---|---|
  | 1 | any ledger entry `pin_conflict` | `blocked` — `PIN_CONFLICT round=N symbol=<s>` exit 1 |
  | 1b | any `filed` entry (fixer `cross_repo`) | `blocked` — `CROSS_REPO_FINDING …` (existing line, unchanged) |
  | 2 | `fixer-result.incomplete > 0` | `progressed`, `next_mode=full` — `ROUND_PROGRESSED (incomplete)`; invariant from `check-fixer-result.py:4` |
  | 3 | verdict Not ready and no P0/P1 in `open|applied|regressed` | `blocked` — `NOT_READY_WITHOUT_BLOCKER round=N` exit 1 (reviewer contract violation; RUNBOOK: `round-state.py reject-review --reason …` then resume → `review: run reason=rejected`) |
  | 4 | a `verify` round found a new P0/P1 | `progressed`, `next_mode=full` |
  | 5 | **this round's** `fixer-result.json` applied a P0, or any of its applied entries has `design_expanded` (the accumulated ledger is not consulted here; persisted `next_mode` is authoritative for the next round) | `progressed`, `next_mode=full` |
  | 6 | `applied > 0` (any verdict) | `progressed`, `next_mode=verify` |
  | 7 | verdict Not ready (so some P0/P1 is `open|regressed` and nothing was applied) | `progressed`, `next_mode=full` — `ROUND_PROGRESSED (open blocker, no repair)`; the cap bounds this |
  | 8 | verdict Ready, positive closure holds, envelope `Head:` == HEAD | `converged` — `CONVERGED round=N` + `residuals.json` (P2/P3 dispositions) + promise |
  | 9 | verdict Ready, closure holds, `Head:` ≠ HEAD (`applied == 0` but the tree moved) | `blocked` — `REVIEW_TREE_DRIFT round=N` exit 1 (v1 line, kept) |
  | 10 | verdict Ready, closure does not hold | `progressed`, `next_mode=verify` — `ROUND_PROGRESSED (unverified P0/P1)` |

  The pre-round cap (`round-pre`, unchanged) is what bounds rows 2, 4–7, 10: at the cap `round-pre` fails
  `ROUND_CAP_REACHED` unless `accept-residuals.txt` exists (human act, unchanged). `next_mode` is persisted
  in `decision.json` and read by `review-mode.py`, which no longer infers it from the previous
  `fixer-result.json` alone.
- Typed lines: `REVIEW_MODE=verify base=<sha> findings=<n>`, `CLOSURE round=N closed=<a> open=<b>
  regressed=<c> new=<d>`, `NOT_READY_WITHOUT_BLOCKER`.
- Tests: `test_review_mode.py` reads `next_mode` from `decision.json` (verify; full after P0; full after
  design_expanded; full when absent). `test_node_converge_closure.py` — one case per table row in order,
  plus the sequence Not ready + P1 applied → `verify` → closed → Ready → CONVERGED, and the precedence cases
  (`pin_conflict` with `incomplete`; new P1 with an applied repair). Negative controls: drop the `Head:` ==
  HEAD check → row 9's case converges; drop the positive closure requirement → a Ready verdict with a P1
  still `applied` converges.

### 4. Pin provenance, pin guard on the staged index, fixer discipline — C (scripts), A (prompts)
- Planner prompt (`:340-360`, add-only): each `pinned_decisions` entry carries `rule` = the **complete bullet
  text** from the spec, `spec_line`, `allowed_change` (`none` or the exception the spec states).
  `validate-joint-plan.py` extracts every bullet under `## Interface (pinned)` and `## Pinned decisions` (and
  each `### <repo>` subsection) that names a backticked symbol or file, and fails `JOINT_PLAN=FAIL pin coverage symbol=<s>` when such a bullet has no
  entry whose `rule` equals the bullet text exactly (whitespace-normalized) — coverage, not substring.
- New `setup/pin-guard.py <artifacts> <baseline-sha>`: for each pinned entry with a `file`, read the symbol
  body from the baseline blob (`git show <sha>:<file>`) and from the **staged** blob (`git show :<file>`),
  so the guard sees exactly what will be committed; renamed/deleted files are `PIN_UNRESOLVED`. Extraction:
  a line-anchored heuristic for TypeScript methods/functions/arrow properties (header regex incl. decorators
  and multi-line signatures, body to the matching brace) and Python `def`; a symbol not found → `PIN_UNRESOLVED
  symbol=<s>` (exit 1, never a silent pass); signature change counts as a body change. The extracted spans are
  written to `round-N/pin-spans.json` for diagnosis. `ponytail:` comment names tree-sitter as the upgrade.
  Results: unchanged → `PIN_OK`; changed and `allowed_change == none` → `PIN_BREACH`; changed with an
  `allowed_change` → `PIN_CHANGED symbol=<s> allowed=<text>` (informational; the reviewer verifies the change
  is the allowed one).
- Wired into `commit-impl` (`:2016`) and `commit-fixer` (`:2315`) **after `git add`, before `git commit`**;
  `PIN_BREACH`/`PIN_UNRESOLVED` fail the node with the edit left staged.
- Recovery: new `feature-pin-amend <run> --token --symbol <s> --allowed-change "<text>" --reason` in
  `archon-run.py` / `feature_chain.py` (additive; `feature-scope-amend` at `archon-run.py:3462` /
  `feature_chain.py:2244` is allowlist-only and cannot amend a pin). Semantics copied from scope-amend:
  refused when any candidate handoff is verified (`feature_chain.py:2200`) and on a locally-verified chain;
  rewrites the approved plan's entry, regenerates the approval snapshot and MAC through the same path
  scope-amend uses (`feature_chain.py:2414`), refreshes the stage's `joint-plan.json`, records an audit row.
  Because the plan digest is part of the review identity (item 1), a completed review of the stopped round
  is invalidated and re-run after an amendment; that is intended. RUNBOOK recipe for
  `PIN_BREACH`: revert the staged hunk and resume, or `feature-pin-amend`, or reject and re-plan.
- Fixer prompt (`:2236`, add-only): P0/P1 always applied; when a P0/P1 repair adds a method, exported symbol,
  or a call into a pinned symbol, the entry carries `design_expanded: true` (next round is `full`). P2 applied
  only with a `suggested_fix` that adds none of those; other P2 → `advisory` with action `Deferred:` and the
  reason. A P0/P1 whose only repair changes a pinned symbol with `allowed_change: none` → new partition
  `pin_conflict` with the symbol. `check-fixer-result.py` validates the partition and the `design_expanded`
  flag; `setup/chain-acceptance.py` counts `pin_conflict` under clause 3 (see Verification for the checker's contract).
- Tests: `test_pin_guard.py` on TypeScript fixtures (unchanged → OK; body edit with `none` → BREACH; body
  edit with allowed_change → CHANGED; decorator-only change → CHANGED; symbol missing → UNRESOLVED; staged vs
  worktree divergence → the staged content decides). `test_joint_plan_coverage.py` gains the coverage check.
  `test_feature_chain_v2.py` gains `feature-pin-amend`. Negative controls: disable the body compare → BREACH
  passes; read the worktree instead of the index → the divergence case passes.

### 5. Planning cap — A
- `preflight` writes `plan-round-cap.txt=3` for repository scope (v1 wrote 2; both v1 attempts needed a hand
  raise to 3 and converged or were accepted there). No auto-accept: the critic gate's blocking rule
  (`:862`, P0/P1 at ≥ 75 blocks) is unchanged, and `plan-accept.txt` stays a human act.
- Test: node test on preflight (cap file content). Negative control: unset the env → no file.

### 6. Review base validation — B
- `round-pre` validates the chosen base: `git cat-file -e <base>^{commit}` and `git merge-base --is-ancestor
  <base> HEAD`, failing `REVIEW_BASE=FAIL base=<sha> reason=missing|not-ancestor` instead of any fallback to
  `origin/main`. `bootstrap` prints `BASELINE_BEHIND upstream=<n>` (informational; goes into the evidence packet
  as "upstream commits not in this candidate").
- Test: node test for both reasons; negative control: remove the ancestry check → a non-ancestor base passes.

### 7. Doc review cost — A
- `docreview` (planning, 15.3 min measured) runs the ce-doc-review persona fan-out on the plan. Replace its
  invocation with the same shape as item 2's winner: one plan reviewer (spec conformance, verifiability) plus
  the validator, `timeout` 8 min, envelope unchanged (`docreview-envelope.txt`, `docreview.diff`). Target ≤ 5 min.
- Measured in the same gate as item 2 on the v1 plan (`692005f7/plan.md`): parity on the doc findings that
  changed the plan, and p90 ≤ 5 min over three runs; if it misses, the scenario is re-derived.
- Test: existing docreview gate fixtures unchanged; prompt-shape test.

### 8. Infra pre-checks — A
- `preflight` (`:6`): the Claude session's OAuth expiry must be ≥ 3 h away (read from the CLI's credential
  store; if unreadable, `PREFLIGHT_WARN` not FAIL), else `PREFLIGHT=FAIL claude session expires in <m> min`;
  `ARCHON_OTP_SECOND_EMAIL` must match the stage `.env` whitelist. Negative control: expiry in 10 min → FAIL.

### 9. Telemetry — C
- `chain-timing.json` gains per stage `review_invocations`, `review_reused`, `review_duplicates`,
  `fixer_invocations`, `fixer_duplicates`, `rounds`
  (from round dirs); `CHAIN_TIMING` prints `reviews=<n>/<reused>`; budget cap from `ARCHON_CHAIN_BUDGET_S`
  (default 7200) applied to the **active** sum, with wall printed alongside.

## Typed strings added
`ROUND_REUSE`, `REVIEW_UNAUTHORIZED`, `FIXER_ABSENT`, `FIXER_INCOMPLETE`, `FIXER_TREE_DRIFT`, `REVIEW_WROTE_TREE`, `GATE_5_input_matches`, `REVIEW_MODE=verify`, `CLOSURE`,
`NOT_READY_WITHOUT_BLOCKER`, `PIN_OK`, `PIN_BREACH`, `PIN_CHANGED`, `PIN_UNRESOLVED`, `PIN_CONFLICT`,
`JOINT_PLAN=FAIL pin coverage`, `REVIEW_BASE=FAIL`, `BASELINE_BEHIND`, `PREFLIGHT=FAIL claude session`. Each
in RUNBOOK §3 (and §4 for the probe facts) and in `setup/tests/nodes/runner.py`.

## Ownership and order
- **A** (yaml incl. lite overlays, doctrine, derivatives, RUNBOOK, runner tokens): items 1 (yaml), 5, 7, 8;
  prompt halves of 2, 3, 4.
- **B** (`review-mode.py`, `review-contract.md`, `write-review-summary.py`, gate fixtures, measurement gate):
  items 2, 3 (mode + ledger), 6.
- **C** (`pin-guard.py`, `check-fixer-result.py`, `validate-joint-plan.py`, `feature_chain.py`,
  `archon-run.py`, `chain-acceptance.py`): items 1 (helper), 4, 9.
- Sequence: item 1 (execution contract) and item 3's ledger/`verify` prompt first; then item 2's standalone
  measurement (it needs the `verify` prompt, not the lane wiring); then item 4; then 5–9; the winner is wired
  last. Worktrees under the Goodword root (`.archon-2hv2-<worker>`),
  branch `feat/archon-2h-v2` off `789b2a1`. Never push `main`. Every commit: no risk-delta path
  (`review_delta_*`, `review_policy*`, `review_session`, `review_verification`, `review_qualification`).

## Verification
1. `python3 -m unittest discover -s setup/tests` green except the six baseline failures on `main`
   (`test_backfill_disposable_postgres_matrix`: `test_database_matrix_subprocesses_use_owned_disposable_docker_resources`
   error + 3 subtests of `test_api_backfill_matrix_sources_are_present_at_selected_root`;
   `test_bugfix_supervision`: `test_conflict_supervision_launches_one_same_provider_successor`,
   `test_three_failed_fix_runs_create_two_successors_then_trip_architecture_breaker`); `NODE_STRESS=100` on
   edited nodes; every negative control run; `bash -n` / `py_compile`; derivatives + doctrine.
2. Measurement gate (item 2) recorded in the PR with both modes' numbers and the pick.
3. Injection sequence test (item 1) recorded: zero duplicate activities across the eight kill points plus the
   interrupted-fixer and failed-gate traces.
4. Live: ENG-3866 v2 as a **new** chain on the same spec. Accept when: active time (`CHAIN_TIMING` sum of
   phase segments) ≤ the gate from the acceptance contract (7200 s at the bar; `(84 + 3d + 2v) × 60 + 240`
   above it, as stated in the PR); ≥ 1 `verify` round per stage; `review_duplicates == 0` and `fixer_duplicates == 0` (a duplicate is a `run` for an activity whose
   completion record matched the current identity and candidate), every `review: run` carries
   `reason ∈ {initial, interrupted, identity, head-moved, gate-failed, rejected}`, and review invocations ≤ 5 across
   both stages and ≤ 3 in any one; 0 `PIN_BREACH`; integration green first attempt; `setup/chain-acceptance.py`
   PASS or PARTIAL naming only `residuals.json` dispositions. No `feature-publish`. Wall time reported, not gated.
   The checker moves into the repo in this PR from the Goodword root's `.omc/scripts/chain-acceptance.py`
   (outside `.archon`, hence invisible to the reviewers); contract: `chain-acceptance.py <chain-id>` reads the
   chain state and each stage's artifacts and prints `ACCEPTANCE=PASS` or `ACCEPTANCE=PARTIAL failures=<n>` with
   one `FAIL <repo>: clause<k> …` line per failure. Clauses: 1 no `accept-residuals.txt`/`yield-stop.txt`; 2 no
   `waivers.md` headings; 3 no `failed`/`incomplete`/`cross_repo`/`pin_conflict` fixer partitions in any round;
   4 final `converge.txt` is `CONVERGED round=N`; 5 `smoke-result.txt` starts `SMOKE=PASS`; 6 integration evidence
   `passed` and every `acceptance_criteria` id covered by a scenario. v2 adds `residuals.json` dispositions as
   the only PARTIAL reasons the live gate tolerates.

## Pre-mortem
1. The trio misses a P1 the persona review caught → item 2's parity bar fails on that candidate; option B ships
   (budget 122) or, if B also fails parity, the plan stops at the gate and reports.
2. The pin guard mis-extracts a TypeScript body (overloads, arrow properties, decorators) → `PIN_UNRESOLVED`
   or a false `PIN_BREACH` blocks a commit with the spans logged; recovery is the staged-hunk revert or
   `feature-pin-amend`; tree-sitter is the upgrade. A silently missed span is the worse failure and is why
   "not found" is a typed stop, not a pass.
3. A resume reuses a review whose envelope was truncated after `Verdict:` → `Review complete` and `Input:`
   are both required; the injection test includes a truncated envelope.
4. `verify` accepts a repair that broke an assumption outside its hunks (the tension the Architect named) →
   `design_expanded` and any new P0/P1 force the next round `full`; the verification recall check in item 2
   measures exactly this on the v1 round-3→4 repair.

## ADR
- **Decision:** build a durable, closure-based review loop with pinned-symbol enforcement; choose the review
  topology by measurement between a lane-owned trio and a capped ce-code-review.
- **Drivers:** review = 61 % of active time; replays = 72 min; fixer scope growth caused five extra rounds.
- **Alternatives:** C (replay + scope only), D (auto-accept) — see Options.
- **Why chosen:** it is the shape whose arithmetic reaches 120 min at the measured bar; each part has a
  measured cause; the assumptions (review, docreview, fixer, invocation count) are each gated by a
  measurement or by the live run before the claim is made.
- **Consequences:** the review contract becomes the lane's (either mode); a new ledger schema and a
  `feature-pin-amend` operation enter chain state; the lite lane gains a no-op reuse line.
- **Follow-ups:** tree-sitter pin guard; consumer-stage `verify_only` on unchanged exported contracts.

## Sources
- Is Three the Magic Number? An Empirical Evaluation of LLM-Based Repair Loops, arXiv 2607.05197.
- Adversarial Review: Structured Disagreement for Grounded Agentic Code Review, arXiv 2608.18167.
- SWE-Review, arXiv 2607.06065. SWR-Bench, arXiv 2509.01494. The Specification as Quality Gate, arXiv 2603.25773.
- ContextCov, arXiv 2603.00822. Temperature and Persona Shape LLM Agent Consensus, arXiv 2507.11198.
- Systima, "The Subagent Tax" (fan-out 2.6–5.9× tokens, never faster).
- Codex (gpt-6-astra) brainstorm and Architect pass, 2026-09-16: `.omc/plans/codex-brainstorm-1.md`,
  scratchpad `claudex-arch-1.md`.
