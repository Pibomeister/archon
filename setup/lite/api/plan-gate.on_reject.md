The human rejected this plan at the gate. Their reason, verbatim:

$REJECTION_REASON

Resolve the artifacts directory first with: echo "$ARTIFACTS_DIR".
Do not revise plan.md, files-allowlist.json, reader-audit.json, or
plan-review.html in this run. The lite envelope check after approval
cannot make an in-run rewrite independently verified. Write
plan-rejection-receipt.json ONLY inside that exact artifacts directory,
containing the verbatim reason, current run id, and
{"required_transition":"FRESH_RUN"}. Explain that the operator must
abandon this run and start a fresh guarded run. End with:
PLAN_REJECTION_RECORDED
