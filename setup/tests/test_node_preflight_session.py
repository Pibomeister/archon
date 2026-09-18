#!/usr/bin/env python3
"""preflight: the two infra pre-checks a repository-scope run needs before it
spends two unattended hours (plan item 8).

The whole preflight body cannot run here — it needs gh auth, bun, the staged CE
skills and the knowledge base — so each check is sliced out of the SHIPPED body
by its own marker and run on its own. Extraction, not transcription: delete or
rename either block in the YAML and these tests stop finding it.

Two things are asserted that a happy-path test would not see:

  * the credential blob never reaches stdout or stderr. The block reads a
    keychain item that holds a live access token and a refresh token; only the
    derived minute count is allowed out.
  * both OTP identities are checked, not just the second. They are minted by the
    same `joint-api-mcp-e2e.sh` call against the same whitelist, so a check that
    covers one and not the other is the one-directional guard this repo keeps
    re-learning about.
"""
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from nodes.extract import runnable_body

SESSION_START = 'CRED=""'
SESSION_END = "# Integration identity:"
OTP_START = "OTP_ENV="
OTP_END = 'echo "PREFLIGHT=PASS"'

FAKE_TOKEN = "sk-ant-oat-FAKE-DO-NOT-PRINT-abcdef0123456789"


def _slice(body, start, end, what):
    i = body.index(start)
    j = body.index(end, i)
    block = body[i:j]
    assert block.strip(), f"{what} block is empty"
    return "set -euo pipefail\n" + block


def session_block(root, subs=()):
    body = runnable_body("full-sdlc-api", "preflight", root=str(root))
    block = _slice(body, SESSION_START, SESSION_END, "session")
    for old, new in subs:
        assert old in block, f"negative-control target not in the body: {old!r}"
        block = block.replace(old, new)
    return block


def otp_block(root, subs=()):
    body = runnable_body("full-sdlc-api", "preflight", root=str(root))
    block = _slice(body, OTP_START, OTP_END, "otp")
    for old, new in subs:
        assert old in block, f"negative-control target not in the body: {old!r}"
        block = block.replace(old, new)
    return block


def credential_blob(refresh_minutes, access_minutes=200):
    now = time.time()
    return json.dumps({
        "claudeAiOauth": {
            "accessToken": FAKE_TOKEN,
            "refreshToken": FAKE_TOKEN + "-refresh",
            "expiresAt": int((now + access_minutes * 60) * 1000),
            # +30 s so int() truncation lands on the minute the caller asked for.
            "refreshTokenExpiresAt": int((now + refresh_minutes * 60 + 30) * 1000),
            "subscriptionType": "max",
        }
    })


class _Fixture:
    """A temp Goodword root with an isolated HOME and a PATH shim dir in front."""

    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="pfsession-"))
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()

    def shim(self, name, script):
        path = self.bin / name
        path.write_text("#!/bin/bash\n" + script + "\n")
        path.chmod(0o755)

    def credentials_file(self, blob):
        d = self.home / ".claude"
        d.mkdir(exist_ok=True)
        (d / ".credentials.json").write_text(blob)

    def api_env(self, whitelist):
        d = self.root / "api"
        d.mkdir(exist_ok=True)
        (d / ".env").write_text(
            "SOME_OTHER=1\nOTP_TEST_WHITELIST_EMAILS=" + whitelist + "\nTAIL=2\n"
        )

    def run(self, block, env=None):
        run_env = dict(os.environ)
        run_env["HOME"] = str(self.home)
        run_env["PATH"] = f"{self.bin}:{run_env['PATH']}"
        run_env.update(env or {})
        return subprocess.run(["bash", "-c", block], capture_output=True,
                              text=True, env=run_env)


