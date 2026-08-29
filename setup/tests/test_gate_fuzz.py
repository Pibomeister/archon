"""Corpus-driven fuzz test: feeds deterministic, seeded mutations of the three
AI-authored JSON envelopes (deslop-review.json, critique.json, revision.json)
through the REAL extracted gate bodies (deslop-review-gate, plan-critic-gate /
rca-critic-gate, plan-converge / rca-converge, both lanes) and asserts each
variant produces exactly one documented gate-terminal typed line, no Python
traceback on stderr, and an exit code consistent with that line's PASS/FAIL
class. See RUNBOOK.md section 3/3a/3b for the typed-line vocabulary this
tests against.

impact.json and recheck.json are NOT covered here: impact.json is read only
by AI-node prompts (never `json.load`'d by any bash gate — see impact-probe
in full-sdlc-api.yaml / bugfix.yaml, an AI node, not a bash node), and
recheck.json is generated entirely by deslop-recheck itself from integer
subprocess exit codes (never AI-authored, never read back by any other
node) — see test_gate_fuzz.py's module docstring companion note in the
mission report for the full reasoning.

Default corpus size is 20 variants per (file, gate, lane) group; set
NODE_STRESS=100 for the full 100.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from nodes.extract import runnable_body
from nodes.fuzz import variants

ROOT = Path(__file__).resolve().parent.parent.parent  # .archon/
FIX = Path(__file__).resolve().parent / "nodes" / "fixtures" / "fuzz"
SEEDS = FIX / "seeds"

N = 100 if os.environ.get("NODE_STRESS") == "100" else 20
TRACEBACK = "Traceback (most recent call last)"


def load_seed(name):
    return json.loads((SEEDS / name).read_text(encoding="utf-8"))


def combined_variants(seed_names, n, seed=0):
    """Split n variants roughly evenly across multiple seeds of the same
    file shape (e.g. a CLEAN/ACCEPT seed and a DIRTY/blocking seed), so
    fields that are only present in the "populated" seed (findings[],
    confidence, line, guard...) actually get exercised by the mutation
    classes that target them."""
    seeds = [load_seed(name) for name in seed_names]
    out = []
    per = -(-n // len(seeds))  # ceil
    for i, s in enumerate(seeds):
        out.extend(variants(s, per, seed=f"{seed}:{i}"))
    return out[:n]


def run_body(workflow, node_id, artifacts_dir, outputs=None, cwd=None):
    body = runnable_body(workflow, node_id, outputs=outputs or {})
    env = dict(os.environ, ARTIFACTS_DIR=str(artifacts_dir))
    return subprocess.run(["bash", "-c", body], capture_output=True, text=True, env=env, cwd=cwd)


def git(args, cwd, env=None):
    full_env = dict(os.environ, **(env or {}))
    full_env.setdefault("GIT_AUTHOR_NAME", "fuzz")
    full_env.setdefault("GIT_AUTHOR_EMAIL", "fuzz@example.com")
    full_env.setdefault("GIT_COMMITTER_NAME", "fuzz")
    full_env.setdefault("GIT_COMMITTER_EMAIL", "fuzz@example.com")
    return subprocess.run(["git"] + args, cwd=cwd, env=full_env, capture_output=True, text=True, check=True)


# ---------------------------------------------------------------------------
# Fixture builders. Each returns the ARTIFACTS_DIR ready for the gate to run,
# with everything EXCEPT the fuzzed target file already in place and valid.
# ---------------------------------------------------------------------------

def make_deslop_review_gate_fixture(tmp):
    """deslop-review-gate needs a real git worktree (it recomputes HEAD/tree
    checksums to detect reviewer tampering) plus a checkpoint taken with the
    worktree unchanged, so the tamper check passes and execution reaches the
    deslop-review.json parser — the part under test."""
    wt = tmp / "wt"
    wt.mkdir()
    git(["init", "-q"], cwd=wt)
    (wt / "README.md").write_text("hello\n", encoding="utf-8")
    git(["add", "-A"], cwd=wt)
    git(["commit", "-q", "-m", "init"], cwd=wt)

    ad = tmp / "artifacts"
    ad.mkdir()
    spec = tmp / "spec.md"
    spec.write_text("# Spec\n", encoding="utf-8")
    (ad / "params.json").write_text(json.dumps({
        "spec": str(spec), "slug": "x", "branch": "archon/x", "worktree": str(wt),
    }), encoding="utf-8")
    (ad / "deslop-round.txt").write_text("1\n", encoding="utf-8")
    rd = ad / "deslop-round-1"
    rd.mkdir()

    index_file = rd / "index"
    env = {"GIT_INDEX_FILE": str(index_file)}
    git(["add", "-A"], cwd=wt, env=env)
    cktree = git(["write-tree"], cwd=wt, env=env).stdout.strip()
    (rd / "checkpoint-tree.txt").write_text(cktree + "\n", encoding="utf-8")
    with open(rd / "checkpoint.tar", "wb") as fh:
        subprocess.run(["git", "archive", cktree], cwd=wt, stdout=fh, check=True)
    head = git(["rev-parse", "HEAD"], cwd=wt).stdout.strip()
    ckindex = git(["write-tree"], cwd=wt).stdout.strip()
    (ad / "deslop-tree.txt").write_text(f"head={head}\nindex={ckindex}\ncheckpoint={cktree}\n", encoding="utf-8")
    return ad


PLAN_MD = (
    "## Goal\nDo the thing.\n\n"
    "## Files\n- apps/api/src/app/x.ts\n\n"
    "## Approach\n1. Change x.\n\n"
    "## Test scenarios\n- x behaves.\n\n"
    "## Verification\nbun run test -- x\n"
)


def make_plan_converge_fixture(tmp, critique_verdict_baseline):
    """Full 'shape-complete' fixture: plan.md/verify.json/files-allowlist.json/
    reader-audit.json are all valid, so an ACCEPT-verdict critique.json that
    survives its mutation unbroken reaches plan-shape.sh and converges
    cleanly (a true PASS), rather than every variant funneling into the same
    shape-fail path regardless of what was actually fuzzed."""
    wt = tmp / "wt"
    wt.mkdir()
    ad = tmp / "artifacts"
    ad.mkdir()
    spec = tmp / "spec.md"
    spec.write_text("# Spec\nNo premises section here.\n", encoding="utf-8")  # no "## Premises to verify"
    (ad / "params.json").write_text(json.dumps({
        "spec": str(spec), "slug": "x", "branch": "archon/x", "worktree": str(wt),
    }), encoding="utf-8")
    (ad / "plan.md").write_text(PLAN_MD, encoding="utf-8")
    (ad / "verify.json").write_text(json.dumps({"test_patterns": ["x.spec"]}), encoding="utf-8")
    (ad / "files-allowlist.json").write_text(json.dumps(["apps/api/src/app/x.ts"]), encoding="utf-8")
    (ad / "reader-audit.json").write_text(json.dumps({"columns": []}), encoding="utf-8")
    (ad / "plan-round.txt").write_text("1\n", encoding="utf-8")
    rd = ad / "plan-round-1"
    rd.mkdir()
    (rd / "plan.pre.md").write_text(PLAN_MD, encoding="utf-8")
    for f in ("verify.json", "files-allowlist.json", "reader-audit.json"):
        shutil.copyfile(ad / f, rd / f"pre-{f}")
    if critique_verdict_baseline == "REVISE":
        (rd / "critique.json").write_text(json.dumps({"verdict": "REVISE", "findings": []}), encoding="utf-8")
    (rd / "revision.json").write_text(json.dumps({"applied": [], "declined": []}), encoding="utf-8")
    return ad, rd


REPO_JSON = {"repo": "api"}
FAILING_TEST_JSON = {
    "repo": "api", "kind": "unit", "test_file": "apps/api/src/app/x.spec.ts",
    "test_name": "does the thing", "command": "bun run test -- x",
    "predicted_failure_signature": "expected true to be false",
}
FIX_PLAN_JSON = {"approach": "patch x", "fix_site": "apps/api/src/app/x.ts", "alternatives": ["none"]}
PROBE_JSON = {"probes": [], "none_reason": "no db probe needed"}
RESIDUALS_JSON = {"residuals": [{"symptom": "none", "disposition": "by-design", "citation": "n/a"}]}
VERIFY_JSON = {"test_patterns": ["x.spec"]}
ALLOWLIST_JSON = ["apps/api/src/app/x.spec.ts", "apps/api/src/app/x.ts"]
RCA_MD = "# RCA\n"
CAUSAL_CHAIN_JSON = {"chain": []}
HYPOTHESES_JSON = {"hypotheses": []}


def make_rca_converge_fixture(tmp, critique_verdict_baseline):
    """Mirrors make_plan_converge_fixture for the bugfix lane: rca-converge
    additionally requires six immutable "imm-*" anchor files (byte-identical
    to their mutable counterparts) before it will even look at critique.json,
    plus the full rca-shape.sh contract for the ACCEPT path."""
    ad = tmp / "artifacts"
    ad.mkdir()

    files = {
        "repo.json": REPO_JSON, "failing-test.json": FAILING_TEST_JSON,
        "fix-plan.json": FIX_PLAN_JSON, "probe.json": PROBE_JSON,
        "residuals.json": RESIDUALS_JSON, "verify.json": VERIFY_JSON,
        "files-allowlist.json": ALLOWLIST_JSON,
    }
    for name, content in files.items():
        (ad / name).write_text(json.dumps(content), encoding="utf-8")
    (ad / "rca.md").write_text(RCA_MD, encoding="utf-8")
    (ad / "causal-chain.json").write_text(json.dumps(CAUSAL_CHAIN_JSON), encoding="utf-8")
    (ad / "hypotheses.json").write_text(json.dumps(HYPOTHESES_JSON), encoding="utf-8")

    # rca-converge's immutable-anchor check (durable, predates the loop).
    for name in ("rca.md", "causal-chain.json", "hypotheses.json", "residuals.json", "probe.json", "repo.json"):
        shutil.copyfile(ad / name, ad / f"imm-{name}")
    shutil.copyfile(ad / "rca.md", ad / "rca.pre.md")
    shutil.copyfile(ad / "causal-chain.json", ad / "causal-chain.pre.json")

    (ad / "rca-round.txt").write_text("1\n", encoding="utf-8")
    rd = ad / "rca-round-1"
    rd.mkdir()
    (rd / "pre-fix-plan.json").write_text(json.dumps(FIX_PLAN_JSON), encoding="utf-8")
    for name in ("files-allowlist.json", "verify.json", "failing-test.json"):
        shutil.copyfile(ad / name, rd / f"pre-{name}")
    if critique_verdict_baseline == "REVISE":
        (rd / "critique.json").write_text(json.dumps({"verdict": "REVISE", "findings": []}), encoding="utf-8")
    (rd / "revision.json").write_text(json.dumps({"applied": [], "declined": []}), encoding="utf-8")
    return ad, rd


def make_critic_gate_fixture(tmp, round_file):
    ad = tmp / "artifacts"
    ad.mkdir()
    (ad / round_file).write_text("1\n", encoding="utf-8")
    rd_name = "plan-round-1" if round_file == "plan-round.txt" else "rca-round-1"
    rd = ad / rd_name
    rd.mkdir()
    return ad, rd


# ---------------------------------------------------------------------------
# Typed-line vocabulary and PASS/FAIL classification, per RUNBOOK.md section 3/3a/3b.
# ---------------------------------------------------------------------------

def deslop_review_gate_classify(line):
    if line.startswith("DESLOP=CLEAN"):
        return True
    if line.startswith("DESLOP=DIRTY") or line.startswith("DESLOP_REVIEW=FAIL"):
        return False
    return None


DESLOP_REVIEW_GATE_TERMINAL = re.compile(r"^(DESLOP=CLEAN\b.*|DESLOP=DIRTY\b.*|DESLOP_REVIEW=FAIL\b.*)$", re.MULTILINE)


def critic_gate_classify(line):
    if line.startswith("CRITIQUE "):
        return True
    if line.startswith("CRITIC_GATE=FAIL"):
        return False
    return None


CRITIC_GATE_TERMINAL = re.compile(r"^(CRITIQUE round=.*|CRITIC_GATE=FAIL.*)$", re.MULTILINE)


def plan_converge_classify(line):
    if line.startswith("PLAN_CONVERGED") or line.startswith("PLAN_ROUND_PROGRESSED"):
        return True
    if line.startswith(("PLAN_REJECTED", "PLAN_SCOPE_DISPUTE", "PLAN_NO_PROGRESS", "PLAN_ROUND_CAP",
                         "PLAN_CONVERGE=FAIL", "CRITIC_GATE=FAIL")):
        return False
    return None


PLAN_CONVERGE_TERMINAL = re.compile(
    r"^(PLAN_CONVERGED\b.*|PLAN_ROUND_PROGRESSED\b.*|PLAN_REJECTED\b.*|PLAN_SCOPE_DISPUTE\b.*|"
    r"PLAN_NO_PROGRESS\b.*|PLAN_ROUND_CAP\b.*|PLAN_CONVERGE=FAIL\b.*|CRITIC_GATE=FAIL\b.*)$",
    re.MULTILINE,
)


def rca_converge_classify(line):
    if line.startswith("RCA_PLAN_CONVERGED") or line.startswith("RCA_PLAN_ROUND_PROGRESSED"):
        return True
    if line.startswith(("RCA_PLAN_REJECTED", "RCA_PLAN_SCOPE_DISPUTE", "RCA_PLAN_NO_PROGRESS",
                         "RCA_PLAN_ROUND_CAP", "RCA_CONVERGE=FAIL", "RCA_PLAN=FAIL", "CRITIC_GATE=FAIL")):
        return False
    return None


RCA_CONVERGE_TERMINAL = re.compile(
    r"^(RCA_PLAN_CONVERGED\b.*|RCA_PLAN_ROUND_PROGRESSED\b.*|RCA_PLAN_REJECTED\b.*|RCA_PLAN_SCOPE_DISPUTE\b.*|"
    r"RCA_PLAN_NO_PROGRESS\b.*|RCA_PLAN_ROUND_CAP\b.*|RCA_CONVERGE=FAIL\b.*|RCA_PLAN=FAIL\b.*|CRITIC_GATE=FAIL\b.*)$",
    re.MULTILINE,
)


class FuzzGateAssertions:
    """Shared assertion body: given a completed subprocess, a compiled
    terminal-line regex, and a classify(line) -> True/False/None function,
    verify the three properties every gate must hold under any malformed
    input: exactly one typed terminal line, no traceback, and an exit code
    consistent with that line's PASS/FAIL class."""

    def assert_gate_fails_closed(self, proc, terminal_re, classify, variant_name):
        self.assertNotIn(TRACEBACK, proc.stderr,
                          f"{variant_name}: python traceback leaked to stderr:\n{proc.stderr}")
        # parse-critique.py's fail() does sys.exit(f"CRITIC_GATE=FAIL ...") — a
        # STRING argument to sys.exit prints to stderr, not stdout (confirmed
        # by test_parse_critique.py, which checks both streams for the same
        # reason). The success line (print(...)) goes to stdout as usual, so
        # the terminal line for this gate can land in either stream.
        matches = terminal_re.findall(proc.stdout + "\n" + proc.stderr)
        self.assertEqual(
            len(matches), 1,
            f"{variant_name}: expected exactly one gate-terminal typed line, got {len(matches)}: "
            f"{matches!r}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}",
        )
        line = matches[0]
        is_pass = classify(line)
        self.assertIsNotNone(is_pass, f"{variant_name}: terminal line not classifiable as PASS/FAIL: {line!r}")
        if is_pass:
            self.assertEqual(proc.returncode, 0, f"{variant_name}: PASS-class line {line!r} but rc={proc.returncode}")
        else:
            self.assertNotEqual(proc.returncode, 0, f"{variant_name}: FAIL-class line {line!r} but rc=0")
        return line, is_pass


