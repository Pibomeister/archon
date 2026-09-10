# ENG-3866 lane run — BLOCKED, and not by this change

Attempted 2026-09-08:

```
DISABLE_OMC=1 ARCHON_REPO=goodword-mcp archon workflow run full-sdlc-api \
  "/Users/eduardopicazo/Documents/Workspace/Goodword/.omc/research/linear/ENG-3866-mcp-share-groups.md"
```

Result — the workflow never started; it failed to LOAD:

```
Discovery: root=... workflows=44 bundled=21 global=0 project=23
Error: Workflow 'full-sdlc-api' failed to load: DAG node validation failed:
  Node 'plan-freeze': must have either 'command', 'prompt', 'bash', 'loop', 'loop_group',
  'approval', 'cancel', 'script', 'include', or 'workflow'
  ... same for 'plan-approval-verify', 'candidate-import', 'ship'
```

## Cause: a documented runtime mismatch, pre-existing

Those four nodes are `controller_action` nodes. `RUNBOOK.md:3-4`, the first paragraph of the
document, states it plainly:

> This working distribution requires the controller-action runtime fork; the installed v0.8.0
> runtime is incompatible.

The installed CLI is `Archon CLI v0.8.0` (`~/.local/bin/archon`, the only archon binary on the
machine; nothing under `~/.archon` and nothing global via npm provides the fork).

## Evidence that this change is not the cause

| Check | Result |
|---|---|
| `controller_action` in the **committed** `full-sdlc-api.yaml`, before any edit here | **4 occurrences** |
| `controller_action` lines in this change's diff | **0** |
| Workflows failing to load in the same run | **12 of 44** |
| Among them, files this change never touched | `backfill.yaml`, `bugfix.yaml`, `bugfix-lite.yaml`, `bugfix-codex.yaml`, `bugfix-lite-codex.yaml`, `full-sdlc-web.yaml`, `full-sdlc-web-codex.yaml`, `wrap-ship.yaml` |

No archon lane in this distribution can execute on this machine with the installed runtime. That is
true of `bugfix` and `full-sdlc-web` exactly as much as of the repo-aware `full-sdlc-api`.

## What was NOT done, deliberately

The blocker was **not** routed around. Specifically not attempted:

- Replacing the four `controller_action` nodes with `bash` or `approval` stubs to make the lane load.
  Those nodes are the human-approval and publication controls; stubbing them to get a run going is
  the precise failure mode the standing goal names — weakening a gate to turn a stop green.
- Installing a runtime fork. `RUNBOOK.md:9-11` says not to: *"Do not install or launch these
  workflows as a production replacement."*

## What the repo-awareness work still demonstrated

The runtime cannot execute the DAG, but the node bodies are ordinary bash and were run directly.
See `P3-P4-manual-evidence.md` in this directory: api preflight passes unchanged and allocates its
port; an mcp run resolves to the mcp worktree with no port; a resumed mcp run adopts its binding and
checks the mcp checkout; a conflicting `ARCHON_REPO` is a typed stop that leaves the binding intact.

## To unblock

Install the controller-action runtime fork the RUNBOOK names, then re-run the command at the top of
this file. The spec, the lane and the tests are all in place; nothing about ENG-3866 needs redoing.
