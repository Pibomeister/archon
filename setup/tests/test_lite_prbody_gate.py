#!/usr/bin/env python3
import subprocess
import sys
import tempfile
import unittest
import json
from pathlib import Path


SETUP = Path(__file__).resolve().parent.parent
SCRIPT = SETUP / "check-lite-prbody.py"


class LitePrbodyGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ad = Path(self.tmp.name)

    def write(self, name, text):
        (self.ad / name).write_text(text, encoding="utf-8")

    def write_json(self, name, value):
        (self.ad / name).write_text(json.dumps(value), encoding="utf-8")

    def run_gate(self, lane):
        return subprocess.run([sys.executable, str(SCRIPT), lane, str(self.ad)],
                              capture_output=True, encoding="utf-8")

    def api_fixture(self):
        envelope = "ENVELOPE triage=S OK\nROUTE=LITE"
        smoke = "SMOKE=PASS api-docs-json=200 guarded-control=401"
        self.write("envelope-post.txt", envelope)
        self.write("smoke-result.txt", smoke)
        self.write("pr-body.md", f"""Lane: full-sdlc-api-lite
## Summary
## Lane
```
{envelope}
```
Lite omissions: one code-review round; no planning critic, doc review, premise verification, deslop pass, or reader audit.
## Reviewer-unverified fixes
None: the review round landed no fixes.
## Known Residuals
None.
## Post-Deploy Monitoring & Validation
{smoke}
""")

    def bugfix_fixture(self):
        envelope = "ENVELOPE triage=S OK\nROUTE=LITE"
        smoke = "SMOKE=SKIP lane=bugfix-lite (RED->GREEN repro + negcontrol is the proof)"
        self.write("envelope-post.txt", envelope)
        self.write("smoke-result.txt", smoke)
        self.write("smoke-matrix-result.txt", "SMOKE_MATRIX=SKIP lane=bugfix-lite")
        self.write("negcontrol-postgreen.txt", "detail\nNEGCONTROL=PASS phase=postgreen")
        self.write("negcontrol-exit.txt", "detail\nNEGCONTROL=PASS phase=exit")
        self.write_json("fix-classification.json", {
            "implementation_result": "FULL_FIX",
            "ticket_disposition": "RESOLVED",
            "approval_scope": "ship-covered-symptoms",
            "ticket_closure_allowed": True,
        })
        self.write("pr-body.md", f"""Lane: bugfix-lite
## Summary
implementation_result=FULL_FIX ticket_disposition=RESOLVED approval_scope=ship-covered-symptoms ticket_closure_allowed=true
## Lane
```
{envelope}
```
Lite omissions: no production evidence, blind chain verification, planning critic, live experiment, deslop pass, HTTP smoke, or in-app smoke matrix.
## Root Cause
x
## Proof
{smoke}
SMOKE_MATRIX=SKIP lane=bugfix-lite
NEGCONTROL=PASS phase=postgreen
NEGCONTROL=PASS phase=exit
## Known Residuals
None.
## Post-Deploy Monitoring & Validation
x
""")

    def assert_fail(self, lane, reason):
        r = self.run_gate(lane)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(reason, r.stdout)

    def test_api_baseline(self):
        self.api_fixture()
        r = self.run_gate("api")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("PRBODY_GATE=PASS lane=full-sdlc-api-lite", r.stdout)

    def test_api_requires_lane_marker(self):
        self.api_fixture()
        p = self.ad / "pr-body.md"
        p.write_text(p.read_text().replace("Lane: full-sdlc-api-lite", "Lane: api"))
        self.assert_fail("api", "lane marker")

    def test_api_requires_verbatim_envelope(self):
        self.api_fixture()
        p = self.ad / "pr-body.md"
        p.write_text(p.read_text().replace("ENVELOPE triage=S OK", "ENVELOPE triage=pass"))
        self.assert_fail("api", "envelope-post.txt")

    def test_api_requires_unreviewed_fix_block(self):
        self.api_fixture()
        self.write("lite-fixes-unreviewed.txt", "fixer_commit=abc\nfiles:\n  x.ts")
        self.assert_fail("api", "lite-fixes-unreviewed.txt")

    def test_api_requires_exact_omissions(self):
        self.api_fixture()
        p = self.ad / "pr-body.md"
        p.write_text(p.read_text().replace("Lite omissions:", "Omissions:"))
        self.assert_fail("api", "lite-omissions")

    def test_bugfix_baseline(self):
        self.bugfix_fixture()
        r = self.run_gate("bugfix")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("PRBODY_GATE=PASS lane=bugfix-lite", r.stdout)

    def test_bugfix_requires_smoke_skip(self):
        self.bugfix_fixture()
        p = self.ad / "pr-body.md"
        p.write_text(p.read_text().replace("SMOKE=SKIP", "SMOKE=PASS"))
        self.assert_fail("bugfix", "smoke-result.txt")

    def test_bugfix_requires_both_negative_controls(self):
        self.bugfix_fixture()
        p = self.ad / "pr-body.md"
        p.write_text(p.read_text().replace("NEGCONTROL=PASS phase=exit", "NEGCONTROL omitted", 1))
        self.assert_fail("bugfix", "negcontrol-exit.txt")

    def test_bugfix_requires_smoke_matrix_skip(self):
        self.bugfix_fixture()
        p = self.ad / "pr-body.md"
        p.write_text(p.read_text().replace("SMOKE_MATRIX=SKIP", "SMOKE_MATRIX=PASS"))
        self.assert_fail("bugfix", "smoke-matrix-result.txt")

    def test_bugfix_non_full_classification_routes_full(self):
        self.bugfix_fixture()
        self.write_json("fix-classification.json", {
            "implementation_result": "PARTIAL_FIX",
            "ticket_disposition": "PRODUCT_DECISION_NEEDED",
            "approval_scope": "ship-covered-symptoms",
            "ticket_closure_allowed": False,
        })
        self.assert_fail("bugfix", "ROUTE=FULL reason=classification")

    def test_bugfix_requires_classification_banner(self):
        self.bugfix_fixture()
        p = self.ad / "pr-body.md"
        p.write_text(p.read_text().replace("implementation_result=FULL_FIX", "implementation_result=PARTIAL_FIX"))
        self.assert_fail("bugfix", "classification banner missing")


if __name__ == "__main__":
    unittest.main()
