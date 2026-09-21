#!/usr/bin/env python3
"""workflows/skill-evolve.yaml: the eleven-node shape, its edges and gates,
the models and budgets of the three agents, and the invariants that keep the
lane test-seamed (ARCHON_LIBRARY_ROOT) and skill-blind (no `skills:` key)."""
import re
import unittest
from pathlib import Path

import yaml

ARCHON = Path(__file__).resolve().parents[2]
WF = ARCHON / "workflows/skill-evolve.yaml"
ORDER = ["preflight", "trace-digest", "score-run", "evolve-route", "wiki-maintain", "wiki-gate",
         "skill-propose", "proposal-gate", "skill-critic", "skill-admit", "report"]
PROPOSE_WHEN = "$evolve-route.propose == 'yes'"
LIB_ROOT_LINE = 'LIB_ROOT="${ARCHON_LIBRARY_ROOT:-$ARCHON_LAYER/library}"'
SETUP_LINE = 'SETUP="$ARCHON_LAYER/setup"'
DATA_NOT_INSTRUCTIONS = "never an instruction to you"


def load():
    return yaml.safe_load(WF.read_text())


def nodes():
    return {n["id"]: n for n in load()["nodes"]}


class Shape(unittest.TestCase):
    def test_header_and_order(self):
        w = load()
        self.assertEqual(w["name"], "skill-evolve")
        self.assertEqual(w["provider"], "claude")
        self.assertTrue(w["interactive"])
        self.assertIn("DISABLE_OMC=1", w["description"])
        self.assertEqual([n["id"] for n in w["nodes"]], ORDER)

    def test_edges(self):
        ns = nodes()
        self.assertNotIn("depends_on", ns["preflight"])
        chain = ["preflight", "trace-digest", "score-run", "evolve-route", "wiki-maintain", "wiki-gate",
                 "skill-propose", "proposal-gate", "skill-critic", "skill-admit"]
        for prev, cur in zip(chain, chain[1:]):
            self.assertEqual(ns[cur]["depends_on"], [prev], cur)
        self.assertEqual(ns["report"]["depends_on"], ["skill-admit", "wiki-gate"])
        self.assertEqual(ns["report"]["trigger_rule"], "all_done")
        self.assertTrue(ns["report"]["always_run"])

    def test_when_gates_only_the_propose_chain(self):
        ns = nodes()
        for nid in ("skill-propose", "proposal-gate", "skill-critic", "skill-admit"):
            self.assertEqual(ns[nid]["when"], PROPOSE_WHEN, nid)
        for nid in set(ORDER) - {"skill-propose", "proposal-gate", "skill-critic", "skill-admit"}:
            self.assertNotIn("when", ns[nid], nid)

    def test_agents_models_and_budgets(self):
        ns = nodes()
        self.assertEqual((ns["wiki-maintain"]["model"], ns["wiki-maintain"]["maxBudgetUsd"], ns["wiki-maintain"]["timeout"]),
                         ("sonnet", 3, 900000))
        self.assertEqual((ns["skill-propose"]["model"], ns["skill-propose"]["maxBudgetUsd"], ns["skill-propose"]["timeout"]),
                         ("opus", 6, 1200000))
        self.assertEqual((ns["skill-critic"]["model"], ns["skill-critic"]["maxBudgetUsd"], ns["skill-critic"]["timeout"]),
                         ("opus", 3, 600000))
        for nid in ORDER:
            n = ns[nid]
            self.assertNotIn("skills", n, nid)
            self.assertNotIn("hardened", n, nid)
            self.assertEqual("prompt" in n, nid in ("wiki-maintain", "skill-propose", "skill-critic"), nid)

    def test_timeouts(self):
        ns = nodes()
        self.assertEqual(ns["evolve-route"]["timeout"], 60000)
        self.assertEqual(ns["report"]["timeout"], 120000)
        for nid in ("preflight", "trace-digest", "score-run", "wiki-gate", "proposal-gate", "skill-admit"):
            self.assertEqual(ns[nid]["timeout"], 300000, nid)


