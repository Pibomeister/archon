---
name: archon-sdlc
description: Use when driving or supervising a Goodword Archon SDLC, bugfix, or backfill run - starting full-sdlc-api on a feature spec, bugfix on a bug report, or backfill on a backfill spec, reading the plan-gate, RCA-gate, or backfill-packet, interpreting loop exits (CONVERGED, NO_PROGRESS, FIXER_BLOCKED, SCOPE_BREACH, ROUND_CAP_REACHED, CHAIN_CONFLICT, FIX_STALLED, ARCHITECTURE_SUSPECT, NEGCONTROL=FAIL, CLAIM_DIVERGED, SAMPLE_SUSPECT, BOUND_BREACH, RECONCILE_FAIL, PLAN_REJECTED, PLAN_NO_PROGRESS, PLAN_SCOPE_DISPUTE, PLAN_ROUND_CAP, RCA_PLAN_REJECTED, RCA_PLAN_SCOPE_DISPUTE, RCA_PLAN_SHAPE=FAIL, CRITIC_GATE=FAIL, IMPACT=UNAVAILABLE, IMPACT=SKIPPED, DESLOP=DIRTY, DESLOP_GATE=FAIL, DESLOP_REVIEW=FAIL, DESLOP_ROUND_CAP), deciding resume vs escalate, or running babysit/cleanup afterwards. Triggers on "archon run", "start the SDLC lane", "archon bugfix", "archon backfill", "the run is stuck", "resume the run", or any mention of a paused/failed archon workflow.
---

<WORKFLOW-NODE-STOP>
If you are an Archon workflow node session (your prompt came from a `full-sdlc-*`,
`bugfix`, `backfill`, `babysit`, or `cleanup` node), ignore this skill entirely. It is written for the
operator session that drives runs from the outside. Shipped workflows declare only
`skills: [ce-code-review]` / `[ce-doc-review]`; anything else reaching a node is
leakage. Do what your node prompt says and nothing here.
</WORKFLOW-NODE-STOP>

# Driving the Archon SDLC pipeline

Archon takes a feature spec to a merge-ready PR through four lanes:
`full-sdlc-api` -> `full-sdlc-web` -> `babysit` -> `cleanup`. This skill is the
decision procedure. The reference is `$ROOT/.archon/RUNBOOK.md` - every section
below names the RUNBOOK section that has the verbatim discriminators and recipes.
Read that section before acting on anything non-obvious.

`$ROOT` is the Goodword root: the directory holding `.archon/config.yaml`,
`api/`, `web-app/`, and `goodword-kb/`. Archon is registered against that folder;
every command in this skill runs from there.

## 0. Three hard guardrails

**Never run `archon workflow approve`, `reject`, or `abandon`.** The plan gate is
the pipeline's single human approval, and an agent that can release its own gate
has no gate. The derived allowlist excludes those verbs deliberately. Your job at
a pause is to *render the packet for the human and stop* - summarize it, name what
you would question, and hand back. Same for `abandon`: killing a run destroys
work a human may want.

**Never merge a PR, never close one, never delete a remote branch that has an
open PR.** The pipeline's terminal state is merge-ready, by design (RUNBOOK §8).

**Never write `accept-residuals.txt`, and never edit `files-allowlist.json` to
clear a `SCOPE_BREACH`.** Both are defined as human acts - the edit *is* the
approval (RUNBOOK §3). You may read them, diff them, and explain exactly what
edit would unblock the run. You may not make it.

## 1. Starting a run

```bash
cd "$ROOT"
DISABLE_OMC=1 archon workflow run full-sdlc-api "$ROOT/.omc/research/<spec>.md" </dev/null 2>&1 | tee /tmp/archon-run.log
```

Every part of that line is load-bearing (RUNBOOK §1):

- **The run message is the absolute path to the spec.** Empty fails preflight with
  `PARAMS=FAIL`. Branch and worktree derive from the filename: `my-feature.md` ->
  branch `archon/my-feature`, worktree `api/.worktrees/my-feature`, durable in the
  run's `params.json`.
- **`DISABLE_OMC=1`** on every archon invocation, or node sessions write
  `.omc/state/` into the repo worktree and repo lint fails on the contamination.
  Never shell into a workflow worktree from an OMC-hooked terminal either.
- **`</dev/null`** so the CLI cannot block on stdin.
- **`| tee` to a file, never through a filter.** Failure-event stdout tails are
  truncated and unreliable; the teed file is the only complete record. Filter at
  read time.
- **Fresh `aws login` first** - the api boots against Secrets Manager and SSO
  credentials expire in about 15 minutes.
