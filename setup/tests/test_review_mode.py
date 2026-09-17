#!/usr/bin/env python3
"""What the full/verify decision must not get wrong.

Every failure here is a reviewer reading the wrong thing, and the two directions
are not symmetric. A `full` round that should have been `verify` costs money. A
`verify` round that should have been `full` costs correctness: the ledger's
finding set is checked, the candidate's new behaviour is not, and a repair that
expanded the design goes unreviewed while the round still reports Ready.

So `verify` is only ever chosen on a positive, persisted instruction --
`converge` wrote `next_mode: verify` into the previous round's decision.json
with the evidence in front of it -- and on a ledger that actually exists.
Anything ambiguous is `full`.

The base is the second half. v1 fell back to origin/main whenever the chosen
base could not be read, which turned "this base is wrong" into "review a
different diff at full price and tell nobody". Item 6 makes both failure shapes
typed stops instead.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ARCHON = Path(__file__).resolve().parents[2]
SCRIPT = ARCHON / "setup" / "review-mode.py"
PREV_HEAD = "6f56dbce0152ccaa97a958d08f4e61b6e9872985"
THIS_HEAD = "447e92ed4f01c4edb401d685c320bd94757d56d6"
BOOT_HEAD = "0b7d1e3a9c2f4d5e6a7b8c9d0e1f2a3b4c5d6e7f"


def entry(fid, severity="P1", state="applied"):
    return {"id": fid, "severity": severity, "state": state,
            "title": "a finding", "head": PREV_HEAD, "round": 1}


class ReviewMode(unittest.TestCase):
    def setUp(self):
        self.ad = Path(tempfile.mkdtemp(prefix="rm-"))
        self.addCleanup(shutil.rmtree, self.ad, ignore_errors=True)
        (self.ad / "bootstrap-head.txt").write_text(BOOT_HEAD + "\n", encoding="utf-8")

    def round_dir(self, n):
        d = self.ad / f"round-{n}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_prev(self, n=1, next_mode="verify", ledger=None, pre_head=PREV_HEAD):
        d = self.round_dir(n)
        if next_mode is not None:
            (d / "decision.json").write_text(json.dumps(
                {"result": "progressed", "next_mode": next_mode}), encoding="utf-8")
        if ledger is not None:
            (d / "ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
        if pre_head is not None:
            (d / "pre-head.txt").write_text(pre_head + "\n", encoding="utf-8")

    def write_this(self, n=2, pre_head=THIS_HEAD):
        (self.round_dir(n) / "pre-head.txt").write_text(pre_head + "\n", encoding="utf-8")

    def ask(self, n, repo=None, expect=0):
        cmd = [sys.executable, str(SCRIPT), str(self.ad), str(n)]
        if repo:
            cmd += ["--repo", str(repo)]
        r = subprocess.run(cmd, capture_output=True, encoding="utf-8")
        self.assertEqual(r.returncode, expect, r.stdout + r.stderr)
        self.assertEqual(r.stderr, "")
        return r.stdout.splitlines()

    def assertDecision(self, n, mode, base, findings=None):
        out = self.ask(n)
        expected = f"REVIEW_MODE={mode} base={base}"
        if findings is not None:
            expected += f" findings={findings}"
        self.assertEqual(out[0], expected, out)
        rd = self.ad / f"round-{n}"
        self.assertEqual((rd / "review-scope.txt").read_text(encoding="utf-8"), mode + "\n")
        self.assertEqual((rd / "review-base.txt").read_text(encoding="utf-8"), base + "\n")

    # --- mode selection ---------------------------------------------------

    def test_round_1_has_nothing_to_verify(self):
        self.assertDecision(1, "full", BOOT_HEAD)

    def test_converge_asking_for_verify_gets_verify_over_the_previous_pre_head(self):
        self.write_prev(next_mode="verify", ledger=[entry("aaaa")])
        self.write_this()
        self.assertDecision(2, "verify", PREV_HEAD, findings=1)
        written = json.loads((self.ad / "round-2" / "review-findings.json").read_text())
        self.assertEqual([e["id"] for e in written], ["aaaa"])

    def test_converge_asking_for_full_after_a_p0_gets_full(self):
        # converge row 5: this round's fixer applied a P0, so the next round
        # rediscovers. The decision is made there, with the fixer result in
        # front of it, and persisted; re-deriving it here is how v1 got it wrong.
        self.write_prev(next_mode="full", ledger=[entry("aaaa", "P0")])
        self.write_this()
        self.assertDecision(2, "full", BOOT_HEAD)

    def test_converge_asking_for_full_after_a_design_expanding_repair_gets_full(self):
        self.write_prev(next_mode="full",
                        ledger=[dict(entry("aaaa"), design_expanded=True)])
        self.write_this()
        self.assertDecision(2, "full", BOOT_HEAD)

    def test_no_decision_file_is_full(self):
        self.write_prev(next_mode=None, ledger=[entry("aaaa")])
        self.write_this()
        self.assertDecision(2, "full", BOOT_HEAD)

    def test_an_unreadable_decision_is_full(self):
        for name, body in (("invalid json", "{not json"),
                           ("no next_mode", json.dumps({"result": "progressed"})),
                           ("unknown mode", json.dumps({"next_mode": "delta"}))):
            with self.subTest(name):
                shutil.rmtree(self.ad, ignore_errors=True)
                self.ad.mkdir(parents=True, exist_ok=True)
                (self.ad / "bootstrap-head.txt").write_text(BOOT_HEAD + "\n")
                self.round_dir(1)
                (self.ad / "round-1" / "decision.json").write_text(body, encoding="utf-8")
                (self.ad / "round-1" / "ledger.json").write_text(json.dumps([entry("aaaa")]))
                (self.ad / "round-1" / "pre-head.txt").write_text(PREV_HEAD + "\n")
                self.write_this()
                self.assertDecision(2, "full", BOOT_HEAD)

    def test_verify_without_a_ledger_falls_back_to_full(self):
        # `verify` is "check this finite set". An absent or empty set is not a
        # cheap round, it is a round that verifies nothing and reports Ready.
        for name, ledger in (("no ledger", None), ("empty ledger", []),
                             ("not a list", {"entries": []})):
            with self.subTest(name):
                shutil.rmtree(self.ad, ignore_errors=True)
                self.ad.mkdir(parents=True, exist_ok=True)
                (self.ad / "bootstrap-head.txt").write_text(BOOT_HEAD + "\n")
                self.write_prev(next_mode="verify", ledger=ledger)
                self.write_this()
                self.assertDecision(2, "full", BOOT_HEAD)
                self.assertFalse((self.ad / "round-2" / "review-findings.json").exists())

    def test_verify_with_an_unmoved_head_has_no_repair_to_verify(self):
        # Nothing was committed since the previous round, so the repair diff is
        # empty and a verify round would verify the same tree it already saw.
        self.write_prev(next_mode="verify", ledger=[entry("aaaa")])
        self.write_this(pre_head=PREV_HEAD)
        self.assertDecision(2, "full", BOOT_HEAD)

    def test_verify_without_a_previous_pre_head_falls_back_to_full(self):
        self.write_prev(next_mode="verify", ledger=[entry("aaaa")], pre_head=None)
        self.write_this()
        self.assertDecision(2, "full", BOOT_HEAD)

    def test_a_non_integer_round_reads_as_round_1(self):
        out = self.ask("N")
        self.assertEqual(out[0], f"REVIEW_MODE=full base={BOOT_HEAD}")

    def test_no_artifacts_dir_writes_nothing_into_the_working_directory(self):
        r = subprocess.run([sys.executable, str(SCRIPT)], cwd=self.ad,
                           capture_output=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "REVIEW_MODE=full base=origin/main\n")
        self.assertEqual(list(self.ad.iterdir()), [self.ad / "bootstrap-head.txt"])

    def test_a_stacked_chain_reviews_from_the_parent_head(self):
        chain_base = "9e8d7c6b5a4f3e2d1c0b9a8f7e6d5c4b3a2f1e0d"
        (self.ad / "bootstrap-head.txt").write_text(chain_base + "\n", encoding="utf-8")
        self.assertDecision(1, "full", chain_base)

    # --- item 6: base validation ------------------------------------------

    def make_repo(self):
        repo = Path(tempfile.mkdtemp(prefix="repo-"))
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        run = lambda *a: subprocess.run(["git", "-C", str(repo), *a],
                                        capture_output=True, check=True)
        subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, check=True)
        run("config", "user.email", "t@t")
        run("config", "user.name", "t")
        shas = []
        for i in range(3):
            (repo / f"f{i}.txt").write_text(str(i))
            run("add", "-A")
            run("commit", "-qm", f"c{i}")
            shas.append(subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                       capture_output=True, text=True).stdout.strip())
        return repo, shas

    def test_a_base_that_is_not_a_commit_is_a_typed_stop(self):
        repo, shas = self.make_repo()
        (self.ad / "bootstrap-head.txt").write_text("f" * 40 + "\n", encoding="utf-8")
        out = self.ask(1, repo=repo, expect=1)
        self.assertEqual(out[0], f"REVIEW_BASE=FAIL base={'f' * 40} reason=missing")
        self.assertFalse((self.ad / "round-1" / "review-base.txt").exists())

    def test_a_base_that_is_not_an_ancestor_is_a_typed_stop(self):
        repo, shas = self.make_repo()
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "side", shas[0]],
                       capture_output=True, check=True)
        (repo / "side.txt").write_text("x")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "side"],
                       capture_output=True, check=True)
        side = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", shas[2]],
                       capture_output=True, check=True)
        (self.ad / "bootstrap-head.txt").write_text(side + "\n", encoding="utf-8")
        out = self.ask(1, repo=repo, expect=1)
        self.assertEqual(out[0], f"REVIEW_BASE=FAIL base={side} reason=not-ancestor")

    def test_a_real_ancestor_base_passes_validation(self):
        # Negative control for both stops above: a validator that rejected
        # everything would make the two tests pass for the wrong reason.
        repo, shas = self.make_repo()
        (self.ad / "bootstrap-head.txt").write_text(shas[0] + "\n", encoding="utf-8")
        out = self.ask(1, repo=repo)
        self.assertEqual(out[0], f"REVIEW_MODE=full base={shas[0]}")

    def test_origin_main_is_validated_too_and_not_used_as_an_escape_hatch(self):
        # The whole point of item 6 is that there is no unvalidated fallback. A
        # repo with no origin/main and no readable bootstrap head must stop,
        # not silently review against a ref that does not resolve.
        repo, shas = self.make_repo()
        (self.ad / "bootstrap-head.txt").write_text("", encoding="utf-8")
        out = self.ask(1, repo=repo, expect=1)
        self.assertEqual(out[0], "REVIEW_BASE=FAIL base=origin/main reason=missing")

    def test_an_upstream_ahead_of_the_candidate_is_reported_but_not_fatal(self):
        repo, shas = self.make_repo()
        subprocess.run(["git", "-C", str(repo), "branch", "-f", "origin/main", shas[2]],
                       capture_output=True, check=True)
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", shas[1]],
                       capture_output=True, check=True)
        (self.ad / "bootstrap-head.txt").write_text(shas[0] + "\n", encoding="utf-8")
        out = self.ask(1, repo=repo)
        self.assertEqual(out[0], f"REVIEW_MODE=full base={shas[0]}")
        self.assertIn("BASELINE_BEHIND upstream=1", out)

    def test_a_candidate_level_with_upstream_prints_no_baseline_line(self):
        repo, shas = self.make_repo()
        subprocess.run(["git", "-C", str(repo), "branch", "-f", "origin/main", shas[2]],
                       capture_output=True, check=True)
        (self.ad / "bootstrap-head.txt").write_text(shas[0] + "\n", encoding="utf-8")
        out = self.ask(1, repo=repo)
        self.assertEqual(len(out), 1, out)

    def test_without_a_repo_nothing_is_validated_and_nothing_is_claimed(self):
        # The helper is called from round-pre inside the worktree; a caller that
        # gives it no repo gets no validation rather than a fabricated PASS.
        (self.ad / "bootstrap-head.txt").write_text("f" * 40 + "\n", encoding="utf-8")
        out = self.ask(1)
        self.assertEqual(out[0], f"REVIEW_MODE=full base={'f' * 40}")


if __name__ == "__main__":
    unittest.main()
