#!/usr/bin/env python3
"""The lane's prompts and setup/prompts/*.md must not drift apart.

The engine reads the YAML, so the prompt has to live there; the FILE is what
gets edited and reviewed. That is two copies of one text, and the failure is
silent in the worst direction: an edited prompt file looks landed, review reads
it as landed, and the lane keeps running the old words for the rest of the run.

`embed-prompts.py --check` is the mechanical answer and this is what makes it
run. Also asserted here: the review mode is named in exactly one place, so
flipping the measured winner cannot half-land.
"""
import re
import subprocess
import sys
import unittest
from pathlib import Path

import yaml

ARCHON = Path(__file__).resolve().parents[2]
EMBED = ARCHON / "setup/embed-prompts.py"
PROMPTS = ARCHON / "setup/prompts"
DERIVED = ("full-sdlc-api-lite", "full-sdlc-api-codex")


def _walk(nodes):
    for n in nodes or []:
        yield n
        lg = n.get("loop_group")
        if isinstance(lg, dict):
            yield from _walk(lg.get("nodes"))


def prompt(lane, node_id, default=None):
    doc = yaml.safe_load((ARCHON / "workflows" / f"{lane}.yaml").read_text(encoding="utf-8"))
    for n in _walk(doc["nodes"]):
        if n["id"] == node_id:
            return n["prompt"]
    if default is not None:
        return default
    raise AssertionError(f"{lane} has no node {node_id}")


def embed(*args):
    return subprocess.run([sys.executable, str(EMBED), *args],
                          capture_output=True, text=True)


class PromptEmbedding(unittest.TestCase):
    def test_the_lane_matches_the_prompt_files(self):
        r = embed("--check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("PROMPT_DRIFT=OK", r.stdout)

    def test_an_edited_prompt_file_that_was_not_re_embedded_fails(self):
        # The negative control, and the only reason the assertion above is
        # evidence. Edit a prompt file, do not re-embed, require the check to
        # notice -- then put it back.
        target = PROMPTS / "review-ce.md"
        before = target.read_text(encoding="utf-8")
        try:
            target.write_text(before + "\nAn edit nobody re-embedded.\n", encoding="utf-8")
            r = embed("--check")
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("PROMPT_DRIFT=FAIL", r.stdout)
        finally:
            target.write_text(before, encoding="utf-8")
        self.assertEqual(embed("--check").returncode, 0)

    def test_the_review_mode_is_named_in_exactly_one_place(self):
        # Flipping the winner is one line. Two places to change is how a flip
        # half-lands and the lane runs one mode while the manifest claims another.
        sources = [p for p in (ARCHON / "setup").glob("*.py")]
        sources += [p for p in (ARCHON / "workflows").glob("*.yaml")]
        hits = []
        for p in sources:
            for m in re.finditer(r'^REVIEW_MODE\s*=\s*"(\w+)"', p.read_text(encoding="utf-8"), re.M):
                hits.append((p.name, m.group(1)))
        self.assertEqual(len(hits), 1, f"REVIEW_MODE assigned in {hits}")
        self.assertEqual(hits[0][0], "embed-prompts.py")

    def test_the_embedded_mode_is_the_mode_the_lane_runs(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("embed_prompts", EMBED)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertIn(f"mode:{mod.REVIEW_MODE}", prompt("full-sdlc-api", "review"))

    def test_no_placeholder_survives_into_the_lane_or_the_read_at_runtime_file(self):
        # `{{SETUP}}` is resolved at embed time. One left in the YAML reaches the
        # model as literal text and it has no way to resolve it.
        for lane in ("full-sdlc-api",) + DERIVED:
            for node in ("review", "docreview"):
                # The lite lane routes planning through lite-envelope and has no
                # docreview node at all; "" is the honest reading of absent.
                with self.subTest(lane=lane, node=node):
                    self.assertNotIn("{{", prompt(lane, node, default=""))
        self.assertNotIn("{{", (PROMPTS / "review-verify.md").read_text(encoding="utf-8"))

    def test_the_derived_lanes_carry_the_embedded_review_prompt(self):
        # derive-lite and derive-codex run AFTER the embed; a lane that missed
        # the regeneration would still be running the previous mode. lite is a
        # byte copy; codex is NOT -- derive-codex.py rewrites the skill
        # invocation for the .agents runtime -- so that one is checked by
        # content it must carry rather than by equality.
        parent = prompt("full-sdlc-api", "review")
        self.assertEqual(prompt("full-sdlc-api-lite", "review"), parent)
        codex = prompt("full-sdlc-api-codex", "review")
        for required in ("YOU ARE READ-ONLY", 'mark "$ARTIFACTS_DIR" review-start',
                         "review-scope.txt"):
            self.assertIn(required, codex)

    def test_every_round_state_call_puts_the_subcommand_first(self):
        """round-state.py is argparse subparsers: the subcommand comes first.

        `round-state.py "$ARTIFACTS_DIR" mark review-start` parses as subcommand
        `$ARTIFACTS_DIR`, which does not exist, so it exits 2 having done
        nothing. It reads perfectly plausibly and it has been written the wrong
        way round in four separate prompt files by three different people, so it
        gets a grep rather than a convention. Checked in the prompt sources AND
        in the lane, because the lane is what runs.
        """
        bad = []
        sources = list(PROMPTS.glob("*.md"))
        for lane in ("full-sdlc-api",) + DERIVED:
            for node in ("review", "fixer"):
                body = prompt(lane, node, default="")
                for line in body.splitlines():
                    if "round-state.py" in line and not re.search(
                            r"round-state\.py (pre|pre-lite|gate|fix-plan|commit-fixer"
                            r"|converge|exit-check|mark|reject-review|id)\b", line):
                        bad.append(f"{lane}:{node}: {line.strip()[:90]}")
        for f in sources:
            for line in f.read_text(encoding="utf-8").splitlines():
                if "round-state.py" in line and not re.search(
                        r"round-state\.py (pre|pre-lite|gate|fix-plan|commit-fixer"
                        r"|converge|exit-check|mark|reject-review|id)\b", line):
                    bad.append(f"{f.name}: {line.strip()[:90]}")
        self.assertEqual(bad, [], "round-state.py called with the artifacts dir first")

    def test_negative_control_the_wrong_order_is_caught(self):
        # The grep above passes trivially if its pattern is wrong. This is the
        # exact shape that shipped, and it must not match a known subcommand.
        import re as _re
        wrong = 'python3 {{SETUP}}/round-state.py "$ARTIFACTS_DIR" mark review-start'
        self.assertIsNone(_re.search(
            r"round-state\.py (pre|pre-lite|gate|fix-plan|commit-fixer"
            r"|converge|exit-check|mark|reject-review|id)\b", wrong))

    def test_the_mode_independent_preamble_survives_any_mode(self):
        # It belongs to item 1's execution contract, not to a topology, so it is
        # prepended by the embed rather than living in the mode files -- where a
        # swap would drop it. This is what that regression looked like when it
        # happened: the marks and the read-only rule vanished with the swap.
        body = prompt("full-sdlc-api", "review")
        for required in ('mark "$ARTIFACTS_DIR" review-start',
                         'mark "$ARTIFACTS_DIR" review-done',
                         "YOU ARE READ-ONLY",
                         "review-scope.txt",
                         "prompts/review-verify.md"):
            self.assertIn(required, body)


if __name__ == "__main__":
    unittest.main()
