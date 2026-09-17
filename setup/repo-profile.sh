#!/usr/bin/env bash
# The closed repo profile — the single definition of what each repository's
# toolchain looks like, so a lane never hardcodes one repo's commands.
#
# Usage inside a node:
#   OUT=$(bash <abs>/repo-profile.sh "$REPO") || { echo "..."; exit 1; }
#   eval "$OUT"
#
# Registry discovery for launchers:
#   bash <abs>/repo-profile.sh --list
#   bash <abs>/repo-profile.sh --json
#
# CAPTURE-AND-CHECK IS MANDATORY, not stylistic. `eval "$(cmd)"` swallows a
# non-zero exit even under `set -euo pipefail` -- measured: `eval "$(false)"`
# continues. So this script ALSO emits an eval-able failure for its own known
# error branches (the params-env.sh idiom), and callers ALSO capture-and-check,
# because the emitted line cannot cover a crash that produces empty stdout.
# Two mechanisms, two failure shapes; neither alone is sufficient.
#
# Defines: REPO, CMD_INSTALL[], CMD_TYPECHECK[], CMD_LINT[], CMD_TEST[],
#          ENV_SRC, HAS_SMOKE, HAS_BROWSER, IMPACT_INDEX.
# Everything is ALWAYS defined -- empty arrays / empty strings where the
# capability does not apply -- so `set -u` consumers can test them without a
# default expansion. An empty value is a DECLARED property of the repo, never
# an inferred absence: that distinction is what stops a lane from relaxing a
# check merely because a value happened to be missing.
#
# CMD_* are ARRAYS, not strings. A string expanded unquoted puts the literal
# quote characters into argv -- measured: an ignore-regex stored as a string
# reached jest as ['\.(e2e|smoke)\.test\.ts$'] and matched nothing, so the
# e2e suite ran anyway. Arrays are the fix; call sites use "${CMD_TEST[@]}".
set -euo pipefail
REPO="${1:?usage: repo-profile.sh <repo>|--list|--json}"

python3 - "$REPO" <<'PY'
import shlex
import sys

repo = sys.argv[1]

