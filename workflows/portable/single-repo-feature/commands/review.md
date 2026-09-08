Perform the W6 code-review role against binding.baseCommit in the isolated
binding.repositoryRoot recorded in run-context.json. Read approved plan.json,
verification.json, policy-context.json, and the SAME frozen knowledge packet used
by planning and implementation. The review is constrained to plan.files.

Review correctness, missing/weak tests, scope drift, and any change outside the
CTA-only frontend intent. The mechanical verification record owns test outcomes;
do not substitute your self-report. Do not edit source, artifacts, knowledge, Git
state, or provider configuration.

Return the existing review-envelope vocabulary: status is complete, degraded, or
failed; verdict is Ready to merge, Ready with fixes, or Not ready; findings are
concrete strings. Only a complete Ready to merge review with no findings can pass
the mechanical gate. Ready with fixes requires another bounded implementation and
verification round. No automatic merge or deployment follows this verdict.

For profile v2, use its explicit source recipe and repository facts. A Bun engine workspace or Python/shell workflow pack does not imply Next or a packageManager field. Do not invent missing metadata.
