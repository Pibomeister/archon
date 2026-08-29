"""bugfix.yaml deslop loop (deslop-recheck / deslop-review-gate / deslop-commit),
ported from scratchpad/drill.sh sections A-J.

Every node body is extracted live via nodes.extract.runnable_body, never
copied from the YAML, so these tests track the workflow as it evolves.

deslop-recheck shells out to the real toolchain (`bun run typecheck`,
`bun run lint`, `bun run test -- <pattern>`) inside the fixture worktree.
Rather than depend on a real `bun` install on whatever machine runs this
suite, a minimal deterministic `bun` shim is put on PATH ahead of the real
one: `bun run <script> [-- <args>]` reads `package.json`'s `scripts.<script>`
in the current directory and execs it via `bash -c`. That is enough to drive
every scenario below, including F4's "typecheck fails" case, which works by
overwriting the script's command in package.json (exactly as the toolchain
would be exercised for real, just without requiring bun on the runner).
"""
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from nodes.extract import runnable_body
from nodes.gitutil import git

BUN_SHIM = """#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" != "run" ]; then echo "bun-shim: unsupported invocation: $*" >&2; exit 1; fi
shift
script="$1"; shift
if [ "${1:-}" = "--" ]; then shift; fi
cmd=$(python3 -c "import json,sys; d=json.load(open('package.json', encoding='utf-8')); print(d['scripts'].get(sys.argv[1], ''))" "$script")
exec bash -c "$cmd"
"""

CLEAN_REVIEW = {
    "verdict": "CLEAN",
    "coverage": {
        "complexity": {"status": "assessed", "evidence": "one 1-branch fn"},
        "tautological_tests": {"status": "not_applicable", "evidence": "only the frozen repro"},
        "yagni": {"status": "assessed", "evidence": "widen is used by the repro"},
        "open_closed": {"status": "not_applicable", "evidence": "no dispatch touched"},
        "comments": {"status": "assessed", "evidence": "no comments added"},
    },
    "findings": [],
}


def write_json(path, obj):
    path.write_text(json.dumps(obj), encoding="utf-8")


class DeslopDrillBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bindir = self.tmp / "_bin"
        self.bindir.mkdir()
        bun = self.bindir / "bun"
        bun.write_text(BUN_SHIM, encoding="utf-8")
        bun.chmod(bun.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    # -- fixture -----------------------------------------------------------

    def mkfix(self, name):
        """Port of drill.sh's mkfix(): a worktree with a committed red repro,
        a committed fix, a deliberately-dirty lockfile (bootstrap drift), and
        a matching artifacts dir with a CLEAN deslop-review.json."""
        base = self.tmp / name
        R = base / "wt"
        AD = base / "art"
        (R / "src").mkdir(parents=True)
        AD.mkdir(parents=True)
        git(R, "init", "-q")
        git(R, "config", "user.email", "a@b")
        git(R, "config", "user.name", "a")
        (R / ".gitignore").write_text("node_modules\n.env\n", encoding="utf-8")
        write_json(R / "package.json", {
            "name": "toy",
            "scripts": {"typecheck": "true", "lint": "true", "test": "true"},
        })
        (R / "pnpm-lock.yaml").write_text("lock-v1 committed\n", encoding="utf-8")
        git(R, "add", "-A")
        git(R, "commit", "-q", "-m", "base")
        # bootstrap leaves the lockfile deliberately modified, uncommitted
        (R / "pnpm-lock.yaml").write_text("lock-v1 bootstrap drift\n", encoding="utf-8")
        (R / "src" / "repro.spec.ts").write_text(
            'import { widen } from "./a";\n'
            'it("reproduces the bug", () => {\n'
            "  expect(widen(2)).toBe(4);\n"
            "});\n",
            encoding="utf-8",
        )
        git(R, "add", "src/repro.spec.ts")
        git(R, "commit", "-q", "-m", "test(api): red repro")
        red_sha = git(R, "rev-parse", "HEAD").stdout.strip()
        (AD / "red-sha.txt").write_text(red_sha + "\n", encoding="utf-8")
        (R / "src" / "a.ts").write_text(
            "function widen(n: number): number {\n"
            "  return n * 2;\n"
            "}\n"
            "export { widen };\n",
            encoding="utf-8",
        )
        git(R, "add", "src/a.ts")
        git(R, "commit", "-q", "-m", "fix(api): widen")
        write_json(AD / "params.json", {
            "spec": "spec.md", "slug": "toy-bug", "branch": "fix/toy", "worktree": str(R),
        })
        (AD / "repo.txt").write_text("api\n", encoding="utf-8")
        write_json(AD / "failing-test.json", {
            "repo": "api", "command": "true", "kind": "unit", "test_file": "src/repro.spec.ts",
        })
        write_json(AD / "files-allowlist.json", ["src/a.ts", "src/repro.spec.ts", "src/scratch.ts"])
        write_json(AD / "verify.json", {"test_patterns": ["a"]})
        write_json(AD / "deslop-result.json", {
            "changed_files": ["src/a.ts"], "passes": [], "reported_not_fixed": [],
            "verification": {"typecheck": 0, "lint": 0, "tests": 0},
        })
        write_json(AD / "deslop-review.json", CLEAN_REVIEW)
        return R, AD

    # -- node runner ---------------------------------------------------

    def run_node(self, node_id, AD):
        body = runnable_body("bugfix", node_id)
        env = dict(os.environ, ARTIFACTS_DIR=str(AD))
        env["PATH"] = str(self.bindir) + os.pathsep + env["PATH"]
        return subprocess.run(["bash", "-c", body], capture_output=True, text=True, env=env)

    def manifest(self, R):
        out = git(R, "ls-files", "-co", "--exclude-standard").stdout
        files = sorted(f for f in out.splitlines() if f and f != "pnpm-lock.yaml")
        lines = []
        for f in files:
            p = R / f
            if p.is_file():
                h = subprocess.run(
                    ["shasum", "-a", "256", str(p)], capture_output=True, text=True, check=True
                ).stdout.split()[0]
                lines.append(f"{h}  {f}")
        return "\n".join(lines)


# === A. baseline + restorability (3 tamper classes, chained) =============

class DeslopTamperRestoreResumeTest(DeslopDrillBase):
    """Ports drill.sh section A: a baseline checkpoint, then three tamper
    classes applied and restored back-to-back on an evolving round counter
    (edit+delete -> reviewer commit -> reviewer-added file), each detected,
    restored using ONLY the triple the gate prints, and resumed via a fresh
    deslop-recheck to a clean deslop-review-gate pass."""

    def test_baseline_checkpoint_pins_lockfile_to_head(self):
        R, AD = self.mkfix("A")
        r = self.run_node("deslop-recheck", AD)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DESLOP_GATE=PASS", r.stdout)
        rd = AD / "deslop-round-1"
        self.assertTrue((rd / "checkpoint.tar").stat().st_size > 0)
        ck = (rd / "checkpoint-tree.txt").read_text().strip()
        self.assertEqual(git(R, "cat-file", "-t", ck).stdout.strip(), "tree")
        ck_lock = git(R, "ls-tree", ck, "pnpm-lock.yaml").stdout.split()[2]
        head_lock = git(R, "rev-parse", "HEAD:pnpm-lock.yaml").stdout.strip()
        self.assertEqual(ck_lock, head_lock)

    def test_tamper_detect_restore_resume_chain(self):
        R, AD = self.mkfix("A")
        r = self.run_node("deslop-recheck", AD)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)  # round=1
        before_head = git(R, "rev-parse", "HEAD").stdout.strip()

        (R / "src" / "scratch.ts").write_text("untracked\n", encoding="utf-8")
        before = self.manifest(R)
        r = self.run_node("deslop-recheck", AD)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)  # round=2
        ck = (AD / "deslop-round-2" / "checkpoint-tree.txt").read_text().strip()

        def tamper_edit_delete():
            with (R / "src" / "a.ts").open("a") as f:
                f.write("TAMPERED\n")
            (R / "src" / "scratch.ts").unlink()

        def tamper_reviewer_commit():
            with (R / "src" / "a.ts").open("a") as f:
                f.write("// slop\n")
            git(R, "add", "-A")
            git(R, "commit", "-q", "-m", "reviewer commit")

        def tamper_reviewer_added_file():
            (R / "src" / "reviewer.ts").write_text("added by reviewer\n", encoding="utf-8")

        for label, tamper in [
            ("T1-edit+delete", tamper_edit_delete),
            ("T2-commit", tamper_reviewer_commit),
            ("T3-added-file", tamper_reviewer_added_file),
        ]:
            with self.subTest(label=label):
                tamper()
                r = self.run_node("deslop-review-gate", AD)
                out = r.stdout + r.stderr
                self.assertEqual(r.returncode, 1, out)
                self.assertIn("DESLOP_REVIEW=FAIL reviewer modified tree", out)
                self.assertIn("reset --soft", out)
                self.assertIn("read-tree", out)
                self.assertIn("clean -fd", out)

                tree_txt = (AD / "deslop-tree.txt").read_text()
                saved_head = re.search(r"^head=(.+)$", tree_txt, re.M).group(1)

                git(R, "reset", "--soft", saved_head)
                git(R, "read-tree", ck)
                git(R, "checkout-index", "-af")
                git(R, "clean", "-fd")

                after = self.manifest(R)
                self.assertEqual(after, before, f"{label}: sha256 manifest drift after restore")
                after_head = git(R, "rev-parse", "HEAD").stdout.strip()
                self.assertEqual(after_head, before_head, f"{label}: HEAD diverged after restore")

                # Immediate re-run: the triple leaves the INDEX holding the
                # checkpoint tree, so this either passes outright or fails
                # ONLY on the index line -- never anything beyond it.
                r2 = self.run_node("deslop-review-gate", AD)
                self.assertIn(r2.returncode, (0, 1), f"{label}: unexpected rc {r2.returncode}")
                residual = [
                    ln for ln in re.findall(r"^[<>] .*$", r2.stdout, re.M) if "index=" not in ln
                ]
                self.assertEqual(residual, [], f"{label}: residual drift beyond the index: {residual}")

                # Resume: the documented recovery is a fresh deslop-recheck,
                # which re-takes the whole checkpoint.
                r3 = self.run_node("deslop-recheck", AD)
                self.assertEqual(r3.returncode, 0, f"{label}: resume recheck failed: {r3.stdout + r3.stderr}")
                n = (AD / "deslop-round.txt").read_text().strip()
                ck = (AD / f"deslop-round-{n}" / "checkpoint-tree.txt").read_text().strip()
                r4 = self.run_node("deslop-review-gate", AD)
                self.assertEqual(r4.returncode, 0, f"{label}: gate not clean after resume: {r4.stdout + r4.stderr}")
                self.assertIn("<promise>DESLOP_CLEAN</promise>", r4.stdout)


