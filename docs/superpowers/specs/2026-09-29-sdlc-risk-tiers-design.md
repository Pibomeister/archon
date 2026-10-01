# Risk-tiered SDLC: sdlc-red / sdlc-yellow / sdlc-green

Design spec agreed in brainstorming on 2026-09-29. All slices are specified here at full
depth per the user's choice. Implementation order is the slice order below.

## Context

Archon today has two hand-written feature lanes, `full-sdlc-api` and `full-sdlc-web`, plus a
generated `full-sdlc-api-lite`. The split is by discipline (backend vs frontend) and by
effort (full vs lite). The research in the agent-kit mission brief and the "Trust Is the
Control Plane" report argues that diligence should follow the work item's risk envelope
(blast radius, reversibility, authority, evidence availability, independence), not the
discipline. The user wants three lanes keyed by a risk score, with the frontend/backend
distinction dissolved: the process (plan, implement, gather evidence, review, ship) is
discipline-agnostic, and only the evidence artifacts are project-specific.

Facts about the current tree that shape the design (all paths relative to the repository
root):

- No risk tier, blast radius, or diligence concept exists. The closest mechanisms are
  `setup/lite-envelope.json` + `setup/lite-envelope.sh` (file caps, `hot_paths`, S/M/L triage,
  prints `ROUTE=LITE|FULL`), the P0–P3 severity gates, `scope.forbiddenPaths` in profiles, and
  the risk-area table in `setup/review_policy.py:27-40`.
- `full-sdlc-web.yaml` is hand-written, not derived. The only real api/web differences are
  test commands (`setup/repo-profile.sh` arrays vs hardcoded pnpm), smoke shape
  (`full-sdlc-api.yaml:2337` http boot + 401 negative control vs web `uat` at
  `full-sdlc-web.yaml:1089` through `setup/browser-verifier.py`), and the
  `browser-evidence.json` artifact. Implement prompts differ; review/fixer prompts are ~92%
  shared.
- Lite derivation is manifest + overlays + produces/consumes contracts
  (`setup/derive-lite.py`, `setup/lite/api.json`, `setup/lite/api/*`), codex twins come from
  `setup/derive-codex.py` (`TARGETS` at lines 59-69), drift is checked in `setup/package.sh:372`
  and `:380`, and shared prompt lines are locked by `setup/lane-doctrine.py`.
- Routing: `setup/archon-run.py:65-68` `FEATURE_LANES` maps api/web only; lite is never
  auto-routed. Bugfix has an adaptive router (`static_bugfix_route`:3101,
  `adaptive_bugfix`:3250, `write_routing_receipt`:3150, `wait_for_pre_envelope`:3160) that is
  the template for tier routing. Repository chains map repo to lane in
  `setup/feature_chain.py:43-54`.
- Profiles: `profiles/project-profile.v2.schema.json` has `verification[]` (argv + timeout),
  `scope.allowedPaths/forbiddenPaths`, `delivery` pinned to draft-only/no-merge.
- Skills layer: `library/<repo>/raw/index.jsonl` is append-only pointers written only by the
  ingest path (`setup/skill-score.py ingest`, fed by `setup/trace-digest.py`);
  `workflows/skill-evolve.yaml` is preflight → trace-digest → score-run → evolve-route →
  wiki-maintain → wiki-gate → skill-propose → proposal-gate → skill-critic → skill-admit →
  report.
- Machine paths in lane yamls are reverse-templated by `setup/package.sh:322` (`template()`),
  not by the portable machine binding.

## Decisions (agreed)

1. **Score source**: deterministic scorer plus agent judgment, both always consulted, combined
   by max. CODEOWNERS is an input when present. PII, auth, crypto, payments, secrets are
   always treated as owned territory (red floor) whether or not CODEOWNERS exists.
2. **Floors dominate points.** A floor sets the minimum tier outright with a named reason.
   Points decide only between green and yellow when no floor fires.
3. **Human gates**: green none, yellow plan gate, red plan gate + merge gate.
4. **Green ships automatically**: non-draft PR, CODEOWNERS reviewers requested, auto-merge
   enabled only when branch protection with required checks exists. Explicit policy change
   recorded in RUNBOOK and profile schema.
