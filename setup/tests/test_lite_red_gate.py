#!/usr/bin/env python3
"""bugfix-lite red-gate overlay: the mechanical tautology guard.

The overlay is the parent's red-gate plus one grep that runs BEFORE the repro
command is spent. This test extracts exactly that guard (the TAUT= line and its
test) and runs it against fixture spec files, so the guard's regex is what is
under test, not a re-typed copy of it."""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

OVERLAY = Path(__file__).resolve().parent.parent / "lite" / "bugfix" / "red-gate.bash.sh"


def guard_script():
    src = OVERLAY.read_text(encoding="utf-8")
    m = re.search(r"^TAUT=.*$\n^test -z \"\$TAUT\".*$", src, re.M)
    assert m, "guard lines not found in overlay"
    return "set -uo pipefail\nWT=\"$1\"; TESTF=\"$2\"\n" + m.group(0) + "\necho RED_GATE_TAUT=OK\n"


class TautologyGuard(unittest.TestCase):
    def setUp(self):
        if not OVERLAY.is_file():
            self.skipTest("overlay not built yet")
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.script = self.tmp / "guard.sh"
        self.script.write_text(guard_script(), encoding="utf-8")

    def check(self, body):
        (self.tmp / "x.spec.ts").write_text(body, encoding="utf-8")
        return subprocess.run(["bash", str(self.script), str(self.tmp), "x.spec.ts"], capture_output=True, encoding="utf-8")

    def test_honest_test_passes(self):
        r = self.check("describe('dedupe', () => {\n  it('keeps one attendee per key', async () => {\n    const out = await svc.run(input);\n    expect(out.attendees).toHaveLength(1);\n  });\n});\n")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("RED_GATE_TAUT=OK", r.stdout)

    def test_it_skip(self):
        r = self.check("it.skip('x', () => { expect(a).toBe(b); });\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("RED_GATE=FAIL tautology-marker", r.stdout)

    def test_only(self):
        r = self.check("describe.only('x', () => {});\n")
        self.assertEqual(r.returncode, 1)

    def test_todo(self):
        r = self.check("it.todo('write this later');\n")
        self.assertEqual(r.returncode, 1)

    def test_literal_expect(self):
        r = self.check("it('x', () => { expect(true).toBe(true); });\n")
        self.assertEqual(r.returncode, 1)

    def test_bare_truthy_on_own_line(self):
        r = self.check("it('x', () => {\n  expect(1).toBeTruthy();\n});\n")
        self.assertEqual(r.returncode, 1)

    def test_trailing_truthy_is_refused_by_design(self):
        # any line ending in toBeTruthy(); is refused on this lane: use a concrete matcher
        r = self.check("it('x', () => {\n  const row = find(rows);\n  expect(row?.email).toBeTruthy();\n});\n")
        self.assertEqual(r.returncode, 1)

    def test_empty_it_body(self):
        r = self.check("it('x');\n")
        self.assertEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main()
