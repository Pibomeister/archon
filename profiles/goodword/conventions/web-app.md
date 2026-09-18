<repo-conventions repo="web-app">
Repo: the web-app git repository. Path comes from params.json (worktree). Never stage or commit across repo boundaries; always `git -C` with that absolute path.
Package manager: pnpm ONLY — `bun install` ignores the overrides block and produces SSR 500s. Node 20 (Node 25 breaks Vite); run under mise.
Tests: `pnpm test` (vitest). Typecheck: `pnpm typecheck` (react-router typegen && tsgo). Lint: `pnpm lint` (biome).
API client regen: `pnpm gen:api` requires the api running; NEVER full-regen in the workflow — selective-patch only (full regen rewrites hundreds of unrelated lines and breaks typecheck). `pnpm install` dirties pnpm-lock.yaml (known main drift) — exclude the lockfile from any automated commit.
No co-author/attribution lines in commits (repo convention, applied repo-wide).
TypeScript strict mode.
</repo-conventions>
