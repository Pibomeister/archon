---
name: archon-sdlc
description: Use when driving or supervising a Goodword Archon SDLC, repository-list feature, bugfix, or backfill run - starting full-sdlc-api on a feature spec, bugfix on a bug report, or backfill on a backfill spec, reading the plan-gate, RCA-gate, or backfill-packet, interpreting loop exits (CONVERGED, NO_PROGRESS, FIXER_BLOCKED, SCOPE_BREACH, ROUND_CAP_REACHED, CHAIN_CONFLICT, FIX_STALLED, ARCHITECTURE_SUSPECT, NEGCONTROL=FAIL, CLAIM_DIVERGED, SAMPLE_SUSPECT, BOUND_BREACH, RECONCILE_FAIL, PLAN_REJECTED, PLAN_NO_PROGRESS, PLAN_SCOPE_DISPUTE, PLAN_ROUND_CAP, RCA_PLAN_REJECTED, RCA_PLAN_SCOPE_DISPUTE, RCA_PLAN_SHAPE=FAIL, CRITIC_GATE=FAIL, IMPACT=UNAVAILABLE, IMPACT=SKIPPED, DESLOP=DIRTY, DESLOP_GATE=FAIL, DESLOP_REVIEW=FAIL, DESLOP_ROUND_CAP, ROUTE=FULL, LITE_FIXES_UNREVIEWED, PROOF_SELF_CONTRADICTED, RCA_PLAN_FINDING_RESTATED, RCA_PLAN_SCOPE_WIDENED, RCA_PLAN_CRITIQUE_ORPHANED, E2E_MUTEX=FAIL, CROSS_REPO_FINDING), choosing between a lite lane (full-sdlc-api-lite, bugfix-lite) and the full lane, deciding resume vs escalate, or running babysit/cleanup afterwards. Triggers on "archon run", "start the SDLC lane", "archon bugfix", "archon backfill", "the run is stuck", "resume the run", or any mention of a paused/failed archon workflow.
---

<WORKFLOW-NODE-STOP>
If you are an Archon workflow node session (your prompt came from a `full-sdlc-*`,
`bugfix`, `bugfix-lite`, `backfill`, `babysit`, or `cleanup` node), ignore this skill entirely. It is written for the
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
- **Do not gate code-workflow launch or control on AWS.** AWS CLI/session is an
  optional evidence capability; evidence and probes degrade explicitly when it
  is absent or expired. Only prompt for `aws login` when a specific downstream
  operation genuinely requires AWS. Backfill production execution remains the
  guarded exception because its reads and writes require fresh credentials.
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

Feature runs now use the provider-neutral launcher:

```bash
python3 "$ROOT/.archon/setup/archon-run.py" feature --provider claude --scope api "/abs/path/to/spec.md"
python3 "$ROOT/.archon/setup/archon-run.py" feature --provider codex --scope api,goodword-mcp "/abs/path/to/spec.md"
```

Direct shell use must pass both `--provider` and `--scope`. `archon-linear`
infers them from ticket classification. Registered names come from
`bash "$ROOT/.archon/setup/repo-profile.sh" --list`: initially `api`,
`goodword-mcp`, and `web-app`. `web` aliases `web-app`. For Codex, standalone
`fullstack` is shorthand for the joint scope `api,web-app`. Claude's scalar
`fullstack` retains its legacy API-first/web-second `feature-api-handoff.json`
chain. Existing persisted API/web chains retain their original schemas.

List order is presentation order; the approved dependencies determine execution
order, with repository-name ordering for independent stages. Reject empty or
duplicate entries (including aliases), unknown/path-like names, and `fullstack`
inside a list. Do not use `ARCHON_REPO` to narrow an explicit list: conflicting
selection is an error. Scalar `--scope api` with `ARCHON_REPO` remains a deprecated
compatibility path. Use explicit repository scope for new work.

A comma-separated scope (`--scope api,goodword-mcp`, `--scope api,web-app`)
runs the joint repository-list chain for either provider: one planning run
writes `joint-plan.json`, then one stage run per repository in dependency order,
then one integration run. Codex advances through guarded `approve`/`resume`.
Claude has no control token; its operator loop after each gate is:

```bash
archon workflow approve <run>
python3 "$ROOT/.archon/setup/archon-run.py" feature-advance --chain <chain-id>
```

`feature-advance` prints `ARCHON_FEATURE_REPOSITORY_CHAIN=PAUSED … gate=…` with
the next run to hand back, or `DISPATCHED` / `LOCALLY_VERIFIED`. The approve
step stays with the human (§0); the agent only runs `feature-advance` and renders
the next packet.

For the guarded Codex path:

- Pin every selected repository's baseline and create separate chain-specific
  worktrees before planning. Preserve dirty main checkouts and existing
  worktrees. Use the repository profiles' installation and verification commands.
- Produce one complete `joint-plan.json`: selected repositories, owned stages,
  per-repository file allowances and tests, interface artifacts, dependencies,
  and a local integration matrix. A dependency inside the selected scope is
  owned work, not an unspecified upstream prerequisite. Outside-scope dependencies,
  cycles, and blocked plans cannot enter implementation; do not expand scope.