- Run from `$ROOT`, not from inside a child repo.
- **If you are starting the run for someone rather than watching it yourself,
  add `--detach`.** A foreground launch blocks until the run pauses at its
  gate, which is twenty minutes or more on the api lane and longer than an
  agent tool call will wait; if the launching process is killed the run dies
  with it. `--detach` returns at once, prints the child's log path, and the run
  shows up in `archon workflow runs` like any other. The pause is not pushed
  to you: watch the child log for the literal `Workflow paused — waiting for
  approval.` line (recipe in §3 "How to hand a pause back"), then read the
  packet and hand back per §3 in the same turn — never leave a paused run
  unannounced. Starting a run is allowed and always was; only approve, reject, and
  abandon are off limits (§0), and the derived allowlist reflects exactly that
  split — `archon workflow run|resume|get|runs|status` are permitted verbs.

The web lane is still toy-pinned (no `params.json`, inlined worktree). **Real
tickets are api-lane-only.** Do not start `full-sdlc-web` on a real spec.

Before starting anything large, read §6 below - a run spends the operator's Claude
subscription window.

## 2. Spec authoring

If you are asked to write or review the spec, the section that matters most is
`## Premises to verify` (RUNBOOK §2). It is a machine-read contract, not prose:

- Numbered questions. For each one the planner must write a **cited** answer to
  `premises.json` - at least one evidence item whose `file` exists in the worktree
  and whose `quote` appears verbatim in it. `plan-snapshot` greps for the quote
  and fails the run on an uncited answer.
- `premise-verify` then re-derives every answer **blind** (questions only, no
  planner reasoning) and `premise-gate` hard-stops on any `conflict`.
- Put a question there for anything the plan's correctness depends on. Leave the
  section out when the spec asserts nothing checkable.

This exists because the first real-ticket run answered a spec question with a
plausible argument instead of a code check, and ten review rounds inherited the
wrong premise. A premise you cannot cite is a premise you have not verified.

The planner also writes `files-allowlist.json` (every path the unit may touch -
the scope gate's contract) and `reader-audit.json` (columns whose interpretation
or presentation semantics the plan changes; `{"columns": []}` when none).

## 3. The plan gate - the highest-leverage moment

The run pauses (`status: paused`) after doc-review and prints
`RENDER_GATE=PASS packet=file://<...>/plan-review.html`. It opens in a browser
when an opener is available; the path is printed either way, so read the file
directly if it did not open.

Seven sections (RUNBOOK §2a): **GIST** plain-language summary, **KB** what prior art
the plan honors, **MAP** files and units, **PLAN** verbatim, **REVIEW** doc-review's
applied edits plus unapplied findings and a "Premise check" block, **CRITIC** the
planning-critic loop's findings (below), **DECIDE** the commands.

What to do here:

1. Read the packet and the raw `plan.md` next to it.
2. In the Premise check block, call out every `cannot_determine` verdict by name.
   Those are the plan's soft spots - the blind verifier could not confirm the
   claim from code. (`conflict` never reaches this packet; it stops the run at
   `premise-gate` with `PREMISE_CONFLICT id=N`.)
3. Read the CRITIC table (RUNBOOK §2a/§3a; added VERSION 2026.08.28-2). It's a
   per-round table of the plan-loop critic's verdict and open P0/P1 finding count
   by kind (scope, regression, gap, verifiability), followed by every finding the
   plan-loop reviser DECLINED in the final round, quoted verbatim alongside its
   justification. **Every declined finding is a soft spot exactly like a
   `cannot_determine` premise** - the critic raised something and a separate
   session decided not to act on it. Call each one out by name; do not read a
   declined finding as resolved just because it did not stop the loop. If the
   impact table is missing, the packet says why (`IMPACT=UNAVAILABLE` or
   `IMPACT=SKIPPED`) - that means unprobed, never "no callers".
4. Check the MAP against the spec: files the plan will touch that the spec never
   mentioned, and spec requirements with no file behind them.
5. Report all of that to the human and **stop**. One wrong plan line becomes a
   thousand wrong code lines; do not rubber-stamp, and do not release the gate.

### How to hand a pause back

**The pause is never delivered to you — you have to watch for it, and the moment
you see it you hand it back, in that same turn.** A detached run prints the
literal line `Workflow paused — waiting for approval.` (plain text, not a JSON
event) into its child log under `~/.archon/logs/`, and `archon workflow status
<id>` flips to `Status: paused`. Nothing else announces it: the browser opening
`plan-review.html` is not a hand-back, and a monitor that greps for a
`workflow_paused` JSON event will sit silent through the whole pause (this
happened; the human found the packet on their own and had to ask). Watch for it
explicitly:

