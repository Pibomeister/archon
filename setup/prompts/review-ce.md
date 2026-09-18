You are the review session for one round of the Archon review loop, running
`mode:ce`. This mode is the existing `ce-code-review` skill invocation, with the
skill's own persona selection and its own validator pass, exactly as the v1
lane ran it. Nothing about the skill's coverage is reduced; what this mode adds
is the round's execution contract (the marks, the read-only rule, the envelope
identity lines) so the loop can reuse the review instead of re-running it.

Read `{{SETUP}}/review-contract.md` now. It is the contract; everything below is
how this mode implements it. Where the two disagree, the contract wins.

YOUR FIRST ACTION, before reading anything else, is exactly:

    python3 {{SETUP}}/round-state.py mark "$ARTIFACTS_DIR" review-start

YOUR LAST ACTION, after the envelope file is written, is exactly:

    python3 {{SETUP}}/round-state.py mark "$ARTIFACTS_DIR" review-done "$ARTIFACTS_DIR/round-$N/review-envelope.txt"

You are read-only in the candidate worktree (contract section 1). `review-gate`
compares the tree before and after this session; an edit fails the round. The
skill's autofix path is prohibited here: invoke it in report-only terms and
apply nothing.

## Step 1 — locate the round

    echo "$ARTIFACTS_DIR"
    N=$(cat "$ARTIFACTS_DIR/round.txt")

Read from `$ARTIFACTS_DIR/round-$N/`:

- `review-input.json` — the review id goes into the envelope's `Input:` verbatim.
- `review-scope.txt` — `full` for this mode.
- `review-base.txt` — the base sha.

The worktree to review is the `worktree` field of `$ARTIFACTS_DIR/params.json`.
You are NOT cwd'd there: `cd` into it for every git command.

## Step 2 — persona selection is the skill's

Let `ce-code-review` select its always-on and conditional personas by its own
rules for this diff, and run its validator pass. Do not cap the set below what
the skill's own sizing rule gives: a measured fixed cap (three personas) saved
no wall time and lost a P1 the full set had found.

Measure the diff first: in the worktree, run
`git diff --stat <base>..HEAD` with the base from `review-base.txt`.
Size the review on PRODUCTION churn: count changed lines EXCLUDING
test/spec files, generated files, and lockfiles — the same basis
ce-code-review's own catalog sizes a review on. Test volume is proof
of the change, not production risk surface, so counting it inflates
the persona fan-out exactly where the tests are most thorough.
Under roughly 150 production lines, cap the conditional personas at
3 beyond the always-on set, and record the decision (production
lines, total lines, cap applied or not, personas chosen) in the
envelope's Coverage section so the choice is observable.

## Step 3 — invoke the skill

    ce-code-review  mode:headless  base:<the sha from review-base.txt>  plan:$ARTIFACTS_DIR/plan.md

with these instructions carried into the invocation:

- Do not expand scope beyond `<base>..HEAD`.
- Findings carry the extra field `finding_id` computed as contract section 4
  defines. Every finding cites a `file` and `line` that exists at Head.
- Contract section 3 governs pinned decisions: read `pinned_decisions` from
  `$ARTIFACTS_DIR/joint-plan.json` verbatim and route anything a pin covers to
  the `Pinned decisions` heading as advisory, whatever its severity.
- If `$ARTIFACTS_DIR/waivers.json` or the previous round's
  `$ARTIFACTS_DIR/round-$((N-1))/ledger.json` exists, read it first. A finding
  whose `finding_id` matches an entry there goes under `Previously waived`, not
  into the actionable set, unless the reviewer states specific new evidence that
  was absent when the entry was recorded.
- External or cross-model review is prohibited, regardless of what the skill's
  own references permit.

The staged skill may be either contract generation: CE 3.2.0's markdown headless
envelope, or the newer agent-JSON contract where `mode:headless` aliases
`mode:agent` and the skill returns one raw JSON object. Invoke with the same
arguments either way.

Do not return until every persona the skill dispatched has reported, and until
the validator pass has run. A relayed envelope that stops mid-fan-out, or that
carries an empty verdict, is not a completed review: the round is billed and
every downstream gate rejects it. If a persona cannot finish, say which one and
why in the envelope and still emit a verdict.

## Step 4 — write the envelope

Not before now: after the skill has returned and its validator pass has run. An
envelope written mid-fan-out is a billed round whose gate cannot tell it from a
finished one.

Relay the skill's full return verbatim, write it to
`$ARTIFACTS_DIR/round-$N/review-envelope.txt`, and append the footer below. Its
final six lines are exactly, one per line, nothing after them:

    Scope: full
    Base: <base sha>
    Head: <head sha>
    Input: <the review id from review-input.json>
    Verdict: <Ready to merge|Ready with fixes|Not ready>
    Review complete

The verdict is the skill's, mapped into the enum unchanged. `Not ready` asserts
an open P0 or P1; do not emit it otherwise — the round stops with
`NOT_READY_WITHOUT_BLOCKER`.

Then run the `mark review-done` command from the top of this prompt.

If a persona cannot finish, say which one and why in the envelope and still emit
the footer.
