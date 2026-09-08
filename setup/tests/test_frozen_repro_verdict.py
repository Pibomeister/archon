#!/usr/bin/env python3
"""A finding in the frozen repro file must not hold the verdict at "Not ready".

The bugfix lanes freeze the repro test at red-sha.txt and enforce it
mechanically. A P0-P2 style or coverage finding whose only site is that file is
therefore unfixable for the life of the run -- and converge stops a loop whose
verdict says not-ready while nothing changed. Run 38d72218 round 5 ended exactly
there: verdict "Not ready", both findings correctly waived (one of them a
validated blocking style rule inside the frozen repro), head_moved=NO, and
`NO_PROGRESS round=5`. No further round could have changed the outcome.

The lite lane had the sharper version of the same gap: it enforces the freeze in
commit-fixes and converge but its review prompt never mentioned it, so a
reviewer could raise a finding whose only possible fix hard-fails the round."""
import unittest
from pathlib import Path

import yaml

ARCHON = Path(__file__).resolve().parent.parent.parent
BUGFIX_LANES = ["bugfix", "bugfix-lite"]


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


def flat(text):
    return " ".join(text.split())


def review_prompt(lane):
    doc = yaml.safe_load((ARCHON / "workflows" / f"{lane}.yaml").read_text(encoding="utf-8"))
    got = [n["prompt"] for n in walk(doc.get("nodes"))
           if n.get("id") == "review" and n.get("prompt")]
    assert len(got) == 1, f"{lane}: expected one review prompt, got {len(got)}"
    return flat(got[0])


class FrozenReproVerdict(unittest.TestCase):
    def test_both_bugfix_lanes_name_the_freeze(self):
        for lane in BUGFIX_LANES:
            p = review_prompt(lane)
            self.assertIn("frozen at red-sha.txt", p, f"{lane}: reviewer is not told the repro is frozen")
            self.assertIn("leave the repro bytes untouched", p, lane)

    def test_both_bugfix_lanes_free_the_verdict(self):
        for lane in BUGFIX_LANES:
            p = review_prompt(lane)
            self.assertIn('must not hold the verdict at "Not ready" on its own', p,
                          f"{lane}: an unfixable finding can still deadlock the loop")
            self.assertIn("let the verdict reflect what THIS round could change", p, lane)

    def test_a_wrong_repro_still_blocks(self):
        # The escape hatch must not become a way to wave through a bad repro:
        # disputing the RCA is a different, still-blocking channel.
        for lane in BUGFIX_LANES:
            p = review_prompt(lane)
            self.assertIn("repro itself is WRONG is different and still blocks", p,
                          f"{lane}: downgrades a correctness dispute along with style")

    def test_the_feature_lanes_are_untouched(self):
        # Feature lanes have no repro test; the rule must not have leaked there.
        for lane in ("full-sdlc-api", "full-sdlc-web", "full-sdlc-api-lite"):
            self.assertNotIn("frozen at red-sha.txt", review_prompt(lane),
                             f"{lane}: bugfix-only doctrine leaked into a feature lane")


if __name__ == "__main__":
    unittest.main()
