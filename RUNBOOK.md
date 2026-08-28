# Archon SDLC Runbook

Operator guide for the Goodword two-lane Archon pipeline (`full-sdlc-api` → `full-sdlc-web` → `babysit` → `cleanup`). Every entry here is an **observed** failure or behavior from the M0–M3 build, not a hypothetical. Format: symptom → verbatim discriminator → action.

Platform note: macOS or desktop Linux. The workflows surface human packets through a browser opener (`xdg-open`, else `open`) and always print the `file://` path, so a host without an opener degrades to "read the path" rather than failing. §6 has the Linux-specific traps.

Driving this from Claude Code: two skills ship with the layer and are staged into `<root>/.claude/skills/` by the installer — `archon-install` (set up or repair the stack) and `archon-sdlc` (start, supervise, and escalate a run). They are decision procedures; this runbook stays the reference they cite.

---

## 1. Starting a run

From the Goodword root (the folder Archon is registered against):

```bash
cd /Users/eduardopicazo/Documents/Workspace/Goodword
DISABLE_OMC=1 archon workflow run full-sdlc-api "/Users/eduardopicazo/Documents/Workspace/Goodword/.omc/research/toy-feature-spec.md" </dev/null 2>&1 | tee /tmp/archon-run.log
```

The run message is the **absolute path to the feature spec** — required; an empty message fails preflight with `PARAMS=FAIL`. Branch and worktree derive from the spec filename: `my-feature.md` → branch `archon/my-feature`, worktree `api/.worktrees/my-feature` (durable in the run's `params.json`). The web lane is still toy-pinned — real tickets are api-lane-only until it is parameterized.

Rules that are load-bearing, not stylistic:

- **`DISABLE_OMC=1` on every archon invocation.** Node Claude sessions (and any shell you open inside a workflow worktree) otherwise write `.omc/state/` into the repo worktree, and repo lint fails on the contamination.
- **Never shell into a workflow worktree from an OMC-hooked terminal.** Same contamination, from your side instead of the node's.
- **Capture the full log to a file; never pipe the launch through `tail` or a filter.** Bash-node stdout/stderr tails inside failure events are unreliable and truncated — the file is the only complete record. Filter at read time.
- **`</dev/null` on every archon invocation.** Guarantees the CLI cannot block on stdin.
- **Fresh `aws login` first.** The api boots against AWS Secrets Manager and SSO credentials expire in ~15 minutes. Preflight fails with `PREFLIGHT=FAIL aws session expired - run: aws login`.
- **Spec paths are absolute.** Node sessions and operator sessions cwd'd inside a child repo resolve relative `.omc/` paths against the wrong directory.
- A run consumes **your personal Claude subscription quota** — see §7 before starting anything large.
- **`--detach` when you are not going to sit in front of it.** `archon workflow run <lane> <spec> --detach` returns immediately, prints the detached child's log path under `~/.archon/logs/`, and the run is tracked normally by `archon workflow runs`. Without it the launch blocks until the first gate — fine for a human watching a terminal, fatal for an agent tool call or an ssh session that can drop, because killing the launcher kills the run. Verified 2026-08-25 on `register-probe`.

The web lane runs after the api lane and presupposes the api worktree exists:

```bash
DISABLE_OMC=1 archon workflow run full-sdlc-web "web lane" </dev/null 2>&1 | tee /tmp/archon-web.log
```

Then `babysit` (drives PRs to merge-ready) and `cleanup` (teardown), same invocation shape.

## 2. Spec authoring: `## Premises to verify`

A spec section titled exactly `## Premises to verify` (numbered questions) is a machine-read contract, not prose. For every question there, the planner must write a cited answer to `premises.json` — each answer needs at least one evidence item whose `file` exists in the worktree and whose `quote` appears verbatim in it. `plan-snapshot` greps for the quote and fails the run on an uncited answer; a separate `premise-verify` node then re-derives each answer BLIND (questions only, no planner reasoning) and `premise-gate` hard-stops on any `conflict` before the human plan gate. This exists because the first real-ticket run answered a spec question ("does row status gate survivors' sync?") with a plausible argument instead of a code check, and ten review rounds inherited the wrong premise. Use the section for anything the plan's correctness depends on; leave it out when the spec asserts nothing checkable.

The planner also writes `files-allowlist.json` (every path the unit may touch — the scope gate's contract) and `reader-audit.json` (columns whose interpretation/presentation semantics the plan changes; `{"columns": []}` when none).

## 2a. The plan gate (human review packet)

After doc-review, the run pauses (`status: paused`) and `plan-render-gate` prints `RENDER_GATE=PASS packet=file://…/plan-review.html`. **The packet opens in your browser automatically when an opener is available** (`xdg-open`, else `open`); the `file://` path is printed either way, and the message says which happened — it never claims "opened in browser" unless the opener actually ran. Read it: GIST (plain-language summary), KB (what prior art the plan honors), MAP (files and units), PLAN (verbatim), REVIEW (doc-review's edits and unapplied findings, plus a "Premise check" block summarizing each blind premise-verify verdict — `cannot_determine` entries deserve your attention; `conflict` entries never reach this packet, they stop the run earlier), DECIDE (the commands below).

This is the pipeline's highest-leverage human moment — one wrong plan line becomes a thousand wrong code lines. Do not rubber-stamp.

```bash
archon workflow approve <run-id> </dev/null >/tmp/archon-approve.log 2>&1 &   # background it — see below
archon workflow reject <run-id> "reason" </dev/null >/tmp/archon-reject.log 2>&1 &   # revises at 2 gates, ends the run at the other 3 — see below
archon workflow abandon <run-id>            # ends the run, no reason recorded, no rework
```

- **A paused run is a healthy run.** `status: paused` is the designed end of the stage, not a failure and not a stall. The workflow did its job and is waiting on you; nothing degrades while it waits. Every lane's gate packet now says this in its DECIDE box, because the first question a new operator asks is "what broke?".
- **`approve` resumes the workflow INSIDE the approving CLI process** and blocks until the run ends or fails. Background it (as above) or expect your terminal to be occupied for the rest of the run. If the resume fails, the approval is still recorded — just `archon workflow resume <run-id>`.
- **`reject` means different things at different gates.** Archon reworks on rejection only when the approval node declares an `on_reject` block (`on_reject.prompt` required, `on_reject.max_attempts` optional, default 3); with no block the reject path falls through to cancel and the run ends with `cancelled: true`. Two gates declare one, three deliberately do not:

  | Gate | reject does | why |
  |---|---|---|
  | `full-sdlc-api` plan-gate | revises `plan.md` + contracts, re-renders the packet, pauses here again | the artifact is a document; feedback is cheap to apply |
  | `bugfix` rca-approval | revises `rca.md` / `fix-plan.json` (including swapping in a depth alternative), re-renders, pauses again | same |
  | `bugfix` smoke-approval | ends the run | a smoke matrix that fails needs a human in the app, not a rewrite |
  | `bugfix-smoke-deployed` deployed-approval | ends the run | same |
  | `backfill` apply-approval | ends the run | the upstream proofs (arm's kill-switch negative control, the render gate's `cmp` of `armed-command.txt` and its sha256) do NOT re-run on a revision, so reworking past this gate would hand the human an unverified packet releasing prod writes |

- **Reject blocks the terminal at the two revising gates**, exactly like approve: the CLI records the rejection and then resumes the run in the same process ("Resuming with on_reject prompt..."). Background it. At the three terminal gates it prints "Rejected and cancelled" and returns immediately.
- **The revision pass is the only node that runs before the gate pauses again.** Upstream nodes do not re-fire, so `premise-verify`'s blind re-derivation, `doc-review`, and every render gate are skipped on a revision. The `on_reject` prompt therefore has to re-render the packet itself and keep the marker sections intact, and it is told to say in the packet that the revised premises were not independently re-derived. `on_reject` takes no `model` or `maxBudgetUsd` — the revision node runs on the tier default with no per-node cost cap.
- `max_attempts: 3` buys **two** rework passes, not three: the counter is checked before the rework, so rejections 1 and 2 revise and rejection 3 cancels the run with `workflow_cancelled` / reason `max_attempts (3) exhausted`. Verified 2026-08-25 with a throwaway probe workflow at `max_attempts: 2`, where the second rejection printed "Rejected and cancelled (max attempts reached)" and left the run `cancelled`.
- For a toy or install-validation run — one whose only job was to reach this gate and prove the stack works — say the run already succeeded before ending it. `abandon` is the clean verb there; rejecting spends a revision pass rewriting something disposable.
- `approve` / `reject` / `abandon` are real verbs but **absent from `--help`**. They exist; use them. Tell anyone you hand a command to that they are missing from `--help`, or they will check, not find them, and assume the command is wrong.
- Find the run id with `archon workflow runs` or `archon workflow status`. It is also the basename of the run's artifacts dir, which is how the gate renderers put a ready-to-paste command in the packet; `archon workflow runs` prints the first 8 characters, the packet prints all 32, and both work.

## 3. Review-loop exits — the four discriminators

The per-repo review loop (review → commit fixes → fixer → converge) ends in exactly one of four ways. The discriminator strings appear verbatim in the converge output (`round-N/converge.txt` in the run's artifacts dir; stdout is teed there because archon persists bash stderr but not stdout).

| Exit | Verbatim signal | Meaning | Action |
|---|---|---|---|
| Converged | `CONVERGED round=N` | Verdict acceptable, HEAD unchanged, tree clean. | None — the run proceeds. |
| Round progressed | verdict acceptable but HEAD moved this round | Fixes landed; the next round re-reviews them. | None — expected. Steady state is ~2 rounds per repo. |
| No progress | `NO_PROGRESS` (converge exits 1) | Verdict `Not ready` AND HEAD unchanged — the fixer isn't moving the needle. | Engineer. Semantic problem, not budget-shaped. Do not resume blindly. |
| Fixer blocked | `FIXER_BLOCKED` (converge exits 1; also fired when `fixer-result.json` is missing) | The fixer reported a P0–P2 it cannot fix, or produced no result file. | Engineer. Read `round-N/fixer-result.json` `failed` partition for the finding. First check whether each `failed` entry is a genuine could-not-fix or a mis-partitioned scope decline (see the observed table below) — a decline belongs in `advisory` and can be reclassified by hand to unblock. |

Bound-related failures that look similar but are different:

| Symptom | Verbatim discriminator | Meaning |
|---|---|---|
| AI node dies on cost | `error_max_budget_usd` / `exceeded cost cap of $` | Quota guard fired on a plain AI node. |
| Loop body dies on cost | `dag.node_budget_cap_exceeded` | Per-body-node cap inside the review `loop_group`. |
| Node dies mid-work | `dag_node_failed` with `isTimeout:true` | Wall-clock timeout — a stall, not a cost event. Bash nodes default to **120s** unless the YAML sets `timeout`. |
| Loop exhausts | `loop_node.failed` + `exceeded max iterations` (bare loops) / `loop_group_node.body_node_failed` (a body node failed the group at that iteration) | Iteration bound hit. |
| Node never starts | `dag_node_pre_execution_failed` | Almost always: `worktree.baseBranch: main` missing from `.archon/config.yaml` while a node references `$BASE_BRANCH`. The installer ships that config — check it wasn't edited. |
| Quota window exhausted | `claude.rate_limit_event` with `status != "allowed"` (`rateLimitType: five_hour`) | Subscription window hit; the org rejects overage, so the run **hard-stops**. See §7. |

Observed in the M4 trial (2026-08-13), all resolved by plain `archon workflow resume`:

| Symptom | Verbatim discriminator | Meaning / action |
|---|---|---|
| AI node fails instantly, no output | `dag.node_empty_output` / "provider stream closed without yielding content" | Transient provider failure. Resume; the node re-fires. |
| Review gate fails though the review looked fine | envelope ends "waiting for background reviewers" / missing `Review complete` | The review session ended its turn before Stage-5 synthesis, or omitted the terminal string. The fail-closed gate refuses an unsynthesized review on purpose. Resume; the next round re-reviews. |
| Node fails with a session-limit message | `Claude API error (rate_limit): You've hit your session limit · resets <time>` | Your 5-hour subscription window is exhausted (§7). Wait for the stated reset, then resume — completed nodes stay cached. |

Observed in the first real-ticket run (ENG-3605, 2026-08-13):

| Symptom | Verbatim discriminator | Meaning / action |
|---|---|---|
| FIXER_BLOCKED on a justified skip | `FIXER_BLOCKED` with a `failed` entry whose reason is "pre-existing" / "out of scope" / "left for a follow-up" | The fixer correctly declined a finding on scope grounds but filed it in `failed` instead of `advisory`, tripping the fail-closed gate. The fixer contract now pins this (declines → `advisory` with "Waived:" prefix), so it should not recur. If it does: verify the reasoning really is a scope decline, move the entry from `failed` to `advisory` in `round-N/fixer-result.json` by hand, and resume. Never reclassify an entry whose reason is "could not complete the fix". |
| FIXER_BLOCKED wedged on transient incompletions | `FIXER_BLOCKED` where every `failed` reason is budget/tooling-shaped ("session budget was exhausted", "edit anchor did not match", "attempted but not landed") | The fixer node itself SUCCEEDED, so resume re-runs only converge — same result file, same block, forever. The contract now has an `incomplete` partition for exactly these: the checker passes it, converge refuses to CONVERGE while any exist, and the next round's review re-surfaces the items for a fresh-budget fixer. On a run recorded under the old contract: move the transient entries from `failed` to `incomplete` (create the key if absent) in `round-N/fixer-result.json`, leave genuine design declines in `advisory` as "Waived:", and resume. |

Hardening gates added after the ENG-3605 retrospective (2026-08-14) — each is a clean human stop, not a retry candidate:

| Symptom | Verbatim discriminator | Meaning / action |
|---|---|---|
| Run stops at premise-gate | `PREMISE_CONFLICT id=N` | The blind re-derivation contradicts the planner's answer to spec premise N (read `premises.json` vs `premise-verify.json` in artifacts). Same handling class as a plan-gate reject: fix the plan and/or spec so they match the code, then `archon workflow resume <run-id>`. Never "fix" the verifier. |
| Run stops at reader-audit-gate (or exit-gate belt) | `READER_AUDIT_FAIL` | A reader of a column whose semantics this plan changes was classified `affected` — the plan missed a consumer. Read `reader-audit-result.json`, extend the plan/diff to cover the reader (or re-classify with justification), resume. |
| Converge / gate-tests / exit-gate stops on a file | `SCOPE_BREACH round=N file=<path>` (round tag absent outside converge) | A change landed outside `files-allowlist.json`. Legitimate scope growth is a HUMAN act: edit `files-allowlist.json` in the run's artifacts to include the path (the edit is the approval), then resume. Otherwise revert the file in the worktree and resume. |
| Review loop stops at the round cap | `ROUND_CAP_REACHED round=N` | The durable round counter hit the cap (default 4, override via `round-cap.txt`) without converging. Read the final round's envelope and fixer result, then either raise the cap or accept residuals (recipes below), then resume. |

Accept-residuals recipe (ends the loop at the NEXT non-converged round at/past the cap, ships with residuals recorded):

```bash
echo "accepted by <name>, <one-line reason>" > <artifacts>/accept-residuals.txt
archon workflow resume <run-id>
```

The PR body then opens Known Residuals with that line verbatim plus the final round's advisory and incomplete entries. Acceptance waives the verdict and fixer checks only — the tree must still be clean, in scope, and passing unit gates. **Writing `accept-residuals.txt` is a human act; agents never write that file.**

Raise-cap recipe: `echo 6 > <artifacts>/round-cap.txt` then resume. The cap counts `round.txt` (durable across resumes), not loop iterations.

Waiver ledger: converge appends each round's `advisory` declines to `<artifacts>/waivers.md`. Reviewers must file findings matching a waived entry under "Previously waived" (not actionable) unless they state new evidence, and the fixer treats matches as advisory by default. This is what stops rounds re-litigating recorded scope decisions — if a run still loops on a waived finding, read `waivers.md` first; the ledger, not the newest envelope, is the memory.

Web-lane parity note: the premise, reader-audit, and scope nodes exist in `full-sdlc-web.yaml` for contract parity, but the web lane is still toy-pinned with no plan stage, so they short-circuit explicitly (`SCOPE_GUARD=SKIP`, `PREMISE_GATE=PASS (no premises declared)`) until the lane is parameterized.

**Three "green run that did nothing" modes** (all observed): an unparseable `when:` silently skips its node and the run still reports SUCCESS; a node whose prompt is refused by its own session still reports Completed; a degraded review can report completion without reviewing. The workflows gate every load-bearing node on a typed disk artifact for exactly this reason — if a run is green but a downstream step complains about a missing artifact, treat the run as failed and read the log, not the status.

## 4. Resume

```bash
archon workflow resume <run-id>
```

- Completed AI nodes **never re-run** (proven across 6+ resumes on one run); only failed bash gates re-execute. Resuming is cheap.
- **Resume does NOT restore AI session context** — every post-gate node re-reads its inputs from disk artifacts. That's by design; nothing for you to do.
- **A failed `loop_group` resumes with a FRESH iteration counter.** `max_iterations` bounds per-invocation work only. The durable round counter is `round.txt` in the run's artifacts — that's the number that means anything across resumes.
- Node outputs may not survive a resume — every consumer has a disk-artifact fallback (envelope files). If you're debugging, trust the files in `$ARTIFACTS_DIR`, not remembered node output.

## 5. Stalls, orphans, and locks

- **You killed the archon CLI (or it died): the run is orphaned as `running` and the worktree lock persists.** Recover with `archon workflow abandon <run-id>`. **There is no `cancel` verb.**
- **Worktree-in-use lock** on a new run: an earlier run still owns `api/.worktrees/archon-toy` (or web's). Abandon the stale run, then `cleanup`.
- **Port sweeps: only ever touch ports the workflow owns (4123 api, 3123 web).** An over-broad sweep during the build killed the operator's own api on 4000. `PREFLIGHT=FAIL port 4123 busy` means find that specific PID (`lsof -ti :4123`, or `ss -ltnp "sport = :4123"` where lsof is absent) and decide — it may be a live run.
- **`PREFLIGHT=FAIL no port-inspection tool`** means neither `lsof`, `ss`, nor `fuser` exists. That is a hard stop on purpose: without one, the busy-port check would pass having checked nothing and two runs would collide on 4123.
- Kill by PID from `lsof -t`, never `pkill -f <pattern>` — a pattern match is one typo from killing an unrelated process.

## 6. Environment traps

- **Vite binds IPv6 `::1` only.** Every web probe and UAT URL uses `localhost`, never `127.0.0.1`. A hand-check with `curl 127.0.0.1:3123` will "prove" the server is down when it isn't.
- **mise shell shims do not apply in bare execs.** Anything detached/scripted must pin `mise x node@20 --` (web) / `mise x node@22 --` (api) explicitly, or it runs ambient Node 25 and Vite crashes.
- **`pnpm install --frozen-lockfile` refuses on main's known lockfile drift.** The workflows install unfrozen and exclude `pnpm-lock.yaml` from every commit. If you hand-fix in a worktree, do the same.
- **A branch-DELETION push still fires husky pre-push** (full jest, multi-minute, historically flaky in hook git env). Delete pushes go `--no-verify`.
- **api boot prints an inspector-port 9229 collision warning** when your own api dev server runs (`start:api` hardcodes `--debug`). It is noise, not a boot failure.
- **`gh pr ready` re-triggers the AI-review bots**, so babysit always terminates with a freshly-pending CodeRabbit status. It resolves green minutes later. Merge-ready = CI green + ready flag; the post-flip bot re-run is expected residue.
- Nodes start at the (non-git) folder root with no git context — which is why every repo path in the workflows is absolute and rendered per machine at install time. Don't "fix" one to a relative path.

### 6a. Linux notes

The target is a **desktop** Linux machine — GUI, a real browser, `xdg-open` present. Headless containers are not a supported operator environment (the web lane drives `agent-browser` against a real Chromium).

- **Opener.** `xdg-open` is probed before `open` everywhere, because `/usr/bin/open` on some distros is util-linux's link to `openvt(1)` — a different program, not a missing one. Install `xdg-utils` if `install.sh` reports no opener. Every opener site prints the `file://` path regardless.
- **AVX2.** On x64 the compiled archon binary **requires AVX2**; the upstream `archon.diy/install` script detects the CPU and aborts without it. arm64 builds have no such requirement. Nothing in this layer can work around that — it is the CLI itself.
- **Port ownership.** `lsof` is preinstalled on macOS and frequently absent on Linux. Every port site tries `lsof -ti` → `ss -ltnp` → `fuser -n tcp`, and preflight/cleanup hard-fail when none of the three exists. Install `lsof`, `iproute2`, or `psmisc`.
- **`agent-browser` system libraries.** Chromium needs the usual distro font/GTK/NSS set. If UAT fails at browser launch rather than at assertion time, that is the cause — it is not a workflow bug.
- **`localhost` resolution.** Probes deliberately try `localhost` first (see the Vite `::1` note above) and `127.0.0.1` second. If the web smoke fails on both, it prints `HOST_RESOLUTION:` with `getent hosts localhost` so the cause is visible instead of a silent six-minute timeout.
- **Locale.** The Python helpers pin `encoding="utf-8"` on every file read and subprocess capture, so `LANG=C` does not break them on LLM output full of em-dashes. Don't remove those.
- **`python3` floor is 3.7** (`subprocess.run(capture_output=…)`), which rules out the system python on RHEL 7/8 and Ubuntu 18.04.

What Linux support has *not* been proven on yet: a real billed lane run, `agent-browser` against real Chromium, Vite/bun bind behaviour, and AWS SSO. Those land on the desktop-Linux pilot. The Docker smoke in `setup/linux-smoke/` proves the install path and the shell/Python portability only — read its "cannot prove" list before trusting it.

## 7. Quota model (read before big runs)

Runs bill the **Claude subscription via OAuth login**, not an API key. `total_cost_usd` in run telemetry is cost-equivalent accounting, not an invoice. Consequences:

- The real currency is your **5-hour window / weekly quota, shared with your own interactive Claude use**. The failure mode of a runaway run is *you locked out of your own Claude for hours*, not a bill.
- Window exhaustion **hard-stops the run** (`claude.rate_limit_event`, org overage rejected). No cap value protects against it — plan runs against your window.
- Calibration anchors: one full 10-persona review of a 7-line diff ≈ $7.65-equivalent / 10 min; the api lane's toy run ≈ $9.71-equivalent; the web lane ran 6 review rounds on the dry-run.
- **Billing guard: never set `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, or `ANTHROPIC_PROFILE`, and never configure `apiKeyHelper`.** Any of these silently outranks the subscription and flips billing to metered API (a Max subscriber in the wild hit $1,800 in two days this way). Preflight and the installer both assert this.
- `total_cost_usd` **resets per resume process** — a resumed run's reported cost is the last process only. Sum per-process costs or use the event log if you need a real number.

## 8. Babysit and merge

- `babysit` watches CI per repo (`gh pr checks --watch`), runs the mechanical thread lane, and flips draft → ready.
- `NO_OPEN_PR` — vacuous pass; there was nothing to babysit. Expected on re-runs after merge.
- `CI_RED` — hard exit; CI is genuinely failing. Engineer.
- `NO_CI_RUNS` (web lane) — zero check runs on the web PR. Known cause: web's `pr.yaml` is gated on base `main`/`prod`; a stacked PR gets no CI. A lone CodeRabbit pass is not a build.
- Thread lane auto-closes a review thread **only** with commit-after-comment proof on the cited path; human authors and AI-review bots always route to needs-human. Needs-human threads are yours.
- **The merge click is yours.** The pipeline's terminal state is merge-ready, deliberately.

## 9. KB duty (do not skip)

Each lane's `kb-capture` node writes **exactly one** new file to `goodword-kb/wiki/change-history/` (provably additive — the gate hard-fails on anything else). Its "Promotion candidates" section is raw intake. **After a run ships, a human runs `kb:compound`** in an interactive session to curate promotions into glossary/ADRs/patterns. Unattended runs never touch curated pages; if the KB is to compound, the promotion pass is on you.

## 10. Cleanup and rollback

```bash
DISABLE_OMC=1 archon workflow run cleanup "teardown" </dev/null 2>&1 | tee /tmp/archon-cleanup.log
```

- Kills stray 4123/3123 PIDs (by lsof-derived PID), removes worktrees, deletes local `archon/*` branches, deletes remote branches **only when no open PR uses them**.
- It **refuses** to touch a branch that isn't `archon/*` and refuses dirty trees — exiting 1 as `CLEANUP=PARTIAL`. That refusal is deliberate, not a bug: look at what it refused and resolve by hand.
- Rollback of a bad ship: close the PRs (operator action — never automated), then `cleanup` deletes the branches. Committed review fixes live on the feature branches, so nothing is lost until you delete those.

## 11. When to hand it to an engineer

Stop resuming and escalate when you see any of: `FIXER_BLOCKED` (semantic defect the fixer can't clear), `NO_PROGRESS` (review and fixer are deadlocked), `CI_RED`, a cross-repo divergence (one repo converged, the other exhausted — the run fails with the converged PR left in draft, by design), or the same node hitting its budget cap on consecutive resumes. These are the taxonomy's "needs a human who can read code" states; more iterations only burn quota.

## 12. The bugfix lane (`bugfix`)

Sibling of the SDLC lanes: bug report in, draft PR out, through a Red -> Green root-cause pipeline
(intake -> evidence -> RCA + blind chain-verify -> live experiment on disputes -> FIRST human gate
(RCA packet) -> bind repo -> failing test (RED) -> fix loop (GREEN) -> negative control -> review
loop -> second negative control -> exit gate (+ search-eval replay when the fix touches search
paths) -> in-app smoke matrix -> SECOND human gate (matrix) -> draft PR). Same conventions as §1:
`DISABLE_OMC=1`, `</dev/null`, tee to a file, absolute spec path.

```bash
cd /Users/eduardopicazo/Documents/Workspace/Goodword
DISABLE_OMC=1 archon workflow run bugfix "/abs/path/to/bug-report.md" </dev/null 2>&1 | tee /tmp/archon-bugfix.log
```

The run message is the **absolute path to a bug-report .md** — it may be thin (a Sentry link, a Linear id,
one paragraph); the intake node expands references. Branch and worktree derive from the filename as in §1,
but the worktree in `params.json` is PROVISIONAL: **the home repo (api or web-app) is a finding of the RCA**,
so `bind-repo` rewrites params and creates the worktree only AFTER the human approval gate.

Lane-specific facts:

- **Ports: this lane owns 4124 (api smoke) and 3124 (reserved). It never touches 4123/3123**, so a bugfix
  run and an SDLC run can coexist. Port sweeps follow §5 rules within that ownership.
- **`aws login` before starting** is a soft prerequisite here, not a hard one: expired SSO degrades the
  evidence stage (`EVIDENCE_AWS=DEGRADED sso expired`) instead of failing preflight. Same for
  `SENTRY_AUTH_TOKEN` and `LINEAR_API_KEY` (`PREFLIGHT_WARN ... unset`). A local-only repro runs with zero
  external evidence.
- **Gate 1 is the RCA packet** (`rca-review.html`): evidence chips, the cited 5-whys chain with the blind
  verifier's per-link verdicts, the live-experiment section (a `cannot_determine` dispute triggers a real
  run of the code — `experiment-design` writes hypotheses with mechanical signatures, `experiment-run`
  executes, `experiment-gate` requires exactly one predicted outcome to match), the residuals table
  (every reported symptom gets a disposition: fixed-by-this-chain / by-design / separate-bug; separate-bug
  entries carry repo + ticket stub and become split tickets in the PR body), the fix plan with depth
  alternatives, and the failing-test contract with its predicted failure signature verbatim. Approving
  starts UNATTENDED red -> fix -> negcontrol -> review. Treat `cannot_determine` verdicts and
  `EXPERIMENT=DEGRADED` as the chain's soft spots, exactly like premise checks in §2a.
- **Gate 2 is the in-app smoke matrix** (`smoke-matrix.html`): after the exit gate, `smoke-stack` boots
  the real stack — e2e compose (54322/8001) + search-eval fixture + api on 4124 (fix worktree for api
  bugs, main checkout for web bugs) + web on 3124 (fix worktree for web bugs, else a disposable
  `web-app/.worktrees/bugfix-smoke` off origin/main). `smoke-auto` runs the generated Playwright rows;
  the pause shows auto rows pre-filled and judgment rows as a checklist to walk in the live app
  (login `search-eval@goodword.internal`, any 6-digit code — whitelisted locally). The stack SURVIVES
  the pause by design (nohup + PID files under `<artifacts>/smoke-stack/`); `smoke-teardown` kills it
  after approval and removes the disposable worktree. When the repro test mocked component behavior and
  the experiment ran, `red-gate` also enforced `premise_evidence` in failing-test.json — mocks must cite
  observed output, never assumption.
- **After merge + staging deploy**: `DISABLE_OMC=1 archon workflow run bugfix-smoke-deployed "<artifacts dir>" </dev/null`
  re-runs the fixture-independent auto rows against `staging-app.goodword.com`, pauses once, records
  `DEPLOYED_SMOKE=...`, and posts the matrix summary as a PR comment.
- **`kind=integration` repro holds a machine-global mutex**: ports 54322/8001 and the pg16 compose. A
  concurrent integration run's `down -v` destroys this run's DB. The gates print
  `RED_NOTE/REPRO_NOTE=integration mutex` when it applies; do not start a second integration-flavored run.
- **Review round cap defaults to 2** in this lane (override via `round-cap.txt` as in §3); the fix loop
  caps at 3 attempts by circuit breaker.
- **Cleanup**: `smoke-teardown` handles the matrix stack (kills 4124/3124 by PID, removes
  `web-app/.worktrees/bugfix-smoke`) after gate-2 approval; the e2e compose (54322/8001) is left up on
  purpose. For an ABANDONED run the sweep is manual per §10 conventions: abandon the run,
  `git -C <repo> worktree remove <wt>` (including `web-app/.worktrees/bugfix-smoke` and
  `web-app/.worktrees/bugfix-smoke-deployed` if present), delete `archon/<slug>` branches, sweep only
  4124/3124 by PID (PID files live under `<artifacts>/smoke-stack/`).

Failure taxonomy (in addition to the shared discriminators of §3 — same resume-vs-escalate logic):

| Verbatim discriminator | Meaning | Action |
|---|---|---|
| `PREFLIGHT_WARN <source> ...` | Non-fatal; that evidence source degrades. | Optionally provision and restart; otherwise none. |
| `EVIDENCE_AWS=DEGRADED sso expired` | AWS evidence skipped; run continues. | `aws login` before the next run if you want DB/log evidence. |
| `RCA_GATE=FAIL chain link N uncited` / `signature too generic` | The RCA broke its own contract. | Resume re-runs the RCA; recurring = engineer. |
| `RCA_GATE=FAIL CROSS_REPO_BUG` (also `BIND=FAIL CROSS_REPO_BUG`) | RCA says the bug spans api and web-app. v1 hard stop; RCA artifacts preserved. | Split the report into per-repo bugs (the expensive analysis already exists) or escalate. |
| `CHAIN_CONFLICT [link=N]` | Blind re-derivation contradicts the RCA chain. Same class as `PREMISE_CONFLICT`. | Never "fix" the verifier. Read both artifacts, fix the RCA or the report, resume. |
| `EXPERIMENT_CONFLICT observed=<id> rca=<id>` | The LIVE RUN contradicts the RCA's mechanism. Same class as `CHAIN_CONFLICT`. | Never "fix" the experiment. Fix the RCA, then resume. |
| `EXPERIMENT_AMBIGUOUS matched=...` | The observation matched zero or several predicted outcomes. | Human reads `experiment-results.txt`; fix `experiment.json` or the RCA, resume. |
| `EXPERIMENT=DEGRADED reason=...` | Experiment could not run (env missing, command failed). Non-fatal, typed. | Run continues; gate 1 shows the dispute unsettled. Treat like `cannot_determine`. |
| `RED_GATE=FAIL premise_evidence missing or uncited` | The repro mocks behavior nobody observed live. | Resume re-runs the red node once; recurring = the RCA's premise is unobserved — engineer. |
| `EVAL_DIVERGED lane=...` | A search-touching fix shifted the offline eval lanes (cassette or baseline). | Human decision: legitimate ranking change -> re-record with `--subset` + additive pin merge inside the fix PR; otherwise revisit the fix. |
| `SMOKE_STACK=FAIL ...` | The matrix stack failed to boot (compose, migrations, seed, api, or web). | Resume once (boot flake); recurring = stack recipe broke — engineer. Sweep 4124/3124 by PID first. |
| `MATRIX_RENDER_GATE=FAIL ...` | The matrix page broke its contract. | Resume re-runs the renderer. |
| `RED_GATE=FAIL test passed - does not reproduce the bug` | The repro test passes on the buggy tree. | Engineer: the chain is wrong, or the bug needs an environment the test lacks. |
| `RED_GATE=FAIL error-not-failure` | Suite errored (missing import, setup) instead of the test failing. | Resume re-runs the red node; recurring = engineer. |
| `RED_GATE=FAIL predicted signature not found` | Test fails but not with the predicted output. | Chain wrong -> engineer. Signature merely over-specific -> a human edits `failing-test.json` (the edit is the approval), then resume. |
| `GREEN_GATE=FAIL repro test modified` / `REVIEW_SCOPE=FAIL repro test modified in review` | Something edited the frozen repro test. Hard stop. | Restore the test (`git checkout <red-sha> -- <test_file>`), investigate why, resume. |
| `FIX_STALLED` | Identical failure hash on consecutive attempts. | Engineer; more attempts only burn quota. |
| `ARCHITECTURE_SUSPECT attempts=3` | Circuit breaker: three failed fixes. | Engineer; the RCA is likely wrong or the fix needs design work. Re-open the RCA. |
| `NEGCONTROL=FAIL fix not causal` | With the fix reverted, the repro STILL passes — the GREEN was a flake or env change. | Engineer. The recorded recovery command in the message restores the tree. |
| `NEGCONTROL=FAIL refailed without predicted signature` | Reverting the fix re-fails, but differently. | Engineer; the failure mode changed under revert. |
| `SMOKE=SKIP repo=web-app` | Typed, expected: the BOOT smoke is api-only. The in-app matrix still runs for web bugs, so behavior is covered. Visible in the PR body. | None. |

KB duty applies as in §9: `kb-capture` writes one `wiki/change-history/<date>-archon-bugfix-<slug>.md` with a
"Why the tests missed it" section — run `kb:compound` after the PR ships.

## 13. The backfill lane (`backfill`)

Safe prod-data execution graph for backfills & data migrations — the work with the highest
cost-of-error. **Execution only**: the instrument (a CLI command or a SQL statement) must
already be MERGED; building it stays with the SDLC/bugfix lanes. The lane takes a spec that
names the instrument plus a population claim, then measures, samples, dry-runs, arms, and —
after the single human gate — snapshots, applies, and reconciles. It never invents commands;
it only arms and executes what the spec names, byte-for-byte.

```bash
cd /Users/eduardopicazo/Documents/Workspace/Goodword
aws login   # HARD prerequisite here, unlike the bugfix lane - census needs prod RO
DISABLE_OMC=1 archon workflow run backfill "/abs/path/to/backfill-spec.md" </dev/null 2>&1 | tee /tmp/archon-backfill.log
```

Node graph: preflight → intake → intake-gate → census → sample → sample-audit → sample-gate
→ dry-run → arm (kill-switch negative control) → packet → render-gate → **apply-approval
(the ONLY human gate)** → snapshot → apply → reconcile → run-report → kb-capture → report.

### 13a. The spec contract

The run message is the absolute path to a backfill spec .md. Intake normalizes it to
`backfill-plan.json` VERBATIM — a missing field becomes `null` and the gate stops with
`SPEC_INCOMPLETE field=...`. The spec must provide:

- `instrument` — `kind: cli|sql`. cli: `repo` (api only), `merged_sha` (full 40-char, must be
  an ancestor of origin/main), `source_files`, `apply_command` (**must contain `{MAX_ROWS}`**;
  may use `{CHUNK_SIZE}`/`{STATEMENT_TIMEOUT_MS}`), `dry_run_command`, optional
  `would_touch_regex` / `progress_regex` (one capture group each; progress captures
  **increments**, never cumulative totals — the monitor sums them), optional `env_map`
  (ENV_VAR → host|port|username|password|dbname; exported over the copied `.env`, so the DB
  target is always the fetched credentials). sql: `apply_sql` (single statement containing
  `{CHUNK_SIZE}`; **must drain its own population** — each chunk's touched rows leave the
  target set, or the chunk-cap stall detector fires) and `dry_run_sql` (single-value count).
- `claim` — `population` prose + exactly one of `expected_count` / `expected_range`.
- `census` — `count_sql`, `distribution_sql`, `largest_sql` (extremes, not sums).
- `sample` — `rows_sql` (LIMIT 15, raw rows) + `clusters_sql` (3 largest per-entity clusters).
- `bounds_proposal` — `max_rows`, `max_fraction`, optional `per_entity_cap` (recorded and
  shown at the gate; enforced by the instrument itself).
- `verification` — `reconcile_queries` (each single-value, with `expect: {op: eq|le|ge, value}`),
  `negative_control_query` (single-value count of rows OUTSIDE the target set), `post_signal`.
  **Author the negative control drift-immune**: prod keeps moving during the apply, so pin it
  (`updated_at < <census time>`, a scope no organic write touches) — reconcile asserts strict
  equality with the census-time baseline, and an organically-drifting control fails it.
- `undo.snapshot` — per table: `table`, `id_col`, `value_cols`, `where_sql` (the target-row
  predicate), `mode: update|insert-missing` (update restores prior values; insert-missing
  re-inserts rows a delete-style instrument removed).
- `premortem` — one sentence: "if this destroys data, the cause will have been ___".

All census/sample/verification SQL is gate-enforced read-only (SELECT/WITH, single statement).
Everything before the gate runs on `dev/database/ro-credentials`; **only snapshot and apply
fetch write credentials** (`dev/database/credentials`), at runtime, never earlier, never into
any artifact or env file.

### 13b. Absolute bounds

`.archon/setup/backfill-limits.json` holds the bounds that do NOT inherit from the spec:
`absolute_max_rows` (100k), `absolute_max_fraction` (0.10), the divergence tolerances
(claim 25%, dry-run 5%, snapshot 5%), chunk size, statement timeout, stall seconds. preflight
copies it into the run's artifacts; every gate reads the per-run copy. Widening for one run is
a HUMAN edit of that copy before census; a durable change edits the setup file. Final armed
bounds = min(spec proposal, census×1.1, absolute ceiling, fraction cap) — `bounds.json` records
every candidate and which one is binding. A #1896-scale apply (1.22M rows) requires explicitly
raising the ceiling; that is the design, not a limitation.

### 13c. The gate packet and the armed command

`backfill-review.html` shows: claim vs MEASURED census, the 15 raw rows verbatim, the dry-run
reconciliation, every bound candidate, the byte-exact armed command with its sha256, the
snapshot/restore statements, the premortem, and the kill-switch negative-control result
(engage → typed refusal → disengage, PROVEN before any real apply). **Run `aws login`
immediately before approving** — SSO expires in ~15 minutes and the post-gate nodes fetch
write credentials at runtime. The apply node re-hashes `armed-command.txt` against
`armed-command.sha256` AND requires the hash verbatim in the approved packet; any drift is a
hard `ARMED_DRIFT` stop.

### 13d. Failure taxonomy

| Verbatim discriminator | Meaning | Action |
|---|---|---|
| `SPEC_INCOMPLETE field=...` / `INTAKE_GATE=FAIL ...` | The spec is missing or malforms a contract field. | Fix the spec, restart (pre-census stops are cheap). |
| `INSTRUMENT=FAIL merged_sha ... not an ancestor` | The instrument is not merged. | Merge it first; this lane executes merged instruments only. |
| `CLAIM_DIVERGED measured=X claimed=Y` | Measurement contradicts the spec's population claim by >25%. | Re-derive the claim. If the measurement is right, a human edits `backfill-plan.json`'s claim (the edit is the approval), then resumes. |
| `EXTRAORDINARY_CLAIM fraction=Z` | The backfill touches >10% of a target table — usually the *definition* is wrong, not the data. | Human writes `<artifacts>/extraordinary-ack.txt` with a one-line reason, then resumes. Agents never write that file. |
| `SAMPLE_SUSPECT note=...` | The raw rows do not look like the claim (the 785k-vs-26k failure class). | Read `sample-rows.txt` yourself. Audit wrong → edit `sample-audit.json` verdict (human act), resume. Audit right → fix the spec. |
| `DRYRUN_DIVERGED would_touch=X census=Y` | The instrument disagrees with the census about the population (>5%). | Engineer: instrument predicate and census SQL diverge; never arm past this. |
| `KILLSWITCH_NEGCONTROL=FAIL` | The refusal path did not fire when engaged. | Engineer. Never proceed to a real apply on an unproven kill switch. |
| `WRITE_CREDS=FAIL sso expired` | SSO lapsed between approval and snapshot/apply. | `aws login`, then resume. |
| `SNAPSHOT_DIVERGED snapshot=X census=Y` | The undo set does not cover the measured population. | Fix `undo.where_sql` or re-census, resume. Orphan `backfill_undo_*` tables from the failed attempt are yours to drop. |
| `ARMED_DRIFT ...` | `armed-command.txt` changed after approval. | Hard stop. Investigate, re-arm, re-approve. |
| `KILL_SWITCH=ENGAGED ...` | The switch file exists (a human engaged it, or a breach did). | Removing `<artifacts>/KILL_SWITCH` is a deliberate human act. |
| `BOUND_BREACH touched=N max_rows=M` | Cumulative touched exceeded the armed cap mid-run; instrument terminated, switch engaged. | Engineer + recovery: `restore-command.txt`. |
| `APPLY_STALLED ...` | No progress for stall_seconds (cli) or chunks exhausted without draining (sql). | Engineer: the instrument is not converging. Recovery: `restore-command.txt`. |
| `APPLY_EXEC=FAIL ...` | The instrument errored mid-apply. | Read `apply.log`. Partial work is covered by the snapshot. |
| `RECONCILE_FAIL ...` | A reconcile expectation failed, the outside-set count moved, or dispositions do not sum to touched. | The failure message prints the restore command. Restore is a human decision — the apply is done; decide restore vs investigate. |

To ENGAGE the kill switch on a running apply by hand: `touch <artifacts>/KILL_SWITCH` — the
executor checks it between chunks and terminates the instrument, typed.

### 13e. Cleanup and the snapshot TTL

- **Snapshot tables** (`backfill_undo_<slug>_<n>_<ts>`, names in `snapshot-tables.txt`) are
  never deleted by the pipeline. Drop them after **14 quiet days** — a human act, with write
  credentials. Failed snapshot attempts can leave orphan `backfill_undo_*` tables; drop those too.
- **Instrument worktree** (`api/.worktrees/backfill-<slug>`, cli kind only) is a disposable
  detached checkout of origin/main: `git -C api worktree remove <path>` when the run is done.
- A backfill too large to snapshot is too large for one run — chunk it into multiple runs by spec.

KB duty applies as in §9: `kb-capture` writes one
`wiki/change-history/<date>-archon-backfill-<slug>.md` with a "Population claim vs measured"
section — run `kb:compound` after the run ships.

The lane is NOT in the gist manifest until it has a clean trial (same bar as the bugfix lane).
Trial candidate: the #1896 follow-up 44k indexer gap — real, bounded, already censused once.
