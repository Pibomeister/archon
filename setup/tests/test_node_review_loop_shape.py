#!/usr/bin/env python3
"""The review loop's TOPOLOGY, asserted against the shipped YAML.

Item 1's durability does not live in any one node body — it lives in the shape:
which node is conditional, which runs anyway when its upstream was skipped,
which node may emit the completion promise, and which two nodes are parsed as
JSON by a sibling's `when:`. Every one of those is a property the engine reads
and no unit test of a bash body can see, and each has a matching silent-failure
mode the RUNBOOK already records:

  * an unparseable `when:` SKIPS its node and the run still reports SUCCESS, so
    a stray human line on `round-pre`'s or `fix-plan`'s stdout does not make the
    decision wrong, it makes the node vanish;
  * a dependent of a `when:`-skipped node is itself skipped unless it carries
    `trigger_rule: all_done`, so a reused envelope would never be gated and a
    reused repair never attested;
  * a completion promise emitted from anything but the group's LAST node is not
    honoured and the group runs to `max_iterations` (probe 4).

Read from the YAML, never transcribed, and asserted on all three lanes that
carry the v2 loop (the parent plus its derived lite and codex twins) so a
regenerated derivative that lost a key fails here.
"""
import re
import unittest
from pathlib import Path

import yaml

ARCHON = Path(__file__).resolve().parents[2]
LANES = ["full-sdlc-api", "full-sdlc-api-lite", "full-sdlc-api-codex"]
HELPER = "setup/round-state.py"

# id -> (depends_on, when, trigger_rule)
EXPECTED = [
    ("round-pre", None, None, None),
    ("review", ["round-pre"], "$round-pre.review == 'run'", None),
    ("review-gate", ["review"], None, "all_done"),
    ("fix-plan", ["review-gate"], None, None),
    ("fixer", ["fix-plan"], "$fix-plan.fixer == 'run'", None),
    ("commit-fixer", ["fixer"], None, "all_done"),
    ("converge", ["commit-fixer"], None, "all_done"),
]
# The subcommand each bash node must hand the helper. `converge` is absent on
# purpose: the lite lane overlays that body with its own single-round contract,
# so it is asserted separately (see the authorization test below).
SUBCOMMAND = {
    "round-pre": ("pre", "pre-lite"),
    "review-gate": ("gate",),
    "fix-plan": ("fix-plan",),
    "commit-fixer": ("commit-fixer",),
}
# Nodes whose stdout a sibling's `when:` parses as a bare JSON object.
JSON_ONLY = ("round-pre", "fix-plan")


def loop_nodes(lane):
    doc = yaml.safe_load((ARCHON / "workflows" / f"{lane}.yaml").read_text(encoding="utf-8"))
    loop = next(n for n in doc["nodes"] if n["id"] == "review-loop")["loop_group"]
    return loop, {n["id"]: n for n in loop["nodes"]}, [n["id"] for n in loop["nodes"]]


