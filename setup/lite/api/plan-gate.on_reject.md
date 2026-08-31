The human rejected this plan at the gate. Their reason, verbatim:

$REJECTION_REASON

Revise the plan so it answers that. You are the ONLY node that runs
before the gate pauses again. No upstream node re-runs, so anything
you do not update yourself stays stale, and no gate re-checks your
output until after approval.

Work in $ARTIFACTS_DIR.
1. Read plan.md, files-allowlist.json, reader-audit.json,
   envelope-plan.txt, and plan-review.html.
2. Rewrite plan.md to address the rejection and nothing else. Change
   only what the feedback requires. If the feedback asks a question
   rather than giving an instruction, answer it inside the plan.
3. Keep the contracts consistent with the revised plan: every path
   the plan now touches must appear in files-allowlist.json, and
   reader-audit.json must list any column whose interpretation or
   presentation semantics the revision changes. Remember this is the
   lite lane: if the revision now touches more files than the envelope
   allows, a migration, an entity, auth, billing, search, infra or a
   lambda, or declares a reader-audit column, the post-approval
   envelope check will stop the run with ROUTE=FULL. Say so plainly in
   the plan rather than trimming the allowlist to sneak past it.
4. Re-render plan-review.html yourself: same sections, same order,
   each still wrapped in its HTML comment marker
   (<!-- GIST --> <!-- KB --> <!-- MAP --> <!-- PLAN --> <!-- REVIEW -->
   <!-- CRITIC --> <!-- DECIDE -->). Carry the DECIDE box over unchanged,
   including this run's id and all three commands. The render gate does
   not run again, so a marker you drop is a marker nobody restores.
5. Open REVIEW with a "Revision" block: the rejection reason verbatim
   and what you changed in response, then the existing "Not run on the
   lite lane" sentence.
6. In CRITIC, keep the Envelope block VERBATIM as it was and put the
   heading "Envelope (pre-revision; the authoritative check runs after
   approval)" above it. Append one line: "Revision N: human-directed;
   not re-critiqued", where N is one more than the number of lines
   already starting with "Revision" in that section.

Do not touch the worktree or any repository file. Plans only.
End your reply with the single line: PLAN_REVISED
