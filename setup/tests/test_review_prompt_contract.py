#!/usr/bin/env python3
"""The review-loop prompt contract, asserted per lane by name.

lane-doctrine.lock.json freezes only the lines the lanes ALREADY agree on, so it
cannot notice a contract that was never landed everywhere in the first place.
These are the text facts A1, A2 and A5 add: the reviewer reads the JSON waiver
ledger, the fixer emits severities and a cross_repo partition, and a plan that
declares joint-plan.json also declares acceptance criteria and their coverage.
A reverted lane is named by the failing subTest.
"""
import unittest
from pathlib import Path

import yaml

ARCHON = Path(__file__).resolve().parent.parent.parent
LANES = ("full-sdlc-api", "bugfix", "full-sdlc-web", "full-sdlc-api-lite", "bugfix-lite")
OVERLAYS = sorted((ARCHON / "setup" / "lite").glob("*/review-loop.review.prompt.md"))


def _walk(nodes):
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        yield n
        for key in ("loop_group", "body"):
            v = n.get(key)
            if isinstance(v, dict):
                yield from _walk(v.get("nodes"))
            elif isinstance(v, list):
                yield from _walk(v)


def prompt(lane, node_id):
    doc = yaml.safe_load((ARCHON / "workflows" / f"{lane}.yaml").read_text(encoding="utf-8"))
    for n in _walk(doc.get("nodes")):
        if n.get("id") == node_id and n.get("prompt"):
            return n["prompt"]
    return None


class ReviewPromptContract(unittest.TestCase):
    def test_every_review_prompt_reads_the_json_ledger(self):
        for lane in LANES:
            with self.subTest(lane=lane):
                p = prompt(lane, "review")
                self.assertIsNotNone(p, f"{lane} declares no review prompt")
                self.assertIn("waivers.json", p)
                self.assertNotIn("waivers.md", p)

    def test_every_prbody_appends_the_filed_cross_repo_section(self):
        overlays = sorted((ARCHON / "setup" / "lite").glob("*/prbody.prompt.md"))
        sources = [(lane, prompt(lane, "prbody")) for lane in LANES]
        sources += [(str(f.relative_to(ARCHON)), f.read_text(encoding="utf-8")) for f in overlays]
        for name, text in sources:
            with self.subTest(source=name):
                self.assertIsNotNone(text, f"{name} declares no prbody prompt")
                self.assertIn('/.archon/setup/cross-repo-keys.py "$ARTIFACTS_DIR" --prbody', text)
                self.assertIn("## Cross-repo findings filed", text)

    def test_every_fixer_prompt_forbids_writing_the_acknowledgement(self):
        for lane in LANES:
            with self.subTest(lane=lane):
                self.assertIn("Never create or edit cross-repo-filed.json.", prompt(lane, "fixer"))

    def test_every_review_overlay_reads_the_json_ledger(self):
        self.assertTrue(OVERLAYS, "no lite review overlay found")
        for f in OVERLAYS:
            with self.subTest(overlay=str(f.relative_to(ARCHON))):
                t = f.read_text(encoding="utf-8")
                self.assertIn("waivers.json", t)
                self.assertNotIn("waivers.md", t)

    def test_every_fixer_prompt_declares_severity_and_cross_repo(self):
        for lane in LANES:
            with self.subTest(lane=lane):
                p = prompt(lane, "fixer")
                self.assertIsNotNone(p, f"{lane} declares no fixer prompt")
                self.assertIn('"severity": "P0|P1|P2|P3"', p)
                self.assertIn('"cross_repo": [...]', p)
                self.assertIn("producer_repo", p)

    def test_a_joint_plan_lane_declares_acceptance_coverage(self):
        # The lite overlay's ralplan writes no joint-plan.json at all (its own
        # triage routes a multi-repo ticket to the full lane), so the rule is
        # asserted on the lanes that actually emit that artifact.
        joint = [lane for lane in LANES
                 if (prompt(lane, "ralplan") or "").find("joint-plan.json") >= 0]
        self.assertTrue(joint, "no lane declares joint-plan.json in its ralplan prompt")
        for lane in joint:
            with self.subTest(lane=lane):
                p = prompt(lane, "ralplan")
                self.assertIn('"acceptance_criteria"', p)
                self.assertIn('"covers"', p)


if __name__ == "__main__":
    unittest.main()
