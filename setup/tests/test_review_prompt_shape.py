#!/usr/bin/env python3
"""The prompt bodies' shape, which is the thing the measurement measured.

Review cost is the fan-out. v1 measured 14.4 minutes a round against 6-13
persona subagents; the trio's whole claim is two discovery roles and a
validator. Nothing in the engine enforces that -- it is a sentence in a prompt --
so a later edit that "just adds the maintainability lens back" silently returns
the lane to v1's arithmetic while every gate still passes.

These tests pin the count, the roles, and the envelope footer the gates parse.
They are deliberately literal: a prompt is text, and a test that paraphrased it
would drift from the text it guards.
"""
import re
import unittest
from pathlib import Path

ARCHON = Path(__file__).resolve().parents[2]
PROMPTS = ARCHON / "setup" / "prompts"
TRIO = (PROMPTS / "review-trio.md").read_text(encoding="utf-8")
CAPPED = (PROMPTS / "review-capped.md").read_text(encoding="utf-8")
CE = (PROMPTS / "review-ce.md").read_text(encoding="utf-8")
VERIFY = (PROMPTS / "review-verify.md").read_text(encoding="utf-8")
CONTRACT = (ARCHON / "setup" / "review-contract.md").read_text(encoding="utf-8")

# The v1 persona set. Any of these names in a review prompt is the cost
# regression this whole item exists to prevent.
V1_PERSONAS = ("maintainability", "project-standards", "agent-native",
               "learnings-researcher", "api-contract", "kieran-typescript",
               "testing-lens")

FOOTER = ["Scope:", "Base:", "Head:", "Input:", "Verdict:", "Review complete"]


def flat(text):
    """Whitespace-collapsed, so an assertion pins the sentence rather than the
    column the paragraph happened to wrap at."""
    return " ".join(text.split())


FLAT = {"trio": flat(TRIO), "capped": flat(CAPPED), "ce": flat(CE), "verify": flat(VERIFY),
        "contract": flat(CONTRACT)}


def named_roles(text):
    """Subagent role names the prompt declares, by the one convention the
    prompts use: a backticked name immediately after the word `subagent`."""
    return set(re.findall(r"subagents?\s+`([a-z0-9-]+)`", text)) | \
        set(re.findall(r"`([a-z0-9-]+)`\s+subagent", text)) | \
        set(re.findall(r"\*\*Subagent `([a-z0-9-]+)`", text))


