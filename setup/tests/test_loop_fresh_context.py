#!/usr/bin/env python3
"""Every judging AI node inside a loop_group starts a fresh session.

archon 0.10.1 threads one session cursor through a loop_group: a body AI node
without `context: fresh` resumes the previous AI node's session, within an
iteration and (fresh_context defaults false) across iterations too. Observed
2026-09-13: one Claude transcript held review -> fixer -> review -> fixer ->
review -> fixer, so every reviewer after round 1 was reading its own edits.

The engine reads `node.context === "fresh"` (enum fresh|shared), so the
contract is pinned per node. Author nodes (fixer, reviser) may continue a
session: they work from artifacts either way. A NEW AI node in any loop body
must be classified here, or this test fails.
"""
import unittest

import yaml

from nodes.extract import WORKFLOWS

JUDGES = {"review", "deslop-review", "plan-critic", "rca-critic", "impact-probe"}
AUTHORS = {"fixer", "deslop-fix", "plan-revise", "rca-revise", "fix"}


def loop_violations(doc):
    """[(group, node, problem)] for AI nodes in loop bodies that break the contract."""
    out = []
    for group in doc.get("nodes") or []:
        body = (group.get("loop_group") or {}).get("nodes") or []
        for n in body:
            if "prompt" not in n and "command" not in n:
                continue
            if n["id"] in JUDGES:
                if n.get("context") != "fresh":
                    out.append((group["id"], n["id"], f"context={n.get('context')!r}"))
            elif n["id"] not in AUTHORS:
                out.append((group["id"], n["id"], "unclassified AI node"))
    return out


def lanes():
    return sorted(p for p in WORKFLOWS.glob("*.yaml"))


class JudgesRunFresh(unittest.TestCase):
    def test_every_lane(self):
        judged = 0
        for path in lanes():
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            with self.subTest(workflow=path.stem):
                self.assertEqual(loop_violations(doc), [])
            judged += sum(1 for g in doc.get("nodes") or []
                          for n in (g.get("loop_group") or {}).get("nodes") or []
                          if n["id"] in JUDGES)
        # parents: api 4, bugfix 4, web 1, api-lite 1, bugfix-lite 1; codex twins mirror them.
        self.assertEqual(judged, 22)

    def test_twins_keep_the_parent_fresh_set(self):
        def fresh(stem):
            doc = yaml.safe_load((WORKFLOWS / f"{stem}.yaml").read_text(encoding="utf-8"))
            return sorted((g["id"], n["id"]) for g in doc["nodes"]
                          for n in (g.get("loop_group") or {}).get("nodes") or []
                          if n.get("context") == "fresh")
        for parent in ("full-sdlc-api", "bugfix", "full-sdlc-web", "full-sdlc-api-lite", "bugfix-lite"):
            with self.subTest(parent=parent):
                self.assertTrue(fresh(parent))
                self.assertEqual(fresh(f"{parent}-codex"), fresh(parent))

    def test_negative_control_a_shared_reviewer_is_caught(self):
        doc = yaml.safe_load((WORKFLOWS / "full-sdlc-api.yaml").read_text(encoding="utf-8"))
        group = next(g for g in doc["nodes"] if g["id"] == "deslop-verify")
        next(n for n in group["loop_group"]["nodes"] if n["id"] == "deslop-review").pop("context")
        group["loop_group"]["nodes"].append({"id": "second-opinion", "prompt": "judge"})
        self.assertEqual(loop_violations(doc), [
            ("deslop-verify", "deslop-review", "context=None"),
            ("deslop-verify", "second-opinion", "unclassified AI node"),
        ])


if __name__ == "__main__":
    unittest.main()
