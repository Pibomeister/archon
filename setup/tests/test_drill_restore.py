"""Restorability negative control, ported from scratchpad/restore-drill.sh.

This does NOT run the deslop-recheck / deslop-review-gate node bodies (that
full-node coverage, including the restore triple the gate itself prints, is
in test_drill_deslop.py's tamper/resume chain). This file isolates the
checkpoint-then-restore ALGORITHM the two nodes share — take a throwaway-index
tree snapshot, detect any drift by recomputing the same three facts, then
recover using ONLY the three git commands the gate prints (reset --soft,
read-tree + checkout-index, clean -fd) — against three tamper classes: an
edit-and-delete, a reviewer commit, and a reviewer-added file. Because this
targets the algorithm rather than one node's bash text, there is no YAML body
to extract live; fidelity to the two nodes is instead pinned by
test_drill_deslop.py running the real nodes end to end.
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from nodes.gitutil import git


def sha256_manifest(repo, paths):
    out = []
    for p in paths:
        f = repo / p
        r = subprocess.run(["shasum", "-a", "256", str(f)], capture_output=True, text=True, check=True)
        out.append(r.stdout.split()[0] + "  " + p)
    return "\n".join(out)


class RestoreDrillTest(unittest.TestCase):
    """One test method per tamper class, each running the full
    checkpoint -> tamper -> detect -> restore -> reverify cycle."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _checkpoint(self, wt, rd):
        """Verbatim from deslop-recheck: throwaway-index write-tree + archive."""
        rd.mkdir(parents=True, exist_ok=True)
        idx = rd / "index"
        idx.unlink(missing_ok=True)
        import os
        env = dict(os.environ, GIT_INDEX_FILE=str(idx))
        subprocess.run(["git", "-C", str(wt), "add", "-A"], env=env, check=True, capture_output=True)
        ck = subprocess.run(
            ["git", "-C", str(wt), "write-tree"], env=env, check=True, capture_output=True, text=True
        ).stdout.strip()
        return ck

    def _recompute_and_detect(self, wt, rd, ck, saved):
        """Verbatim from deslop-review-gate: recompute head/index/checkpoint and
        cmp against the saved triple. Returns True if tamper was detected."""
        import os
        idx = rd / "index-verify"
        idx.unlink(missing_ok=True)
        env = dict(os.environ, GIT_INDEX_FILE=str(idx))
        subprocess.run(["git", "-C", str(wt), "add", "-A"], env=env, check=True, capture_output=True)
        now_ck = subprocess.run(
            ["git", "-C", str(wt), "write-tree"], env=env, check=True, capture_output=True, text=True
        ).stdout.strip()
        now_head = git(wt, "rev-parse", "HEAD").stdout.strip()
        now_index = git(wt, "write-tree").stdout.strip()
        return (saved["head"], saved["index"], ck) != (now_head, now_index, now_ck)

    def _drill(self, class_name, tamper_fn):
        wt = self.tmp / class_name / "wt"
        rd = self.tmp / class_name / "round-1"
        wt.mkdir(parents=True)
        git(wt, "init", "-q", "-b", "main")
        (wt / "README.md").write_text("base\n")
        git(wt, "-c", "user.name=t", "-c", "user.email=t@l", "add", "-A")
        git(wt, "-c", "user.name=t", "-c", "user.email=t@l", "commit", "-q", "-m", "base")
        (wt / "impl-a.ts").write_text("export const a = 1;\n")
        (wt / "impl-b.ts").write_text("export const b = 2;\n")
        tracked_paths = ["README.md", "impl-a.ts", "impl-b.ts"]
        before = sha256_manifest(wt, tracked_paths)
        before_head = git(wt, "rev-parse", "HEAD").stdout.strip()

        ck = self._checkpoint(wt, rd)
        saved = {
            "head": before_head,
            "index": git(wt, "write-tree").stdout.strip(),
            "checkpoint": ck,
        }

        tamper_fn(wt)

        detected = self._recompute_and_detect(wt, rd, ck, saved)
        self.assertTrue(detected, f"{class_name}: tamper was NOT detected")

        # Recovery: run ONLY the printed triple, in order.
        git(wt, "reset", "--soft", saved["head"])
        git(wt, "read-tree", ck)
        git(wt, "checkout-index", "-af")
        git(wt, "clean", "-fd")

        after = sha256_manifest(wt, tracked_paths)
        after_head = git(wt, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(after_head, saved["head"], f"{class_name}: HEAD diverged after restore")
        self.assertEqual(after, before, f"{class_name}: bytes diverged after restore")

        extra = [
            p.name
            for p in wt.iterdir()
            if p.name != ".git" and p.name not in {"README.md", "impl-a.ts", "impl-b.ts"}
        ]
        self.assertEqual(extra, [], f"{class_name}: leftover files after restore: {extra}")

        # And the gate's own recompute-vs-checkpoint cmp on the restored tree.
        rd2 = self.tmp / class_name / "round-1b"
        now_ck = self._checkpoint(wt, rd2)
        self.assertEqual(now_ck, ck, f"{class_name}: checkpoint tree cmp failed after restore")

    def test_edit_and_delete(self):
        def tamper(wt):
            (wt / "impl-a.ts").write_text("export const a = 999; // reviewer edited\n")
            (wt / "impl-b.ts").unlink()

        self._drill("edit-and-delete", tamper)

    def test_reviewer_commit(self):
        def tamper(wt):
            (wt / "impl-a.ts").write_text("export const a = 999; // reviewer edited then committed\n")
            git(wt, "-c", "user.name=r", "-c", "user.email=r@l", "add", "-A")
            git(wt, "-c", "user.name=r", "-c", "user.email=r@l", "commit", "-q", "-m", "reviewer commit")

        self._drill("reviewer-commit", tamper)

    def test_reviewer_added_file(self):
        def tamper(wt):
            (wt / "impl-c.ts").write_text("export const sneaky = 3;\n")

        self._drill("reviewer-added-file", tamper)


if __name__ == "__main__":
    unittest.main()
