# Repository skills library

Cross-run knowledge for the Goodword lanes, one directory per repository
(`library/api`, `library/goodword-mcp`, `library/web-app`, the names that
`setup/repo-profile.sh --list` prints). The design follows the WikiSkill
separation of evidence, belief and policy:

| State | Where | Mutation rule | Who reads it |
|---|---|---|---|
| Raw | `<repo>/raw/index.jsonl` | append-only pointers to run artifacts, never copies | `skill-evolve` agents, by following the pointer |
| Wiki | `<repo>/wiki/` | pattern pages are created and patched through `setup/wiki-apply.py`; never deleted, never reset when a skill rolls back | the wiki maintainer and the skill proposer only |
| Skills | `<repo>/skills/` | one atomic candidate per repository, validated against the next K live runs, accepted or rolled back by `setup/skill-score.py` | the `implement`, `fix` and `fixer` nodes, through `skills.md` |

## Rules

- **Evidence is not knowledge, knowledge is not policy.** Raw pointers stay
  raw; the wiki interprets them; only a `SKILL.md` reaches a live run.
- **Rollback is asymmetric.** A candidate skill that does not beat its frozen
  baseline window is restored to the pre-candidate bytes (or removed). The
  wiki keeps every page and every ledger line, including the rejected
  proposal, so the proposer never repeats it.
- **The runtime agent sees skills only.** `setup/stage-skills-library.py`
  renders `active` and `candidate` skills into `$ARTIFACTS_DIR/skills.md` and
  never opens `PURPOSE.md` or `wiki/`. Reviewers, critics and planners stay
  skill-free so the score is not contaminated.
- **One candidate per repository.** `skills/index.json` holds at most one
  `candidate`; a second proposal waits until the window closes.
- **Every write is mechanical.** `setup/skill-admit.py`, `setup/skill-score.py`
  and `setup/wiki-apply.py` are the only writers. Operators do not edit this
  tree by hand; forced rollback is `skill-admit.py rollback <repo>`.
- **Commits are attributable.** Library commits are authored as
  `archon skill-evolve <skill-evolve@archon.local>` and stage only
  `library/<repo>`. `git log -- library/<repo>` is the audit trail.
- **No absolute paths.** Nothing under `wiki/` or `skills/` may contain a home
  path, a `~` path or a URL. Absolute artifact paths live only in the raw
  ledger, which is never packaged.
- **The score is code.** `setup/skill-score.py` defines and versions the
  run score; a window that mixes score versions is inconclusive and rolls
  back rather than accepting on stale numbers.

## Packaging

Only this file ships in the installer payload. Per-repo directories are
committed in this checkout and re-created by `skill_library.ensure_skeleton`
on a fresh install, so an evolved registry is never clobbered by a re-install.

## Operator entry points

- `RUNBOOK.md` section 17: when to run `skill-evolve`, how to read
  `wiki/skill-impact.md`, forced rollback, window size, stale locks.
- `setup/skill-admit.py status <repo>` for the current candidate and counts.
- `setup/skill-score.py window <repo>` for the open scoring window.
