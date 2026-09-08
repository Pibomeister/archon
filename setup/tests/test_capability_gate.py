#!/usr/bin/env python3
"""The capability gate must fire on the reports that actually exist.

It keyed solely on `retrievable_by: probe` tags in the report's `## Intake
gaps`. No report carries them -- the archon-linear snapshot writes bare
absences -- so the gate was decoration. Run 38d72218 passed it at 14:24Z with
prod-db UNAVAILABLE (`aws-session-expired`) and then spent four hours reaching a
smoke gate whose outcome was already determined: the customer's import was
never identified, so its symptom could only ever be class-hardening-only, and
shipping needed a human to accept that.

Stopping at minute one costs an `aws login`. Not stopping costs the run."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ARCHON = Path(__file__).resolve().parent.parent.parent

# Verbatim from run 38d72218's bug-report.md: bare absences, no tags.
REAL_REPORT = """## Intake gaps

- Referenced Mesh CSV is not attached.
- Customer account, import ID, and approximate import time are missing.
- Specific contacts expected to have notes are missing.
- Sales Navigator onboarding screenshot and app version are missing.

## Classification

`defect`
"""


def walk(nodes):
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        yield n
        for key in ("loop_group", "body"):
            v = n.get(key)
            if isinstance(v, dict):
                yield from walk(v.get("nodes"))
            elif isinstance(v, list):
                yield from walk(v)


def gate_bash():
    doc = yaml.safe_load((ARCHON / "workflows" / "bugfix.yaml").read_text(encoding="utf-8"))
    return [n for n in walk(doc["nodes"]) if n.get("id") == "capability-gate"][0]["bash"]


class CapabilityGate(unittest.TestCase):
    def setUp(self):
        self.ad = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.ad, ignore_errors=True)

    def write(self, report, prod_db="UNAVAILABLE", reason="aws-session-expired detail here"):
        (self.ad / "bug-report.md").write_text(report, encoding="utf-8")
        (self.ad / "capabilities.json").write_text(json.dumps(
            {"schema_version": 2,
             "capabilities": {"prod-db": {"status": prod_db, "reason": reason}}}), encoding="utf-8")

    def run_gate(self):
        return subprocess.run(["bash", "-c", gate_bash()], capture_output=True, encoding="utf-8",
                              env={**os.environ, "ARTIFACTS_DIR": str(self.ad)})

    def test_the_real_untagged_report_stops_the_run(self):
        self.write(REAL_REPORT)
        r = self.run_gate()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("CAPABILITY_GATE=FAIL capability-required reason=aws-session-expired", r.stdout)
        # One match is enough to block, and one is what this excerpt has:
        # "Customer account, import ID, and approximate import time are
        # missing." The CSV, the contacts line and the screenshot name nothing
        # a query resolves, which is the point -- the rule stays narrow.
        self.assertIn("probe_gaps=1", r.stdout)
        self.assertIn("aws login", r.stdout)

    def test_the_same_report_passes_when_the_capability_is_there(self):
        # Negative control on the capability half: the gate must key on the
        # database being reachable, not merely on the report having gaps.
        self.write(REAL_REPORT, prod_db="AVAILABLE", reason="reachable")
        r = self.run_gate()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("CAPABILITY_GATE=PASS", r.stdout)

    def test_gaps_no_query_could_answer_do_not_block(self):
        # A local-only repro must still run with zero external evidence. A
        # missing screenshot names nothing a database can resolve.
        self.write("""## Intake gaps

- Sales Navigator onboarding screenshot is not attached.
- The reporter did not say which browser they used.

## Classification

`defect`
""")
        r = self.run_gate()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("probe_gaps=0", r.stdout)

    def test_a_feature_is_never_blocked_on_prod_evidence(self):
        self.write(REAL_REPORT.replace("`defect`", "`api-feature`"))
        r = self.run_gate()
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_a_report_with_no_gaps_section_passes(self):
        self.write("## Classification\n\n`defect`\n")
        r = self.run_gate()
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_the_tagged_form_still_wins(self):
        # Tagged reports keep their exact semantics; the untagged path is a
        # fallback, not a replacement.
        self.write("""## Intake gaps

- gap: which screenshot, retrievable_by: probe

## Classification

