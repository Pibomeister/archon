#!/usr/bin/env python3
"""Node determinism stress harness.

Every covered bash node is run N times (NODE_STRESS, default 3) against a
freshly built fixture per run, and asserted to produce byte-identical typed
output, exit code, and artifact-dir contents. See nodes/runner.py for the
observation contract and nodes/AUDIT.md for the nondeterminism audit that
drove the RED tests at the bottom of this file.

External tools are stubbed through a PATH-prepended shim dir (see SHIMS):
  bun   - `bun run <script> [-- <pattern>]`; echoes its argv, exits
          $SHIM_RC_BUN_<SCRIPT> (default 0).
  pnpm  - same shape, $SHIM_RC_PNPM_<SCRIPT>.
  mise  - `mise x node@20 -- <cmd...>`; drops everything up to `--` and
          execs the rest, so the pnpm shim runs underneath it.
No covered node calls gh, aws, or archon.
"""
import atexit
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from nodes.extract import ARCHON_ROOT, runnable_body
from nodes.runner import run_node

FIXTURES = Path(__file__).resolve().parent / "nodes" / "fixtures"
SHARED = Path(__file__).resolve().parent / "fixtures"
ENVELOPE = (FIXTURES / "review-gate" / "round-1" / "review-envelope.txt").read_text(
    encoding="utf-8"
)
VERDICTS = ("Ready to merge", "Ready with fixes", "Not ready")
GOODWORD_ROOT = ARCHON_ROOT.parent

# --------------------------------------------------------------------------
# shims
# --------------------------------------------------------------------------
_NODE_TOOL = """#!/usr/bin/env bash
echo "shim {tool} $*"
sub="$1"; [ "$1" = run ] && sub="$2"
key="SHIM_RC_{TOOL}_$(printf '%s' "$sub" | tr '[:lower:]-' '[:upper:]_')"
rc="${{!key:-0}}"
exit "$rc"
"""

_MISE = """#!/usr/bin/env bash
# `mise x node@20 -- <cmd...>`: drop the runtime selector, exec the real cmd.
while [ $# -gt 0 ] && [ "$1" != "--" ]; do shift; done
[ "$1" = "--" ] && shift
exec "$@"
"""

SHIMS = {
    "bun": _NODE_TOOL.format(tool="bun", TOOL="BUN"),
    "pnpm": _NODE_TOOL.format(tool="pnpm", TOOL="PNPM"),
    "mise": _MISE,
}


def write_shims(tmp, tools=("bun", "pnpm", "mise")):
    d = tmp / "bin"
    d.mkdir(exist_ok=True)
    for t in tools:
        p = d / t
        p.write_text(SHIMS[t], encoding="utf-8")
        p.chmod(0o755)
    return d


# --------------------------------------------------------------------------
# git worktree fixtures
# --------------------------------------------------------------------------
def git(repo, *args, check=True):
    return subprocess.run(
        ["/usr/bin/git", "-C", str(repo), *args],
        capture_output=True, encoding="utf-8", check=check,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )


BASE_FOO = "export function foo(x: number): number {\n  return x + 1;\n}\n"
BASE_SPEC = (
    'import { foo } from "./foo";\n\n'
    'describe("foo", () => {\n'
    '  it("adds one", () => {\n'
    "    expect(foo(1)).toBe(2);\n"
    "  });\n"
    "});\n"
)


def prerun(workflow, node, tmp, env=None):
    """Run an UPSTREAM node body to build the state the node under test reads.

    Used where reproducing a predecessor's output by hand would be a second
    implementation of it that could drift (deslop-recheck's three-part
    checkpoint, rca-gate's durable imm-* anchors). Fails loudly.
    """
    body = runnable_body(workflow, node)
    run_env = dict(os.environ)
    run_env.update(env or {})
    run_env["ARTIFACTS_DIR"] = str(tmp / "artifacts")
    shim = tmp / "bin"
    if shim.is_dir():
        run_env["PATH"] = f"{shim}:{run_env['PATH']}"
    p = subprocess.run(["bash", "-c", body], capture_output=True, encoding="utf-8",
                       env=run_env, cwd=str(tmp))
    if p.returncode != 0:
        raise AssertionError(
            f"prerun {workflow}:{node} failed rc={p.returncode}\n{p.stdout}\n{p.stderr}"
        )
    return p


DIRTY_FOO = "export function foo(x: number): number {\n  return x + 2;\n}\n"

_TEMPLATE_LOCK = threading.Lock()
_TEMPLATE = None


def _template_repo():
    """Build the base repo ONCE per process and copy it per fixture.

    Seven `git` execs per fixture x 3 stress runs x ~40 fixtures dominated the
    suite's wall time. A git repo has no absolute paths of its own, and the one
    absolute value this sets (core.hooksPath) points at a process-lifetime dir,
    so a plain copytree is a faithful clone of the template.
    """
    global _TEMPLATE
    with _TEMPLATE_LOCK:
        if _TEMPLATE is None:
            root = Path(tempfile.mkdtemp(prefix="nodestress-template-"))
            atexit.register(shutil.rmtree, root, ignore_errors=True)
            hooks = root / "nohooks"
            hooks.mkdir()
            repo = root / "repo" / "src"
            repo.parent.mkdir()
            repo.mkdir()
            git(repo.parent, "init", "-q", "-b", "main")
            git(repo.parent, "config", "user.email", "test@example.com")
            git(repo.parent, "config", "user.name", "test")
            git(repo.parent, "config", "commit.gpgsign", "false")
            # Repo-local overrides so a developer's global config (signing
            # keys, a hooksPath) cannot make a fixture behave differently
            # machine to machine. The hooks dir lives OUTSIDE the worktree: an
            # empty dir inside it would read as untracked drift.
            git(repo.parent, "config", "core.hooksPath", str(hooks))
            (repo / "foo.ts").write_text(BASE_FOO, encoding="utf-8")
            (repo / "foo.spec.ts").write_text(BASE_SPEC, encoding="utf-8")
            git(repo.parent, "add", "-A")
            git(repo.parent, "commit", "-q", "-m", "base")
            base = git(repo.parent, "rev-parse", "HEAD").stdout.strip()
            _TEMPLATE = (repo.parent, base)
    return _TEMPLATE


def init_worktree(path, dirty=True):
    """A throwaway repo with src/foo.ts + src/foo.spec.ts committed, and (by
    default) a one-line uncommitted edit to src/foo.ts so scope/slop/status
    guards have something in the plan's allowlist to look at.
    Returns the base commit sha."""
    src, base = _template_repo()
    shutil.copytree(src, path)
    if dirty:
        (path / "src" / "foo.ts").write_text(DIRTY_FOO, encoding="utf-8")
    return base


def params(tmp, wt, slug="toy", branch="archon/toy", spec=None):
    return {
        "spec": str(spec or (tmp / "artifacts" / "spec.md")),
        "slug": slug,
        "branch": branch,
        "worktree": str(wt),
    }


def jdump(path, obj):
    Path(path).write_text(json.dumps(obj), encoding="utf-8")


def copy_shared(name, dest):
    shutil.copytree(SHARED / name, dest, dirs_exist_ok=True)


# --------------------------------------------------------------------------
# review-gate: 3 verdicts x 3 entry paths, in all three lanes
# --------------------------------------------------------------------------
PREHEAD = "abc1234def5678901234567890123456789abcde"


def envelope_with(verdict):
    assert "Verdict: Ready with fixes" in ENVELOPE
    return ENVELOPE.replace("Verdict: Ready with fixes", f"Verdict: {verdict}")