5. **Escalate only.** Score at intake, re-score after plan and after implement. A run is
   promoted by handoff to the higher lane; never demoted.
6. **Calibration loop** closes the gap between intake and final tiers over time, via a
   propose → gate → critic → human approval → admit path. Calibration can never lower a
   sensitive-domain or factory-control floor and can never turn auto-merge on.
7. **One discipline-agnostic family.** `sdlc-red` hand-written parent; yellow and green
   derived. `full-sdlc-api`, `full-sdlc-web`, `full-sdlc-api-lite` and their codex twins retire
   after parity. Bugfix lanes untouched.
8. **Fixes are always reviewed.** Every tier's review loop is review → fixer → delta review
   of the fixes. No tier ships "unreviewed fixes".
9. **Structure A**: three derived lanes with handoff escalation (not a single tiered graph).

## Slice 1: Risk scorer, policy, CODEOWNERS, ledger pointer

### New files

- `setup/risk-score.py` (stdlib only; workflow nodes have no PyYAML). CLI:
  `risk-score.py <intake|plan|impl> --artifacts <dir> --profile <profile.json> --repo-root <wt>
  [--base <sha>] [--policy <risk-policy.json>] [--overlay <policy-overlay.json>]
  [--handoff <escalation.json>] [--override-tier <red|yellow>]`.
  Typed last line: `RISK_TIER=<red|yellow|green> stage=<stage> score=<n> floors=<a,b>` exit 0;
  `RISK=FAIL <reason>` exit 1. Writes `risk-<stage>.json` (schema `archon.risk-score.v1`)
  atomically and appends one line to `risk-trajectory.jsonl`.
- `setup/risk-policy.json` (successor of `setup/lite-envelope.json`): `version`,
  `scoringVersion`, `thresholds {yellow, red}`, `taskClassPoints`, `sizeThresholds`
  (`max_files`, `max_test_files`, `max_d1_callers`, `max_chain_links`, carried over),
  `sensitiveDomains` (built-in red floors: path/name regexes for auth, oauth, session, token,
  crypto, kms, secret, payment, billing, stripe, pii, gdpr, consent, plus profile globs),
  `factoryControl` (red floor: `workflows/**`, `setup/**`, `profiles/**`, `**/CODEOWNERS`,
  `.github/workflows/**`, CI config), `reversibility` (lockfiles/manifests yellow,
  migrations red, data-mutation script globs red), `publicContract` (yellow: generated
  clients, exported DTO globs), `repro_command_allow` (moved from lite-envelope for bugfix
  compatibility).
- `setup/risk_policy.py`: importable helpers shared by scorer, calibrator, gates: load and
  merge (defaults ← profile.risk ← repo overlay), floor-invariant check, glob matching with
  the anchored-prefix semantics `lite-envelope.sh` already uses, CODEOWNERS parser (last
  matching rule wins, gitignore-style globs, absent file → `codeowners: null`).
- `setup/tests/test_risk_score.py`, `setup/tests/test_risk_policy.py`.

### Inputs per stage

| Stage | Reads | Diff footprint |
|---|---|---|
| intake | brief (run message path), profile, CODEOWNERS at base commit, task class from brief header (`Kind:` line or `## Kind`; missing → `feature`) | none; path signals come only from explicit paths in the brief |
| plan | `files-allowlist.json`, `impact.json` (callers, chain links), `triage.json`, `risk-judgment.json` | allowlist |
| impl | `git diff --name-status <bootstrap-head>..HEAD`, test-file count, `impact.json` | actual diff |

### Output contract (`archon.risk-score.v1`)

```
{ schema, scoringVersion, policyVersion, overlayVersion|null, stage,
  mechanical: {tier, score, signals:[{id, value, points, floor|null, source:"mechanical", evidence}]},
  agent: {tier|null, rationale|null, unknowns:[]},
  prior: {tier|null, stage|null},
  tier, floors:[{id, reason, paths:[]}],
  codeowners: {present, ownersTouched:[], floorsApplied:[]} ,
  inputs: {briefSha256, allowlistSha256|null, diffSha256|null, codeownersSha256|null,
           profileSha256, policySha256, overlaySha256|null, baseCommit, head|null},
  escalated: bool, escalatedFrom: tier|null }
```

