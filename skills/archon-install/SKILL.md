---
name: archon-install
description: Use when setting up or repairing the Archon SDLC stack on a machine - installing the pinned archon CLI, the compound-engineering plugin, the CLI prerequisites, staging operator skills, or debugging install.sh. Triggers on "install archon", "set up the SDLC pipeline", "install.sh failed", "PREFLIGHT=FAIL command missing", or a fresh clone of the Archon layer.
---

<WORKFLOW-NODE-STOP>
If you are an Archon workflow node session, ignore this skill. It is for operator
sessions setting up a machine. A node never installs anything.
</WORKFLOW-NODE-STOP>

# Installing the Archon SDLC stack

`setup/install.sh` currently prints `INSTALL=DISABLED` and exits 1. Do not run
it as a production installer. The CLI pin below is **v0.10.1** for when
admission reopens. The rest of this skill is the prior operator surface.

Two roots, never one baked-in home path (same contract as `archon-sdlc`):

| Variable | Meaning |
|---|---|
| `$ARCHON_LAYER` | This pack (workflows, `setup/`, `profiles/`, `RUNBOOK.md`, operator skills). |
| `$PROJECT_ROOT` | The project folder the run targets. For Goodword, the directory holding `api/`, `web-app/`, and `goodword-kb/`. |

When operating from this checkout, `$ARCHON_LAYER` is this repo. Helpers are
`$ARCHON_LAYER/setup/...`. `archon-run.py` exports both roots. Raw
`archon workflow run` without them fails at preflight. Project-specific how-to
lives in `$ARCHON_LAYER/profiles/<project>/` (Goodword: `profiles/goodword/`;
Fluxkeep: `profiles/fluxkeep/` plus `profiles/fluxkeep-next.v1.json`). The five
graphs and their Codex/Grok twins still encode Goodword topology; a Fluxkeep pack exists so operators do
not invent stack commands, not because those lanes are Fluxkeep-ready.

This pack is the **Archon layer only** - workflows, setup scripts, templates,
the runbook, the operator skills, provider-neutral feature launcher,
repository-list planning/execution helpers, and a derived permissions allowlist.
The repository-list feature uses the existing feature workflows and generated
Codex twins; it does not require a patched stock Archon binary.
The dev environment is a **precondition**: `install.sh` asserts it and refuses to
continue if anything is missing. It never installs dev tooling. It also must
not rewrite machine home paths into YAML: the production graphs are env-generic.

That split is the whole design. Your job is to get the preconditions true.
When admission reopens, let `install.sh` place or link the pack and stage
skills; until then, export the two roots (or use `archon-run.py`) from this
checkout and read PASS/FAIL lines from the prior installer surface below.

## 0. Guardrails

- **Never set `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_API_KEY`,
  `CLAUDE_CODE_OAUTH_TOKEN`, or `ANTHROPIC_PROFILE`. Never configure
  `apiKeyHelper`.** Any of them silently outranks the subscription login and flips
  billing from the operator's Claude subscription to metered API. A Max subscriber
  in the wild hit four figures in two days this way. `install.sh` guards for
  exactly this - if the guard fires, remove the variable; never remove the guard.
- **Never create or fetch `api/.env` or `web-app/.env`.** They are distributed out
  of band by a teammate and are deliberately absent from the gist.
- **compound-engineering: validated baseline 3.2.0, tolerated forward, vendored
  fallback.** The hard gate is the headless CONTRACT (the `mode:headless` and
  `"verdict"` markers the staging step and every lane preflight grep), not the
  version number. Staging prefers the validated baseline from the cache, then
  the vendored validated snapshot, and only then a newer cached version whose
  contract holds (every non-baseline pick is announced);
  versions that dropped the contract (upstream restructured these skills after
  3.2.0) are skipped with a NOTE, and the vendored 3.2.0 snapshot shipped in
  the payload is the final fallback. Never delete the marker checks and never
  hand-edit a cached or vendored skill; if the vendored fallback fires, tell
  the maintainer. Newer CE generations that moved the contract into their
  reference files (mode:headless aliasing mode:agent, JSON returns) are ALSO
  accepted — the review gates parse both the old markdown envelope and the new
  raw-JSON return via setup/parse-review-envelope.py.
