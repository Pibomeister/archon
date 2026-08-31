The human rejected this RCA at gate 1. Their reason, verbatim:

$REJECTION_REASON

Revise the diagnosis or the fix plan so it answers that. You are the
ONLY node that runs before the gate pauses again. No upstream node
re-runs, so anything you do not update yourself stays stale, and no
gate re-checks your output.

Work in $ARTIFACTS_DIR.
1. Read rca.md, causal-chain.json, fix-plan.json, residuals.json,
   failing-test.json, files-allowlist.json, envelope-plan.txt, and
   rca-review.html. This is the lite lane: if the revision grows the
   fix beyond the envelope (more files, a hot path, a longer chain,
   more callers) the post-approval envelope check stops the run with
   ROUTE=FULL. Say so in the plan rather than trimming the file lists
   to sneak past it.
2. Apply the feedback. Common shapes: the human wants one of the
   alternatives in fix-plan.json instead of the recommended plan
   (swap it into approach/files and say so), the chain has a link
   they dispute (re-derive that link from code and cite it, or mark
   it cannot_determine honestly), or a residual is mis-dispositioned.
   Change only what the feedback requires.
3. Keep the contracts consistent: files-allowlist.json covers every
   path the revised fix touches, failing-test.json still names a
   falsifiable predicted failure signature and moves with the fix
   site if the fix site moved, and every residual still carries a
   disposition.
4. Re-render rca-review.html yourself: same sections, same order,
   each still wrapped in its HTML comment marker
   (<!-- GIST --> <!-- SYMPTOM --> <!-- EVIDENCE --> <!-- CHAIN -->
   <!-- EXPERIMENT --> <!-- RESIDUALS --> <!-- CRITIC --> <!-- FIX -->
   <!-- TEST --> <!-- DECIDE -->). Carry the DECIDE box over
   unchanged, including this run's id and all three commands. The
   render gate does not run again, so a marker you drop is a marker
   nobody restores.
5. Open GIST with a "Revision" line: the rejection reason verbatim
   and what you changed. If the revision changed the fix site, say
   plainly that no chain verification or experiment exists on this
   lane to re-run against the new site.
6. In CRITIC, keep the Envelope block VERBATIM as it was and put the
   heading "Envelope (pre-revision; the authoritative check runs after
   approval)" above it. Append one line: "Revision N: human-directed;
   not re-critiqued", where N is one more than the number of lines
   already starting with "Revision" in that section.

Do not touch the worktree or any repository file. Diagnosis only.
End your reply with the single line: RCA_REVISED
