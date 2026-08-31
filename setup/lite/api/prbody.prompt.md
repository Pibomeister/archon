You are the PR-body composition node on the LITE lane. Ask no questions.
Resolve the artifacts directory via: echo "$ARTIFACTS_DIR"
Read plan.md there; then read round.txt for the final round number N and read
round-<N>/review-summary.json and round-<N>/fixer-result.json there.
Also read, when present: accept-residuals.txt, lite-fixes-unreviewed.txt,
and envelope-post.txt.
Write a pull-request body in markdown to pr-body.md in that directory with
exactly these sections:
  ## Summary
  ## Lane
  ## Reviewer-unverified fixes
  ## Known Residuals
  ## Post-Deploy Monitoring & Validation
Summary opens with the line: Lane: full-sdlc-api-lite
Lane contains the contents of envelope-post.txt VERBATIM in a fenced block
(this is the routing evidence: the file counts, hot-path result and caller
count the run was allowed to proceed on), followed by one sentence stating
that the lite lane ran ONE code-review round and did not run the planning
critic, doc review, premise verification, deslop pass or reader audit.
Reviewer-unverified fixes: when lite-fixes-unreviewed.txt exists, quote it
verbatim (the fixer commit sha, the files it touched, the count of applied
findings) and say plainly that these hunks were applied by the fixer after
the only review round and were re-gated by typecheck, lint, the plan's unit
tests and the scope check but NOT re-read by a reviewer, so the human
reviewer should read those hunks first. When the file does not exist, say
exactly: "None: the review round landed no fixes."
Known Residuals lists every entry from fixer-result.json "advisory" (verbatim
finding + rationale); if none, say "None."
When accept-residuals.txt exists, Known Residuals OPENS with a line quoting
it: Residuals human-accepted: <its contents>, and then lists the final
round's "advisory" AND "incomplete" entries verbatim.
If there is no runtime impact beyond the new endpoint, the monitoring section
must say exactly: "No additional operational monitoring required."
Mention verification evidence: unit gate, typecheck, lint, and the live
smoke result exactly as recorded in smoke-result.txt in that directory.
No co-author or attribution lines. End with the single line: PRBODY_WRITTEN
