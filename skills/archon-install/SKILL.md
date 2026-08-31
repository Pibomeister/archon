---
name: archon-install
description: Use when setting up or repairing the Goodword Archon SDLC stack on a machine - installing the pinned archon CLI, the compound-engineering plugin, the CLI prerequisites (bun, pnpm, mise+node, gh, agent-browser, aws, python3), staging the Claude skills, or running/debugging install.sh from the setup gist. Triggers on "install archon", "set up the SDLC pipeline", "install.sh failed", "PREFLIGHT=FAIL command missing", or a fresh clone of the Archon setup gist.
---

<WORKFLOW-NODE-STOP>
If you are an Archon workflow node session, ignore this skill. It is for operator
sessions setting up a machine. A node never installs anything.
</WORKFLOW-NODE-STOP>

# Installing the Archon SDLC stack

The setup gist ships the **Archon layer only** - workflows, setup scripts,
templates, the runbook, the operator skills, and a derived permissions allowlist.
The dev environment is a **precondition**: `install.sh` asserts it and refuses to
continue if anything is missing. It never installs dev tooling.

That split is the whole design. Your job is to get the preconditions true, then
let `install.sh` do the rest and read its PASS/FAIL lines.

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
  maintainer action from the Goodword root only.
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
- `python3` must be **3.7 or newer** - the helpers use
  `subprocess.run(capture_output=...)`, which raises `TypeError` on the 3.6 that
  ships with RHEL 7/8 and Ubuntu 18.04.

## 2. Preconditions checklist

Layout - three sibling repos cloned under **one root** (call it `$ROOT`):

```
$ROOT/api/          (.git present, .env present)
$ROOT/web-app/      (.git present, .env present)
$ROOT/goodword-kb/  (wiki/ present)
```

Commands `install.sh` asserts, and how they are normally installed:

| Command | macOS | Linux | Notes |
|---|---|---|---|
| `bun` | `curl -fsSL https://bun.sh/install \| bash` | same | api runtime |
| `pnpm` | `brew install pnpm` | `corepack enable pnpm` | web package manager |
| `mise` | `brew install mise` | `curl https://mise.run \| sh` | then `mise install node@20 node@22` |
| `gh` | `brew install gh` | distro package | then the user runs `gh auth login` |
| `python3` | preinstalled | preinstalled | must be >= 3.7 |
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
bash install.sh --root /absolute/path/to/Goodword
```

Add `-y` to merge the derived permissions allowlist into
`$ROOT/.claude/settings.json`. The merge is additive and prints every rule it
adds. It deliberately excludes `archon workflow approve`, `rm`, `pkill`, unscoped
`curl`, and `git commit/push/add` - an agent that can release its own plan gate
has no gate.

`install.sh` is **idempotent**, and re-running it (after `git pull` in the gist
clone) is the supported upgrade path.

What its seven steps do, so you can read a failure:

0. **Root validation** - absolute path, three repos present. Aborts before
   anything else if the layout is wrong.
1. **Preconditions** - browser opener, commands, port tool, python floor, mise
   node 20/22, `gh` auth, repo fetch access, aws session, billing guard, `.env`
   files, CE plugin >= 3.2.0 (newest resolved; WARN when newer than the
   validated baseline). Every line is PASS or FAIL; it aborts on any FAIL.
2. **Archon CLI** - downloads `archon.diy/install` and runs it with
   `VERSION=v0.8.0 INSTALL_DIR=$HOME/.local/bin`. That installer resolves
   `archon-linux-x64` / `archon-linux-arm64` / the mac builds on its own. Warns if
   `~/.local/bin` is not on PATH.
3. **Render the payload** - unflattens each `archon__*` gist file into
   `$ROOT/.archon/` and substitutes the real root path for the
   `GOODWORD_ROOT` placeholder. Archon has no per-node cwd, so every path in the
   workflows is absolute and machine-specific by construction.
4. **Stage skills** - symlinks `ce-code-review` and `ce-doc-review` from the
   pinned CE cache, plus `archon-sdlc` and `archon-install` from
   `$ROOT/.archon/skills/`, into `$ROOT/.claude/skills/`. Node sessions do not
   load installed plugins (proven), so the project-scope symlink plus a `skills:`
   declaration on the node is the only mechanism that works.
5. **Folder registration** - one `register-probe` run; must print
   `REGISTER_PROBE_OK` and `BASE_BRANCH=[main]`.
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
| `FAIL archon on PATH is not v0.8.0` | another archon shadows it | put `~/.local/bin` ahead of the other install |
| `FAIL BASE_BRANCH did not resolve to [main]` | `.archon/config.yaml` edited | restore `worktree.baseBranch: main` |
| `FAIL workflow not ok: <w>` | YAML/schema problem | read the validator output it prints |
| `Error: Workflow '<name>' not found` (at run time) | installed payload predates the lane | `ls <root>/.archon/workflows/` + `cat <root>/.archon/VERSION`; if the yaml is absent, pull the gist and rerun `install.sh`. If the gist lacks `archon__workflows__<name>.yaml`, the maintainer adds it to the `package.sh` MANIFEST (`grep <name> setup/package.sh`) and `--publish`. Never auto-retry |
| `.env missing` | expected | get it from a teammate out of band |

`install.sh` runs without `set -e` on purpose (it accumulates FAILs rather than
dying on the first), which is why it resolves `grep`/`head` up front and exits if
it cannot - a guard built on a missing binary would otherwise pass silently.

## 5. Verify, then hand off

After `=== DONE ===` with zero FAIL:

```bash
ls -l "$ROOT/.claude/skills"        # 4 links, all resolving
cat "$ROOT/.archon/VERSION"         # matches the gist's VERSION
archon --version                    # Archon CLI v0.8.0
```

Then the first run is the toy dry-run, and driving it is a different job - use the
`archon-sdlc` skill, and read `$ROOT/.archon/RUNBOOK.md`.

One thing to say out loud before that first run: **it spends the operator's own
Claude subscription window** (5-hour and weekly quota, shared with their
interactive use), not an API budget. Cost caps in the workflows are quota guards.
