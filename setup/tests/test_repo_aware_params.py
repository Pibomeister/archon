#!/usr/bin/env python3
"""The api lane became repo-aware; these pin that it did not become api-broken.

Two kinds of test, deliberately not mixed:

  PRESERVATION (test_p*) asserts something the api or bugfix lane does TODAY is
  unchanged. It must pass against the old implementation too -- a preservation
  test that only passes after the change is misfiled, and proves nothing about
  preservation.

  ACCEPTANCE / MUTATION (test_a*, test_m*) asserts behaviour that did not exist
  before, or that a deliberate break is caught. Mutation tests carry a matched
  anchor and a passing control, because a command broken by its ENVIRONMENT
  fails a negative control for the wrong reason -- which is how an earlier draft
  of the mcp test command (missing NODE_OPTIONS, so every suite died with
  "Cannot use import statement outside a module") survived a review pass.

Expected command strings come from the workflow YAML itself, not from
repo-profile.sh, so this cannot pass by both sides agreeing on the same bug.
"""
import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ARCHON = Path(__file__).resolve().parent.parent.parent
SETUP = ARCHON / "setup"
WORKFLOWS = ARCHON / "workflows"

# The api command lines this change replaced, transcribed from full-sdlc-api.yaml
# as it stood beforehand. Spec literals, not values recomputed from the profile.
API_COMMANDS_BEFORE = {
    "CMD_INSTALL": ["bun", "install", "--frozen-lockfile"],
    "CMD_TYPECHECK": ["bun", "run", "typecheck"],
    "CMD_LINT": ["bun", "run", "lint"],
    "CMD_TEST": ["bun", "run", "test", "--"],
}


def profile(repo, setup_dir=SETUP):
    """Resolve a profile through bash exactly as a node does, and read the argv
    back. Arrays are read via printf so quoting defects surface as extra
    arguments rather than being normalised away by the test."""
    script = (
        'set -euo pipefail\n'
        f'OUT=$(bash "{setup_dir}/repo-profile.sh" "{repo}")\n'
        'eval "$OUT"\n'
        'for n in CMD_INSTALL CMD_TYPECHECK CMD_LINT CMD_TEST; do\n'
        '  eval "arr=(\\"\\${$n[@]}\\")"\n'
        '  printf "%s\\n" "$n"\n'
        '  if [ "${#arr[@]}" -gt 0 ]; then printf "  %s\\n" "${arr[@]}"; fi\n'
        '  printf "END\\n"\n'
        'done\n'
        'printf "SCALARS\\n%s\\n%s\\n%s\\n%s\\n%s\\n" '
        '"$REPO" "$ENV_SRC" "$HAS_SMOKE" "$HAS_BROWSER" "$IMPACT_INDEX"\n'
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, encoding="utf-8")
    if r.returncode != 0:
        raise AssertionError(f"profile({repo}) failed: {r.stdout}{r.stderr}")
    out, cur, name = {}, [], None
    lines = r.stdout.splitlines()
    i = 0
    while i < len(lines) and lines[i] != "SCALARS":
        line = lines[i]
        if line == "END":
            out[name] = cur
            cur, name = [], None
        elif line.startswith("  "):
            cur.append(line[2:])
        else:
            name = line
        i += 1
    scalars = lines[i + 1:i + 6]
    out["_scalars"] = dict(zip(
        ("REPO", "ENV_SRC", "HAS_SMOKE", "HAS_BROWSER", "IMPACT_INDEX"), scalars))
    return out


def recheck_writer():
    """The production recheck.json writer, EXTRACTED FROM THE LANE at test time.

    An earlier version was a copy pasted into this file, so breaking the real
    writer could not fail the test -- the exact defect this suite is supposed to
    catch elsewhere.
    """
    body = node_bash("deslop-recheck")
    start = body.index('python3 - "$RD/recheck.json"')
    start = body.index("\n", start) + 1
    end = body.index("\nPY\n", start)
    return body[start:end]


def _walk(nodes):
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        yield n
        for key in ("loop_group", "body"):
            v = n.get(key)
            if isinstance(v, dict):
                yield from _walk(v.get("nodes"))
            elif isinstance(v, list):
                yield from _walk(v)


def node_bash(node_id, lane="full-sdlc-api.yaml"):
    import yaml
    doc = yaml.safe_load((WORKFLOWS / lane).read_text(encoding="utf-8"))
    for n in _walk(doc.get("nodes")):
        if n.get("id") == node_id and n.get("bash"):
            return n["bash"]
    raise AssertionError(f"no bash node {node_id} in {lane}")


def node_prompt_containing(lane_text, needle):
    """The prompt block that carries `needle`. Each node sees only its own prompt,
    so 'the instruction exists somewhere in the lane' is not the same as 'this node
    was told' -- which is how a deslop node ended up pointed at another node's
    conventions block."""
    import yaml
    doc = yaml.safe_load(lane_text)
    for n in _walk(doc.get("nodes")):
        if n.get("prompt") and needle in n["prompt"]:
            return n["prompt"]
    raise AssertionError(f"no prompt contains {needle!r}")


def shim(bin_dir, name, body):
    """A fake executable on PATH that records what it was asked to do."""
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def run_node(body, fake, env=None, timeout=None):
    """Execute a node's bash with ARTIFACTS_DIR pointed at the fixture. The lane
    hardcodes absolute setup paths, so this exercises the real helpers."""
    e = dict(os.environ)
    e.pop("ARCHON_REPO", None)
    e["ARTIFACTS_DIR"] = str(fake.ad)
    e.update(env or {})
    try:
        return subprocess.run(["bash", "-c", body], capture_output=True,
                              encoding="utf-8", env=e, timeout=timeout)
    except subprocess.TimeoutExpired as exp:
        # A node that ran long enough to time out is a node that got PAST the
        # skip branch, which is exactly what the api control asserts.
        class _Timed:
            returncode = None
            stdout = exp.stdout.decode() if isinstance(exp.stdout, bytes) else (exp.stdout or "")
            stderr = exp.stderr.decode() if isinstance(exp.stderr, bytes) else (exp.stderr or "")
        return _Timed()


