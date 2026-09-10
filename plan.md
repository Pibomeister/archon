**DELIVERABLE: a build-ready implementation spec** for a change to this repository's Archon
harness. Hold it to a spec's bar: every step names its files, its acceptance criteria and how it is
verified.

**Revision pin.** All `file:line` citations are against the **working tree** of
`/Users/eduardopicazo/Documents/Workspace/Goodword/.archon`, HEAD
`457017e3eaa803892c3f907feef3536b4a841386` on `main` plus 72 uncommitted changes. Read the checkout
as it stands, not `git show HEAD:<path>`. The sibling repository
`/Users/eduardopicazo/Documents/Workspace/Goodword/goodword-mcp` is pinned at
`a2e63d9f8f00db188a3142ca4ea727aba806db9d`.

**Revision 7** — closes the Critic's final MINOR. Revision 6 (0 CRITICAL / 5 MAJOR / 2 MINOR, all accepted).
Revision 2 answered the Architect but introduced an ordering bug that breaks ordinary api preflight,
omitted a live `web-app` caller, and specified a test command whose quoting silently defeats its own
exclusion. Three of the seven were confirmed by execution. What changed is in §9.

# Archon: make the existing `full-sdlc-api` lane repo-aware, then run ENG-3866 on goodword-mcp

## 0. Constraint

**No new archon workflow files.** A prior plan derived a sixth lane; it is withdrawn. `goodword-mcp`
must run through the existing `full-sdlc-api` lane. "Add a separate mcp lane" is not available.

## 1. What is measured, and what was falsified

Phase 1 is complete and green. `goodword-mcp` was fast-forwarded to `origin/main` (`a2e63d9`);
inventory is 49 `src/**/*.ts`, 28 tools, 27 unit test files. In an archon-shaped worktree
(`goodword-mcp/.worktrees/phase1-probe`, branch `archon/phase1-probe`):
`pnpm install --frozen-lockfile` → 4s; `pnpm exec tsc --noEmit` → clean; `pnpm test:unit` → **27
suites, 168 tests, all passed**. Node 20 via `mise` (system default is 25), the same wrapper idiom
`full-sdlc-web.yaml` already uses.

**Baseline of every drift gate, recorded before any edit:** `LANE_DOCTRINE=OK nodes=22 lines=606`,
`LITE_DRIFT=OK` for `full-sdlc-api-lite` and `bugfix-lite`, `CODEX_DRIFT=OK` across all eight codex
twins. Any gate that is red later is red because of this change.

Corrections to the original blocker list:

| Original blocker | Status |
|---|---|
| B1 `resolve-params.sh:49` hardcodes `$root/api/.worktrees/$slug` | **Real.** |
| B2 `repo-policy.py` two-repo map / test vocabulary | **Falsified.** `repo-policy.py` has **0 call sites** in `full-sdlc-api.yaml`; only the four bugfix lanes reference it. Out of scope. |
| B3 api-shaped commands | **Real**, and larger than "commands" — see §2. |
| B4 six registries · B5 `test_archon_run.py:441-444` | **Moot.** No new lane, so `FEATURE_LANES` gains no key. |
| B6 GitNexus api-only | **Real and worse than stated** — see finding F7 in §2. |

Two mechanical facts that shape the design:

- `lane-doctrine.py` walks only `prompt:` blocks (`lane-doctrine.py:57`) and `check` fails only on
  lines the lock *has* that a lane has *dropped* (`lane-doctrine.py:74-88`). Added lines are
  invisible to it. So prompt additions need no re-lock — **but** the Architect is right that this
  also means doctrine cannot detect a semantic contradiction introduced by an addition (F5).
- The lock already carries api **and** web command vocabularies inside single prompts (`fix` locks
  `bun run typecheck` and `pnpm lint`; `rca` locks both). A third repo's shapes alongside them is the
  established idiom.

## 2. The eight findings this revision answers

Each was verified against the code; four were verified by running something.

**F1 — `.env` and smoke-port relaxations would change api failure behaviour.** `full-sdlc-api.yaml:148`
copies `$API/.env` unconditionally and fails the run if it cannot. `:61` rejects an empty `APIPORT`.
Making either *conditional on the value* silently weakens the api lane. **Both must be keyed on the
repo profile, never on whether a file or port happens to exist.**

**F2 — repo selection and rebinding were unspecified.** Positional args 4 and 5 of
`resolve-params.sh` already mean port bases and `bugfix.yaml:75` passes two of them, so the repo
cannot be another positional. Worse: `bind-repo.py:18-21` rewrites `repo` and `worktree` after the
bugfix RCA gate — **a resolved `commands` block written at params time would not be rewritten with
it**, leaving api commands on a web-bound bugfix run. This finding kills the original design.