- **Do not run `claude /login` on the user's behalf**, and do not run
  `gh auth login` or `aws login` for them - those are interactive identity steps.
  Tell them to run it (`! aws login` puts the output in this session).
- **Never run `package.sh --publish`.** That releases the team gist and is a
  maintainer action from this layer checkout only.
- Installing system packages changes the user's machine. Propose the command,
  say what it installs, and let them confirm.

## 1. Platform

macOS or **desktop** Linux (a GUI machine with a real browser, since the plan gate
surfaces an HTML packet and the web lane drives `agent-browser`). Both are
supported: `install.sh` probes for a browser opener rather than asserting an OS.

Linux specifics:

- The opener is `xdg-open`. Note that `/usr/bin/open` on some distros is
  util-linux's link to `openvt(1)` - a *different program*, not a missing one,
  which is why `xdg-open` is probed first everywhere.
- **On x64, the archon binary requires AVX2.** The upstream installer detects the
  CPU and refuses without it. arm64 has no such requirement.
- Port inspection needs one of `lsof`, `ss` (iproute2), or `fuser` (psmisc).
- `python3` must be **3.10 or newer** for the repository-list helpers' type
  annotations and path APIs. Do not advertise the older 3.7 minimum for this
  package.

## 2. Preconditions checklist

Goodword layout - three sibling repos cloned under **one project root**
(`$PROJECT_ROOT`):

```
$PROJECT_ROOT/api/          (.git present, .env present)
$PROJECT_ROOT/web-app/      (.git present, .env present)
$PROJECT_ROOT/goodword-kb/  (wiki/ present)
```

Also clone `$PROJECT_ROOT/goodword-mcp/` when selecting it for feature work. Each
selected repository must already exist and have a registered `repo-profile.sh`
profile; the launcher does not clone missing repositories or expand feature
scope. Other projects follow `$ARCHON_LAYER/profiles/<project>/`, not this
Goodword tree.

Commands `install.sh` asserts, and how they are normally installed:

| Command | macOS | Linux | Notes |
|---|---|---|---|
| `bun` | `curl -fsSL https://bun.sh/install \| bash` | same | api runtime |
| `pnpm` | `brew install pnpm` | `corepack enable pnpm` | web package manager |
| `mise` | `brew install mise` | `curl https://mise.run \| sh` | then `mise install node@20 node@22` |
| `gh` | `brew install gh` | distro package | then the user runs `gh auth login` |
| `python3` | preinstalled | preinstalled | must be >= 3.10 |
| `agent-browser` | `pnpm add -g agent-browser` | same | web-lane UAT driver |
| `claude` | Claude Code install | same | logged in via subscription OAuth (`/login`) |
| `aws` | `brew install awscli` | distro package | SSO configured; creds last ~15 min |
| `git`, `curl`, `diff` | preinstalled | preinstalled | |
| `lsof` **or** `ss` **or** `fuser` | `lsof` preinstalled | install one | port ownership |
| `xdg-open` (Linux only) | n/a | `xdg-utils` | opens the plan packet |

Both node versions matter and are pinned per lane: **node@20 for web, node@22 for
api**. mise shims do not apply in bare execs, so the workflows call
`mise x node@NN --` explicitly.

Then the CE plugin:

```bash
claude plugin marketplace add EveryInc/compound-engineering-plugin
claude plugin install compound-engineering@compound-engineering-plugin
```

Any version **>= 3.2.0** in the plugin cache works; `install.sh` resolves the
newest installed one and prints a WARN when it is newer than the validated
baseline (3.2.0). The staging step then verifies the headless contract markers
the pipeline actually depends on — that check, not the version, is the gate.

## 3. Install

```bash
git clone git@gist.github.com:<gist-id>.git archon-setup
cd archon-setup
bash install.sh --root /absolute/path/to/project
```