```bash
LOG=~/.archon/logs/detached-run-cli-<...>.log   # printed by --detach
until grep -q 'Workflow paused' "$LOG" \
   || ! DISABLE_OMC=1 archon workflow status <run-id> 2>/dev/null | grep -q 'Status: running'; do
  sleep 60
done
```

When it fires, post the hand-back below immediately — the packet reading and
all three commands with the run id filled in. Ending a turn on a paused run
without those commands in front of the human is the failure mode; the human
cannot act on a packet they have not been told is waiting on them. If the
human works from an agent session, say that `!` in front of the command runs it
there (`! DISABLE_OMC=1 archon workflow approve <run-id> ...`).

Your report is the whole interface for someone who has not read this runbook, so
lead with the state and not the mechanics. The run finished this stage and is
paused waiting on a person, which is how the stage is supposed to end. Say that
first, before any recommendation. A reader who thinks "paused" means "broken"
will spend their time debugging a healthy run.

Then your reading of the packet, then the decision. Give all three commands with
the run id already filled in (it is `basename "$ARTIFACTS_DIR"`, and the DECIDE
box in the packet carries it too), and say which one you would pick and why:

```bash
DISABLE_OMC=1 archon workflow approve <run-id> </dev/null >/tmp/archon-approve.log 2>&1 &
DISABLE_OMC=1 archon workflow reject <run-id> "why you are stopping it"
DISABLE_OMC=1 archon workflow abandon <run-id>
```

- `approve` runs the rest of the workflow inside the terminal it is typed in and
  holds that terminal until the run ends, which is why it is backgrounded. If
  the resume fails the approval is still recorded and
  `archon workflow resume <run-id>` picks it up.
- `reject` behaves differently per gate, because Archon only reworks when the
  approval node declares an `on_reject` block. Two gates declare one:
  **`full-sdlc-api` plan-gate** and **`bugfix` rca-approval**. There, reject
  records your reason, hands it to a revision pass that rewrites the plan or
  RCA and re-renders the packet, and pauses at the same gate again. Three
  rejections cancel the run. Reject resumes the run in the terminal it is
  typed in exactly like approve, so background it the same way.
- At the other three gates (`bugfix` smoke-approval, `bugfix-smoke-deployed`,
  `backfill` apply-approval) there is no handler and reject ends the run.
  Those gates are deliberately terminal: a smoke matrix that fails needs a
  human looking at the app, not a rewrite, and the backfill gate's upstream
  proofs (the arm node's kill-switch negative control, the render gate's
  byte-comparison of the armed command) do not re-run on a revision, so
  reworking past that gate would hand the human an unverified packet.
- `abandon` ends any run outright with no reason recorded and no rework.
- The revision pass is the only node that runs before the gate pauses again.
  Upstream gates do not re-fire, so a revised plan's premises were never
  re-derived blind. The revised packet says so; read that line.
- For a toy or install-validation run whose only job was to reach this gate,
  say plainly that the run already succeeded before recommending how to end it.
  There, `abandon` is cleaner than `reject` — rejecting spends a revision pass
  rewriting something disposable.

None of the three appear in `archon --help`. Say so in the same breath, or the
human checks `--help`, does not find them, and concludes you invented them.

They run the command. You never do (§0).

## 4. Supervising the review loop

Between `implement` and `ship` sits a per-repo loop: review -> commit fixes ->
fixer -> converge. It ends in exactly one of four ways (RUNBOOK §3), and the
discriminator string is verbatim in `round-N/converge.txt` in the run's artifacts:

| Verbatim | Meaning | Action |
|---|---|---|
| `CONVERGED round=N` | Verdict acceptable, HEAD unchanged, tree clean | None - the run proceeds |
| verdict acceptable, HEAD moved | Fixes landed; next round re-reviews them | None - expected |
| `NO_PROGRESS` | `Not ready` AND HEAD unchanged - the fixer is not moving the needle | Escalate. Semantic, not budget-shaped |
| `FIXER_BLOCKED` | Fixer reported a P0-P2 it cannot fix, or wrote no result file | Escalate. Read the `failed` partition first |

Three rules that decide most supervision calls:

- **`round.txt` is the only number that means anything.** A failed `loop_group`
  resumes with a fresh iteration counter; `max_iterations` bounds per-invocation
  work only. The durable counter is `round.txt` in the artifacts dir (RUNBOOK §4).
- **Resume is cheap.** Completed AI nodes never re-run; only failed bash gates
  re-execute. Resume does not restore AI session context, and it does not need to -
  every post-gate node re-reads its inputs from disk.
