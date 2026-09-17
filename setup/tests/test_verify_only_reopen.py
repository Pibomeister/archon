#!/usr/bin/env python3
"""A --verify-only feature-reopen skips only the implement node.

Chain 42b42a13 run e21573ca: feature-phase set implement=no, gate-tests depended
on implement alone, so every verification node after it was skipped, the run
"completed", and feature-advance failed on a missing feature-result.json. And
bootstrap-head.txt was the worktree HEAD, which already held the fix, so even a
running tail would have diffed nothing.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from nodes.extract import WORKFLOWS, runnable_body
from nodes.runner import _isolation_env
from test_node_stress import git, init_worktree, jdump, params, write_shims

HELPER = Path(__file__).resolve().parents[1] / "verify-only-base.py"
TAIL = ["gate-tests", "deslop", "deslop-verify", "reader-audit", "reader-audit-gate",
        "commit-impl", "review-loop", "exit-gate", "smoke", "local-candidate"]


def simulate(workflow, phase_output, nodes=None):
    """Port of archon-engine dag-executor checkTriggerRule plus `$feature-phase.X == 'Y'`
    conditions; every node that runs is assumed to complete."""
    nodes = nodes or yaml.safe_load((WORKFLOWS / f"{workflow}.yaml").read_text(encoding="utf-8"))["nodes"]
    state = {}
    for node in nodes:
        deps = [state.get(d, "failed") for d in node.get("depends_on") or []]
        rule = node.get("trigger_rule", "all_success")
        if deps and rule == "all_success":
            run = all(s == "completed" for s in deps)
        elif deps and rule == "none_failed_min_one_success":
            run = "failed" not in deps and "completed" in deps
        else:
            run = True
        cond = node.get("when")
        if run and cond:
            m = re.fullmatch(r"\$feature-phase\.(\w+) == '(\w+)'", cond)
            run = phase_output[m.group(1)] == m.group(2)
        state[node["id"]] = "completed" if run else "skipped"
    return state


def phase(name, implement):
    return {"phase": name, "plan": "yes" if name in ("planning", "legacy") else "no",
            "bootstrap": "yes" if name in ("implement", "legacy") else "no",
            "implement": implement, "integration": "yes" if name == "integration" else "no"}


class VerifyOnlyDag(unittest.TestCase):
    def test_verify_only_runs_the_whole_tail_without_implement(self):
        for lane in ("full-sdlc-api", "full-sdlc-api-codex"):
            with self.subTest(lane=lane):
                state = simulate(lane, phase("implement", "no"))
                self.assertEqual(state["implement"], "skipped")
                self.assertEqual({n: state[n] for n in TAIL}, {n: "completed" for n in TAIL})

    def test_other_phases_are_unchanged(self):
        state = simulate("full-sdlc-api", phase("implement", "yes"))
        self.assertEqual({n: state[n] for n in ["implement"] + TAIL}, {n: "completed" for n in ["implement"] + TAIL})
        legacy = simulate("full-sdlc-api", phase("legacy", "yes"))
        self.assertEqual(legacy["gate-tests"], "completed")
        self.assertEqual(legacy["local-candidate"], "skipped")
        for name in ("planning", "integration"):
            skipped = simulate("full-sdlc-api", phase(name, "no"))
            self.assertEqual({n: skipped[n] for n in TAIL}, {n: "skipped" for n in TAIL}, name)

    def test_negative_control_gate_tests_on_implement_alone_skips_the_tail(self):
        nodes = yaml.safe_load((WORKFLOWS / "full-sdlc-api.yaml").read_text(encoding="utf-8"))["nodes"]
        gate = next(n for n in nodes if n["id"] == "gate-tests")
        gate["depends_on"] = ["implement"]
        gate.pop("trigger_rule")
        state = simulate("full-sdlc-api", phase("implement", "no"), nodes)
        self.assertEqual({n: state[n] for n in TAIL}, {n: "skipped" for n in TAIL})


class VerifyOnlyBootstrapWiring(unittest.TestCase):
    def test_bootstrap_rewrites_the_base_after_recording_head(self):
        for lane in ("full-sdlc-api", "full-sdlc-api-codex", "full-sdlc-web", "full-sdlc-web-codex"):
            with self.subTest(lane=lane):
                body = runnable_body(lane, "bootstrap")
                recorded = body.index('rev-parse HEAD > "$ARTIFACTS_DIR/bootstrap-head.txt"')
                rewritten = body.index('verify-only-base.py "$ARTIFACTS_DIR" "$WT"')
                self.assertLess(recorded, rewritten)


class VerifyOnlyGateTests(unittest.TestCase):
    """verify-only-base.py (bootstrap) followed by the real gate-tests body."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="verify-only-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.art = self.tmp / "artifacts"
        self.art.mkdir()
        write_shims(self.tmp, ("bun",))
        self.wt = self.tmp / "wt"
        self.previous = init_worktree(self.wt, dirty=False)
        jdump(self.art / "verify.json", {"test_patterns": ["src/__tests__/foo.spec.ts"]})
        jdump(self.art / "files-allowlist.json", ["src/foo.ts"])
        doc = params(self.tmp, self.wt)
        doc.update(repo="api", feature_verify_only="yes", feature_previous_head=self.previous)
        jdump(self.art / "params.json", doc)

    def commit_hand_fix(self):
        (self.wt / "src" / "foo.ts").write_text("export function foo(x: number): number {\n  return x + 2;\n}\n", encoding="utf-8")
        git(self.wt, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "fix(api): validate window")
        return git(self.wt, "rev-parse", "HEAD").stdout.strip()

    def bootstrap(self):
        # bootstrap writes the worktree HEAD first; the helper then rebases it.
        head = git(self.wt, "rev-parse", "HEAD").stdout.strip()
        (self.art / "bootstrap-head.txt").write_text(head + "\n", encoding="utf-8")
        return subprocess.run([sys.executable, str(HELPER), str(self.art), str(self.wt)], capture_output=True, text=True)

    def gate_tests(self):
        env = dict(os.environ, **_isolation_env(self.tmp), ARTIFACTS_DIR=str(self.art))
        env["PATH"] = f"{self.tmp / 'bin'}:{env['PATH']}"
        return subprocess.run(["bash", "-c", runnable_body("full-sdlc-api", "gate-tests")],
                               capture_output=True, text=True, env=env, cwd=str(self.tmp))

    def test_a_committed_hand_fix_is_verified_as_changed_against_the_previous_head(self):
        head = self.commit_hand_fix()
        b = self.bootstrap()
        self.assertEqual(b.returncode, 0, b.stdout + b.stderr)
        self.assertIn(f"VERIFY_ONLY=BASE previous={self.previous} head={head}", b.stdout)
        self.assertEqual((self.art / "commit-msg.txt").read_text(encoding="utf-8"), "fix(api): validate window\n")
        p = self.gate_tests()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("REOPEN_GATE=PASS verify-only", p.stdout)
        self.assertIn("GATE_TESTS=PASS", p.stdout)
        result = json.loads((self.art / "feature-result.json").read_text(encoding="utf-8"))
        self.assertEqual((result["outcome"], result["baseline"], result["head"]), ("CHANGED", self.previous, head))

    def test_negative_control_without_the_base_rewrite_the_fix_is_invisible(self):
        head = self.commit_hand_fix()
        (self.art / "bootstrap-head.txt").write_text(head + "\n", encoding="utf-8")
        (self.art / "commit-msg.txt").write_text("fix(api): validate window\n", encoding="utf-8")
        p = self.gate_tests()
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("IMPLEMENT=FAIL verify-only reopen has no change", p.stdout)

    def test_verify_only_with_no_change_since_the_previous_head_is_refused(self):
        b = self.bootstrap()
        self.assertEqual(b.returncode, 0, b.stdout + b.stderr)
        p = self.gate_tests()
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn(f"IMPLEMENT=FAIL verify-only reopen has no change since previous head {self.previous}", p.stdout)
        self.assertNotIn("GATE_TESTS=PASS", p.stdout)

    def test_non_verify_only_keeps_the_worktree_head_as_base(self):
        head = self.commit_hand_fix()
        doc = json.loads((self.art / "params.json").read_text(encoding="utf-8"))
        doc.pop("feature_verify_only")
        jdump(self.art / "params.json", doc)
        b = self.bootstrap()
        self.assertEqual(b.returncode, 0, b.stdout + b.stderr)
        self.assertIn("VERIFY_ONLY=SKIP", b.stdout)
        self.assertEqual((self.art / "bootstrap-head.txt").read_text(encoding="utf-8").strip(), head)
        self.assertFalse((self.art / "commit-msg.txt").exists())

    def test_a_previous_head_outside_the_worktree_history_is_refused(self):
        doc = json.loads((self.art / "params.json").read_text(encoding="utf-8"))
        doc["feature_previous_head"] = "0" * 40
        jdump(self.art / "params.json", doc)
        b = self.bootstrap()
        self.assertEqual(b.returncode, 1, b.stdout + b.stderr)
        self.assertIn("VERIFY_ONLY=FAIL previous head 000000000000 is not an ancestor", b.stdout)


if __name__ == "__main__":
    unittest.main()
