# Fluxkeep operator playbook

Single Next.js git repository. Path comes from the machine binding.

## Diagnose

- Use the repo's own logs and Playwright/Jest output. Do not use aws or gcloud unless a later profile says so.
- There is no generated OpenAPI client and no API-client regen step.

## Tools

- pnpm@10.21.0. Verification argv is in the project profile (typecheck, lint, format, focused Jest).
- GitNexus is not required. Impact UNAVAILABLE must not eject a lite ticket.
- No smoke HTTP stack in the v1 profile.

## Scope

Only the files listed in the profile allowlist. Forbidden: .env, prisma, billing, extraction, apps/**, packages/**.