- **Trust files over remembered node output.** Node outputs may not survive a
  resume; every consumer has an envelope-file fallback. When debugging, read
  `$ARTIFACTS_DIR`, not the transcript.

Also know the "green run that did nothing" modes (RUNBOOK §3, end): an unparseable
`when:` silently skips its node and the run still reports SUCCESS; a node whose
prompt was refused still reports Completed; a degraded review can report
completion without reviewing. **If a run is green but a downstream step complains
about a missing artifact, treat the run as failed and read the log, not the status.**

Budget expectation, measured on the first real ticket: **about two fix rounds
after implement.** The loop converges on toy diffs and *diverges* on real ones -
more rounds past that is a signal to look, not to wait.

**Before `review-loop`, `gate-tests` feeds `deslop` and `deslop-verify`** (RUNBOOK
§3b; VERSION 2026.08.28-2; api-lane path observed live 2026-08-28 on d3aa3b55). Every path into
`review-loop` depends on `deslop-verify`, so no un-deslopped code reaches a
reviewer. Stops here:

- `DESLOP_GATE=FAIL <guard>` (typecheck/lint/unit/scope/slop) - the writer's own
  cleanup broke something. Hand-fix the worktree, resume; resume re-enters the
  loop fresh and re-checkpoints.
- `DESLOP=DIRTY round=N blocking=N` - the independent reviewer session filed a
  finding at confidence >=75. There is no automatic writer retry: hand-fix the
  flagged issue in the worktree yourself, then resume.
- `DESLOP_ROUND_CAP round=N` - the DIRTY-verdict counter hit 2. Same
  resume-after-hand-fix pattern, or accept and ship with the residual noted.
- `DESLOP_REVIEW=FAIL coverage incomplete round=N <reason>` /
  `DESLOP_REVIEW=FAIL malformed finding round=N <reason>` /
  `DESLOP_REVIEW=FAIL verdict inconsistent round=N declared DIRTY with 0
  blocking findings` - a schema failure on the reviewer's own
  `deslop-review.json`. Resume re-runs the reviewer fresh; recurring on the
  same shape means the reviewer prompt drifted - escalate.
- **`DESLOP_REVIEW=FAIL reviewer modified tree` - never plain-resume this one.**
  The reviewer session is told to write only `deslop-review.json`; if a single
  byte of the worktree differs from the pre-review checkpoint, the gate refuses
  to trust anything the reviewer wrote and does not even validate its JSON. The
  failure output prints the exact restore commands (`git reset --soft`, then
  `git read-tree <checkpoint> && git checkout-index -af`, then `git clean -fd`).
  **Run those by hand first, THEN resume** - `deslop-recheck` re-takes the
  checkpoint on every iteration, so resuming without restoring first silently
  adopts the reviewer's edits as the new baseline and blesses them as if the
  writer had made them. On a `web-app` bugfix this restore also reverts the
  bootstrap's deliberate lockfile drift; re-run the unfrozen install after
  restoring (RUNBOOK §12).

## 5. When to resume, and when to stop

Resume — always via `bash .archon/setup/resume.sh <run-id>`, which wraps `archon workflow resume` with a
wrong-run guard (archon 0.8.0 resumes the NEWEST failed run of the lane on the path, not the id you pass; RUNBOOK §4) —
is right for the transient class:
`dag.node_empty_output` / "provider stream closed without yielding content", a
review gate that ended "waiting for background reviewers", and a session-limit
message once the stated reset has passed. All observed, all cleared by a plain
resume (RUNBOOK §3).

**Stop and escalate to a human - do not resume** on any of these (RUNBOOK §11):

- `FIXER_BLOCKED` - a semantic defect the fixer cannot clear
- `NO_PROGRESS` - review and fixer are deadlocked
- `CI_RED` - CI is genuinely failing
- a cross-repo divergence (one repo converged, the other exhausted)
- **the same node hitting its budget cap on consecutive resumes**
- `PREMISE_CONFLICT id=N`, `READER_AUDIT_FAIL`, `SCOPE_BREACH`,
  `ROUND_CAP_REACHED` - each is a designed human stop with its own recipe in
  RUNBOOK §3. Read the artifact, explain what the fix would be, hand back.
- `PLAN_REJECTED`, `PLAN_NO_PROGRESS`, `PLAN_SCOPE_DISPUTE`, `PLAN_CONVERGE=FAIL`
  (RUNBOOK §3a) - the plan-loop critic and reviser disagree, or the loop stalled.
  Read `plan-round-N/critique.json` and `revision.json`, explain the disagreement,
  hand back. `PLAN_ROUND_CAP` alone is resumable after raising the cap or
  accepting the loop's last state by hand.