# === B. frozen repro: writer edits the repro test =========================

class DeslopFrozenReproRecheckTest(DeslopDrillBase):
    def test_uncommitted_repro_edit_fails_recheck(self):
        R, AD = self.mkfix("B")
        with (R / "src" / "repro.spec.ts").open("a") as f:
            f.write("\n// writer touched the repro\n")
        r = self.run_node("deslop-recheck", AD)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("DESLOP=FAIL repro test modified", r.stdout)

    def test_committed_repro_edit_fails_recheck(self):
        R, AD = self.mkfix("B2")
        with (R / "src" / "repro.spec.ts").open("a") as f:
            f.write("\n// writer touched the repro\n")
        git(R, "add", "-A")
        git(R, "commit", "-q", "-m", "sneak")
        r = self.run_node("deslop-recheck", AD)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("DESLOP=FAIL repro test modified", r.stdout)


# === C. lockfile-only tree -> SKIP, no commit =============================

class DeslopLockfileOnlyTest(DeslopDrillBase):
    def test_dirty_lockfile_alone_passes_scope_and_produces_no_commit(self):
        R, AD = self.mkfix("C")
        r = self.run_node("deslop-recheck", AD)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DESLOP_GATE=PASS", r.stdout)
        ck_lock = git(R, "ls-tree", (AD / "deslop-round-1" / "checkpoint-tree.txt").read_text().strip(),
                       "pnpm-lock.yaml").stdout.split()[2]
        self.assertEqual(ck_lock, git(R, "rev-parse", "HEAD:pnpm-lock.yaml").stdout.strip())
        n_commits = git(R, "rev-list", "--count", "HEAD").stdout.strip()
        r = self.run_node("deslop-commit", AD)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DESLOP_COMMIT=SKIP nothing to commit", r.stdout)
        self.assertEqual(git(R, "rev-list", "--count", "HEAD").stdout.strip(), n_commits)
        self.assertEqual(git(R, "status", "--porcelain", "pnpm-lock.yaml").stdout, " M pnpm-lock.yaml\n")