# ENV_SRC is repo-relative; the caller joins it with the repo directory.
# "" means the repo declares no env file to seed a worktree with.
PROFILES = {
    "api": {
        "install":   ["bun", "install", "--frozen-lockfile"],
        # lockfile_scope.py: the lockfile follows package.json into scope. A
        # frozen install never rewrites it, so lockfile-only drift is a breach.
        "lockfiles": ["bun.lock", "bun.lockb"],
        "lockfile_install_drift": "",
        "typecheck": ["bun", "run", "typecheck"],
        "lint":      ["bun", "run", "lint"],
        # jest's positional arg is a testPathPattern; the `--` keeps it out of
        # bun's own argument parsing. Pattern is appended by the call site.
        "test":      ["bun", "run", "test", "--"],
        # Regexes for spec paths the unit config above IGNORES (api/jest.config.js
        # testPathIgnorePatterns). check-unit-patterns.py rejects a verify.json
        # pattern that can only select these: jest exits "No tests found".
        "unit_test_excludes": [r"\.int\.spec\.ts$", r"\.ai\.spec\.ts$", r"\.ext\.spec\.ts$",
                               r"apps/api-e2e/src/load-tests"],
        "env_src":   ".env",
        "smoke":     "1",
        "browser":   "1",
        # Paths with NO browser-reachable surface (browser-exemption.py). Only
        # these may carry a not_applicable browser policy; every other path is
        # presumed reachable. Migrations alone change no page; scripts/tools/
        # the CLI/e2e harness are operator-side; the three async lambdas never
        # run in the local smoke stack a browser check would hit. `*` crosses /.
        "browser_exempt": [
            "libs/data-access/src/lib/rds/migrations/*",
            "scripts/*", "tools/*", "docs/*",
            "apps/utilities-cli/*", "apps/api-e2e/*",
            "apps/analytic-service/*", "apps/enrichment-service/*", "apps/notification-service/*",
            "*.spec.ts", "*.md",
        ],
        # Files a framework loads by glob, so no import names their exports
        # (check-slop.py yagni). TypeORM: rds/utils.ts `migrations/*{.ts,.js}`.
        "framework_loaded": ["libs/data-access/src/lib/rds/migrations/*"],
        "impact":    "mono",
    },
    "goodword-mcp": {
        # Node 20 via mise: the system default is 25 and the repo's CI pins 20.
        "install":   ["mise", "x", "node@20", "--", "pnpm", "install", "--frozen-lockfile"],
        "lockfiles": ["pnpm-lock.yaml"],
        "lockfile_install_drift": "",
        # `build` is `tsc && copy-static-assets`, so --noEmit IS the typecheck.
        "typecheck": ["mise", "x", "node@20", "--", "pnpm", "exec", "tsc", "--noEmit"],
        # No lint script in package.json. Declared absent, not inferred.
        "lint":      [],
        # NODE_OPTIONS is not optional: jest.config.mjs is ts-jest/presets/
        # default-esm and package.json supplies it via cross-env. Without it
        # every suite dies with "Cannot use import statement outside a module"
        # -- which would fail a negative control for the wrong reason.
        # The ignore pattern excludes .e2e/.smoke suites, which need a live API
        # and a token; testMatch would otherwise admit them for a broad pattern.
        "test":      ["mise", "x", "node@20", "--", "env",
                      "NODE_OPTIONS=--experimental-vm-modules",
                      "pnpm", "exec", "jest",
                      "--testPathIgnorePatterns", r"\.(e2e|smoke)\.test\.ts$",
                      "--testPathPatterns"],
        "unit_test_excludes": [r"\.(e2e|smoke)\.test\.ts$"],
        "env_src":   "",
        # setup/mcp-smoke.sh. Declaring it is what allocates APIPORT: the port is
        # allocated only when HAS_SMOKE is non-empty (resolve-params.sh:113), and
        # the lane treats a missing APIPORT for a smoke-bearing repo as a hard
        # PREFLIGHT=FAIL. An empty value here would leave the smoke with no port.
        "smoke":     "1",
        "browser":   "",
        "browser_exempt": [],
        "impact":    "",
    },
    # web-app is NOT speculative future scope: bind-repo.py accepts it and
    # bugfix.yaml runs params-env.sh on the params file it rewrites. Omitting
    # it would break every web-app bugfix run, not just an mcp one. The web
    # lane itself uses resolve-web-params.sh and consumes none of these.
    "web-app": {
        "install":   ["mise", "x", "node@20", "--", "pnpm", "install", "--no-frozen-lockfile"],
        # The unfrozen install rewrites pnpm-lock.yaml at bootstrap (main's lock
        # drifts from package.json), so UNCOMMITTED lockfile-only drift is
        # expected here and is tolerated, never staged.
        "lockfiles": ["pnpm-lock.yaml"],
        "lockfile_install_drift": "1",
        "typecheck": ["mise", "x", "node@20", "--", "pnpm", "typecheck"],
        "lint":      ["mise", "x", "node@20", "--", "pnpm", "lint"],
        "test":      ["mise", "x", "node@20", "--", "pnpm", "test", "--run"],
        # vite.config.ts test.exclude: tests/ holds the Playwright suites.
        "unit_test_excludes": [r"(^|/)tests/"],
        "env_src":   "",
        "smoke":     "1",
        "browser":   "1",
        # Every web-app path is presumed page-reachable.
        "browser_exempt": [],
        "impact":    "",
    },
}

profile = PROFILES.get(repo)
if repo == "--list":
    for name in sorted(PROFILES):
        print(name)
    raise SystemExit(0)

if repo == "--json":
    import json
    registry = {
        "schema": "archon.repo-profiles.v1",
        "profiles": PROFILES,
        "aliases": {"web": "web-app"},
        "shorthands": {"fullstack": ["api", "web-app"]},
    }
    print(json.dumps(registry, sort_keys=True))
    raise SystemExit(0)

if profile is None:
    known = ", ".join(sorted(PROFILES))
    # Emitted as shell for the caller to eval: a bare non-zero exit here would
    # be swallowed by an `eval "$(...)"` call site.
    #
    # The repo name is UNTRUSTED and goes through shlex.quote. Interpolating it
    # raw into a single-quoted string let a value containing an apostrophe close
    # the quote and run the rest as commands -- and a trailing `#` then commented
    # out the `exit 1`, so the caller executed the payload and continued with
    # exit 0 under `set -euo pipefail`. `exit 1` is on its own line for the same
    # reason: nothing in the message can comment it out.
    print(f"echo {shlex.quote(f'REPO_PROFILE=FAIL unknown repo {repo} (known: {known})')} >&2")
    print("exit 1")
    raise SystemExit(0)


def array(name, argv):
    return f"{name}=({' '.join(shlex.quote(a) for a in argv)})"


print(f"REPO={shlex.quote(repo)}")
print(array("CMD_INSTALL", profile["install"]))
print(array("CMD_TYPECHECK", profile["typecheck"]))
print(array("CMD_LINT", profile["lint"]))
print(array("CMD_TEST", profile["test"]))
print(f"ENV_SRC={shlex.quote(profile['env_src'])}")
print(f"HAS_SMOKE={shlex.quote(profile['smoke'])}")
print(f"HAS_BROWSER={shlex.quote(profile['browser'])}")
print(f"IMPACT_INDEX={shlex.quote(profile['impact'])}")
PY