- `PLAN_ROUND_PRE=FAIL` (RUNBOOK §3a) / `RCA_ROUND_PRE=FAIL` (RUNBOOK §12) - the
  round counter (`plan-round.txt` / `rca-round.txt`) is not an integer, or a
  planning artifact the round needs is missing. Both mean a hand-edited or
  truncated artifacts dir, not a model failure, and a plain resume re-runs the
  same broken pre-check. Inspect the artifacts dir, fix the file by hand, then
  resume. A junk round **cap** does not stop a run (it falls back to 3); only a
  junk **counter** does.
- `DESLOP_REVIEW=FAIL reviewer modified tree` (RUNBOOK §3b) - **never plain-resume**;
  run the printed restore triple first (§4 above), then resume.
- `DESLOP=DIRTY`, `beyond_five_guards` findings (bugfix lane, RUNBOOK §12) - hand-fix
  the flagged issue in the worktree before resuming; there is no automatic writer
  retry.

More iterations on any of these only burn quota. When you escalate, say which
discriminator fired, which artifact file holds the evidence, and what the RUNBOOK
recipe for it is.

Never "fix" a verifier to get past a gate. If `premise-verify` contradicts the
plan, the plan or the spec is wrong.

## 5a. Concurrency: one run per project path, by measurement

Archon's run lock keys on `working_path` and nothing else, and the Goodword root is a
"folder" project, so **every run of every lane shares one lock**. Measured 2026-08-30
(`workflows/lock-probe.yaml`, a zero-spend probe that holds its node 45 s; full analysis
RUNBOOK §5a):

- A second `workflow run` of ANY lane while one is `running` **or `paused`** is created
  and instantly self-cancelled: `Workflow already active on this path (<status>): <lane>`.
  A paused run at a gate holds the lock until a human decides it.
- Symlinking a second directory does not help — the CLI realpaths the cwd.
- `--branch <b>` DOES give each run its own worktree `working_path` (two probe runs ran
  concurrently in a scratch git project), but it requires the project to be a git repo
  with an `origin` remote, which the Goodword root is not today. Making it one, and
  parameterizing the hardcoded smoke ports (4123 sdlc / 4124+3124 bugfix), are the two
  changes that would make runs truly independent — a supervised decision, not a launch-time
  flag. Until then:

**Operating rules.** Before any launch or resume, `archon workflow runs` and clear the
path: wait, `approve`/`reject` the paused run, or `archon workflow abandon <id>` a dead
one. Never queue a second run and walk away — it is already cancelled. To test whether
the path is actually free, launch `lock-probe` (costs nothing, exits in ~45 s). And
because several failed runs of one lane can accumulate on the path, resume is only safe
through `setup/resume.sh` (§5) — the raw CLI resumes the newest resumable run, not the
one you name.

## 6. Quota - the real currency

Runs bill the **Claude subscription via OAuth login**, not an API key.
`total_cost_usd` is cost-equivalent accounting, not an invoice (RUNBOOK §7).

- The currency is the operator's 5-hour window and weekly quota, **shared with
  their own interactive Claude use**. A runaway run locks them out of Claude for
  hours; it does not produce a bill.
- Window exhaustion hard-stops the run (`claude.rate_limit_event`). No cap value
  protects against it. Say so before proposing anything large.
- **Never set `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_API_KEY`,
  `CLAUDE_CODE_OAUTH_TOKEN`, or `ANTHROPIC_PROFILE`, and never configure
  `apiKeyHelper`.** Any of them silently outranks the subscription and flips
  billing to metered API. Preflight and the installer both assert this; do not
  "fix" a preflight failure by unsetting the assertion.
- `total_cost_usd` resets per resume process - a resumed run reports the last
  process only.

## 7. Aftercare

- **`babysit`** watches CI per repo, runs the mechanical thread lane, and flips
  draft -> ready (RUNBOOK §8). `NO_OPEN_PR` is a vacuous pass. `CI_RED` is a hard
  exit. `NO_CI_RUNS` on the web lane means web's `pr.yaml` was gated on base
  `main`/`prod` and a stacked PR got no CI - a lone bot pass is not a build.
  Threads routed to needs-human are the human's; do not answer them as the author.
- **The merge click is the human's.** Always.
- **`cleanup`** kills stray 4123/3123 PIDs by PID, removes worktrees, and deletes
  remote branches only when no open PR uses them. It **refuses** non-`archon/*`
  branches and dirty trees, exiting 1 as `CLEANUP=PARTIAL`. That refusal is
  deliberate (RUNBOOK §10) - report what it refused, do not force past it. Port
  sweeps touch only 4123 and 3123; kill by PID, never `pkill -f`.
