#!/usr/bin/env python3
"""The one normalization that decides whether two findings are the same finding.

The waiver ledger and the yield measurement both key findings, and two
implementations of the same transform drift silently: the ledger keeps writing
entries and `reraised` keeps reading zero. One module, imported by both.

Reviewers restate a waived finding with the round's provenance appended
(" -- re-raised by project-standards this round") and with the file:line it
happens to be at this round (the same guard moved from :979 to :972 between
rounds). Neither is new evidence, so neither may mint a new ledger entry: cut
the text at the first " -- " (or its em-dash spelling) and drop a trailing
(path:line, ...) parenthetical before normalizing.
"""
import re

KEY_LEN = 80
# " -- " or " <em dash> " -- the provenance suffix reviewers append.
SUFFIX = re.compile(r"\s(?:--|—)\s")
# A trailing parenthetical that leads with a file:line, with whatever the
# reviewer listed after it ("(group.service.ts:979, getShareLink vs ...)").
LOCATION = re.compile(r"\s*\([^()]*\.[A-Za-z0-9_]+:\d+[^()]*\)\s*$")


def key_of(text):
    """Normalized finding key: cut the provenance suffix and a trailing
    file:line parenthetical, casefold, strip everything outside [a-z0-9 ],
    collapse whitespace, truncate."""
    head = LOCATION.sub("", SUFFIX.split(str(text), 1)[0])
    return " ".join(re.sub(r"[^a-z0-9\s]", "", head.casefold()).split())[:KEY_LEN]
