#!/usr/bin/env python3
"""Say at minute one whether this run can close its ticket.

Only ticket_disposition=RESOLVED lets a run close its Linear ticket, and
RESOLVED needs EVERY effective symptom `fixed`. A multi-track report splits its
other tracks to their own tickets -- correctly -- and separate-ticket is not
fixed, so closure is unreachable and the smoke gate asks a human to accept
residuals.

That is knowable the moment the symptom ledger is sealed. It was not said, and
the cost was a full day: ENG-3860 (3 effective symptoms) was driven through six
runs toward an unassisted PR that its own shape had ruled out before the RCA
started. ENG-3483 (1 symptom) reached a PR the same evening.

This is a NOTICE and must stay one. Multi-track reports are legitimate work that
ends at a human act; blocking them would repeat the over-blocking mistake the
capability gate already made once."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ARCHON = Path(__file__).resolve().parent.parent.parent
LANES = ["bugfix", "bugfix-codex"]


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


def intake_gate(lane):
    doc = yaml.safe_load((ARCHON / "workflows" / f"{lane}.yaml").read_text(encoding="utf-8"))
    got = [n["bash"] for n in walk(doc.get("nodes")) if n.get("id") == "intake-gate"]
    assert len(got) == 1, lane
    return got[0]


def snippet(bash):
    """The heredoc the notice runs, extracted so it can be executed directly."""
    start = bash.index('python3 - "$ARTIFACTS_DIR/symptoms.json" <<\'PY\'')
    body = bash[start:].split("\n", 1)[1]
    return body.split("\nPY", 1)[0]


class ClosureReachability(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def verdict(self, count, lane="bugfix"):
        led = self.tmp / "symptoms.json"
        led.write_text(json.dumps({
            "effective_symptoms": [{"id": f"E{i+1}"} for i in range(count)]}), encoding="utf-8")
        r = subprocess.run(["python3", "-c", snippet(intake_gate(lane)), str(led)],
                           capture_output=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_a_single_symptom_report_can_close(self):
        # ENG-3483's shape.
        self.assertIn("CLOSURE_REACHABLE=YES effective_symptoms=1", self.verdict(1))

    def test_a_multi_track_report_cannot(self):
        # ENG-3860's shape, and the sentence that would have saved a day.
        out = self.verdict(3)
        self.assertIn("CLOSURE_REACHABLE=NO effective_symptoms=3", out)
        self.assertIn("residual acceptance", out)

    def test_it_says_why_rather_than_only_what(self):
        # A notice that states a verdict without its reason gets ignored. Assert
        # the two facts an operator needs to act: what RESOLVED requires, and
        # what will therefore happen at the smoke gate.
        out = " ".join(self.verdict(2).split())
        self.assertIn("every symptom `fixed`", out)
        self.assertIn("human residual acceptance", out)

    def test_it_is_a_notice_not_a_gate(self):
        # The load-bearing property. A multi-track run must still proceed.
        for n in (2, 3, 7):
            with self.subTest(n=n):
                led = self.tmp / "symptoms.json"
                led.write_text(json.dumps({
                    "effective_symptoms": [{"id": f"E{i+1}"} for i in range(n)]}), encoding="utf-8")
                r = subprocess.run(["python3", "-c", snippet(intake_gate("bugfix")), str(led)],
                                   capture_output=True, encoding="utf-8")
                self.assertEqual(r.returncode, 0, "the notice became a gate")

    def test_it_runs_before_the_gate_reports_pass(self):
        for lane in LANES:
            bash = intake_gate(lane)
            self.assertIn("CLOSURE_REACHABLE", bash, f"{lane}: no closure notice")
            self.assertLess(bash.index("CLOSURE_REACHABLE"), bash.index('echo "INTAKE_GATE=PASS"'),
                            f"{lane}: notice printed after the gate's own verdict")


if __name__ == "__main__":
    unittest.main()
