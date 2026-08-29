import subprocess, sys, tempfile, os, unittest
SCRIPT = os.path.join(os.path.dirname(__file__), "..", "parse-review-envelope.py")

def run(text):
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(text); path = f.name
    try:
        out = subprocess.run([sys.executable, SCRIPT, path], capture_output=True, text=True)
        return dict(l.split("=", 1) for l in out.stdout.strip().splitlines())
    finally:
        os.unlink(path)

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
    def test_no_signal_fails_closed(self):
        r = run("just prose, nothing terminal")
        self.assertEqual(r["G1"], "FAIL"); self.assertEqual(r["VERDICT"], "")

if __name__ == "__main__":
    unittest.main()
