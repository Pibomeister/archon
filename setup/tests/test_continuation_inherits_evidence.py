#!/usr/bin/env python3
"""A successor must inherit the evidence, not just the conclusions.

Every artifact the continuation bundle carried was model-authored: the causal
chain, the hypotheses, the assessments, the RCA prose, a failed patch. None of
it was the retrieved data. So a successor received the conclusion that had been
proven wrong and nothing that proved it wrong, re-derived from source code, and
produced another code-derived hypothesis.

Observed twice in a row on one ticket. Run dd962f9d died at
`RCA_GATE=FAIL PROBE_CONFLICT` because production showed all 24 notes of the
identified import carried a non-null about_id, refuting its stranded-note
mechanism. Its successor 4f3691f4 inherited that refuted chain WITHOUT
probe-results.txt, hypothesised a different mechanism -- that Note creation is
restricted to rows with a free-text notes-style column -- and died at the same
gate when the retrieved mapping showed the Note target reading from a
structured "Organization Name" column."""
import importlib.util
import unittest
from pathlib import Path

ARCHON = Path(__file__).resolve().parent.parent.parent
spec = importlib.util.spec_from_file_location("archon_run", ARCHON / "setup" / "archon-run.py")
ar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ar)

EVIDENCE = ("probe.json", "probe-results.txt", "reassess.md", "occurrence-window.json")


class ContinuationInheritsEvidence(unittest.TestCase):
    def test_retrieved_evidence_is_carried(self):
        for name in EVIDENCE:
            self.assertIn(name, ar.CONTINUATION_ARTIFACTS,
                          f"a successor cannot read {name}, so it re-derives blind")

    def test_the_probe_rows_themselves_are_carried(self):
        # The single most important one: the rows, not a summary of them. A
        # successor reasoning from prose about the data is reasoning from a
        # model's reading of it, which is what failed.
        self.assertIn("probe-results.txt", ar.CONTINUATION_ARTIFACTS)

    def test_the_model_authored_set_is_unchanged(self):
        # Negative control on scope: adding evidence must not have dropped the
        # diagnosis lineage a successor also needs.
        for name in ("symptoms.json", "symptoms.seal.json", "causal-chain.json",
                     "hypotheses.json", "rca.md", "evidence-manifest.json"):
            self.assertIn(name, ar.CONTINUATION_ARTIFACTS, name)

    def test_no_diagnosis_artifact_was_smuggled_in(self):
        # Inheriting evidence must not become a way to inherit a VERDICT. These
        # are the frozen-diagnosis and approval artifacts; a successor must
        # re-derive them, and rca-gate's frozen-diagnosis check depends on it.
        for forbidden in ("imm-rca.md", "imm-causal-chain.json", "reassess-pre.sha256",
                          "accept-residuals.txt", "files-allowlist.json", "fix-plan.json",
                          "symptom-dispositions.json"):
            self.assertNotIn(forbidden, ar.CONTINUATION_ARTIFACTS, forbidden)

    def test_the_set_has_no_duplicates(self):
        self.assertEqual(len(ar.CONTINUATION_ARTIFACTS), len(set(ar.CONTINUATION_ARTIFACTS)))


if __name__ == "__main__":
    unittest.main()