Rules: `tier = max(mechanical, agent, prior, override)`; any missing or unparsable required
input → `RISK=FAIL` and the calling node treats it as red (fail closed, never green).
Agent tier comes from `risk-judgment.json` `{schema:"archon.risk-judgment.v1", tier,
rationale, unknowns[]}` emitted by ralplan (Slice 3 adds it to the prompt and contracts).
Disagreement between mechanical and agent is recorded, never resolved.

### Profile extension (part of v3, defined fully in Slice 2)

```
"risk": {
  "protectedAreas": [{"paths":["libs/data-access/src/lib/rds/migrations/"], "floor":"red", "reason":"schema"}],
  "codeowners": {"ownerFloors": {"@org/platform":"red"}, "defaultOwnedFloor":"yellow"},
  "sensitiveDomains": {"extraPaths": []},
  "publicContract": {"paths": []},
  "sideEffects": {"paths": []},
  "thresholds": {"yellow": 30, "red": 60}
}
```
Seed the Goodword protected areas from today's `hot_paths` list.

### Ledger pointer

Extend `setup/trace-digest.py` to include a `risk` block when `risk-trajectory.jsonl` exists:
`{intake, plan, impl, final, escalatedAt|null, escalationRunId|null, reviewSeverities{P0..P3},
fixerRounds, exitGate, mergeGate|null, delivery{mode, autoMerge, prUrl|null}}`. Absent → null,
never inferred. `setup/skill-score.py ingest` already writes the digest pointer into
`library/<repo>/raw/index.jsonl`; no new writer. Bump `SCHEMA_DIGEST` consumers' tests
(`setup/tests/test_trace_digest.py` if present, else add).

### Tests (Slice 1)

- Floors dominate: a one-line docs change under `auth/` is red regardless of points.
- Sensitive domains fire without CODEOWNERS; with CODEOWNERS, owner floors and
  `ownersTouched` populate; malformed CODEOWNERS → FAIL.
- Max rule across mechanical/agent/prior/override; override never lowers.
- Fail closed: missing allowlist at `plan`, unreadable diff at `impl`, bad profile.
- Digest binding: same inputs → identical JSON; changed diff → changed `diffSha256`.
- Factory-control paths red; lockfile yellow; migration red; public contract yellow.
- Unknown coverage adds points (needs the Slice 2 evidence section; stub as "no probes").

## Slice 2: Profile evidence contract and evidence runner

### Schema: `profiles/project-profile.v3.schema.json`

Additive over v2. New `evidence` and `risk` objects; `delivery` becomes per-tier.

```
"evidence": {
  "static":     [{"id","argv","timeoutSeconds"}],            // typecheck, lint
  "tests":      {"argv":[], "patternMode":"append"|"flag", "patternFlag":"--testPathPatterns", "timeoutSeconds"},
  "behavioral": [{
     "id", "kind":"http"|"browser"|"cli"|"script", "covers":["globs"],
     "launch": {"argv":[], "env":{}, "readiness": {"url"|"argv", "expect"}, "portVar":"APIPORT"},
     "probe":  {"url"|"argv", "expect": {"status"|"exitStatus"|"stdoutRegex"|"file"}},
     "negativeControl": {"url"|"argv", "expect"} | null,
     "cleanup": {"argv":[]},
     "timeoutSeconds"
  }],
  "prepare": [{"id","argv","timeoutSeconds"}]                 // replaces gen-api-sync
},
"delivery": {
  "baseBranch",
  "green":  {"publish":"pr",    "autoMerge": true|false, "requestCodeowners": true},
  "yellow": {"publish":"draft"},
  "red":    {"publish":"draft", "mergeGate": true},
  "autoDeploy": false
}
```
Browser probes keep the approved-digest binding: `kind:"browser"` runs
`setup/browser-verifier.py run` with the run's `browser-evidence.sha256`; `check-browser-evidence.py`
becomes the probe's expectation check. `setup/mcp-smoke.sh` becomes a `cli` probe in the
goodword-mcp profile.

### Profiles to write