- Require one human joint approval bound to the exact spec bytes, repository set,
  baselines, plan/supporting artifacts, contracts, order, and verification plan.
  Planning workers write only run artifacts. After approval, each worker writes
  only its assigned worktree and run artifacts; routing and approved-plan files
  remain read-only. Drift invalidates authorization; return to joint planning.
- Execute stages sequentially. Consumers use the verified predecessors' exact
  local commits and hashed interface artifacts from `candidate-inputs.json`.
  Do not require an upstream PR, merge, deployed API, or package publication.
  Contract `artifact` is a literal producer-relative file. Generated files use
  an approved `export: {"argv": [...]}` hook whose candidate-owned script writes
  that file under `ARCHON_INTERFACE_OUTPUT_DIR` after verification, without
  changing the worktree. Merely listing an export under `verification` does not
  execute it. New integration commands use `{"repo": "api", "argv": [...]}`;
  cwd is that repository's disposable candidate worktree. Arguments may use
  `${ARCHON_REPO_API_WORKTREE}` and `${ARCHON_REPO_API_COMMIT}` (or
  `GOODWORD_MCP` / `WEB_APP` for other selected repositories). Scripts belong
  to the approved candidates. Validate with `setup/validate-joint-plan.py`.
  Trace response and error transformations through clients before promising
  consumer behavior; discarded error details cannot support distinct handling.
- Share **240 active minutes and 30 million tokens across the whole chain**,
  including planning, retries, all repository stages, and integration. Approval
  waits do not consume active time. Exact recorded Codex sessions and native
  subagent ancestry are deduplicated; unrelated concurrent features are excluded.
  The first rollout `session_meta.id` identifies its thread even when `session_id`
  is inherited. Provider SQLite spawn edges supplement rollout ancestry; owned
  child evidence must be present and consistent. New `token_usage_record`
  cumulative receipts supplement legacy counters, including final responses.
  An accounting correction can expose an existing overrun: preserve every count
  and keep the run stopped until a sufficient allowance is explicitly authorized.
  Resumes never replenish the budget. Unavailable accounting is a containment
  failure, and exhausted budgets prevent dispatch. Do not claim this Codex
  watchdog/accounting guarantee for the separate Claude path.
  Before launching a large repository-list Codex feature, run a forecast without
  launching AI:

  ```bash
  python3 "$ROOT/.archon/setup/archon-run.py" feature-estimate --scope api,goodword-mcp /absolute/path/to/spec.md
  python3 "$ROOT/.archon/setup/archon-run.py" --max-total-tokens 60000000 feature-estimate --scope api,goodword-mcp /absolute/path/to/spec.md
  ```

  For an existing Codex repository-list chain, check the allowance against
  refreshed private usage without launching AI. The shepherd additionally saves
  its forecast for chains created with this policy:

  ```bash
  python3 "$ROOT/.archon/setup/archon-run.py" feature-estimate --chain <chain-id>
  python3 "$ROOT/.archon/setup/archon-run.py" feature-shepherd --chain <chain-id>
  ```

  Forecasts are heuristic ranges, not statistical guarantees. They use private
  chain and budget ledgers when available; unfinished histories are lower bounds.
  Cold starts rely on explicit priors. The token unit is the enforced Codex
  total-token count: input, cached input counted once by provider accounting, and
  output. A forecast never increases, resets, or overrides the controller's
  enforced budget ceiling.
  New Codex repository-list chains warn before worktree creation if the allowance
  is below the forecast upper bound. They recheck at stage/resume boundaries and
  before each planning review round, saving read-only `budget-forecast.json`.
  `BUDGET_SHEPHERD=WARN` is advisory: continue under the unchanged hard cap.
  Actual token/time exhaustion and unavailable accounting still stop execution.
  Forecasts never accept findings or replenish tokens. Untouched older chains skip
  this advisory check. To apply an explicitly authorized higher total ceiling to
  a stopped Codex repository-list run, use:

  ```bash
  python3 "$ROOT/.archon/setup/archon-run.py" feature-budget-update <run-id> --token <operator-token> --total-tokens 100000000 --enable-shepherd --reason "Authorized ENG-3866 retry"
  ```

  The total includes prior usage; active time is not replenished. The controller
  authenticates under the chain lock, refuses live execution/competing controls,
  and journals the chain, ledger, and run-control amendment. Interrupted writes
  block dispatch: retry the identical command with the same operator token,
  ceiling, reason, and option. Never repair private allowance files manually.
  Amendment preserves control authority; normal resume still rotates the token.
  Optional `--total-active-minutes` raises the total active-time ceiling only with
  explicit authorization. Omission preserves that ceiling. Neither ceiling may
  decrease, and all consumed time/tokens remain counted. Include the identical
  optional flag when retrying an interrupted amendment; existing token-only
  amendment IDs remain compatible.
  `--enable-shepherd` enables older chains' advisory forecasts through controller
  supervision, including planning-round boundaries, without replacing their
  captured workflow or model. Verify preserved usage and all allowance records
  before guarded resume. A review-cap increase requires separate authorization
  and preserves the existing round counter/findings; it does not waive review.
  If a stopped repository implementation run needs exactly one existing tracked
  file added to the current stage allowlist, use guarded scope recovery instead
  of hand-editing approval artifacts:

  ```bash
  python3 "$ROOT/.archon/setup/archon-run.py" feature-scope-amend <run-id> --token <operator-token> --add-file <repo-relative-tracked-file> --reason "Authorized scope recovery"
  ```

  This command authenticates under the chain lock, refuses live processes,
  pending controls/dispatch, incomplete budget amendments, and any verified
  handoff/integration/publication. It is add-only for the current repository's
  safe owned non-symlink tracked file; it preserves contracts, repo order, tests,
  spec bytes, workflow source, worktrees, budget/accounting, approval history,
  and operator authority. It journals `scope_amendment`, writes a deterministic
  amendment packet under the original planning artifacts, refreshes only the
  current stage's bound plan/allowlist/candidate-revision digests/rendered plan,
  then signs the new approval snapshot. If interrupted, retry the identical
  command with the same token, file, and reason. Pending scope amendments block
  resume/approve and dispatch; abandon/reject containment remains available.
  Normal guarded resume still rotates the token.
  Guarded Codex controls deny unexpected Claude invocations before model startup
  (`ARCHON_CODEX_PROVIDER_GUARD=FAIL`). Stock rejection hooks cannot pin their
  provider through scoped hook fields; do not add ignored configuration keys.
  For historical provider usage, use guarded `feature-account-provider-usage`
  with the completed event ID and exact transcript while stopped (RUNBOOK).
  Verified private receipts count once toward the same cap, preserve Codex
  high-water marks, and do not duplicate recorded active time.
  Estimates group
  history by repository/phase, not semantic task difficulty, and are not calibrated
  statistical predictions. Fewer than three completed samples retain the explicit
  cold-start upper bound; incomplete histories never replace successful samples.
