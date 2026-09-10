# Testing checkpoint

This checkpoint is for development/integration testing, not production admission.

## Source checkouts

| Component | Remote | Branch | Revision |
|---|---|---|---|
| Workflow layer | GoodwordTeam/archon and Pibomeister/archon | main | Commit containing this document |
| Runtime fork | Pibomeister/archon-engine | hardening/controller-boundary | `9114ab3689d8ce4a325e1ea0ad7ed84bf0638706` |
| API ledger counterpart | GoodwordTeam/api | checkpoint/archon-backfill-ledger-20260909 | `73d7b4679109ca420d31f5d6190ad958dea569e2` |

The runtime checkpoint includes remote Oxc and proxy-budget changes through `634beb0e` plus controller-bound Node HTTP verification. Do not test against the older runtime checkpoint `6c073de0` while claiming this checkpoint's results.

## Checks repeated before push

- Workflow: 80 focused release/parameter tests; lite/Codex generation and doctrine checks.
- Reconciled runtime: 105 focused controller/observer tests passed; three opt-in observer tests were skipped in that unit invocation.
- Reconciled runtime: all three real Docker controller scenarios passed separately (static approval, static rejection, Node HTTP approval).
- Runtime full typecheck and focused lint/format/complexity checks passed, including commit hooks.
- API checkpoint was already pushed and its remote SHA was verified. Its real PostgreSQL matrix evidence remains in `backfill-db-matrix.log`.

## Test boundaries

Use the existing pinned test images and Playwright 1.60.0 modules for Docker fixtures. The source integration test documents required opt-in environment variables. Node HTTP support is for checked-in entrypoints, not arbitrary dependency installation or Nest/Vite builds.

Workflow publication, package publication, installation and production backfill admission remain disabled or unadmitted as described in `continuation.md`. The separately permissioned portable workflow is not certified by these checks. Do not run the installer or production backfills as a test workaround.

Local `.omc` caches, dependency directories, and unrelated API work are intentionally not part of this source checkpoint. No runtime binary, Docker image or database migration was installed/deployed by this push operation.

Saved logs are sanitized development-test evidence with trailing whitespace normalized, not production authorization receipts.