class DeslopReviewGateFuzzTest(FuzzGateAssertions, unittest.TestCase):
    SEEDS = ["deslop-review.json", "deslop-review-dirty.json"]

    def _run_lane(self, workflow):
        results = {"pass": 0, "typed_fail": 0, "untyped": 0}
        vs = combined_variants(self.SEEDS, N, seed=0)
        for v in vs:
            with tempfile.TemporaryDirectory() as tmp:
                ad = make_deslop_review_gate_fixture(Path(tmp))
                (ad / "deslop-review.json").write_bytes(v["content"])
                proc = run_body(workflow, "deslop-review-gate", ad)
                with self.subTest(variant=v["name"]):
                    try:
                        _, is_pass = self.assert_gate_fails_closed(
                            proc, DESLOP_REVIEW_GATE_TERMINAL, deslop_review_gate_classify, v["name"])
                        results["pass" if is_pass else "typed_fail"] += 1
                    except AssertionError:
                        results["untyped"] += 1
                        raise
        print(f"FUZZ {workflow}:deslop-review-gate:deslop-review.json n={len(vs)} "
              f"pass={results['pass']} typed_fail={results['typed_fail']} untyped={results['untyped']}")

    def test_full_sdlc_api(self):
        self._run_lane("full-sdlc-api")

    def test_bugfix(self):
        self._run_lane("bugfix")


