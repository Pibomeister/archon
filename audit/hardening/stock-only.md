# Stock-only continuation

The supported runtime is unmodified upstream Archon. Earlier fork validation in
`continuation.md` is historical component evidence, not stock qualification.
The boundary applies to both Claude and Codex; provider-specific settings do not
substitute for isolation from controller secrets.

## Minimal implementation sequence

1. Reuse `setup/control_contract.py` for immutable approval bytes and explicit
   operator decisions. Add regression tests before correcting unsafe IO or
   malformed-input behavior. No new dependencies or service.
2. Wire these records into the existing launcher only with a verified worker
   isolation/quiescence boundary. Use stock commands; do not emulate fork-only
   `gate-evidence`, launch keys, or controller nodes inside the runtime.
3. Replace the portable lane's fork-only approval dependency with that external
   boundary; retain fail-closed publication until end-to-end validation passes.
4. Qualify both providers using disposable fixtures, then integrate existing
   candidate verification/publication and backfill components. Separate real
   provider trials from deterministic component tests.

## Current admission status

Approval snapshot helpers alone are not an approval endpoint, worker sandbox,
operator authentication, native gate compare-and-swap, or release certificate.
They require a trusted caller, private key/storage inaccessible to workers, and
already-quiescent artifact exports. Native paused status is insufficient to prove
all worker processes are stopped. Existing publication/install/backfill refusal
paths remain unchanged. The separately permissioned portable lane still contains
fork-only checks and must not be represented as stock-qualified.

No live approval, installation, publication, or production backfill is authorized
by this document. Full hardening remains incomplete until the above wiring and
provider-neutral end-to-end checks are verified.

## Verified approval-record increment

Changed `setup/control_contract.py` and its focused regression test. The shared
helpers capture allowlisted bytes, authenticate snapshots with a caller-owned key,
record explicit approved/rejected decisions, detect drift, and replay identical
concurrent decisions without overwriting conflicting records. Both provider values
are tested. Nonregular files, symlinks, hardlinks, malformed metadata and oversized
input fail closed; interrupted writes remove temporary files.

The change reuses the existing private JSON/HMAC utilities. No launcher command,
runtime modification, dependency, service, or admission switch was added.
`stock-approval-tests.log` records the component-suite result; it is not a stock
runtime E2E result. Pyright and Python compilation pass for the changed Python
files, and the diff whitespace check passes. Independent review findings about
capture limits, concurrent decision replay, and malformed decision types were
reproduced with failing regressions and corrected.