def review_gate_fixture(verdict, path):
    """path is one of:
    envelope - the review node returned a full envelope, no new CE run dir.
    metadata - a NEW ce-code-review dir whose metadata.json head_sha
               prefix-matches pre-head.txt carries the verdict.
    resume   - $review.output is empty (node outputs did not survive the
               resume); the envelope FILE on disk is the only source.
    """
    def build(tmp):
        art = tmp / "artifacts"
        rd = art / "round-1"
        rd.mkdir(parents=True)
        (art / "round.txt").write_text("1\n", encoding="utf-8")
        (rd / "pre-head.txt").write_text(PREHEAD + "\n", encoding="utf-8")
        ce = tmp / "ce-review-root"
        ce.mkdir()
        # prerun-dirs.txt is what round-pre wrote before the review ran.
        (rd / "prerun-dirs.txt").write_text("", encoding="utf-8")
        if path == "resume":
            (rd / "review-envelope.txt").write_text(envelope_with(verdict), encoding="utf-8")
        if path == "metadata":
            # A DIFFERENT envelope verdict, so `source=metadata` in the
            # assertion cannot be satisfied by the envelope fallback.
            other = "Ready to merge" if verdict == "Not ready" else "Not ready"
            (rd / "review-envelope.txt").write_text(envelope_with(other), encoding="utf-8")
            run = ce / "run-0001"
            run.mkdir()
            jdump(run / "metadata.json", {"head_sha": PREHEAD[:12], "verdict": verdict})
        return {"CE_REVIEW_ROOT": str(ce)}

    return build


def review_gate_output(verdict, path):
    if path == "envelope":
        return envelope_with(verdict)
    if path == "metadata":
        other = "Ready to merge" if verdict == "Not ready" else "Not ready"
        return envelope_with(other)
    return ""  # resume: node output did not survive


class ReviewGateStress(unittest.TestCase):
    """S1: 3 enum verdicts x 3 entry paths x 3 lanes, N runs each."""

    def _one(self, workflow, verdict, path):
        r = run_node(
            workflow, "review-gate",
            review_gate_fixture(verdict, path),
            outputs={"review": review_gate_output(verdict, path)},
        )
        self.assertEqual(r["rc"], 0, r["output"])
        self.assertIn(f"REVIEW_GATE=PASS round=1", r["output"])
        src = "metadata" if path == "metadata" else "envelope"
        self.assertIn(
            f"GATE_3_verdict_in_enum=PASS verdict=[{verdict}] source={src}", r["output"]
        )
        return r


def _mk_review_gate_tests():
    for workflow in ("full-sdlc-api", "full-sdlc-web", "bugfix"):
        for verdict in VERDICTS:
            for path in ("envelope", "metadata", "resume"):
                vslug = verdict.lower().replace(" ", "_")
                name = f"test_{workflow.replace('-', '_')}__{vslug}__{path}"

                def t(self, w=workflow, v=verdict, p=path):
                    self._one(w, v, p)

                t.__name__ = name
                setattr(ReviewGateStress, name, t)


_mk_review_gate_tests()


# --------------------------------------------------------------------------
# AUDIT: review-gate's ce-code-review run-dir scan (see nodes/AUDIT.md rows
# RG-1 and RG-2). Two independent inputs decide which run dir the verdict is
# read from: the SET DIFFERENCE against prerun-dirs.txt, and the head_sha
# prefix match. Each gets its own test, and the collation guard gets a
# negative control that must reproduce the bug when the guard is reverted.
# --------------------------------------------------------------------------
def has_locale(name):
    r = subprocess.run(["locale", "-a"], capture_output=True, encoding="utf-8")
    return name in {ln.strip() for ln in (r.stdout or "").splitlines()}


def require_locale(case, name):
    """A missing locale is a FAILURE, never a skip.

    These tests are the regression cover for RG-1 — a collation mismatch that
    makes `comm` report a pre-existing ce-code-review dir as new — and for
    ENV-1. Skipping them on a machine without the locale is precisely backwards:
    a minimal Linux CI image is where a collation bug is most likely to ship
    unnoticed, and a green run would claim cover it never had.
    """
    if not has_locale(name):
        case.fail(
            f"locale {name} is not installed, so this test cannot run — and a "
            f"skip here would silently drop the RG-1 / ENV-1 regression cover.\n"
            f"  Debian/Ubuntu: sudo sed -i 's/^# *{name}/{name}/' /etc/locale.gen "
            f"&& sudo locale-gen\n"
            f"  Fedora/RHEL:   sudo dnf install glibc-langpack-en glibc-langpack-tr\n"
            f"  Alpine:        apk add musl-locales\n"
            f"  macOS ships both. See RUNBOOK.md 6a, 'Locales the test suite requires'."
        )


UTF8_LOCALE = "en_US.UTF-8"
# C collation puts 'B' before 'a'; en_US.UTF-8 collation puts 'alpha' before
# 'Beta'. Two dirs are enough to make the two orderings disagree.
C_ORDER = ("ce-Beta", "ce-alpha")
FOREIGN_SHA = "f" * 40

# The guard, and the pre-guard text a negative control reverts it to.
SORT_GUARD = '2>/dev/null | LC_ALL=C sort > "$RD/post-dirs.txt"'
SORT_UNGUARDED = '2>/dev/null | sort > "$RD/post-dirs.txt"'


def ce_scan_fixture(mode):
    """mode='collation': two dirs that BOTH pre-date the review, with
    prerun-dirs.txt written in C collation (as a differently-configured shell
    that started the run would have written it). Neither is new, so the gate
    must read the envelope.
    mode='foreign-sha': a genuinely NEW dir, but from someone else's review of
    a different HEAD. The prefix match must reject it."""
    def build(tmp):
        art = tmp / "artifacts"
        rd = art / "round-1"
        rd.mkdir(parents=True)
        (art / "round.txt").write_text("1\n", encoding="utf-8")
        (rd / "pre-head.txt").write_text(PREHEAD + "\n", encoding="utf-8")
        ce = tmp / "ce-review-root"
        ce.mkdir()
        if mode == "collation":
            for name in C_ORDER:
                (ce / name).mkdir()
            # A foreign run that happens to sit on the SAME head sha - the
            # prefix match cannot save us here, only the set difference can.
            jdump(ce / "ce-alpha" / "metadata.json",
                  {"head_sha": PREHEAD[:12], "verdict": "Not ready"})
            (rd / "prerun-dirs.txt").write_text(
                "".join(f"{ce}/{n}/\n" for n in C_ORDER), encoding="utf-8"
            )
        else:
            (rd / "prerun-dirs.txt").write_text("", encoding="utf-8")
            (ce / "ce-other").mkdir()
            jdump(ce / "ce-other" / "metadata.json",
                  {"head_sha": FOREIGN_SHA, "verdict": "Not ready"})
        return {"CE_REVIEW_ROOT": str(ce)}

    return build


