#!/usr/bin/env python3
"""Read-side contract of the skills layer across the five lanes and their codex
twins: the stage-skills node exists with the right edges, only implement, fix
and fixer are told about skills.md, the two prompt lines are byte-identical
everywhere, and the doctrine lock carries them."""
import json
import re
import unittest
from pathlib import Path

import yaml

ARCHON = Path(__file__).resolve().parents[2]
LANES = ("full-sdlc-api", "full-sdlc-web", "bugfix", "full-sdlc-api-lite", "bugfix-lite")
TWINS = tuple(f"{lane}-codex" for lane in LANES)
ALL = LANES + TWINS
PROMPT_LINES = (
    "If skills.md exists in the artifacts directory, read it before you edit anything: it holds",
    "repository skills compiled from earlier runs; they never override the plan, the allowlist, or this prompt.",
)
PROMPT_NODES = {"implement", "fix", "fixer"}
HELPER = "setup/stage-skills-library.py"
TEE = 'exec > >(tee -a "$ARTIFACTS_DIR/node-stage-skills.out") 2> >(tee -a "$ARTIFACTS_DIR/node-stage-skills.out" >&2)'


def load(lane):
    return yaml.safe_load((ARCHON / "workflows" / f"{lane}.yaml").read_text())


def nodes(lane):
    return {n["id"]: n for n in load(lane)["nodes"]}


def walk(node):
    """Every node reachable inside a loop_group, flattened, keyed by id."""
    out = {node["id"]: node}
    for child in node.get("loop_group", {}).get("nodes", []) or []:
        out.update(walk(child))
    return out


def prompt_nodes(lane):
    out = {}
    for top in load(lane)["nodes"]:
        for nid, n in walk(top).items():
            if "prompt" in n:
                out[nid] = n["prompt"]
    return out


class StageSkillsNode(unittest.TestCase):
    def test_present_in_every_lane_with_tee_and_helper(self):
        for lane in ALL:
            with self.subTest(lane=lane):
                n = nodes(lane)["stage-skills"]
                body = n["bash"]
                self.assertTrue(body.startswith("set -euo pipefail\n"), lane)
                self.assertIn(TEE, body)
                self.assertIn(HELPER, body)
                self.assertIn("--artifacts \"$ARTIFACTS_DIR\"", body)
                self.assertEqual(n["timeout"], 120000)
                self.assertNotIn("skills:", n)
                # derive-codex counts `$SK/ce-*/SKILL.md` inserts; the node must
                # never look like one of the guarded targets.
                self.assertNotIn("$SK/", body)
                self.assertNotIn("PURPOSE.md\"", body)

    def test_when_only_on_the_api_parent_and_its_twin(self):
        for lane in ALL:
            with self.subTest(lane=lane):
                n = nodes(lane)["stage-skills"]
                if lane in ("full-sdlc-api", "full-sdlc-api-codex"):
                    self.assertEqual(n["when"], "$feature-phase.implement == 'yes'")
                else:
                    self.assertNotIn("when", n)

    def test_depends_on_edges(self):
        for lane in ("full-sdlc-api", "full-sdlc-api-codex"):
            ns = nodes(lane)
            self.assertEqual(ns["stage-skills"]["depends_on"], ["bootstrap"])
            self.assertEqual(ns["implement"]["depends_on"], ["plan-gate", "implementation-ready", "stage-skills"])
            self.assertIn("trigger_rule", ns["implement"])
        for lane in ("full-sdlc-api-lite", "full-sdlc-api-lite-codex"):
            ns = nodes(lane)
            self.assertEqual(ns["stage-skills"]["depends_on"], ["bootstrap"])
            self.assertEqual(ns["implement"]["depends_on"], ["lite-envelope-post", "stage-skills"])
        for lane in ("full-sdlc-web", "full-sdlc-web-codex"):
            ns = nodes(lane)
            self.assertEqual(ns["stage-skills"]["depends_on"], ["bootstrap"])
            self.assertEqual(ns["implement"]["depends_on"],
                             ["gen-api-sync", "premise-gate", "web-plan-lock-gate", "stage-skills"])
        for lane in ("bugfix", "bugfix-codex", "bugfix-lite", "bugfix-lite-codex"):
            ns = nodes(lane)
            self.assertEqual(ns["stage-skills"]["depends_on"], ["commit-red"])
            self.assertEqual(ns["fix-loop"]["depends_on"], ["stage-skills"])
            ids = list(ns)
            self.assertLess(ids.index("commit-red"), ids.index("stage-skills"))
            self.assertLess(ids.index("stage-skills"), ids.index("fix-loop"))
            # red-test and red-gate stay upstream and skill-blind
            for blind in ("red-test", "red-gate"):
                self.assertLess(ids.index(blind), ids.index("stage-skills"))

    def test_repo_routing_in_the_body(self):
        for lane in ALL:
            with self.subTest(lane=lane):
                body = nodes(lane)["stage-skills"]["bash"]
                # the repo is the helper's positional argument, not a flag
                self.assertNotIn("--repo", body)
                if lane.startswith("full-sdlc-web"):
                    self.assertIn("stage-skills-library.py web-app", body)
                    self.assertNotIn('params-env.sh "', body)  # never invoked (a comment may name it)
                    self.assertNotIn("$REPO", body)
                else:
                    self.assertIn("params-env.sh", body)
                    self.assertRegex(body, r'stage-skills-library\.py"? "\$REPO"')
                    self.assertNotIn("web-app", body)

    def test_lite_manifests_declare_the_node(self):
        for name, consumer in (("api", "implement"), ("bugfix", "fix-loop")):
            m = json.loads((ARCHON / "setup/lite" / f"{name}.json").read_text())
            self.assertIn("stage-skills", m["nodes"])
            self.assertEqual(m["nodes"].index("stage-skills") - 1,
                             m["nodes"].index("bootstrap" if name == "api" else "commit-red"))
            self.assertEqual(m["contracts"]["stage-skills"],
                             {"produces": ["skills.md", "skills-staged.json"], "consumes": ["params.json"]})
            self.assertIn("skills.md", m["contracts"][consumer]["consumes"])
            self.assertIn("skills.md", m["contracts"]["review-loop"]["consumes"])
            if consumer in m["depends_on"]:
                self.assertIn("stage-skills", m["depends_on"][consumer])
        self.assertEqual(json.loads((ARCHON / "setup/lite/api.json").read_text())["remove_fields"]["stage-skills"],
                         ["when"])


