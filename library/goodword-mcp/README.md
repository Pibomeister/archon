# Skills library: goodword-mcp

Repository-specific knowledge compiled from Archon runs on `goodword-mcp`. Three
states, one rule each:

- **raw/** holds pointers to run artifacts (`raw/index.jsonl`), never copies.
  Absolute artifact paths live only here; this directory is never packaged.
- **wiki/** holds pattern pages a maintainer agent proposes and
  `setup/wiki-apply.py` gates. Pages are never deleted and never shown to a
  runtime agent. `wiki/index.md` is generated; `wiki/log.md` and
  `wiki/skill-impact.*` are append-only ledgers.
- **skills/** holds `SKILL.md` files that `setup/stage-skills-library.py`
  renders into `skills.md` for the implement, fix and fixer nodes. At most one
  `candidate` exists per repository at a time; it is validated against the
  next K live runs and then accepted or rolled back.

Rules:

- The runtime agent sees skills only. `PURPOSE.md`, the wiki and this file are
  never staged. WikiSkill's ablation is the reason: a runtime agent that can
  read the wiki solves tasks with knowledge the skill does not hold, and the
  score stops measuring the skill.
- Rollback is asymmetric: skills roll back, the wiki never does. A page that
  later evidence contradicts is marked `contested` (quarantined from proposals)
  or `superseded`; it is never deleted. Rejected and rolled-back candidates
  keep their diff in `wiki/skill-impact.md` so the proposer never repeats one.
- Ties roll back. A candidate must beat its frozen baseline window strictly;
  neutral stepping stones are discarded by design.
- Every write is mechanical (`skill-admit.py`, `skill-score.py`,
  `wiki-apply.py`). Do not edit this directory by hand.
- Commits are authored as `archon skill-evolve <skill-evolve@archon.local>`
  and touch only `library/goodword-mcp`; `git log -- library/goodword-mcp` is the audit trail.
- No absolute home paths, no `~` paths, no URLs anywhere under `wiki/` or
  `skills/`: the library must read the same on every machine.
- The score is defined and versioned in `setup/skill-score.py`; a window whose
  runs were scored under another version is inconclusive and rolls back.