class FakeRoot:
    """A throwaway <root>/.archon/setup with just the resolver's dependencies."""

    FILES = ("port-alloc.sh", "resolve-params.sh", "params-env.sh", "repo-profile.sh")

    def __enter__(self):
        self.td = Path(tempfile.mkdtemp())
        self.root = self.td / "root"
        self.setup = self.root / ".archon" / "setup"
        self.setup.mkdir(parents=True)
        for name in self.FILES:
            shutil.copy(SETUP / name, self.setup / name)
        self.spec = self.td / "ENG-0000-fixture.md"
        self.spec.write_text("# fixture\n", encoding="utf-8")
        self.ad = self.td / "ad"
        self.ad.mkdir()
        return self

    def __exit__(self, *_exc):
        shutil.rmtree(self.td, ignore_errors=True)

    def resolve(self, *extra, repo=None, bases=("4123",)):
        env = dict(os.environ)
        env.pop("ARCHON_REPO", None)
        if repo is not None:
            env["ARCHON_REPO"] = repo
        return subprocess.run(
            ["bash", str(self.setup / "resolve-params.sh"), str(self.root),
             str(self.spec), str(self.ad), *bases, *extra],
            capture_output=True, encoding="utf-8", env=env)

    def params(self):
        return json.loads((self.ad / "params.json").read_text(encoding="utf-8"))

    def write_params(self, **fields):
        base = {"spec": str(self.spec), "slug": "eng-0000-fixture",
                "branch": "archon/eng-0000-fixture",
                "worktree": str(self.root / "api/.worktrees/eng-0000-fixture")}
        base.update(fields)
        (self.ad / "params.json").write_text(json.dumps(base), encoding="utf-8")

    def env_from_params(self):
        r = subprocess.run(
            ["bash", "-c",
             f'set -euo pipefail\neval "$(bash {self.setup}/params-env.sh '
             f'{self.ad}/params.json)"\n'
             'printf "%s\\n" "$SPEC" "$SLUG" "$BR" "$WT" "$APIPORT" "$WEBPORT" "$REPO"'],
            capture_output=True, encoding="utf-8")
        return r


class Preservation(unittest.TestCase):
    """Must pass before AND after. No negative control applies to these."""

    def test_p1_api_is_the_default_and_keeps_its_worktree(self):
        with FakeRoot() as f:
            r = f.resolve()
            self.assertEqual(0, r.returncode, r.stdout + r.stderr)
            p = f.params()
            self.assertEqual("api", p["repo"])
            self.assertTrue(p["worktree"].endswith("/api/.worktrees/eng-0000-fixture"),
                            p["worktree"])
            self.assertEqual("archon/eng-0000-fixture", p["branch"])
            self.assertIn("api_port", p)

    def test_p2_api_commands_are_byte_identical_to_the_literals_replaced(self):
        got = profile("api")
        for name, expected in API_COMMANDS_BEFORE.items():
            with self.subTest(command=name):
                self.assertEqual(expected, got[name])

    def test_p2b_the_yaml_no_longer_hardcodes_bun_in_bash_but_the_profile_supplies_it(self):
        """Guards the pair: the lane stopped naming bun, and the profile now does.
        Passing only half of this is how a lane silently runs the wrong toolchain."""
        text = (WORKFLOWS / "full-sdlc-api.yaml").read_text(encoding="utf-8")
        for literal in ('bun run typecheck || {', 'bun run lint || {',
                        'bun run test -- "$PAT"', 'bun install --frozen-lockfile'):
            with self.subTest(literal=literal):
                self.assertNotIn(literal, text,
                                 "bash block still hardcodes the api toolchain")
        self.assertEqual(["bun", "run", "typecheck"], profile("api")["CMD_TYPECHECK"])

    def test_p6_api_declares_a_smoke_stack_and_a_browser_surface(self):
        s = profile("api")["_scalars"]
        self.assertEqual("1", s["HAS_SMOKE"])
        self.assertEqual("1", s["HAS_BROWSER"])
        self.assertEqual(".env", s["ENV_SRC"])
        self.assertEqual("mono", s["IMPACT_INDEX"])

    def test_p6_api_port_allocation_failure_still_fails_and_is_not_a_smoke_skip(self):
        """The relaxation this change introduced is keyed on the repo's declared
        capability, so a repo that DOES declare a smoke stack must still die when
        its port cannot be allocated -- it must never fall through to the
        'no smoke' path."""
        with FakeRoot() as f:
            control = f.resolve()
            self.assertEqual(0, control.returncode, "control must pass")
            (f.ad / "params.json").unlink()
            (f.setup / "port-alloc.sh").write_text(
                "echo 'no free port' >&2\nexit 1\n", encoding="utf-8")
            r = f.resolve()
            self.assertEqual(1, r.returncode, r.stdout + r.stderr)
            self.assertIn("cannot allocate api port", r.stdout + r.stderr)
            self.assertNotIn("NOT_APPLICABLE", r.stdout + r.stderr)
            self.assertFalse((f.ad / "params.json").exists())

    def test_p6b_a_repo_with_no_smoke_never_touches_the_allocator(self):
        """Control for the case above: mcp must succeed even with a broken
        allocator, which is what proves the skip is capability-driven rather than
        failure-driven."""
        with FakeRoot() as f:
            (f.setup / "port-alloc.sh").write_text(
                "echo 'no free port' >&2\nexit 1\n", encoding="utf-8")
            r = f.resolve("--allow", "api,goodword-mcp", repo="goodword-mcp")
            self.assertEqual(0, r.returncode, r.stdout + r.stderr)
            self.assertNotIn("api_port", f.params())

    def test_p8a_params_env_still_exports_the_original_six_for_a_web_bound_run(self):
        with FakeRoot() as f:
            f.write_params(repo="web-app", api_port=4124, web_port=3124,
                           worktree=str(f.root / "web-app/.worktrees/eng-0000-fixture"))
            r = f.env_from_params()
            self.assertEqual(0, r.returncode, r.stdout + r.stderr)
            spec, slug, br, wt, ap, wp, repo = r.stdout.splitlines()[:7]
            self.assertEqual(str(f.spec), spec)
            self.assertEqual("eng-0000-fixture", slug)
            self.assertEqual("archon/eng-0000-fixture", br)
            self.assertTrue(wt.endswith("/web-app/.worktrees/eng-0000-fixture"), wt)
            self.assertEqual("4124", ap)
            self.assertEqual("3124", wp)
            self.assertEqual("web-app", repo)

    def test_p9_params_without_a_repo_key_resolve_to_api(self):
        """A params.json written before repo-awareness, or a resumed legacy run."""
        with FakeRoot() as f:
            f.write_params(api_port=4133)
            r = f.env_from_params()
            self.assertEqual(0, r.returncode, r.stdout + r.stderr)
            self.assertEqual("api", r.stdout.splitlines()[6])

    def test_p11_bugfix_two_positional_bases_still_parse(self):
        with FakeRoot() as f:
            r = f.resolve(bases=("4124", "3124"))
            self.assertEqual(0, r.returncode, r.stdout + r.stderr)
            p = f.params()
            self.assertIn("api_port", p)
            self.assertIn("web_port", p)