- `profiles/goodword-api.v3.json`, `profiles/goodword-web-app.v3.json`,
  `profiles/goodword-mcp.v3.json` (facts migrated from `setup/repo-profile.sh` PROFILES and the
  smoke/uat nodes), and bump `profiles/goodword-archon.v2.json` → v3.
- `setup/repo-profile.sh` becomes a thin adapter that reads the v3 profile so bugfix lanes
  keep working unchanged (`--list`, `--json`, eval output preserved).

### Runner: `setup/evidence-run.py`

CLI: `evidence-run.py --artifacts <dir> --profile <p> --tier <t> --worktree <wt>
--touched <files-allowlist.json|diff-list> [--classes static,tests,behavioral]
[--patterns <gate-tests-patterns.txt>] [--port-env]`.
Writes `evidence-manifest.json` (`archon.evidence.v1`):
```
{ schema, tier, revision:{commit, tree}, checks:[{id, class, kind, argv, startedAt, endedAt,
  exitStatus, verdict:"pass"|"fail"|"blocked"|"unavailable", verdictReason, outputSha256,
  artifacts:[], covers:[]}], coverage:{touched:[], covered:[], uncovered:[]},
  negativeControls:[{id, verdict}], summary:{pass, fail, blocked, unavailable} }
```
Typed line: `EVIDENCE=PASS|FAIL|BLOCKED tier=<t> checks=<n> uncovered=<k>`.
Rules: verdict is computed from the observation, not the exit code (a zero-exit run whose
output matches the profile's failure regex is `fail`); port allocation reuses
`setup/port-alloc.sh`; cleanup runs always (trap) and sweeps only this run's ports (keeps
`setup/tests/test_parallel_safety.py` green); unknown coverage is reported, and the tier
decides: green/yellow `flag`, red `block` when `uncovered` is non-empty. Negative control
required for red; a probe without one is `blocked` at red.

### Tests (Slice 2)

- Schema validates the four profiles; v2 profiles still validate against v2.
- Verdict separate from exit: fixture prints "FAIL" and exits 0 → `fail`.
- Unknown coverage: red blocks, yellow flags.
- Negative control missing at red → blocked; present and wrong → fail.
- Cleanup runs on failure; no port literals (parallel-safety test).
- `repo-profile.sh` adapter output byte-identical to today for api/web-app/goodword-mcp.

## Slice 3: `sdlc-red` parent lane

New `workflows/sdlc-red.yaml`, built from `workflows/full-sdlc-api.yaml` with web absorbed.
Keep the reverse-template machine-path convention. Node list, in order (★ new, ✎ changed):

```
preflight ✎        validate profile v3, tools, ports; emit params.json with profile path
feature-phase
kb-recon, kb-recon-gate
bootstrap
stage-skills
risk-intake ★      bash: risk-score.py intake  → risk-intake.json, RISK_TIER line
ralplan ✎          prompt also emits risk-judgment.json (contracts updated)
plan-snapshot
plan-loop (max 3)  plan-round-pre, impact-probe, plan-critic(opus), plan-critic-gate, plan-revise, plan-converge
plan-post-snapshot, premise-strip, premise-verify, premise-gate
docreview, docreview-gate
plan-render ✎      renders risk tier + floors into plan-review.html
plan-render-gate
plan-gate          human approval; message names the tier and floors
risk-plan ★        risk-score.py plan; in red: ledger only (cannot escalate)
implementation-ready
joint-integration  (repository-phase multi-repo only, unchanged)
prepare ★          runs profile evidence.prepare[] (replaces web gen-api-sync)
implement ✎        discipline-agnostic prompt; repo conventions come from staged skills + profile
gate-tests ✎       evidence-run.py --classes static,tests
deslop, deslop-verify (max 2)
reader-audit, reader-audit-gate
commit-impl
risk-impl ★        risk-score.py impl
review-loop (max 4) round-pre ✎ picks responsibilities via review_policy.required_review_plan
                    from risk-impl.json floors/risk areas; review; review-gate; commit-fixes;
                    fixer; commit-fixer; delta-review ★ (bounded-delta scope on fixer diff);
                    delta-review-gate ★; converge ✎ (never converges with unreviewed fixes)
exit-gate ✎        also requires evidence-manifest not yet stale (revision check happens in evidence)
evidence ★         evidence-run.py --classes behavioral (replaces smoke/uat/servers-down/uat-gate)
evidence-bundle ★  writes evidence-bundle.json (archon.evidence-bundle.v1)
merge-gate ★       human approval (red only); message summarizes bundle, P1 waivers here only
local-candidate    (repository-phase)
prbody ✎           renders the bundle
ship ✎             per-tier delivery from profile; requests CODEOWNERS reviewers
run-report, kb-capture, kb-capture-gate
risk-ledger ★      appends final trajectory line; no library write (ingest does that)
report
```