class ReviewPromptShape(unittest.TestCase):
    def assertFooterInOrder(self, text, scope):
        idx = [text.rfind(tok) for tok in FOOTER]
        self.assertNotIn(-1, idx, f"missing footer token in {scope}")
        self.assertEqual(idx, sorted(idx), f"footer out of order in {scope}: {idx}")
        self.assertIn(f"Scope: {scope}", text)

    # --- the trio ---------------------------------------------------------

    def test_the_trio_names_exactly_two_discovery_roles_and_one_validator(self):
        roles = named_roles(TRIO)
        self.assertEqual(roles, {"correctness-and-spec", "risk", "validator"}, roles)

    def test_the_trio_says_two_out_loud_so_the_count_is_not_only_implied(self):
        self.assertIn("dispatch exactly two discovery subagents", FLAT["trio"])
        self.assertRegex(TRIO, r"There are two, not three")

    def test_the_trio_does_not_reintroduce_the_v1_persona_set(self):
        for persona in V1_PERSONAS:
            with self.subTest(persona=persona):
                self.assertNotIn(persona, TRIO)

    def test_the_trio_discovery_subagents_run_on_sonnet_and_in_parallel(self):
        self.assertIn("Both on model `sonnet`", TRIO)
        self.assertIn("single message so they run concurrently", TRIO)

    def test_the_trio_add_on_is_triggered_by_a_path_match_not_by_judgement(self):
        self.assertIn("`migrations/`, `auth`, or `payments`", TRIO)
        self.assertIn("the trigger is the path match, not your judgement",
                      FLAT["trio"])

    def test_the_trio_builds_one_evidence_packet_for_both_subagents(self):
        self.assertIn("Both discovery subagents get this same block verbatim",
                      FLAT["trio"])
        self.assertIn("pinned_decisions", TRIO)
        self.assertIn("**verbatim**", TRIO)

    def test_the_trio_emits_the_full_scope_footer(self):
        self.assertFooterInOrder(TRIO, "full")

    # --- the capped ce-code-review ----------------------------------------

    def test_the_capped_mode_fixes_three_personas_and_keeps_the_validator(self):
        self.assertIn("correctness, security, adversarial", CAPPED)
        self.assertIn("Exactly those three, every round", CAPPED)
        self.assertIn("the skill's own validator pass", CAPPED)

    def test_the_capped_mode_refuses_the_catalogs_own_selection(self):
        # The skill sizes its fan-out on diff size. That is exactly the
        # behaviour being capped, so the override has to be explicit.
        self.assertIn("If the skill's catalog wants to select differently, the "
                      "cap overrides it", FLAT["capped"])
        self.assertIn("do not add conditional personas", CAPPED)

    def test_the_capped_mode_emits_the_full_scope_footer(self):
        self.assertFooterInOrder(CAPPED, "full")

    def test_the_capped_mode_still_forbids_external_review(self):
        self.assertIn("External or cross-model review is prohibited", CAPPED)

    # --- the verify pass --------------------------------------------------

    def test_verify_names_exactly_one_role_and_it_is_the_validator(self):
        roles = named_roles(VERIFY)
        self.assertEqual(roles, {"validator"}, roles)
        self.assertIn("exactly one subagent in this mode", FLAT["verify"])
        self.assertIn("No discovery fan-out", FLAT["verify"])

    def test_verify_requires_a_cited_line_at_head_for_every_closure(self):
        self.assertIn("Cite `file:line` **at the current HEAD**", FLAT["verify"])
        self.assertIn("A `closed` without a cited line at the current head is "
                      "not closure. Return it as `open`", FLAT["verify"])

    def test_verify_bounds_the_regression_scan_to_a_named_perimeter(self):
        self.assertIn("Bounded regression scan", VERIFY)
        for clause in ("enclosing function in full", "Every caller, repository-wide",
                       "Every reader of any state the repair moved"):
            with self.subTest(clause=clause):
                self.assertIn(clause, VERIFY)

    def test_verify_emits_the_verify_scope_footer(self):
        self.assertFooterInOrder(VERIFY, "verify")

    def test_verify_states_the_verdict_rule_the_ledger_depends_on(self):
        self.assertIn("iff no P0 or P1 is `open` or `regressed` and no new P0 or "
                      "P1 was found", FLAT["verify"])

    # --- shared obligations ----------------------------------------------

    def test_every_review_prompt_opens_and_closes_with_the_round_state_marks(self):
        for name, text in (("trio", TRIO), ("capped", CAPPED), ("ce", CE), ("verify", VERIFY)):
            with self.subTest(prompt=name):
                self.assertIn('mark review-start', text)
                self.assertIn('mark review-done', text)
                self.assertLess(text.index("mark review-start"),
                                text.index("mark review-done"))
                self.assertIn("YOUR FIRST ACTION", text)
                self.assertIn("YOUR LAST ACTION", text)

    def test_every_review_prompt_binds_itself_to_the_versioned_contract(self):
        for name, text in (("trio", TRIO), ("capped", CAPPED), ("ce", CE), ("verify", VERIFY)):
            with self.subTest(prompt=name):
                # verify is rendered in place (read off disk by the session), so its
                # path is absolute; the embedded modes keep the placeholder.
                self.assertRegex(text, r"(\{\{SETUP\}\}|/setup)/review-contract\.md")
                self.assertIn("Where the two disagree, the contract wins",
                              FLAT[name])

    def test_every_review_prompt_states_the_reviewer_is_read_only(self):
        for name, text in (("trio", TRIO), ("capped", CAPPED), ("ce", CE), ("verify", VERIFY)):
            with self.subTest(prompt=name):
                self.assertIn("read-only in the candidate worktree", FLAT[name])

    def test_the_contract_pins_the_finding_id_formula_the_ledger_recomputes(self):
        self.assertIn('finding_id = sha1(normalize(file) + "|" + normalize(title))[:12]',
                      FLAT["contract"])
        self.assertIn("setup/finding_key.py", CONTRACT)

    def test_the_contract_pins_the_verdict_enum_the_gate_parses(self):
        self.assertIn("Ready to merge | Ready with fixes | Not ready", CONTRACT)

    # --- the bounded doc review ------------------------------------------

    # docreview shape cases removed: the bounded docreview prompt was not shipped (its
    # measurement never completed), so the node keeps the ce-doc-review skill invocation.

