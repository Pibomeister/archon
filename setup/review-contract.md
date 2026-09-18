# Archon review contract v2.0

This file is the lane's review contract. Its sha256 is the `contract_digest`
constituent of the review identity, so editing it invalidates every completed
review of an open round and forces a re-review. Change it deliberately and bump
the version line above.

Every review session in the `review-loop` — discovery (`full`) or verification
(`verify`) — obeys this contract. The lane's prompt bodies
(`setup/prompts/review-trio.md`, `setup/prompts/review-capped.md`,
`setup/prompts/review-verify.md`) reference it rather than restating it.

## 1. The reviewer is read-only

The reviewer never edits, stages, commits, reverts or formats a file in the
candidate worktree, and never runs a tool that does. It never runs the fixer's
job "while it is here". Repairs belong to the `fixer` node.

`review-gate` snapshots the candidate tree before the session starts and
compares it afterwards. A tree that moved under a reviewer fails
`REVIEW_WROTE_TREE round=N files=<n>`, the round's gate does not pass, and the
work is thrown away. This is not advisory.

Reading is unrestricted: read any file in the worktree, run any read-only git
command, run the repository's own test and type-check commands.

## 2. Scope

The round decides the scope before the session starts and records it in
`round-N/review-scope.txt` and `round-N/review-base.txt`. The session is given
both. It reviews exactly `<base>..<head>` and nothing else.

- `full` — the whole candidate diff against the round's base.
- `verify` — the previous round's repair only: the hunks between the previous
  round's `pre-head` and the current head, plus a bounded regression scan
  (section 6).

Widening the scope on the reviewer's own judgement is a contract violation.
Out-of-diff code is read as *evidence* for a finding about the diff; it is never
itself the subject of a finding.

## 3. Pinned decisions

`joint-plan.json` in the artifacts directory may carry a non-empty
`pinned_decisions` array. Each entry names a symbol or file the approved plan
froze, with the spec's complete rule text and an `allowed_change`.

A finding whose only fix would change a pinned symbol's name, signature or
stated rule is **advisory**, whatever its severity. It goes under a heading
reading exactly `Pinned decisions`, carries the field `pinned: <symbol>`, and
states what a re-plan would have to decide. It is never raised as an actionable
P0–P2 for the fixer. The plan gate, not this review, is where a pin is reopened.

An entry whose `allowed_change` names an exception permits exactly that change
and nothing wider; the reviewer verifies the change made is the one the
exception describes.

## 4. Finding shape

Findings are emitted as JSON conforming to
`vendor/ce-skills/3.2.0/ce-code-review/references/findings-schema.json`, with
one added required field:

```
finding_id = sha1(normalize(file) + "|" + normalize(title))[:12]
```

`normalize` is the transform in `setup/finding_key.py`: cut the text at the
first ` -- ` (or its em-dash spelling), drop a trailing `(path:line, …)`
parenthetical, casefold, strip everything outside `[a-z0-9 ]`, collapse
whitespace. `file` is normalized the same way. The id is what the ledger keys
on, so a finding restated next round with a new line number or a provenance
suffix keeps its id and does not mint a second entry.

Severities are the schema's: `P0` (data loss, security, breakage on the
happy path), `P1` (a real defect a user or caller hits), `P2` (correctness or
coverage debt with no live failure), `P3` (style, naming, nits).

A finding must cite `file` and `line` that exist in the candidate at `head`, and
`evidence` must quote or name what was read. A finding whose cited line does not
exist is dropped by the validator, not downgraded.

## 5. The validator pass

Every mode ends with one `validator` subagent. It re-reads each finding against
the candidate and may only **drop or downgrade** — never add a finding, never
raise a severity, never rewrite a title (which would change the `finding_id`).

It drops a finding when the cited line does not exist, when the claim is
contradicted by code the finding did not read, or when the behaviour described
is already handled out of diff. It downgrades when the failure mode is real but
narrower than the severity claims.

Its drops and downgrades are listed in the envelope with one reason each.

## 6. Verification rounds

A `verify` round is given `round-N/review-findings.json`, the accumulated
ledger. For each entry it returns exactly one of:

- `closed` — the defect is gone, with a cited line at the current head showing why.
- `open` — the defect is still present, with a cited line.
- `regressed` — the repair introduced a different failure of the same finding.

A claim of `closed` without a cited line at the current head is not closure and
is read as `open`.

The bounded regression scan covers: every hunk of the repair diff, every symbol
those hunks call or are called by within the changed files, and every caller of
a changed exported symbol. A new P0/P1 found there is reported as a new finding
and forces the next round back to `full`.

Verdict for a verify round is `Ready with fixes` (or `Ready to merge`) iff no
P0/P1 is `open` or `regressed` and no new P0/P1 was found; otherwise
`Not ready`.

## 7. Verdict

Exactly one of the three, spelled exactly:

```
Ready to merge | Ready with fixes | Not ready
```

`Not ready` asserts that at least one P0 or P1 is open or regressed. A `Not
ready` verdict with no such finding is a contract violation and stops the round
with `NOT_READY_WITHOUT_BLOCKER`.

`Ready to merge` asserts no actionable finding at any severity. `Ready with
fixes` is the normal outcome of a round that found P2/P3 work or P0/P1 work the
fixer will apply.

## 8. Envelope

The session's last output is the envelope. Its final lines are, in this order,
one per line, with no markdown emphasis and nothing after them:

```
Scope: <full|verify>
Base: <base sha>
Head: <head sha>
Input: <the review id from review-input.json>
Verdict: <one of the three>
Review complete
```

`review-gate` compares `Input:` and `Head:` mechanically against
`round-N/review-input.json` (`GATE_5_input_matches`). A mismatch fails the
round: it means the envelope describes a different candidate than the one the
round opened on.

Above the footer the envelope carries, in any readable form: the findings as
JSON, the validator's drops and downgrades with reasons, a `Pinned decisions`
heading when section 3 applied, and — in a verify round — the per-finding
disposition table.

A truncated envelope is not a review. If a subagent cannot finish, say which and
why in the envelope and still emit the footer.

## 9. Prohibitions

External or cross-model review is prohibited in this unattended pipeline: the
diff is never sent to any peer outside this session.

The reviewer does not invoke autofix, does not write to the artifacts directory
except through `round-state.py`, and does not read another round's artifacts as
though they described this one.
