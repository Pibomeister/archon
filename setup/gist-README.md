# Goodword Archon SDLC — team setup

This gist distributes the **Archon layer only** of Goodword's two-lane SDLC pipeline: workflows, setup scripts, templates, runbook, and a derived permissions allowlist. Your dev environment is a **precondition** — the installer asserts it and refuses to continue if anything is missing; it never installs dev tooling for you.

## What you need first (asserted, never auto-installed)

- macOS or **desktop** Linux — the installer probes for a browser opener (`xdg-open`, else `open`) rather than asserting an OS. Linux notes: `xdg-open` is needed for the plan packet (`/usr/bin/open` there is often util-linux's `openvt(1)`, a different program); one of `lsof`/`ss`/`fuser` for port ownership; `python3` >= 3.7; and on x64 the archon binary requires **AVX2**
- `bun`, `pnpm`, `mise` (with node 20 and node 22 installed), `gh` (logged in, with access to the Goodword repos), `python3`, `agent-browser`, `aws` (SSO configured), `claude` CLI logged in via **subscription OAuth** (`/login`) — and none of the API-key env vars set (`ANTHROPIC_API_KEY` etc.; a stray key silently flips billing from your subscription to metered API)
- The three sibling repos cloned under one root directory: `api/`, `web-app/`, `goodword-kb/`
- `.env` files for `api/` and `web-app/` (obtained from a teammate out of band — they are never in this gist)
- The compound-engineering plugin (recommended, any version **>= 3.2.0**):

  ```
  claude plugin marketplace add EveryInc/compound-engineering-plugin
  claude plugin install compound-engineering@compound-engineering-plugin
  ```

  3.2.0 is the validated baseline. The skill-staging step verifies the headless contract markers the pipeline actually depends on: a newer cached version that kept them is staged (with a WARN at preflight); versions that restructured the skills (upstream did, after 3.2.0) are skipped with a NOTE, and staging falls back to the **vendored 3.2.0 snapshot shipped in this payload** — so installs never depend on upstream keeping our contract. If you see the vendored-fallback NOTE, tell the maintainer; it means the pipeline should eventually be ported to the new CE contract.

## Install

**Linux x64 only — check this first.** The archon binary requires AVX2 and the
upstream installer aborts without it. One command, before anything else:

```bash
grep -qw avx2 /proc/cpuinfo && echo "AVX2 ok" || echo "NO AVX2 — stop, tell the maintainer"
```

(arm64 has no such requirement, and neither does macOS.)

```bash
# HTTPS + your gh credentials. Secret gists need auth, and ssh to gist.github.com
# fails host-key verification on any machine that has not accepted that host.
git clone -c credential.helper='!gh auth git-credential' \
  https://gist.github.com/GIST_ID_HERE.git archon-setup
cd archon-setup
bash install.sh --root /absolute/path/to/Goodword
```

`--root` must be absolute.

Every step prints PASS/FAIL. Re-running is safe and is the supported upgrade path (`git pull` first).

Add `-y` to merge the derived permissions allowlist (`allowlist.json` — read-only inspection + the workflow's own tooling) into `<root>/.claude/settings.json`. The merge is additive and shows you the diff first. It deliberately excludes `archon workflow approve` (an agent that can release its own gate has no gate), `rm`, `pkill`, unscoped `curl`, and `git commit/push/add`.

## After install

Three operator skills are staged into both `<root>/.claude/skills/` and `<root>/.agents/skills/`: **`archon-install`** (set up or repair this stack), **`archon-sdlc`** (start and supervise a run), and **`archon-linear`** (read-only Linear intake into an immutable snapshot, then supported routing). They are not copied into the dedicated workflow-node Codex home. Say the skill name in a Claude or Codex operator session at the root. They cite the runbook rather than restating it, and do not merge a PR.

This gist ships fourteen workflows: the nine Claude workflows (`babysit`, `bugfix`, `bugfix-lite`, `bugfix-smoke-deployed`, `cleanup`, `full-sdlc-api`, `full-sdlc-api-lite`, `full-sdlc-web`, `register-probe`) plus five generated Codex twins. The two `-lite` lanes and all `-codex` lanes are generated; never hand-edit them. If a workflow is missing, compare against `<root>/.archon/VERSION`.

Read `archon__RUNBOOK.md` (installed to `<root>/.archon/RUNBOOK.md`). Claude lanes use `DISABLE_OMC=1 archon workflow run …`. Lite Codex lanes MUST use `python3 .archon/setup/archon-run.py`; it validates the dedicated home/external index, forces narrow workspace-write roots through a private wrapper, and arms the watchdog. Preserve the control token printed on each `STARTED` line and use it in the packet-rendered gate command.

## File naming

Gists cannot contain directories, so the `.archon/` payload is shipped flat: `archon__setup__stage-skills.sh` unpacks to `.archon/setup/stage-skills.sh`. `install.sh` does the unpacking and substitutes your root path into the workflows (Archon has no per-node cwd, so paths must be absolute per machine).

`VERSION` identifies this payload build; the installer copies it to `<root>/.archon/VERSION` so drift between your tree and the gist is visible.
