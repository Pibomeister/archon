## Shipping

All changes ship through the no-mistakes gate:

- Commit on a feature branch, then push with `git push no-mistakes <branch>`. Never push directly to `origin`.
- no-mistakes runs review, test, lint and document checks, then pushes to origin and opens the PR. Follow progress with `no-mistakes status` or the `/no-mistakes` skill.
- Per-repo gate commands live in `.no-mistakes.yaml`. It is only read from the default branch.