class ReviewGateScanIsolation(unittest.TestCase):
    ENVELOPE_VERDICT = "Ready to merge"

    def _probe(self, workflow, mode, subs=None, locale=UTF8_LOCALE):
        return run_node(
            workflow, "review-gate", ce_scan_fixture(mode),
            outputs={"review": envelope_with(self.ENVELOPE_VERDICT)},
            env={"LC_ALL": locale}, subs=subs,
        )

    def test_prerun_listing_in_another_collation_yields_no_phantom_new_dir(self):
        """RG-1. prerun-dirs.txt was written by round-pre, possibly from a
        differently-configured shell (a resume from another terminal). If the
        two listings are sorted in different collations, `comm` silently
        reports a PRE-EXISTING dir as new and the gate reads a foreign run's
        verdict. LC_ALL=C on both sorts pins the collation."""
        require_locale(self, UTF8_LOCALE)
        for workflow in ("full-sdlc-api", "full-sdlc-web", "bugfix"):
            with self.subTest(workflow=workflow):
                r = self._probe(workflow, "collation")
                self.assertIn(
                    f"verdict=[{self.ENVELOPE_VERDICT}] source=envelope", r["output"]
                )
                self.assertIn("rundir=[]", r["output"])

    def test_negative_control_unguarded_sort_reads_the_foreign_dir(self):
        """The guard above is load-bearing: revert `LC_ALL=C sort` to a bare
        `sort` and the same fixture reads ce-alpha's foreign verdict instead.
        If this test ever starts passing the fixture stopped reproducing and
        the guard test above is proving nothing."""
        require_locale(self, UTF8_LOCALE)
        r = self._probe("full-sdlc-api", "collation",
                        subs=[(SORT_GUARD, SORT_UNGUARDED)])
        self.assertIn("verdict=[Not ready] source=metadata", r["output"])
        self.assertIn("ce-alpha", r["output"])

    def test_new_dir_from_a_different_head_is_ignored(self):
        """RG-2. A genuinely new ce-code-review dir belonging to another run on
        another sha must not supply the verdict. Already guarded by the
        head_sha prefix match; this pins it."""
        for workflow in ("full-sdlc-api", "full-sdlc-web", "bugfix"):
            with self.subTest(workflow=workflow):
                r = self._probe(workflow, "foreign-sha", locale="C")
                self.assertIn(
                    f"verdict=[{self.ENVELOPE_VERDICT}] source=envelope", r["output"]
                )
                self.assertIn("rundir=[]", r["output"])


# ==========================================================================
# plan-loop (full-sdlc-api): plan-round-pre, plan-critic-gate, plan-converge
# ==========================================================================
def critique(verdict, blocking=0, nonblocking=0):
    def finding(sev, conf, kind="scope"):
        return {
            "kind": kind, "severity": sev, "confidence": conf,
            "section": "## Approach",
            "evidence": [{"source": "plan", "quote": "Change foo."}],
            "recommendation": "Narrow the unit.",
        }
    findings = [finding("P0", 100) for _ in range(blocking)]
    findings += [finding("P2", 50) for _ in range(nonblocking)]
    return {"verdict": verdict, "findings": findings}


def revision(declined_scope_100=0):
    return {
        "applied": [],
        "declined": [
            {"kind": "scope", "confidence": 100, "why": "spec says otherwise"}
            for _ in range(declined_scope_100)
        ],
    }


def plan_artifacts(art):
    copy_shared("plan-minimal", art)


def plan_round_pre_fixture(counter=None, cap=None):
    def build(tmp):
        art = tmp / "artifacts"
        plan_artifacts(art)
        if counter is not None:
            (art / "plan-round.txt").write_text(f"{counter}\n", encoding="utf-8")
        if cap is not None:
            (art / "plan-round-cap.txt").write_text(f"{cap}\n", encoding="utf-8")
    return build


def plan_critic_gate_fixture(doc):
    def build(tmp):
        art = tmp / "artifacts"
        plan_artifacts(art)
        (art / "plan-round.txt").write_text("1\n", encoding="utf-8")
        rd = art / "plan-round-1"
        rd.mkdir()
        if doc is not None:
            jdump(rd / "critique.json", doc)
    return build


def plan_converge_fixture(verdict, moved, dispute=0, blocking=0):
    def build(tmp):
        art = tmp / "artifacts"
        plan_artifacts(art)
        wt = tmp / "wt"
        wt.mkdir()
        jdump(art / "params.json", params(tmp, wt, spec=art / "spec.md"))
        (art / "plan-round.txt").write_text("1\n", encoding="utf-8")
        rd = art / "plan-round-1"
        rd.mkdir()
        pre = (art / "plan.md").read_text(encoding="utf-8")
        (rd / "plan.pre.md").write_text(
            pre.replace("Change foo.", "Change bar.") if moved else pre, encoding="utf-8"
        )
        for f in ("verify.json", "files-allowlist.json", "reader-audit.json"):
            shutil.copyfile(art / f, rd / f"pre-{f}")
        jdump(rd / "critique.json", critique(verdict, blocking=blocking))
        jdump(rd / "revision.json", revision(dispute))
    return build


class PlanLoopStress(unittest.TestCase):
    WF = "full-sdlc-api"

    def test_plan_round_pre_first_round(self):
        r = run_node(self.WF, "plan-round-pre", plan_round_pre_fixture())
        self.assertEqual(r["rc"], 0, r["output"])
        self.assertIn("PLAN_ROUND=1 cap=3", r["output"])

    def test_plan_round_pre_junk_counter_is_a_typed_stop(self):
        r = run_node(self.WF, "plan-round-pre", plan_round_pre_fixture(counter="two"))
        self.assertEqual(r["rc"], 1)
        self.assertIn("PLAN_ROUND_PRE=FAIL plan-round.txt is not an integer: [two]",
                      r["output"])

    def test_plan_round_pre_junk_cap_falls_back_to_three(self):
        r = run_node(self.WF, "plan-round-pre", plan_round_pre_fixture(cap="lots"))
        self.assertEqual(r["rc"], 0, r["output"])
        self.assertIn("PLAN_ROUND=1 cap=3", r["output"])

    def test_plan_round_pre_cap_reached(self):
        r = run_node(self.WF, "plan-round-pre", plan_round_pre_fixture(counter=3, cap=3))
        self.assertEqual(r["rc"], 1)
        self.assertIn("PLAN_ROUND_CAP round=3 cap=3", r["output"])

    def test_plan_critic_gate_valid_envelope(self):
        r = run_node(self.WF, "plan-critic-gate", plan_critic_gate_fixture(critique("ACCEPT")))
        self.assertEqual(r["rc"], 0, r["output"])
        self.assertIn("CRITIQUE round=1 verdict=ACCEPT scope=0", r["output"])

    def test_plan_critic_gate_missing_critique(self):
        r = run_node(self.WF, "plan-critic-gate", plan_critic_gate_fixture(None))
        self.assertEqual(r["rc"], 1)
        self.assertIn("CRITIC_GATE=FAIL no critique.json round=1", r["output"])

    def test_plan_converge_accept(self):
        r = run_node(self.WF, "plan-converge", plan_converge_fixture("ACCEPT", moved=True))
        self.assertEqual(r["rc"], 0, r["output"])
        self.assertIn("PLAN_CONVERGED round=1", r["output"])

    def test_plan_converge_revise_progressed(self):
        r = run_node(self.WF, "plan-converge", plan_converge_fixture("REVISE", moved=True))
        self.assertEqual(r["rc"], 0, r["output"])
        self.assertIn("PLAN_ROUND_PROGRESSED round=1", r["output"])

    def test_plan_converge_revise_no_progress(self):
        r = run_node(self.WF, "plan-converge", plan_converge_fixture("REVISE", moved=False))
        self.assertEqual(r["rc"], 1)
        self.assertIn("PLAN_NO_PROGRESS round=1 other_mutated=none", r["output"])

    def test_plan_converge_reject(self):
        r = run_node(self.WF, "plan-converge", plan_converge_fixture("REJECT", moved=False))
        self.assertEqual(r["rc"], 1)
        self.assertIn("PLAN_REJECTED round=1 plan_mutated=NO artifacts_mutated=none",
                      r["output"])

    def test_plan_converge_scope_dispute(self):
        r = run_node(self.WF, "plan-converge",
                     plan_converge_fixture("REVISE", moved=True, dispute=1))
        self.assertEqual(r["rc"], 1)
        self.assertIn("PLAN_SCOPE_DISPUTE round=1 declined_scope_100=1", r["output"])

    def test_plan_converge_accept_with_blocking_findings_is_refused(self):
        r = run_node(self.WF, "plan-converge",
                     plan_converge_fixture("ACCEPT", moved=True, blocking=1))
        self.assertEqual(r["rc"], 1)
        self.assertIn("CRITIC_GATE=FAIL verdict inconsistent round=1", r["output"])


