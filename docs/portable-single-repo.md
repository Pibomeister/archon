# Portable single-repository feature lane

`portable-single-repo-feature` separates project facts from machine paths. It is
additive: existing generated Goodword API/web workflows and their defaults are
unchanged. The new packaged workflow contains no hardcoded Goodword checkout,
API sibling, API generation, deployment or live knowledge-base write path.

The first profile is a small Fluxkeep frontend CTA change. A single Git repository
can contain multiple packages: Fluxkeep's root Next app coexists with `apps/*` and
`packages/*`. The pilot explicitly excludes those applications/packages, billing,
extraction, migrations and credential files.

## Pins and authority

- Workflow-layer baseline: GoodwordTeam/archon `b9e17845d843ec67769df5270af28419c2366545`.
- Required qualified engine: `85fe6e2f4c6ffca60a77478ac0698ef4b6da4b5f`, based on
  coleam00/Archon v0.10.1, including exact gate occurrences and the restricted
  `approval-evidence.v1` manifest policy. Earlier unfiltered evidence pins must
  not be used for this lane.
- `profiles/fluxkeep-next.v1.json` records command/path facts checked against
  Fluxkeep commit `299ff3f89d3478af286115af5ef6b275e64aecea`. This does not claim the
  actual Fluxkeep commands or any provider have been qualified by this patch.
- The factory bridge owns worktree creation, immutable Ready inputs, qualified
  engine/provider permits and machine binding. This workflow never uses the
  manual checkout or creates a worktree by inferring a project from cwd.

## Profile and machine binding

`profiles/project-profile.v1.schema.json` defines the portable, non-secret project
profile. Verification commands are argv arrays with bounded timeouts. The planner
cannot replace them. The Fluxkeep profile uses pnpm 10.21.0, root/test TypeScript
checks, the committed lint and formatting scripts, and focused Jest:

```text
pnpm exec jest --watchman=false --runInBand --runTestsByPath test/src/app/(admin)/documents/components/document-detail.test.tsx
```

Only `document-detail.tsx` and its focused test are allowed in this pilot profile.
`typecheck:all` is deliberately excluded because it invokes the mobile workspace.
Dependencies must be prepared in the isolated worktree from the committed lockfile;
this workflow does not copy a manual checkout's dependencies or `.env` files.

`profiles/machine-binding.v1.schema.json` defines the machine-local binding:

| Field | Meaning |
| --- | --- |
| `projectId`, `machineId` | Explicit project and machine identity |
| `repositoryRoot`, `allowedWorktreeRoot` | Actual linked worktree and its configured parent directory |
| `branch`, `baseCommit` | Exact branch and frozen remote-main commit; never a floating ref |
| `profilePath`, `profileSha256` | Immutable Ready profile file and SHA-256 of its original bytes |
| `briefPath`, `briefSha256` | Immutable Ready brief and SHA-256 of its original bytes |
| `knowledgeRoot`, `knowledgeCommit` | Read-only Git source and exact knowledge commit |
| `engineCommand` | Qualified read-only engine interface argv, beginning with an absolute executable path |
| `allowPublish` | Explicit authorization to create a draft PR after all gates |

The production `engineCommand` interface must expose only `workflow get` and
`workflow gate-evidence`, with no control tokens. Bound response and resume remain
private bridge actions; role-visible context is not an authorization channel.

The example binding contains nonfunctional `/configured/` paths and zero digests;
it is documentation, never a runnable default. The bridge must materialize real
values. Profile or binding drift is a hard stop; missing data never falls back to
Goodword's API/web paths. POSIX machine paths are the v1 support surface.

## Workflow

1. Validate actual root, linked-worktree status, remote, branch, package manager,
   frozen base, profile and brief digests. Capture committed KB bytes with `git
   cat-file`; dirty or untracked KB content is never imported. Reuse the existing
   `repo-policy.py` extraction against the frozen source commit.
2. Run native plan and critique roles, then mechanically seal the plan, its file
   allowlist and profile-owned commands. Planning roles may not modify source.
   The producer re-derives the pinned context, copies its fixed plan/context/KB
   inputs into `approval-evidence/`, and writes a versioned manifest last. It
   never selects the general artifacts tree or provider credential homes.
3. Pause at the native human gate. Reject ends the attempt. The factory submits
   an exact run/occurrence/evidence-bound decision and resumes the exact run.
4. Read the engine's original sealed plan/context/KB evidence before each critical
   stage. Editing a local plan and recomputing its local checksum cannot substitute
   different approved content.
5. Run the existing W3 implementation/W6 review responsibilities in a native
   `loop_group`, with at most two durable rounds. Real commands own test results;
   incomplete/degraded review or findings cannot pass. Native engine resumes cannot
   reset the separate durable round cap.
6. Compose a PR body, mechanically commit/push and create or reuse a matching
   **draft** PR only when publication is authorized. Recheck scope, content and
   cleanliness after commit hooks before pushing. A hook-changed commit is retained
   locally and requires a new review; it is never pushed automatically.
7. Save proposed knowledge only under run artifacts. Promotion to the project KB
   is a separate reviewed job. There is no merge or deployment action.

Without `allowPublish`, the workflow records `pr-evidence.json` with an explicit
publication hold and fails that step before any commit or push. It must not be
presented as a completed PR. An interrupted commit without its matching receipt
also stops for inspection instead of guessing ownership.

## Installation and invocation

The pack lives at `workflows/portable/single-repo-feature/` in this repository.
Stage it under the engine source root at
`.archon/workflows/portable/single-repo-feature/`; preserve the two helper symlinks
or copy their target contents. The engine's source capture dereferences them, so
repository policy and review vocabulary are pinned with the workflow. Python
bytecode writes are disabled inside the helper to preserve that captured source.
The team package manifest includes all resources.

```text
<qualified engine argv> workflow run portable-single-repo-feature \
  --cwd <isolated linked worktree> --workflow-source <staged source root> \
  --input binding=<immutable machine-binding.json> --launch-key <attempt command id>
```

Use `launch-intent` to obtain the engine-computed launch digest when binding a
prior Ready decision. Fresh interactive runs remain foreground processes owned by
the bridge; do not disable gates to detach them. Bound `respond --json` records the
decision only; exact-ID `resume` executes it.

The factory must qualify Codex subscription/OAuth permits and the actual runtime
before real model execution. Do not inherit the coordinator's task identity into
fixture/worker processes. Keep observer/OMX state in a separate run-owned directory;
`.omx` files leaking into the target repository remain scope violations, not an
implicit exception. Native provider settings/authentication are otherwise retained.

## Verification scope

```text
python3 -m unittest setup.tests.test_portable_project
ARCHON_ENGINE_ROOT=<qualified engine checkout> python3 -m unittest setup.tests.test_portable_engine
```

The engine tests load the production graph unchanged, then exercise its mechanical
boundaries using explicitly stubbed agent judgments. Actual source capture,
script execution, gate persistence, bound response and resume remain native engine
operations. Tests cover original-evidence tampering and a real command's failure
then success. They never contact a provider or publish a GitHub PR.

These results qualify the portable contracts and mechanical integration. They do
not qualify real model quality, actual Fluxkeep Jest execution, full SDLC delivery,
provider allowances, deployment, or mobile acceptance. Those remain Stage 4 work.
