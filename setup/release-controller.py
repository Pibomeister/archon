#!/usr/bin/env python3
"""Controller-only release operations. No agent-facing CLI or signing endpoint."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shlex
import shutil
import stat
import tempfile
import subprocess
from pathlib import Path

import control_contract

CONTROLLER_PATH = "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin"

SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
BRANCH = re.compile(r"archon/[a-zA-Z0-9][a-zA-Z0-9_/-]*\Z")
PIN_FIELDS = {"run_id", "chain_id", "epoch", "repository", "commit", "tree",
              "baseline_commit", "baseline_tree", "workflow_hash", "policy_hash",
              "oracle_hash", "environment_id"}


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def verify_authenticated(secret: str, value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("receipt must be an object")
    body = {key: item for key, item in value.items() if key != "authority_mac"}
    mac = value.get("authority_mac")
    if not isinstance(mac, str) or not hmac.compare_digest(
            mac, control_contract.hmac_sha256(secret, body)):
        raise ValueError("receipt authentication failed")
    if type(body.get("schema_version")) is not int or body["schema_version"] != 1:
        raise ValueError("unsupported receipt version")
    return body


def validate_pins(pins: dict) -> None:
    if not isinstance(pins, dict) or set(pins) != PIN_FIELDS:
        raise ValueError("incomplete release binding")
    if not all(isinstance(pins[key], str) and pins[key] for key in PIN_FIELDS - {"epoch"}):
        raise ValueError("invalid release binding")
    if type(pins["epoch"]) is not int or pins["epoch"] < 1:
        raise ValueError("invalid release epoch")
    if not all(SHA.fullmatch(pins[key]) for key in ("commit", "tree", "baseline_commit", "baseline_tree")):
        raise ValueError("invalid release commit/tree")
    if not all(DIGEST.fullmatch(pins[key]) for key in ("workflow_hash", "policy_hash", "oracle_hash")):
        raise ValueError("invalid release policy hash")
    if not REPOSITORY.fullmatch(pins["repository"]):
        raise ValueError("invalid release repository")
    if any(part in {".", ".."} for part in pins["repository"].split("/")):
        raise ValueError("invalid release repository")


def unique_stages(value: object, label: str) -> set[str]:
    if (not isinstance(value, list) or not value
            or not all(isinstance(item, str) and re.fullmatch(r"[a-z][a-z0-9-]*", item) for item in value)
            or len(set(value)) != len(value)):
        raise ValueError(f"invalid required {label}")
    return set(value)


def verified_receipts(state: dict, key: str, kind: str, required: set[str], controller_secret: str) -> list[dict]:
    values = state.get(key)
    if not isinstance(values, list):
        raise ValueError(f"missing {key}")
    found = {}
    for value in values:
        body = verify_authenticated(controller_secret, value)
        if body.get("pins") != state["pins"] or body.get("kind") != kind:
            raise ValueError(f"{key} binding mismatch")
        stage = body.get("stage")
        if not isinstance(stage, str) or stage in found:
            raise ValueError(f"duplicate or malformed {key} stage")
        found[stage] = body
    if set(found) != required:
        raise ValueError(f"missing or unexpected {key} stages")
    return [found[stage] for stage in sorted(found)]


def validate_execution(receipt: dict) -> None:
    if (type(receipt.get("executed")) is not int or receipt["executed"] <= 0
            or type(receipt.get("failed")) is not int or receipt["failed"] != 0
            or type(receipt.get("skipped")) is not int or receipt["skipped"] != 0):
        raise ValueError("verification execution incomplete")
    evidence = receipt.get("evidence")
    if (not isinstance(evidence, dict) or not evidence
            or not all(isinstance(key, str) and key and isinstance(value, str)
                       and DIGEST.fullmatch(value) for key, value in evidence.items())):
        raise ValueError("verification execution evidence missing")


def verify_controller_state(state: dict, controller_secret: str) -> dict:
    if not isinstance(state, dict) or not isinstance(controller_secret, str):
        raise ValueError("missing controller release authority")
    body = {key: value for key, value in state.items() if key != "state_mac"}
    expected = control_contract.hmac_sha256(controller_secret, body)
    actual = state.get("state_mac")
    if not isinstance(actual, str) or not hmac.compare_digest(expected, actual):
        raise ValueError("release state authentication failed")
    embedded = state.get("chain_secret")
    if not isinstance(embedded, str) or not hmac.compare_digest(embedded.encode(), controller_secret.encode()):
        raise ValueError("release state controller key mismatch")
    return state


def finalize_manifest(state: dict, *, controller_secret: str) -> dict:
    state = verify_controller_state(state, controller_secret)
    pins = state.get("pins")
    validate_pins(pins)
    if pins["tree"] == pins["baseline_tree"] and pins["commit"] != pins["baseline_commit"]:
        raise ValueError("NO_CHANGE requires acceptance verification of the pinned baseline commit")
    if state.get("logical_chain_id") != pins["chain_id"] or state.get("phase") != "verified":
        raise ValueError("release phase/binding mismatch")
    policy = state.get("release")
    if not isinstance(policy, dict):
        raise ValueError("missing release policy")
    stages = unique_stages(policy.get("required_stages"), "verification stages")
    if not {"review", "tests"}.issubset(stages):
        raise ValueError("review and tests are mandatory")
    if pins["repository"].endswith("/web-app") and "browser" not in stages:
        raise ValueError("web publication requires browser verification")
    approvals = unique_stages(policy.get("required_approvals"), "approvals")
    for approval in verified_receipts(state, "approvals", "human-approval", approvals, controller_secret):
        if approval.get("decision") != "approved":
            raise ValueError("human approval required")
    receipts = verified_receipts(state, "receipts", "verification", stages, controller_secret)
    for receipt in receipts:
        validate_execution(receipt)
    branch = policy.get("branch")
    if not isinstance(branch, str) or not BRANCH.fullmatch(branch) or "//" in branch or branch.endswith("/"):
        raise ValueError("release branch must be controller-owned archon branch")
    expected = policy.get("expected_remote_sha")
    if expected is not None and (not isinstance(expected, str) or not SHA.fullmatch(expected)):
        raise ValueError("invalid expected remote SHA")
    operation = policy.get("operation_id")
    title = policy.get("title")
    if not isinstance(operation, str) or not re.fullmatch(r"[a-zA-Z0-9-]{1,100}", operation):
        raise ValueError("invalid publication operation")
    if not isinstance(title, str) or not title.strip() or "\n" in title or len(title) > 200:
        raise ValueError("invalid approved PR title")
    if policy.get("base") != "main":
        raise ValueError("unsupported publication base")
    body = "## Certified verification\n\nStage | Executed | Failed | Skipped\n--- | ---: | ---: | ---:\n"
    body += "".join(f"{r['stage']} | {r['executed']} | {r['failed']} | {r['skipped']}\n" for r in receipts)
    body += f"\nCommit: `{pins['commit']}`\n\n<!-- archon-operation:{operation} -->\n"
    result = {
        "schema_version": 1, "kind": "release-manifest", "pins": pins,
        "outcome": "NO_CHANGE" if pins["tree"] == pins["baseline_tree"] else "CHANGED",
        "commit": pins["commit"], "tree": pins["tree"], "repository": pins["repository"],
        "branch": branch, "base": "main", "expected_remote_sha": expected,
        "operation_id": operation, "title": title, "pr_body": body,
        "pr_body_digest": digest(body.encode()),
        "receipt_digests": [digest(control_contract.canonical_bytes(r)) for r in state["receipts"]],
    }
    result["authority_mac"] = control_contract.hmac_sha256(controller_secret, result)
    return result


def controller_binary(name: str) -> str:
    selected = shutil.which(name, path=CONTROLLER_PATH)
    if selected is None:
        raise ValueError(f"required controller binary unavailable: {name}")
    resolved = Path(selected).resolve(strict=True)
    info = resolved.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.getuid()}
            or info.st_mode & 0o022):
        raise ValueError(f"unsafe controller binary: {name}")
    return str(resolved)


def controller_command(command: list[str], cwd: Path | str, *, authenticated: bool = False) -> subprocess.CompletedProcess[str]:
    env = {"PATH": CONTROLLER_PATH, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"}
    if authenticated:
        token = os.environ.get("GH_TOKEN")
        if not token or not token.strip():
            raise ValueError("publication requires an explicit controller GH_TOKEN")
        env["GH_TOKEN"] = token
    with tempfile.TemporaryDirectory(prefix="archon-release-") as private_home:
        env["HOME"] = private_home
        if authenticated:
            env["GH_CONFIG_DIR"] = private_home
            env["GH_PROMPT_DISABLED"] = "1"
        return subprocess.run(command, cwd=cwd, env=env, capture_output=True,
                              text=True, timeout=120, check=False)


def git(quarantine: Path, *args: str) -> str:
    authenticated = args[0] in {"ls-remote", "fetch", "push"} and any(
        arg.startswith("https://github.com/") for arg in args)
    command = [controller_binary("git"), "--no-optional-locks", "-c", "core.hooksPath=/dev/null",
               "-c", "core.fsmonitor=false", "-c", "credential.helper="]
    if authenticated:
        helper = shlex.quote(controller_binary("gh"))
        command += ["-c", f"credential.https://github.com.helper=!{helper} auth git-credential"]
    result = controller_command(command + ["-C", str(quarantine), *args], "/",
                                authenticated=authenticated)
    if result.returncode:
        raise ValueError(f"quarantine git operation failed: {result.stderr.strip()}")
    return result.stdout.strip()


def import_candidate(bundle: Path, quarantine: Path, commit: str, tree: str) -> None:
    if not SHA.fullmatch(commit) or not SHA.fullmatch(tree):
        raise ValueError("invalid candidate identity")
    if quarantine.exists():
        raise ValueError("quarantine must be a new controller-owned directory")
    quarantine.mkdir(mode=0o700, parents=False)
    git(quarantine, "init", "--bare", ".")
    git(quarantine, "bundle", "verify", str(bundle.resolve()))
    git(quarantine, "-c", "protocol.file.allow=always", "fetch", "--no-tags", "--no-write-fetch-head",
        str(bundle.resolve()), f"{commit}:refs/candidates/sealed")
    git(quarantine, "fsck", "--strict", "--no-reflogs")
    if git(quarantine, "rev-parse", "refs/candidates/sealed^{tree}") != tree:
        raise ValueError("candidate tree mismatch")


def publish_commit(state: dict, manifest: dict, quarantine: Path, intent_path: Path, *, controller_secret: str) -> dict:
    """Publish only the sealed object; PR creation remains a separate controller action."""
    expected_manifest = finalize_manifest(state, controller_secret=controller_secret)
    if manifest != expected_manifest:
        raise ValueError("release manifest invalidated")
    if git(quarantine, "rev-parse", "refs/candidates/sealed") != manifest["commit"]:
        raise ValueError("quarantine candidate mismatch")
    if git(quarantine, "rev-parse", "refs/candidates/sealed^{tree}") != manifest["tree"]:
        raise ValueError("quarantine tree mismatch")
    baseline = state["pins"]["baseline_commit"]
    if git(quarantine, "rev-parse", f"{baseline}^{{tree}}") != state["pins"]["baseline_tree"]:
        raise ValueError("quarantine baseline tree mismatch")
    if manifest["outcome"] == "NO_CHANGE":
        return {"status": "no-change", "commit": manifest["commit"], "pr_url": None}
    git(quarantine, "merge-base", "--is-ancestor", baseline, manifest["commit"])
    target = f"https://github.com/{manifest['repository']}.git"
    ref = f"refs/heads/{manifest['branch']}"
    intent = {"operation_id": manifest["operation_id"], "manifest": manifest,
              "target": target, "ref": ref, "status": "pending"}
    recovering = intent_path.exists()
    if recovering:
        existing = control_contract.secure_read_json(intent_path)
        verified = verify_authenticated(controller_secret, existing)
        if any(verified.get(key) != value for key, value in intent.items() if key != "status"):
            raise ValueError("conflicting publication intent")
    remote = git(quarantine, "ls-remote", "--refs", target, ref)
    actual = remote.split()[0] if remote else None
    if actual == manifest["commit"]:
        if not recovering:
            raise ValueError("cannot adopt publication without prior intent")
        return {"status": "commit-published", "commit": actual, "recovered": True}
    if actual != manifest["expected_remote_sha"]:
        raise ValueError("remote branch lease conflict")
    if not recovering:
        saved = {"schema_version": 1, **intent}
        saved["authority_mac"] = control_contract.hmac_sha256(controller_secret, saved)
        control_contract.secure_write_json(intent_path, saved)
    if actual:
        git(quarantine, "fetch", "--no-tags", "--no-write-fetch-head", target, ref)
        git(quarantine, "merge-base", "--is-ancestor", actual, manifest["commit"])
    git(quarantine, "push", f"--force-with-lease={ref}:{actual or ''}", target,
        f"{manifest['commit']}:{ref}")
    acknowledged = git(quarantine, "ls-remote", "--refs", target, ref).split()
    if not acknowledged or acknowledged[0] != manifest["commit"]:
        raise ValueError("published commit not acknowledged")
    return {"status": "commit-published", "commit": manifest["commit"], "recovered": False}


def github(*args: str) -> object:
    result = controller_command([controller_binary("gh"), "api", *args], "/", authenticated=True)
    if result.returncode:
        raise ValueError("GitHub publication request failed; inspect remote state before retrying")
    return json.loads(result.stdout)


def validate_pr(pr: dict, manifest: dict) -> str:
    if not isinstance(pr, dict):
        raise ValueError("invalid PR response")
    head, base = pr.get("head") or {}, pr.get("base") or {}
    if (head.get("sha") != manifest["commit"] or head.get("ref") != manifest["branch"]
            or (head.get("repo") or {}).get("full_name") != manifest["repository"]
            or base.get("ref") != manifest["base"]
            or (base.get("repo") or {}).get("full_name") != manifest["repository"]
            or pr.get("state") != "open" or pr.get("title") != manifest["title"]
            or not isinstance(pr.get("body"), str)
            or digest(pr["body"].encode()) != manifest["pr_body_digest"]):
        raise ValueError("conflicting PR publication state")
    url = pr.get("html_url")
    prefix = f"https://github.com/{manifest['repository']}/pull/"
    if not isinstance(url, str) or not url.startswith(prefix) or not url[len(prefix):].isdigit():
        raise ValueError("invalid PR identity")
    return url


def publish_release(state: dict, manifest: dict, quarantine: Path, intent_path: Path, *, controller_secret: str) -> dict:
    published = publish_commit(state, manifest, quarantine, intent_path, controller_secret=controller_secret)
    if published["status"] == "no-change":
        return published
    owner = manifest["repository"].split("/")[0]
    prs = github(f"repos/{manifest['repository']}/pulls?state=open&head={owner}:{manifest['branch']}")
    if not isinstance(prs, list) or len(prs) > 1:
        raise ValueError("ambiguous PR publication state")
    if prs:
        url = validate_pr(prs[0], manifest)
    else:
        pr = github("--method", "POST", f"repos/{manifest['repository']}/pulls",
                    "-f", f"title={manifest['title']}", "-f", f"head={manifest['branch']}",
                    "-f", f"base={manifest['base']}", "-f", f"body={manifest['pr_body']}",
                    "-F", "draft=true")
        url = validate_pr(pr, manifest)
    saved = control_contract.secure_read_json(intent_path)
    saved.pop("authority_mac")
    saved.update(status="completed", pr_url=url)
    saved["authority_mac"] = control_contract.hmac_sha256(controller_secret, saved)
    control_contract.secure_write_json(intent_path, saved)
    return {**published, "status": "published", "pr_url": url}