# ==========================================================================
# rca-plan-loop + rca-gate + rca-plan-shape (bugfix)
# ==========================================================================
RCA_GATE_FIX = FIXTURES / "rca-gate"
RCA_MD = (RCA_GATE_FIX / "rca.md").read_text(encoding="utf-8")


def rca_artifacts(art):
    copy_shared("rca-minimal", art)
    shutil.copyfile(RCA_GATE_FIX / "chain-evidence.ts", art / "chain-evidence.ts")
    shutil.copyfile(RCA_GATE_FIX / "rca.md", art / "rca.md")
    jdump(art / "causal-chain.json", {"links": [
        {"step": "pager subtracts one",
         "evidence": {"quote": "const pageSize = limit - 1;", "file": "chain-evidence.ts"}},
        {"step": "last row is dropped", "fixable": True, "fix_site": "src/foo.ts:42",
         "evidence": {"quote": "const pageSize = limit - 1;", "file": "chain-evidence.ts"}},
    ]})
    jdump(art / "hypotheses.json", [{"id": "h1", "status": "confirmed-by-experiment"}])


def rca_gate_fixture(tmp):
    rca_artifacts(tmp / "artifacts")


def rca_round_pre_fixture(counter=None, cap=None):
    def build(tmp):
        art = tmp / "artifacts"
        rca_artifacts(art)
        prerun("bugfix", "rca-gate", tmp)          # writes the imm-* anchors
        if counter is not None:
            (art / "rca-round.txt").write_text(f"{counter}\n", encoding="utf-8")
        if cap is not None:
            (art / "rca-round-cap.txt").write_text(f"{cap}\n", encoding="utf-8")
    return build


def rca_critic_gate_fixture(doc):
    def build(tmp):
        art = tmp / "artifacts"
        rca_artifacts(art)
        (art / "rca-round.txt").write_text("1\n", encoding="utf-8")
        rd = art / "rca-round-1"
        rd.mkdir()
        if doc is not None:
            jdump(rd / "critique.json", doc)
    return build


def rca_converge_fixture(verdict, moved, dispute=0, mutate_immutable=False):
    def build(tmp):
        art = tmp / "artifacts"
        rca_artifacts(art)
        prerun("bugfix", "rca-gate", tmp)
        prerun("bugfix", "rca-round-pre", tmp)
        rd = art / "rca-round-1"
        if moved:
            # rca-round-pre snapshotted pre-fix-plan.json; move the live one.
            d = json.loads((art / "fix-plan.json").read_text(encoding="utf-8"))
            d["approach"] = "fix the off-by-one, narrowly"
            jdump(art / "fix-plan.json", d)
        if mutate_immutable:
            (art / "rca.md").write_text(RCA_MD + "\nEdited after the gate.\n",
                                        encoding="utf-8")
        jdump(rd / "critique.json", critique(verdict))
        jdump(rd / "revision.json", revision(dispute))
    return build


class RcaPlanLoopStress(unittest.TestCase):
    WF = "bugfix"

    def test_rca_gate(self):
        r = run_node(self.WF, "rca-gate", rca_gate_fixture)
        self.assertEqual(r["rc"], 0, r["output"])
        self.assertIn("RCA_GATE=PASS repo=api links=2 kind=unit", r["output"])

    def test_rca_plan_shape(self):
        r = run_node(self.WF, "rca-plan-shape", lambda tmp: rca_artifacts(tmp / "artifacts"))
        self.assertEqual(r["rc"], 0, r["output"])
        self.assertIn("RCA_PLAN_SHAPE=OK repo=api kind=unit", r["output"])

    def test_rca_plan_shape_fails_closed_on_bad_artifacts(self):
        def build(tmp):
            rca_artifacts(tmp / "artifacts")
            jdump(tmp / "artifacts" / "repo.json", {"repo": "both"})
        r = run_node(self.WF, "rca-plan-shape", build)
        self.assertEqual(r["rc"], 1)
        self.assertIn("RCA_PLAN_SHAPE=FAIL CROSS_REPO_BUG", r["output"])

    def test_rca_round_pre_first_round(self):
        r = run_node(self.WF, "rca-round-pre", rca_round_pre_fixture())
        self.assertEqual(r["rc"], 0, r["output"])
        self.assertIn("RCA_PLAN_ROUND=1 cap=3", r["output"])

    def test_rca_round_pre_junk_counter_is_a_typed_stop(self):
        r = run_node(self.WF, "rca-round-pre", rca_round_pre_fixture(counter="two"))
        self.assertEqual(r["rc"], 1)
        self.assertIn("RCA_ROUND_PRE=FAIL rca-round.txt is not an integer: [two]",
                      r["output"])

    def test_rca_round_pre_cap_reached(self):
        r = run_node(self.WF, "rca-round-pre", rca_round_pre_fixture(counter=3, cap=3))
        self.assertEqual(r["rc"], 1)
        self.assertIn("RCA_PLAN_ROUND_CAP round=3 cap=3", r["output"])

    def test_rca_critic_gate_valid_envelope(self):
        r = run_node(self.WF, "rca-critic-gate", rca_critic_gate_fixture(critique("ACCEPT")))
        self.assertEqual(r["rc"], 0, r["output"])
        self.assertIn("CRITIQUE round=1 verdict=ACCEPT", r["output"])

    def test_rca_critic_gate_missing_critique(self):
        r = run_node(self.WF, "rca-critic-gate", rca_critic_gate_fixture(None))
        self.assertEqual(r["rc"], 1)
        self.assertIn("CRITIC_GATE=FAIL no critique.json round=1", r["output"])

    def test_rca_converge_accept(self):
        r = run_node(self.WF, "rca-converge", rca_converge_fixture("ACCEPT", moved=True))
        self.assertEqual(r["rc"], 0, r["output"])
        self.assertIn("RCA_PLAN_CONVERGED round=1", r["output"])

    def test_rca_converge_revise_progressed(self):
        r = run_node(self.WF, "rca-converge", rca_converge_fixture("REVISE", moved=True))
        self.assertEqual(r["rc"], 0, r["output"])
        self.assertIn("RCA_PLAN_ROUND_PROGRESSED round=1", r["output"])

    def test_rca_converge_no_progress(self):
        r = run_node(self.WF, "rca-converge", rca_converge_fixture("REVISE", moved=False))
        self.assertEqual(r["rc"], 1)
        self.assertIn("RCA_PLAN_NO_PROGRESS round=1 other_mutated=none", r["output"])

    def test_rca_converge_reject(self):
        r = run_node(self.WF, "rca-converge", rca_converge_fixture("REJECT", moved=False))
        self.assertEqual(r["rc"], 1)
        self.assertIn("RCA_PLAN_REJECTED round=1 mutated=NO", r["output"])

    def test_rca_converge_scope_dispute(self):
        r = run_node(self.WF, "rca-converge",
                     rca_converge_fixture("REVISE", moved=True, dispute=1))
        self.assertEqual(r["rc"], 1)
        self.assertIn("RCA_PLAN_SCOPE_DISPUTE round=1 declined_scope_100=1", r["output"])

    def test_rca_converge_refuses_a_mutated_immutable_artifact(self):
        r = run_node(self.WF, "rca-converge",
                     rca_converge_fixture("ACCEPT", moved=True, mutate_immutable=True))
        self.assertEqual(r["rc"], 1)
        self.assertIn("RCA_PLAN=FAIL immutable artifact modified rca.md round=1",
                      r["output"])


# ==========================================================================
# gate-tests (api + web)
# ==========================================================================
def gate_tests_api_fixture(tmp):
    art = tmp / "artifacts"
    write_shims(tmp, ("bun",))
    wt = tmp / "wt"
    base = init_worktree(wt)
    jdump(art / "params.json", params(tmp, wt))
    jdump(art / "verify.json", {"test_patterns": ["src/foo.spec.ts"]})
    jdump(art / "files-allowlist.json", ["src/foo.ts"])
    (art / "bootstrap-head.txt").write_text(base + "\n", encoding="utf-8")
    (art / "commit-msg.txt").write_text("feat(foo): add one\n", encoding="utf-8")


