# Hardening continuation — 2026-09-10

Baseline: workflow main 258d848; runtime hardening/controller-boundary 6c073de0.
The original 11-finding remediation plan remains the objective; this is not a release certificate.

## Immediate boundary repair plan

The stock-runtime compatibility checkpoint restored direct publication in the legacy ship nodes, although installation/package publication and backfill execution remain disabled. Preserve stock parser compatibility, but remove unverified push/PR commands until trusted controller dispatch is admitted. Regression first: all legacy ship nodes must return the explicit disabled result without external commands, and remain always-run on resume. Update authoritative parents and regenerate twins/lite variants; retain the existing controller library for future trusted wiring. This is a stopgap, not agent-resistant isolation or finding closure.

Independent slices: runtime candidate application verification; isolated real-PostgreSQL backfill regression matrix. Do not touch unrelated API work or runtime .omc files, run production workflows, change installed binaries/config, or enable admission.

## Remaining closure requirements

Existing small fixes, semantic handoffs and no-change routing need revalidation on the consolidated baseline. Runtime declared repository seeds already exist: historical notes saying none are wired are not current implementation evidence. Final controller publication/backfill handlers still refuse; application verification is currently static-web-only. All 11 findings stay open until their complete source-to-runtime path and required integration tests are proven. Never accept fabricated controller receipts as E2E proof.

## Parameter boundary repair plan

Current params-env.sh interpolates ports without shell quoting and can emit only partial assignments after a JSON error; callers eval the output and lose the helper exit status. Add regression cases for malicious ports, invalid/missing inputs and quoted missing filenames, then emit assignments atomically after validation and a fixed eval-able refusal on any failure. Preserve repo-profile semantics and legacy defaults. This reduces accidental/host-shell exposure, but does not replace required container isolation.

## Verified increment

- Legacy top-level ship nodes now stop with `SHIP=DISABLED`, are always-run, and invoke no external commands. Parents/lite manifests declare their unadmitted state; generated variants and doctrine checks pass. This intentionally retains stock node syntax, not certified stock parser execution. The separately permissioned portable workflow is outside this stopgap, not implicitly hardened.
- `params-env.sh` now validates a complete JSON object before emitting quoted assignments. Ports require valid integer ranges; only an absent repo key defaults to API. Malformed inputs emit an eval-able refusal. The before/after logs record actual synthetic shell-injection failures and passing regressions.
- Runtime `node-http-app-v1` starts a checked-in Node entrypoint from the imported read-only candidate. Startup is chosen by protected operator policy, included in the signed action manifest, candidate digest and sealed blackbox receipt; `node --` and a controlled PORT prevent option interpretation. It does not install dependencies or build arbitrary Nest/Vite applications.
- An independent review caught a missing startup field between policy parsing and manifest construction; fixed with a controller action regression. Actual Docker tests now cover static approve, static reject, Node approve, receipt binding and replay, rather than only mocked observer output.
- The real PostgreSQL primary/replica failure matrix passed against clean API checkpoint `73d7b4679109ca420d31f5d6190ad958dea569e2`. Its hash/provenance and output are retained in `backfill-db-matrix.log`; this validates the DB component, not production dispatch or migration admission.

### Validation

80 focused release/parameter tests; 27 backfill tests with the real DB matrix opted in; 67 runtime controller unit tests; 38 observer unit tests (separate Docker opt-ins recorded explicitly); three real Docker controller scenarios; one real Docker Node observer scenario. Additional generator, doctrine, package-manifest, fullstack-contract and controller-attestation checks passed. Runtime typecheck, lint, formatting and Oxlint complexity cap 20 checked. Logs beside this file distinguish concrete fixtures from real product/provider E2E.

### Still incomplete

Trusted runtime publication and backfill dispatch remain refusing. General application build/dependency environments, full API/web and real-provider trials, release certification and production admission remain outstanding. No live install, production workflow, approval, push, PR or migration was executed; no API checkout was modified. Workflow edits remain on main; runtime edits stay on the existing hardening branch. Do not treat this increment as closure of the entire original plan.