`defect`
""")
        r = self.run_gate()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("probe_gaps=1", r.stdout)

    def test_a_probe_code_gap_never_needs_an_aws_session(self):
        # The code graph and the repository answer these; prod Postgres cannot.
        # Measured 2026-09-07: ENG-3549 died here on an expired AWS session for
        # "exact user-visible surface", a gap its own Linear triage comment had
        # already answered from code (nightly-reminder-generation-handler.ts:423,
        # reminder.controller.ts:687). A full relaunch plus an `aws login` bought
        # nothing.
        self.write("""## Intake gaps

- Exact user-visible surface: the reminders list in web-app vs goodword-mcp — retrievable_by: probe-code

## Classification

`defect`
""")
        r = self.run_gate()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("probe_gaps=0", r.stdout)

    def test_a_probe_prod_gap_still_stops_the_run(self):
        # The other half of the split: naming a timestamp or an id IS what the
        # gate exists for. ENG-3842 was blocked on exactly this and was right.
        self.write("""## Intake gaps

- No timestamps beyond the report time, so the send cannot be located in logs — retrievable_by: probe-prod

## Classification

`defect`
""")
        r = self.run_gate()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("probe_gaps=1", r.stdout)

    def test_a_probe_code_gap_does_not_fall_through_to_the_untagged_heuristic(self):
        # NEGATIVE CONTROL for the fallback guard, and the bug the first attempt
        # at this change shipped: with the guard keyed on an empty result rather
        # than on the absence of tags, a fully-tagged probe-code report fell
        # through to LOCATING, whose `user` matches "user-visible surface" -- so
        # the gate blocked anyway and the fix looked like it worked in review.
        self.write("""## Intake gaps

- Whether the signed-in user's own row is returned — retrievable_by: probe-code
- No repository stated; the symptom is user-visible — retrievable_by: probe-code

## Classification

`defect`
""")
        r = self.run_gate()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("probe_gaps=0", r.stdout)

    def test_one_probe_prod_gap_among_probe_code_gaps_still_blocks(self):
        self.write("""## Intake gaps

- Exact user-visible surface — retrievable_by: probe-code
- Affected account id and send timestamp unknown — retrievable_by: probe-prod

## Classification

`defect`
""")
        r = self.run_gate()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("probe_gaps=1", r.stdout)

    def test_a_report_with_a_reproduction_is_never_blocked(self):
        # The plan's own constraint: a local-only repro must run with zero
        # external evidence. Without this the gate becomes the thing that stops
        # locally reproducible bug fixes whenever AWS happens to be down --
        # worse than the decoration it replaced.
        self.write(REAL_REPORT + """
## Repro

bun run test -- commit-import-note-resolution
""")
        r = self.run_gate()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("probe_gaps=0", r.stdout)

    def test_the_heading_real_tickets_use_also_exempts(self):
        # Archon's lite lane asks for "## Repro". Every Linear bug report in
        # this workspace writes "### Steps to Reproduce" instead, so matching
        # only the first meant the exemption could never fire on the population
        # it exists for. Verbatim shape from ENG-3483.
        self.write(REAL_REPORT + """
### Steps to Reproduce

Open AI Chat, view the ranked results, and load more results in the same chat.
""")
        r = self.run_gate()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("probe_gaps=0", r.stdout)

    def test_classification_is_still_read_under_a_deeper_heading(self):
        # Loosening the section matcher must not break the sections it already
        # read: a report whose Classification sits under ### still classifies.
        self.write(REAL_REPORT.replace("## Classification", "### Classification"))
        r = self.run_gate()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("capability-required", r.stdout)

    def test_an_empty_repro_heading_does_not_exempt(self):
        # Negative control: the heading alone is not a reproduction, or every
        # report could opt out of the gate by writing four characters.
        self.write(REAL_REPORT + "\n## Repro\n\n")
        r = self.run_gate()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("capability-required", r.stdout)

    def test_a_missing_capabilities_file_is_a_typed_stop(self):
        (self.ad / "bug-report.md").write_text(REAL_REPORT, encoding="utf-8")
        r = self.run_gate()
        self.assertEqual(r.returncode, 1)
        self.assertIn("CAPABILITY_GATE=FAIL no capabilities.json", r.stdout)


if __name__ == "__main__":
    unittest.main()
