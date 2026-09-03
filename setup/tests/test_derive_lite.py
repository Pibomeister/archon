#!/usr/bin/env python3
"""Tests for derive-lite.py: the lite lanes are DEFINED by parent + manifest +
overlays, so the generator's guarantees are what keep them honest.

  - a retained node is the parent's bytes, modulo the manifest's declared
    overrides (depends_on, loop cap, port map) and its overlay files
  - the shipped YAML equals a fresh regeneration (drift = packaging failure)
  - the generator refuses: an overlay for an unlisted id, a dangling
    depends_on, a consumed artifact with no producer
"""
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

SETUP = Path(__file__).resolve().parent.parent
ARCHON = SETUP.parent
GEN = SETUP / "derive-lite.py"
LANES = {"api": "full-sdlc-api.yaml", "bugfix": "bugfix.yaml"}


def run_gen(args, env=None):
    return subprocess.run([sys.executable, str(GEN), *args], capture_output=True, encoding="utf-8", env=env)


def load(p):
    return yaml.safe_load(Path(p).read_text(encoding="utf-8"))


def subst_ports(obj, ports):
    if isinstance(obj, str):
        for a, b in ports.items():
            obj = obj.replace(a, b)
        return obj
    if isinstance(obj, list):
        return [subst_ports(x, ports) for x in obj]
    if isinstance(obj, dict):
        return {k: subst_ports(v, ports) for k, v in obj.items()}
    return obj


class RetainedNodesAreParentBytes(unittest.TestCase):
    def check_lane(self, lane):
        mpath = SETUP / "lite" / f"{lane}.json"
        if not mpath.is_file():
            self.skipTest(f"{lane} manifest not built yet")
        m = json.loads(mpath.read_text(encoding="utf-8"))
        parent = {n["id"]: n for n in load(ARCHON / "workflows" / m["parent"])["nodes"]}
        out = ARCHON / "workflows" / f"{m['name']}.yaml"
        self.assertTrue(out.is_file(), f"{out} not generated")
        gen = {n["id"]: n for n in load(out)["nodes"]}
        overlay_dir = SETUP / "lite" / lane
        overlaid = set()
        for f in os.listdir(overlay_dir) if overlay_dir.is_dir() else []:
            overlaid.add(f.split(".")[0])
        self.assertEqual(list(gen), m["nodes"], "generated id order != manifest")
        for nid in m["nodes"]:
            if nid not in parent:
                self.assertIn(nid, overlaid, f"new node {nid} has no overlay")
                continue
            exp = copy.deepcopy(parent[nid])
            if nid in m.get("depends_on", {}):
                exp["depends_on"] = list(m["depends_on"][nid])
            if nid in m.get("loops", {}):
                exp["loop_group"]["max_iterations"] = m["loops"][nid]["max_iterations"]
            exp = subst_ports(exp, m.get("ports", {}))
            got = gen[nid]
            if nid in overlaid:
                # only the overlaid fields may differ
                for k in exp:
                    if k in ("prompt", "bash", "approval", "loop_group"):
                        continue
                    self.assertEqual(exp[k], got.get(k), f"{nid}: non-overlay field {k} drifted")
            else:
                self.assertEqual(exp, got, f"{nid}: retained node differs from parent (modulo declared overrides)")

    def test_api(self):
        self.check_lane("api")

    def test_bugfix(self):
        self.check_lane("bugfix")


class ShippedEqualsRegeneration(unittest.TestCase):
    def test_check_mode(self):
        for lane in LANES:
            if not (SETUP / "lite" / f"{lane}.json").is_file():
                continue
            r = run_gen([lane, "--check"])
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("LITE_DRIFT=OK", r.stdout)


class LiteSafetyNodes(unittest.TestCase):
    def test_approval_lanes_are_interactive(self):
        for lane in LANES:
            with self.subTest(lane=lane):
                m = json.loads((SETUP / "lite" / f"{lane}.json").read_text())
                parent = load(ARCHON / "workflows" / m["parent"])
                self.assertIs(parent.get("interactive"), True)

    def test_control_guard_is_first_and_always_run(self):
        for lane in LANES:
            with self.subTest(lane=lane):
                m = json.loads((SETUP / "lite" / f"{lane}.json").read_text())
                self.assertEqual(m["nodes"][0], "codex-control-guard")
                self.assertEqual(m["depends_on"]["preflight"], ["codex-control-guard"])
                derived = Path(m["parent"]).stem + "-lite.yaml"
                node = load(ARCHON / "workflows" / derived)["nodes"][0]
                self.assertIs(node.get("always_run"), True)

    def test_prbody_gate_is_between_prbody_and_ship(self):
        for lane in LANES:
            with self.subTest(lane=lane):
                m = json.loads((SETUP / "lite" / f"{lane}.json").read_text())
                self.assertLess(m["nodes"].index("prbody"), m["nodes"].index("prbody-gate"))
                self.assertLess(m["nodes"].index("prbody-gate"), m["nodes"].index("ship"))
                self.assertEqual(m["depends_on"]["prbody-gate"], ["prbody"])
                self.assertEqual(m["depends_on"]["ship"], ["prbody-gate"])

    def test_impact_overlays_require_successful_query_provenance(self):
        for lane in LANES:
            for suffix in ("lite-impact.node.yaml", "lite-impact-post.node.yaml"):
                with self.subTest(lane=lane, overlay=suffix):
                    text = (SETUP / "lite" / lane / suffix).read_text(encoding="utf-8")
                    for token in ("query_status", "query_repo", "query_target", "ANY impact call"):
                        self.assertIn(token, text)

    def test_lite_approval_messages_match_actual_gate_count(self):
        api = load(ARCHON / "workflows" / "full-sdlc-api-lite.yaml")
        plan = next(n for n in api["nodes"] if n["id"] == "plan-gate")
        self.assertIn("provider-appropriate commands", plan["approval"]["message"])
        bug = load(ARCHON / "workflows" / "bugfix-lite.yaml")
        rca = next(n for n in bug["nodes"] if n["id"] == "rca-approval")
        self.assertIn("only gate", rca["approval"]["message"])
        self.assertIn("no in-app smoke gate", rca["approval"]["message"])