# === D. deslop edit present -> commit created ==============================

class DeslopEditCommitTest(DeslopDrillBase):
    def test_deslop_edit_is_committed_without_the_lockfile(self):
        R, AD = self.mkfix("D")
        p = R / "src" / "a.ts"
        p.write_text(p.read_text().replace("return n * 2;", "return n * 2; // narrating\n"), encoding="utf-8")
        n_commits = git(R, "rev-list", "--count", "HEAD").stdout.strip()
        r = self.run_node("deslop-commit", AD)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DESLOP_COMMIT=OK sha=", r.stdout)
        self.assertEqual(git(R, "rev-list", "--count", "HEAD").stdout.strip(), str(int(n_commits) + 1))
        self.assertEqual(git(R, "log", "-1", "--pretty=%s").stdout.strip(), "fix(deslop): remove ai slop from toy-bug")
        self.assertEqual(git(R, "status", "--porcelain", "pnpm-lock.yaml").stdout, " M pnpm-lock.yaml\n")
        committed = git(R, "show", "--name-only", "--pretty=format:", "HEAD").stdout
        self.assertNotIn("pnpm-lock.yaml", committed)


# === E. review-gate schema fixtures ========================================

class DeslopReviewGateSchemaTest(DeslopDrillBase):
    def setUp(self):
        super().setUp()
        self.R, self.AD = self.mkfix("E")
        r = self.run_node("deslop-recheck", self.AD)
        assert r.returncode == 0, r.stdout + r.stderr

    def test_e1_clean_produces_promise(self):
        r = self.run_node("deslop-review-gate", self.AD)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("<promise>DESLOP_CLEAN</promise>", r.stdout)

    def test_e2_declared_clean_with_blocking_finding_is_derived_dirty(self):
        review = dict(CLEAN_REVIEW)
        review["findings"] = [{
            "guard": "comments", "file": "src/a.ts", "line": 2,
            "confidence": 75, "evidence": "narrating comment",
        }]
        write_json(self.AD / "deslop-review.json", review)
        r = self.run_node("deslop-review-gate", self.AD)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("DESLOP=DIRTY", r.stdout)
        self.assertNotIn("<promise>", r.stdout)

    def test_e3_declared_dirty_with_zero_blocking_is_inconsistent(self):
        review = dict(CLEAN_REVIEW)
        review["verdict"] = "DIRTY"
        review["findings"] = []
        write_json(self.AD / "deslop-review.json", review)
        r = self.run_node("deslop-review-gate", self.AD)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("verdict inconsistent", r.stdout)

    def test_e4_malformed_bad_confidence(self):
        review = dict(CLEAN_REVIEW)
        review["verdict"] = "DIRTY"
        review["findings"] = [{
            "guard": "comments", "file": "src/a.ts", "line": 2, "confidence": 90, "evidence": "x",
        }]
        write_json(self.AD / "deslop-review.json", review)
        r = self.run_node("deslop-review-gate", self.AD)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("malformed finding", r.stdout)

    def test_e5_malformed_extra_top_level_key(self):
        review = dict(CLEAN_REVIEW)
        review["findings"] = []
        review["notes"] = "hi"
        write_json(self.AD / "deslop-review.json", review)
        r = self.run_node("deslop-review-gate", self.AD)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("malformed finding", r.stdout)

    def test_e6_malformed_unparseable_json(self):
        (self.AD / "deslop-review.json").write_text("{not json", encoding="utf-8")
        r = self.run_node("deslop-review-gate", self.AD)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("malformed finding", r.stdout)

    def test_e7_coverage_incomplete_missing_guard(self):
        write_json(self.AD / "deslop-review.json", {
            "verdict": "CLEAN",
            "coverage": {
                k: {"status": "assessed", "evidence": "e"}
                for k in ("complexity", "tautological_tests", "yagni", "comments")
            },
            "findings": [],
        })
        r = self.run_node("deslop-review-gate", self.AD)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("coverage incomplete round=1 missing=open_closed", r.stdout)

    def test_e8_coverage_incomplete_empty_evidence(self):
        review = {
            "verdict": "CLEAN",
            "coverage": {
                k: {"status": "assessed", "evidence": "e"}
                for k in ("complexity", "tautological_tests", "yagni", "open_closed", "comments")
            },
            "findings": [],
        }
        review["coverage"]["yagni"]["evidence"] = "   "
        write_json(self.AD / "deslop-review.json", review)
        r = self.run_node("deslop-review-gate", self.AD)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("coverage incomplete", r.stdout)

    def test_e9_missing_review_json_entirely(self):
        (self.AD / "deslop-review.json").unlink()
        r = self.run_node("deslop-review-gate", self.AD)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("no deslop-review.json", r.stdout)