`--root` is `$PROJECT_ROOT`. Until admission reopens, do not run that script;
export `$ARCHON_LAYER` (this checkout) and `$PROJECT_ROOT` and use
`python3 "$ARCHON_LAYER/setup/archon-run.py"` instead.

Add `-y` to merge the derived permissions allowlist into
`$PROJECT_ROOT/.claude/settings.json`. The merge is additive and prints every
rule it adds. It deliberately excludes `archon workflow approve`, `rm`, `pkill`,
unscoped `curl`, and `git commit/push/add` - an agent that can release its own
plan gate has no gate.

`install.sh` is **idempotent**, and re-running it (after `git pull` in the gist
clone) is the supported upgrade path when admission reopens.

What its seven steps do, so you can read a failure:

0. **Root validation** - absolute `$PROJECT_ROOT`, Goodword three-repo layout
   present. Aborts before anything else if the layout is wrong.
1. **Preconditions** - browser opener, commands, port tool, python floor, mise
   node 20/22, `gh` auth, repo fetch access, aws session, billing guard, `.env`
   files, CE plugin >= 3.2.0 (newest resolved; WARN when newer than the
   validated baseline). Every line is PASS or FAIL; it aborts on any FAIL.
2. **Archon CLI** - downloads `archon.diy/install` and runs it with
   `VERSION=v0.10.1 INSTALL_DIR=$HOME/.local/bin`. That installer resolves
   `archon-linux-x64` / `archon-linux-arm64` / the mac builds on its own. Warns if
   `~/.local/bin` is not on PATH.
3. **Place the payload** - historically unflattened each `archon__*` gist file
   into `$PROJECT_ROOT/.archon/` (that directory *is* `$ARCHON_LAYER` after a
   gist install). The old render also substituted a host path for
   `GOODWORD_ROOT`. That substitution is obsolete: the five production graphs
   and their Codex and Grok twins contain no machine home paths. Nodes bind
   `$ARCHON_LAYER` and `$PROJECT_ROOT` at preflight. When operating from this
   checkout, the pack is this repo; do not copy it under the project or rewrite
   YAML paths. When admission reopens, the installer should place or link the
   pack and stage skills; it must not bake a machine home into YAML.
4. **Stage skills** - symlinks `ce-code-review` and `ce-doc-review` from the
   pinned CE cache, plus `archon-sdlc`, `archon-install`, and `archon-linear`
   from `$ARCHON_LAYER/skills/`, into both `$PROJECT_ROOT/.claude/skills/` and
   `$PROJECT_ROOT/.agents/skills/`. Node sessions do not
   load installed plugins (proven), so the project-scope symlink plus a `skills:`
   declaration on the node is the only mechanism that works.
5. **Git shell + repo registration** - `install.sh` makes `$PROJECT_ROOT` a git
   SHELL (`.gitignore` = `*`, one empty commit, a bare origin under
   `~/.archon/shells/`) when it is not already a git repo, then runs
   `register-probe --branch archon-setup-probe`. It must print
   `REGISTER_PROBE_OK`, `BASE_BRANCH=[main]`, and a cwd under
   `worktrees/archon/task-archon-setup-probe`.

   The shell exists so `--branch` works, and `--branch` is what makes runs
   concurrent: archon locks a run on its `working_path` and nothing else, so
   without it every lane shares the project root and a second launch
   self-cancels. Nothing is ever tracked in the shell — node bodies address
   every repo through `$PROJECT_ROOT` and `params.json` worktrees, so the
   archon worktree is only a lock key and an artifacts anchor.

   **If a machine registered the root as a FOLDER project first**, the stored kind
   is sticky and archon refuses `--branch` with *"Worktree options require a
   git-repo project."* Flip it:

   ```bash
   sqlite3 ~/.archon/archon.db "update remote_agent_codebases \
     set kind='repo', default_branch='main' where default_cwd='$PROJECT_ROOT'"
   ```

   Rollback of the whole thing is `rm -rf "$PROJECT_ROOT/.git"`, then re-register.

   **The artifacts root moves with it**: a repo project writes to
   `~/.archon/workspaces/_local/<Project>/artifacts/runs/`, not
   `_folder/goodword/...`. Anything resolving a run's artifacts must go through
   `"$ARCHON_LAYER/setup/run-artifacts.sh"`, which reads the run's own
   `output_root`.
