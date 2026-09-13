#!/usr/bin/env python3
"""`incomplete` must mean "a retry could clear this".

The loop cannot converge while any incomplete entry stands, and no round can
clear a blocker that is not transient. Run 38d72218 round 4 filed one: the
fixer extracted a shared helper, verified 262 tests green, and could not land
it because the repo's whole-file oxlint complexity hook failed on four
PRE-EXISTING violations in the same file. Its own action text said remediating
those was "out of this finding's scope" -- which is the decline criterion
verbatim -- but the contract's word "attempted but not landed" fit too, so it
chose the bucket that deadlocks the loop. The run then hit its cap on a finding
no round was ever able to fix."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ARCHON = Path(__file__).resolve().parent.parent.parent
CHECK_FIXER_RESULT = ARCHON / "setup" / "check-fixer-result.py"
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


def flat(text):
    """Prompts are block scalars at several indent depths; compare on words."""
    return " ".join(text.split())


def fixer_prompts(lane):
    """Only prompts that DEFINE the bucket. "Declining is not failing" is the
    fixer contract's signature line; prbody merely reads the field."""
    doc = yaml.safe_load((ARCHON / "workflows" / f"{lane}.yaml").read_text(encoding="utf-8"))
    return [(n["id"], flat(n["prompt"])) for n in walk(doc.get("nodes"))
            if n.get("prompt") and "Declining is not failing" in n["prompt"]]


class IncompleteContract(unittest.TestCase):
    def test_the_probe_finds_the_contract_in_every_lane(self):
        for lane in LANES:
            self.assertTrue(fixer_prompts(lane), f"{lane}: no prompt defines the incomplete bucket")

    def test_every_lane_requires_a_retry_to_be_able_to_succeed(self):
        for lane in LANES:
            for nid, prompt in fixer_prompts(lane):
                self.assertIn("a retry could plausibly succeed", prompt,
                              f"{lane}/{nid}: incomplete is not conditioned on a retry succeeding")

    def test_every_lane_routes_a_permanent_blocker_to_advisory(self):
        for lane in LANES:
            for nid, prompt in fixer_prompts(lane):
                self.assertIn("CANNOT change on a retry", prompt,
                              f"{lane}/{nid}: no rule for a blocker a retry cannot clear")
                self.assertIn("deadlocks the loop", prompt,
                              f"{lane}/{nid}: does not say why a permanent incomplete is fatal")

    def test_the_named_deadlock_causes_are_the_ones_observed(self):
        # A rule with no examples is a rule nobody applies. These three are the
        # shapes that have actually reached the bucket.
        for lane in LANES:
            for nid, prompt in fixer_prompts(lane):
                self.assertIn("pre-existing violations", prompt, f"{lane}/{nid}")
                self.assertIn("outside this diff's scope", prompt, f"{lane}/{nid}")


class CrossRepoPartition(unittest.TestCase):
    """B3: check-fixer-result.py accepts an optional cross_repo list."""

    def run_check(self, obj):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(obj, f)
            path = f.name
        try:
            return subprocess.run(["python3", str(CHECK_FIXER_RESULT), path],
                                   capture_output=True, encoding="utf-8")
        finally:
            Path(path).unlink()

    def test_valid_cross_repo_entry_passes_and_is_counted(self):
        r = self.run_check({
            "applied": [], "failed": [], "advisory": [],
            "cross_repo": [{"finding": "f1", "action": "a1", "producer_repo": "web-app"}],
        })
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("CROSS_REPO=1", r.stdout)

    def test_entry_missing_producer_repo_blocks(self):
        r = self.run_check({
            "applied": [], "failed": [], "advisory": [],
            "cross_repo": [{"finding": "f1", "action": "a1", "producer_repo": ""}],
        })
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("FIXER_BLOCKED: cross_repo entry missing producer_repo", r.stderr)


if __name__ == "__main__":
    unittest.main()