class ClaudeSessionExpiry(unittest.TestCase):
    def setUp(self):
        self.fx = _Fixture()

    def test_a_long_lived_session_passes(self):
        self.fx.shim("security", f"cat <<'EOF'\n{credential_blob(10 * 24 * 60)}\nEOF")
        p = self.fx.run(session_block(self.fx.root))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("PREFLIGHT_SESSION=OK claude session expires in 14400 min", p.stdout)

    def test_a_session_expiring_in_ten_minutes_fails(self):
        # The plan's named negative control for item 8.
        self.fx.shim("security", f"cat <<'EOF'\n{credential_blob(10)}\nEOF")
        p = self.fx.run(session_block(self.fx.root))
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("PREFLIGHT=FAIL claude session expires in 10 min", p.stdout)

    def test_an_already_expired_session_reads_as_zero_and_fails(self):
        self.fx.shim("security", f"cat <<'EOF'\n{credential_blob(-500)}\nEOF")
        p = self.fx.run(session_block(self.fx.root))
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("PREFLIGHT=FAIL claude session expires in 0 min", p.stdout)

    def test_an_unreadable_store_warns_and_never_fails(self):
        self.fx.shim("security", "exit 1")
        p = self.fx.run(session_block(self.fx.root))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("PREFLIGHT_WARN claude session expiry unreadable", p.stdout)
        self.assertNotIn("PREFLIGHT=FAIL", p.stdout)

    def test_a_garbage_store_warns_and_never_fails(self):
        self.fx.shim("security", "echo 'not json at all'")
        p = self.fx.run(session_block(self.fx.root))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("PREFLIGHT_WARN claude session expiry unreadable", p.stdout)

    def test_the_credentials_file_is_the_linux_fallback(self):
        # No keychain: Linux hosts (RUNBOOK 6a) keep the same JSON on disk.
        self.fx.shim("security", "exit 1")
        self.fx.credentials_file(credential_blob(10 * 24 * 60))
        p = self.fx.run(session_block(self.fx.root))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("PREFLIGHT_SESSION=OK", p.stdout)

    def test_no_token_material_is_printed(self):
        self.fx.shim("security", f"cat <<'EOF'\n{credential_blob(10 * 24 * 60)}\nEOF")
        p = self.fx.run(session_block(self.fx.root))
        self.assertNotIn(FAKE_TOKEN, p.stdout + p.stderr)
        self.assertNotIn("refreshToken", p.stdout + p.stderr)

    def test_negative_control_the_three_hour_bar_is_load_bearing(self):
        # Revert exactly the guard: with the bar at 0, the 10-minute session the
        # test above rejects sails through. If this ever passes at the shipped
        # threshold, the comparison stopped deciding anything.
        self.fx.shim("security", f"cat <<'EOF'\n{credential_blob(10)}\nEOF")
        p = self.fx.run(session_block(self.fx.root, subs=[('-lt 180', '-lt 0')]))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("PREFLIGHT_SESSION=OK", p.stdout)


class OtpWhitelist(unittest.TestCase):
    def setUp(self):
        self.fx = _Fixture()

    def test_both_identities_whitelisted_passes(self):
        self.fx.api_env("edy@goodword.com,edy+archon2@goodword.com")
        p = self.fx.run(otp_block(self.fx.root))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("PREFLIGHT_OTP=OK", p.stdout)

    def test_a_missing_second_identity_fails(self):
        self.fx.api_env("edy@goodword.com")
        p = self.fx.run(otp_block(self.fx.root))
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("PREFLIGHT=FAIL ARCHON_OTP_SECOND_EMAIL=edy+archon2@goodword.com "
                      "is not in OTP_TEST_WHITELIST_EMAILS", p.stdout)

    def test_a_missing_first_identity_fails_too(self):
        # The sibling case. joint-api-mcp-e2e.sh mints BOTH tokens and fails on
        # either, so a check that only knows the second one is half a guard.
        self.fx.api_env("edy+archon2@goodword.com")
        p = self.fx.run(otp_block(self.fx.root))
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("PREFLIGHT=FAIL ARCHON_OTP_TEST_EMAIL=edy@goodword.com "
                      "is not in OTP_TEST_WHITELIST_EMAILS", p.stdout)

    def test_an_overridden_address_is_checked_not_the_default(self):
        self.fx.api_env("edy@goodword.com,edy+archon2@goodword.com")
        p = self.fx.run(otp_block(self.fx.root),
                        env={"ARCHON_OTP_SECOND_EMAIL": "someone+else@goodword.com"})
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("ARCHON_OTP_SECOND_EMAIL=someone+else@goodword.com", p.stdout)

    def test_a_substring_match_is_not_a_match(self):
        # `,`-delimited compare, not a substring search: a whitelist naming a
        # longer address that merely contains ours must not satisfy it.
        self.fx.api_env("notedy@goodword.com,xedy+archon2@goodword.comx")
        p = self.fx.run(otp_block(self.fx.root))
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("PREFLIGHT=FAIL ARCHON_OTP_TEST_EMAIL=", p.stdout)

    def test_an_absent_env_warns_and_never_fails(self):
        p = self.fx.run(otp_block(self.fx.root))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("PREFLIGHT_WARN api .env unreadable", p.stdout)

    def test_an_env_without_the_whitelist_key_fails(self):
        (self.fx.root / "api").mkdir(exist_ok=True)
        (self.fx.root / "api" / ".env").write_text("UNRELATED=1\n")
        p = self.fx.run(otp_block(self.fx.root))
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("PREFLIGHT=FAIL ARCHON_OTP_TEST_EMAIL=", p.stdout)

    def test_negative_control_dropping_the_second_identity_lets_it_through(self):
        # Revert exactly the sibling half of the loop; the case the test above
        # catches now passes, which is what makes that test evidence.
        self.fx.api_env("edy@goodword.com")
        block = otp_block(self.fx.root, subs=[
            (' \\\n              "ARCHON_OTP_SECOND_EMAIL:'
             '${ARCHON_OTP_SECOND_EMAIL:-edy+archon2@goodword.com}"', ""),
        ])
        p = self.fx.run(block)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("PREFLIGHT_OTP=OK", p.stdout)


if __name__ == "__main__":
    unittest.main()
