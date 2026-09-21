<repo-conventions repo="goodword-mcp">
Use this block INSTEAD of the api one when params.json's "repo" is "goodword-mcp". params.json is authoritative; do not mix the two.
Package manager: pnpm@10.12.1 (never bun). Node 20 — prefix every command with `mise x node@20 --`.
Typecheck: `mise x node@20 -- pnpm exec tsc --noEmit`. There is NO `typecheck` script; `build` is `tsc && copy-static-assets`, so --noEmit IS the typecheck.
Lint: THERE IS NONE. Do not invent one, and do not list one under Verification.
Unit tests: `mise x node@20 -- pnpm test:unit` for the whole suite. For a pattern, the gate runs the full form with NODE_OPTIONS: jest is configured as ts-jest/presets/default-esm, and without `NODE_OPTIONS=--experimental-vm-modules` every suite dies with "Cannot use import statement outside a module" — a failure that looks like a broken test.
Test files are `*.unit.test.ts` under `tests/` or `src/**/__tests__/`.
`*.e2e.test.ts` and `*.smoke.test.ts` need a live API and a token; the gate excludes them and this node must not run them.
Transport is httpStream at /mcp with WorkOS bearer auth. TypeScript strict.
</repo-conventions>