6. **Workflow validation** - gates on OUR shipped workflows only (`babysit`,
   `bugfix`, `bugfix-lite`, `bugfix-smoke-deployed`, `cleanup`, `full-sdlc-api`,
   `full-sdlc-api-lite`, `full-sdlc-web`, `register-probe`). Archon validates its
   own bundled workflows too, and those can error harmlessly.
7. **Allowlist merge** (only with `-y`).

## 4. Reading failures

| Symptom | Cause | Fix |
|---|---|---|
| `FAIL command missing: X` | precondition absent | install X, re-run |
| `FAIL no browser opener` | no `xdg-open`/`open` | install `xdg-utils` |
| `FAIL no port-inspection tool` | no lsof/ss/fuser | install one |
| `FAIL python3 3.7+ required` | system python 3.6 | install a newer python3 |
| `FAIL cannot resolve a grep binary` | no grep on PATH or in /usr/bin | fix PATH - the guards below it are not trustworthy without it |
| `FAIL billing guard: <VAR> is set` | an API-key env var | unset the variable, never the guard |
| `WARN CE <ver> is newer than the validated baseline` | forward version drift | expected; staging's contract markers decide |
| `NOTE: CE <ver> present but its skills lack the headless contract markers — skipping` | that newer CE restructured the skills (real upstream change after 3.2.0) | expected; staging falls through to the next candidate or the vendored snapshot |
| `NOTE: ... staging the vendored 3.2.0 snapshot` | no cached CE carries the contract | fine — the payload ships the validated skills; tell the maintainer so the pipeline eventually gets ported to the new CE contract |
| `FAIL: no compound-engineering skills with the headless contract found` | neither cache nor the vendored snapshot qualifies (corrupt/partial payload) | re-clone the gist and re-run; if it persists, report to the maintainer — never edit skills to force a pass |
| `FAIL archon on PATH is not v0.10.1` | another archon shadows it | put `~/.local/bin` ahead of the other install |
| `FAIL BASE_BRANCH did not resolve to [main]` | `.archon/config.yaml` edited | restore `worktree.baseBranch: main` |
| `FAIL workflow not ok: <w>` | YAML/schema problem | read the validator output it prints |
| `Error: Workflow '<name>' not found` (at run time) | installed payload predates the lane | `ls "$ARCHON_LAYER/workflows/"` + `cat "$ARCHON_LAYER/VERSION"`; if the yaml is absent, pull the layer and (when admission reopens) rerun `install.sh`. If the gist lacks `archon__workflows__<name>.yaml`, the maintainer adds it to the `package.sh` MANIFEST (`grep <name> setup/package.sh`) and `--publish`. Never auto-retry |
| `.env missing` | expected | get it from a teammate out of band |

`install.sh` runs without `set -e` on purpose (it accumulates FAILs rather than
dying on the first), which is why it resolves `grep`/`head` up front and exits if
it cannot - a guard built on a missing binary would otherwise pass silently.

## 5. Verify, then hand off

After `=== DONE ===` with zero FAIL:

```bash
ls -l "$PROJECT_ROOT/.claude/skills"   # 4 links, all resolving
cat "$ARCHON_LAYER/VERSION"            # matches this pack's VERSION
archon --version                       # Archon CLI v0.10.1
```

Then the first run is the toy dry-run, and driving it is a different job - use the
`archon-sdlc` skill, and read `$ARCHON_LAYER/RUNBOOK.md`.

One thing to say out loud before that first run: **it spends the operator's own
Claude subscription window** (5-hour and weekly quota, shared with their
interactive use), not an API budget. Cost caps in the workflows are quota guards.

## 6. Codex lanes (optional, one-time)