- **KB duty (RUNBOOK §9).** Each lane's `kb-capture` writes exactly one file to
  `goodword-kb/wiki/change-history/`. Its "Promotion candidates" section is raw
  intake. After a run ships, say `kb:compound` in an interactive session to curate
  those candidates into glossary/ADR/pattern pages. Unattended runs never touch
  curated pages - if the KB is to compound, that pass has to happen.

## 8. Environment traps worth remembering

Full list in RUNBOOK §6. The ones that most often look like a code bug:

- **Vite binds IPv6 `::1` only on macOS.** Probes use `localhost`; a hand-check
  with `curl 127.0.0.1:3123` "proves" the server is down when it is not.
- **mise shims do not apply in bare execs** - anything detached must pin
  `mise x node@20 --` (web) / `mise x node@22 --` (api) or it runs ambient Node.
- **`pnpm install --frozen-lockfile` refuses on main's lockfile drift** - the
  workflows install unfrozen and exclude `pnpm-lock.yaml` from every commit.
- **A branch-deletion push still fires husky pre-push**, so delete pushes go
  `--no-verify`.
- **Nodes start at the non-git folder root with no git context**, which is why
  every repo path in the workflows is absolute and rendered per machine at install
  time. Do not "fix" one to a relative path.
- On Linux: the packet opener is `xdg-open`; `/usr/bin/open` there is util-linux's
  `openvt(1)`, a different program. The gate prints the `file://` path regardless.

## 9. The bugfix lane

`bugfix` is the sibling graph for bugs: bug report .md in, draft PR out, through
Red -> Green root-cause discipline. RUNBOOK §12 has the verbatim discriminators.

```bash
cd "$ROOT"
DISABLE_OMC=1 archon workflow run bugfix "/abs/path/to/bug-report.md" </dev/null 2>&1 | tee /tmp/archon-bugfix.log
```

Everything in §0-§1 applies unchanged (guardrails, `DISABLE_OMC=1`, `</dev/null`,
tee, quota). Differences that decide supervision calls:

- **Before gate 1, `chain-gate` feeds `rca-plan-loop`** (RUNBOOK §12's RCA
  planning-critic table; VERSION 2026.08.28-2, design-only - not yet observed
  live), the same critic/reviser/converge shape as `plan-loop` in §3-§4 but
  with the diagnosis frozen for the whole loop: `rca.md`, `causal-chain.json`,
  `hypotheses.json`, `residuals.json`, `probe.json`, `repo.json` are read-only
  to the critic and reviser, and `RCA_PLAN=FAIL immutable artifact modified`
  is a hard engineer stop if any of them moved. `rca-plan-shape` re-validates
  the four mutable planning files once the loop exits, before
  `experiment-design` ever runs.
- **Two human gates.** Gate 1 is the RCA packet: `rca-review.html` shows the
  evidence chips, the cited 5-whys chain with the blind verifier's per-link
  verdicts, the live-experiment verdict (a `cannot_determine` dispute triggers
  a real run of the code that settles it — the EXPERIMENT section says
  skipped / degraded / observed), the residuals table (every reported symptom
  gets a disposition; separate-bug rows become split tickets, never silent
  scope-outs), a **CRITIC section** (VERSION 2026.08.28-2, design-only,
  positioned between RESIDUALS and FIX — same per-round table and declined-
  findings treatment as §3 above; findings the reviser declined because they'd
  require editing the frozen diagnosis are marked distinctly, since only you
  at this gate can settle those), the fix plan, and the failing-test contract
  with its predicted failure signature verbatim. Approving starts UNATTENDED
  red-test -> fix -> deslop -> negative-control -> review. Your job at the
  pause: summarize, call out every `cannot_determine` link verdict, every
  degraded experiment, and every CRITIC-declined finding by name, check the
  fix site against the chain, hand back per §3. Never approve it yourself.
- Gate 2 is the **in-app smoke matrix**: after the exit gate the lane boots
  the REAL stack (api on 4124 from the fix worktree — or the main checkout
  when the fix is web — web on 3124, e2e DB with the search-eval fixture),
  runs the matrix's auto rows as generated Playwright, and pauses with
  `smoke-matrix.html`: auto rows pre-filled, judgment rows as a checklist the
  human walks in the live app (login `search-eval@goodword.internal`, any
  6-digit code). Approving tears the stack down and ships the draft PR. The
  stack deliberately survives the pause via PID files; `smoke-teardown` kills
  it after approval. This closes the old `SMOKE=SKIP repo=web-app` gap: the
  matrix runs for web-repo bugs too.
- **The home repo is a finding of the RCA.** `params.json`'s worktree is
  provisional until `bind-repo` rewrites it after approval. `CROSS_REPO_BUG` is
  a typed v1 stop with the RCA preserved: the human splits the report per repo.
