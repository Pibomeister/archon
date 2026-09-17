#!/usr/bin/env python3
"""The review node's persona fan-out is the most expensive thing the pipeline
buys, and the only lever bounding it is the line threshold in its prompt.

Run 38d72218 measured what happens when that threshold is computed on the wrong
quantity: a 30-line fix carrying a 204-line repro test measured 234 "changed
lines", exceeded the ~150 threshold, ran an uncapped 8-persona fan-out, and the
round-2 review died at its cost cap having written no envelope. The lane's own
doctrine guarantees the failure repeats -- the repro test IS the proof, so it is
routinely larger than the fix, so a total-line threshold is disabled on nearly
every bugfix.

ce-code-review's catalog already sizes reviews "excluding test files, generated
files, and lockfiles". These tests pin every lane to that same basis."""
import unittest
from pathlib import Path
import yaml

ARCHON = Path(__file__).resolve().parents[2]
LANES = ["bugfix", "bugfix-lite", "full-sdlc-api", "full-sdlc-web",
         "full-sdlc-api-lite"]
OVERLAY = ARCHON / "setup/lite/bugfix/review-loop.review.prompt.md"


def review_prompts(text):
    """Every review-ish prompt in a workflow, found structurally.

    The review node lives at nodes[N].loop_group.nodes[M], so this recurses
    over the whole document rather than guessing container keys -- a walker
    that only knew "nodes" and "body" silently found nothing here."""
    out = []

    def walk(o):
        if isinstance(o, dict):
            if "review" in str(o.get("id", "")) and o.get("prompt"):
                out.append((o["id"], o["prompt"], o))
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(yaml.safe_load(text))
    return out


class ReviewSizingTest(unittest.TestCase):
    def _sizing_prompts(self, lane):
        p = ARCHON / f"workflows/{lane}.yaml"
        got = [(i, pr) for i, pr, _ in review_prompts(p.read_text())
               if "cap the conditional personas" in pr]
        self.assertTrue(got, f"{lane}: no persona-sizing prompt found")
        return got

    def test_every_lane_sizes_on_production_churn(self):
        for lane in LANES:
            for nid, pr in self._sizing_prompts(lane):
                self.assertIn("PRODUCTION churn", pr,
                              f"{lane}/{nid} does not size on production churn")
                self.assertIn("150 production lines", pr,
                              f"{lane}/{nid} threshold is not in production lines")

    def test_no_lane_counts_test_lines_toward_the_cap(self):
        # The negative control for the line above: the exact superseded rule
        # must not survive anywhere, in any lane or overlay, generated twins
        # included -- a twin regenerated from a stale parent is the likeliest
        # way this silently comes back.
        for p in list((ARCHON / "workflows").glob("*.yaml")) + [OVERLAY]:
            self.assertNotIn("150 changed lines", p.read_text(),
                             f"{p.name} still sizes personas on total lines")

    def test_sizing_names_the_excluded_file_classes(self):
        # "production churn" is only actionable if the prompt says what that
        # excludes; a reviewer that has to guess will guess "tests count".
        for lane in LANES:
            for nid, pr in self._sizing_prompts(lane):
                for token in ("test/spec files", "generated files", "lockfiles"):
                    self.assertIn(token, pr, f"{lane}/{nid} omits {token!r}")

    def test_lite_overlay_is_the_source_for_the_lite_lane(self):
        # bugfix-lite.yaml is generated; fixing only the generated file is
        # undone by the next derive. Pin the overlay itself.
        self.assertIn("PRODUCTION churn", OVERLAY.read_text())

    def test_review_budget_clears_a_measured_full_round(self):
        # A full 8-persona round on a ~230-line diff measured ~$10 (run
        # 38d72218: round 1 finished just under it, round 2 reached it at
        # 630s). A cap set AT the typical cost fails about half its rounds,
        # and a review killed mid-fan-out writes no envelope -- the round is
        # billed and lost. Require headroom over that measurement.
        found = [n for _, _, n in
                 review_prompts((ARCHON / "workflows/bugfix.yaml").read_text())
                 if n.get("id") == "review"]
        self.assertEqual(len(found), 1)
        self.assertGreaterEqual(found[0].get("maxBudgetUsd", 0), 15)


class ApiLaneCapsClearMeasuredCosts(unittest.TestCase):
    """archon.db node costs for full-sdlc-api, max observed per node as of
    2026-09-16: plan-critic hit its $4 cap (4.09), fixer hit $5, ralplan hit $3
    on a two-repository plan (f07acb10/733e9012), review peaked at 13.04 of 15,
    docreview 5.48, plan-revise 2.67, plan-render 2.19. Those peaks are
    single-repository runs; a joint plan carries every selected repo's files,
    tests and contracts through the same planning nodes, and archon has no
    per-run cap scaling. Require ~1.5x headroom over each measured peak."""

    PEAKS = {"ralplan": 3.12, "plan-critic": 4.09, "plan-revise": 2.67, "docreview": 5.48,
             "plan-render": 2.19, "review": 13.04, "fixer": 5.0, "implement": 3.09,
             # 343ad00e (joint verify-only stage): reader-audit hit its $3 cap.
             "reader-audit": 3.0}

    def test_caps_have_headroom_over_measured_peaks(self):
        caps = {}

        def walk(o):
            if isinstance(o, dict):
                if "maxBudgetUsd" in o:
                    caps[o["id"]] = o["maxBudgetUsd"]
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(yaml.safe_load((ARCHON / "workflows/full-sdlc-api.yaml").read_text()))
        for node, peak in self.PEAKS.items():
            with self.subTest(node=node):
                self.assertGreaterEqual(caps[node], round(peak * 1.5, 2))


class ReviewMustFinishItsFanOut(unittest.TestCase):
    """Run 38d72218 round 3: the review node returned while its personas were
    still running (archon logged dag.node_result_with_live_background_tasks).
    The envelope stopped mid-persona, review-summary.json read {"verdict": ""},
    and the round was billed for a decision it never made."""

    def test_every_lane_forbids_returning_mid_fan_out(self):
        for lane in LANES:
            path = ARCHON / f"workflows/{lane}.yaml"
            got = [(i, pr) for i, pr, _ in review_prompts(path.read_text())
                   if "cap the conditional personas" in pr]
            self.assertTrue(got, f"{lane}: no review prompt found")
            for nid, pr in got:
                self.assertIn("Do not return until every persona", pr,
                              f"{lane}/{nid}: review may return with personas still running")
                self.assertIn("is not a completed review", pr,
                              f"{lane}/{nid}: no rule that a truncated fan-out is incomplete")


if __name__ == "__main__":
    unittest.main()
