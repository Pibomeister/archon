# P3 / P4 — manual execution evidence

The unit suite does **not** cover these. `preflight` and `bootstrap` depend on real `gh` auth, a real
git remote and the actual `api/` checkout, so executing their full node bodies inside
`setup/tests` would touch the real repository. They were run by hand instead, against the real node
bash extracted from `workflows/full-sdlc-api.yaml`, and this file is the record.

Run 2026-09-08 on darwin, `.archon` at working tree of `457017e3` + local changes.

## P3 — api preflight, `ARCHON_REPO` unset (the control for the ordering defect)

```
PREFLIGHT_WARN aws unavailable or expired - AWS evidence/runtime integrations may degrade
PARAMS=OK spec=<tmp>/spec.md slug=spec repo=api (new) branch=archon/spec api_port=4243 web_port=none
PREFLIGHT_PORTS api=4243 (per-run, from base 4123)
PREFLIGHT=PASS
  params repo: api
```

The `aws` line is pre-existing and non-blocking (expired local credentials).

## A9 — fresh mcp selection

```
PARAMS=OK spec=<tmp>/spec.md slug=spec repo=goodword-mcp (new) branch=archon/spec api_port=none web_port=none
PREFLIGHT_PORTS none (goodword-mcp declares no smoke stack)
PREFLIGHT=PASS
  worktree: /Users/eduardopicazo/Documents/Workspace/Goodword/goodword-mcp/.worktrees/spec
```

## R1 — resumed mcp preflight, `ARCHON_REPO` UNSET

The defect this closes: before the change, preflight resolved the repo from the environment while the
resolver adopted the binding, so a resumed run checked `api/.git` and then targeted the mcp worktree.

```
PARAMS=OK spec=<tmp>/spec.md slug=spec repo=goodword-mcp (adopted) branch=archon/spec api_port=none
PREFLIGHT_PORTS none (goodword-mcp declares no smoke stack)
PREFLIGHT=PASS
  repo after resume: goodword-mcp
```

## M6 — conflicting `ARCHON_REPO` on a bound run

```
PARAMS=FAIL REPO_CONFLICT run is bound to goodword-mcp but ARCHON_REPO=api
  (rebinding a live run is not supported; start a new run or clear the artifacts dir)
  repo survived: goodword-mcp
```

## P4 — bootstrap

Not executed end to end: `bootstrap` performs `git fetch origin main` and `git worktree add` against
the real repository. Its two changed behaviours are covered instead by
`EnvSeedingStaysMandatoryForApi`, which lifts the env branch verbatim from the lane (with an anchor
test that fails when the branch changes) and executes it against controlled directories.