`evidence-bundle.json`: `{schema, intent:{briefSha256}, scope:{allowlistSha256, allowedPaths},
diff:{base, head, files}, static:[ids], behavioral:[ids], regression:{patterns},
risk:{trajectory, floors, codeowners}, review:{rounds, findingsBySeverity, waivers},
provenance:{lane, tier, provider, models, policyVersion, overlayVersion, scoringVersion},
recovery:{revertCommand, notes}}`.

Independence for red: `round-pre` assigns the second model family (the codex reviewer
already used by risk-delta, `gpt-5.6-sol`) to `security` and `data_reliability`
responsibilities when present; on the claude lane this reuses the `review_session.py` seat
machinery, and if the seat is unavailable the checkpoint blocks (no self-review substitution).

Prompt discipline: `implement`, `review`, `fixer` prompts lose api/web vocabulary; commands
come from `evidence-run.py` and the profile. Re-lock `setup/lane-doctrine.lock.json` once
for the family.

Routing wiring for red is in Slice 4; until then `archon-run.py run sdlc-red <spec>` works
by hand (add to `LANES`).

### Tests (Slice 3)

- Node ordering and contracts: extend `test_skills_read_side_contract.py`,
  `test_parallel_safety.py`, `test_review_prompt_contract.py`, `test_node_exit_gate_cross_repo.py`
  to cover `sdlc-red`.
- `test_review_round_pre_cap.py`: responsibilities derived from risk floors; unavailable seat
  blocks.
- `test_lite_converge.py` successor: converge refuses when delta-review is missing.
- Merge-gate present exactly once, before ship; plan-gate message includes tier.
- `test_fullstack_contract.py`: single cross-repo plan gate still holds.

## Slice 4: Derive yellow and green, routing, escalation

### Manifests

Generalize `setup/derive-lite.py`: replace the `api|bugfix` switch with a registry read from
`setup/lite/*.json` (`parent`, `name`, `nodes`, `depends_on`, `loops`, `ports`,
`inherited_dynamic`, `contracts`, `remove_fields`, unchanged shape). Keep `bugfix.json`.
`api.json` is deleted in Slice 6.

- `setup/lite/sdlc-yellow.json` + `setup/lite/sdlc-yellow/`: parent `sdlc-red.yaml`.
  Drops `docreview`, `docreview-gate`, `deslop-verify` (keeps `deslop` + one `deslop-recheck`
  node overlay), `merge-gate`. `plan-loop` max 2, `review-loop` max 2. `risk-plan` and
  `risk-impl` overlays add the escalation branch. Ship overlay: draft.
- `setup/lite/sdlc-green.json` + `setup/lite/sdlc-green/`: drops `plan-loop`, premise nodes,
  `docreview*`, `deslop*`, `reader-audit*`, `plan-gate` (replaced by `plan-seal.node.yaml`:
  mechanical seal that digests plan + allowlist + risk-plan.json), `merge-gate`.
  `review-loop` max 1 (review → fixer → delta-review). Ship overlay: `publish: pr`,
  auto-merge per profile.
- Ports: yellow and green get their own base ports via `setup/port-alloc.sh` strides (as lite
  did with 4125).
- `setup/derive-codex.py` `TARGETS` gains `sdlc-red`, `sdlc-yellow`, `sdlc-green`.
- `setup/package.sh`: lite-drift and codex-drift loops cover the three; payload lists gain
  the new files; `profiles/project-profile.v3.schema.json` added at the schema list (line 51).

### Routing (`setup/archon-run.py`)

