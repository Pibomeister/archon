# Goodword operator playbook

This is project guidance for the Goodword folder of git repositories (api, web-app, goodword-mcp, goodword-kb). The control graph does not name these tools; you do, from this file.

## Diagnose

- use aws cli to diagnose RDS, CloudWatch logs, and deployed API evidence. If SSO is expired, evidence degrades (`EVIDENCE_AWS=DEGRADED`); do not fail the run solely for that.
- `gh` is required for draft-PR preparation. Unauthenticated `gh` is a hard preflight fail on this project.
- GitNexus MCP indexes the repo named `api`. Impact `UNAVAILABLE` on lite routes the ticket to the full lane.

## Tools

- api: bun, node 22 via mise. web-app and goodword-mcp: pnpm, node 20 via mise.
- agent-browser is required on the frontend lane for UAT.
- AWS is a soft prerequisite: warn when `aws sts get-caller-identity` fails, continue.

## Envelope

Lite tickets that hit hot paths (migrations, auth, oauth, billing, search, infra, generated api-client) route to the full lane. Thresholds are in this pack's envelope.json.