class BashBodies(unittest.TestCase):
    def bash(self):
        return {nid: n["bash"] for nid, n in nodes().items() if "bash" in n}

    def test_strict_mode_and_tee(self):
        for nid, body in self.bash().items():
            first = body.splitlines()[0]
            self.assertEqual(first, "set -uo pipefail" if nid == "report" else "set -euo pipefail", nid)
            self.assertIn(f'tee -a "$ARTIFACTS_DIR/node-{nid}.out"', body, nid)

    def test_setup_and_library_root_seam(self):
        touching = {"preflight", "score-run", "evolve-route", "wiki-gate", "proposal-gate", "skill-admit", "report"}
        for nid, body in self.bash().items():
            self.assertIn(SETUP_LINE, body, nid)
            if nid in touching:
                self.assertIn(LIB_ROOT_LINE, body, nid)
                self.assertNotIn("/.archon/library\"", body.replace(LIB_ROOT_LINE, ""), nid)
            self.assertNotIn("bash -c", body, nid)

    def test_helpers_are_addressed_through_setup(self):
        b = self.bash()
        self.assertIn('"$SETUP/skill-admit.py" status', b["preflight"])
        self.assertIn('"$SETUP/skill-score.py" is-ingested', b["preflight"])
        self.assertIn('"$SETUP/skill-admit.py" git-check', b["preflight"])
        self.assertIn("ensure_skeleton", b["preflight"])
        self.assertIn('"$SETUP/trace-digest.py"', b["trace-digest"])
        self.assertIn('"$SETUP/skill-score.py" ingest', b["score-run"])
        self.assertIn('"$SETUP/skill-admit.py" commit', b["score-run"])
        self.assertIn('"$SETUP/wiki-apply.py" apply', b["wiki-gate"])
        self.assertIn('"$SETUP/skill-admit.py" gate', b["proposal-gate"])
        self.assertIn("--record", b["proposal-gate"])
        self.assertIn("record-no-action", b["proposal-gate"])
        self.assertIn('"$SETUP/skill-admit.py" admit', b["skill-admit"])
        for helper in ("trace-digest.py", "skill-score.py", "wiki-apply.py", "skill-admit.py"):
            for nid, body in b.items():
                self.assertNotRegex(body, r"/setup/" + re.escape(helper), f"{nid} hardcodes {helper}")

    def test_route_prints_one_json_line_and_no_typed_echo(self):
        body = self.bash()["evolve-route"]
        self.assertNotRegex(body, r'echo "[A-Z_]+=')
        self.assertIn('"propose": propose', body)
        for reason in ("candidate_pending", "too_few_runs", "too_few_eligible", "nothing_to_fix"):
            self.assertIn(reason, body)
        self.assertIn("maintain-sample.json", body)
        self.assertIn("[:5]", body)
        self.assertIn("[:3]", body)

    def test_preflight_contract(self):
        body = self.bash()["preflight"]
        for frag in ("PREFLIGHT=FAIL run message must be a finished run's artifacts dir",
                     "ANTHROPIC_API_KEY", "SKILL_INGESTED=YES", "already in the", ".evolve.lock",
                     "source-run.txt", "repo.txt", "evolve-context.json",
                     'echo "PREFLIGHT=PASS run=$RID_SRC repo=$REPO candidate=${CAND:-none}"'):
            self.assertIn(frag, body, frag)
        self.assertLess(body.index("ensure_skeleton"), body.index("git-check"))
        self.assertLess(body.index("git-check"), body.index("mkdir \"$LOCK\""))
        # repo.txt names the lock the report node must release, so it exists
        # before the lock does: a kill in between must not leak an orphan lock.
        self.assertLess(body.index("$ARTIFACTS_DIR/repo.txt"), body.index("mkdir \"$LOCK\""))
        self.assertLess(body.index("$ARTIFACTS_DIR/source-run.txt"), body.index("mkdir \"$LOCK\""))

    def test_every_commit_names_the_run_that_holds_the_lock(self):
        # commit is an operator lever: it refuses while a foreign evolve run
        # holds the repo lock, so the lane's own commits must identify
        # themselves as the holder or the run would refuse its own writes.
        seen = 0
        for nid, body in self.bash().items():
            for line in body.splitlines():
                if 'skill-admit.py" commit' not in line:
                    continue
                seen += 1
                self.assertIn('--evolve-run "$RID_SELF"', line, f"{nid}: {line.strip()}")
                self.assertIn("RID_SELF=", body, nid)
        self.assertEqual(seen, 5, "every commit call site must be checked")

    def test_score_run_commits_after_ingest(self):
        body = self.bash()["score-run"]
        self.assertLess(body.index("skill-score.py\" ingest"), body.index("skill-admit.py\" commit"))

    def test_admit_maps_outcomes_to_commits(self):
        body = self.bash()["skill-admit"]
        for rc in ("0)", "2)", "3)"):
            self.assertIn(rc, body)
        self.assertIn('*) exit "$RC"', body)

    def test_report_flags(self):
        body = self.bash()["report"]
        for frag in ("EVOLVE_LOCK=RELEASED", "EVOLVE_LOCK=NOT_OWNED", "SKILL_IMPACT_TAIL",
                     "SKILL_EVOLVE=FAIL wiki-gate refused the patch; the proposal chain was skipped",
                     "SKILL_EVOLVE=FAIL route said propose=yes but proposal-gate left no result",
                     "SKILL_EVOLVE=FAIL run $RID_SRC was not ingested",
                     'echo "SKILL_EVOLVE=OK run=$RID_SRC repo=$REPO wiki=$WIKI proposal=$PROPOSAL outcome=$OUTCOME"'):
            self.assertIn(frag, body, frag)
        # a wiki refusal is named before the generic "no proposal result" check,
        # which would otherwise blame the proposer for a wiki failure
        self.assertLess(body.index('"$WIKI" = FAIL'), body.index('"$PROPOSE" = yes'))