class Acceptance(unittest.TestCase):
    """Behaviour that did not exist before. Reverting these trivially fails."""

    def test_a1_web_app_profile_exists_because_bind_repo_can_select_it(self):
        """bind-repo.py accepts web-app and bugfix.yaml runs params-env.sh on the
        file it rewrites, so omitting the row breaks web-app bugfix runs."""
        self.assertIn("web-app", (SETUP / "bind-repo.py").read_text(encoding="utf-8"))
        got = profile("web-app")
        self.assertEqual(["mise", "x", "node@20", "--", "pnpm", "lint"], got["CMD_LINT"])
        self.assertEqual("1", got["_scalars"]["HAS_SMOKE"])

    def test_a3_mcp_declares_no_lint_no_smoke_no_browser_no_impact_index(self):
        s = profile("goodword-mcp")["_scalars"]
        self.assertEqual("", s["HAS_SMOKE"])
        self.assertEqual("", s["HAS_BROWSER"])
        self.assertEqual("", s["ENV_SRC"])
        self.assertEqual("", s["IMPACT_INDEX"])
        # Length 0, not a one-element array holding "": the gate branches on
        # ${#CMD_LINT[@]} -gt 0, so ('') would make it try to run an empty command.
        self.assertEqual([], profile("goodword-mcp")["CMD_LINT"])
        self.assertEqual(3, len(profile("api")["CMD_LINT"]))

    def test_a3b_mcp_test_command_carries_node_options_and_excludes_non_unit(self):
        """Both halves were defects in earlier drafts: without NODE_OPTIONS every
        suite dies on ESM, and with the ignore regex stored as a string the shell
        passed literal apostrophes so the exclusion silently matched nothing."""
        argv = profile("goodword-mcp")["CMD_TEST"]
        self.assertIn("NODE_OPTIONS=--experimental-vm-modules", argv)
        self.assertIn("--testPathIgnorePatterns", argv)
        pattern = argv[argv.index("--testPathIgnorePatterns") + 1]
        self.assertEqual(r"\.(e2e|smoke)\.test\.ts$", pattern,
                         "regex reached argv with shell quoting baked in")
        self.assertEqual("--testPathPatterns", argv[-1],
                         "the caller appends the pattern, so it must come last")

    def test_a8_a_bound_run_is_adopted_without_an_allow_list(self):
        """bugfix.yaml passes no --allow and bind-repo.py rewrites repo=web-app,
        so applying the default allowlist on resume would reject the run."""
        with FakeRoot() as f:
            f.write_params(repo="web-app",
                           worktree=str(f.root / "web-app/.worktrees/eng-0000-fixture"))
            r = f.resolve(bases=("4124", "3124"))
            self.assertEqual(0, r.returncode, r.stdout + r.stderr)
            self.assertEqual("web-app", f.params()["repo"])

    def test_a8b_an_explicit_value_equal_to_the_binding_is_adopted_too(self):
        """Without the 'or equal' clause, naming the repo you are already bound to
        would fail while saying nothing about it would succeed."""
        with FakeRoot() as f:
            f.write_params(repo="web-app",
                           worktree=str(f.root / "web-app/.worktrees/eng-0000-fixture"))
            r = f.resolve(repo="web-app", bases=("4124", "3124"))
            self.assertEqual(0, r.returncode, r.stdout + r.stderr)
            self.assertEqual("web-app", f.params()["repo"])

    def test_a9_a_resumed_mcp_run_keeps_its_repo_and_allocates_no_port(self):
        with FakeRoot() as f:
            r = f.resolve("--allow", "api,goodword-mcp", repo="goodword-mcp")
            self.assertEqual(0, r.returncode, r.stdout + r.stderr)
            self.assertNotIn("api_port", f.params(),
                             "a repo with no smoke stack must not hold a port")
            r2 = f.resolve("--allow", "api,goodword-mcp")  # ARCHON_REPO unset
            self.assertEqual(0, r2.returncode, r2.stdout + r2.stderr)
            self.assertIn("adopted", r2.stdout)
            p = f.params()
            self.assertEqual("goodword-mcp", p["repo"])
            self.assertTrue(p["worktree"].endswith("/goodword-mcp/.worktrees/eng-0000-fixture"))

    def test_a10_the_lane_declares_an_allow_list_and_the_lite_lane_does_not(self):
        """The lite lane's overlay carries its own preflight; leaving it without an
        --allow is what keeps it api-only rather than silently running bun against
        an mcp worktree."""
        lane = (WORKFLOWS / "full-sdlc-api.yaml").read_text(encoding="utf-8")
        self.assertIn("--allow api,goodword-mcp", lane)
        lite = (SETUP / "lite/api/preflight.bash.sh").read_text(encoding="utf-8")
        self.assertIn("resolve-params.sh", lite)
        self.assertNotIn("--allow", lite)

    # The smoke node boots a server and its exit trap kills whatever owns the
    # port it was given. Running it in a test with a real port base could
    # terminate an unrelated api run, so the branch is extracted and executed
    # against controlled values instead. The anchor test below is what keeps that
    # extraction honest -- change the node and this fails rather than drifting.
    SMOKE_BRANCH = (
        'if [ -z "$HAS_SMOKE" ]; then\n'
        '  echo "SMOKE=NOT_APPLICABLE ($REPO declares no boot smoke)" '
        '| tee "$ARTIFACTS_DIR/smoke-result.txt"\n'
        '  exit 0\n'
        'fi'
    )

    def test_a11_anchor_the_smoke_skip_branch_is_still_the_lane_s(self):
        self.assertIn(self.SMOKE_BRANCH, node_bash("smoke"),
                      "smoke's skip branch changed; re-derive this test")

    def test_a11b_the_branch_skips_only_when_the_repo_declares_no_smoke(self):
        def run(has_smoke, repo):
            with tempfile.TemporaryDirectory() as td:
                script = ('set -euo pipefail\n'
                          f'ARTIFACTS_DIR={td}\nHAS_SMOKE={has_smoke!r}\nREPO={repo!r}\n'
                          + self.SMOKE_BRANCH + '\necho REACHED_BOOT_PATH')
                r = subprocess.run(["bash", "-c", script], capture_output=True,
                                   encoding="utf-8")
                written = Path(td) / "smoke-result.txt"
                return r, (written.read_text(encoding="utf-8") if written.exists() else "")

        r, written = run("", "goodword-mcp")
        self.assertEqual(0, r.returncode)
        self.assertIn("SMOKE=NOT_APPLICABLE", written)
        self.assertIn("goodword-mcp", written)
        self.assertNotIn("REACHED_BOOT_PATH", r.stdout,
                         "a repo with no smoke must not fall through to the boot")

        # Control: api declares a smoke stack, so the branch must NOT fire and the
        # node must continue to the real boot path.
        r, written = run("1", "api")
        self.assertEqual(0, r.returncode)
        self.assertEqual("", written, "api must not get a skip artifact")
        self.assertIn("REACHED_BOOT_PATH", r.stdout)

    def test_p10_the_api_smoke_boots_and_probes_both_endpoints(self):
        """Runs the REAL smoke node against shimmed bun/curl/lsof.

        Shims, not a live server: the node's exit trap kills whatever owns the
        port it was handed, so a test that used a real port could terminate an
        unrelated api run. The shims record their argv, which is what lets this
        assert the two named probes actually happened -- a source grep for
        '/api-docs-json' passes even with SMOKE=FAIL injected right after the
        skip branch, which is how the previous version of this test was useless.
        """
        with tempfile.TemporaryDirectory() as td, FakeRoot() as f:
            bin_dir, trace = Path(td) / "bin", Path(td) / "argv.log"
            bin_dir.mkdir()
            wt = Path(td) / "wt"
            wt.mkdir()
            shim(bin_dir, "bun", f'echo "bun $*" >> {trace}\nsleep 30\n')
            # Every probe reports 200; the node distinguishes them by URL, and the
            # trace is what proves both were requested.
            # URL-aware on purpose: the node itself uses /info as an AUTH
            # negative control and requires 401 there, so a blanket 200 shim
            # fails the node for the wrong reason. Encoding the real contract
            # is what makes this test evidence rather than theatre.
            shim(bin_dir, "curl",
                 f'echo "curl $*" >> {trace}\n'
                 'case "$*" in\n'
                 '  */info) printf 401 ;;\n'
                 '  */api-docs-json) printf 200 ;;\n'
                 '  *) printf 200 ;;\n'
                 'esac\n')
            shim(bin_dir, "lsof", "exit 1\n")   # no listener: nothing to sweep
            shim(bin_dir, "kill", "true\n")     # never signal a real process
            shim(bin_dir, "sleep", "true\n")    # collapse the node's 60x3s readiness loop
            f.write_params(repo="api", api_port=4123, worktree=str(wt))
            r = run_node(node_bash("smoke"), f,
                         env={"PATH": f"{bin_dir}:{os.environ['PATH']}"},
                         timeout=180)
            recorded = trace.read_text(encoding="utf-8") if trace.exists() else ""
            result = ""
            if (f.ad / "smoke-result.txt").exists():
                result = (f.ad / "smoke-result.txt").read_text(encoding="utf-8")

            self.assertNotIn("NOT_APPLICABLE", result,
                             "api must never take the no-smoke path")
            self.assertIn("bun start", recorded, "the api server was never started")
            self.assertIn("/api-docs-json", recorded, "the readiness probe never ran")
            self.assertIn("/info", recorded, "the control probe never ran")
            self.assertIn("SMOKE=PASS", result + r.stdout,
                          f"smoke did not pass with healthy probes: {result} {r.stdout[-800:]}")
            self.assertIn("guarded-control=401", result + r.stdout,
                          "the auth negative control was not recorded")

    def test_p10b_a_failing_probe_makes_the_api_smoke_fail(self):
        """The control for the case above: with the probes returning 500 the same
        node must NOT report PASS. Without this, P10 passes for a node that
        reports success unconditionally."""
        with tempfile.TemporaryDirectory() as td, FakeRoot() as f:
            bin_dir = Path(td) / "bin"
            bin_dir.mkdir()
            wt = Path(td) / "wt"
            wt.mkdir()
            shim(bin_dir, "bun", "sleep 30\n")
            # Readiness never comes up; the node must not report PASS.
            shim(bin_dir, "curl", "printf 500\n")
            shim(bin_dir, "lsof", "exit 1\n")
            shim(bin_dir, "kill", "true\n")
            shim(bin_dir, "sleep", "true\n")
            f.write_params(repo="api", api_port=4123, worktree=str(wt))
            r = run_node(node_bash("smoke"), f,
                         env={"PATH": f"{bin_dir}:{os.environ['PATH']}"},
                         timeout=180)
            result = ""
            if (f.ad / "smoke-result.txt").exists():
                result = (f.ad / "smoke-result.txt").read_text(encoding="utf-8")
            self.assertNotIn("SMOKE=PASS", result + r.stdout,
                             "the smoke gate reported PASS with failing probes")

    def test_a12_result_artifacts_report_the_real_repo(self):
        lane = (WORKFLOWS / "full-sdlc-api.yaml").read_text(encoding="utf-8")
        self.assertNotIn('"repo":"api"', lane)
        # Both outcomes, and the worktrees record, must pass $REPO through.
        self.assertEqual(3, lane.count('"$REPO" "$BASE" "$(git rev-parse HEAD)"'))
        self.assertIn('"repo":"%s"}\\n\' "$WT" "$BR" "$REPO"', lane)
        self.assertIn("api_worktree", lane,
                      "archon-run.py and resolve-web-params.sh read this key")

    def test_a13_lint_applicability_is_written_into_the_record_that_is_read(self):
        """Executes the recheck.json writer both ways. The string-matching version
        of this test still passed after the PR-body instructions were deleted."""
        for applicable, expected in (("true", True), ("false", False)):
            with self.subTest(lint_applicable=applicable):
                with tempfile.TemporaryDirectory() as td:
                    out = Path(td) / "recheck.json"
                    subprocess.run(
                        ["python3", "-c", recheck_writer(), str(out),
                         "0", "0", "0", "0", "0", applicable],
                        check=True, capture_output=True)
                    got = json.loads(out.read_text(encoding="utf-8"))
                    self.assertEqual(expected, got["lint_applicable"])
                    for k in ("typecheck", "lint", "tests", "scope", "slop"):
                        self.assertEqual(0, got[k], "the five integers must not change shape")

    def test_a13b_the_reviewer_and_pr_body_are_told_to_read_it(self):
        lane = (WORKFLOWS / "full-sdlc-api.yaml").read_text(encoding="utf-8")
        reviewer = node_prompt_containing(lane, "AUTHORITATIVE verification record")
        self.assertIn("lint_applicable", reviewer,
                      "the deslop reviewer must be told how to read it")
        prbody = node_prompt_containing(lane, "PR-body composition node")
        self.assertIn("deslop-round.txt", prbody)
        self.assertIn("lint_applicable", prbody)


