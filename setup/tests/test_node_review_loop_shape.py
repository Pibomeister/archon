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
    "round-pre": ("pre",),
    "review-gate": ("gate",),
    "fix-plan": ("fix-plan",),
    "commit-fixer": ("commit-fixer",),
}
# Nodes whose stdout a sibling's `when:` parses as a bare JSON object.
JSON_ONLY = ("round-pre", "fix-plan")


_SUBST = re.compile(r"\$\([^()]*\)")


def _emitting(line):
    """`line` with comments and command substitutions removed.

    An `echo` inside `$( ... )` writes to a subshell's stdout, which the caller
    captures; an `echo` in a comment writes nothing. Neither reaches the node's
    stdout, and a naive search for the word matched both -- reporting three
    findings in a body that emits nothing, which is the same defect as reporting
    none in a body that does.
    """
    line = line.split("#", 1)[0]
    while True:
        stripped = _SUBST.sub("", line)
        if stripped == line:
            return line
        line = stripped


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
                    # An echo whose line also exits non-zero is fine: the engine
                    # never evaluates a failed node's `when:`, so a typed stop on
                    # stdout cannot corrupt a decision that will not be read. Any
                    # OTHER write to stdout can, and must not be here. Matching
                    # `echo` anywhere in the line, not just at its start, because
                    # the two typed stops in these bodies live inside a `case` arm
                    # and an `if`, and a start-anchored match walked straight past
                    # them while claiming to have checked.
                    bare = [ln.strip() for ln in body.splitlines()
                            if re.search(r"\b(echo|printf)\b", _emitting(ln))
                            and ">&2" not in ln and ">>" not in ln and '> "' not in ln
                            and "exit 1" not in ln]
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
                    # Subcommand first, artifacts second: round-state.py uses
                    # argparse subparsers, so the order is not negotiable.
                    calls = re.findall(
                        re.escape(HELPER) + r' ([a-z-]+) "\$ARTIFACTS_DIR"', body)
                    # review-gate calls it twice on purpose and exclusively:
                    # once with --fail to record a rejected envelope, once plain
                    # for the real gate. What must hold is that every call on
                    # this node names THIS node's subcommand -- a body that
                    # reaches into another node's state machine is the bug.
                    self.assertTrue(calls, f"{nid} never calls the helper")
                    self.assertEqual(set(calls), set(subs) & set(calls),
                                     f"{nid} calls the wrong subcommand: {calls}")
                    self.assertEqual(set(calls), {subs[0]}, calls)

    def test_the_two_marked_prompts_open_and_close_on_the_helper(self):
        # The envelope and the repair-completion record are written by the AI
        # sessions themselves; a prompt that lost either call turns a finished
        # invocation into an interrupted one and the next round pays for it again.
        for lane in LANES:
            _, nodes, _ = loop_nodes(lane)
            for nid, start, done in (
                ("review", 'mark "$ARTIFACTS_DIR" review-start',
                           'mark "$ARTIFACTS_DIR" review-done'),
                ("fixer", 'mark "$ARTIFACTS_DIR" repair-start',
                          'mark "$ARTIFACTS_DIR" repair-done')):
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

    def test_both_json_nodes_type_their_exits(self):
        """A zero exit must print a PASS-class line and a non-zero exit a
        FAIL-class one, or RUNBOOK cannot route it.

        These two nodes are the ones at risk: their stdout is a bare JSON object,
        which is not a typed line at all, so the ONLY thing that can type them is
        what they put on stderr. Run for real rather than asserted from the body,
        because the line that types them comes from round-state.py, not from any
        text this file can read.
        """
        helper = ARCHON / HELPER
        if not helper.is_file():
            self.skipTest("round-state.py is not in this tree yet")
        import json as _json
        import os
        import subprocess
        import tempfile
        from nodes.extract import runnable_body
        from nodes.runner import classify, _typed_lines

        for lane in ("full-sdlc-api", "full-sdlc-api-lite"):
            tmp = Path(tempfile.mkdtemp(prefix="jsonnode-"))
            ad, wt = tmp / "ad", tmp / "wt"
            ad.mkdir(); wt.mkdir()
            subprocess.run(
                "git init -q && git config user.email t@t && git config user.name t"
                " && echo a > a && git add . && git commit -qm base",
                cwd=wt, shell=True, check=True, capture_output=True)
            head = subprocess.run("git rev-parse HEAD", cwd=wt, shell=True,
                                  capture_output=True, encoding="utf-8").stdout
            (ad / "params.json").write_text(_json.dumps(
                {"spec": "/x.md", "slug": "x", "branch": "archon/x", "worktree": str(wt)}))
            (ad / "files-allowlist.json").write_text('["a"]')
            (ad / "bootstrap-head.txt").write_text(head)
            (ad / "plan.md").write_text("# plan\n")
            env = {**os.environ, "ARTIFACTS_DIR": str(ad)}
            for node in ("round-pre", "fix-plan"):
                r = subprocess.run(["bash", "-c", runnable_body(lane, node)],
                                   capture_output=True, encoding="utf-8", env=env)
                has_pass, has_fail = classify(
                    _typed_lines((r.stdout or "") + (r.stderr or "")))
                with self.subTest(lane=lane, node=node):
                    # The JSON line itself must still parse: an unparseable
                    # `when:` skips its node while the run reports SUCCESS.
                    _json.loads(r.stdout.strip())
                    if r.returncode == 0:
                        self.assertTrue(
                            has_pass,
                            f"{node} exited 0 with no PASS-class line; typed lines "
                            f"were {_typed_lines((r.stdout or '') + (r.stderr or ''))}")
                    else:
                        self.assertTrue(has_fail, f"{node} exited {r.returncode} untyped")

    def test_the_lite_overlay_differs_only_in_the_cap(self):
        # The lite overlay replaces round-pre WHOLESALE, which is the one place
        # this lane can silently stop emitting the JSON line the inherited
        # `review` node's `when:` reads, or grow a second implementation of
        # reuse, identity and base validation. It does neither: it seeds the
        # one-round cap and then calls the same `pre` the parent calls.
        lite = loop_nodes("full-sdlc-api-lite")[1]["round-pre"]["bash"]
        parent = loop_nodes("full-sdlc-api")[1]["round-pre"]["bash"]
        # Both keep the durable cap in the node -- round.txt is the only bound
        # that survives a resume, since a loop_group re-enters with a fresh
        # iteration counter. The overlay exists for the DEFAULT, nothing else.
        self.assertIn('round-cap.txt" || echo 1 >', lite)
        self.assertIn("CAP=1", lite.replace('echo 1)', 'CAP=1)'))
        self.assertIn("|| echo 4)", parent)
        self.assertNotIn("|| echo 4)", lite)
        for body in (lite, parent):
            self.assertIn('round-state.py pre "$ARTIFACTS_DIR"', body)
            self.assertIn("ROUND_CAP_REACHED", body)


if __name__ == "__main__":
    unittest.main()