- **The repro test is frozen after RED — through the deslop group too.**
  `GREEN_GATE=FAIL repro test modified`, `REVIEW_SCOPE=FAIL repro test modified
  in review`, and (VERSION 2026.08.28-2, design-only) `DESLOP=FAIL repro test
  modified` — checked in `deslop-recheck` and again before AND after
  `deslop-commit` stages anything — are all hard stops, same class as
  SCOPE_BREACH: restoring the test (`git checkout <red-sha> -- <test_file>`)
  is a human act.
- **The deslop group sits between the fix loop and negative control**
  (RUNBOOK §3b/§12; VERSION 2026.08.28-2, design-only). Same stops as §4
  above (`DESLOP_GATE=FAIL <guard>`, `DESLOP=DIRTY`, `DESLOP_ROUND_CAP`,
  the three `DESLOP_REVIEW=FAIL` schema variants, and never-plain-resume on
  `reviewer modified tree`), plus one bugfix-only reviewer value: **`beyond_five_guards`** — this
  lane is causal-minimal, so the writer may fix only the five guards and must
  report anything else under `reported_not_fixed`; the reviewer diffs the
  writer's actual edits against that declaration and files
  `beyond_five_guards` for anything undeclared. It blocks like any other
  finding — hand-fix (usually: revert the out-of-scope edit), then resume.
  `DESLOP_GATE=FAIL repro harness error rc=97` is a harness bug, not a
  regression — fix the environment, not the diff.
- **Escalate, do not resume**, on: `CHAIN_CONFLICT` (blind verifier contradicts
  the RCA — never "fix" the verifier), `EXPERIMENT_CONFLICT` (the LIVE RUN
  contradicts the RCA — same rule: never "fix" the experiment),
  `EXPERIMENT_AMBIGUOUS` (the observation matched zero or several predicted
  outcomes; a human reads `experiment-results.txt`), `RED_GATE=FAIL test
  passed`, `FIX_STALLED`, `ARCHITECTURE_SUSPECT attempts=3`, `EVAL_DIVERGED`
  (a search-touching fix shifted the offline eval lanes; the human decides
  re-record with `--subset` + additive pin merge vs revisiting the fix),
  `RCA_PLAN_REJECTED` / `RCA_PLAN_SCOPE_DISPUTE` / `RCA_PLAN_NO_PROGRESS` /
  `RCA_PLAN=FAIL immutable artifact modified` (the RCA planning-critic loop's
  designed human stops — same handling as `PLAN_REJECTED`/`PLAN_NO_PROGRESS`/
  `PLAN_SCOPE_DISPUTE` in §5; `RCA_PLAN_NO_PROGRESS` carries
  `other_mutated=<list|none>`, which says whether the revision landed on the
  contract files instead of `fix-plan.json`),
  `RCA_CONVERGE=FAIL missing durable anchor imm-<file>` (the pre-loop copy
  `rca-gate` took of one of the six immutable diagnosis artifacts is gone, so
  nothing can check the diagnosis was not rewritten — **never restore it from
  `rca-round-<N>/imm-<file>`**, that per-round copy is re-taken every round and
  may already hold a mutation; re-derive from the run's `rca-gate` outputs or
  abandon and re-run the RCA),
  `GREEN_GATE=FAIL VERIFY_CONTRACT attempt=N` (`verify.json` unreadable or its
  `test_patterns` empty — a contract error no fix attempt can repair; a human
  fixes the file, then resume), and
  any `NEGCONTROL=FAIL` (the fix was proven non-causal or the failure mode
  changed under revert; the failure line names the recovery command).
- **Resume is right** for the same transient class as §5, plus
  `RED_GATE=FAIL error-not-failure` once, and `SMOKE_STACK=FAIL` once (boot
  flakes; recurring means the stack recipe broke — engineer).
  `RCA_PLAN_ROUND_CAP` is resumable the same way `PLAN_ROUND_CAP` is: raise the
  cap (`echo N > <artifacts>/rca-round-cap.txt`) or accept the loop's last state
  by hand first, since a bare resume re-enters the loop and hits the same cap.
- `EXPERIMENT=DEGRADED` and `EXPERIMENT_GATE=PASS degraded` are typed and
  non-fatal: the run continues and the packet shows the dispute unsettled —
  call it out at gate 1 like a `cannot_determine`.
- **`RED_GATE=FAIL predicted signature not found`** forks: chain wrong ->
  engineer; signature merely over-specific -> a human edits `failing-test.json`
  (the edit is the approval), then resume. You may propose the edit, never make it.