- `FEATURE_LANES = {"claude": {"red":"sdlc-red","yellow":"sdlc-yellow","green":"sdlc-green"}, "codex": {...-codex}}`.
- New `static_risk_route(report, profile, base_checkout) -> (tier, receipt)`: runs
  `risk-score.py intake` in a scratch artifacts dir against the frozen base commit (CODEOWNERS
  read via `git cat-file`, matching the KB import idiom), writes
  `risk-routing-receipt.json` via `write_routing_receipt`. `--tier` raises only; lowering
  fails with `risk-override-cannot-lower`.
- `adaptive_feature(args)` mirrors `adaptive_bugfix`: launch tier lane, wait for terminal
  status; on `RISK_ESCALATE` relaunch (below). Chains: `setup/feature_chain.py`
  `DEFAULT_REPOSITORY_LANES` becomes tier-keyed; the chain state records the tier; every repo
  in the chain runs the same tier lane.
- `CODEX_LANES` gains the three codex twins with budgets (green like lite: 90 min / 8M;
  yellow 180 / 20M; red 240 / 30M).

### Escalation handoff

In yellow/green, `risk-plan` and `risk-impl` overlays: if `tier > lane tier`, write
`escalation.json` (`archon.risk-escalation.v1`): `{fromLane, toTier, stage, reasons, floors,
runId, artifactsDir, planSha256, allowlistSha256, branch, head, bootstrapHead,
riskTrajectory}`; print `RISK_ESCALATE=<tier> stage=<stage>`; exit 1 (fail-class terminal so
nothing downstream runs; `trace-digest.py` classifies it as `escalated`, a new terminal
class).

Launcher: `wait_for_escalation(row)` reads the typed line from `node-risk-<stage>.out`
(same authority rule as `wait_for_pre_envelope`: malformed → fail, never infer), then
launches the higher lane with env `ARCHON_RISK_HANDOFF=<escalation.json>`.

Higher lane behavior with a handoff (`preflight` validates digests; `bootstrap` reuses the
branch instead of creating one; `risk-intake` records `escalatedFrom`):
- stage `plan`: `ralplan` runs in "critique-and-carry" mode: input is the sealed plan, output
  is the same plan with a `## Escalation` section; the tier's critic loop and human plan gate
  run on it.
- stage `impl`: as above, plus `implement` runs in "reconcile" mode: the branch already has
  commits; it must apply critic findings and may not revert unrelated work. All lower-tier
  evidence is discarded; the higher lane re-runs `gate-tests`, review, and `evidence` at its
  own revision. The ledger links both runs (`escalationRunId`).

### Tests (Slice 4)

- `test_derive_lite.py`: three manifests round-trip and `--check`; dangling deps; contracts.
- `test_derive_codex.py`: new targets.
- `test_risk_route.py`: tier selection from fixtures; override raises only; receipt written;
  malformed intake → fail.
- `test_risk_escalation.py`: handoff digest mismatch refused; lower evidence not in the
  higher lane's manifest; ledger links; `trace-digest` `escalated` terminal.
- Synthetic engine-free runs (recipe in memory `archon-skill-evolve-synthetic-test`): one
  green end-to-end, one yellow→red at `impl`.

## Slice 5: Calibration loop

- `setup/risk-calibrate.py <repo> [--min-runs 12] [--window <n>]`: reads
  `library/<repo>/raw/index.jsonl` digests' `risk` blocks. Computes per signal: intake
  under-score rate (intake < final), escalation stage histogram, paths that triggered ≥2
  impl-stage escalations, "quiet red" rate (red with zero P0–P2 and no floor), green outcome
  (reverts, post-merge failures when `delivery.prUrl` outcomes are ingested). Writes
  `risk-calibration.json` and, when bounds allow, `risk-policy-proposal.json`
  (`archon.risk-proposal.v1`): `{edits:[{op:"threshold"|"protect"|"weight"|"revokeAutoMerge",
  ...}], evidence:{runIds, stats}, bounds:{maxThresholdDelta:10, maxNewProtected:3,
  maxWeightDelta:5}}`.
- Hard invariants (checked by `risk_policy.assert_invariants` at proposal, admit, and load):
  no edit lowers a `sensitiveDomains` or `factoryControl` floor; no edit sets
  `autoMerge:true`; overlay versions are monotonic and retained.