WEB_TOY_WT = str(GOODWORD_ROOT / "web-app" / ".worktrees" / "archon-toy")


def gate_tests_web_fixture(tmp):
    write_shims(tmp, ("pnpm", "mise"))
    init_worktree(tmp / "wt")   # toy lane has no plan stage -> no allowlist
    return {"WT_FIXTURE": str(tmp / "wt")}


class GateTestsStress(unittest.TestCase):
    def test_full_sdlc_api(self):
        r = run_node("full-sdlc-api", "gate-tests", gate_tests_api_fixture)
        self.assertEqual(r["rc"], 0, r["output"])
        self.assertIn("GATE_TESTS=PASS", r["output"])
        self.assertIn("SCOPE_OK files=1", r["output"])

    def test_full_sdlc_api_typecheck_failure_is_typed(self):
        r = run_node("full-sdlc-api", "gate-tests", gate_tests_api_fixture,
                     env={"SHIM_RC_BUN_TYPECHECK": "1"})
        self.assertEqual(r["rc"], 1)
        self.assertIn("GATE_TESTS=FAIL typecheck", r["output"])

    def test_full_sdlc_api_scope_breach_is_typed(self):
        def build(tmp):
            gate_tests_api_fixture(tmp)
            (tmp / "wt" / "src" / "sneaky.ts").write_text("export const x = 1;\n",
                                                          encoding="utf-8")
        r = run_node("full-sdlc-api", "gate-tests", build)
        self.assertEqual(r["rc"], 1)
        self.assertIn("SCOPE_BREACH file=src/sneaky.ts", r["output"])
        self.assertIn("GATE_TESTS=FAIL scope breach", r["output"])

    def test_full_sdlc_web(self):
        # The web lane hardcodes its toy worktree path; bind it to the fixture.
        r = run_node("full-sdlc-web", "gate-tests", gate_tests_web_fixture,
                     subs=[(WEB_TOY_WT, '"$WT_FIXTURE"')])
        self.assertEqual(r["rc"], 0, r["output"])
        self.assertIn("SCOPE_GUARD=SKIP no files-allowlist.json", r["output"])
        self.assertIn("GATE_TESTS=PASS", r["output"])


# ==========================================================================
# deslop-verify: deslop-recheck + deslop-review-gate (api + bugfix),
# deslop-commit (bugfix only - the api lane commits via commit-impl)
# ==========================================================================
DESLOP_RESULT = {"changed_files": ["src/foo.ts"], "passes": ["comments"]}


def failing_test_json(command="true"):
    return {
        "repo": "api", "kind": "unit", "test_file": "src/foo.spec.ts",
        "test_name": "does the thing", "command": command,
        "predicted_failure_signature": "expected 1 to equal 2",
    }


def deslop_common(tmp, lane):
    art = tmp / "artifacts"
    write_shims(tmp, ("bun",))
    wt = tmp / "wt"
    base = init_worktree(wt)
    jdump(art / "params.json", params(tmp, wt))
    jdump(art / "verify.json", {"test_patterns": ["src/foo.spec.ts"]})
    jdump(art / "files-allowlist.json", ["src/foo.ts", "src/foo.spec.ts"])
    jdump(art / "deslop-result.json", DESLOP_RESULT)
    if lane == "api":
        (art / "bootstrap-head.txt").write_text(base + "\n", encoding="utf-8")
    else:
        (art / "repo.txt").write_text("api\n", encoding="utf-8")
        (art / "red-sha.txt").write_text(base + "\n", encoding="utf-8")
        jdump(art / "failing-test.json", failing_test_json())
    return base


def deslop_recheck_fixture(lane):
    def build(tmp):
        deslop_common(tmp, lane)
    return build


def deslop_review(verdict="CLEAN", blocking=0):
    guards = ["complexity", "tautological_tests", "yagni", "open_closed", "comments"]
    doc = {
        "verdict": verdict,
        "coverage": {g: {"status": "assessed", "evidence": f"checked {g}"} for g in guards},
        "findings": [
            {"guard": "comments", "file": "src/foo.ts", "line": 2,
             "confidence": 75, "evidence": "narrating comment"}
            for _ in range(blocking)
        ],
    }
    return doc


def deslop_review_gate_fixture(lane, review_doc):
    workflow = "full-sdlc-api" if lane == "api" else "bugfix"

    def build(tmp):
        deslop_common(tmp, lane)
        prerun(workflow, "deslop-recheck", tmp)   # takes the real checkpoint
        jdump(tmp / "artifacts" / "deslop-review.json", review_doc)
    return build


# deslop-commit is the one covered node that CREATES a commit, and a commit sha
# embeds the commit timestamp. Left to the clock, two runs land on the same sha
# only when they fall in the same second — so the harness would pass on a fast
# machine and report a moving SHA slot on a slow one. Pinning the dates makes
# the timestamp an input like any other, and the resulting sha a value the
# stability check genuinely verifies rather than one it has to excuse.
GIT_FIXED_DATE = {
    "GIT_AUTHOR_DATE": "2026-01-02T03:04:05+00:00",
    "GIT_COMMITTER_DATE": "2026-01-02T03:04:05+00:00",
}


def deslop_commit_fixture(dirty=True):
    def build(tmp):
        art = tmp / "artifacts"
        wt = tmp / "wt"
        base = init_worktree(wt, dirty=dirty)
        jdump(art / "params.json", params(tmp, wt))
        (art / "red-sha.txt").write_text(base + "\n", encoding="utf-8")
        jdump(art / "failing-test.json", failing_test_json())
        return dict(GIT_FIXED_DATE)
    return build