**F3 — an empty web scope does not survive the planning gate.** `plan-shape.sh:29-31` asserts
`isinstance(required, list) and required` on `browser-evidence.json`, with each entry needing a
`path` starting with `/` and a non-empty `assertions` list drawn from a web vocabulary
(`text|selector|url|title|testid|click|fill`). An MCP tool change has no browser surface, so an
honest empty policy **hard-fails at plan-snapshot, before implement**. A prompt sentence cannot move
a mechanical gate. Scope of the fix is narrow and was measured: in the api lane `browser-evidence.json`
is only *authored* (`:247-257`) and *frozen* (`:333`, `:602`, `:684`); `check-browser-evidence.py` is
**not invoked by this lane**.

**F4 — the proposed mcp test command was broken. Confirmed by execution.**
`mise x node@20 -- pnpm exec jest --testPathIgnorePatterns … --testPathPatterns delete-group` →
`SyntaxError: Cannot use import statement outside a module`, **1 suite failed, 0 tests run**.
`jest.config.mjs:3` is `ts-jest/presets/default-esm` and `package.json:13` supplies
`NODE_OPTIONS='--experimental-vm-modules'` via `cross-env`. The Architect's sharpest point stands:
that command would have *passed* a "make a test fail, confirm the gate fails" control while failing
for entirely the wrong reason. With the env preserved, the same selector runs **11 tests, passed**.

Separately measured: `--testPathPatterns tools` selects **only** `tests/tools.e2e.test.ts`, an e2e
suite needing a live api and a token. The unit restriction is mandatory, not cosmetic.

**F5 — additive prompts leave contradictory instructions.** `full-sdlc-api.yaml:191` requires
"exactly three commands" under `## Verification`; mcp has no lint. `:1009` and `:1031` scope
implementation to "only API files" and "do not touch web-app". Declaring params authoritative does
not repeal those sentences.

**F6 — lite overlays replace whole fields and would not inherit this.**
`setup/lite/api/preflight.bash.sh:42` carries its own copy of the api hardcodes,
`ralplan.prompt.md` its own planning instructions, `post-fix-gate.node.yaml:19` its own literal bun
check. Regeneration reproduces those unchanged.

**F7 — the impact node would emit confidently wrong evidence, not a skip.**
`full-sdlc-api.yaml:361-370` self-gates only on "no symbols modified" (`SKIPPED`) and "tools
unavailable" (`UNAVAILABLE`); otherwise it queries the index `"mono"`, whose own comment says *"it
covers api/ only"*. An mcp run with gitnexus available and symbols modified would query **api's
index** and record the result as this run's impact. That is worse than the `GITNEXUS=SKIP`
degradation the earlier plan claimed, and the discriminator is `IMPACT=`, not `GITNEXUS=`.

**F8 — artifact identity would lie.** `:1065` and `:1601` hardcode `"repo":"api"` into
`feature-result.json`; `:151` writes `api_worktree`. Measured consumers: `archon-run.py:2054`
(`worktrees.get("api_worktree") or params.get("worktree")`) and `resolve-web-params.sh:75`, which
only a web handoff reaches. **So the `api_worktree` key stays for compatibility and `repo` is added
alongside it** — renaming would break a consumer for no gain.

## 3. Design: a closed repo profile, resolved before params.json exists

`params.json` stores **only `repo`**. Everything else is derived from one closed table, so rebinding
`repo` (`bind-repo.py`) corrects the derived values by construction and a legacy `params.json` with no
`repo` resolves to `api`.

**The table lives in a new `setup/repo-profile.sh <repo>`, not in `params-env.sh`.** It takes a repo
*name* and needs no params file, so `resolve-params.sh` can consult it while deciding whether to
allocate a port, and `params-env.sh` delegates to it after reading `repo` from `params.json`. One
definition, two entry points.

**There is exactly one selection point, and it is `resolve-params.sh`.** Revision 3 had preflight
compute `${ARCHON_REPO:-api}` at line 53 and the resolver adopt an existing binding at line 59 — so a
resumed mcp run checked `api/.git` and then selected `goodword-mcp` (Architect R1). Revision 4 deletes
the early resolution outright: `resolve-params.sh` runs first, `params-env.sh` exports `REPO` and the
profile, and **every** repo-dependent check follows. Nothing reads `$REPO` before it is resolved once.

**Helper failure must be caught two ways, because one is not enough.** `eval "$(cmd)"` swallows a
non-zero exit — confirmed: `set -euo pipefail; eval "$(false)"` continues (Architect R4). Revision 4
proposed only the `params-env.sh:9` emit idiom, but that covers a *deliberate* error branch and still
misses a plain non-zero exit with empty stdout (Critic round 2). Both are required, and the
composition was measured:

| mechanism | helper exits 3, empty stdout |
|---|---|
| `eval "$(repo-profile.sh …)"` | **continues — swallowed** |
| `OUT=$(repo-profile.sh …) \|\| { echo FAIL; exit 1; }; eval "$OUT"` | **stops** |
| `params-env.sh` capture-and-check, re-emitting an eval-able failure to its own `eval "$(…)"` caller | **stops at the call site, exit 1** |

So: every `repo-profile.sh` call site uses **capture-and-check**, and `repo-profile.sh` *also* emits
`echo 'REPO_PROFILE=FAIL <reason>'; exit 1` for its known error branches. `params-env.sh` forwards the
helper's stdout verbatim on success and re-emits an eval-able failure on any non-zero exit, so its
existing `eval "$(params-env.sh …)"` callers stop without changing their idiom. That answers the
Critic's delegation question: `params-env.sh` captures, it does not blind-eval.

| | `api` | `goodword-mcp` | `web-app` (compatibility) |
|---|---|---|---|
| worktree | `$root/api/.worktrees/$slug` | `$root/goodword-mcp/.worktrees/$slug` | `$root/web-app/.worktrees/$slug` |
| install | `bun install --frozen-lockfile` | `mise x node@20 -- pnpm install --frozen-lockfile` | `mise x node@20 -- pnpm install --no-frozen-lockfile` |
| typecheck | `bun run typecheck` | `mise x node@20 -- pnpm exec tsc --noEmit` | `mise x node@20 -- pnpm typecheck` |
| lint | `bun run lint` | *not applicable* | `mise x node@20 -- pnpm lint` |
| test (pattern appended) | `bun run test --` | `mise x node@20 -- env NODE_OPTIONS=--experimental-vm-modules pnpm exec jest --testPathIgnorePatterns <regex> --testPathPatterns` | `mise x node@20 -- pnpm test --run` |
| env source | `$root/api/.env` — **required** | *none* | *none* |
| smoke | yes | *none* | yes |
| browser surface | yes | **no** | yes |
| impact index | `mono` | *none* | *none* |

`web-app` is **not** new scope — it is a live caller. `bind-repo.py:16` accepts it and
`bugfix.yaml:2950` runs `params-env.sh` on the params file it rewrote. A two-entry table that hard-fails
on unknown repos would break every web-app bugfix run (Critic C2). Its row exists so that path keeps
working; the web lane itself still uses `resolve-web-params.sh` and consumes none of these values.

### 3.1 Commands are arrays, not strings — and this is why

Storing a command as a string and expanding it unquoted puts **literal apostrophes** into argv.
Measured under `bash` (which is what a node runs; zsh does not word-split, so a zsh probe is not
representative):

```
CMD_TEST="... --testPathIgnorePatterns '\.(e2e|smoke)\.test\.ts$' --testPathPatterns"
$CMD_TEST "tools"   ->  argv: [--testPathIgnorePatterns] ['\.(e2e|smoke)\.test\.ts$'] ...
                    ->  regex matches nothing, and tests/tools.e2e.test.ts IS SELECTED
```

That suite calls a live API at `goodword-mcp/tests/tools.e2e.test.ts:11`. The exclusion would have
failed **silently**, which is worse than failing loudly.

`repo-profile.sh` therefore emits `eval`-able **bash array declarations**, built with
`shlex.quote`, and every call site uses `"${CMD_TEST[@]}" "$PAT"`. Verified with both controls:

```
"${CMD_TEST[@]}" "tools"        -> selects NOTHING          (exclusion works)
"${CMD_TEST[@]}" "delete-group" -> tests/delete-group.unit.test.ts   (positive control)
```

### 3.2 Selection, validation, and resume

A lane declares what it may newly select by passing a trailing `--allow <csv>` to
`resolve-params.sh` (default `api`); a non-member is `PARAMS=FAIL repo <x> not supported by this lane`.
`--allow` does **not** apply in every case — the numbered sequence below is the authoritative and only
statement of the rule, and this paragraph deliberately does not paraphrase it.

**Repo selection is one decision sequence, stated once.** Revisions 3 and 4 described it in two places
and the descriptions contradicted each other (Critic round 2). This is the only statement of it, and
§4.2 defers to it rather than restating:

1. If `params.json` exists and carries a `repo`, that is the **existing binding**.
2. If `ARCHON_REPO` is set and differs from it → typed stop. Never a silent rebind.
3. Existing binding present and `ARCHON_REPO` is **unset or equal to it** → **adopt it, skipping the
   `--allow` check**. It was already validated by whoever wrote it (`resolve-params.sh` on the first
   pass, or `bind-repo.py`, which carries its own enum). The "or equal" clause is not cosmetic:
   without it, binding `web-app` plus an explicit `ARCHON_REPO=web-app` passes step 2 (no conflict),
   fails step 3 (env is set), and is then rejected by step 4's default `--allow api` — so naming the
   repo you are already bound to would break the run while saying nothing would not (Architect
   round 3).
