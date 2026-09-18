#!/usr/bin/env python3
"""Commit nodes stage only what the allowlist names.

`git add -A` staged whatever an agent left in the worktree. A round-2 fixer
wrote a scratch probe (`__tests__/zzz-timing-check.spec.ts`), `git add -A`
committed it, and converge's post-hoc scope guard then stopped the run for a
breach that was already in history and only a human could clear. The guard was
right and too late: staging is where scope has to be enforced, because that is
the last point at which a stray is still a deletable file."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ARCHON = Path(__file__).resolve().parent.parent.parent
SCRIPT = ARCHON / "setup" / "check-scope.py"
LANES = ["full-sdlc-api", "bugfix", "full-sdlc-web", "full-sdlc-api-lite", "bugfix-lite"]


def walk(nodes):
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        yield n
        for key in ("loop_group", "body"):
            v = n.get(key)
            if isinstance(v, dict):
                yield from walk(v.get("nodes"))
            elif isinstance(v, list):
                yield from walk(v)


def commit_nodes(lane):
    doc = yaml.safe_load((ARCHON / "workflows" / f"{lane}.yaml").read_text(encoding="utf-8"))
    return [(n["id"], n["bash"]) for n in walk(doc.get("nodes")) if "git commit -m" in (n.get("bash") or "")]


class CommitStagingScope(unittest.TestCase):
    def test_no_commit_node_stages_the_whole_worktree(self):
        for lane in LANES:
            for nid, bash in commit_nodes(lane):
                for line in bash.splitlines():
                    line = line.strip()
                    if line.startswith("#") or "git add -A" not in line:
                        continue
                    # The deslop checkpoint writes a throwaway index to hash the
                    # tree; it never commits, so -A there is correct.
                    self.assertIn("GIT_INDEX_FILE=", line, f"{lane}/{nid}: {line}")

    def test_every_commit_node_stages_through_the_scope_gate(self):
        for lane in LANES:
            nodes = commit_nodes(lane)
            self.assertTrue(nodes, f"{lane}: probe found no commit nodes")
            for nid, bash in nodes:
                gated = "check-scope.py" in bash and "--stage" in bash
                # commit-red predates the gate and is stricter than it: it stages
                # the one file named by failing-test.json and nothing else.
                explicit = any(
                    l.strip().startswith("git add ") and "-A" not in l and "--stage" not in l
                    for l in bash.splitlines()
                )
                self.assertTrue(gated or explicit,
                                f"{lane}/{nid} commits without staging through the allowlist")

    def test_the_gate_runs_inside_the_worktree(self):
        # The gate takes the worktree as an argument. The web lane never sets
        # $WT -- it cds into the path from params.json -- so the sites pass
        # "$PWD" and every one of them must be preceded by a cd.
        for lane in LANES:
            for nid, bash in commit_nodes(lane):
                lines = bash.splitlines()
                idx = [i for i, l in enumerate(lines) if "--stage" in l]
                if not idx:
                    continue
                self.assertIn('"$PWD"', lines[idx[0]], f"{lane}/{nid} gate must judge the cwd")
                before = lines[: idx[0]]
                self.assertTrue(any(l.strip().startswith("cd ") for l in before),
                                f"{lane}/{nid} stages before entering the worktree")

    def test_the_probe_is_not_vacuous(self):
        # A probe that matches no node passes every assertion above.
        total = sum(len(commit_nodes(lane)) for lane in LANES)
        self.assertGreaterEqual(total, 12, "commit-node probe stopped matching real nodes")


class StageMode(unittest.TestCase):
    def setUp(self):
        self.wt = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.wt, ignore_errors=True)
        run = lambda c: subprocess.run(c, cwd=self.wt, shell=True, check=True, capture_output=True)
        run("git init -q && git config user.email t@t && git config user.name t")
        (self.wt / "keep.ts").write_text("export const a = 1;\n")
        (self.wt / "gone.ts").write_text("export const b = 2;\n")
        (self.wt / "pnpm-lock.yaml").write_text("lock: 1\n")
        run("git add -A && git commit -qm base")
        # Outside the worktree: the real artifacts dir is, and an allowlist file
        # sitting in the tree would be a stray by its own rule.
        self.allow = Path(tempfile.mkdtemp()) / "allowlist.json"
        self.addCleanup(shutil.rmtree, self.allow.parent, ignore_errors=True)
        self.allow.write_text(json.dumps(["keep.ts", "gone.ts"]))

    def stage(self):
        return subprocess.run(
            ["python3", str(SCRIPT), str(self.allow), str(self.wt), "HEAD", "--stage",
             "--exclude", "pnpm-lock.yaml"],
            capture_output=True, encoding="utf-8")

    def staged(self):
        out = subprocess.run(["git", "-C", str(self.wt), "diff", "--cached", "--name-only"],
                             capture_output=True, encoding="utf-8").stdout
        return sorted(p for p in out.split() if p)

    def test_stages_allowlisted_edits_and_deletions(self):
        (self.wt / "keep.ts").write_text("export const a = 2;\n")
        (self.wt / "gone.ts").unlink()
        r = self.stage()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("COMMIT_SCOPE=OK", r.stdout)
        self.assertEqual(self.staged(), ["gone.ts", "keep.ts"])

    def test_a_stray_stops_the_round_and_stages_nothing(self):
        # The observed failure, reproduced: an agent leaves a scratch spec behind.
        (self.wt / "keep.ts").write_text("export const a = 2;\n")
        (self.wt / "zzz-timing-check.spec.ts").write_text("it('probe', () => {});\n")
        r = self.stage()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("COMMIT_SCOPE=STRAY file=zzz-timing-check.spec.ts", r.stdout)
        self.assertEqual(self.staged(), [], "a refused round must leave the index empty")

    def test_excluded_paths_are_neither_staged_nor_a_stray(self):
        (self.wt / "pnpm-lock.yaml").write_text("lock: 2\n")
        (self.wt / "keep.ts").write_text("export const a = 3;\n")
        r = self.stage()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.staged(), ["keep.ts"])

    def test_a_missing_allowlist_fails_typed_not_with_a_traceback(self):
        # This gate now runs inside commit nodes. A node that dies untyped is
        # unreadable to the operator and to the node-stress harness, which
        # counts untyped exits as a defect in its own right.
        self.allow.unlink()
        r = self.stage()
        self.assertEqual(r.returncode, 1)
        self.assertIn("COMMIT_SCOPE=FAIL unreadable files-allowlist.json", r.stdout)
        self.assertNotIn("Traceback", r.stdout + r.stderr)

    def test_an_allowlisted_file_in_a_new_directory_is_not_a_stray(self):
        # Without --untracked-files=all porcelain reports "newmod/", which is not
        # in the allowlist: the round stopped on a file the plan approved.
        (self.wt / "newmod" / "deep").mkdir(parents=True)
        (self.wt / "newmod" / "deep" / "x.ts").write_text("export const x = 1;\n")
        self.allow.write_text(json.dumps(["keep.ts", "gone.ts", "newmod/deep/x.ts"]))
        r = self.stage()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.staged(), ["newmod/deep/x.ts"])
        scan = subprocess.run(["python3", str(SCRIPT), str(self.allow), str(self.wt), "HEAD"],
                              capture_output=True, encoding="utf-8")
        self.assertEqual(scan.returncode, 0, scan.stdout)

    def test_untouched_allowlist_paths_stage_nothing(self):
        r = self.stage()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.staged(), [])


class StrayTriage(unittest.TestCase):
    """--quarantine resolves a stray instead of paging a human, on the one axis
    that separates the two things agents actually leave behind. Run 38d72218
    produced both in the SAME directory: commit-import.util.ts, a new file a
    Blocking repo rule required, and zzz-timing-check.spec.ts, a scratch probe.
    The first shares a stem with an allowlisted file; the second shares nothing.
    Those two cases are the fixtures here, under their real names."""

    def setUp(self):
        self.wt = Path(tempfile.mkdtemp())
        self.art = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.wt, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.art, ignore_errors=True)
        run = lambda c: subprocess.run(c, cwd=self.wt, shell=True, check=True, capture_output=True)
        run("git init -q && git config user.email t@t && git config user.name t")
        for d in ("lib", "lib/__tests__"):
            (self.wt / d).mkdir(parents=True, exist_ok=True)
        (self.wt / "lib/commit-import.service.ts").write_text("export const a = 1;\n")
        (self.wt / "lib/__tests__/commit-import-note-resolution.spec.ts").write_text("it('x', () => {});\n")
        (self.wt / "lib/other.service.ts").write_text("export const b = 2;\n")
        run("git add -A && git commit -qm base")
        self.allow = self.art / "files-allowlist.json"
        self.allow_paths = [
            "lib/commit-import.service.ts",
            "lib/__tests__/commit-import-note-resolution.spec.ts",
        ]
        self.allow.write_text(json.dumps(self.allow_paths))
        (self.art / "params.json").write_text(json.dumps({"repo": "api"}))
        (self.art / "joint-plan.json").write_text(json.dumps({
            "schema": "archon.joint-feature-plan.v1",
            "stages": {"api": {"files_allowlist": list(self.allow_paths)}},
        }))

    def stage(self, feature_scope=None):
        env = os.environ.copy()
        if feature_scope is not None:
            env["ARCHON_FEATURE_SCOPE"] = feature_scope
        return subprocess.run(
            ["python3", str(SCRIPT), str(self.allow), str(self.wt), "HEAD", "--stage",
             "--quarantine", str(self.art), "--exclude", "pnpm-lock.yaml"],
            capture_output=True, encoding="utf-8", env=env)

    def staged(self):
        out = subprocess.run(["git", "-C", str(self.wt), "diff", "--cached", "--name-only"],
                             capture_output=True, encoding="utf-8").stdout
        return sorted(p for p in out.split() if p)

    def test_a_mandated_sibling_is_adopted_and_committed(self):
        (self.wt / "lib/commit-import.util.ts").write_text("export const c = 3;\n")
        r = self.stage()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("COMMIT_SCOPE=ADOPTED file=lib/commit-import.util.ts", r.stdout)
        self.assertIn("lib/commit-import.util.ts", self.staged())
        self.assertIn("lib/commit-import.util.ts", json.loads(self.allow.read_text()))
        rec = json.loads((self.art / "allowlist-auto-expansion.json").read_text())
        self.assertEqual(rec[0]["path"], "lib/commit-import.util.ts")
        self.assertEqual(rec[0]["sibling_of"], ["lib/commit-import.service.ts"])

    def test_scalar_feature_scope_still_adopts_mandated_siblings(self):
        (self.wt / "lib/commit-import.util.ts").write_text("export const c = 3;\n")
        r = self.stage(feature_scope="api")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("COMMIT_SCOPE=ADOPTED file=lib/commit-import.util.ts", r.stdout)
        self.assertIn("lib/commit-import.util.ts", self.staged())
        self.assertIn("lib/commit-import.util.ts", json.loads(self.allow.read_text()))

    def test_repository_list_scope_rejects_sibling_auto_expansion_without_mutation(self):
        sibling = self.wt / "lib/commit-import.util.ts"
        sibling.write_text("export const c = 3;\n")
        before = json.loads(self.allow.read_text())

        r = self.stage(feature_scope="repositories")

        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(
            "COMMIT_SCOPE=STRAY file=lib/commit-import.util.ts "
            "(new sibling outside the approved repository-stage allowlist)",
            r.stdout,
        )
        self.assertIn("no auto-expansion applied", r.stdout)
        self.assertTrue(sibling.exists(), "repo-list rejection must preserve the file")
        self.assertEqual(json.loads(self.allow.read_text()), before)
        self.assertFalse((self.art / "allowlist-auto-expansion.json").exists())
        self.assertFalse((self.art / "strays/lib/commit-import.util.ts").exists())
        self.assertEqual(self.staged(), [], "a refused repo-list round must leave the index empty")

    def test_a_scratch_probe_is_quarantined_not_committed_and_not_deleted(self):
        probe = self.wt / "lib/__tests__/zzz-timing-check.spec.ts"
        probe.write_text("it('probe', () => {});\n")
        r = self.stage()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("COMMIT_SCOPE=QUARANTINED file=lib/__tests__/zzz-timing-check.spec.ts", r.stdout)
        self.assertIn("COMMIT_SCOPE=FAIL", r.stdout)
        self.assertIn("RECOVERY=", r.stdout)
        self.assertNotIn("COMMIT_SCOPE=OK", r.stdout)
        self.assertFalse(probe.exists(), "the stray must leave the worktree")
        kept = self.art / "strays/lib/__tests__/zzz-timing-check.spec.ts"
        self.assertTrue(kept.exists(), "quarantine must keep the file, never delete it")
        self.assertNotIn("lib/__tests__/zzz-timing-check.spec.ts", self.staged())
        self.assertNotIn("zzz-timing-check", self.allow.read_text())

    def test_c4_unrelated_new_file_on_repository_list_quarantines_and_stops(self):
        # Chain 42b42a13: fixer created a new module with no stem sibling; the
        # gate moved it to strays/ and printed COMMIT_SCOPE=OK, so the next
        # round compiled against an import of a file that was gone.
        created = self.wt / "lib/new-module.ts"
        created.write_text("export const n = 1;\n")
        r = self.stage(feature_scope="repositories")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("COMMIT_SCOPE=QUARANTINED file=lib/new-module.ts", r.stdout)
        self.assertIn("COMMIT_SCOPE=FAIL", r.stdout)
        self.assertIn("RECOVERY=", r.stdout)
        self.assertNotIn("COMMIT_SCOPE=OK", r.stdout)
        self.assertFalse(created.exists())
        self.assertTrue((self.art / "strays/lib/new-module.ts").exists())
        self.assertEqual(self.staged(), [])

    def test_hand_edited_allowlist_on_repository_list_is_drift(self):
        edited = list(self.allow_paths) + ["lib/sneak.ts"]
        self.allow.write_text(json.dumps(edited))
        r = self.stage(feature_scope="repositories")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("ALLOWLIST_DRIFT=FAIL", r.stdout)
        self.assertIn("RECOVERY=", r.stdout)
        self.assertNotIn("COMMIT_SCOPE=OK", r.stdout)
        self.assertEqual(self.staged(), [])

    def test_a_sibling_in_another_directory_is_not_adopted(self):
        # The stem alone is not the unit; the directory is half the rule.
        (self.wt / "lib/__tests__/commit-import.util.ts").write_text("export const d = 4;\n")
        r = self.stage()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("COMMIT_SCOPE=QUARANTINED file=lib/__tests__/commit-import.util.ts", r.stdout)
        self.assertIn("RECOVERY=", r.stdout)

    def test_an_edit_to_an_unallowlisted_tracked_file_still_stops(self):
        # Adoption is for NEW files. Editing code someone else owns is a scope
        # decision and stays a human one.
        (self.wt / "lib/other.service.ts").write_text("export const b = 99;\n")
        r = self.stage()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("COMMIT_SCOPE=STRAY file=lib/other.service.ts (modified, not new", r.stdout)
        self.assertEqual(self.staged(), [], "a refused round must leave the index empty")

    def test_a_blocking_edit_is_not_rescued_by_an_adoptable_file_beside_it(self):
        # Mixed batch: the human stop wins and nothing is adopted or moved.
        (self.wt / "lib/commit-import.util.ts").write_text("export const c = 3;\n")
        (self.wt / "lib/other.service.ts").write_text("export const b = 99;\n")
        r = self.stage()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertNotIn("ADOPTED", r.stdout)
        self.assertNotIn("commit-import.util.ts", json.loads(self.allow.read_text()).__str__())

    def test_without_quarantine_the_old_hard_stop_is_unchanged(self):
        # Negative control on the flag itself: no --quarantine, no triage.
        (self.wt / "lib/commit-import.util.ts").write_text("export const c = 3;\n")
        r = subprocess.run(
            ["python3", str(SCRIPT), str(self.allow), str(self.wt), "HEAD", "--stage"],
            capture_output=True, encoding="utf-8")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("COMMIT_SCOPE=STRAY file=lib/commit-import.util.ts", r.stdout)


if __name__ == "__main__":
    unittest.main()