class PlanCriticGateFuzzTest(FuzzGateAssertions, unittest.TestCase):
    SEEDS = ["critique.json", "critique-blocking.json"]

    def _run(self, workflow, node_id, round_file):
        results = {"pass": 0, "typed_fail": 0, "untyped": 0}
        vs = combined_variants(self.SEEDS, N, seed=0)
        for v in vs:
            with tempfile.TemporaryDirectory() as tmp:
                ad, rd = make_critic_gate_fixture(Path(tmp), round_file)
                (rd / "critique.json").write_bytes(v["content"])
                proc = run_body(workflow, node_id, ad)
                with self.subTest(variant=v["name"]):
                    try:
                        _, is_pass = self.assert_gate_fails_closed(
                            proc, CRITIC_GATE_TERMINAL, critic_gate_classify, v["name"])
                        results["pass" if is_pass else "typed_fail"] += 1
                    except AssertionError:
                        results["untyped"] += 1
                        raise
        print(f"FUZZ {workflow}:{node_id}:critique.json n={len(vs)} "
              f"pass={results['pass']} typed_fail={results['typed_fail']} untyped={results['untyped']}")

    def test_full_sdlc_api_plan_critic_gate(self):
        self._run("full-sdlc-api", "plan-critic-gate", "plan-round.txt")

    def test_bugfix_rca_critic_gate(self):
        self._run("bugfix", "rca-critic-gate", "rca-round.txt")


