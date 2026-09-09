#!/usr/bin/env python3
"""Unsealed browser observations are diagnostics, never release authority."""
import binascii
import hashlib
import struct
import zipfile
import zlib
import json
import subprocess
import sys
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

SETUP = Path(__file__).resolve().parents[1]
CHECK = SETUP / "check-browser-evidence.py"
VERIFIER = SETUP / "browser-verifier.py"
ROOT = SETUP.parents[1]
WEB_APP = ROOT / "web-app"
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_T = "d" * 40


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def file_digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def png_bytes(width=1, height=1):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"\x00" + b"\xff\xff\xff" * width
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def write_trace(path):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("trace.trace", "{}")


class TextHandler(BaseHTTPRequestHandler):
    body = b"<html><title>Other</title><body>Other origin</body></html>"

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *_args):
        return


def start_server(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}"


def stop_server(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def run_verifier(ad, wt, timeout_ms="2000"):
    return subprocess.run([sys.executable, str(VERIFIER), "run", "--artifacts", str(ad),
                           "--web-worktree", str(wt), "--playwright-root", str(WEB_APP),
                           "--candidate-commit", SHA_A, "--candidate-tree", SHA_T, "--api-commit", SHA_B,
                           "--timeout-ms", str(timeout_ms)],
                          capture_output=True, encoding="utf-8", timeout=90)


class BrowserEvidenceContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ad = Path(self.tmp.name)
        self.req = {
            "required": [{
                "id": "browser-1",
                "criterion": "Welcome text is visible",
                "path": "/",
                "assertions": [{"type": "text", "value": "Welcome"}],
            }]
        }
        write_json(self.ad / "browser-evidence.json", self.req)
        (self.ad / "browser-evidence.sha256").write_text(digest(self.req) + "\n", encoding="utf-8")
        (self.ad / "smoke-urls.txt").write_text("web=http://localhost:4173\napi=http://localhost:4127\n", encoding="utf-8")
        ev = self.ad / "browser-evidence"
        ev.mkdir()
        (ev / "browser-1.png").write_bytes(png_bytes())
        write_trace(ev / "trace.zip")
        self.receipt = {
            "receipt_version": 1,
            "authority": "controller-playwright",
            "authority_state": "unsealed-local-receipt",
            "controller_action": "finalize-evidence",
            "runner": "archon-browser-verifier",
            "status": "passed",
            "requirements_digest": digest(self.req),
            "origin": "http://localhost:4173",
            "viewport": {"width": 1280, "height": 720},
            "candidate": {"commit": SHA_A, "tree": SHA_T, "worktree": "/tmp/wt"},
            "api": {"commit": SHA_B, "origin": "http://localhost:4127", "handoff_digest": "c" * 64},
            "criteria": [{
                "id": "browser-1",
                "path": "/",
                "status": "passed",
                "assertions": [{"type": "text", "value": "Welcome", "status": "passed"}],
            }],
            "evidence": {
                "screenshots": [{"criterion": "browser-1", "path": "browser-evidence/browser-1.png", "sha256": file_digest(ev / "browser-1.png")}],
                "traces": [{"path": "browser-evidence/trace.zip", "sha256": file_digest(ev / "trace.zip")}],
            },
        }
        write_json(self.ad / "browser-verifier-receipt.json", self.receipt)

    def tearDown(self):
        self.tmp.cleanup()

    def run_check(self):
        return subprocess.run([sys.executable, str(CHECK), str(self.ad / "browser-evidence.json"),
                               str(self.ad / "browser-verifier-receipt.json"), str(self.ad / "smoke-urls.txt")],
                              capture_output=True, encoding="utf-8")

    def mutate_receipt(self, fn):
        data = json.loads((self.ad / "browser-verifier-receipt.json").read_text())
        fn(data)
        write_json(self.ad / "browser-verifier-receipt.json", data)

    def test_valid_unsealed_receipt_remains_advisory(self):
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("BROWSER_EVIDENCE=ADVISORY criteria=1 authority=none", result.stdout)
        self.assertNotIn("UAT_PASSED", result.stdout)

    def test_agent_authored_legacy_json_cannot_authorize(self):
        write_json(self.ad / "browser-verifier-receipt.json", {
            "passed": [{"criterion": "browser-1", "detail": "looked fine"}],
            "failed": [],
            "evidence": [str(self.ad / "browser-evidence/browser-1.png")],
        })
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not an advisory Playwright observation", result.stdout)

    def test_modified_acceptance_checks_stale_the_receipt(self):
        req = dict(self.req)
        req["required"] = [dict(self.req["required"][0], assertions=[{"type": "text", "value": "Changed"}])]
        write_json(self.ad / "browser-evidence.json", req)
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("browser-evidence.json does not match approved browser policy digest", result.stdout)

    def test_supplied_screenshot_path_without_matching_digest_is_rejected(self):
        (self.ad / "browser-evidence" / "browser-1.png").write_bytes(png_bytes(width=2))
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("screenshot evidence digest mismatch", result.stdout)

    def test_one_byte_fake_screenshot_with_matching_digest_is_rejected(self):
        shot = self.ad / "browser-evidence" / "browser-1.png"
        shot.write_bytes(b"x")
        self.mutate_receipt(lambda data: data["evidence"]["screenshots"][0].update(sha256=file_digest(shot)))
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("screenshot evidence is not a PNG", result.stdout)

    def test_invalid_zip_trace_with_matching_digest_is_rejected(self):
        trace = self.ad / "browser-evidence" / "trace.zip"
        trace.write_bytes(b"not a zip")
        self.mutate_receipt(lambda data: data["evidence"]["traces"][0].update(sha256=file_digest(trace)))
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("trace evidence is not a zip", result.stdout)

    def test_bogus_candidate_tree_is_rejected(self):
        self.mutate_receipt(lambda data: data["candidate"].update(tree="tree"))
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("candidate tree identity", result.stdout)

    def test_unbounded_viewport_is_rejected(self):
        self.mutate_receipt(lambda data: data.update(viewport={"width": 0, "height": 720}))
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("viewport out of bounds", result.stdout)

    def test_skipped_required_criterion_is_rejected(self):
        def skip(data):
            data["criteria"][0]["status"] = "skipped"
        self.mutate_receipt(skip)
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("required criterion did not pass", result.stdout)

    def test_wrong_origin_is_rejected(self):
        self.mutate_receipt(lambda data: data.update(origin="http://evil.example"))
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("origin does not match smoke origin", result.stdout)

    def test_self_authored_controller_seal_cannot_authorize(self):
        self.mutate_receipt(lambda data: data.update(controller_seal={"action": "finalize-evidence", "digest": "f" * 64}))
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot self-author controller authentication", result.stdout)

    def test_required_criteria_need_frozen_assertions(self):
        bad_req = {"required": [{"id": "browser-1", "criterion": "Welcome", "path": "/"}]}
        write_json(self.ad / "browser-evidence.json", bad_req)
        (self.ad / "browser-evidence.sha256").write_text(digest(bad_req) + "\n", encoding="utf-8")
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("needs frozen assertions", result.stdout)


class BrowserVerifierPlaywrightTest(unittest.TestCase):
    def test_playwright_helper_executes_against_isolated_local_origin(self):
        if not (WEB_APP / "node_modules" / "playwright").is_dir():
            self.skipTest("web-app Playwright dependency is not installed")
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            site = root / "site"
            ad = root / "artifacts"
            wt = root / "wt"
            site.mkdir()
            ad.mkdir()
            wt.mkdir()
            (site / "index.html").write_text("""<html><title>Verifier</title><body><button onclick="document.body.append('Clicked')">Press</button></body></html>""", encoding="utf-8")
            req = {"required": [{"id": "browser-1", "criterion": "click reveals text", "path": "/", "assertions": [{"type": "click", "value": "button"}, {"type": "text", "value": "Clicked"}]}]}
            write_json(ad / "browser-evidence.json", req)
            (ad / "browser-evidence.sha256").write_text(digest(req) + "\n", encoding="utf-8")
            write_json(ad / "params.json", {"api_head_sha": SHA_B})
            write_json(ad / "api-handoff.json", {"api_head_sha": SHA_B})
            handler = lambda *args, **kwargs: SimpleHTTPRequestHandler(*args, directory=str(site), **kwargs)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                origin = f"http://127.0.0.1:{server.server_port}"
                (ad / "smoke-urls.txt").write_text(f"web={origin}\napi=http://localhost:4127\n", encoding="utf-8")
                result = subprocess.run([sys.executable, str(VERIFIER), "run", "--artifacts", str(ad),
                                         "--web-worktree", str(wt), "--playwright-root", str(WEB_APP),
                                         "--candidate-commit", SHA_A, "--candidate-tree", SHA_T, "--api-commit", SHA_B],
                                        capture_output=True, encoding="utf-8", timeout=90)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            receipt = json.loads((ad / "browser-verifier-receipt.json").read_text())
            self.assertEqual(receipt["status"], "passed")
            check = subprocess.run([sys.executable, str(CHECK), str(ad / "browser-evidence.json"),
                                    str(ad / "browser-verifier-receipt.json"), str(ad / "smoke-urls.txt")],
                                   capture_output=True, encoding="utf-8")
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

    def test_playwright_helper_records_real_failure_observation(self):
        if not (WEB_APP / "node_modules" / "playwright").is_dir():
            self.skipTest("web-app Playwright dependency is not installed")
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            site = root / "site"
            ad = root / "artifacts"
            wt = root / "wt"
            site.mkdir()
            ad.mkdir()
            wt.mkdir()
            (site / "index.html").write_text("<html><body><button>Press</button></body></html>", encoding="utf-8")
            req = {"required": [{"id": "browser-1", "criterion": "missing text", "path": "/", "assertions": [{"type": "text", "value": "Never Appears"}]}]}
            write_json(ad / "browser-evidence.json", req)
            (ad / "browser-evidence.sha256").write_text(digest(req) + "\n", encoding="utf-8")
            write_json(ad / "params.json", {"api_head_sha": SHA_B})
            write_json(ad / "api-handoff.json", {"api_head_sha": SHA_B})
            handler = lambda *args, **kwargs: SimpleHTTPRequestHandler(*args, directory=str(site), **kwargs)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                origin = f"http://127.0.0.1:{server.server_port}"
                (ad / "smoke-urls.txt").write_text(f"web={origin}\napi=http://localhost:4127\n", encoding="utf-8")
                result = subprocess.run([sys.executable, str(VERIFIER), "run", "--artifacts", str(ad),
                                         "--web-worktree", str(wt), "--playwright-root", str(WEB_APP),
                                         "--candidate-commit", SHA_A, "--candidate-tree", SHA_T, "--api-commit", SHA_B,
                                         "--timeout-ms", "1000"],
                                        capture_output=True, encoding="utf-8", timeout=90)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            receipt = json.loads((ad / "browser-verifier-receipt.json").read_text())
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(receipt["criteria"][0]["status"], "failed")
            self.assertIn("error", receipt["criteria"][0]["assertions"][0])

    def test_playwright_helper_marks_redirect_to_other_origin_failed(self):
        if not (WEB_APP / "node_modules" / "playwright").is_dir():
            self.skipTest("web-app Playwright dependency is not installed")
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            site = root / "site"
            ad = root / "artifacts"
            wt = root / "wt"
            site.mkdir(); ad.mkdir(); wt.mkdir()
            other_server, other_thread, other_origin = start_server(TextHandler)

            class RedirectHandler(BaseHTTPRequestHandler):
                def do_GET(self):
                    if self.path == "/redirect":
                        self.send_response(302)
                        self.send_header("Location", other_origin + "/")
                        self.end_headers()
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(b"<html><body>Home</body></html>")

                def log_message(self, *_args):
                    return

            web_server, web_thread, web_origin = start_server(RedirectHandler)
            try:
                req = {"required": [{"id": "redirect", "criterion": "redirect stays local", "path": "/redirect", "assertions": [{"type": "title", "value": "Other"}]}]}
                write_json(ad / "browser-evidence.json", req)
                (ad / "browser-evidence.sha256").write_text(digest(req) + "\n", encoding="utf-8")
                write_json(ad / "params.json", {"api_head_sha": SHA_B})
                write_json(ad / "api-handoff.json", {"api_head_sha": SHA_B})
                (ad / "smoke-urls.txt").write_text(f"web={web_origin}\napi=http://localhost:4127\n", encoding="utf-8")
                result = run_verifier(ad, wt)
            finally:
                stop_server(web_server, web_thread)
                stop_server(other_server, other_thread)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            receipt = json.loads((ad / "browser-verifier-receipt.json").read_text())
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(receipt["criteria"][0]["observed_origin"], other_origin)
            self.assertIn("navigation changed origin", receipt["criteria"][0]["error"])

    def test_playwright_helper_marks_click_to_other_origin_failed(self):
        if not (WEB_APP / "node_modules" / "playwright").is_dir():
            self.skipTest("web-app Playwright dependency is not installed")
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            site = root / "site"
            ad = root / "artifacts"
            wt = root / "wt"
            site.mkdir(); ad.mkdir(); wt.mkdir()
            other_server, other_thread, other_origin = start_server(TextHandler)
            (site / "index.html").write_text(f'<html><body><a href="{other_origin}/">Leave</a></body></html>', encoding="utf-8")
            handler = lambda *args, **kwargs: SimpleHTTPRequestHandler(*args, directory=str(site), **kwargs)
            web_server, web_thread, web_origin = start_server(handler)
            try:
                req = {"required": [{"id": "click-away", "criterion": "click stays local", "path": "/", "assertions": [{"type": "click", "value": "a"}]}]}
                write_json(ad / "browser-evidence.json", req)
                (ad / "browser-evidence.sha256").write_text(digest(req) + "\n", encoding="utf-8")
                write_json(ad / "params.json", {"api_head_sha": SHA_B})
                write_json(ad / "api-handoff.json", {"api_head_sha": SHA_B})
                (ad / "smoke-urls.txt").write_text(f"web={web_origin}\napi=http://localhost:4127\n", encoding="utf-8")
                result = run_verifier(ad, wt)
            finally:
                stop_server(web_server, web_thread)
                stop_server(other_server, other_thread)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            receipt = json.loads((ad / "browser-verifier-receipt.json").read_text())
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(receipt["criteria"][0]["observed_origin"], other_origin)
            self.assertIn("assertion click changed origin", receipt["criteria"][0]["assertions"][0]["error"])

    def test_playwright_helper_uses_unique_screenshot_names_for_sanitized_collisions(self):
        if not (WEB_APP / "node_modules" / "playwright").is_dir():
            self.skipTest("web-app Playwright dependency is not installed")
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            site = root / "site"
            ad = root / "artifacts"
            wt = root / "wt"
            site.mkdir(); ad.mkdir(); wt.mkdir()
            (site / "index.html").write_text("<html><body>Welcome</body></html>", encoding="utf-8")
            req = {"required": [
                {"id": "same/id", "criterion": "first", "path": "/", "assertions": [{"type": "text", "value": "Welcome"}]},
                {"id": "same:id", "criterion": "second", "path": "/", "assertions": [{"type": "text", "value": "Welcome"}]},
            ]}
            write_json(ad / "browser-evidence.json", req)
            (ad / "browser-evidence.sha256").write_text(digest(req) + "\n", encoding="utf-8")
            write_json(ad / "params.json", {"api_head_sha": SHA_B})
            write_json(ad / "api-handoff.json", {"api_head_sha": SHA_B})
            handler = lambda *args, **kwargs: SimpleHTTPRequestHandler(*args, directory=str(site), **kwargs)
            server, thread, origin = start_server(handler)
            try:
                (ad / "smoke-urls.txt").write_text(f"web={origin}\napi=http://localhost:4127\n", encoding="utf-8")
                result = run_verifier(ad, wt)
            finally:
                stop_server(server, thread)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            receipt = json.loads((ad / "browser-verifier-receipt.json").read_text())
            self.assertEqual(receipt["status"], "passed")
            paths = [entry["path"] for entry in receipt["evidence"]["screenshots"]]
            self.assertEqual(len(paths), 2)
            self.assertEqual(len(set(paths)), 2)
            for rel in paths:
                self.assertTrue((ad / rel).is_file(), rel)


if __name__ == "__main__":
    unittest.main()