class Mutation(unittest.TestCase):
    """Deliberate breaks, each with a matched anchor and a passing control."""

    def test_m5_an_unknown_repo_is_a_typed_stop_not_a_default(self):
        with FakeRoot() as f:
            control = f.resolve("--allow", "api,goodword-mcp")
            self.assertEqual(0, control.returncode, "control must pass")
            (f.ad / "params.json").unlink()
            r = f.resolve("--allow", "api,goodword-mcp", repo="nonsense")
            self.assertEqual(1, r.returncode)
            self.assertIn("PARAMS=FAIL", r.stdout + r.stderr)
            self.assertFalse((f.ad / "params.json").exists(),
                             "a rejected run must not leave params behind")

    def test_m4_a_lane_without_an_allow_list_refuses_a_non_api_repo(self):
        with FakeRoot() as f:
            control = f.resolve()  # no --allow, no ARCHON_REPO -> api
            self.assertEqual(0, control.returncode, "control must pass")
            (f.ad / "params.json").unlink()
            r = f.resolve(repo="goodword-mcp")
            self.assertEqual(1, r.returncode, r.stdout + r.stderr)
            self.assertIn("not supported by this lane", r.stdout + r.stderr)

    def test_m6_a_conflicting_explicit_repo_never_silently_rebinds(self):
        with FakeRoot() as f:
            f.write_params(repo="web-app",
                           worktree=str(f.root / "web-app/.worktrees/eng-0000-fixture"))
            control = f.resolve(bases=("4124", "3124"))
            self.assertEqual(0, control.returncode, "adoption control must pass")
            r = f.resolve(repo="api", bases=("4124", "3124"))
            self.assertEqual(1, r.returncode, r.stdout + r.stderr)
            self.assertIn("REPO_CONFLICT", r.stdout + r.stderr)
            self.assertEqual("web-app", f.params()["repo"], "binding must survive")

    def test_m7a_an_emitted_helper_failure_stops_both_call_paths(self):
        with FakeRoot() as f:
            f.write_params(repo="nonsense")
            r = f.env_from_params()
            self.assertNotEqual(0, r.returncode, r.stdout + r.stderr)
            self.assertIn("FAIL", r.stdout + r.stderr)

    def test_m7b_a_silent_nonzero_helper_stops_both_call_paths(self):
        """The branch the eval-able-failure idiom alone cannot catch: `eval "$(cmd)"`
        swallows a non-zero exit, so capture-and-check is what makes this fail."""
        with FakeRoot() as f:
            control = f.resolve()
            self.assertEqual(0, control.returncode, "control must pass")
            anchor = (f.setup / "repo-profile.sh").read_text(encoding="utf-8")
            self.assertIn("PROFILES", anchor, "mutation anchor no longer matches")
            (f.setup / "repo-profile.sh").write_text("exit 3\n", encoding="utf-8")

            r = f.resolve()
            self.assertNotEqual(0, r.returncode, "resolver swallowed a silent failure")
            self.assertIn("repo-profile.sh failed", r.stdout + r.stderr)

            f.write_params(repo="api")
            r2 = f.env_from_params()
            self.assertNotEqual(0, r2.returncode, "params-env swallowed a silent failure")

    def test_m8_the_helper_is_packaged(self):
        """params-env.sh now depends on it, so an install that omits it breaks the
        api lane, not just mcp."""
        self.assertIn("setup/repo-profile.sh",
                      (SETUP / "package.sh").read_text(encoding="utf-8"))

    def test_m9_the_browser_gate_still_rejects_an_untyped_empty_policy(self):
        shape = (SETUP / "plan-shape.sh").read_text(encoding="utf-8")
        self.assertIn("not_applicable", shape)
        self.assertIn('assert required == []', shape,
                      "the typed disposition must still forbid a populated list")
        self.assertIn("assert isinstance(required, list) and required", shape,
                      "an untyped empty policy must still fail")