- Ports 4124/3124 belong to this lane; 4123/3123 stay with the SDLC lanes.
  `SMOKE=SKIP repo=web-app` is typed and expected for the BOOT smoke (the
  in-app matrix still runs for web bugs). An integration-kind repro, the exit
  gate's eval lanes, and the smoke-matrix stack all hold the machine-global
  54322/8001 e2e mutex — never run two mutex holders at once.
- Cleanup: `smoke-teardown` kills the matrix stack by PID and removes the
  disposable web worktree after gate-2 approval. For an abandoned run the
  sweep is manual (RUNBOOK §12): abandon the run, remove the worktree(s)
  (including `web-app/.worktrees/bugfix-smoke`), delete the `archon/<slug>`
  branch, sweep only 4124/3124 by PID.
- **After merge + staging deploy**, run the deployed smoke:
  `DISABLE_OMC=1 archon workflow run bugfix-smoke-deployed "<that run's artifacts dir>" </dev/null`
  — it re-runs the matrix's fixture-independent auto rows against
  staging-app.goodword.com, pauses once, and posts the summary as a PR
  comment (`DEPLOYED_SMOKE=...`).

## 10. The backfill lane

`backfill` is the prod-data execution graph: a MERGED instrument (CLI command or
SQL) plus a population claim in, a snapshotted + applied + reconciled backfill
out. RUNBOOK §13 has the spec contract and the verbatim discriminators.

```bash
cd "$ROOT"
aws login   # HARD prerequisite - census needs prod RO from the first minute
DISABLE_OMC=1 archon workflow run backfill "/abs/path/to/backfill-spec.md" </dev/null 2>&1 | tee /tmp/archon-backfill.log
```

Everything in §0-§1 applies (guardrails, `DISABLE_OMC=1`, `</dev/null`, tee,
quota). What decides supervision calls here:

- **Execution only.** The lane refuses an unmerged instrument
  (`INSTRUMENT=FAIL ... not an ancestor`). Building the CLI or migration is
  SDLC/bugfix work; do not try to smuggle it into a backfill spec.
- **One human gate, and it releases prod WRITES.** `backfill-review.html`
  shows the measured census vs the claim, the 15 raw rows verbatim, the
  dry-run reconciliation, the bounds with the binding candidate named, the
  byte-exact armed command with its sha256, the snapshot/restore statements,
  the premortem, and the kill-switch negative-control proof. Your job at the
  pause: summarize, check the ROWS section against the claim with your own
  eyes, verify the binding bound makes sense, remind the human to `aws login`
  right before approving (SSO ~15 min; write creds are fetched at runtime
  post-gate), hand back per §3. Never approve it yourself.
- **The measurement stops are designed human stops, not retry candidates**:
  `CLAIM_DIVERGED` (measured vs claimed >25% - re-derive the claim; if the
  measurement wins, a human edits `backfill-plan.json`, the edit is the
  approval), `EXTRAORDINARY_CLAIM` (>10% of a table - a human writes
  `extraordinary-ack.txt`; agents never write that file), `SAMPLE_SUSPECT`
  (the raw rows contradict the claim - the 785k-vs-26k failure class; a human
  reads `sample-rows.txt` and either fixes the spec or overrides the audit
  verdict by hand), `DRYRUN_DIVERGED` (instrument vs census >5% - engineer,
  never arm past it).
- **Escalate, do not resume**, on: `KILLSWITCH_NEGCONTROL=FAIL` (the refusal
  path is broken - an apply must never run on an unproven kill switch),
  `ARMED_DRIFT` (the command changed after approval), `BOUND_BREACH`,
  `APPLY_STALLED`, `APPLY_EXEC=FAIL`, and `RECONCILE_FAIL` (the failure
  message prints the restore command; restoring is a human decision).
- **Resume is right** for `WRITE_CREDS=FAIL sso expired` (run `aws login`
  first) and transient provider failures, as in §5.
- **Recovery is the snapshot.** `snapshot-tables.txt` names the
  `backfill_undo_*` tables; `restore-command.txt` is the exact restore SQL.
  Snapshot tables live 14 days and dropping them is a human act - so is
  removing the instrument worktree (`api/.worktrees/backfill-<slug>`).
- To stop a running apply by hand: `touch <artifacts>/KILL_SWITCH` - the
  executor checks it between chunks, terminates the instrument, and exits
  typed. Removing the file is equally a deliberate human act.
- KB duty as in §7: `kb-capture` writes one change-history file; run
  `kb:compound` afterwards.

## Setting the pipeline up

If archon, the CLI stack, or the CE plugin is not installed yet, that is a
different job - use the `archon-install` skill.
