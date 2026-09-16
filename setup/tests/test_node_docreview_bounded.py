#!/usr/bin/env python3
"""docreview: the bounded two-role shape, and the envelope contract it must keep.

The planning stage's most expensive node was the ce-doc-review persona fan-out,
measured at 15.3 min on the v1 plan. The replacement is a prompt that dispatches
exactly one plan reviewer and exactly one validator. Two things can silently undo
that and neither is visible in a run that reports SUCCESS:

  * a prompt that grows a third role, or re-invokes the skill it replaced, is
    back to a fan-out with a smaller timeout;
  * a prompt that stops emitting `Review complete` bills the whole planning
    round and fails DOCREVIEW_GATE, which greps for exactly that string.

The prompt is read from the shipped YAML, and the gate's requirement is read
from the shipped gate body, so neither side is transcribed here.
"""
import re
import unittest
from pathlib import Path

import yaml

ARCHON = Path(__file__).resolve().parents[2]
PROMPT_FILE = ARCHON / "setup/prompts/docreview-bounded.md"


def node(nid, lane="full-sdlc-api"):
    doc = yaml.safe_load((ARCHON / "workflows" / f"{lane}.yaml").read_text(encoding="utf-8"))
    return next(n for n in doc["nodes"] if n["id"] == nid)


class DocreviewBounded(unittest.TestCase):
    def setUp(self):
        self.n = node("docreview")
        self.prompt = self.n["prompt"]

    def test_the_embedded_prompt_is_the_shipped_prompt_file(self):
        # The YAML copy is what runs; the file is what gets edited. Drift between
        # them means an edit that looks landed and is not.
        self.assertEqual(self.prompt.strip(), PROMPT_FILE.read_text().strip())

    def test_exactly_one_reviewer_and_one_validator_are_dispatched(self):
        steps = re.findall(r"^## Step \d+ — dispatch one `([a-z-]+)` subagent",
                           self.prompt, re.M)
        self.assertEqual(steps, ["plan-reviewer", "validator"])

    def test_the_skill_is_no_longer_invoked(self):
        # Staged, per preflight, but invoking it restores the fan-out this node
        # exists to remove.
        self.assertNotIn("mode:headless", self.prompt)
        self.assertNotIn("invoke the ce-doc-review skill", self.prompt)

    def test_the_validator_may_only_drop_or_downgrade(self):
        self.assertIn("drop or downgrade", self.prompt)
        self.assertIn("never add and never raise a severity",
                      " ".join(self.prompt.split()))

    def test_the_timeout_is_the_bounded_one(self):
        # 8 min. A run still going past it is stuck, not thorough.
        self.assertEqual(self.n["timeout"], 480000)

    def test_the_envelope_terminator_the_gate_greps_for_is_required(self):
        gate = node("docreview-gate")["bash"]
        terminator = re.search(r"grep -q '([^']+)'", gate).group(1)
        self.assertEqual(terminator, "Review complete")
        self.assertIn(terminator, self.prompt)

    def test_the_prompt_forbids_questions(self):
        # Unattended: a node that stops to ask burns its whole timeout.
        self.assertIn("ask no questions", self.prompt)


if __name__ == "__main__":
    unittest.main()