4. **No existing binding.** The repo is `${ARCHON_REPO:-api}`, and this is the only case `--allow`
   gates.

Step 3 is load-bearing: `bugfix.yaml:75` passes no `--allow`, and after `bind-repo.py:19` its params
carry `repo:"web-app"`. Applying `--allow` to an adopted binding rejects a resumed web-bound bugfix
run against the default `api` (Architect R2).

**Adoption is new behaviour, not preservation.** Today `resolve-params.sh:42-55` builds a fresh params
object, drops any `repo`, forces `{root}/api/.worktrees/{slug}`, and opens the file write-only — it
never reads an existing binding. The Critic executed that body against a web-bound params object and
confirmed all three. So every resume case belongs in §5b, not §5a.

**Port bases stay positional literals in the lane YAML.** `derive-lite.py:128` substitutes ports by
plain string match (`lite/api.json` carries `{"4123": "4125"}`), so moving `4123` out of the lane
would make the lite twin collide with the full lane on the same port. The lane keeps passing its base
positionally exactly as today; the *profile* decides whether a port is allocated at all.

This also makes the lite lane fail-closed (Architect F6): `setup/lite/api/preflight.bash.sh` passes no
`--allow`, so `ARCHON_REPO=goodword-mcp` against `full-sdlc-api-lite` is a typed stop, and its overlays
need no edit.

## 4. Changes, file by file

### 4.1 `setup/repo-profile.sh` (new)
`repo-profile.sh <repo>` prints `eval`-able assignments and **array declarations** for the §3 table:
`REPO`, `CMD_INSTALL[]`, `CMD_TYPECHECK[]`, `CMD_LINT[]`, `CMD_TEST[]`, `ENV_SRC`, `HAS_SMOKE`,
`HAS_BROWSER`, `IMPACT_INDEX`. All are always defined (empty arrays / empty strings where not
applicable) so `set -u` consumers can test them. An unknown repo emits an **eval-able failure** —
`echo 'REPO_PROFILE=FAIL unknown repo <x>'; exit 1` on stdout, the `params-env.sh:9` idiom — because a
plain non-zero exit is swallowed by `eval` (§3, Architect R4).

**It must be added to `setup/package.sh`'s manifest** (`package.sh:73` region), which is an explicit
list, not a glob. `params-env.sh` now depends on it, so a packaged install that omits it breaks the
**api** lane, not just mcp. `setup/tests/test_setup_scripts_are_packaged.py:44` already tests this
class and will catch it (Architect R3).

### 4.2 `setup/resolve-params.sh`
Parse trailing `--allow <csv>` (default `api`). Implement §3.2's four-step sequence exactly — it is
the only statement of the rule; do not re-derive it here. Read the existing `params.json` before
overwriting it (today the file is opened write-only and never read). Write `"repo"` and derive
`"worktree"` from it. Positional args 1-5 keep
their present meaning; the port base is allocated only when the profile declares a smoke stack, so
`bugfix.yaml:75`'s two-base call and every other existing caller is untouched.

### 4.3 `setup/params-env.sh`
Read `repo` from `params.json` (absent ⇒ `api`), delegate to `repo-profile.sh`, and keep emitting
today's `SPEC`/`SLUG`/`BR`/`WT`/`APIPORT`/`WEBPORT` unchanged. Existing consumers see no difference.

### 4.4 `workflows/full-sdlc-api.yaml` — bash blocks (outside doctrine)

Ordering is the load-bearing part (Critic C1): the node sets `set -euo pipefail` at `:11`, so **every**
use of `$REPO` must follow its resolution.