- `workflows/skill-evolve.yaml` gains a branch after `score-run`: `risk-calibrate` (bash) →
  `risk-proposal-gate` (bash: bounds, min samples, invariants) → `risk-critic` (agent, same
  shape as `skill-critic`) → `risk-approval` (human approval node; learning proposes, never
  self-approves) → `risk-admit` (bash: writes `library/<repo>/risk/policy-overlay.json`
  and `policy-overlay-history/<version>.json`, commits as `archon skill-evolve`). Runs only
  when `risk-calibrate` produced a proposal; otherwise `RISK_CALIBRATE=NO_ACTION`.
- `setup/risk-score.py --overlay` defaults to `library/<repo>/risk/policy-overlay.json`
  when present. `setup/skill-admit.py rollback` gains `--risk` to restore the previous
  overlay version.
- `library/README.md`: add the `risk/` state with its rule (mechanical writer only, never
  lowers floors, history retained).

### Tests (Slice 5)

- Proposal bounds; invariants reject floor-lowering and auto-merge enabling; min samples;
  overlay precedence (overlay over profile over defaults); rollback restores prior version;
  under-score rate on fixtures; no proposal when nothing exceeds bounds.

## Slice 6: Parity and retirement

- `setup/tests/test_lane_parity.py`: for the Goodword api and web profiles, compare
  `sdlc-red` against `full-sdlc-api`/`full-sdlc-web` on: node set minus known renames,
  produces/consumes artifacts, locked doctrine lines, and evidence coverage (the old smoke
  and uat checks exist as probes in the v3 profiles).
- One real run of a small spec through `sdlc-red` and one through `sdlc-green` on the
  Goodword api repo before deletion (record run ids in RUNBOOK).
- Delete: `workflows/full-sdlc-api.yaml`, `full-sdlc-web.yaml`, `full-sdlc-api-lite.yaml`,
  their `-codex` twins, `setup/lite/api.json`, `setup/lite/api/`, `setup/lite-envelope.json`,
  `setup/lite-envelope.sh` (bugfix reads `repro_command_allow` from `risk-policy.json`; keep
  the envelope size checks for bugfix by moving them into `risk_policy.py`), the web-specific
  `gen-api-sync` logic, `templates/repo-conventions-api.md` and `-web.md` (content moves to
  staged skills or profile notes).
- Update: `RUNBOOK.md` §2a/§3/§14/§15 and the feature launcher section (tier table, green
  auto-ship policy, escalation, calibration), `docs/` new `risk-tiers.md`,
  `setup/package.sh` payload lists, `setup/feature_chain.py` lane maps, `setup/archon-run.py`
  `LANES`, all tests that name the old lanes.

## Verification (end-to-end)

1. `bash setup/package.sh` drift gates: `LITE_DRIFT=OK` for `sdlc-yellow`, `sdlc-green`,
   `bugfix-lite`; `CODEX_DRIFT=OK` for all twins; `LANE_DOCTRINE=OK` after re-lock.
2. Unit suite: `uv run --offline --no-project --python 3.13.9 --with pyyaml==6.0.3 python -m
   unittest discover -s setup/tests` (compare against the ~144 known environmental failures,
   per module against a clean worktree).
3. Scorer fixtures: a docs-only spec → green; a spec touching `auth/` → red with floor
   `sensitive-domain:auth`; the same with a CODEOWNERS mapping `@org/security` → red with
   `ownersTouched`; a lockfile bump → yellow.
4. Synthetic runs: green completes to a PR with `evidence-bundle.json`; yellow escalates to
   red at `impl` with a linked ledger pair; red blocks on uncovered paths.
5. Real run on Goodword api: `archon-run.py feature --tier green <small-spec>` opens a
   non-draft PR with CODEOWNERS reviewers and auto-merge enabled only if the repo has required
   checks; verify the refusal path on a branch without protection.
6. Calibration: seed the ledger with the synthetic runs plus backfilled digests from existing
   `artifacts/runs`, run `risk-calibrate.py api`, confirm a bounded proposal and that a
   floor-lowering edit is rejected.