class DeslopStress(unittest.TestCase):
    def test_deslop_recheck_api(self):
        r = run_node("full-sdlc-api", "deslop-recheck", deslop_recheck_fixture("api"))
        self.assertEqual(r["rc"], 0, r["output"])
        self.assertIn("DESLOP_GATE=PASS files=1 round=1 checkpoint=", r["output"])

    def test_deslop_recheck_bugfix(self):
        r = run_node("bugfix", "deslop-recheck", deslop_recheck_fixture("bugfix"))
        self.assertEqual(r["rc"], 0, r["output"])
        self.assertIn("DESLOP_GATE=PASS files=1 round=1 checkpoint=", r["output"])

    def test_deslop_recheck_api_lint_failure_is_typed(self):
        r = run_node("full-sdlc-api", "deslop-recheck", deslop_recheck_fixture("api"),
                     env={"SHIM_RC_BUN_LINT": "1"})
        self.assertEqual(r["rc"], 1)
        self.assertIn("DESLOP_GATE=FAIL lint round=1", r["output"])

    def test_deslop_recheck_bugfix_repro_regression_is_typed(self):
        def build(tmp):
            deslop_common(tmp, "bugfix")
            jdump(tmp / "artifacts" / "failing-test.json", failing_test_json("false"))
        r = run_node("bugfix", "deslop-recheck", build)
        self.assertEqual(r["rc"], 1)
        self.assertIn("DESLOP_GATE=FAIL repro no longer green round=1 rc=1", r["output"])

    def test_deslop_review_gate_api_clean(self):
        r = run_node("full-sdlc-api", "deslop-review-gate",
                     deslop_review_gate_fixture("api", deslop_review()))
        self.assertEqual(r["rc"], 0, r["output"])
        self.assertIn("DESLOP=CLEAN round=1", r["output"])

    def test_deslop_review_gate_bugfix_clean(self):
        r = run_node("bugfix", "deslop-review-gate",
                     deslop_review_gate_fixture("bugfix", deslop_review()))
        self.assertEqual(r["rc"], 0, r["output"])
        self.assertIn("DESLOP=CLEAN round=1", r["output"])

    def test_deslop_review_gate_api_dirty(self):
        r = run_node("full-sdlc-api", "deslop-review-gate",
                     deslop_review_gate_fixture("api", deslop_review("DIRTY", blocking=1)))
        self.assertEqual(r["rc"], 1)
        self.assertIn("DESLOP=DIRTY round=1 blocking=1", r["output"])

    def test_deslop_review_gate_bugfix_dirty(self):
        r = run_node("bugfix", "deslop-review-gate",
                     deslop_review_gate_fixture("bugfix", deslop_review("DIRTY", blocking=1)))
        self.assertEqual(r["rc"], 1)
        self.assertIn("DESLOP=DIRTY round=1 blocking=1", r["output"])

    def test_deslop_review_gate_api_detects_a_reviewer_edit(self):
        def build(tmp):
            deslop_review_gate_fixture("api", deslop_review())(tmp)
            (tmp / "wt" / "src" / "foo.ts").write_text(
                "export function foo(x: number): number {\n  return x + 3;\n}\n",
                encoding="utf-8")
        r = run_node("full-sdlc-api", "deslop-review-gate", build)
        self.assertEqual(r["rc"], 1)
        self.assertIn("DESLOP_REVIEW=FAIL reviewer modified tree round=1", r["output"])

    def test_deslop_commit_commits(self):
        r = run_node("bugfix", "deslop-commit", deslop_commit_fixture(dirty=True))
        self.assertEqual(r["rc"], 0, r["output"])
        self.assertIn("DESLOP_COMMIT=OK sha=<SHA:1>", r["output"])

    def test_deslop_commit_skips_a_clean_tree(self):
        r = run_node("bugfix", "deslop-commit", deslop_commit_fixture(dirty=False))
        self.assertEqual(r["rc"], 0, r["output"])
        self.assertIn("DESLOP_COMMIT=SKIP nothing to commit sha=<SHA:1>", r["output"])

    def test_deslop_commit_refuses_a_modified_repro(self):
        def build(tmp):
            deslop_commit_fixture(dirty=True)(tmp)
            (tmp / "wt" / "src" / "foo.spec.ts").write_text(
                BASE_SPEC.replace("toBe(2)", "toBe(3)"), encoding="utf-8")
        r = run_node("bugfix", "deslop-commit", build)
        self.assertEqual(r["rc"], 1)
        self.assertIn("DESLOP=FAIL repro test modified", r["output"])


# ==========================================================================
# green-check (bugfix)
# ==========================================================================
def green_check_fixture(modify_repro=False, command="true"):
    def build(tmp):
        art = tmp / "artifacts"
        write_shims(tmp, ("bun",))
        wt = tmp / "wt"
        base = init_worktree(wt)
        jdump(art / "params.json", params(tmp, wt))
        (art / "fix-attempt.txt").write_text("1\n", encoding="utf-8")
        (art / "attempt-1").mkdir()
        (art / "repo.txt").write_text("api\n", encoding="utf-8")
        (art / "red-sha.txt").write_text(base + "\n", encoding="utf-8")
        jdump(art / "failing-test.json", failing_test_json(command))
        jdump(art / "verify.json", {"test_patterns": ["src/foo.spec.ts"]})
        jdump(art / "files-allowlist.json", ["src/foo.ts", "src/foo.spec.ts"])
        if modify_repro:
            (wt / "src" / "foo.spec.ts").write_text(
                BASE_SPEC.replace("toBe(2)", "toBe(3)"), encoding="utf-8")
    return build


class GreenCheckStress(unittest.TestCase):
    def test_green(self):
        r = run_node("bugfix", "green-check", green_check_fixture())
        self.assertEqual(r["rc"], 0, r["output"])
        self.assertIn("GREEN_CHECK attempt=1 green=true reason=[ok]", r["output"])

    def test_not_green_when_the_repro_was_modified(self):
        r = run_node("bugfix", "green-check", green_check_fixture(modify_repro=True))
        self.assertEqual(r["rc"], 0, r["output"])
        self.assertIn("GREEN_CHECK attempt=1 green=false reason=[repro test modified]",
                      r["output"])

    def test_not_green_when_the_repro_still_fails(self):
        r = run_node("bugfix", "green-check", green_check_fixture(command="false"))
        self.assertEqual(r["rc"], 0, r["output"])
        self.assertIn("green=false reason=[repro still failing rc=1]", r["output"])


# ==========================================================================
# converge (full-sdlc-api + bugfix)
# ==========================================================================
def fixer_result(failed=0, advisory=0, incomplete=0):
    return {
        "applied": [],
        "failed": [{"finding": "f", "reason": "could not fix"} for _ in range(failed)],
        "advisory": [{"finding": "Waived: out of scope", "action": "declined"}
                     for _ in range(advisory)],
        "incomplete": [{"finding": "i", "reason": "budget"} for _ in range(incomplete)],
    }


def converge_fixture(lane, verdict, fixer=None, dirty=False, cap=None, accept=False):
    workflow_lane = lane

    def build(tmp):
        art = tmp / "artifacts"
        wt = tmp / "wt"
        base = init_worktree(wt, dirty=dirty)
        jdump(art / "params.json", params(tmp, wt))
        (art / "round.txt").write_text("1\n", encoding="utf-8")
        rd = art / "round-1"
        rd.mkdir()
        (rd / "pre-head.txt").write_text(base + "\n", encoding="utf-8")
        jdump(rd / "review-summary.json",
              {"verdict": verdict, "residual_count": -1, "degraded": False})
        jdump(rd / "fixer-result.json", fixer if fixer is not None else fixer_result())
        jdump(art / "files-allowlist.json", ["src/foo.ts"])
        if cap is not None:
            (art / "round-cap.txt").write_text(f"{cap}\n", encoding="utf-8")
        if accept:
            (art / "accept-residuals.txt").write_text("accepted by test\n", encoding="utf-8")
        if workflow_lane == "full-sdlc-api":
            (art / "bootstrap-head.txt").write_text(base + "\n", encoding="utf-8")
        else:
            (art / "red-sha.txt").write_text(base + "\n", encoding="utf-8")
            jdump(art / "failing-test.json", failing_test_json())
    return build


class ConvergeStress(unittest.TestCase):
    LANES = ("full-sdlc-api", "bugfix")

    def test_converged(self):
        for lane in self.LANES:
            with self.subTest(lane=lane):
                r = run_node(lane, "converge",
                             converge_fixture(lane, "Ready to merge"))
                self.assertEqual(r["rc"], 0, r["output"])
                self.assertIn("CONVERGED round=1", r["output"])

    def test_no_progress(self):
        for lane in self.LANES:
            with self.subTest(lane=lane):
                r = run_node(lane, "converge", converge_fixture(lane, "Not ready"))
                self.assertEqual(r["rc"], 1)
                self.assertIn("NO_PROGRESS round=1 (Not ready and nothing changed)",
                              r["output"])

    def test_fixer_blocked(self):
        for lane in self.LANES:
            with self.subTest(lane=lane):
                r = run_node(lane, "converge",
                             converge_fixture(lane, "Ready to merge",
                                              fixer=fixer_result(failed=1)))
                self.assertEqual(r["rc"], 1)
                self.assertIn("FIXER_BLOCKED round=1", r["output"])

    def test_unknown_verdict(self):
        for lane in self.LANES:
            with self.subTest(lane=lane):
                r = run_node(lane, "converge", converge_fixture(lane, "Looks fine"))
                self.assertEqual(r["rc"], 1)
                self.assertIn("CONVERGE=FAIL unknown verdict [Looks fine]", r["output"])

    def test_round_cap_reached(self):
        for lane in self.LANES:
            with self.subTest(lane=lane):
                r = run_node(lane, "converge",
                             converge_fixture(lane, "Ready with fixes",
                                              fixer=fixer_result(incomplete=1), cap=1))
                self.assertEqual(r["rc"], 1)
                self.assertIn("ROUND_CAP_REACHED round=1", r["output"])

    def test_human_accepted_residuals_converge(self):
        for lane in self.LANES:
            with self.subTest(lane=lane):
                r = run_node(lane, "converge",
                             converge_fixture(lane, "Ready with fixes",
                                              fixer=fixer_result(incomplete=1),
                                              cap=1, accept=True))
                self.assertEqual(r["rc"], 0, r["output"])
                self.assertIn("CONVERGED round=1 (human accepted residuals)", r["output"])

    def test_scope_breach(self):
        for lane in self.LANES:
            with self.subTest(lane=lane):
                def build(tmp, lane=lane):
                    converge_fixture(lane, "Ready to merge")(tmp)
                    (tmp / "wt" / "src" / "sneaky.ts").write_text("export const x = 1;\n",
                                                                  encoding="utf-8")
                r = run_node(lane, "converge", build)
                self.assertEqual(r["rc"], 1)
                self.assertIn("SCOPE_BREACH round=1 file=src/sneaky.ts", r["output"])


