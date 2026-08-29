import shlex, subprocess, sys, tempfile, os, unittest
SCRIPT = os.path.join(os.path.dirname(__file__), "..", "parse-review-envelope.py")

def raw(text):
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(text); path = f.name
    try:
        return subprocess.run([sys.executable, SCRIPT, path], capture_output=True, text=True).stdout
    finally:
        os.unlink(path)

def run(text):
    # Consume the output the way the gate does: shell-eval it, then read the vars back.
    out = raw(text)
    sh = out + '\nprintf "%s\\n" "G1=$G1" "G2=$G2" "VERDICT=$VERDICT" "VSRC=$VSRC"'
    p = subprocess.run(["bash", "-u", "-c", sh], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return dict(l.split("=", 1) for l in p.stdout.strip().splitlines())

class ParseReviewEnvelope(unittest.TestCase):
    def test_persona_json_does_not_hijack_text_envelope(self):
        # Live shape from run d3aa3b55: a persona block {"status": "ok"} sits inside the prose
        text = 'Findings:\n{"reviewer": "correctness", "findings": []}\nlens: {"status": "ok", "notes": []}\n\nVerdict: Ready to merge\n\nReview complete\n'
        r = run(text)
        self.assertEqual(r["G1"], "PASS"); self.assertEqual(r["VERDICT"], "Ready to merge"); self.assertEqual(r["VSRC"], "envelope")
    def test_agent_json_envelope_still_parses(self):
        r = run('{"status": "complete", "verdict": "Not ready", "actionable_findings": []}')
        self.assertEqual(r["G1"], "PASS"); self.assertEqual(r["VERDICT"], "Not ready"); self.assertEqual(r["VSRC"], "json")
    def test_embedded_real_envelope_wins(self):
        r = run('noise {"status": "ok"} then {"status": "degraded", "verdict": "Ready with fixes"} end')
        self.assertEqual(r["G2"], "FAIL"); self.assertEqual(r["VERDICT"], "Ready with fixes")
    def test_verdict_values_are_shell_quoted_for_eval(self):
        # Every enum verdict contains a space; the gate does eval "$(parser | sed 's/^/ENV_/')".
        # Unquoted output ran the command `with` and left ENV_VERDICT unset (live, d3aa3b55 round 2).
        for v in ("Ready to merge", "Ready with fixes", "Not ready"):
            for text in (f"Verdict: {v}\n\nReview complete\n", '{"status": "complete", "verdict": "%s"}' % v):
                out = raw(text)
                line = [l for l in out.splitlines() if l.startswith("VERDICT=")][0]
                self.assertEqual(line, "VERDICT=" + shlex.quote(v))
                self.assertEqual(run(text)["VERDICT"], v)
        self.assertIn("VERDICT=''", raw("nothing here"))

    def test_no_signal_fails_closed(self):
        r = run("just prose, nothing terminal")
        self.assertEqual(r["G1"], "FAIL"); self.assertEqual(r["VERDICT"], "")

if __name__ == "__main__":
    unittest.main()