- Guarded `approve`, `resume`, and `supervise` advance the existing chain.
  Resume the failed stage without repeating verified predecessors. For a terminal,
  **unapproved planning** run that needs fresh planning, use
  `feature-replan <run-id> --token CONTROL_TOKEN_FROM_LAST_LAUNCH` after repairing
  the cause. It preserves the chain, selected worktrees, and shared budget;
  prior plan and critic/revision evidence are supplied as hashed, read-only
  `prior-planning-evidence.json` for refinement, without transferring approval.
  It cannot replace approved or implemented work. A `RECOVERABLE` start supplies
  usable control authority; retain its operator-held token. Never persist tokens
  in handoff documents or edit private state to bypass a failed check.
- Finish only when the approved integration matrix passes against disposable
  worktrees of all selected candidates and the chain receipt says
  **`locally_verified` with publication held**. A completed child run, disabled
  publication, missing artifacts, zero executed tests, failed integration, or
  skipped required verification is not verified delivery.

Repository-list qualification requires a supervised two-repository Sol/medium
Codex trial through the human gate, both stages, local integration, and shared
budget evidence. That receipt exists: chain `2205cded…` reached
`locally_verified` under the `claude` provider with receipt `7da448da…`,
publication held; `feature-publish` opened draft PRs api#2359 and
goodword-mcp#32. Qualification is established. After `LOCALLY_VERIFIED`, publication is the explicit
human-run `python3 "$ROOT/.archon/setup/archon-run.py" feature-publish --chain <id>`:
it pushes each candidate branch (`--no-verify`) and opens draft PRs against `main` in
dependency order, adopting an open PR only on an exact head match and recording
`NO_CHANGE` repositories without a PR. This feature does not authorize production
access or Linear writes, and the agent never marks a PR ready, merges, closes, or
deletes branches; the merge click is the human's.

Before starting anything large, read §6 below - a run spends the operator's Claude
subscription window.

### 1.0a. A start creates a supervision obligation

`ARCHON_BUGFIX=STARTED` or `CODEX_LITE_RUN=STARTED` means only that the exact
process group, control guard, and watchdog are active. It is not evidence that
intake, evidence gathering, RCA, or planning succeeded. The operator that starts
a run owns it until one of these observable stop conditions:

- the exact run is paused at a named human gate and its packet has been read;
- the exact run is failed/cancelled and the typed discriminator has been read;
- the exact run completed and its terminal artifacts were verified; or
- supervision was explicitly handed to another operator with run id, log paths,
  control token location, and current status.

Poll the exact run id and its workflow/watchdog logs. Do not end the operator
turn after printing `STARTED`, and do not wait for the human to ask “what
happened?” before surfacing a pause or failure. A proactive stop report names the
node, typed discriminator, evidence files, impact on ticket scope, and the next
safe action.

## 1a. Choosing the lane: lite or full

Two lite lanes exist: `full-sdlc-api-lite` (feature spec in) and `bugfix-lite`
(bug report in). They keep the human gate, one `ce-code-review` round, and the
negative controls, and drop the planning critic, doc review, blind premise and
chain verification, prod evidence, deslop, and the smoke matrix (RUNBOOK §14).
Whether a ticket may take one is not your judgment alone: every lite run passes
a fail-closed **routing envelope** whose thresholds live in ONE file. Read it,
never restate it:

```bash
cat "$ROOT/.archon/setup/lite-envelope.json"
```

Route by these questions, in order. Any "yes" means the full lane:

1. Does the change touch a `hot_paths` entry (migrations, entities, baseline
   schema, auth, oauth, billing, global-search, infra, the integration or
   bridge service, the web `api-client.d.ts`)?
2. Will it touch more than `max_files` non-test files, or more than one repo?
3. Is the mechanism unknown - would someone have to investigate before naming
   the single cause or the single change? (Lite bugs need a `## Repro` block
   with one test-runner command the reporter already ran and its observed
   output; a bug that needs prod evidence to reproduce is a full-lane bug.)
4. Does it change a contract another repo consumes, or authorization?
5. For a feature spec: does it carry `## Premises to verify`? Premises are the
   full lane's machine-read contract; the lite lane refuses them.

If every answer is "no" or "probably no", **start lite**. It refuses cheaply
and names why: `ROUTE=FULL reason=<check>` at `lite-envelope-pre` (bugfix, after
intake only), at `lite-envelope` (before the gate), or at `lite-envelope-post`
(after approval, against the plan as it stands then). On a refusal, relaunch
the SAME spec on the full lane. Never edit `lite-envelope.json`, `triage.json`,
`files-allowlist.json` or `fix-plan.json` to get past a refusal - the envelope
is the whole point. The one sanctioned override is the spec line `Lane: lite-ok`,
which lets a triage verdict of `M` (two mechanisms or one contract change) stay
lite; `L` is never overridable. That line is a human's decision to write.

```bash
cd "$ROOT"
DISABLE_OMC=1 archon workflow run full-sdlc-api-lite --branch <unique-slug> "$ROOT/.omc/research/<spec>.md" </dev/null 2>&1 | tee /tmp/archon-lite.log
DISABLE_OMC=1 archon workflow run bugfix-lite --branch <other-slug> "/abs/path/to/bug-report.md" </dev/null 2>&1 | tee /tmp/archon-bugfix-lite.log
```

**`--branch` is not optional.** Archon locks a run on its `working_path` and
nothing else. With `--branch` each run gets its own worktree and its own lock, so
runs are concurrent; without it every lane shares the project root and the second
launch self-cancels with `Workflow already active on this path`. Give each run a
branch nobody else is using — `archon-run.py` derives one from the spec slug
automatically, so only a raw `archon workflow run` needs you to pick it. See §5a.

Ports are **per run**, allocated at preflight from each lane's base
(4123 sdlc-api, 4124/3124 bugfix, 4125 api-lite, 4126/3126 bugfix-lite,
4127/3127 sdlc-web) at stride 10. Read this run's pair from its `PREFLIGHT_PORTS`
line or `params.json`; never assume a lane's base is what it bound. Everything in
§0 applies unchanged.

What the lite lanes give up, so you can say it plainly at the gate: on
`full-sdlc-api-lite` the single review round means fixes the fixer lands are
re-gated by typecheck, lint, the plan's tests and the scope check but are NOT
re-read by a reviewer; the PR body lists them under "Reviewer-unverified fixes"
and the run log prints `LITE_FIXES_UNREVIEWED`. On `bugfix-lite` there is no
prod evidence and no smoke of any kind; the proof is the repro test going RED
to GREEN plus two negative controls, and the PR body's "Proof" section carries
the signatures. Both packets print the envelope block the run was allowed on.

## 1b. Codex twins (`*-codex`)

Every lane has a GENERATED `provider: codex` twin (`bugfix-lite-codex`, `full-sdlc-api-codex`, ...) that runs the same DAG on the OpenAI Codex CLI, billed to the operator's **ChatGPT plan** instead of the Claude subscription. Reach for one when the Claude window is exhausted (or being saved for interactive work) and the ticket would otherwise wait, or when the operator explicitly asks for a codex run. Same gates, same typed lines, same lite-vs-full routing envelope.

Two differences that change supervision (RUNBOOK §15):

- Codex feature, lite, and full bugfix lanes have one supported launcher; raw Archon run/control commands are rejected by an always-run workflow guard:

  ```bash
  python3 "$ROOT/.archon/setup/archon-run.py" check
  python3 "$ROOT/.archon/setup/archon-run.py" bugfix --provider codex "/abs/path/to/report.md"
  python3 "$ROOT/.archon/setup/archon-run.py" run full-sdlc-api-lite-codex "/abs/path/to/spec.md"
  ```

  Use the same script's `approve`, `reject`, `resume`, and `abandon` subcommands at gates, passing `--token CONTROL_TOKEN_FROM_LAST_LAUNCH` and replacing the placeholder with the token printed by the latest `STARTED` or `RECOVERABLE` line. It validates ChatGPT auth and dedicated-home skills, checks optional GitNexus health for the pinned `api` index at `$HOME/.archon/gitnexus/api-main`, captures a stable exact launcher PGID/fingerprint, and returns only after the watchdog arms and the workflow consumes a one-time private guard. Lite defaults are 90 active minutes/8M cumulative tokens; scalar full feature and full bugfix defaults are 240/30M per lane. New Codex repository-list features share 240/30M across the entire chain, not per repository. When GitNexus is healthy, its index, token hash, signal authority, and enforced Codex wrapper all live outside the AI-writable repository roots; when it is absent/stale/missing MCP, the run emits explicit degraded evidence and continues with repo-local investigation. `abandon` and `reject` remain available even when auth, ports, or optional evidence sources are unhealthy.
  Repository-list Codex launches install a trusted `PreToolUse` native-spawn guard in the dedicated Codex home. The hook no-ops outside `ARCHON_FEATURE_SCOPE=repositories`, and the launcher derives repository-list pins from the trusted dedicated `CODEX_HOME/config.toml` when adapter argv omits them. Native child requests must explicitly match the chain model and effort. A denial prints the required values, for example `model=gpt-5.6-sol` and `reasoning_effort=medium`; correct the child request instead of bypassing hook trust. The dedicated Codex home must be private (`0700`; config/manifest not group/world writable) before launch.