class PlanConvergeCritiqueFuzzTest(FuzzGateAssertions, unittest.TestCase):
    """Fuzzes critique.json against plan-converge with a valid, ACCEPT-shaped
    baseline everywhere else, so a mutation that leaves verdict=ACCEPT and
    findings schema-valid reaches (and should cleanly pass) plan-shape.sh."""
    SEEDS = ["critique.json", "critique-blocking.json"]

    def test_full_sdlc_api(self):
        results = {"pass": 0, "typed_fail": 0, "untyped": 0}
        vs = combined_variants(self.SEEDS, N, seed=0)
        for v in vs:
            with tempfile.TemporaryDirectory() as tmp:
                ad, rd = make_plan_converge_fixture(Path(tmp), critique_verdict_baseline="ACCEPT")
                (rd / "critique.json").write_bytes(v["content"])
                proc = run_body("full-sdlc-api", "plan-converge", ad)
                with self.subTest(variant=v["name"]):
                    try:
                        _, is_pass = self.assert_gate_fails_closed(
                            proc, PLAN_CONVERGE_TERMINAL, plan_converge_classify, v["name"])
                        results["pass" if is_pass else "typed_fail"] += 1
                    except AssertionError:
                        results["untyped"] += 1
                        raise
        print(f"FUZZ full-sdlc-api:plan-converge:critique.json n={len(vs)} "
              f"pass={results['pass']} typed_fail={results['typed_fail']} untyped={results['untyped']}")