# === F. mechanical recheck negative controls ================================

class DeslopRecheckNegativeControlsTest(DeslopDrillBase):
    def test_f_scope_breach(self):
        R, AD = self.mkfix("F")
        (R / "src" / "rogue.ts").write_text("out of scope\n", encoding="utf-8")
        r = self.run_node("deslop-recheck", AD)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("DESLOP_GATE=FAIL scope", r.stdout)

    def test_f2_no_deslop_result_json(self):
        R, AD = self.mkfix("F2")
        (AD / "deslop-result.json").unlink()
        r = self.run_node("deslop-recheck", AD)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("DESLOP_GATE=FAIL no deslop-result.json", r.stdout)

    def test_f3_verify_json_no_test_patterns(self):
        R, AD = self.mkfix("F3")
        write_json(AD / "verify.json", {"test_patterns": []})
        r = self.run_node("deslop-recheck", AD)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("verify.json has no test_patterns", r.stdout)

    def test_f4_typecheck_failure_records_all_six_exit_codes(self):
        R, AD = self.mkfix("F4")
        pkg = json.loads((R / "package.json").read_text())
        pkg["scripts"]["typecheck"] = "exit 3"
        write_json(R / "package.json", pkg)
        r = self.run_node("deslop-recheck", AD)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("DESLOP_GATE=FAIL typecheck", r.stdout)
        recheck = json.loads((AD / "deslop-round-1" / "recheck.json").read_text())
        self.assertEqual(sorted(recheck), ["lint", "repro", "scope", "slop", "tests", "typecheck"])
        self.assertEqual(recheck["typecheck"], 3)

    def test_f5_repro_no_longer_green(self):
        R, AD = self.mkfix("F5")
        ft = json.loads((AD / "failing-test.json").read_text())
        ft["command"] = "exit 1"
        write_json(AD / "failing-test.json", ft)
        r = self.run_node("deslop-recheck", AD)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("DESLOP_GATE=FAIL repro no longer green", r.stdout)


# === G. deslop-commit re-checks the frozen repro ============================

class DeslopCommitReproGuardTest(DeslopDrillBase):
    def test_g_uncommitted_repro_edit_blocks_commit_leaves_nothing_staged(self):
        R, AD = self.mkfix("G")
        with (R / "src" / "repro.spec.ts").open("a") as f:
            f.write("\n// touched\n")
        with (R / "src" / "a.ts").open("a") as f:
            f.write("// deslop\n")
        n_commits = git(R, "rev-list", "--count", "HEAD").stdout.strip()
        r = self.run_node("deslop-commit", AD)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("DESLOP=FAIL repro test modified", r.stdout)
        self.assertNotIn("DESLOP_COMMIT=OK", r.stdout)
        self.assertEqual(git(R, "rev-list", "--count", "HEAD").stdout.strip(), n_commits)
        self.assertEqual(git(R, "diff", "--cached", "--name-only").stdout.strip(), "")

    def test_g2_committed_repro_edit_blocks_commit(self):
        R, AD = self.mkfix("G2")
        with (R / "src" / "repro.spec.ts").open("a") as f:
            f.write("\n// touched\n")
        git(R, "add", "-A")
        git(R, "commit", "-q", "-m", "sneak")
        n_commits = git(R, "rev-list", "--count", "HEAD").stdout.strip()
        r = self.run_node("deslop-commit", AD)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("DESLOP=FAIL repro test modified", r.stdout)
        self.assertEqual(git(R, "rev-list", "--count", "HEAD").stdout.strip(), n_commits)