- `maxBudgetUsd` is unsupported under codex, so generated twins remove the inert fields. AI-node timeouts remain non-lethal; the mandatory external watchdog is the structural brake.

Twins are generated by `setup/derive-codex.py`; never edit one by hand — fix the parent and regenerate.

Repository-list Codex nodes use fresh conversations through the external
wrapper, preserving model/effort and resuming work from artifacts. The adapter's
conversation-level `resume` must not let a critic inherit a reviser's private
history. Every fresh session remains in the same shared budget. A run-local
`AGENTS.md` can supply cited operator review evidence; workers receive read-only
access, and the evidence never grants approval or waives findings.

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

### Gate purpose, not gate worship

A hard gate is fail-closed by design, but its premise remains falsifiable. Before
changing code or artifacts merely to satisfy one, classify what it protects:

- authority/safety invariant (human approval, destructive action, immutable
  lineage, security boundary): never bypass it;
- evidence adequacy: gather, rework, or preserve an explicit open state rather
  than manufacturing certainty;
- harness/environment health: repair or degrade the harness, never change
  product semantics to appease it;
- obsolete premise: when current evidence disproves the protected assumption,
  update the gate and add a regression test before resuming the exact failed
  node.

Every hard failure must state the invariant, the observed contradiction, and a
safe unblock path. A gate that cannot do so is a workflow defect, not evidence
that the product change is wrong. Do not circumvent it; repair its contract.

Knowledge capture is run-local first. A workflow node writes and validates
`kb-capture.md` in its own artifacts directory; an external/team KB is an
optional operator promotion sink. Missing write authority to that sink never
turns a shipped, verified PR into a failed delivery.

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
- `reject` behaves differently per gate. `full-sdlc-api` plan-gate retains its
  revision pass. Bugfix RCA gates only record a rejection receipt: they never
  mutate an attested RCA or plan in place. Abandon the rejected bugfix run and
  use its guarded, scope-preserving successor so blind proof and criticism run
  against the new bytes. Reject resumes in the terminal it is typed in, so
  background it like approve.
- At the other three gates (`bugfix` smoke-approval, `bugfix-smoke-deployed`,
  `backfill` apply-approval) there is no handler and reject ends the run.
  Those gates are deliberately terminal: a smoke matrix that fails needs a
  human looking at the app, not a rewrite, and the backfill gate's upstream
  proofs (the arm node's kill-switch negative control, the render gate's
  byte-comparison of the armed command) do not re-run on a revision, so
  reworking past that gate would hand the human an unverified packet.
- `abandon` ends any run outright with no reason recorded and no rework.
- A feature-plan revision pass is the only node that runs before its gate
  pauses again. Bugfixes deliberately do not use that exception.
- For a toy or install-validation run whose only job was to reach this gate,
  say plainly that the run already succeeded before recommending how to end it.
  There, `abandon` is cleaner than `reject` — rejecting spends a revision pass
  rewriting something disposable.

None of the three appear in `archon --help`. Say so in the same breath, or the
human checks `--help`, does not find them, and concludes you invented them.

They run the command. You never do (§0).

## 4. Supervising the review loop

Between `implement` and `ship` sits a per-repo loop: review -> commit fixes ->
fixer -> converge. It ends in exactly one of five ways (RUNBOOK §3), and the
discriminator string is verbatim in `round-N/converge.txt` in the run's artifacts:

| Verbatim | Meaning | Action |
|---|---|---|
| `CONVERGED round=N` | Verdict acceptable, HEAD unchanged, tree clean | None - the run proceeds |
| verdict acceptable, HEAD moved | Fixes landed; next round re-reviews them | None - expected |
| `NO_PROGRESS` | `Not ready` AND HEAD unchanged - the fixer is not moving the needle | Escalate. Semantic, not budget-shaped |
| `FIXER_BLOCKED` | Fixer reported a P0-P2 it cannot fix, or wrote no result file | Escalate. Read the `failed` partition first |
| `CROSS_REPO_FINDING round=N count=N repos=<comma list>` | A fixer finding's defect lives in a different repository of this chain - not waivable, not fixable here | Escalate. Read `cross-repo-findings.json` |

Three rules that decide most supervision calls:

- **`round.txt` is the only number that means anything.** A failed `loop_group`
  resumes with a fresh iteration counter; `max_iterations` bounds per-invocation
  work only. The durable counter is `round.txt` in the artifacts dir (RUNBOOK §4).
- **Resume is cheap.** Completed AI nodes never re-run; only failed bash gates
  re-execute. Resume does not restore AI session context, and it does not need to -
  every post-gate node re-reads its inputs from disk.
- **A disproved cause is not automatically a resolved ticket.** For a report
  with multiple symptoms, evaluate each symptom after an RCA/chain dispute. If
  `CHAIN_CONFLICT` disproves the proposed cause for one symptom but another
  symptom remains evidenced and unexplained, keep the unresolved symptom in
  active ticket scope. The run must not approve the contradicted RCA, but it also
  must not be described as finished or abandoned merely because one premise was
  false. Do not edit the RCA or verifier output in place: completed AI nodes
  normally remain cached. Read `proof-recovery.json`; a
  `RECOVERY_SUCCESSOR_REQUIRED` stop is consumed by the watched neutral launcher,
  which creates exactly one same-provider full-lane successor with the original
  root symptom ledger, pinned baseline, and parent lineage. If automatic
  supervision is no longer attached, use the guarded continuation-seed control;
  never reconstruct a narrower report by hand.
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
  hand back. A cap round whose blocking findings were all applied (none declined, no P0)
  converges to the plan gate with `plan-cap-unverified.json`; call those
  un-recritiqued edits out by name at the gate like declined findings.
  `PLAN_ROUND_CAP` alone is resumable after raising the cap or
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

## 5a. Concurrency: N runs at once, one worktree each

Runs are concurrent as of 2026-09-07. Three ran simultaneously with no measurable
slowdown (RCA wall-clock 11.0/14.1/14.6 min, against 10.3-17.3 min across eleven
serial runs). Full contract in RUNBOOK §5a.

**The rule: every run needs its own `working_path`, and `--branch` is how it gets
one.** The lock keys on `working_path` and nothing else. `--branch <b>` puts the run
in `~/.archon/workspaces/_local/Goodword/worktrees/archon/task-<b>`; drop it and you
are back to one shared path and an instant self-cancel. `archon workflow runs` showing
distinct `working_path`s is the proof that runs are really in flight.

`archon-run.py` passes `--branch` itself, derived from the spec slug, so the launchers
in §2 need nothing extra. Only a raw `archon workflow run` makes you choose, and the
branch must be unique per run — two launches sharing a branch share a lock.

**Why this works here:** the Goodword root is a *git shell* (`.gitignore` = `*`, one
empty commit, a bare origin under `~/.archon/shells/`). Nothing is ever tracked in it.
Node bodies address every repo by absolute path, so the archon worktree is only a lock
key and an artifacts anchor — never the tree the work happens in. The api/web-app
worktrees are still cut under `<repo>/.worktrees/` as before.

**What is isolated, and what is not:**

| Resource | Isolation |
|---|---|
| Path lock, artifacts dir | per run |
| Smoke ports | per run (`setup/port-alloc.sh`, recorded in `params.json`) |
| api/web worktrees, incl. `bugfix-smoke-<slug>` | per run |
| **e2e docker stack (54322/8001)** | **shared, serialized by `setup/e2e-mutex.sh`** |
| **ce-code-review `/tmp` root** | **shared**; only the `head_sha` prefix match separates lanes |
| **goodword-kb** | shared; the gates are scoped to each run's own file |

**The e2e stack is the one thing that still serializes.** `up -d --wait` attaches to a
running stack instead of erroring, so a second run's migrations and seed would rewrite
the first's rows silently. `E2E_MUTEX=FAIL` is a typed stop naming the owning run's
artifacts dir — it is not a queue, because the smoke stack stays up across a human gate
that can take hours. `smoke-teardown` (`always_run`) releases it; a run that dies before
teardown strands it and the message prints the `rm -rf` that clears it.

*Known ceiling:* an **integration-kind repro** reaches the same database through
`.env.e2e` at red-gate, green-check, exit-gate and negcontrol WITHOUT taking the mutex.
Never run one alongside anything that boots the e2e stack.

**Every bash gate tees its typed line to `$ARTIFACTS_DIR/node-<id>.out`.** That is
where a failed gate's reason lives: `archon.db` stores the node's SCRIPT, not its
stdout, so `archon workflow get` echoes source and truncates. With detached
concurrent runs there is no terminal either. Read the file; if it predates this
change, replay the gate's helper against a COPY of the artifacts dir, never the
live one (those helpers write files the gate reads).

