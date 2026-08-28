<repo-conventions repo="api">
Repo: /Users/eduardopicazo/Documents/Workspace/Goodword/api (its own git repo — never stage or commit across repo boundaries; always `git -C` with the absolute path).
Package manager: bun@1.3.10. Node: 22.16.0 exactly (via mise).
Unit tests: `bun run test "<pattern>"` — NEVER bare `bun test`. `test` is a bun builtin, so bare `bun test` invokes bun's own runner instead of jest and silently breaks the suite's mocks. (api/CLAUDE.md:32 documents the bare form; that line is wrong.)
Integration tests: `bun run test:integration "<pattern>"` — SERIAL-ONLY. The group shares one e2e Docker/Postgres stack; two concurrent integration runs destroy each other ("No such container" is the signature). Never run in parallel with any other node that runs integration tests.
Typecheck: `bun run typecheck` (tsgo) — NOT `build:fast`, which is esbuild-only and typechecks nothing.
Lint: `bun run lint` (biome).
`git push` triggers a husky pre-push hook running typecheck + test:pre-push (multi-minute, historically flaky in hook git env): either budget the push node's timeout for it, or gate explicitly first and push `--no-verify`.
TypeScript strict mode. Unit specs live in `__tests__/` subdirectories.
</repo-conventions>