class ReviewLoopTopology(unittest.TestCase):
    def test_the_node_list_and_order_are_the_revised_dag(self):
        for lane in LANES:
            with self.subTest(lane=lane):
                _, _, order = loop_nodes(lane)
                self.assertEqual(order, [row[0] for row in EXPECTED])

    def test_commit_fixes_is_gone(self):
        # It committed whatever the REVIEW session left behind. The reviewer is
        # read-only now and review-gate enforces that against a tree snapshot,
        # so a node that commits unreviewed edits has no remaining purpose.
        for lane in LANES:
            with self.subTest(lane=lane):
                _, _, order = loop_nodes(lane)
                self.assertNotIn("commit-fixes", order)

    def test_every_conditional_and_trigger_rule_is_present(self):
        for lane in LANES:
            _, nodes, _ = loop_nodes(lane)
            for nid, deps, when, trigger in EXPECTED:
                with self.subTest(lane=lane, node=nid):
                    self.assertEqual(nodes[nid].get("depends_on"), deps)
                    self.assertEqual(nodes[nid].get("when"), when)
                    self.assertEqual(nodes[nid].get("trigger_rule"), trigger)

    def test_the_promise_is_emitted_only_from_the_last_node(self):
        # Probe 4: a promise from any earlier node is not honoured and the group
        # runs to max_iterations instead of converging.
        for lane in LANES:
            loop, nodes, order = loop_nodes(lane)
            promise = f"<promise>{loop['until']}</promise>"
            for nid in order:
                with self.subTest(lane=lane, node=nid):
                    body = nodes[nid].get("bash", "") + nodes[nid].get("prompt", "")
                    if nid == order[-1]:
                        continue
                    self.assertNotIn(promise, body)

    def test_the_two_json_nodes_never_write_to_stdout(self):
        # Their stdout IS the `when:` expression's input. Every `echo`/`printf`
        # in these bodies must be redirected to stderr or a file; the helper call
        # is the single writer of the JSON line.
        for lane in LANES:
            _, nodes, _ = loop_nodes(lane)
            for nid in JSON_ONLY:
                with self.subTest(lane=lane, node=nid):
                    body = nodes[nid]["bash"]
                    bare = [ln.strip() for ln in body.splitlines()
                            if re.match(r"^\s*(echo|printf)\b", ln)
                            and ">&2" not in ln and ">>" not in ln and "> \"" not in ln]
                    self.assertEqual(bare, [], f"{nid} writes to stdout: {bare}")
                    self.assertNotIn("exec > >(", body,
                                     f"{nid} tees stdout, which corrupts the JSON line")

    def test_every_bash_node_calls_the_helper_with_its_subcommand(self):
        for lane in LANES:
            _, nodes, _ = loop_nodes(lane)
            for nid, subs in SUBCOMMAND.items():
                with self.subTest(lane=lane, node=nid):
                    body = nodes[nid]["bash"]
                    self.assertIn(HELPER, body)
                    calls = re.findall(
                        re.escape(HELPER) + r' "\$ARTIFACTS_DIR" ([a-z-]+)', body)
                    self.assertEqual(len(calls), 1,
                                     f"{nid} must call the helper exactly once, got {calls}")
                    self.assertIn(calls[0], subs)

    def test_the_two_marked_prompts_open_and_close_on_the_helper(self):
        # The envelope and the repair-completion record are written by the AI
        # sessions themselves; a prompt that lost either call turns a finished
        # invocation into an interrupted one and the next round pays for it again.
        for lane in LANES:
            _, nodes, _ = loop_nodes(lane)
            for nid, start, done in (("review", "mark review-start", "mark review-done"),
                                     ("fixer", "mark repair-start", "mark repair-done")):
                with self.subTest(lane=lane, node=nid):
                    prompt = nodes[nid]["prompt"]
                    self.assertIn(start, prompt)
                    self.assertIn(done, prompt)
                    self.assertLess(prompt.index(start), prompt.index(done))

    def test_every_converge_body_requires_authorization(self):
        # converge carries `all_done` so it can replay a terminal decision, which
        # also means it runs after a FAILED review-gate. v1 was safe here only by
        # accident: a failed gate skipped every node behind it, converge included.
        # review-summary.json is written before the gate's final checks and stays
        # readable with a Ready verdict, so a converge that reads only the verdict
        # would converge a round whose review was never gated. The parent checks
        # this inside the helper; the lite overlay replaces that body wholesale
        # and has to carry its own check.
        for lane in LANES:
            with self.subTest(lane=lane):
                body = loop_nodes(lane)[1]["converge"]["bash"]
                if HELPER in body:
                    continue
                self.assertIn("review.ok", body)
                self.assertIn("REVIEW_UNAUTHORIZED", body)
                self.assertIn("fixer.ok", body)

    def test_the_lite_lane_takes_the_lite_round_pre(self):
        # The lite overlay replaces round-pre wholesale, so it is the one body
        # that can silently stop emitting the JSON line the inherited `review`
        # node's `when:` reads.
        _, nodes, _ = loop_nodes("full-sdlc-api-lite")
        self.assertIn("pre-lite", nodes["round-pre"]["bash"])
        _, parent, _ = loop_nodes("full-sdlc-api")
        self.assertNotIn("pre-lite", parent["round-pre"]["bash"])


if __name__ == "__main__":
    unittest.main()