**Operating rules.** Read this run's ports from its own `PREFLIGHT_PORTS` line or
`params.json`, and its live URLs from `$ARTIFACTS_DIR/smoke-urls.txt` — a lane base is
no longer where anything binds. Resume only through `setup/resume.sh` (§5): a relaunch
of the same ticket reuses its worktree, so a path can hold several runs and the raw CLI
takes the newest. `lock-probe` still costs nothing if you want to prove a path is free.

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
- **KB duty (RUNBOOK §9).** Each lane's `kb-capture` writes exactly one required
  run-local `kb-capture.md` inside its artifacts directory. External KB promotion
  is optional operator work and missing sink access cannot fail an otherwise
  shipped, verified PR. After a run ships, say `kb:compound` in an interactive
  session when the human wants to curate promotion candidates into
  glossary/ADR/pattern pages. Unattended runs never touch external curated pages.

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
For a small, locally reproducible bug, §1a's `bugfix-lite` is the cheaper sibling
(RUNBOOK §14); it refuses anything outside the envelope and you relaunch here.

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
  The RED node must therefore implement every regression case promised by the
  approved plan, run the repository formatter before its final failing output,
  and satisfy blocking test-content rules (including collision-safe fixtures)
  before `red-sha.txt` is written. Review must classify any later repro-file
  finding as manual and must never auto-edit or commit that file.
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
  (a search-touching fix shifted the offline eval lanes; never update baseline
  truth merely to turn the gate green. First distinguish a missing reranker
  fixture from a real behavior regression. For candidate-shape-only drift use
  `run.ts --replay <baseline-cassette> --record-reranker --subset <ids>` so
  planner/embed inputs stay frozen, require every existing fixture byte to be
  unchanged and only new keys added, then replay the full corpus. Any remaining
  regression means revisit the fix, not lower/re-record the passing baseline),
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
  `GREEN_GATE=FAIL VERIFY_CONTRACT attempt=N` (`verify.json` unreadable, its
  `test_patterns` empty, a pattern that is not a unit spec, or a pattern matching no tests — a contract error no fix attempt can repair; a human
  fixes the file, then resume), and
  any `NEGCONTROL=FAIL` (the fix was proven non-causal or the failure mode
  changed under revert; the failure line names the recovery command).
- **Resume is right** for the same transient class as §5, plus
  `RED_GATE=FAIL error-not-failure` once, and `SMOKE_STACK=FAIL` once (boot
  flakes; recurring means the stack recipe broke — engineer).
  `RCA_PLAN_ROUND_CAP` is resumable the same way `PLAN_ROUND_CAP` is: raise the
  cap (`echo N > <artifacts>/rca-round-cap.txt`) or accept the loop's last state
  by hand first, since a bare resume re-enters the loop and hits the same cap.
- **A failed bash GATE whose cause lives in an artifact an AI node wrote is NOT
  fixed by resuming.** Only the gate re-executes — completed AI nodes never
  re-run (§5) — so it re-reads the same bytes and returns the same verdict. Edit
  the named artifact first, then `resume.sh`. `PROOF_SELF_CONTRADICTED` is the
  clearest case: `proof-assessment.json` claims `occurrence_attributed: true`
  while the cited source's provenance row is `evidence_kind: class`. Either drop
  that source from `occurrence_evidence_sources` (if another cited source carries
  the attribution) or set `occurrence_attributed: false` and accept
  `class-hardening-only`. RUNBOOK §12 used to say "resume re-runs the RCA" here;
  it does not.
- **`RCA_PLAN_FINDING_RESTATED` and `RCA_PLAN_SCOPE_WIDENED` are diagnostics, not
  stops.** They print inside `rca-converge` and change no verdict. RESTATED means
  the critic re-raised something the reviser DECLINED last round — the reviser may
  not edit that artifact, so another round cannot close it; read the previous
  round's `revision.json` justification and either settle it or accept at the cap
  with `rca-plan-accept.txt` (which never waives a P0). SCOPE_WIDENED means a
  revision pulled a HIGH/CRITICAL symbol into the blast radius that round 1 did not
  carry. Seeing both on the same run means the loop is escalating, not converging:
  on run 08389c0f it went 5 -> 40 -> 29+26 direct dependents across three rounds and
  then died on the critic's cost cap. **Raising that cap buys another wider round —
  read these two lines before touching `maxBudgetUsd`.**
- **`RCA_PLAN_CRITIQUE_ORPHANED round=N`** prints at the start of a RESUMED round:
  round N's critic finished and wrote `rca-round-N/critique.json`, then the round
  died before converge. A `maxBudgetUsd` cap does exactly this — archon throws from
  inside the node and that throw cannot be intercepted from the workflow, so the
  node is `failed` even though its contract artifact is complete and valid. Read
  that critique before paying for the next one; the resumed round WILL commission a
  fresh critic, deliberately, because a resumed round must not be judged by a
  critique written against the pre-edit plan.
- **`E2E_MUTEX=FAIL`** means another run owns the shared e2e stack (§5a), not that
  anything is broken. The message names the owner's artifacts dir. Let that run reach
  its smoke gate and approve it, or release by hand once it is gone.
- **Resume is not arbitrary rewind.** Archon resumes failed loop/node work while
  skipping completed nodes; it cannot safely jump behind a frozen RED or an
  approved manifest. Never delete workflow-event rows or rewrite hashes to fake
  a rewind. If a corrected artifact is consumed by the failed node, fix that
  exact human-editable contract and resume. If the correction belongs to an
  earlier completed/frozen node, use a fresh guarded run or an explicitly
  re-proven RED recovery that preserves the original evidence and audit trail.
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
- KB duty as in §7: `kb-capture` writes run-local `kb-capture.md`; run
  `kb:compound` afterwards only when promoting into an external KB.

