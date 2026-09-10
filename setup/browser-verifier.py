#!/usr/bin/env python3
"""Run advisory Playwright UAT; trusted controller finalization happens later."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HEX40 = re.compile(r"^[0-9a-fA-F]{40}$")
VALID_ASSERTIONS = {"text", "selector", "url", "title", "testid", "click", "fill"}
RUNNER_JS = r'''
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { chromium } = require(process.env.ARCHON_PLAYWRIGHT_MODULE);

const input = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const out = process.argv[3];
const origin = input.origin.replace(/\/$/, '');
const timeout = input.timeoutMs || 10000;
const results = [];
const evidence = { screenshots: [], traces: [] };

function safeName(id) {
  return id.replace(/[^a-zA-Z0-9._-]+/g, '-').slice(0, 64) || 'criterion';
}

function screenshotName(index, id) {
  const digest = crypto.createHash('sha256').update(id).digest('hex').slice(0, 12);
  return `browser-evidence/${String(index + 1).padStart(3, '0')}-${safeName(id)}-${digest}.png`;
}

function urlOrigin(value) {
  try { return new URL(value).origin; } catch (_err) { return ''; }
}

function assertPageOrigin(page, expected, stage) {
  const observed = urlOrigin(page.url());
  if (observed !== expected) throw new Error(`${stage} changed origin to ${observed || page.url()}`);
  return observed;
}

async function restoreOriginBeforeScreenshot(page, expected) {
  const observed = urlOrigin(page.url());
  if (observed !== expected) {
    await page.goto(expected, { waitUntil: 'domcontentloaded', timeout });
  }
  assertPageOrigin(page, expected, 'pre-screenshot');
}

async function checkAssertion(page, assertion) {
  const value = assertion.value;
  if (assertion.type === 'text') {
    await page.getByText(value, { exact: false }).first().waitFor({ timeout });
    return;
  }
  if (assertion.type === 'selector') {
    await page.locator(value).first().waitFor({ timeout });
    return;
  }
  if (assertion.type === 'testid') {
    await page.getByTestId(value).first().waitFor({ timeout });
    return;
  }
  if (assertion.type === 'click') {
    await page.locator(value).first().click({ timeout });
    return;
  }
  if (assertion.type === 'fill') {
    await page.locator(value).first().fill(assertion.text, { timeout });
    return;
  }
  if (assertion.type === 'url') {
    if (!page.url().includes(value)) throw new Error(`url missing ${value}: ${page.url()}`);
    return;
  }
  if (assertion.type === 'title') {
    const title = await page.title();
    if (!title.includes(value)) throw new Error(`title missing ${value}: ${title}`);
    return;
  }
  throw new Error(`unsupported assertion type ${assertion.type}`);
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: input.viewport });
  await context.tracing.start({ screenshots: true, snapshots: true });
  const page = await context.newPage();
  for (let index = 0; index < input.required.length; index++) {
    const criterion = input.required[index];
    const row = { id: criterion.id, path: criterion.path, status: 'passed', assertions: [], observed_origin: origin };
    try {
      const target = new URL(criterion.path, origin).toString();
      if (!target.startsWith(origin + '/') && target !== origin) throw new Error('cross-origin navigation refused');
      await page.goto(target, { waitUntil: 'domcontentloaded', timeout });
      row.observed_origin = assertPageOrigin(page, origin, 'navigation');
      for (const assertion of criterion.assertions) {
        try {
          await checkAssertion(page, assertion);
          await page.waitForLoadState('domcontentloaded', { timeout: Math.min(timeout, 1000) }).catch(() => {});
          await page.waitForTimeout(100);
          row.observed_origin = assertPageOrigin(page, origin, `assertion ${assertion.type}`);
          row.assertions.push({ type: assertion.type, value: assertion.value, text: assertion.text, status: 'passed' });
        } catch (err) {
          row.assertions.push({ type: assertion.type, value: assertion.value, text: assertion.text, status: 'failed', error: String(err.message || err) });
          row.observed_origin = urlOrigin(page.url()) || row.observed_origin;
          row.status = 'failed';
        }
      }
    } catch (err) {
      row.status = 'failed';
      row.error = String(err.message || err);
      row.observed_origin = urlOrigin(page.url()) || row.observed_origin;
    }
    try {
      await restoreOriginBeforeScreenshot(page, origin);
    } catch (err) {
      row.status = 'failed';
      row.error = row.error || String(err.message || err);
      row.observed_origin = urlOrigin(page.url()) || row.observed_origin;
    }
    const shot = screenshotName(index, criterion.id);
    await page.screenshot({ path: path.join(input.artifacts, shot), fullPage: true });
    evidence.screenshots.push({ criterion: criterion.id, path: shot });
    results.push(row);
  }
  const trace = 'browser-evidence/trace.zip';
  await context.tracing.stop({ path: path.join(input.artifacts, trace) });
  evidence.traces.push({ path: trace });
  await browser.close();
  fs.writeFileSync(out, JSON.stringify({ criteria: results, evidence }, null, 2) + '\n');
})().catch(err => {
  fs.writeFileSync(out, JSON.stringify({ fatal: String(err.stack || err) }, null, 2) + '\n');
  process.exit(2);
});
'''


def fail(message: str) -> None:
    raise SystemExit(f"BROWSER_VERIFIER=FAIL {message}")


def read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"{label} unreadable: {exc}")
    if not isinstance(data, dict):
        fail(f"{label} must be an object")
    return data


def canonical_digest(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def same_origin_path(path: str) -> bool:
    return path.startswith("/") and not path.startswith("//")


def load_smoke_urls(artifacts: Path) -> tuple[str, str]:
    values: dict[str, str] = {}
    for line in (artifacts / "smoke-urls.txt").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().rstrip("/")
    web = values.get("web", "")
    api = values.get("api", "")
    if not web.startswith(("http://localhost:", "http://127.0.0.1:")):
        fail("web origin must come from smoke-urls.txt local origin")
    if not api.startswith(("http://localhost:", "http://127.0.0.1:")):
        fail("api origin must come from smoke-urls.txt local origin")
    return web, api


def validated_required(requirements: dict[str, Any]) -> list[dict[str, Any]]:
    required = requirements.get("required")
    if not isinstance(required, list) or not required:
        fail("browser-evidence has no required criteria")
    ids: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in required:
        if not isinstance(item, dict):
            fail("required criteria must be objects")
        cid = item.get("id")
        path = item.get("path")
        assertions = item.get("assertions")
        if not isinstance(cid, str) or not cid or cid in ids:
            fail("required criterion ids must be unique nonempty strings")
        if not isinstance(path, str) or not same_origin_path(path):
            fail(f"required criterion {cid} needs a same-origin path")
        if not isinstance(assertions, list) or not assertions:
            fail(f"required criterion {cid} needs frozen assertions")
        frozen_assertions: list[dict[str, str]] = []
        for assertion in assertions:
            if not isinstance(assertion, dict):
                fail(f"criterion {cid} assertion must be an object")
            kind = assertion.get("type")
            value = assertion.get("value")
            if kind not in VALID_ASSERTIONS or not isinstance(value, str) or not value:
                fail(f"criterion {cid} assertion malformed")
            frozen = {"type": kind, "value": value}
            if kind == "fill":
                text = assertion.get("text")
                if not isinstance(text, str) or not text:
                    fail(f"criterion {cid} fill assertion needs text")
                frozen["text"] = text
            frozen_assertions.append(frozen)
        ids.add(cid)
        out.append({"id": cid, "path": path, "assertions": frozen_assertions})
    return out


def git_value(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, encoding="utf-8")
    if proc.returncode != 0:
        fail(f"git {' '.join(args)} failed in {repo}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def evidence_with_digests(artifacts: Path, evidence: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"screenshots": [], "traces": []}
    for key in ("screenshots", "traces"):
        entries = evidence.get(key)
        if not isinstance(entries, list) or not entries:
            fail(f"playwright produced no {key}")
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                fail(f"playwright {key} entry malformed")
            path = artifacts / entry["path"]
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(artifacts.resolve(strict=True))
            except (OSError, ValueError) as exc:
                fail(f"unsafe {key} evidence path: {entry.get('path')} ({exc})")
            if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
                fail(f"unsafe or empty {key} evidence file: {entry.get('path')}")
            sealed = dict(entry)
            sealed["sha256"] = sha_file(path)
            out[key].append(sealed)
    return out


def build_receipt(args: argparse.Namespace, requirements: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    params = read_json(args.artifacts / "params.json", "params")
    api_handoff = read_json(args.artifacts / "api-handoff.json", "api-handoff")
    web_origin, api_origin = load_smoke_urls(args.artifacts)
    required = validated_required(requirements)
    candidate_commit = args.candidate_commit or git_value(args.web_worktree, "rev-parse", "HEAD")
    candidate_tree = args.candidate_tree or git_value(args.web_worktree, "rev-parse", "HEAD^{tree}")
    api_commit = args.api_commit or params.get("api_head_sha") or api_handoff.get("api_head_sha") or api_handoff.get("api_baseline")
    if not HEX40.fullmatch(str(candidate_commit)) or not HEX40.fullmatch(str(api_commit)):
        fail("candidate and API commits must be 40-character git SHAs")
    criteria = raw.get("criteria")
    if not isinstance(criteria, list):
        criteria = []
    evidence = evidence_with_digests(args.artifacts, raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {})
    status = "passed" if criteria and all(isinstance(row, dict) and row.get("status") == "passed" for row in criteria) and len(criteria) == len(required) else "failed"
    return {
        "receipt_version": 1,
        "authority": "controller-playwright",
        "authority_state": "unsealed-local-receipt",
        "controller_action": "finalize-evidence",
        "runner": "archon-browser-verifier",
        "status": status,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "requirements_digest": canonical_digest(requirements),
        "approved_requirements_digest": canonical_digest(requirements),
        "origin": web_origin,
        "viewport": {"width": args.viewport_width, "height": args.viewport_height},
        "candidate": {"commit": candidate_commit, "tree": candidate_tree, "worktree": str(args.web_worktree)},
        "api": {"commit": str(api_commit), "origin": api_origin, "handoff_digest": sha_file(args.artifacts / "api-handoff.json")},
        "criteria": criteria,
        "evidence": evidence,
    }


def run(args: argparse.Namespace) -> int:
    args.artifacts.mkdir(parents=True, exist_ok=True)
    (args.artifacts / "browser-evidence").mkdir(exist_ok=True)
    requirements_path = args.artifacts / "browser-evidence.json"
    requirements = read_json(requirements_path, "browser-evidence")
    approved_path = args.approved_digest or requirements_path.with_suffix(".sha256")
    try:
        approved_digest = approved_path.read_text(encoding="utf-8").strip().split()[0].lower()
    except (OSError, IndexError) as exc:
        fail(f"approved browser policy digest unavailable: {exc}")
    if approved_digest != canonical_digest(requirements):
        fail("browser-evidence.json does not match approved browser policy digest")
    required = validated_required(requirements)
    web_origin, _api_origin = load_smoke_urls(args.artifacts)
    trusted_playwright_root = Path(__file__).resolve().parents[2] / "web-app"
    if args.playwright_root.resolve() != trusted_playwright_root.resolve():
        fail("Playwright root must be the controller-pinned Goodword web-app checkout")
    module = trusted_playwright_root / "node_modules" / "playwright"
    if not module.is_dir():
        fail(f"Playwright module not found under {trusted_playwright_root}")
    with tempfile.TemporaryDirectory(prefix="archon-browser-verifier-") as tmp_name:
        tmp = Path(tmp_name)
        runner = tmp / "runner.cjs"
        runner.write_text(RUNNER_JS, encoding="utf-8")
        input_path = tmp / "input.json"
        raw_path = tmp / "raw-result.json"
        input_path.write_text(json.dumps({
            "artifacts": str(args.artifacts),
            "origin": web_origin,
            "viewport": {"width": args.viewport_width, "height": args.viewport_height},
            "timeoutMs": args.timeout_ms,
            "required": required,
        }), encoding="utf-8")
        env = {**dict(os_environ_without_secrets()), "ARCHON_PLAYWRIGHT_MODULE": str(module)}
        proc = subprocess.run(["node", str(runner), str(input_path), str(raw_path)], cwd=str(args.playwright_root), env=env, capture_output=True, encoding="utf-8", timeout=max(args.timeout_ms * max(len(required), 1) // 1000 + 30, 60))
        if not raw_path.exists():
            fail(f"Playwright runner produced no result rc={proc.returncode} stderr={proc.stderr.strip()}")
        raw = read_json(raw_path, "raw playwright result")
        if proc.returncode != 0 and "fatal" in raw:
            fail(str(raw["fatal"]))
    receipt = build_receipt(args, requirements, raw)
    out = args.artifacts / "browser-verifier-receipt.json"
    out.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"BROWSER_VERIFIER_DONE status={receipt['status']} authority=none receipt={out}")
    return 0


def os_environ_without_secrets() -> dict[str, str]:
    import os
    blocked = ("TOKEN", "KEY", "SECRET", "PASSWORD", "CREDENTIAL", "AWS_", "ANTHROPIC_", "OPENAI_", "CLAUDE_")
    return {key: value for key, value in os.environ.items() if not any(part in key.upper() for part in blocked)}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--artifacts", type=Path, required=True)
    run_parser.add_argument("--web-worktree", type=Path, required=True)
    run_parser.add_argument("--playwright-root", type=Path, required=True)
    run_parser.add_argument("--candidate-commit")
    run_parser.add_argument("--candidate-tree")
    run_parser.add_argument("--api-commit")
    run_parser.add_argument("--approved-digest", type=Path)
    run_parser.add_argument("--viewport-width", type=int, default=1280)
    run_parser.add_argument("--viewport-height", type=int, default=720)
    run_parser.add_argument("--timeout-ms", type=int, default=10000)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.command == "run":
        return run(args)
    fail("unknown command")


if __name__ == "__main__":
    raise SystemExit(main())