| Line | Today | Becomes |
|---|---|---|
| 53 | `test -d <root>/api/.git` | **moves to after :60** and becomes `test -d "<root>/$REPO/.git"`. Revision 3 resolved `$REPO` here from the environment while the resolver adopted a different one below — a resumed mcp run would check `api/.git` (Architect R1). There is now no early resolution at all. |
| 58-59 | `resolve-params.sh … 4123` | `… 4123 --allow api,goodword-mcp` — the `4123` literal **stays** for `derive-lite`'s port map. This is the single selection point. |
| 61-62 | `test -n "$APIPORT"` unconditional | required **iff** `$HAS_SMOKE`; else `PREFLIGHT_PORTS none (<repo> declares no smoke stack)`. Keyed on the profile, so an allocation *failure* for api still fails. |
| 138 | `API="<root>/api"` | `REPO_DIR="<root>/$REPO"`, placed **after** the `params-env.sh` eval at :139 |
| 140, 145, 148, 149 | `$API` references | `$REPO_DIR` — all four sites, not just the two revision 2 listed |
| 148 | `cp "$API/.env" "$WT/.env"` | copy **required** when `$ENV_SRC` is set (api unchanged, still fails if absent); skipped only when the profile declares no env source |
| 149 | `bun install --frozen-lockfile` | `"${CMD_INSTALL[@]}"` |
| 151 | `{"api_worktree":…,"branch":…}` | same keys **plus** `"repo"` — `api_worktree` is read by `archon-run.py:2054` and `resolve-web-params.sh:75`, so it stays |
| 1045-1046 | `bun run typecheck` / `bun run lint` | `"${CMD_TYPECHECK[@]}"`; lint runs iff `${#CMD_LINT[@]}` non-zero, else records not-applicable in `lint.txt` **and** in `recheck.json` |
| 1060, 1212, 1948 | `bun run test -- "$PAT"` | `"${CMD_TEST[@]}" "$PAT"` |
| **1065, 1069, 1601** | `"repo":"api"` literal | `"$REPO"` — **three** writers; revision 2 missed the `CHANGED` branch at `:1069` |
| 1200-1201 | `TC=`/`LT=` exit capture | shape unchanged; a skipped lint is recorded in `recheck.json` as `"lint_applicable": false` **alongside** the doctrine-locked integer, which stays. `:1292` already reads that file; `:2009` must be taught to (§4.6). A marker file no consumer reads is not evidence — revision 3's `lint.txt` marker alone was exactly that. |
| 1952-1999 (`smoke`) | boots `bun start` on `$APIPORT` | when `$HAS_SMOKE` is empty: write `SMOKE=NOT_APPLICABLE (<repo> declares no boot smoke)`, exit 0. Profile-keyed, never port-keyed. |

`prbody` (`:2027`) reads `smoke-result.txt` verbatim; the marker satisfies it with no change.

### 4.5 Mechanical gates that prompt text cannot move

- **`setup/plan-shape.sh:29-31`** — accept a typed `{"not_applicable": "<reason>", "required": []}`
  browser disposition **only** for a repo with no browser surface; nonempty stays mandatory otherwise.
  The `.sha256` still covers the document, so the disposition is frozen at approval.
  `check-browser-evidence.py` is untouched — this lane never calls it and the web contract must not
  loosen.
- **`workflows/full-sdlc-api.yaml:361-370`** — add self-gating rule 0 **ahead** of the existing two:
  read `repo` from `params.json`; if the profile declares no impact index, write `impact.json` with
  `"status":"SKIPPED"` and a recorded reason. It must precede the tool-availability check, or an mcp
  run with gitnexus present queries api's `mono` index and records the answer as its own. This node is
  a *prompt*, so it obtains the repo by reading `params.json` — it cannot see another node's shell
  exports (Critic open question).

### 4.6 Prompts — every consumer, not just the planning ones (Critic C4)

Revision 2 scoped three sites and missed two. The complete set:

| Line | What it asserts today | Override added |
|---|---|---|
| 191 | `## Verification` lists "exactly three commands" | command count follows the profile — three for api/web-app, two where lint is not applicable |
| 247-257 | browser policy authoring — implies a populated policy | authoring instruction for the typed non-applicable disposition, matching the validator in §4.5 |
| 1009, 1031 | "only API files", "do not touch web-app", and the three-command completion sentence | scope applies when `repo` is `api`; for a single-repo profile the allowlist is the whole scope, `web-files-allowlist.json` is `[]`, and the command count follows the profile |
| **167** | planner demands "ONE SHARED CROSS-REPOSITORY implementation plan", API-relative and web-relative paths | for a single-repo profile the plan is single-repo, paths are relative to that repo, and `web-files-allowlist.json` is `[]` |
| **1148-1152, 1165** | deslop prompt requires `bun run typecheck`, `bun run lint`, `bun run test -- "<pattern>"` to exit zero, and represents lint as an integer exit code | same profile-driven override; `lint` reported as not-applicable rather than as a fabricated zero |
| **1292** | the deslop reviewer reads `recheck.json` as the authoritative all-zero record | lint applicability carried **inside `recheck.json`** as an additive `"lint_applicable": false`, not in a marker file no consumer reads (Architect R5) |
| **2009** | prbody reads `round.txt`, `round-<N>/review-summary.json`, `round-<N>/fixer-result.json`, `reader-audit-result.json`, `accept-residuals.txt` — and **not** `recheck.json` (verified) | add an explicit read of `deslop-round.txt` and `deslop-round-<N>/recheck.json`, so the PR body can report lint as not-applicable instead of silently implying it passed (Critic round 2) |

Every edit is additive, so `lane-doctrine.py check` passes unforced. Doctrine cannot detect a
*semantic* contradiction introduced by an addition — which is exactly why this table enumerates
consumers instead of relying on the gate.

