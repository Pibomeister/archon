# Node nondeterminism audit

Every bash node covered by `setup/tests/test_node_stress.py` was read for the
sources that make a node's output depend on something other than its inputs:
scans of shared `/tmp`, `date`, `$RANDOM`, `mktemp`, `git status` ordering,
`sort` without a pinned collation, and reads of ambient environment
(`NODE_ENV`, `DISABLE_OMC`, `HOME`, `TZ`, `LANG`, `LC_*`).

Covered nodes:

| Workflow | Nodes |
|---|---|
| `full-sdlc-api` | `review-gate`, `plan-round-pre`, `plan-critic-gate`, `plan-converge`, `deslop-recheck`, `deslop-review-gate`, `gate-tests`, `converge` |
| `full-sdlc-web` | `review-gate`, `gate-tests` |
| `bugfix` | `review-gate`, `rca-gate`, `rca-plan-shape`, `rca-round-pre`, `rca-critic-gate`, `rca-converge`, `deslop-recheck`, `deslop-review-gate`, `deslop-commit`, `green-check`, `converge` |

Coverage as swept at `NODE_STRESS=100` (a "group" is one fixture run N times;
`N=1` groups are the `EnvInvariance` baseline/perturbed pairs):

| Node | Groups | Executions |
|---|---|---|
| `full-sdlc-api:review-gate` | 14 | 1202 |
| `full-sdlc-web:review-gate` | 11 | 1100 |
| `bugfix:review-gate` | 11 | 1100 |
| `full-sdlc-api:converge` | 9 | 702 |
| `bugfix:converge` | 7 | 700 |
| `bugfix:rca-converge` | 6 | 600 |
| `full-sdlc-api:plan-converge` | 6 | 600 |
| `full-sdlc-api:gate-tests` | 5 | 302 |
| `full-sdlc-api:deslop-recheck` | 4 | 202 |
| `full-sdlc-api:plan-round-pre` | 4 | 400 |
| `bugfix:deslop-commit` | 3 | 300 |
| `bugfix:green-check` | 3 | 300 |
| `bugfix:rca-round-pre` | 3 | 300 |
| `full-sdlc-api:deslop-review-gate` | 3 | 300 |
| `bugfix:deslop-recheck` | 2 | 200 |
| `bugfix:deslop-review-gate` | 2 | 200 |
| `bugfix:rca-critic-gate` | 2 | 200 |
| `bugfix:rca-plan-shape` | 2 | 200 |
| `full-sdlc-api:plan-critic-gate` | 2 | 200 |
| `bugfix:rca-gate` | 1 | 100 |
| `full-sdlc-web:gate-tests` | 1 | 100 |
| **total** | **101** | **9308** |

Every group reported `identical=<N> untyped_exits=0`; zero non-identical runs,
zero untyped exits. Swept in per-class chunks (the harness kills a detached
process between turns, and the whole module at N=100 exceeds a single 10-minute
window):

```
NODE_STRESS=100 python3 -m unittest discover -s setup/tests -p 'test_node_stress.py' -k <Class>
```

Wall time, macOS 15.5 / 14 cores: ReviewGateStress 29s, ReviewGateScanIsolation
8s, EnvInvariance 4s, PlanLoopStress (+RcaPlanLoopStress) 137s, GreenCheckStress
53s, ConvergeStress 51s, GateTestsStress 217s, DeslopStress 367s — **866s
total** for 9308 node executions. (Chunk wall times vary run to run with
machine load; an earlier identical sweep of the same 101 groups came in at
595s. The group and execution counts, and the zero anomalies, were identical
both times.) The unchunked default sweep is
`NODE_STRESS=100 python3 -m unittest discover -s setup/tests -p
'test_node_stress.py'` and takes about the same time in one process.