# ==========================================================================
# round-pre: the PRODUCER half of the pair ReviewGateScanIsolation covers from
# the consumer side. It writes `round-N/prerun-dirs.txt` — the baseline
# `review-gate` later takes `comm -13` against — so RG-1 lives in this node as
# much as in the gate (AUDIT.md counts six sites; three of them are here).
#
# The api and bugfix lanes reach their worktree through params.json; the web
# lane hardcodes its toy worktree (AUDIT row GT-2) and is bound to the fixture
# exactly as GateTestsStress binds gate-tests.
# ==========================================================================
ROUND_PRE_GUARD = '2>/dev/null | LC_ALL=C sort > "$RD/prerun-dirs.txt"'
ROUND_PRE_UNGUARDED = '2>/dev/null | sort > "$RD/prerun-dirs.txt"'
WEB_WT_SUB = [(WEB_TOY_WT, '"$WT_FIXTURE"')]
ROUND_PRE_LANES = ("full-sdlc-api", "full-sdlc-web", "bugfix")


def seeded_listing(names):
    """`prerun-dirs.txt` as the snapshot normalizes it: one absolute path per
    seeded dir, trailing slash, the fixture root collapsed to <ROOT>."""
    return "".join(f"<ROOT>/ce-review-root/{n}/\n" for n in names)


def round_pre_fixture(seeds=C_ORDER, counter=None):
    """A per-run CE_REVIEW_ROOT holding exactly `seeds`, plus a throwaway
    worktree for the `git rev-parse HEAD` the node opens with.

    The default seeds are C_ORDER — `ce-Beta`, `ce-alpha` — because C collation
    orders them Beta-before-alpha and en_US.UTF-8 orders them the other way, so
    the file this node writes is only stable if the sort's collation is pinned.
    """
    def build(tmp):
        art = tmp / "artifacts"
        wt = tmp / "wt"
        init_worktree(wt)
        jdump(art / "params.json", params(tmp, wt))
        if counter is not None:
            (art / "round.txt").write_text(f"{counter}\n", encoding="utf-8")
        ce = tmp / "ce-review-root"
        ce.mkdir()
        for name in seeds:
            (ce / name).mkdir()
        return {"CE_REVIEW_ROOT": str(ce), "WT_FIXTURE": str(wt)}

    return build


class RoundPreStress(unittest.TestCase):
    """The producer side of the ce-code-review discovery pair, all three lanes."""

    def _run(self, workflow, fixture, **kw):
        subs = kw.pop("subs", [])
        if workflow == "full-sdlc-web":
            subs = WEB_WT_SUB + list(subs)
        return run_node(workflow, "round-pre", fixture, subs=subs or None, **kw)

    def test_first_round_lists_exactly_the_seeded_dirs_in_c_order(self):
        for workflow in ROUND_PRE_LANES:
            with self.subTest(workflow=workflow):
                r = self._run(workflow, round_pre_fixture())
                self.assertEqual(r["rc"], 0, r["output"])
                self.assertIn("ROUND=1 head=<SHA:1>", r["output"])
                self.assertEqual(r["files"]["round.txt"], "1\n")
                self.assertEqual(r["files"]["round-1/pre-head.txt"], "<SHA:1>\n")
                self.assertEqual(r["files"]["round-1/prerun-dirs.txt"],
                                 seeded_listing(C_ORDER))

    def test_counter_advances_and_writes_into_the_new_round_dir(self):
        for workflow in ROUND_PRE_LANES:
            with self.subTest(workflow=workflow):
                r = self._run(workflow, round_pre_fixture(counter=2))
                self.assertEqual(r["rc"], 0, r["output"])
                self.assertIn("ROUND=3 head=<SHA:1>", r["output"])
                self.assertEqual(r["files"]["round-3/prerun-dirs.txt"],
                                 seeded_listing(C_ORDER))

    def test_empty_ce_review_root_yields_an_empty_listing_not_a_failure(self):
        """`ls` of an empty root exits non-zero; `|| true` plus the `touch` is
        what turns that into an empty baseline rather than a dead round. With
        `set -euo pipefail` above it, that swallow is load-bearing."""
        for workflow in ROUND_PRE_LANES:
            with self.subTest(workflow=workflow):
                r = self._run(workflow, round_pre_fixture(seeds=()))
                self.assertEqual(r["rc"], 0, r["output"])
                self.assertEqual(r["files"]["round-1/prerun-dirs.txt"], "")

    def test_listing_is_c_ordered_under_a_utf8_locale(self):
        """RG-1, producer side. The consumer sorts with `LC_ALL=C`; if this node
        honored the ambient locale instead, the two listings would disagree and
        `comm` would report a pre-existing dir as new."""
        require_locale(self, UTF8_LOCALE)
        for workflow in ROUND_PRE_LANES:
            with self.subTest(workflow=workflow):
                r = self._run(workflow, round_pre_fixture(),
                              env={"LC_ALL": UTF8_LOCALE})
                self.assertEqual(r["files"]["round-1/prerun-dirs.txt"],
                                 seeded_listing(C_ORDER))

    def test_negative_control_unguarded_sort_follows_the_ambient_locale(self):
        """The guard above is load-bearing: revert `LC_ALL=C sort` to a bare
        `sort` and the same fixture writes alpha-before-Beta under en_US.UTF-8.
        If this ever starts producing C order the fixture stopped reproducing
        and the guard test above is proving nothing."""
        require_locale(self, UTF8_LOCALE)
        r = self._run("full-sdlc-api", round_pre_fixture(),
                      env={"LC_ALL": UTF8_LOCALE},
                      subs=[(ROUND_PRE_GUARD, ROUND_PRE_UNGUARDED)])
        self.assertEqual(r["files"]["round-1/prerun-dirs.txt"],
                         seeded_listing(tuple(reversed(C_ORDER))))


# ==========================================================================
# wrap-review: the standalone W6 wrapper, same snapshot-then-set-difference
# shape as round-pre/review-gate with two differences that matter — the two
# listings live directly in ARTIFACTS_DIR rather than under round-N/, and the
# head_sha match is EXACT rather than a prefix, with no envelope fallback, so
# a run dir the gate cannot claim is a typed FAIL rather than a fallback read.
#
# Both nodes hardcode the M0.6c fixture repo the wrapper was proven against;
# `pre` cd's into it, so it is bound to the harness worktree the same way the
# web lane's is. `gate` assigns the same literal and never reads it.
# ==========================================================================
WRAP_REVIEW_REPO = (
    "/private/tmp/claude-501/-Users-eduardopicazo-Documents-Workspace-Goodword/"
    "88bb31f2-0094-41b2-bd3f-8409f1264538/scratchpad/m1-fixture/repo"
)
WRAP_PRE_SUB = [(WRAP_REVIEW_REPO, '"$WT_FIXTURE"')]


