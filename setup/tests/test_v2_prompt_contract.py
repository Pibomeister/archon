#!/usr/bin/env python3
"""The prompt-side contract of the v2 review loop, on the lanes that run it.

Three of item 1/3/4's guarantees are enforced only by prompt text, which means
nothing else in the suite can see them go missing:

  * the planner records a pin's COMPLETE bullet text, its spec line, and whether
    the spec itself allows an exception. v1 recorded a summary instead, and four
    review rounds were then spent on code the pin had already forbidden, because
    the part of the rule that got dropped was the part that forbade it;
  * the fixer applies P0/P1 always and P2 only when the fix adds no method, no
    exported symbol and no call into a pinned symbol, flags a design-expanding
    repair so the next round is a full review rather than a verification pass,
    and files a P0/P1-versus-pin deadlock in its own partition instead of
    burying it in advisory;
  * the reviewer is read-only.

`lane-doctrine.lock.json` freezes only what the lanes ALREADY share, so it
cannot see a contract that was never landed on the other lanes — and these are
deliberately api-only. Hence a test per fact, named by lane.

The companion assertion is the negative one: the fixer's result-shape line is
shared doctrine across all five lanes, so the pin_conflict key had to be ADDED
beside it rather than edited into it. If a later change rewrites that line on
this lane alone, doctrine fails — and this test says why it must not.
"""
import unittest
from pathlib import Path

import yaml

ARCHON = Path(__file__).resolve().parents[2]
# The lanes that run the v2 loop. bugfix and full-sdlc-web still run v1.
V2_LANES = ("full-sdlc-api", "full-sdlc-api-lite", "full-sdlc-api-codex")
SHARED_RESULT_SHAPE = ('{"applied": [...], "failed": [...], "advisory": [...], '
                       '"incomplete": [...], "cross_repo": [...]}')


def _walk(nodes):
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        yield n
        lg = n.get("loop_group")
        if isinstance(lg, dict):
            yield from _walk(lg.get("nodes"))


def prompt(lane, node_id):
    doc = yaml.safe_load((ARCHON / "workflows" / f"{lane}.yaml").read_text(encoding="utf-8"))
    for n in _walk(doc["nodes"]):
        if n["id"] == node_id:
            return n.get("prompt") or ""
    raise AssertionError(f"{lane}: no node {node_id}")


def flat(text):
    """Line wrapping in a YAML block scalar is not part of the contract."""
    return " ".join(text.split())


class PlannerPinProvenance(unittest.TestCase):
    def test_every_pin_entry_carries_its_provenance(self):
        for lane in ("full-sdlc-api", "full-sdlc-api-codex"):
            with self.subTest(lane=lane):
                p = flat(prompt(lane, "ralplan"))
                self.assertIn('"spec_line"', p)
                self.assertIn('"allowed_change"', p)

    def test_the_rule_must_be_the_complete_bullet(self):
        for lane in ("full-sdlc-api", "full-sdlc-api-codex"):
            with self.subTest(lane=lane):
                p = flat(prompt(lane, "ralplan"))
                self.assertIn("COMPLETE bullet text", p)
                self.assertIn("copied verbatim, not a paraphrase", p)

    def test_allowed_change_defaults_to_none(self):
        for lane in ("full-sdlc-api", "full-sdlc-api-codex"):
            with self.subTest(lane=lane):
                p = flat(prompt(lane, "ralplan"))
                self.assertIn('"allowed_change" is "none" unless the spec states an exception', p)


class FixerDiscipline(unittest.TestCase):
    def test_p0_and_p1_are_always_applied_and_p2_is_gated(self):
        for lane in V2_LANES:
            with self.subTest(lane=lane):
                p = flat(prompt(lane, "fixer"))
                self.assertIn("P0 and P1 are ALWAYS applied", p)
                self.assertIn('P2 is applied ONLY when the finding carries a "suggested_fix"', p)
                self.assertIn("adds no method, no exported symbol, and no call into a pinned symbol", p)
                self.assertIn('action starting "Deferred:"', p)

    def test_a_design_expanding_repair_is_declared(self):
        for lane in V2_LANES:
            with self.subTest(lane=lane):
                p = flat(prompt(lane, "fixer"))
                self.assertIn('"design_expanded": true', p)
                self.assertIn("costs the next round a full review", p)

    def test_a_pin_deadlock_gets_its_own_partition(self):
        for lane in V2_LANES:
            with self.subTest(lane=lane):
                p = flat(prompt(lane, "fixer"))
                self.assertIn('"pin_conflict", entries {"finding", "action", "symbol", "severity"}', p)
                # Explicitly NOT advisory and NOT failed: both were wrong homes.
                self.assertIn("It is not advisory", p)
                self.assertIn('it is not "failed"', p)

    def test_an_allowed_change_is_not_a_conflict(self):
        for lane in V2_LANES:
            with self.subTest(lane=lane):
                p = flat(prompt(lane, "fixer"))
                self.assertIn('A pinned symbol whose "allowed_change" names an exception is NOT a conflict', p)

    def test_every_entry_carries_the_reviewers_finding_id(self):
        # Without it a repair mints a NEW ledger entry instead of moving the
        # reviewer's to applied, and a finding that never reaches applied can
        # never reach closed -- so positive closure deadlocks on findings that
        # were in fact fixed. Measured on a replay of v1's api run: 36 ledger
        # entries from a much smaller real population, three repaired P1s
        # unclosed at the cap.
        for lane in V2_LANES:
            with self.subTest(lane=lane):
                p = flat(prompt(lane, "fixer"))
                self.assertIn('EVERY entry, in EVERY partition, also carries "finding_id" '
                              'copied VERBATIM', p)

    def test_the_shared_result_shape_line_was_not_edited(self):
        # Doctrine across all five lanes. pin_conflict is declared BESIDE it, not
        # inside it; rewriting it here would drift this lane from bugfix and
        # full-sdlc-web and fail lane-doctrine.py check.
        for lane in V2_LANES + ("bugfix", "full-sdlc-web"):
            with self.subTest(lane=lane):
                self.assertIn(SHARED_RESULT_SHAPE, prompt(lane, "fixer"))


class ReviewerIsReadOnly(unittest.TestCase):
    def test_the_reviewer_is_told_it_may_not_write(self):
        for lane in V2_LANES:
            with self.subTest(lane=lane):
                p = flat(prompt(lane, "review"))
                self.assertIn("YOU ARE READ-ONLY", p)
                self.assertIn("do not commit", p)

    def test_the_reviewer_is_told_the_guard_exists(self):
        # Stating the rule without the consequence is how a prompt rule becomes
        # advisory. The tree snapshot is what actually enforces it.
        for lane in V2_LANES:
            with self.subTest(lane=lane):
                p = flat(prompt(lane, "review"))
                self.assertIn("takes a tree snapshot before you start", p)


if __name__ == "__main__":
    unittest.main()
