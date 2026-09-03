The repository checkout to review is the "worktree" field of
params.json in the directory named by the ARTIFACTS_DIR environment
variable (you are NOT cwd'd there — cd into it for every git
command). It is already on the branch to be reviewed. The diff is a
BUG FIX: a red repro test plus a minimal fix; rca.md in the
artifacts directory plays the plan's role.

Resolve the artifacts directory by running: echo "$ARTIFACTS_DIR"
Read repo-policy.json and test-placement.json there before review. Treat every
blocking repository rule as a blocking finding with its exact cited source.
Verify that a new test file is permitted when a baseline test for the same
production file already exists.

Persona selection happens in this session, so three instructions
BEFORE you invoke the skill:
- Measure the diff first: in the worktree, run
  git diff --stat "$(cat <that directory>/bootstrap-head.txt)"..HEAD
  Under roughly 150 changed lines, cap the conditional personas at
  3 beyond the always-on set, and record the decision (diff size,
  cap applied or not, personas chosen) in the envelope's Coverage
  section so the choice is observable.
- Task one reviewer lens with exactly this: "verify the fix targets
  the root cause named in rca.md's Best Explanation — not the
  symptom site — and verify the test would catch a regression of
  exactly that cause; report mismatches as findings."
- LITE lane, no deslop pass ran: task a second lens with exactly
  this: "read the repro test named in failing-test.json's test_file
  against red-out.txt in the artifacts directory; a test that would
  pass without the fix, asserts a literal, mocks the very behavior
  under test, or fails for a reason other than the defect in the
  causal chain is a P0 finding titled 'tautological or
  non-reproducing test'." Feed it through the same merge pipeline. Feed its
  findings through the skill's normal merge pipeline.
- If waivers.md exists in the artifacts directory, read it first.
  A finding that materially matches a waived entry goes under a
  "Previously waived" heading in the envelope — NOT as a new
  actionable finding — unless the reviewer states specific new
  evidence that was absent when the waiver was recorded.

Then invoke the ce-code-review skill with arguments exactly:
  mode:headless base:<SHA from bugfix-chain.json baseline.commits[repo]> plan:<that directory>/rca.md
Do not expand scope beyond the diff against that base.
The staged ce-code-review may be either contract generation: CE 3.2.0's
markdown headless envelope, or the newer agent-JSON contract (there
mode:headless aliases mode:agent and the skill returns ONE raw JSON
object with status and verdict fields). Invoke with the same
arguments either way and relay the skill's full return VERBATIM
(envelope or raw JSON) as part of your output. External/cross-model
review is PROHIBITED in this unattended pipeline: never send the
diff to any external peer, regardless of what the skill's own
references permit.
