Prepare proposed knowledge from this run's plan.json, verification.json, review.json,
and pr-evidence.json. Read the frozen knowledge packet to distinguish known facts
from new observations. Record the real publication state; never say a change shipped
when only a draft or publication intent exists.

Return JSON with summary and promotionCandidates. Each candidate should explain a
specific decision, discovered footgun, or useful test and cite the relevant run
artifact. Return an empty candidate list if nothing new was learned. Do not write to
or read a floating external KB checkout. A mechanical step saves this proposal in
run artifacts; reviewed promotion is a separate job.