## 5. Acceptance — preservation tests and mutation tests are different things

Revision 2 applied one blanket rule ("revert it, confirm the test fails") to all seventeen cases. That
is unsatisfiable for preservation cases: a test asserting api behaviour is *unchanged* must **pass**
against both implementations (Critic C5). The two kinds are now separated.

Port allocation is mocked throughout — `port-alloc.sh:13` returns a different port when a slot is
occupied, so unmocked comparison is not deterministic.

### 5a. Baseline-preservation — must PASS before and after. No negative control applies.

Every row here asserts that something the api (or bugfix) lane does today is **unchanged**. A
preservation test that fails against the old implementation is misfiled, not rigorous.

| # | Case | Observable |
|---|---|---|
| P1 | api `params.json` byte-identical to today's but for `repo:"api"` | file diff |
| P2 | every resolved api command array equals the literal it replaces | argv comparison at all eight sites of §4.4 |
| P3 | api preflight succeeds from a clean environment with `ARCHON_REPO` unset, **and checks `api/.git`** | exit 0, `PREFLIGHT=PASS`, and an assertion on *which* repo directory was checked — the direct control for the R1 ordering defect |
| P4 | api bootstrap succeeds on both create and adopt paths | exit 0, worktree on `$BR` |
| P5 | missing api `.env` still fails bootstrap | non-zero, same message as today |
| P6 | api port-allocation failure still fails preflight, and does **not** become a smoke skip | non-zero at preflight |
| P7 | `bugfix.yaml`'s two-positional-base call still parses | exit 0 |
| P8a | `params-env.sh` on a `bind-repo`-rewritten `web-app` params file still emits today's `SPEC`/`SLUG`/`BR`/`WT`/`APIPORT`/`WEBPORT` unchanged | argv/value comparison against the current implementation |
| P9 | legacy `params.json` with no `repo` resolves to `api` | resolved values equal P2's |
| P10 | api smoke still boots and probes | **`SMOKE=PASS` with the expected `/api-docs-json` and `/info` probes** — not merely "non-`NOT_APPLICABLE`", which would admit `SMOKE=FAIL` |
| P11 | `plan-shape.sh` still rejects an empty browser policy for api and web-app | non-zero |

### 5b. New behaviour — acceptance cases

These assert something that does not exist today, so "revert it and watch it fail" is trivially true
and proves nothing. Each states its observable; where a mutation control is meaningful it is in §5c.

| # | Case | Observable |
|---|---|---|
| A1 | derived `web-app` command arrays are correct | argv comparison against `full-sdlc-web.yaml`'s literals |
| A2 | mcp `CMD_TEST` excludes **e2e** | selector `tools` → zero test files; `tools.e2e.test.ts` absent from `--listTests`. Positive control: `delete-group` still selected. Both measured. |
| A2b | mcp `CMD_TEST` excludes **smoke** | selector `search-timeout` → zero test files. Separately asserted because `tools` targets only an e2e file: **measured** that dropping `smoke` from the regex still passes A2 and its control, while leaking `tests/search-timeout.smoke.test.ts` (Critic round 2). |
| A8 | a resumed **web-bound bugfix** preflight adopts its binding | no `--allow`, `ARCHON_REPO` unset, existing `repo:"web-app"` → exit 0, `repo` still `web-app`, web worktree retained. Was P12; adoption does not exist today, so it is not preservation. |
| A8b | an explicit `ARCHON_REPO` **equal** to the existing binding is adopted, not rejected | `ARCHON_REPO=web-app` with binding `web-app` and no `--allow` → exit 0. Without the "or equal" clause this fails while the unset case succeeds (Architect round 3). |
| A9 | a resumed **mcp** preflight is repo-correct throughout | existing `repo:"goodword-mcp"`, `ARCHON_REPO` unset → `goodword-mcp/.git` is the directory checked, mcp worktree retained, **no** smoke port allocated. P3 covers fresh api selection and does not exercise the resume divergence that motivated R1. |
| A3 | lint/smoke report honestly to their consumers | `recheck.json` carries lint-not-applicable (read at `:1292`, `:2009`); `smoke-result.txt` contains `SMOKE=NOT_APPLICABLE` |
| A4 | `plan-shape.sh` accepts the mcp disposition | exit 0, while P11 still rejects api/web empty |
| A5 | impact node skips for mcp **even with gitnexus available** | `impact.json` `status=SKIPPED` with reason, plus a tool-call trace showing **no** gitnexus call |
| A6 | artifact identity is truthful | `feature-result.json` `"repo":"goodword-mcp"` on **both** `NO_CHANGE` and `CHANGED` branches; `worktrees.json` keeps `api_worktree`, adds `repo` |
| A7 | mcp `CMD_TEST` runs the real suite | 11 tests pass on `delete-group` (measured) |

