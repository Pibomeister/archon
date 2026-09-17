#!/usr/bin/env python3
"""Tests for check-slop.py against a real temp git repo fixture: exercises all
four guards, the legacy-excess REPORT-not-FAIL distinction, a clean fixture,
the working-tree-vs-base..HEAD anchoring negative control, and the C-quoted
porcelain paths that used to drop a file with a space in its name."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "check-slop.py"

LEGACY_FN_14 = "\n".join(
    ["export function legacyFn(x: number): number {", "  let r = 0;"]
    + [f"  if (x === {i}) r += 1;" for i in range(1, 15)]
    + ["  return r;", "}", ""]
)

LEGACY_FN_14_TWEAKED = "\n".join(
    ["export function legacyFn(x: number): number {", "  let r = 0;"]
    + [f"  if (x === {i}) r += 1;" for i in range(1, 15)]
    + ["  return r + 0;", "}", ""]  # one-line change, branch count unchanged
)

NEW_FN_14 = "\n".join(
    ["function newFn(x: number): number {", "  let r = 0;"]
    + [f"  if (x === {i}) r += 2;" for i in range(1, 15)]
    + ["  return r;", "}", ""]
)


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, encoding="utf-8", check=check)


def init_repo(repo):
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "test")


def run(worktree, base, *extra):
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(worktree), base, *extra],
        capture_output=True, encoding="utf-8",
    )


class CheckSlopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    # --- the combined fixture: exactly 4 FAIL + 1 REPORT ---------------

    def _dirty_repo(self):
        repo = self.tmp / "repo"
        init_repo(repo)
        (repo / "src").mkdir()
        (repo / "src" / "example.ts").write_text(LEGACY_FN_14, encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "base")
        base = git(repo, "rev-parse", "HEAD").stdout.strip()

        # 1) legacy 14-branch function gets a one-line change -> REPORT not FAIL
        # 2) a NEW 14-branch function is added -> FAIL complexity
        # 3) a narrating comment above counter++ -> FAIL narrating_comment
        working = LEGACY_FN_14_TWEAKED + "\n" + NEW_FN_14 + (
            "\nlet counter = 0;\n// increment counter\ncounter++;\n"
            "\nexport const unusedThing = 42;\n"
        )
        (repo / "src" / "example.ts").write_text(working, encoding="utf-8")

        # 4) tautological test in a brand-new spec file -> FAIL tautological_test
        (repo / "src" / "example.spec.ts").write_text(
            "import { legacyFn } from \"./example\";\n\n"
            "describe(\"legacyFn\", () => {\n"
            "  it(\"is tautological\", () => {\n"
            "    const a = 5;\n"
            "    expect(a).toBe(a);\n"
            "  });\n"
            "});\n",
            encoding="utf-8",
        )
        # left uncommitted/untracked deliberately: proves working-tree anchoring
        return repo, base

    def test_combined_fixture_exactly_4_fail_1_report(self):
        repo, base = self._dirty_repo()
        r = run(repo, base)
        lines = r.stdout.splitlines()
        fail_lines = [l for l in lines if l.startswith("SLOP=FAIL ") and not l.startswith("SLOP=FAIL count=")]
        report_lines = [l for l in lines if l.startswith("SLOP=REPORT")]
        self.assertEqual(len(fail_lines), 4, r.stdout)
        self.assertEqual(len(report_lines), 1, r.stdout)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(lines[-1], "SLOP=FAIL count=4")

        guards = {l.split()[1] for l in fail_lines}
        self.assertEqual(guards, {"complexity", "tautological_test", "narrating_comment", "yagni"})
        self.assertIn("SLOP=REPORT complexity", report_lines[0])
        self.assertIn("legacyFn", report_lines[0])

    def test_working_tree_anchoring_not_base_dot_dot_head(self):
        # The implementation is entirely uncommitted (HEAD == base): a
        # base..HEAD-anchored run would see zero files. check-slop.py must
        # still find the working-tree functions.
        repo, base = self._dirty_repo()
        head = git(repo, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(base, head, "fixture must leave HEAD at base for this control")
        head_diff = git(repo, "diff", "--name-only", f"{base}..HEAD").stdout
        self.assertEqual(head_diff.strip(), "", "sanity: base..HEAD is empty since nothing was committed")

        r = run(repo, base)
        self.assertNotIn("SLOP=OK files=0", r.stdout)
        self.assertIn("SLOP=FAIL", r.stdout)  # it did see the working-tree files

    # --- clean fixture ---------------------------------------------------

    def test_clean_fixture_ok(self):
        repo = self.tmp / "clean"
        init_repo(repo)
        (repo / "src").mkdir()
        (repo / "src" / "clean.ts").write_text(
            "export function add(a: number, b: number): number {\n  return a + b;\n}\n",
            encoding="utf-8",
        )
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "base")
        base = git(repo, "rev-parse", "HEAD").stdout.strip()

        (repo / "src" / "clean.ts").write_text(
            "export function add(a: number, b: number): number {\n"
            "  // sums two numbers using a positive delta\n"
            "  return a + b;\n"
            "}\n"
            "\n"
            "export function subtract(a: number, b: number): number {\n"
            "  return a - b;\n"
            "}\n",
            encoding="utf-8",
        )
        (repo / "src" / "clean.spec.ts").write_text(
            "import { add, subtract } from \"./clean\";\n\n"
            "describe(\"clean\", () => {\n"
            "  it(\"adds\", () => {\n"
            "    expect(add(1, 2)).toBe(3);\n"
            "  });\n"
            "  it(\"subtracts\", () => {\n"
            "    expect(subtract(3, 1)).toBe(2);\n"
            "  });\n"
            "});\n",
            encoding="utf-8",
        )
        r = run(repo, base)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(r.stdout.strip().startswith("SLOP=OK files="))
        self.assertNotIn("SLOP=FAIL", r.stdout)

    # --- --exclude passthrough -------------------------------------------

    def test_exclude_skips_file(self):
        repo, base = self._dirty_repo()
        r = run(repo, base, "--exclude", "src/example.ts", "--exclude", "src/example.spec.ts")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(r.stdout.strip().startswith("SLOP=OK files=0"))

    # --- porcelain path quoting -------------------------------------------

    def test_untracked_path_with_space_is_scanned(self):
        # git status --porcelain (without -z) C-quotes any path it considers
        # unusual: `?? "src/new file.ts"`. Keeping the quotes makes the .ts suffix
        # test fail, so the file is dropped from the scan and its slop ships. The
        # file below carries an unambiguous narrating comment, so a scanned file
        # MUST produce SLOP=FAIL naming it.
        repo = self.tmp / "spacey"
        init_repo(repo)
        (repo / "src").mkdir()
        (repo / "src" / "base.ts").write_text("export const x = 1;\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "base")
        base = git(repo, "rev-parse", "HEAD").stdout.strip()

        (repo / "src" / "new file.ts").write_text(
            "let counter = 0;\n// increment counter\ncounter++;\n", encoding="utf-8"
        )
        # Negative control on the fixture itself: prove git really does quote it,
        # so this test fails for the reason it claims if git ever stops.
        porcelain = git(repo, "status", "--porcelain").stdout
        self.assertIn('"src/new file.ts"', porcelain, porcelain)

        r = run(repo, base)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("src/new file.ts", r.stdout)
        self.assertIn("SLOP=FAIL narrating_comment", r.stdout)

    def test_committed_path_with_space_is_scanned(self):
        # Same class on the other input: the diff --name-only lane. Committing the
        # file takes it out of porcelain entirely, so only the diff lane can find it.
        repo = self.tmp / "spacey-committed"
        init_repo(repo)
        (repo / "src").mkdir()
        (repo / "src" / "base.ts").write_text("export const x = 1;\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "base")
        base = git(repo, "rev-parse", "HEAD").stdout.strip()

        (repo / "src" / "new file.ts").write_text(
            "let counter = 0;\n// increment counter\ncounter++;\n", encoding="utf-8"
        )
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "spacey")
        self.assertEqual(git(repo, "status", "--porcelain").stdout.strip(), "")

        r = run(repo, base)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("src/new file.ts", r.stdout)

    def test_renamed_path_with_space_uses_new_name(self):
        # With -z a rename is two records: the NEW path, then the ORIGINAL. Both
        # carry a space here, so this covers the quoting fix and the record-pairing
        # at once: the new path must be scanned (it holds the narrating comment) and
        # the original — which no longer exists — must not be reported.
        repo = self.tmp / "renamed"
        init_repo(repo)
        (repo / "src").mkdir()
        (repo / "src" / "old name.ts").write_text(
            "let counter = 0;\n// increment counter\ncounter++;\n", encoding="utf-8"
        )
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "base")
        base = git(repo, "rev-parse", "HEAD").stdout.strip()

        git(repo, "mv", "src/old name.ts", "src/new name.ts")
        porcelain = git(repo, "status", "--porcelain").stdout
        self.assertIn("R", porcelain, porcelain)

        r = run(repo, base)
        self.assertIn("src/new name.ts", r.stdout, r.stdout + r.stderr)
        self.assertNotIn("src/old name.ts", r.stdout)
        self.assertIn("SLOP=FAIL narrating_comment", r.stdout)

    def _base_repo(self):
        repo = self.tmp / "yagni"
        init_repo(repo)
        (repo / "README.md").write_text("x\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "base")
        return repo, git(repo, "rev-parse", "HEAD").stdout.strip()

    def test_yagni_counts_a_same_file_reference(self):
        # run 48c1c1c5: a DTO referenced as a property type in its own file.
        repo, base = self._base_repo()
        (repo / "help.dto.ts").write_text(
            "export class SetupHomeDto {\n  url!: string;\n}\n\n"
            "export class HelpDto {\n  home!: SetupHomeDto;\n}\n"
            "export const unusedThing = 42;\n", encoding="utf-8")
        (repo / "use.ts").write_text("import { HelpDto } from './help.dto';\n", encoding="utf-8")
        r = run(repo, base)
        self.assertNotIn("export=SetupHomeDto", r.stdout)
        self.assertIn("export=unusedThing reason=unreferenced", r.stdout,
                      "control: a symbol named only on its declaration line is still flagged")

    def test_yagni_skips_profile_framework_loaded_files(self):
        repo, base = self._base_repo()
        mig = repo / "libs/data-access/src/lib/rds/migrations/1792010000000-add-x.ts"
        mig.parent.mkdir(parents=True)
        mig.write_text("export class AddX1792010000000 {}\n", encoding="utf-8")
        self.assertIn("export=AddX1792010000000", run(repo, base).stdout,
                      "control: without --repo the glob-loaded class is flagged")
        r = run(repo, base, "--repo", "api")
        self.assertEqual(0, r.returncode, r.stdout)
        self.assertNotIn("yagni", r.stdout)

    def test_yagni_reports_an_approved_contract_symbol_instead_of_failing(self):
        # chain 42b42a13: the plan reserved TravelCandidatesDto on the response DTO.
        repo, base = self._base_repo()
        (repo / "dto").mkdir()
        (repo / "dto" / "briefing-response.dto.ts").write_text(
            "export class TravelCandidatesDto {}\nexport const unusedThing = 42;\n", encoding="utf-8")
        contracts = self.tmp / "contract-symbols.json"
        contracts.write_text(json.dumps({"contracts": [
            {"artifact": "dto/briefing-response.dto.ts", "symbols": ["TravelCandidatesDto"]},
            {"artifact": "dto/other.dto.ts", "symbols": ["unusedThing"]},
        ]}), encoding="utf-8")
        self.assertIn("SLOP=FAIL yagni file=dto/briefing-response.dto.ts line=1 export=TravelCandidatesDto",
                      run(repo, base).stdout, "control: without the contract the export is flagged")
        r = run(repo, base, "--contract-symbols", str(contracts))
        self.assertIn("SLOP=REPORT yagni file=dto/briefing-response.dto.ts line=1 export=TravelCandidatesDto "
                      "reason=approved-contract", r.stdout)
        self.assertNotIn("SLOP=FAIL yagni file=dto/briefing-response.dto.ts line=1", r.stdout)
        # A symbol declared for ANOTHER file does not cover this one.
        self.assertIn("SLOP=FAIL yagni file=dto/briefing-response.dto.ts line=2 export=unusedThing", r.stdout)
        self.assertEqual(1, r.returncode, r.stdout)

    def test_an_unreadable_contract_file_keeps_the_strict_guard(self):
        repo, base = self._base_repo()
        (repo / "x.ts").write_text("export class Reserved {}\n", encoding="utf-8")
        r = run(repo, base, "--contract-symbols", str(self.tmp / "missing.json"))
        self.assertIn("SLOP=FAIL yagni file=x.ts line=1 export=Reserved", r.stdout)

    def test_untracked_files_in_a_new_directory_are_scanned(self):
        repo, base = self._base_repo()
        (repo / "newmod" / "deep").mkdir(parents=True)
        (repo / "newmod" / "deep" / "x.ts").write_text("export const unusedThing = 42;\n", encoding="utf-8")
        self.assertIn("file=newmod/deep/x.ts", run(repo, base).stdout)

    def test_max_complexity_override(self):
        repo, base = self._dirty_repo()
        r = run(repo, base, "--max-complexity", "50")
        fail_lines = [l for l in r.stdout.splitlines() if l.startswith("SLOP=FAIL complexity")]
        self.assertEqual(fail_lines, [])  # nothing crosses 50 branches


if __name__ == "__main__":
    unittest.main()
