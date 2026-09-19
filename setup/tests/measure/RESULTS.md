# Review topology measurement gate — item 2 and item 7

Measured 2026-09-16 against `.archon` `feat/archon-2h-v2-b`. Harness:
`setup/tests/measure/review-bench.py`, re-summarised by
`setup/tests/measure/summarize.py`. Provider `claude -p --model sonnet
--output-format stream-json`, one session per run, cwd set to a fixed candidate
worktree, `ARTIFACTS_DIR` a scratch directory seeded with that round's files.

Every number below is a measurement from these runs. Nothing is carried over
from v1 except the candidate shas and the v1 finding lists the parity bar
compares against, which are named with their source paths.

## What was run

| label | prompt | candidate | base..head |
|---|---|---|---|
| `api-trio` | `setup/prompts/review-trio.md` | api | `c13a1f48`..`e42b2422` |
| `api-capped` | `setup/prompts/review-capped.md` | api | `c13a1f48`..`e42b2422` |
| `mcp-trio` | `setup/prompts/review-trio.md` | mcp | `baaf2dfa`..`86b8e133` |
| `mcp-capped` | `setup/prompts/review-capped.md` | mcp | `baaf2dfa`..`86b8e133` |
| `api-verify` | `setup/prompts/review-verify.md` | api | `466b2e92`..`02b46b36` |
| `docreview` | `setup/prompts/docreview-bounded.md` | plan `692005f7/plan.post-critic.md` | — |

Three runs each, the two candidates sequentially rather than in parallel: two
review sessions competing for the same subscription quota measure contention,
not topology.

## Methodology decisions, and why each one was forced

**The candidate ranges are not the ones the plan names, and the plan is wrong.**
The plan names api `e42b2422..85b43ebd` and mcp `86b8e133..f193473a` and calls
them "the round-1 diff", then asks for parity against v1's round-1 findings.
Read from the v1 artifacts, those are v1's ROUND-2 delta ranges: round 1 of each
stage ran `mode=full base=origin/main` with `pre-head` `e42b2422` (api) and
`86b8e133` (mcp), so the diff v1's round-1 review read is `bootstrap..pre-head`
— `c13a1f48..e42b2422` and `baaf2dfa..86b8e133`. The plan's ranges exclude that
code entirely.

Two things follow, both measured. The api P1 on the parity list — the permanent
share/reshare lockout via the legacy `reserved` column — lives in the guard
added *at* `e42b2422`, which is the BASE of the plan's range, so no mode could
have found it there; a first trio run on the plan's range returned two different
P1s and would have been scored a parity failure for reviewing exactly what it
was told to review. And the plan's ranges are the smaller diffs (196 and 117
changed lines against 339 and 532), so measuring latency on them understates
review cost and biases p90 toward passing the bar. That run was discarded.

**The candidate worktrees are new, detached checkouts at the exact shas.** The
plan named the v1 stage worktrees, but neither named head is still an ancestor
of those worktrees' current HEAD — the branches were rewritten after v1 — so
`base..HEAD` there is not the candidate the plan specified. Dedicated detached
worktrees under `.bench-2hv2-b/` review exactly `base..<named head>`, and they
are throwaway, so a reviewer that violated the read-only contract could not
damage anything a worker is using. Every run records `tree_moved`; see the table.

**Timing is the envelope file's mtime, taken after the session exits.** Two
earlier metrics were wrong and both were measured to be wrong rather than
reasoned about. Matching `Review complete` anywhere in the event stream timed
the session's echo of its own prompt — every prompt quotes the footer it must
emit — and reported an eight-minute review as 0.2 minutes. Process exit
overstates it, because the prompt's last action is a marker write and models
narrate afterwards; wall time is recorded beside the metric so the gap is
visible rather than assumed.

**`verify` is measured once, not once per mode.** The verification pass is the
validator role alone over the ledger. It is the same prompt whichever discovery
topology ships, so running it twice would have measured the same thing twice and
billed for it. The measured `v` applies to both modes.