The `*-codex` twins bill the ChatGPT plan through a dedicated codex home so
node sessions shed the operator's personal `~/.codex` config (preamble, hooks,
MCP servers). Set it up once:

```bash
mkdir -p "$HOME/.archon/codex-home"
printf 'preferred_auth_method = "chatgpt"\nsandbox_mode = "workspace-write"\n' > "$HOME/.archon/codex-home/config.toml"
CODEX_HOME="$HOME/.archon/codex-home" codex login   # ChatGPT login, opens a browser
```

The lanes use GitNexus as optional evidence acceleration. Codex reads MCP
servers from its OWN home; when GitNexus is absent, stale, missing MCP, or
not visible to the node, the workflows must emit `GITNEXUS=UNAVAILABLE` or
`IMPACT=UNAVAILABLE` and continue with repo-local investigation rather than
blocking launch/control:

```bash
# Recommended: pin the server's cwd for deterministic default-repo selection:
#   [mcp_servers.gitnexus]
#   cwd = "/absolute/path/to/project"   # $PROJECT_ROOT; add to $CODEX_HOME/config.toml
# For Goodword, $PROJECT_ROOT is only a git SHELL (tracks nothing), so it is not
# a source of code to index. Build the pinned API main index from a dedicated
# clean worktree so lite impact is deterministic. NOTE: this index is a SHARED,
# UNVERSIONED resource -- re-analyzing it while another lane is mid-run changes
# what that run reads. Since 2026-09-07 `capabilities.json` records
# `index_commit`/`expected_commit` so a run can say which index it used;
# re-analyze between runs, not during one:
git -C "$PROJECT_ROOT/api" fetch origin main
# GitNexus names indexes from the remote repo. Remove the old main-checkout
# index first so the pinned worktree is the ONE registry entry named `api`.
if [ -f "$PROJECT_ROOT/api/.gitnexus/run.cjs" ]; then
  (cd "$PROJECT_ROOT/api" && node .gitnexus/run.cjs clean --force)
fi
INDEX_WT="$HOME/.archon/gitnexus/api-main"
mkdir -p "$(dirname "$INDEX_WT")"
if [ ! -e "$INDEX_WT/.git" ]; then
  git -C "$PROJECT_ROOT/api" worktree add --detach "$INDEX_WT" origin/main
else
  test -z "$(git -C "$INDEX_WT" status --porcelain)" || { echo "api index worktree is dirty - GitNexus acceleration disabled until inspected"; exit 0; }
  git -C "$INDEX_WT" switch --detach origin/main
fi
(cd "$INDEX_WT" && npx gitnexus analyze)
# Re-run through the generated runner so status provenance matches the exact
# binary the MCP command will execute (npx/pnpm resolver paths can differ).
(cd "$INDEX_WT" && node .gitnexus/run.cjs analyze)
CODEX_HOME="$HOME/.archon/codex-home" codex mcp remove gitnexus >/dev/null 2>&1 || true
CODEX_HOME="$HOME/.archon/codex-home" codex mcp add gitnexus -- \
  python3 "$ARCHON_LAYER/setup/gitnexus-mcp-dispatch.py"
CODEX_HOME="$HOME/.archon/codex-home" codex mcp list   # gitnexus enabled
python3 "$ARCHON_LAYER/setup/archon-run.py" check
```

AWS CLI, a live SSO session, and GitNexus are optional evidence capabilities
for code workflows, not install/readiness failures. Do not make setup, launch,
resume, approve, reject, or abandon fail because AWS is missing/expired or
GitNexus is absent/stale/missing MCP. Backfill production execution remains
the exception because its read/write authority genuinely comes from AWS.

The pinned index has two legitimate targets when GitNexus is available: current
`origin/main` for a new run, and the immutable `bugfix-chain.json` baseline when
resuming an existing run. The launcher reports `GITNEXUS=UNAVAILABLE` with
`expected-origin/main` versus `expected-stored-run-baseline` details instead of
blocking; rebuild/switch to the named target when graph evidence is wanted.