## Setting the pipeline up

If archon, the CLI stack, or the CE plugin is not installed yet, that is a
different job - use the `archon-install` skill.

## Bugfix v2 handoff contract

For new bugfix runs, read `fix-classification.json` and `bugfix-chain.json` before describing any gate or failure. Report both implementation result and ticket disposition, list open effective/source symptoms, and never call class hardening a ticket fix. `FULL_FIX` is valid only when closure is true.

`CHAIN_CONFLICT`/`EXPERIMENT_CONFLICT` now flow through `proof-recovery.json`. `RECOVERY_SUCCESSOR_REQUIRED` is a typed, scope-preserving continuation request owned by `archon-run.py`; do not edit verifier output or reconstruct a narrower report. `EVIDENCE_BLOCKED` is an honest investigation result. Default neutral launches supervise to the first gate/terminal state; handoffs persist command templates with token placeholders, never the token.

Human approval covers the exact rendered manifests and controller attestations.
Never edit RCA or plan artifacts at a gate; rejection records feedback and
requires a guarded successor so verification and criticism rerun. Each causal
fix attempt gets one pristine-baseline run. The launcher seals the failed patch,
continues the first two failures as linked investigation successors, and stops
the third with `ARCHITECTURE_SUSPECT`. Attempt four requires the explicit
`bugfix-architecture-approve` receipt command emitted by the controller.

Before recommending approval, read `repo-policy.json` and
`test-placement.json`. A blocking repository rule is a gate, not a style
preference. Call out any new scenario-specific test when pinned repository
policy requires extending an existing spec. Preflight, RCA, RED, review, and
exit enforce this contract within their existing nodes.
The policy helper currently checks existing-spec reuse, test naming/location,
real timer waits, barrel imports, raw-SQL setup, and collision-safe profile
fixtures only when the target repo's pinned guidance marks each rule blocking.
`verify.json.test_patterns` is unit-runner-only; integration/AI/eval coverage
belongs to the existing exit gate, which invokes explicit spec paths rather
than the obsolete `TEST_GROUP=evals` selector.

Read `change-context.json` and `change-context-assessment.json` when supervising
a bugfix. Every recent related PR must be classified as solves, partial,
unrelated, or superseded using current pinned code. For Codex runs,
`GITNEXUS=GATHERED transport=cli-fallback` is valid protected graph evidence;
`GITNEXUS=UNAVAILABLE` means both MCP and the CLI fallback failed and must be
surfaced as an evidence gap.
The CLI fallback is deliberately bounded to 16 query terms, three symbol
contexts, and 120 seconds per command, with progress lines. Before a new launch
the protected index targets current `origin/main`; before resuming a pinned run
it targets `bugfix-chain.json`'s stored GitNexus commit. Treat
`expected-origin/main` and `expected-stored-run-baseline` as different repair
instructions, not contradictory freshness checks.

Read `boundary-trace.json.surface_equivalence` before recommending the RCA
gate. It must name the reported surface, actual runtime entrypoint/owner, RED
entrypoint/runtime owner, and smoke entrypoint/runtime owner with separate typed
file/quote evidence for each ownership link; all three runtime-owner identifiers
must match exactly and both equivalence booleans must be true. Shared types,
similarly named services, and passing tests
on an adjacent path are not runtime ownership. If the fix tests SearchV4 while
the reported Chat/People Search action dispatches elsewhere, reject before
implementation instead of presenting internal class hardening as a surface fix.
An ambiguous user-facing surface is not permission to select the path with the
best fixtures. It blocks implementation until report evidence or a captured
runtime reproduction identifies the entrypoint. An ambiguous RCA with an empty
fix plan is valid open investigation state; do not “repair” it by making
candidate owner strings match.
Then read `reproduction_equivalence`. Same runtime owner is not enough when the
RED changes a triggering threshold, fixture cardinality, feature flag, caller
context, planner behavior, cache/fallback state, permission, or tenant scope.
Any `changes-causal-boundary` difference blocks implementation; require a
surface-equivalent proof rather than padding or stubbing around the failure.
When the observation exists only in smoke/test infrastructure, compare that
environment with the repository's accepted integration/eval profile. Missing
seeds, overrides, flags, clocks, or provider stubs are harness drift; do not
promote them into product requirements or production-default changes.

At the smoke gate, read every auto row's `failure_class` and `observed` evidence.
`product` means reject/reopen. `harness` means a route/selector drifted and the
current reported surface must be checked manually; it never excuses visibly
wrong output. `infrastructure` or `unknown` remains unverified. Screenshots and
visible content outrank a stale testid: selector drift and product failure can
coexist.

Do not recommend the second approval gate unless the deterministic readiness
contract has passed. A full bugfix run must have `ticket_disposition=RESOLVED`
and `ticket_closure_allowed=true`, unless a separate explicit residual
acceptance artifact is present. Any auto smoke row with
`failure_class=product` is a hard stop, not a judgment-row item. If those
blockers appear, surface the exact open symptom IDs or row IDs and continue
from a corrected/successor spec instead of shipping a PR that leaves the
reported symptom unresolved.
