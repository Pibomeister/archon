#!/usr/bin/env python3
"""setup/mcp-smoke.sh against a stub HTTP server.

The real server needs `pnpm build` and a node toolchain, so these substitute a
stub through MCP_SMOKE_START_CMD -- the one documented override, honoured only
when set. What is exercised is the script's own logic: the readiness poll, the
child-liveness check, and both halves of the 401 assertion (status AND the
WWW-Authenticate header, since a 401 without it is what a crashed proxy returns).
"""
import os
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

SETUP = Path(__file__).resolve().parent.parent
SCRIPT = SETUP / "mcp-smoke.sh"
PASS_LINE = "SMOKE=PASS mcp-boot=ok favicon=200 unauth-mcp=401"

STUB = '''
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

MODE = os.environ["STUB_MODE"]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path != "/favicon.ico":
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/x-icon")
        self.send_header("Content-Length", "1")
        self.end_headers()
        self.wfile.write(b"\\x00")

    def do_POST(self):
        if MODE == "unauth-200":
            self.send_response(200)
        elif MODE == "no-www-authenticate":
            self.send_response(401)
        else:
            self.send_response(401)
            self.send_header(
                "WWW-Authenticate", 'Bearer resource_metadata="https://stub/prm"')
        self.send_header("Content-Length", "0")
        self.end_headers()


HTTPServer(("127.0.0.1", int(os.environ["PORT"])), H).serve_forever()
'''


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class McpSmokeTest(unittest.TestCase):
    def run_smoke(self, mode="ok", start_cmd=None, args=None):
        """Returns (completed_process, smoke-result.txt or '', seconds)."""
        with tempfile.TemporaryDirectory() as td:
            wt, ad = Path(td) / "wt", Path(td) / "ad"
            wt.mkdir()
            ad.mkdir()
            stub = wt / "stub.py"
            stub.write_text(STUB, encoding="utf-8")
            port = free_port()
            env = dict(os.environ)
            env["STUB_MODE"] = mode
            env["MCP_SMOKE_START_CMD"] = start_cmd or f"exec python3 {stub}"
            argv = ["bash", str(SCRIPT), str(wt), str(ad), str(port)]
            if args is not None:
                argv = ["bash", str(SCRIPT), *args]
            started = time.monotonic()
            r = subprocess.run(argv, capture_output=True, encoding="utf-8",
                               env=env, timeout=180)
            elapsed = time.monotonic() - started
            result = ad / "smoke-result.txt"
            return r, (result.read_text(encoding="utf-8").strip()
                       if result.exists() else ""), elapsed

    def test_a1_a_healthy_server_passes_with_the_typed_line(self):
        r, result, _ = self.run_smoke()
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        self.assertEqual(PASS_LINE, result)
        self.assertIn(PASS_LINE, r.stdout)

    def test_a2_an_unauthenticated_200_on_mcp_fails(self):
        """The whole point of the probe: if /mcp answers a token-less POST with
        200 the auth wall is gone, and the smoke must not report PASS."""
        r, result, _ = self.run_smoke(mode="unauth-200")
        self.assertEqual(1, r.returncode, r.stdout + r.stderr)
        self.assertEqual("SMOKE=FAIL unauth /mcp code=200 expected=401", result)

    def test_a3_a_401_without_the_challenge_header_fails(self):
        """Negative control for the half that is easy to drop: the header, not
        just the status. A crashed proxy returns a bare 401."""
        r, result, _ = self.run_smoke(mode="no-www-authenticate")
        self.assertEqual(1, r.returncode, r.stdout + r.stderr)
        self.assertEqual(
            "SMOKE=FAIL unauth /mcp 401 carries no WWW-Authenticate: Bearer header",
            result)

    def test_a4_a_child_that_exits_fails_on_liveness_not_on_timeout(self):
        """A dead process must be reported as a dead process. Without the
        liveness check this burns the full 60-poll budget and reports the
        indistinguishable `favicon code=000`."""
        r, result, elapsed = self.run_smoke(start_cmd="exit 7")
        self.assertEqual(1, r.returncode, r.stdout + r.stderr)
        self.assertIn("SMOKE=FAIL mcp-boot exited before ready", result)
        self.assertNotIn("favicon code=", result)
        self.assertLess(elapsed, 30, "took the timeout path, not the liveness path")

    def test_a5_a_missing_port_is_a_typed_failure_not_a_shell_error(self):
        """R3: a run resumed from a params.json written before goodword-mcp
        declared a smoke stack passes an empty api_port."""
        with tempfile.TemporaryDirectory() as td:
            r = subprocess.run(["bash", str(SCRIPT), td, td, ""],
                               capture_output=True, encoding="utf-8", timeout=60)
        self.assertNotEqual(0, r.returncode)
        self.assertIn("SMOKE=FAIL no port", r.stderr)


if __name__ == "__main__":
    unittest.main()