class GeneratorRefusals(unittest.TestCase):
    """Copy the api manifest + overlays into a temp setup/ tree next to a copy of
    the generator, mutate one thing, and assert the typed failure."""

    def setUp(self):
        if not (SETUP / "lite" / "api.json").is_file():
            self.skipTest("api manifest not built yet")
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "workflows").mkdir()
        shutil.copy(ARCHON / "workflows" / "full-sdlc-api.yaml", self.tmp / "workflows")
        self.setup = self.tmp / "setup"
        (self.setup / "lite").mkdir(parents=True)
        shutil.copy(GEN, self.setup / "derive-lite.py")
        shutil.copy(SETUP / "lite" / "api.json", self.setup / "lite" / "api.json")
        shutil.copytree(SETUP / "lite" / "api", self.setup / "lite" / "api")
        self.manifest = json.loads((self.setup / "lite" / "api.json").read_text())

    def save(self):
        (self.setup / "lite" / "api.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def gen(self):
        return subprocess.run([sys.executable, str(self.setup / "derive-lite.py"), "api", "--out", str(self.tmp / "out.yaml")],
                              capture_output=True, encoding="utf-8")

    def test_baseline_generates(self):
        r = self.gen()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DERIVE_LITE=OK", r.stdout)

    def test_overlay_for_unlisted_id(self):
        (self.setup / "lite" / "api" / "docreview.prompt.md").write_text("x", encoding="utf-8")
        r = self.gen()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("overlay for id not in the manifest node list: docreview", r.stdout + r.stderr)

    def test_dangling_depends_on(self):
        self.manifest["depends_on"]["implement"] = ["docreview-gate"]
        self.save()
        r = self.gen()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("depends_on 'docreview-gate' is not a selected node", r.stdout + r.stderr)

    def test_consumer_without_producer(self):
        self.manifest["contracts"]["prbody"]["consumes"].append("smoke-matrix.json")
        self.save()
        r = self.gen()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("prbody consumes 'smoke-matrix.json' but no selected node produces it", r.stdout + r.stderr)

    def test_missing_contract_for_selected_node(self):
        del self.manifest["contracts"]["smoke"]
        self.save()
        r = self.gen()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("contracts missing for selected nodes: smoke", r.stdout + r.stderr)

    def test_new_node_without_node_yaml(self):
        os.remove(self.setup / "lite" / "api" / "post-fix-gate.node.yaml")
        r = self.gen()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("post-fix-gate: not in the parent and no post-fix-gate.node.yaml overlay", r.stdout + r.stderr)

    def test_hand_edit_is_drift(self):
        r = self.gen()
        self.assertEqual(r.returncode, 0, r.stderr)
        out = self.tmp / "out.yaml"
        out.write_text(out.read_text(encoding="utf-8").replace("PREFLIGHT=PASS", "PREFLIGHT=PASS # edited"), encoding="utf-8")
        r = subprocess.run([sys.executable, str(self.setup / "derive-lite.py"), "api", "--out", str(out), "--check"],
                           capture_output=True, encoding="utf-8")
        self.assertEqual(r.returncode, 1)
        self.assertIn("LITE_DRIFT=FAIL full-sdlc-api-lite", r.stdout)

    def test_grep_lint_is_warning_only(self):
        # reference an artifact nobody produces inside a NEW node's bash: warn, still rc 0
        p = self.setup / "lite" / "api" / "post-fix-gate.node.yaml"
        p.write_text(p.read_text(encoding="utf-8").replace('rm -rf .omc', 'rm -rf .omc; cat "$ARTIFACTS_DIR/never-produced.txt" || true'), encoding="utf-8")
        r = self.gen()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DERIVE_LITE=WARN post-fix-gate references never-produced.txt", r.stderr)


if __name__ == "__main__":
    unittest.main()
