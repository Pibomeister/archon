#!/usr/bin/env python3
"""`skipped` must mean "nothing to fetch", never "needed and unreachable".

The intake extracts linear_refs precisely because those tickets are relevant to
the report. Recording an unset LINEAR_API_KEY as `linear=skipped` retires the
run's own question silently, and every downstream reader treats skipped as
"there was nothing there".

ENG-3860 named four related tickets. One of them, ENG-2091 ("CSV import notes
showing job title instead of name of who they are about"), was already closed
for the same symptom family. Six runs extracted all five refs into
evidence-plan.json, read none of them, logged `linear=skipped`, and re-derived
the prior art from source code -- twice producing a mechanism production then
refuted."""
import unittest
from pathlib import Path

import yaml

ARCHON = Path(__file__).resolve().parent.parent.parent
LANES = ["bugfix", "bugfix-codex"]


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


def cheap(lane):
    doc = yaml.safe_load((ARCHON / "workflows" / f"{lane}.yaml").read_text(encoding="utf-8"))
    got = [n["bash"] for n in walk(doc.get("nodes")) if n.get("id") == "evidence-cheap"]
    assert len(got) == 1, f"{lane}: expected one evidence-cheap"
    return got[0]


class PriorArtNotSilentlySkipped(unittest.TestCase):
    def test_the_probe_finds_the_node(self):
        for lane in LANES:
            self.assertIn("linear=", cheap(lane), f"{lane}: no linear status recorded")

    def test_unreadable_refs_are_not_recorded_as_skipped(self):
        for lane in LANES:
            bash = cheap(lane)
            self.assertIn("prior-art-unread", bash,
                          f"{lane}: an unset key with refs present is still logged as skipped")
            self.assertIn("LINEAR=unavailable", bash,
                          f"{lane}: the status is not corrected to unavailable")

    def test_the_unread_ticket_ids_are_named(self):
        # A degraded line that does not say WHICH tickets went unread cannot be
        # acted on: the operator has to go find them.
        for lane in LANES:
            self.assertIn("refs=$UNREAD", cheap(lane), lane)

    def test_it_only_fires_when_there_was_something_to_read(self):
        # Negative control on over-blocking: a report naming no related tickets
        # must keep recording skipped, not manufacture a degraded state. This is
        # the guard against the correction becoming noise on every run.
        for lane in LANES:
            self.assertIn('[ -n "$UNREAD" ]', cheap(lane),
                          f"{lane}: fires even when there is no prior art to miss")
            self.assertIn('[ "$LINEAR" = skipped ]', cheap(lane),
                          f"{lane}: overwrites a status that was not skipped")


if __name__ == "__main__":
    unittest.main()
