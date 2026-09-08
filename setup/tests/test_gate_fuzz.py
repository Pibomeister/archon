"""Corpus-driven fuzz test: feeds deterministic, seeded mutations of the three
AI-authored JSON envelopes (deslop-review.json, critique.json, revision.json)
through the REAL extracted gate bodies (deslop-review-gate, plan-critic-gate /
rca-critic-gate, plan-converge / rca-converge, both lanes) and asserts each
variant produces exactly one documented gate-terminal typed line matching
its FULL shape (not just a matching prefix — round=<N>, cap=<N>, closed
enums, etc.), no Python traceback on EITHER stream, and an exit code
consistent with that line's PASS/FAIL class. The pattern tables below are
transcribed directly from the `echo`/`print` statements in workflows/*.yaml
and setup/parse-critique.py (see RUNBOOK.md section 3/3a/3b for the prose
summary of the same vocabulary). OracleSelfCheckTest below proves the
oracle itself rejects two things a weaker check would rubber-stamp: a
traceback printed to stdout instead of stderr, and a well-prefixed but
structurally garbage terminal line.

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

N = max(20, int(os.environ.get("NODE_STRESS") or 0))  # NODE_STRESS=100 → the full corpus
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
    (ad / "web-files-allowlist.json").write_text(json.dumps(["app/routes/feature.tsx"]), encoding="utf-8")
    (ad / "reader-audit.json").write_text(json.dumps({"columns": []}), encoding="utf-8")
    (ad / "plan-round.txt").write_text("1\n", encoding="utf-8")
    rd = ad / "plan-round-1"
    rd.mkdir()
    (rd / "plan.pre.md").write_text(PLAN_MD, encoding="utf-8")
    for f in ("verify.json", "files-allowlist.json", "web-files-allowlist.json", "reader-audit.json"):
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
# Typed-line vocabulary: one FULL-SHAPE regex per distinct `echo`/`print`
# statement in the gate's own source (transcribed from workflows/*.yaml and
# setup/parse-critique.py directly, not from RUNBOOK.md's prose summary).
# Each pattern is matched with `fullmatch` against a single line — a prefix
# match is not enough, so every numeric field is `\d+`, every enum field is
# a closed alternation, and only genuinely free-text portions (a Python
# `!r`-repr of an untrusted JSON value, an exception message) are left as
# `.+`. A line that merely starts with the right token but has a garbage
# value in a structured field (e.g. `round=garbage`) must NOT match.
# ---------------------------------------------------------------------------

def _alt(*parts):
    return "(?:" + "|".join(parts) + ")"


INT = r"\d+"
FIVE_FILES = _alt(r"verify\.json", r"files-allowlist\.json", r"reader-audit\.json",
                   r"premises\.json", r"smoke-probe\.json")
FIVE_LIST = _alt("none", FIVE_FILES + r"(?:," + FIVE_FILES + r")*")
THREE_FILES_RCA = _alt(r"files-allowlist\.json", r"verify\.json", r"failing-test\.json")
THREE_LIST_RCA = _alt("none", THREE_FILES_RCA + r"(?:," + THREE_FILES_RCA + r")*")
FOUR_FILES_RCA = _alt(r"fix-plan\.json", r"files-allowlist\.json", r"verify\.json", r"failing-test\.json")
FOUR_LIST_RCA = _alt("NO", FOUR_FILES_RCA + r"(?:," + FOUR_FILES_RCA + r")*")
SIX_FILES_RCA = _alt(r"rca\.md", r"causal-chain\.json", r"hypotheses\.json",
                      r"residuals\.json", r"probe\.json", r"repo\.json")
GUARD5 = _alt("complexity", "tautological_tests", "yagni", "open_closed", "comments")

# deslop-review-gate's embedded python validator: malformed()/incomplete()
# reason strings, one alternative per distinct call site in the YAML.
_MALFORMED_REASON = _alt(
    r"unparseable json: .+",
    r"top level is not an object",
    r"extra top-level keys=\[.*\]",
    r"verdict=.+",
    r"findings is not an array",
    rf"index={INT} finding is not an object",
    rf"index={INT} guard=.+",
    rf"index={INT} (?:file|evidence)=.+",
    rf"index={INT} line=.+",
    rf"index={INT} confidence=.+",
)
_INCOMPLETE_REASON = _alt(
    r"coverage is not an object",
    rf"missing={GUARD5}",
    rf"guard={GUARD5} status=.+",
    rf"guard={GUARD5} empty evidence",
)

DESLOP_REVIEW_GATE_PATTERNS = [
    (rf"DESLOP=CLEAN round={INT}", True),
    (rf"DESLOP=DIRTY round={INT} blocking={INT}", False),
    (r"DESLOP_REVIEW=FAIL no deslop-round\.txt \(deslop-recheck did not run\)", False),
    (rf"DESLOP_REVIEW=FAIL no checkpoint for round={INT}", False),
    (rf"DESLOP_REVIEW=FAIL no deslop-review\.json round={INT}", False),
    (rf"DESLOP_REVIEW=FAIL reviewer modified tree round={INT}", False),
    (rf"DESLOP_REVIEW=FAIL recompute (?:add|write-tree|rev-parse|live-index tree) round={INT}", False),
    (rf"DESLOP_REVIEW=FAIL malformed finding round={INT} {_MALFORMED_REASON}", False),
    (rf"DESLOP_REVIEW=FAIL coverage incomplete round={INT} {_INCOMPLETE_REASON}", False),
    (rf"DESLOP_REVIEW=FAIL verdict inconsistent round={INT} declared DIRTY with 0 blocking findings", False),
    (rf"DESLOP_REVIEW=FAIL validator error rc={INT} round={INT}", False),
]

# parse-critique.py's fail() reason strings, one per distinct call site.
_CRITIC_REASON = _alt(
    r"usage: parse-critique\.py <critique\.json> --round N",
    r"cannot read/parse .+",
    r"top level is not an object",
    r"verdict out of enum: .+",
    r"findings is not a list",
    rf"finding {INT} is not an object",
    rf"finding {INT} missing kind",
    rf"finding {INT} severity out of enum: .+",
    rf"finding {INT} confidence out of enum: .+",
    rf"finding {INT} missing section",
    rf"finding {INT} missing evidence",
    rf"finding {INT} evidence {INT} malformed",
    rf"finding {INT} missing recommendation",
)
CRITIC_GATE_PATTERNS = [
    (rf"CRITIQUE round={INT} verdict={_alt('ACCEPT', 'REVISE', 'REJECT')} "
     rf"scope={INT} regression={INT} gap={INT} verifiability={INT}", True),
    (r"CRITIC_GATE=FAIL no plan-round\.txt", False),
    (r"CRITIC_GATE=FAIL no rca-round\.txt", False),
    (rf"CRITIC_GATE=FAIL no critique\.json round={INT}", False),
    (rf"CRITIC_GATE=FAIL {_CRITIC_REASON}", False),
]

PLAN_CONVERGE_PATTERNS = [
    (rf"PLAN_CONVERGED round={INT}", True),
    (rf"PLAN_ROUND_PROGRESSED round={INT}", True),
    (r"PLAN_CONVERGE=FAIL no plan-round\.txt", False),
    (r"PLAN_CONVERGE=FAIL plan-round\.txt is not an integer: \[.*\]", False),
    (rf"PLAN_CONVERGE=FAIL unknown verdict \[.*\] round={INT}", False),
    (rf"PLAN_CONVERGE=FAIL no revision\.json round={INT}", False),
    (rf"PLAN_CONVERGE=FAIL critique\.json findings unreadable round={INT}", False),
    (rf"PLAN_CONVERGE=FAIL shape round={INT}", False),
    (rf"PLAN_CONVERGE=FAIL revision\.json malformed round={INT}", False),
    (rf"CRITIC_GATE=FAIL verdict inconsistent round={INT} declared ACCEPT with {INT} blocking findings", False),
    (rf"PLAN_REJECTED round={INT} plan_mutated={_alt('YES', 'NO')} artifacts_mutated={FIVE_LIST}", False),
    (rf"PLAN_SCOPE_DISPUTE round={INT} declined_scope_100={INT}", False),
    (rf"PLAN_NO_PROGRESS round={INT} other_mutated={FIVE_LIST}", False),
    (rf"PLAN_ROUND_CAP round={INT} cap={INT}", False),
]

RCA_CONVERGE_PATTERNS = [
    (rf"RCA_PLAN_CONVERGED round={INT}", True),
    (rf"RCA_PLAN_ROUND_PROGRESSED round={INT}", True),
    (r"RCA_CONVERGE=FAIL no rca-round\.txt", False),
    (r"RCA_CONVERGE=FAIL rca-round\.txt is not an integer: \[.*\]", False),
    (rf"RCA_CONVERGE=FAIL unknown verdict \[.*\] round={INT}", False),
    (rf"RCA_CONVERGE=FAIL no revision\.json round={INT}", False),
    (rf"RCA_CONVERGE=FAIL missing durable anchor imm-{SIX_FILES_RCA} round={INT}", False),
    (rf"RCA_PLAN=FAIL immutable artifact modified {SIX_FILES_RCA} round={INT}", False),
    (rf"RCA_CONVERGE=FAIL critique\.json findings unreadable round={INT}", False),
    (rf"RCA_CONVERGE=FAIL revision\.json malformed round={INT}", False),
    (rf"CRITIC_GATE=FAIL verdict inconsistent round={INT} declared ACCEPT with {INT} blocking findings", False),
    (rf"RCA_PLAN_REJECTED round={INT} mutated={FOUR_LIST_RCA}", False),
    (rf"RCA_PLAN_SCOPE_DISPUTE round={INT} declined_scope_100={INT}", False),
    (rf"RCA_PLAN_NO_PROGRESS round={INT} other_mutated={THREE_LIST_RCA}", False),
    (rf"RCA_CONVERGE=FAIL shape round={INT}", False),
    (rf"RCA_PLAN_ROUND_CAP round={INT} cap={INT}", False),
]


def compile_patterns(patterns):
    return [(re.compile(p), is_pass) for p, is_pass in patterns]


DESLOP_REVIEW_GATE_TERMINAL = compile_patterns(DESLOP_REVIEW_GATE_PATTERNS)
CRITIC_GATE_TERMINAL = compile_patterns(CRITIC_GATE_PATTERNS)
PLAN_CONVERGE_TERMINAL = compile_patterns(PLAN_CONVERGE_PATTERNS)
RCA_CONVERGE_TERMINAL = compile_patterns(RCA_CONVERGE_PATTERNS)


def find_terminal_lines(text, compiled_patterns):
    """Every line in text that fullmatches one of compiled_patterns, paired
    with that pattern's PASS/FAIL class. A line matching more than one
    pattern is impossible by construction (the alternatives are disjoint
    prefixes), so first-match is fine."""
    out = []
    for line in text.splitlines():
        for regex, is_pass in compiled_patterns:
            if regex.fullmatch(line):
                out.append((line, is_pass))
                break
    return out


class FuzzGateAssertions:
    """Shared assertion body: given a completed subprocess and this gate's
    compiled (regex, is_pass) pattern table, verify the three properties
    every gate must hold under any malformed input: exactly one FULL-SHAPE
    typed terminal line (not just a matching prefix), no traceback on
    EITHER stream, and an exit code consistent with that line's class."""

    def assert_gate_fails_closed(self, proc, compiled_patterns, variant_name):
        # Check both streams: a traceback could in principle land on either
        # (e.g. if a future change adds an unguarded print-then-raise), and
        # an oracle that only checks stderr would rubber-stamp one that
        # printed to stdout with rc=0 — see OracleSelfCheckTest below.
        self.assertNotIn(TRACEBACK, proc.stdout,
                          f"{variant_name}: python traceback leaked to stdout:\n{proc.stdout}")
        self.assertNotIn(TRACEBACK, proc.stderr,
                          f"{variant_name}: python traceback leaked to stderr:\n{proc.stderr}")
        # parse-critique.py's fail() does sys.exit(f"CRITIC_GATE=FAIL ...") — a
        # STRING argument to sys.exit prints to stderr, not stdout (confirmed
        # by test_parse_critique.py, which checks both streams for the same
        # reason). The success line (print(...)) goes to stdout as usual, so
        # the terminal line for this gate can land in either stream.
        matches = find_terminal_lines(proc.stdout + "\n" + proc.stderr, compiled_patterns)
        self.assertEqual(
            len(matches), 1,
            f"{variant_name}: expected exactly one gate-terminal typed line matching its full "
            f"documented shape, got {len(matches)}: {matches!r}\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}",
        )
        line, is_pass = matches[0]
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
                            proc, DESLOP_REVIEW_GATE_TERMINAL, v["name"])
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
                            proc, CRITIC_GATE_TERMINAL, v["name"])
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
                            proc, PLAN_CONVERGE_TERMINAL, v["name"])
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
                            proc, RCA_CONVERGE_TERMINAL, v["name"])
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
                            proc, PLAN_CONVERGE_TERMINAL, v["name"])
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
                            proc, RCA_CONVERGE_TERMINAL, v["name"])
                        results["pass" if is_pass else "typed_fail"] += 1
                    except AssertionError:
                        results["untyped"] += 1
                        raise
        print(f"FUZZ bugfix:rca-converge:revision.json n={len(vs)} "
              f"pass={results['pass']} typed_fail={results['typed_fail']} untyped={results['untyped']}")