# === H. check-slop scans only the in-scope non-repro file ===================

class DeslopCheckSlopScopeTest(DeslopDrillBase):
    def test_only_non_repro_in_scope_file_is_scanned(self):
        R, AD = self.mkfix("H")
        r = self.run_node("deslop-recheck", AD)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("SLOP=OK files=1", r.stdout)


# === I. repro harness error (rc 97) typed apart from a red repro ============

class DeslopReproHarnessErrorTest(DeslopDrillBase):
    def test_i2_missing_command_field_is_harness_error_not_red_repro(self):
        # run-repro.sh itself starts fine (only test_file is read to get this
        # far); it exits 97 because failing-test.json has no "command".
        R, AD = self.mkfix("I2")
        ft = json.loads((AD / "failing-test.json").read_text())
        del ft["command"]
        write_json(AD / "failing-test.json", ft)
        r = self.run_node("deslop-recheck", AD)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("DESLOP_GATE=FAIL repro harness error rc=97", r.stdout)

    def test_i3_red_repro_is_typed_as_red_not_harness_error(self):
        R, AD = self.mkfix("I3")
        ft = json.loads((AD / "failing-test.json").read_text())
        ft["command"] = "exit 1"
        write_json(AD / "failing-test.json", ft)
        r = self.run_node("deslop-recheck", AD)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("DESLOP_GATE=FAIL repro no longer green round=1 rc=1", r.stdout)
        self.assertNotIn("repro harness error", r.stdout)


# === J. beyond_five_guards finding channel ===================================

class DeslopBeyondFiveGuardsTest(DeslopDrillBase):
    def setUp(self):
        super().setUp()
        self.R, self.AD = self.mkfix("J")
        r = self.run_node("deslop-recheck", self.AD)
        assert r.returncode == 0, r.stdout + r.stderr

    def test_j1_beyond_five_guards_at_confidence_100_blocks(self):
        review = dict(CLEAN_REVIEW)
        review["verdict"] = "DIRTY"
        review["findings"] = [{
            "guard": "beyond_five_guards", "file": "src/a.ts", "line": 2,
            "confidence": 100, "evidence": "writer deduplicated a helper it was told to report",
        }]
        write_json(self.AD / "deslop-review.json", review)
        r = self.run_node("deslop-review-gate", self.AD)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("DESLOP=DIRTY", r.stdout)
        self.assertIn("DESLOP_FINDING guard=beyond_five_guards", r.stdout)

    def test_j2_beyond_five_guards_below_confidence_75_passes(self):
        review = dict(CLEAN_REVIEW)
        review["findings"] = [{
            "guard": "beyond_five_guards", "file": "src/a.ts", "line": 2,
            "confidence": 50, "evidence": "writer deduplicated a helper it was told to report",
        }]
        write_json(self.AD / "deslop-review.json", review)
        r = self.run_node("deslop-review-gate", self.AD)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("<promise>DESLOP_CLEAN</promise>", r.stdout)

    def test_j3_bogus_guard_name_rejected(self):
        review = dict(CLEAN_REVIEW)
        review["findings"] = [{
            "guard": "vibes", "file": "src/a.ts", "line": 2, "confidence": 50, "evidence": "x",
        }]
        write_json(self.AD / "deslop-review.json", review)
        r = self.run_node("deslop-review-gate", self.AD)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("malformed finding", r.stdout)

    def test_j4_coverage_still_requires_the_original_five(self):
        write_json(self.AD / "deslop-review.json", {
            "verdict": "CLEAN",
            "coverage": {
                k: {"status": "assessed", "evidence": "e"}
                for k in ("complexity", "tautological_tests", "yagni", "comments", "beyond_five_guards")
            },
            "findings": [],
        })
        r = self.run_node("deslop-review-gate", self.AD)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("coverage incomplete round=1 missing=open_closed", r.stdout)


if __name__ == "__main__":
    unittest.main()