`sandbox_mode = "workspace-write"` is mandatory, but the pinned CLI still
overrides it with a danger-full-access flag. The guarded launcher therefore installs a
private mode-0500 `codex-workspace-wrapper.sh` and points `CODEX_BIN_PATH` at it;
the wrapper replaces the adapter flag with restricted workspace permissions,
uses the private run binding for the assigned worktree, and adds only the
prompt-bound run artifact root. For repository-list planning, only run artifacts
are writable. After approval, one selected repository worktree is writable;
routing files and the approved plan/test/input artifacts remain read-only.
The nonce, control-token hash, signal targets, and GitNexus runner stay outside
those roots. Real adapter probes must deny control-code writes and allow the
single run artifact directory.

`stage-skills.sh` (installer step 4) links the CE review and operator skills into
`$PROJECT_ROOT/.agents/skills/`; the guarded launcher covers Codex API/web feature lanes plus bugfix lanes and mirrors **only** the external CE
review targets into `$CODEX_HOME/skills/` because the narrowed Codex cwd is
`api`. Operator skills retain `WORKFLOW-NODE-STOP` and are never exposed in
that dedicated workflow-node home.
The twins' preflight asserts the project links and ChatGPT auth
(`BILLING_GUARD=FAIL` typed line on a stale login).
No `archon ai login` is needed; `archon doctor`'s "Codex not configured" line
is about archon's own store and is expected. RUNBOOK §15 is the operating
reference.

## 6a. Grok lanes (generated twins, not a live run path)

`setup/derive-grok.py` emits `*-grok` twins of the five production graphs
(`provider: grok`, xAI OIDC billing guard, `/skill-name` slash tokens). Never
hand-edit them. Login is `grok login` into `${GROK_HOME:-$HOME/.grok}/auth.json`
(`auth_mode` `oidc`). Never set `XAI_API_KEY` or `GROK_CODE_XAI_API_KEY`.

Vanilla coleam00/Archon does not register `provider: grok`. `install.sh` still
lists the generated YAML when admission reopens; a validator ERROR on unknown
provider is expected until an upstream community provider lands. Do not add
Grok to `archon-run.py --provider`, and do not install a fork. RUNBOOK §15a.

### Repository-list readiness

The package must contain `feature_chain.py`, `feature-budget.py`,
`validate-joint-plan.py`, `run-joint-integration.py`,
`trusted-local-candidate.sh`, and `write-local-candidate.py` under `$ARCHON_LAYER/setup/`,
alongside the launcher, watchdog, Codex workspace wrapper, `codex-spawn-guard.py`, repository profiles, and generated feature twins.
Operator skills come from `$ARCHON_LAYER/skills/`; staging links these packaged sources
into the provider skill directories. Do not hand-edit `dist/gist` or a generated
Codex twin to repair a missing capability.

Verify registry discovery and the guarded launcher before starting a feature:

```bash
bash "$ARCHON_LAYER/setup/repo-profile.sh" --list
python3 "$ARCHON_LAYER/setup/archon-run.py" check
```

Then hand off to `archon-sdlc` for, for example:

```bash
python3 "$ARCHON_LAYER/setup/archon-run.py" feature --provider codex \
  --scope api,goodword-mcp "/absolute/path/to/spec.md"
python3 "$ARCHON_LAYER/setup/archon-run.py" feature --provider claude \
  --scope goodword-mcp --base goodword-mcp=<40-hex-parent> "/absolute/path/to/spec.md"
```

Keep the Codex controller and shared-budget ledger outside worker-writable
roots. New Codex repository-list chains default to 240 active minutes and
30 million tokens for the whole chain; approval waits are excluded, retries
share the remaining allowance, and exact sessions include native subagents.
Missing accounting must fail containment. The endpoint is `locally_verified`
with publication held, not a push or PR. Qualification is established
(chain `2205cded…`, receipt `7da448da…`). Operator stop recipes after a
launch: `$ARCHON_LAYER/docs/operator-recovery.md` and `archon-sdlc`.