class FakeProc:
    """Stand-in for a completed subprocess.CompletedProcess, so the oracle
    itself (assert_gate_fails_closed) can be tested against hand-built
    stdout/stderr/returncode without spawning bash."""

    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class OracleSelfCheckTest(FuzzGateAssertions, unittest.TestCase):
    """The fuzz oracle (assert_gate_fails_closed) must reject bad evidence,
    not just bad gate output — a Codex review of this file found two ways
    the PREVIOUS version of the oracle would have rubber-stamped a broken
    gate:

    (1) A traceback printed to STDOUT (not stderr) with rc=0. The old
        oracle only checked `TRACEBACK not in proc.stderr`, so a node that
        leaked a traceback to stdout alongside a coincidentally-valid
        terminal line and exited 0 would have passed clean. Now checked on
        both streams.
    (2) A well-prefixed but structurally garbage terminal line, e.g.
        `CRITIQUE round=garbage verdict=ACCEPT scope=0 regression=0 gap=0
        verifiability=0` with rc=0. The old oracle classified by
        `line.startswith("CRITIQUE ")`, which accepts this even though
        `round=` isn't a number — a node that started emitting malformed
        round numbers (or any other structured field) would have gone
        undetected. Now matched with `fullmatch` against a full-shape
        regex, so a garbage field produces zero matches, not a false PASS.

    Both counterexamples must make the oracle raise AssertionError. This
    test is meaningless run against the vocabulary tables alone — it tests
    assert_gate_fails_closed's own logic, using FakeProc so no gate body
    needs to run."""

    def test_rejects_traceback_on_stdout_even_with_valid_terminal_line_and_rc_zero(self):
        proc = FakeProc(
            stdout=(
                'Traceback (most recent call last):\n'
                '  File "<string>", line 1, in <module>\n'
                'ValueError: boom\n'
                'DESLOP=CLEAN round=1\n'
            ),
            stderr="",
            returncode=0,
        )
        with self.assertRaises(AssertionError):
            self.assert_gate_fails_closed(proc, DESLOP_REVIEW_GATE_TERMINAL, "self-check-traceback-on-stdout")

    def test_rejects_prefix_only_garbage_terminal_line(self):
        proc = FakeProc(
            stdout="CRITIQUE round=garbage verdict=ACCEPT scope=0 regression=0 gap=0 verifiability=0\n",
            stderr="",
            returncode=0,
        )
        with self.assertRaises(AssertionError):
            self.assert_gate_fails_closed(proc, CRITIC_GATE_TERMINAL, "self-check-garbage-terminal-line")

    def test_still_accepts_a_genuinely_valid_pass_line(self):
        # Negative control for the two tests above: the oracle must not
        # have become so strict that it rejects real, well-formed output.
        proc = FakeProc(stdout="DESLOP=CLEAN round=1\n", stderr="", returncode=0)
        line, is_pass = self.assert_gate_fails_closed(proc, DESLOP_REVIEW_GATE_TERMINAL, "self-check-valid-pass")
        self.assertTrue(is_pass)
        self.assertEqual(line, "DESLOP=CLEAN round=1")


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