### 5c. Mutation cases — named mutation, matched anchor, expected exit, passing control

The passing control is not optional: an environment-broken command fails a negative control for the
wrong reason, which is precisely how revision 2's test command survived a review pass.

| # | Mutation | Anchor | Expected | Control |
|---|---|---|---|---|
| M1 | deliberate failing assertion in an mcp unit test | that spec file | non-zero, report names that test | A7's passing run |
| M2 | zero-match selector | `CMD_TEST` invocation | **non-zero gate exit with zero executed tests** — the observable revision 2 left undefined | A7 |
| M3 | deliberate type error in mcp source | `CMD_TYPECHECK` | non-zero | clean `tsc --noEmit` |
| M4 | `ARCHON_REPO=goodword-mcp` against `full-sdlc-api-lite` | lite preflight | typed `PARAMS=FAIL` | lite with api still runs |
| M5 | `ARCHON_REPO=nonsense` | `resolve-params.sh` | typed fail, never a silent default | P3 |
| M6 | `ARCHON_REPO` conflicting with an existing binding | `resolve-params.sh` step 2 | typed stop, never a silent rebind | A8 |
| M7a | `repo-profile.sh` given an unknown repo (emits the eval-able failure) | its capture-and-check call site inside `resolve-params.sh`, **and** the delegated path through `params-env.sh` | run stops at both | P3 |
| M7b | `repo-profile.sh` made to exit non-zero with **empty stdout** | same two call sites | run stops at both — the branch the emit idiom alone cannot catch | P3 |
| M8 | `repo-profile.sh` removed from `package.sh`'s manifest | packaging test | `test_setup_scripts_are_packaged.py` fails | full suite green with it listed |

`setup/tests/test_node_stress.py:740` and `:956` already cover api gate success, typecheck failure,
deslop success and lint failure. They are the existing floor, not a substitute for P3-P4, P10 or M2.

## 6. Regeneration and gates

`derive-lite.py api` · `derive-codex.py --all` · then all four `--check` gates plus the full
`setup/tests` suite. A `--check` made green by hand-editing generated YAML is a failure dressed as a
pass. Expected: `LANE_DOCTRINE=OK` unforced with no re-lock, since every prompt edit is additive.

## 7. ENG-3866