class RcaConvergeCritiqueFuzzTest(FuzzGateAssertions, unittest.TestCase):
    SEEDS = ["critique.json", "critique-blocking.json"]

    def test_bugfix(self):
        results = {"pass": 0, "typed_fail": 0, "untyped": 0}
        vs = combined_variants(self.SEEDS, N, seed=0)
        for v in vs:
            with tempfile.TemporaryDirectory() as tmp:
                ad, rd = make_rca_converge_fixture(Path(tmp), critique_verdict_baseline="ACCEPT")
                (rd / "critique.json").write_bytes(v["content"])
                proc = run_body("bugfix", "rca-converge", ad)
                with self.subTest(variant=v["name"]):
                    try:
                        _, is_pass = self.assert_gate_fails_closed(
                            proc, RCA_CONVERGE_TERMINAL, rca_converge_classify, v["name"])
                        results["pass" if is_pass else "typed_fail"] += 1
                    except AssertionError:
                        results["untyped"] += 1
                        raise
        print(f"FUZZ bugfix:rca-converge:critique.json n={len(vs)} "
              f"pass={results['pass']} typed_fail={results['typed_fail']} untyped={results['untyped']}")


class PlanConvergeRevisionFuzzTest(FuzzGateAssertions, unittest.TestCase):
    """Fuzzes revision.json against plan-converge with critique.json pinned
    to verdict=REVISE + zero findings, so execution reaches the DISPUTE
    parse of revision.json (which the ACCEPT path exits before reaching)."""
    SEEDS = ["revision.json", "revision-with-dispute.json"]

    def test_full_sdlc_api(self):
        results = {"pass": 0, "typed_fail": 0, "untyped": 0}
        vs = combined_variants(self.SEEDS, N, seed=0)
        for v in vs:
            with tempfile.TemporaryDirectory() as tmp:
                ad, rd = make_plan_converge_fixture(Path(tmp), critique_verdict_baseline="REVISE")
                (rd / "revision.json").write_bytes(v["content"])
                proc = run_body("full-sdlc-api", "plan-converge", ad)
                with self.subTest(variant=v["name"]):
                    try:
                        _, is_pass = self.assert_gate_fails_closed(
                            proc, PLAN_CONVERGE_TERMINAL, plan_converge_classify, v["name"])
                        results["pass" if is_pass else "typed_fail"] += 1
                    except AssertionError:
                        results["untyped"] += 1
                        raise
        print(f"FUZZ full-sdlc-api:plan-converge:revision.json n={len(vs)} "
              f"pass={results['pass']} typed_fail={results['typed_fail']} untyped={results['untyped']}")