Verdict vocabulary: **fixed** (a real source, a RED test, then a YAML change),
**harmless** (present but cannot change the node's typed output or artifacts),
**normalized** (real, but not part of any contract — the harness compares the
contract instead), **recorded** (real, not fixed here; the reason is stated).

---

## Findings

| # | Node(s) | Source | Verdict | Why |
|---|---|---|---|---|
| RG-1 | `review-gate` and `round-pre`, all three lanes (6 sites) | `ls -1d /tmp/compound-engineering/ce-code-review/*/ \| sort` | **fixed** | Two defects in one line — see below. |
| RG-2 | `review-gate`, all three lanes | a genuinely NEW ce-code-review dir belonging to another run | **harmless** | The `head_sha` prefix match already rejects it. Pinned by `test_new_dir_from_a_different_head_is_ignored`. |
| RG-3 | `review-gate`, all three lanes | `for d in $NEW` — unquoted word split over the dir list | **recorded** | A run dir whose path contains a space splits into two words; both then fail `test -f "$M"` and are skipped. Deterministic (same input, same output), so it is not a stress finding; it is a robustness one. CE names run dirs by timestamp, so the case does not arise today. Fixing it means a `while IFS= read -r` loop; left alone rather than churn the discovery block that RG-1 just touched. |
| RG-4 | `review-gate`, all three lanes | `CE_REVIEW_ROOT` is honored for READS only | **recorded** | See "Residual" under RG-1. |
| CONV-1 | `converge`, `full-sdlc-api` + `bugfix` | `exec > >(tee "$RD/converge.txt") 2>&1` | **harmless in-harness; see caveat** | Process substitution: bash does not reap the `tee` child, so in principle the shell can exit before `tee` has flushed the last lines. Measured at `NODE_STRESS=100` over 16 converge fixtures = **1402 executions**: exit code, typed lines and `round-N/converge.txt` byte-identical every time, discriminator line always present, zero truncation. **Caveat on what that proves:** the harness reads the node's stdout pipe to EOF, and the `tee` child holds the write end, so the harness necessarily waits for `tee` — it cannot observe a reader that stops at `waitpid()` instead. The result rules out a *file*-side flush race (`converge.txt` is what RUNBOOK.md §3 tells operators to read); it does not prove the archon engine's own capture is immune. Left as is: the tee is what makes the discriminator survive at all, since archon persists bash stderr but not stdout. |
| CONV-2 | `converge`, `gate-tests`, `deslop-recheck`, `deslop-commit` | `git status --porcelain` line ordering | **harmless** | git emits porcelain in byte order irrespective of `LC_COLLATE`. Verified directly: `Beta.ts Zulu.ts able.ts alpha.ts` come back in that same order under both `LC_ALL=C` and `LC_ALL=en_US.UTF-8`, whereas `sort` reorders them. |
| DS-1 | `deslop-recheck`, both lanes | `git archive "$CKTREE" > "$RD/checkpoint.tar"` | **normalized** | Archiving a bare *tree* has no commit date to take, so git stamps the CURRENT time into every member header: two runs over a byte-identical tree produce different tar bytes. The tar is a belt artifact for restoring bytes; the contract is the checkpoint TREE SHA, which is compared separately and is deterministic. Not fixed in the YAML because `git archive --mtime` needs git ≥ 2.39 and `install.sh` does not pin a git version. `runner._tar_digest` compares every member's name, type, mode, size and content hash instead, so a changed file still fails the run. |
| DS-2 | `deslop-recheck`, both lanes | `GIT_INDEX_FILE="$RD/index" git add -A` leaves a binary index in the artifacts dir | **normalized** | A git index carries per-file stat data (inode, ctime), so its bytes differ run to run. It is a throwaway staging file; the tree sha written out of it is deterministic and is the thing `deslop-review-gate` compares. The harness records it as `<BINARY bytes=N>`. |
| DS-3 | `deslop-review-gate`, both lanes | recomputes HEAD / live-index tree / full-tree checkpoint and `cmp`s | **harmless** | All three are content-addressed. Pinned in both directions: the CLEAN fixtures assert the compare passes, and `test_deslop_review_gate_api_detects_a_reviewer_edit` asserts a one-byte worktree edit trips `DESLOP_REVIEW=FAIL reviewer modified tree`. |
| GT-1 | `gate-tests`, `deslop-recheck` | `rm -rf .omc` inside `$WT` | **harmless** | Idempotent, and load-bearing: it is what keeps agent session state out of the checkpoint and out of the scope guard. |
| GT-2 | `gate-tests`, `full-sdlc-web` | `cd <goodword>/web-app/.worktrees/archon-toy` hardcoded | **recorded** | Not a determinism defect — a parameterization gap. The lane is toy-pinned by design (RUNBOOK.md §3, "Web-lane parity note"). The harness binds it with `run_node(subs=…)`; parameterizing the lane is the tracked follow-up, not this task. |
| ENV-1 | every covered node | `NODE_ENV`, `DISABLE_OMC`, `HOME`, `TZ`, `LANG`, `LC_*` | **harmless** | No covered body reads any of them; `$HOME` appears only in `preflight` (not covered). Asserted, not assumed: `EnvInvariance` runs `review-gate`, `converge`, `gate-tests` and `deslop-recheck` under `NODE_ENV=production DISABLE_OMC=1 TZ=Asia/Tokyo LANG=LC_ALL=tr_TR.UTF-8` and requires the same rc and the same typed lines as the default environment. |
| ENV-2 | `gate-tests`, `deslop-recheck`, `green-check` | PATH-resolved `bun` / `pnpm` / `mise` | **by design** | The node's verdict is supposed to depend on the toolchain. The harness stubs them with deterministic shims so what is being measured is the node, not the toolchain. |
| PY-1 | `check-scope.py`, `check-slop.py`, `parse-critique.py` | set / dict iteration under `PYTHONHASHSEED` randomization | **harmless** | Every printed collection is either `sorted()` (`check-scope.py:45`, `check-slop.py:101`) or an insertion-ordered dict/list built by scanning lines in order (`check-slop.py:159`). Sets appear only in membership tests and `len()`. |
| — | all covered nodes | `date`, `$RANDOM`, `mktemp` | **not present** | Grepped across all 21 covered bodies; zero hits. |

---

## RG-1 in full

`round-pre` writes `prerun-dirs.txt`; `review-gate` writes `post-dirs.txt` and
takes `comm -13` between them to find the ce-code-review run dir this round
created. The line that produced both had two problems.

**(a) A fixed, world-shared root.** `/tmp/compound-engineering/ce-code-review`
is not overridable, so the node cannot be exercised hermetically at all, and two
runs on one host — or two users on a shared host — scan the same directory. The
body's own comment already flags the shared-`/tmp` hazard for the *output* file;
the *input* root had the same shape.

**(b) `sort` with no pinned collation.** `round-pre` and `review-gate` are
separate node executions. A run started in one shell and resumed from another
(`archon workflow resume`, the documented normal path) can collate the two
listings differently, and `comm` then reports a **pre-existing** dir as new —
silently, exit 0, no warning. Verified on macOS 15.5:

```
$ mkdir ce/ce-Beta ce/ce-alpha
$ LC_ALL=C           ls -1d ce/*/ | sort   ->  ce-Beta, ce-alpha
$ LC_ALL=en_US.UTF-8 ls -1d ce/*/ | sort   ->  ce-alpha, ce-Beta
$ LC_ALL=en_US.UTF-8 comm -13 <C-sorted> <UTF8-sorted>
ce/ce-alpha/                               # phantom "new" dir, rc 0
```

If that phantom dir's `metadata.json` happens to carry a `head_sha` that
prefix-matches this run's `pre-head.txt` — two lanes reviewing the same commit —
the gate reads a **foreign run's verdict** and reports it as this round's.

**Fix.** At all six sites in the three lanes:

```diff
-ls -1d /tmp/compound-engineering/ce-code-review/*/ 2>/dev/null | sort > "$RD/post-dirs.txt"
+ls -1d "${CE_REVIEW_ROOT:-/tmp/compound-engineering/ce-code-review}"/*/ 2>/dev/null | LC_ALL=C sort > "$RD/post-dirs.txt"
```

Same shape applied to the two sibling sites in `workflows/wrap-review.yaml`,
which additionally wrote its post listing to a fixed, shared
`/tmp/wrap-review-post.txt` — now `$ARTIFACTS_DIR/post-dirs.txt`, matching what
the three lanes already do.

**Tests.** `ReviewGateScanIsolation`:
`test_prerun_listing_in_another_collation_yields_no_phantom_new_dir` (all three
lanes) asserts the verdict still comes from the envelope, and
`test_negative_control_unguarded_sort_reads_the_foreign_dir` reverts
`LC_ALL=C sort` to a bare `sort` through `run_node(subs=…)` and asserts the bug
**does** reproduce. If that negative control ever starts passing, the fixture
stopped reproducing and the guard test above is proving nothing.

**Residual, not fixed.** `CE_REVIEW_ROOT` is honored on the *read* side only.
The `ce-code-review` skill still writes to the default root, so the variable is
an isolation and override affordance (it is what makes the node testable), not
yet a per-run guarantee. Two concurrent lanes reviewing the **same** head sha can
still each see the other's dir as new, and the `head_sha` prefix match cannot
separate them — it is the same sha. Closing that needs the skill to honor the
same variable, or the gate to match on a run id it minted itself; both are
outside this task.