class PromptLines(unittest.TestCase):
    def test_exactly_implement_fix_fixer_mention_skills_md(self):
        for lane in ALL:
            with self.subTest(lane=lane):
                mentions = {nid for nid, text in prompt_nodes(lane).items() if "skills.md" in text}
                expected = {"implement", "fixer"} if "web" in lane or "full-sdlc-api" in lane else {"fix", "fixer"}
                self.assertEqual(mentions, expected, lane)
                self.assertTrue(mentions <= PROMPT_NODES)

    def test_both_lines_verbatim_and_adjacent(self):
        for lane in ALL:
            with self.subTest(lane=lane):
                for nid, text in prompt_nodes(lane).items():
                    if "skills.md" not in text:
                        continue
                    lines = [l.strip() for l in text.splitlines()]
                    i = lines.index(PROMPT_LINES[0])
                    self.assertEqual(lines[i + 1], PROMPT_LINES[1], f"{lane}:{nid}")
                    self.assertEqual(text.count("skills.md"), 1, f"{lane}:{nid}")
                    # placed before the repo conventions block where one exists
                    if "<repo-conventions" in text:
                        self.assertLess(text.index(PROMPT_LINES[0]), text.index("<repo-conventions"), f"{lane}:{nid}")
                    if nid == "fixer":
                        self.assertLess(text.index(PROMPT_LINES[0]), text.index("fixer-result.json"), f"{lane}:{nid}")

    def test_no_bash_node_outside_stage_skills_reads_skills_md(self):
        for lane in ALL:
            with self.subTest(lane=lane):
                for top in load(lane)["nodes"]:
                    for nid, n in walk(top).items():
                        if "bash" in n and nid != "stage-skills":
                            self.assertNotIn("skills.md", n["bash"], f"{lane}:{nid}")

    def test_doctrine_lock_carries_both_lines(self):
        lock = json.loads((ARCHON / "setup/lane-doctrine.lock.json").read_text())
        for nid in PROMPT_NODES:
            for line in PROMPT_LINES:
                self.assertIn(line, lock[nid], f"{nid}: {line}")
        for nid, lines in lock.items():
            if nid not in PROMPT_NODES:
                self.assertFalse(any("skills.md" in l for l in lines), nid)


class Packaging(unittest.TestCase):
    def test_helper_is_stdlib_and_imports_the_module(self):
        text = (ARCHON / HELPER).read_text()
        self.assertIn("import skill_library as sl", text)
        imports = set(re.findall(r"^(?:import|from) (\w+)", text, re.M))
        self.assertTrue(imports <= {"argparse", "os", "sys", "skill_library"}, imports)
        self.assertNotIn("PURPOSE.md", text.split('"""', 2)[2])
        self.assertNotIn("wiki/", text.split('"""', 2)[2])


if __name__ == "__main__":
    unittest.main()