**The bench's scratch directory lives outside every checkout.** The first real
trio run resolved `{{SETUP}}` back into the `.archon` worktree through a
symlink, ran `find` across it, read `setup/finding_key.py` and the harness's own
source, and billed four minutes of a review to doing so. The contract and the
one schema file the prompts cite are now byte copies inside the scratch tree.
That run was discarded; the numbers below are from the restarted gate.

## Caveats a reader has to carry

**Parity is judged by reading the envelopes, not by matching titles.** A finding
restated in different words is the same finding, and a mechanical comparison
would report a false miss. The parity targets are v1's round-1 P0/P1s
(`8ddb9cce/round-1/fixer-result.json` and `01845af1/round-1/fixer-result.json`);
there are no P0s, and one P1 each. api: "Permanent share/reshare lockout: once a
group's legacy `reserved` column is ever true, `GroupService.shareGroup` /
`reshareGroup` reject it forever". mcp: "share_group cannot detect an existing
invite-only share and will silently create an additional public link on top of
it". Both were filed `advisory` by v1's round-1 fixer, so both are still present
in the candidate at its head.

**No installed `node_modules` in the candidate worktrees, and no
`gate-tests.txt` seeded.** The lane gives the reviewer the gate-test output the
stage already produced. The bench does not, so findings here come from static
reading. This depresses coverage equally for both modes, and the trio says so in
its own Coverage note.

<!-- RESULTS -->

## Results (2026-09-16, harness `review-bench.py`, sonnet, one session per run)

Minutes are `seconds_to_envelope` (envelope mtime after session exit). Parity = the v1 round-1 P1 for
that candidate re-found as a P0/P1 finding (read from each run's finding list, titles compared by
meaning). Docreview runs did not start: the Claude session limit was hit after `api-verify` run 1.

| label | runs (min) | p50 | p90 | verdicts | parity (v1 round-1 P1) |
|---|---|---:|---:|---|---|
| api-trio | 15.1, 9.7, 14.4 | 14.4 | 15.0 | RwF, RtM, RwF | 0/3 — run 1 saw the `isReserved` asymmetry and the validator dropped it as out-of-diff (contract §2); runs 2–3 did not raise it |
| api-capped | 15.0, 13.0, 13.7 | 13.7 | 14.7 | RwF, RtM, RwF | 0/3 — run 1's P1 is a different finding (`sharedlinks.repo.ts:310`); run 2 filed the `deleteGroup` guard asymmetry as P3 advisory |
| mcp-trio | 11.3, 16.9, 11.2 | 11.3 | 15.8 | RwF ×3 | 0/3 — findings: ownership `withErrorDetail`, unvalidated share-link response, concurrent double-share (P3) |
| mcp-capped | 8.2, 12.2, 11.4 | 11.4 | 12.0 | NR, RwF, RwF | 0/3 — run 1's three P1s are the concurrent-call race and response-shape findings, not the invite-only existing-share miss |
| api-verify | 4.2 (runs 2–3 did not complete) | 4.2 | — | RtM | recall target (v1 round-4 P1, public→invite-only leaves the old public link live) **not found**; the three applied findings were verified `closed` with cited lines |
| docreview | — | — | — | — | not measured |

Bar from the plan: discovery p90 ≤ 8, verify p90 ≤ 4, parity on both candidates, verify recall hit.
Neither discovery mode meets any part of the bar, and their costs are indistinguishable from the v1
persona fan-out (v1 measured 8.8–19.7, mean 14.4). The topology is therefore not the lever the plan
assumed: replays and rounds are.

<!-- PICK -->

## Pick

**No topology change.** Mode `ce` (`setup/prompts/review-ce.md`): the unchanged `ce-code-review`
skill invocation with the round's execution contract prepended (marks, read-only rule, envelope
`Input:`/`Head:`). Rationale: the full persona set found the api P1 that both reduced modes missed,
at the same cost. `verify` is kept for P2-only rounds at its measured 4.2 min with the recall miss
recorded as a known residual (a P1 repair still forces a `full` next round). The bounded docreview
is not shipped (unmeasured). Re-derived scenario with measured numbers: `84 + 3×14.4 + 2×4.2 ≈ 136`
min active; the live gate is stated as such in the PR, not as 120.