class RcaConvergeRevisionFuzzTest(FuzzGateAssertions, unittest.TestCase):
    SEEDS = ["revision.json", "revision-with-dispute.json"]

    def test_bugfix(self):
        results = {"pass": 0, "typed_fail": 0, "untyped": 0}
        vs = combined_variants(self.SEEDS, N, seed=0)
        for v in vs:
            with tempfile.TemporaryDirectory() as tmp:
                ad, rd = make_rca_converge_fixture(Path(tmp), critique_verdict_baseline="REVISE")
                (rd / "revision.json").write_bytes(v["content"])
                proc = run_body("bugfix", "rca-converge", ad)
                with self.subTest(variant=v["name"]):
                    try:
                        _, is_pass = self.assert_gate_fails_closed(
                            proc, RCA_CONVERGE_TERMINAL, rca_converge_classify, v["name"])
                        results["pass" if is_pass else "typed_fail"] += 1
                    except AssertionError:
                        results["untyped"] += 1
                        raise
        print(f"FUZZ bugfix:rca-converge:revision.json n={len(vs)} "
              f"pass={results['pass']} typed_fail={results['typed_fail']} untyped={results['untyped']}")


# ---------------------------------------------------------------------------
# Regression pins: specific variants that were red (untyped traceback) before
# the fix and must stay green. Each reproduces a defect found by the fuzz
# corpus above and pins it as a named, non-random test so a regression is
# caught immediately rather than only probabilistically by the seeded fuzz.
# ---------------------------------------------------------------------------