class ShellSafety(unittest.TestCase):
    """A repo name reaching the error path is UNTRUSTED input that becomes shell.

    Interpolating it raw into a single-quoted diagnostic let a value containing an
    apostrophe close the quote and run the rest as commands; a trailing `#` then
    commented out the `exit 1`, so the caller executed the payload and continued
    with exit 0 under `set -euo pipefail`. Text-matching cannot detect this --
    only a side effect can -- so each case writes a file and the assertion is that
    the file does not exist.
    """

    PAYLOADS = {
        "apostrophe_and_comment": "bad'; touch {marker}; #",
        "double_quote": 'x"; touch {marker}; #',
        "command_substitution": "a$(touch {marker})",
        "backtick": "a`touch {marker}`",
        "newline": "a\ntouch {marker}",
        "semicolon": "a; touch {marker}",
    }

    def _assert_inert(self, script_args, label):
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / "pwned"
            repo = self.PAYLOADS[label].format(marker=marker)
            script, extra_setup = script_args(repo, td)
            r = subprocess.run(["bash", "-c", script], capture_output=True,
                               encoding="utf-8")
            self.assertFalse(marker.exists(),
                             f"{label}: payload EXECUTED -- {r.stdout}{r.stderr}")
            self.assertNotEqual(0, r.returncode,
                                f"{label}: caller continued after a bad repo")
            del extra_setup

    def test_m10_repo_profile_never_executes_an_untrusted_repo_name(self):
        def build(repo, td):
            quoted = repo.replace("'", "'\\''")
            return (f"set -euo pipefail\n"
                    f"O=$(bash {SETUP}/repo-profile.sh '{quoted}') || exit 1\n"
                    f'eval "$O"\n'
                    f"echo REACHED"), None
        for label in self.PAYLOADS:
            with self.subTest(payload=label):
                self._assert_inert(build, label)

    def test_m11_params_env_never_executes_an_untrusted_repo_name(self):
        """The same value arriving through a params.json this script did not write."""
        def build(repo, td):
            params = Path(td) / "params.json"
            params.write_text(json.dumps({
                "spec": "/x", "slug": "s", "branch": "b",
                "worktree": "/w", "repo": repo}), encoding="utf-8")
            return (f"set -euo pipefail\n"
                    f'eval "$(bash {SETUP}/params-env.sh {params})"\n'
                    f"echo REACHED"), None
        for label in self.PAYLOADS:
            with self.subTest(payload=label):
                self._assert_inert(build, label)

    def test_params_ports_cannot_execute_shell_or_continue_after_invalid_input(self):
        for field in ("api_port", "web_port"):
            for label in self.PAYLOADS:
                with self.subTest(field=field, payload=label):
                    def build(payload, td):
                        params = Path(td) / "params.json"
                        params.write_text(json.dumps({
                            "spec": "/x", "slug": "s", "branch": "b",
                            "worktree": "/w", "repo": "api", field: payload}), encoding="utf-8")
                        return (f'set -euo pipefail\neval "$(bash {SETUP}/params-env.sh {params})"\necho REACHED'), None
                    self._assert_inert(build, label)

    def test_invalid_params_make_eval_caller_stop(self):
        invalid = ["{", "null", "[]", '{}',
                   json.dumps({"spec": "/x", "slug": "s", "branch": "b", "worktree": "/w", "api_port": True}),
                   json.dumps({"spec": "/x", "slug": "s", "branch": "b", "worktree": "/w", "api_port": 65536})]
        for repo in (None, False, 0, [], ""):
            invalid.append(json.dumps({"spec": "/x", "slug": "s", "branch": "b", "worktree": "/w", "repo": repo}))
        for contents in invalid:
            with self.subTest(contents=contents), tempfile.TemporaryDirectory() as td:
                params = Path(td) / "params.json"
                params.write_text(contents)
                result = subprocess.run(
                    ["bash", "-c", 'set -euo pipefail; eval "$(bash "$1" "$2")"; echo REACHED',
                     "bash", str(SETUP / "params-env.sh"), str(params)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertNotIn("REACHED", result.stdout)

    def test_missing_params_filename_is_not_evaluated_as_shell(self):
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / "pwned"
            missing = Path(td) / ("missing'; touch " + str(marker) + "; #")
            result = subprocess.run(
                ["bash", "-c", 'set -euo pipefail; eval "$(bash "$1" "$2")"; echo REACHED',
                 "bash", str(SETUP / "params-env.sh"), str(missing)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertFalse(marker.exists())
            self.assertNotIn("REACHED", result.stdout)

    def test_m12_a_valid_repo_still_resolves(self):
        """The control: the quoting fix must not break the normal path."""
        r = subprocess.run(
            ["bash", "-c", f'set -euo pipefail\nO=$(bash {SETUP}/repo-profile.sh api)\n'
                           f'eval "$O"\necho "$REPO"'],
            capture_output=True, encoding="utf-8")
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        self.assertEqual("api", r.stdout.strip())


class BrowserPolicyIsRepoGated(unittest.TestCase):
    """The exemption must come from the RUN'S REPO, never from the artifact's own
    say-so. Accepting `not_applicable` from any repo turns the api gate off with
    one added string -- which is what the first implementation did."""

    def _check(self, repo, kind):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "wt").mkdir()
            (d / "params.json").write_text(json.dumps({
                "spec": "/x", "slug": "s", "branch": "b",
                "repo": repo, "worktree": str(d / "wt")}), encoding="utf-8")
            (d / "plan.md").write_text(
                "## Goal\n## Files\n## Approach\n## Test scenarios\n## Verification\n",
                encoding="utf-8")
            (d / "verify.json").write_text('{"test_patterns":["p"]}', encoding="utf-8")
            (d / "files-allowlist.json").write_text('["a.ts"]', encoding="utf-8")
            (d / "web-files-allowlist.json").write_text("[]", encoding="utf-8")
            (d / "reader-audit.json").write_text('{"columns":[]}', encoding="utf-8")
            (d / "web-reader-audit.json").write_text('{"columns":[]}', encoding="utf-8")
            if kind == "not_applicable":
                policy = {"not_applicable": "no browser surface", "required": []}
            else:
                policy = {"required": [{"id": "b1", "criterion": "c", "path": "/x",
                                        "assertions": [{"type": "text", "value": "v"}]}]}
            (d / "browser-evidence.json").write_text(json.dumps(policy), encoding="utf-8")
            import hashlib
            digest = hashlib.sha256(json.dumps(
                policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            (d / "browser-evidence.sha256").write_text(digest, encoding="utf-8")
            spec = d / "spec.md"
            spec.write_text("# spec\n", encoding="utf-8")
            r = subprocess.run(["bash", str(SETUP / "plan-shape.sh"),
                                str(d), str(d / "wt"), str(spec)],
                               capture_output=True, encoding="utf-8")
            return r.returncode == 0

    def test_p11_api_cannot_exempt_itself(self):
        self.assertTrue(self._check("api", "populated"), "control: api must pass")
        self.assertFalse(self._check("api", "not_applicable"),
                         "the api browser gate was switched off by an artifact field")

    def test_p11b_web_app_cannot_exempt_itself(self):
        self.assertFalse(self._check("web-app", "not_applicable"))

    def test_p11c_legacy_params_without_a_repo_are_treated_as_api(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "wt").mkdir()
            (d / "params.json").write_text(json.dumps({
                "spec": "/x", "slug": "s", "branch": "b",
                "worktree": str(d / "wt")}), encoding="utf-8")
            # everything else identical to _check's not_applicable case
            (d / "plan.md").write_text(
                "## Goal\n## Files\n## Approach\n## Test scenarios\n## Verification\n",
                encoding="utf-8")
            for name, body in (("verify.json", '{"test_patterns":["p"]}'),
                               ("files-allowlist.json", '["a.ts"]'),
                               ("web-files-allowlist.json", "[]"),
                               ("reader-audit.json", '{"columns":[]}'),
                               ("web-reader-audit.json", '{"columns":[]}')):
                (d / name).write_text(body, encoding="utf-8")
            policy = {"not_applicable": "claiming an exemption", "required": []}
            (d / "browser-evidence.json").write_text(json.dumps(policy), encoding="utf-8")
            import hashlib
            (d / "browser-evidence.sha256").write_text(hashlib.sha256(json.dumps(
                policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                encoding="utf-8")
            spec = d / "spec.md"
            spec.write_text("# spec\n", encoding="utf-8")
            r = subprocess.run(["bash", str(SETUP / "plan-shape.sh"),
                                str(d), str(d / "wt"), str(spec)],
                               capture_output=True, encoding="utf-8")
            self.assertNotEqual(0, r.returncode,
                                "a legacy params.json must not gain an exemption")

    def test_a4_mcp_may_use_the_typed_disposition(self):
        self.assertTrue(self._check("goodword-mcp", "not_applicable"))
        self.assertTrue(self._check("goodword-mcp", "populated"),
                        "a populated policy must still be accepted")


class EnvSeedingStaysMandatoryForApi(unittest.TestCase):
    """bootstrap's .env copy was made conditional. The condition must be the repo's
    DECLARED env source, never 'the file happens to exist' -- keyed on existence, a
    missing api .env would stop failing the run and an adopted worktree would
    silently keep a stale one.

    bootstrap hardcodes an absolute REPO_DIR, so this lifts its env branch verbatim
    from the YAML and runs it against a controlled directory. Soften the branch
    (`|| true`, an `-f` guard) and the extraction stops matching, so the test fails
    on its anchor rather than passing quietly.
    """

    BRANCH = (
        'if [ -n "$ENV_SRC" ]; then\n'
        '  cp "$REPO_DIR/$ENV_SRC" "$WT/$ENV_SRC"\n'
        'fi'
    )

    def test_the_branch_is_still_the_one_in_the_lane(self):
        self.assertIn(self.BRANCH, node_bash("bootstrap"),
                      "bootstrap's env branch changed; re-derive this test")

    def _run(self, env_src, seed):
        with tempfile.TemporaryDirectory() as td:
            repo_dir, wt = Path(td) / "repo", Path(td) / "wt"
            repo_dir.mkdir()
            wt.mkdir()
            if seed:
                (repo_dir / ".env").write_text("KEY=value\n", encoding="utf-8")
            script = ("set -euo pipefail\n"
                      f"REPO_DIR={repo_dir}\nWT={wt}\nENV_SRC='{env_src}'\n"
                      + self.BRANCH)
            r = subprocess.run(["bash", "-c", script], capture_output=True,
                               encoding="utf-8")
            return r.returncode, (wt / ".env").exists()

    def test_p5_api_with_a_missing_env_still_fails(self):
        rc, copied = self._run(".env", seed=False)
        self.assertNotEqual(0, rc, "a missing api .env must still fail bootstrap")
        self.assertFalse(copied)

    def test_p5b_api_with_the_env_present_succeeds(self):
        """The control: without it, the case above passes for any broken branch."""
        rc, copied = self._run(".env", seed=True)
        self.assertEqual(0, rc)
        self.assertTrue(copied, "api must still get its .env seeded")

    def test_a5_a_repo_declaring_no_env_source_skips_without_failing(self):
        rc, copied = self._run("", seed=False)
        self.assertEqual(0, rc)
        self.assertFalse(copied)


MCP_PROBE = (Path("/Users/eduardopicazo/Documents/Workspace/Goodword")
             / "goodword-mcp/.worktrees/phase1-probe")


@contextlib.contextmanager
def disposable_mcp():
    """A throwaway copy of the mcp worktree with node_modules SYMLINKED.

    tsconfig.json includes `src/**/*`, so injecting a type error into the shared
    worktree is visible to every other typecheck running against it -- a unique
    filename bounds the overwrite risk but not the interference. Copying the
    source (a few hundred KB) and linking the installed modules gives real
    isolation for the price of a directory copy.
    """
    with tempfile.TemporaryDirectory() as td:
        dest = Path(td) / "wt"
        dest.mkdir()
        for name in ("src", "tests"):
            shutil.copytree(MCP_PROBE / name, dest / name)
        for name in ("package.json", "tsconfig.json", "tsconfig.test.json",
                     "jest.config.mjs"):
            shutil.copy(MCP_PROBE / name, dest / name)
        (dest / "node_modules").symlink_to(MCP_PROBE / "node_modules")
        yield dest


@unittest.skipUnless((MCP_PROBE / "node_modules").is_dir(),
                     "needs an installed goodword-mcp worktree")
class McpGateActuallyRuns(unittest.TestCase):
    """Selection AND execution against the real repo. An earlier draft of this
    command failed every suite with an ESM error, which a naive negative control
    reads as success -- so each exclusion has a positive control beside it."""

    def _jest(self, *args):
        prof = profile("goodword-mcp")
        return subprocess.run(list(prof["CMD_TEST"]) + list(args),
                              cwd=MCP_PROBE, capture_output=True, encoding="utf-8")

    def test_a7_a_real_unit_suite_passes(self):
        r = self._jest("delete-group")
        self.assertEqual(0, r.returncode, (r.stdout + r.stderr)[-2000:])
        self.assertIn("Tests:", r.stdout + r.stderr)

    def test_a2_e2e_suites_are_never_selected(self):
        r = self._jest("tools", "--listTests")
        self.assertNotIn("e2e.test.ts", r.stdout,
                         "a live-API e2e suite reached the gate")
        self.assertEqual("", r.stdout.strip())

    def test_a2b_smoke_suites_are_never_selected(self):
        """Separate from A2 on purpose: `tools` names only an e2e file, so dropping
        `smoke` from the ignore regex still passes A2 and its control."""
        r = self._jest("search-timeout", "--listTests")
        self.assertNotIn("smoke.test.ts", r.stdout)
        self.assertEqual("", r.stdout.strip())

    def test_a2c_unit_suites_are_still_selected(self):
        """Control for both exclusions: a regex matching everything would pass A2
        and A2b and be worthless."""
        r = self._jest("delete-group", "--listTests")
        self.assertIn("delete-group.unit.test.ts", r.stdout)

    def test_m2_a_zero_match_selector_is_not_a_silent_pass(self):
        r = self._jest("no-such-spec-anywhere-xyz")
        self.assertNotEqual(0, r.returncode,
                            "a selector matching nothing reported success")

    def test_m3_the_typecheck_catches_a_real_type_error(self):
        """Runs in a disposable copy. Injecting into the shared worktree was
        visible to every concurrent typecheck (tsconfig includes src/**/*), so a
        parallel run could fail on another process's error."""
        prof = profile("goodword-mcp")
        with disposable_mcp() as wt:
            control = subprocess.run(prof["CMD_TYPECHECK"], cwd=wt,
                                     capture_output=True, encoding="utf-8")
            self.assertEqual(0, control.returncode,
                             "control failed: the copy is not clean")
            probe = wt / "src" / "__archon_typecheck_probe.ts"
            probe.write_text('export const bad: number = "not a number";\n',
                             encoding="utf-8")
            r = subprocess.run(prof["CMD_TYPECHECK"], cwd=wt,
                               capture_output=True, encoding="utf-8")
            self.assertNotEqual(0, r.returncode, "the typecheck gate cannot fail")
            self.assertIn(probe.name, r.stdout + r.stderr,
                          "the gate failed, but not for the injected reason")

    def test_m1_a_failing_unit_test_fails_the_gate_for_the_right_reason(self):
        """The mutation the plan names as M1. The 'right reason' assertion is the
        point: an earlier draft of this command failed EVERY suite with an ESM
        error, which a bare non-zero check reads as a working gate."""
        prof = profile("goodword-mcp")
        with disposable_mcp() as wt:
            spec = wt / "tests" / "delete-group.unit.test.ts"
            control = subprocess.run(list(prof["CMD_TEST"]) + ["delete-group"],
                                     cwd=wt, capture_output=True, encoding="utf-8")
            self.assertEqual(0, control.returncode,
                             "control failed: " + (control.stdout + control.stderr)[-1500:])
            original = spec.read_text(encoding="utf-8")
            marker = "ARCHON_INJECTED_FAILURE"
            spec.write_text(
                original + f'\ndescribe("{marker}", () => {{\n'
                f'  it("fails on purpose", () => {{ expect(1).toBe(2); }});\n}});\n',
                encoding="utf-8")
            r = subprocess.run(list(prof["CMD_TEST"]) + ["delete-group"],
                               cwd=wt, capture_output=True, encoding="utf-8")
            out = r.stdout + r.stderr
            self.assertNotEqual(0, r.returncode, "a failing unit test did not fail the gate")
            self.assertIn(marker, out, "failed, but not because of the injected test")
            self.assertNotIn("Cannot use import statement outside a module", out,
                             "failed for an ESM environment reason, not the test")


if __name__ == "__main__":
    unittest.main()