def wrap_review_pre_fixture(seeds=C_ORDER):
    def build(tmp):
        wt = tmp / "wt"
        init_worktree(wt)
        ce = tmp / "ce-review-root"
        ce.mkdir()
        for name in seeds:
            (ce / name).mkdir()
        return {"CE_REVIEW_ROOT": str(ce), "WT_FIXTURE": str(wt)}

    return build


def wrap_review_gate_fixture(head_sha=PREHEAD, verdict="Ready with fixes"):
    """`head_sha=None` seeds no run dir at all. This gate has no envelope
    fallback, so that is its FAIL path, not a degraded PASS."""
    def build(tmp):
        art = tmp / "artifacts"
        (art / "pre-head.txt").write_text(PREHEAD + "\n", encoding="utf-8")
        (art / "prerun-dirs.txt").write_text("", encoding="utf-8")
        ce = tmp / "ce-review-root"
        ce.mkdir()
        if head_sha is not None:
            run = ce / "run-0001"
            run.mkdir()
            jdump(run / "metadata.json", {"head_sha": head_sha, "verdict": verdict})
        return {"CE_REVIEW_ROOT": str(ce)}

    return build


class WrapReviewStress(unittest.TestCase):
    WF = "wrap-review"

    def test_pre_snapshots_head_and_the_run_dir_set(self):
        r = run_node(self.WF, "pre", wrap_review_pre_fixture(), subs=WRAP_PRE_SUB)
        self.assertEqual(r["rc"], 0, r["output"])
        self.assertIn("PRE_OK head=<SHA:1>", r["output"])
        self.assertEqual(r["files"]["pre-head.txt"], "<SHA:1>\n")
        self.assertEqual(r["files"]["prerun-dirs.txt"], seeded_listing(C_ORDER))

    def test_gate_reads_the_matching_run_dir(self):
        r = run_node(self.WF, "gate", wrap_review_gate_fixture(),
                     outputs={"review": envelope_with("Ready with fixes")})
        self.assertEqual(r["rc"], 0, r["output"])
        self.assertIn("GATE_1_review_complete_present=PASS", r["output"])
        self.assertIn("GATE_2_degraded_absent=PASS", r["output"])
        self.assertIn(
            "GATE_3_verdict_in_enum=PASS verdict=[Ready with fixes] "
            "rundir=[<ROOT>/ce-review-root/run-0001/]", r["output"])
        self.assertIn("W6_GATE=PASS", r["output"])
        self.assertEqual(
            json.loads(r["files"]["review-summary.json"]),
            {"verdict": "Ready with fixes", "residual_count": -1, "degraded": False})

    def test_gate_without_a_matching_run_dir_is_a_typed_fail(self):
        """Exact-match, no fallback: an envelope carrying a valid verdict does
        NOT rescue a missing metadata.json here, unlike the lanes' review-gate."""
        r = run_node(self.WF, "gate", wrap_review_gate_fixture(head_sha=None),
                     outputs={"review": envelope_with("Ready to merge")})
        self.assertEqual(r["rc"], 1)
        self.assertIn("GATE_3_verdict_in_enum=FAIL verdict=[] rundir=[]", r["output"])
        self.assertIn("W6_GATE=FAIL", r["output"])

    def test_gate_rejects_a_run_dir_from_a_different_head(self):
        r = run_node(self.WF, "gate", wrap_review_gate_fixture(head_sha=FOREIGN_SHA),
                     outputs={"review": envelope_with("Ready to merge")})
        self.assertEqual(r["rc"], 1)
        self.assertIn("GATE_3_verdict_in_enum=FAIL verdict=[] rundir=[]", r["output"])
        self.assertIn("W6_GATE=FAIL", r["output"])


# ==========================================================================
# AUDIT row RG-4, the residual: CE_REVIEW_ROOT is honored on the READ side
# only, so on a real host every concurrent lane scans ONE directory. Every
# other group here hands each repetition a private root, which is what makes
# them hermetic — and also means none of them exercises the arrangement
# production actually runs in. This one deliberately gives all N repetitions
# the SAME root, holding a foreign run dir, and requires the gate to stay on
# the envelope. No new harness hook was needed: `run_node(env=…)` is applied to
# every repetition before the fixture's own vars, so a builder that simply
# declines to return CE_REVIEW_ROOT leaves the shared value standing.
# ==========================================================================
class ReviewGateSharedRoot(unittest.TestCase):
    def test_concurrent_repetitions_sharing_one_root_ignore_a_foreign_run(self):
        shared = Path(tempfile.mkdtemp(prefix="nodestress-shared-ce-"))
        self.addCleanup(shutil.rmtree, shared, ignore_errors=True)
        foreign = shared / "ce-foreign"
        foreign.mkdir()
        jdump(foreign / "metadata.json",
              {"head_sha": FOREIGN_SHA, "verdict": "Not ready"})

        def build(tmp):
            art = tmp / "artifacts"
            rd = art / "round-1"
            rd.mkdir(parents=True)
            (art / "round.txt").write_text("1\n", encoding="utf-8")
            (rd / "pre-head.txt").write_text(PREHEAD + "\n", encoding="utf-8")
            (rd / "prerun-dirs.txt").write_text("", encoding="utf-8")
            # Deliberately no CE_REVIEW_ROOT: the shared one from env stands.
            return {}

        r = run_node("full-sdlc-api", "review-gate", build,
                     outputs={"review": envelope_with("Ready to merge")},
                     env={"CE_REVIEW_ROOT": str(shared)})
        self.assertEqual(r["rc"], 0, r["output"])
        self.assertEqual(r["identical"], r["n"])
        self.assertIn(
            "GATE_3_verdict_in_enum=PASS verdict=[Ready to merge] "
            "source=envelope rundir=[]", r["output"])
        self.assertIn("REVIEW_GATE=PASS round=1", r["output"])


# ==========================================================================
# AUDIT row ENV-1: no covered node body reads NODE_ENV, DISABLE_OMC, HOME or
# TZ, and after the LC_ALL=C fix none is collation-sensitive either. That is a
# claim about the bodies, so it gets a test rather than a comment.
# ==========================================================================
class EnvInvariance(unittest.TestCase):
    PERTURBED = {
        "NODE_ENV": "production",
        "DISABLE_OMC": "1",
        "TZ": "Asia/Tokyo",
        "LANG": "tr_TR.UTF-8",
        "LC_ALL": "tr_TR.UTF-8",
    }

    def _same(self, workflow, node, builder, **kw):
        base = run_node(workflow, node, builder, n=1, **kw)
        alt = run_node(workflow, node, builder, n=1, env=dict(self.PERTURBED), **kw)
        self.assertEqual(base["rc"], alt["rc"], alt["output"])
        self.assertEqual(base["typed"], alt["typed"], alt["output"])

    def test_review_gate(self):
        require_locale(self, "tr_TR.UTF-8")
        self._same("full-sdlc-api", "review-gate",
                   review_gate_fixture("Ready with fixes", "envelope"),
                   outputs={"review": envelope_with("Ready with fixes")})

    def test_converge(self):
        require_locale(self, "tr_TR.UTF-8")
        self._same("full-sdlc-api", "converge",
                   converge_fixture("full-sdlc-api", "Ready to merge"))

    def test_gate_tests(self):
        require_locale(self, "tr_TR.UTF-8")
        self._same("full-sdlc-api", "gate-tests", gate_tests_api_fixture)

    def test_deslop_recheck(self):
        require_locale(self, "tr_TR.UTF-8")
        self._same("full-sdlc-api", "deslop-recheck", deslop_recheck_fixture("api"))


if __name__ == "__main__":
    unittest.main()