class PlanConvergeBlockingCalcRegressionTest(unittest.TestCase):
    """plan-converge's / rca-converge's inline BLOCKING-count one-liner
    (`int(str(x.get("confidence")))`) had NO exception handling and NO
    stderr redirection, unlike the verdict-extraction one-liner directly
    above it in the same script (which already had `2>/dev/null`). Any of:
    a non-coercible confidence on a P0/P1 finding, invalid JSON, or a
    non-list `findings` field, reached plan-converge/rca-converge directly
    (as any gate must tolerate — a hand-edited artifacts dir + resume is a
    documented recovery path, and nothing upstream can be assumed to have
    already validated the file) and leaked a raw Python traceback to
    stderr, even though a typed FAIL line still followed. Fixed by adding
    `2>/dev/null` to both one-liners, matching the sibling verdict line."""

    def _run(self, workflow, node_id, critique_bytes, fixture_fn):
        with tempfile.TemporaryDirectory() as tmp:
            ad, rd = fixture_fn(Path(tmp), critique_verdict_baseline="ACCEPT")
            (rd / "critique.json").write_bytes(critique_bytes)
            return run_body(workflow, node_id, ad)

    def test_null_confidence_on_p0_finding_full_sdlc_api(self):
        bad = json.dumps({
            "verdict": "ACCEPT",
            "findings": [{"kind": "scope", "severity": "P0", "confidence": None,
                          "section": "## Files", "evidence": [{"source": "spec", "quote": "x"}],
                          "recommendation": "y"}],
        }).encode("utf-8")
        proc = self._run("full-sdlc-api", "plan-converge", bad, make_plan_converge_fixture)
        self.assertNotIn(TRACEBACK, proc.stderr, proc.stderr)
        self.assertIn("PLAN_CONVERGE=FAIL critique.json findings unreadable round=1", proc.stdout)
        self.assertEqual(proc.returncode, 1)

    def test_null_confidence_on_p0_finding_bugfix(self):
        bad = json.dumps({
            "verdict": "ACCEPT",
            "findings": [{"kind": "scope", "severity": "P0", "confidence": None,
                          "section": "## Files", "evidence": [{"source": "spec", "quote": "x"}],
                          "recommendation": "y"}],
        }).encode("utf-8")
        proc = self._run("bugfix", "rca-converge", bad, make_rca_converge_fixture)
        self.assertNotIn(TRACEBACK, proc.stderr, proc.stderr)
        self.assertIn("RCA_CONVERGE=FAIL critique.json findings unreadable round=1", proc.stdout)
        self.assertEqual(proc.returncode, 1)

    def test_invalid_json_full_sdlc_api(self):
        proc = self._run("full-sdlc-api", "plan-converge", b"not json at all", make_plan_converge_fixture)
        self.assertNotIn(TRACEBACK, proc.stderr, proc.stderr)
        self.assertIn("PLAN_CONVERGE=FAIL unknown verdict", proc.stdout)
        self.assertEqual(proc.returncode, 1)

    def test_findings_not_a_list_full_sdlc_api(self):
        bad = json.dumps({"verdict": "ACCEPT", "findings": 5}).encode("utf-8")
        proc = self._run("full-sdlc-api", "plan-converge", bad, make_plan_converge_fixture)
        self.assertNotIn(TRACEBACK, proc.stderr, proc.stderr)
        self.assertIn("PLAN_CONVERGE=FAIL critique.json findings unreadable round=1", proc.stdout)
        self.assertEqual(proc.returncode, 1)

    def test_findings_not_a_list_bugfix(self):
        bad = json.dumps({"verdict": "ACCEPT", "findings": 5}).encode("utf-8")
        proc = self._run("bugfix", "rca-converge", bad, make_rca_converge_fixture)
        self.assertNotIn(TRACEBACK, proc.stderr, proc.stderr)
        self.assertIn("RCA_CONVERGE=FAIL critique.json findings unreadable round=1", proc.stdout)
        self.assertEqual(proc.returncode, 1)


if __name__ == "__main__":
    unittest.main()