Intake snapshot is written to `.omc/research/linear/ENG-3866.md`. The API surface it must reuse
already exists in `goodword-mcp/src/api-client.d.ts`: `POST /group/share` (:980), `GET
/group/share/{sharedLinkId}` (:1012), `ShareGroupPayloadDto` (:3948). The direct sibling is ENG-3865
(`delete-group.ts`, PR #27), the same mutating-group-tool shape — so a lane defect is distinguishable
from a ticket defect. Record per stop: the typed discriminator, whether it fired correctly, and what
would have made it cheaper to reach.

## 8. Out of scope, and the evidence we are knowingly not getting

- Any new workflow file, lite manifest, or overlay directory.
- `repo-policy.py`, `bind-repo.py`'s repo enum, `rca-shape.sh`, `bugfix-contract.py`,
  `experiment-runner.py` — bugfix surface, unreached by this lane.
- `check-browser-evidence.py` and the web lane's browser contract stay exactly as strict.
- **No mcp boot smoke.** Accepted, and it is a real gap: `tsc --noEmit` and unit tests do not verify
  httpStream startup or transport. This is not equivalent to the api lane's smoke evidence and must
  not be reported as though it were.
- No `-codex` twin run; no mcp e2e suite (needs a live api and a token).

## 9. What changed in revision 4

| Architect round-2 finding | Change |
|---|---|
| R1 High — preflight resolved `${ARCHON_REPO:-api}` at `:53` while the resolver adopted a different binding at `:59`; a resumed mcp run would check `api/.git` | early resolution **deleted**. `resolve-params.sh` is the single selection point; every repo-dependent check moves after it. P3 now asserts *which* directory was checked. |
| R2 High — `--allow` default `api` would reject a resumed web-bound bugfix run (`bugfix.yaml:75` passes no `--allow`; `bind-repo.py:19` writes `web-app`) | `--allow` gates new *selection*, not resumption. A conflicting `ARCHON_REPO` is a typed stop. New case tests the composition — filed as P12 in revision 4, moved to A8 in revision 5 once the Critic showed adoption is new behaviour, not preservation. |
| R3 High — `package.sh:73` is an explicit manifest; a new helper `params-env.sh` depends on would break **api** in a packaged install | manifest update specified; M8 mutation-tests it via `test_setup_scripts_are_packaged.py:44` |
| R4 Medium — `eval "$(repo-profile.sh …)"` swallows a non-zero exit (**reproduced**: `set -euo pipefail; eval "$(false)"` continues) | `repo-profile.sh` emits an eval-able failure, the `params-env.sh:9` idiom already used here. M7 tests the helper-failure path specifically. Noted as a pre-existing class this change neither adds to nor fixes. |
| R5 Medium — planner `:167` still demands a cross-repo plan; the deslop reviewer reads `recheck.json` at `:1292` and prbody at `:2009`, neither of which reads a `lint.txt` marker | `:167` added to the consumer table; lint applicability now travels **inside `recheck.json`** |
| R6 Medium — P8 asserted an observable that does not exist today; several 5b rows had no mutation; P10 would admit `SMOKE=FAIL` | §5 split three ways — preservation (P1-P11), acceptance (A1-A9), mutation (M1-M8). P8→P8a is now genuine preservation, P10 requires `SMOKE=PASS` with named probes. |

### Revision 6

| Architect round-3 finding | Change |
|---|---|
| Medium — an explicit `ARCHON_REPO` **equal** to the existing binding fell through steps 2 and 3 and was then rejected by step 4's default `--allow api`, so naming the repo you are already bound to broke the run while saying nothing did not | step 3 now adopts when `ARCHON_REPO` is **unset or equal**; step 4 applies only when there is no binding. New case A8b. |
| Low — stale operative references: M6 named the retired P12, M7a/M7b named an `eval` site in preflight that no longer exists, and §3.2's intro prose restated the pre-adoption rule | M6 → A8; M7a/M7b name the capture-and-check sites in `resolve-params.sh` and `params-env.sh`; the intro defers to the numbered sequence instead of paraphrasing it |

**Round trend.** Architect 4H/4M → 3H/3M → 1Med/1Low. Critic 5MAJ/2MIN → 1MAJ/2MIN. No finding was
rejected across three rounds, and no round introduced a new defect class — later rounds closed
unclosed halves of earlier ones.

### Revision 7

The Critic's final MINOR: §9 claimed the stale §3.2 intro prose had been corrected, and it had not —
the edit's anchor did not match, and the check that reported success tested a *different* string. The
paragraph is now replaced and the replacement verified by asserting the disputed text is absent. Worth
recording as its own class: a self-written "OK" that checks the wrong condition is not evidence.

**Final state: Architect 0 Critical / 0 High / 0 Medium / 0 Low. Critic ACCEPT-WITH-RESERVATIONS,
0 CRITICAL / 0 MAJOR / 0 MINOR open.** Across three rounds no finding from either reviewer was
rejected.

## 10. ADR

**Decision.** Make `full-sdlc-api` repo-aware through a closed, repo-keyed profile in a standalone
`repo-profile.sh`, with lane-declared repo allowlists.

**Drivers.** (1) The user forbids new workflow files. (2) The api lane is the only lane doing real
work; preservation must be demonstrated, not asserted. (3) The api/mcp differences span environment,
test selection, lint, smoke, browser policy and impact index — so the seam must carry all of them.

**Alternatives considered.**
*A separate mcp lane* — strongest isolation; unavailable by constraint.
*Explicit `if repo = api / else` dispatch inside the workflow* — the Architect's synthesis. The
Critic is right that revision 2 overstated its cost: it would **not** duplicate every gate, since
scope checks, artifact handling and the review loop stay shared. Its real cost is roughly eight
branched command sites plus branched preflight/bootstrap/smoke, against this design's cost of a new
shared helper on a path `bugfix` also uses. Genuinely close.
*An open command map in `params.json`* — revision 1; rejected because `bind-repo.py` rewrites `repo`
without rewriting derived commands.

**Why chosen.** Both surviving options are defensible, and revision 3's decisive argument was wrong:
the Architect is right that explicit `case "$REPO"` dispatch can also make applicability *declared*,
so "the F1-class bug is unrepresentable" does not distinguish them. The honest reason is narrower —
**one definition of commands and capabilities shared across multiple consumers** (`full-sdlc-api`,
`params-env.sh`'s bugfix callers, and the tests), against internal dispatch's repetition of the same
policy branch at every site. The costs are real and now specified: a new shared dependency on a path
`bugfix` also uses (§4.1), packaging (R3), and the eval-failure idiom (R4).

**The preservation claim is not "by construction".** Revision 2 said that; the Critic correctly
rejected it, since this expands shared-helper behaviour. Preservation rests on §5a's eleven tests —
P3 in particular, which is the direct control for the ordering bug that revision 2 would have shipped.

**Consequences.** `repo-profile.sh` must not grow an open extension point. A fourth repo means a table
row and an `--allow` entry — deliberately more friction than a config file.

**Follow-ups.** An httpStream boot smoke for mcp; whether a bugfix-shaped mcp lane justifies the larger
enum surface, deferred until a real mcp bug demands it.

**Status: pending approval.** Nothing is implemented.