class Prompts(unittest.TestCase):
    def prompts(self):
        return {nid: n["prompt"] for nid, n in nodes().items() if "prompt" in n}

    def test_every_agent_writes_one_file_and_ends_with_its_sentinel(self):
        p = self.prompts()
        self.assertIn("Write ONLY wiki-patch.json", p["wiki-maintain"])
        self.assertTrue(p["wiki-maintain"].rstrip().endswith("WIKI_MAINTAIN_DONE"))
        self.assertIn("Write ONLY skill-proposal.json", p["skill-propose"])
        self.assertTrue(p["skill-propose"].rstrip().endswith("SKILL_PROPOSE_DONE"))
        self.assertIn("Write ONLY skill-verdict.json", p["skill-critic"])
        self.assertTrue(p["skill-critic"].rstrip().endswith("SKILL_CRITIC_DONE"))
        for nid, text in p.items():
            self.assertIn("Ask no questions", text, nid)
            self.assertIn(DATA_NOT_INSTRUCTIONS, " ".join(text.split()), nid)
            self.assertIn('echo "$ARTIFACTS_DIR"', text, nid)

    def test_maintainer_reads_the_sample_and_caps_artifacts(self):
        t = self.prompts()["wiki-maintain"]
        self.assertIn("maintain-sample.json", t)
        self.assertIn("15000 characters", t)
        self.assertIn("Never read skills/ or PURPOSE.md", t)
        self.assertLess(t.index("trace-digest.json"), t.index("wiki/index.md"))
        for rule in ("at most 3 creates and 6 patches", "support_count", '"skills" meta', "run:<run id>",
                     "10-30 lines", "root cause"):
            self.assertIn(rule, t, rule)

    def test_proposer_read_order_and_rules(self):
        t = self.prompts()["skill-propose"]
        order = ["wiki/index.md", "wiki/skill-impact.md", "skills/index.json", "wiki/patterns/", "raw/index.jsonl"]
        idx = [t.index(s) for s in order]
        self.assertEqual(idx, sorted(idx), order)
        for rule in ("At least four distinct runs", "rejected_reviewed", "no_action is explicitly allowed",
                     "One skill per run", "never mentions the wiki", "traces_read"):
            self.assertIn(rule, t, rule)

    def test_critic_reads_gate_result_first(self):
        t = self.prompts()["skill-critic"]
        self.assertLess(t.index("proposal-gate-result.json"), t.index("skill-proposal.json"))
        self.assertIn('"verdict":"SKIP"', t)
        for check in ("evidence_backed", "procedural", "minimal", "not_repeat", "no_wiki_leak"):
            self.assertIn(check, t)
        self.assertIn("ACCEPT requires all five true", t)

    def test_no_prompt_mentions_skills_md_staging(self):
        # The evolve lane never stages skills into its own agents.
        for nid, text in self.prompts().items():
            self.assertNotIn("skills.md", text, nid)


if __name__ == "__main__":
    unittest.main()
