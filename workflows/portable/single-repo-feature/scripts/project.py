#!/usr/bin/env python3
"""Deterministic profile/context/verification seams for the native Archon DAG.

The engine owns provider execution, gate authority, and loop topology. This module
never approves a gate and never writes to the source knowledge repository.
"""
from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

# Imported helper bytecode must not mutate the engine's frozen source capture.
sys.dont_write_bytecode = True

import source_recipes
import repo_policy
import repair_publication
from review_envelope import ENUM as REVIEW_VERDICTS

PROFILE_VERSION = "archon.project-profile.v1"
BINDING_VERSION = "archon.machine-binding.v1"
SHA = re.compile(r"^[a-f0-9]{40}$")
DIGEST = re.compile(r"^[a-f0-9]{64}$")
FACTORY_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._:-]{1,127}$")


def encoded(value) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_digest(path: Path) -> str:
    return digest(regular(path).read_bytes())


def regular(path: Path) -> Path:
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError(f"Expected regular file without hardlinks: {path}")
    return path


def write(path: Path, value, immutable=False) -> None:
    content = encoded(value)
    if immutable and path.exists():
        if regular(path).read_bytes() != content:
            raise ValueError(f"snapshot drift: {path.name}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError(f"Refusing symlink artifact: {path}")
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
        output.write(content)
        temporary = Path(output.name)
    os.replace(temporary, path)


def remove_artifact(path: Path) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Refusing unsafe artifact removal: {path}")
        path.unlink()


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout


def git_text(root: Path, *args: str) -> str:
    return git(root, *args).decode().strip()


def relative(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise ValueError("Invalid repository-relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or value.startswith("-"):
        raise ValueError(f"Unsafe relative path: {value}")
    return value


def remote_identity(value: str) -> str:
    if value.startswith("git@"):
        value = value[4:].replace(":", "/", 1)
    elif value.startswith("https://"):
        value = value[8:]
        if "@" in value or "?" in value or "#" in value:
            raise ValueError("Repository remote must not contain credentials or query data")
    else:
        raise ValueError("Expected explicit SSH/HTTPS repository remote")
    return value.removesuffix(".git").rstrip("/").lower()


def object_fields(value, fields, label):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError(f"Invalid {label} fields")


def validate_factory_execution(value: dict) -> dict:
    required = ("factoryJobId", "logicalChainId", "readySnapshotId", "readyDigest", "commandId", "launchId", "attemptId", "runtimeBundleId")
    fields = required + (("launchKey",) if isinstance(value, dict) and "launchKey" in value else ())
    object_fields(value, fields, "factory execution identity")
    for name in ("factoryJobId", "logicalChainId", "readySnapshotId", "commandId", "launchId", "attemptId", "runtimeBundleId"):
        if not isinstance(value[name], str) or not IDENTIFIER.fullmatch(value[name]):
            raise ValueError(f"Invalid factory execution identity field: {name}")
    if "launchKey" in value and (not isinstance(value["launchKey"], str) or not IDENTIFIER.fullmatch(value["launchKey"])):
        raise ValueError("Invalid factory execution identity field: launchKey")
    if not isinstance(value["readyDigest"], str) or not FACTORY_DIGEST.fullmatch(value["readyDigest"]):
        raise ValueError("Invalid factory execution identity field: readyDigest")
    return dict(value)


def factory_execution_from_env(artifacts: Path) -> dict | None:
    existing_context = artifacts / "run-context.json"
    existing = None
    if existing_context.exists():
        existing = json.loads(regular(existing_context).read_text()).get("factoryExecution")
    path = os.environ.get("INPUTS_FACTORY_EXECUTION")
    supplied = None
    if path:
        supplied = validate_factory_execution(json.loads(regular(Path(path)).read_text()))
    if existing is not None:
        existing = validate_factory_execution(existing)
        if supplied is not None and supplied != existing:
            raise ValueError("factory execution identity drift")
        return existing
    return supplied


def github_repository(value: str) -> dict:
    origin = remote_identity(value)
    if not origin.startswith("github.com/"):
        raise ValueError("Expected GitHub repository identity")
    parts = origin.removeprefix("github.com/").split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError("Expected GitHub owner/repository identity")
    return {"provider": "github", "owner": parts[0], "name": parts[1]}


def merge_review_manifest(context: dict, github_repo: str, url: str, head: str) -> dict | None:
    execution = context.get("factoryExecution")
    if execution is None:
        return None
    execution = validate_factory_execution(execution)
    if not SHA.fullmatch(head):
        raise ValueError("Merge review manifest requires a full head SHA")
    match = re.fullmatch(r"https://github\.com/" + re.escape(github_repo) + r"/pull/([0-9]+)", url, re.I)
    if not match:
        raise ValueError("Merge review manifest requires exact PR URL evidence")
    return {
        "kind": "factory-merge-review.v1",
        "repository": github_repository("https://github.com/" + github_repo),
        "pullRequestNumber": int(match.group(1)),
        "headSha": head,
        "execution": execution,
    }


def validate_profile(profile: dict) -> None:
    if not isinstance(profile, dict):
        raise ValueError("Invalid project profile fields")
    version = profile.get("profileVersion")
    if version not in (PROFILE_VERSION, source_recipes.VERSION):
        raise ValueError("Unsupported project profile version")
    v2 = version == source_recipes.VERSION
    fields = ("profileVersion", "projectId", "repository", "verification", "scope", "knowledge", "recovery", "delivery")
    expected_fields = fields
    if v2:
        expected_fields += ("sourceRecipe",)
    if "noChangeClosure" in profile:
        expected_fields += ("noChangeClosure",)
    if "guidance" in profile:
        expected_fields += ("guidance",)
    if "capabilities" in profile:
        expected_fields += ("capabilities",)
    object_fields(profile, expected_fields, "project profile")
    object_fields(profile["repository"], ("remote", "defaultBranch", "stack") + (() if v2 else ("packageManager",)), "repository")
    if v2 and profile["sourceRecipe"] not in source_recipes.RECIPES:
        raise ValueError("Unknown source recipe")
    object_fields(profile["scope"], ("allowedPaths", "forbiddenPaths"), "scope")
    knowledge_fields = ("paths", "maxBytes")
    if isinstance(profile.get("knowledge"), dict) and "required" in profile["knowledge"]:
        knowledge_fields = ("required", "paths", "maxBytes")
    object_fields(profile["knowledge"], knowledge_fields, "knowledge")
    object_fields(profile["recovery"], ("maxRounds",), "recovery")
    object_fields(profile["delivery"], ("draftOnly", "autoMerge", "autoDeploy") + (("baseBranch",) if v2 else ()), "delivery")
    if "noChangeClosure" in profile:
        object_fields(profile["noChangeClosure"], ("enabled", "verifierIds"), "no-change closure")
    if not isinstance(profile["projectId"], str) or not profile["projectId"]:
        raise ValueError("Profile requires project identity")
    repository = profile["repository"]
    if any(not isinstance(value, str) or not value for value in repository.values()):
        raise ValueError("Repository fields must be nonempty text")
    remote_identity(repository["remote"])
    if not repository.get("defaultBranch") or (not v2 and not repository.get("packageManager")):
        raise ValueError("Profile must declare branch and package manager")
    if v2:
        branch = profile["delivery"]["baseBranch"]
        if not isinstance(branch, str) or not branch or branch.startswith("-") or subprocess.run(["git", "check-ref-format", "--branch", branch], capture_output=True).returncode:
            raise ValueError("Invalid explicit PR base branch")
    checks = profile["verification"]
    if not isinstance(checks, list):
        raise ValueError("Verification must be a command array")
    for check in checks:
        object_fields(check, ("id", "argv", "timeoutSeconds"), "verification command")
    if not checks or len({check["id"] for check in checks}) != len(checks):
        raise ValueError("Verification commands must have distinct ids")
    for check in checks:
        if not re.fullmatch(r"[a-z][a-z0-9-]*", check["id"]):
            raise ValueError("Invalid verification id")
        if not isinstance(check["argv"], list) or not check["argv"] or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in check["argv"]):
            raise ValueError("Verification must use explicit argv")
        if type(check["timeoutSeconds"]) is not int or not 1 <= check["timeoutSeconds"] <= 1200:
            raise ValueError("Verification timeout must be bounded")
    if v2:
        no_change = profile.get("noChangeClosure", {"enabled": False, "verifierIds": []})
        verifier_ids = no_change.get("verifierIds", [])
        declared_ids = {check["id"] for check in checks}
        if type(no_change["enabled"]) is not bool:
            raise ValueError("No-change closure must be explicitly enabled or disabled")
        if no_change["enabled"] and not verifier_ids:
            raise ValueError("No-change closure requires pinned verification command ids")
        if (
            not isinstance(verifier_ids, list)
            or len(set(verifier_ids)) != len(verifier_ids)
            or any(not isinstance(item, str) or item not in declared_ids for item in verifier_ids)
        ):
            raise ValueError("No-change closure requires pinned verification command ids")
    if not isinstance(profile["knowledge"]["paths"], list) or not profile["knowledge"]["paths"]:
        raise ValueError("Profile requires explicit knowledge paths")
    if not isinstance(profile["scope"]["allowedPaths"], list) or not profile["scope"]["allowedPaths"] or not isinstance(profile["scope"]["forbiddenPaths"], list):
        raise ValueError("Profile requires an explicit allowed scope")
    for value in profile["scope"]["allowedPaths"] + profile["scope"]["forbiddenPaths"] + profile["knowledge"]["paths"]:
        relative(value)
    if type(profile["knowledge"]["maxBytes"]) is not int or not 0 < profile["knowledge"]["maxBytes"] <= 10 * 1024 * 1024:
        raise ValueError("Knowledge capture must have a bounded byte limit")
    if type(profile["recovery"]["maxRounds"]) is not int or not 1 <= profile["recovery"]["maxRounds"] <= 2:
        raise ValueError("Recovery is limited to at most two rounds")
    if profile["delivery"]["draftOnly"] is not True or profile["delivery"]["autoMerge"] is not False or profile["delivery"]["autoDeploy"] is not False:
        raise ValueError("Portable delivery must be draft-only with merge/deploy disabled")
    if "guidance" in profile:
        guidance = profile["guidance"]
        allowed = {"root", "conventions", "playbook", "envelopePath", "debug"}
        if not isinstance(guidance, dict) or "root" not in guidance or set(guidance) - allowed:
            raise ValueError("Invalid guidance fields")
        if not isinstance(guidance["root"], str) or not guidance["root"] or guidance["root"].startswith("/") or ".." in guidance["root"]:
            raise ValueError("Invalid guidance fields")
    if "capabilities" in profile:
        capabilities = profile["capabilities"]
        allowed = {
            "gitnexusRepo", "smoke", "generatedApiSync", "browserUat",
            "impactUnavailable", "requiredTools",
            "layout", "defaultRepo", "allowedRepos",
        }
        if not isinstance(capabilities, dict) or set(capabilities) - allowed:
            raise ValueError("Invalid capabilities fields")


def binding_and_profile(path: Path):
    binding = json.loads(regular(path).read_text())
    object_fields(binding, ("bindingVersion", "projectId", "machineId", "repositoryRoot", "allowedWorktreeRoot", "branch", "baseCommit", "profilePath", "profileSha256", "briefPath", "briefSha256", "knowledgeRoot", "knowledgeCommit", "engineCommand", "allowPublish"), "machine binding")
    if any(not isinstance(binding[name], str) or not binding[name] for name in ("projectId", "machineId", "branch")):
        raise ValueError("Binding requires explicit project, machine and branch identity")
    if binding.get("bindingVersion") != BINDING_VERSION:
        raise ValueError("Unsupported machine binding version")
    for name in ("repositoryRoot", "allowedWorktreeRoot", "profilePath", "briefPath", "knowledgeRoot"):
        if not isinstance(binding[name], str) or not Path(binding[name]).is_absolute():
            raise ValueError(f"Binding requires absolute {name}")
    for name in ("baseCommit", "knowledgeCommit"):
        if not SHA.fullmatch(binding[name]):
            raise ValueError(f"Binding requires a frozen full Git SHA: {name}")
    for name in ("profileSha256", "briefSha256"):
        if not DIGEST.fullmatch(binding[name]):
            raise ValueError(f"Binding requires SHA-256: {name}")
    if type(binding.get("allowPublish")) is not bool or not binding.get("machineId"):
        raise ValueError("Binding requires machine identity and explicit publish authorization")
    profile_bytes = regular(Path(binding["profilePath"])).read_bytes()
    if digest(profile_bytes) != binding["profileSha256"]:
        raise ValueError("profile digest drift")
    if file_digest(Path(binding["briefPath"])) != binding["briefSha256"]:
        raise ValueError("brief digest drift")
    command = binding.get("engineCommand")
    if not isinstance(command, list) or not command or any(not isinstance(arg, str) or not arg for arg in command) or not Path(command[0]).is_absolute():
        raise ValueError("Binding requires explicit qualified engineCommand argv")
    profile = json.loads(profile_bytes)
    validate_profile(profile)
    if binding["projectId"] != profile["projectId"]:
        raise ValueError("Binding/profile project mismatch")
    root = Path(binding["repositoryRoot"])
    allowed = Path(binding["allowedWorktreeRoot"]).resolve()
    if root.is_symlink() or root.resolve() == allowed or allowed not in root.resolve().parents:
        raise ValueError("Repository is outside the allowed worktree root")
    if not (root / ".git").is_file() or (root / ".git").is_symlink():
        raise ValueError("Portable execution requires a linked worktree, never a manual checkout")
    git_dir = Path(git_text(root, "rev-parse", "--absolute-git-dir")).resolve()
    common_dir = Path(git_text(root, "rev-parse", "--git-common-dir"))
    common_dir = (root / common_dir).resolve() if not common_dir.is_absolute() else common_dir.resolve()
    registered = git(root, "worktree", "list", "--porcelain", "-z").split(b"\0")
    if git_dir == common_dir or b"worktree " + str(root.resolve()).encode() not in registered or regular(git_dir / "gitdir").read_text().strip() != str((root / ".git").resolve()):
        raise ValueError("Expected an actual registered linked worktree")
    if Path(git_text(root, "rev-parse", "--show-toplevel")).resolve() != root.resolve():
        raise ValueError("Repository root does not match actual Git root")
    if git_text(root, "branch", "--show-current") != binding["branch"] or binding["branch"] in (profile["repository"]["defaultBranch"], profile["delivery"].get("baseBranch")):
        raise ValueError("Unexpected or protected branch")
    if remote_identity(git_text(root, "remote", "get-url", "origin")) != remote_identity(profile["repository"]["remote"]):
        raise ValueError("Repository remote mismatch")
    if git_text(root, "rev-parse", binding["baseCommit"] + "^{commit}") != binding["baseCommit"]:
        raise ValueError("Frozen base commit unavailable")
    if profile["profileVersion"] == source_recipes.VERSION:
        source_recipes.check_metadata(profile, root)
    else:
        package = json.loads(regular(root / "package.json").read_text())
        if package.get("packageManager") != profile["repository"]["packageManager"]:
            raise ValueError("Package-manager profile drift")
    return binding, profile, root


def capture(binding_path: Path, artifacts: Path) -> dict:
    binding, profile, root = binding_and_profile(binding_path)
    repair = repair_publication.from_environment(binding, profile)
    if repair is not None:
        repair_publication.assert_remote_head(root, repair)
    if git_text(root, "rev-parse", "HEAD") != binding["baseCommit"]:
        raise ValueError("Repository HEAD does not match frozen base commit")
    if not (artifacts / "run-context.json").exists() and git(root, "status", "--porcelain"):
        raise ValueError("New run requires a clean isolated worktree")
    artifacts.mkdir(parents=True, exist_ok=True)
    toolchain = source_recipes.facts(profile, root) if profile["profileVersion"] == source_recipes.VERSION else {}
    if profile["profileVersion"] == PROFILE_VERSION and any(check["argv"][0] == "pnpm" for check in profile["verification"]):
        actual = subprocess.run(["pnpm", "--version"], cwd=root, check=True, capture_output=True, text=True, timeout=60).stdout.strip()
        expected = profile["repository"]["packageManager"].removeprefix("pnpm@")
        if actual != expected:
            raise ValueError(f"Package-manager executable mismatch: expected {expected}, found {actual}")
        toolchain["pnpm"] = actual
    kb = Path(binding["knowledgeRoot"])
    if git_text(kb, "rev-parse", binding["knowledgeCommit"] + "^{commit}") != binding["knowledgeCommit"]:
        raise ValueError("Frozen knowledge commit unavailable")
    entries = git(kb, "ls-tree", "-r", "-z", binding["knowledgeCommit"], "--", *profile["knowledge"]["paths"]).split(b"\0")
    knowledge = {}
    total = 0
    for entry in filter(None, entries):
        metadata, raw_path = entry.split(b"\t", 1)
        mode, kind, blob = metadata.decode().split()
        name = relative(raw_path.decode())
        if not review_evidence_path(name):
            raise ValueError("Knowledge path is not permitted by the engine evidence policy")
        if mode not in ("100644", "100755") or kind != "blob":
            raise ValueError("Knowledge capture refuses symlinks and submodules")
        content = git(kb, "cat-file", "blob", blob)
        total += len(content)
        if total > profile["knowledge"]["maxBytes"]:
            raise ValueError("Knowledge capture exceeds profile byte limit")
        target = artifacts / "knowledge" / name
        if artifacts.resolve() not in target.resolve().parents:
            raise ValueError("Knowledge snapshot escapes the run artifacts")
        if target.exists() and regular(target).read_bytes() != content:
            raise ValueError("knowledge snapshot drift")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        knowledge[name] = digest(content)
    if not knowledge:
        raise ValueError("Required pinned knowledge context is empty")
    brief = regular(Path(binding["briefPath"])).read_bytes()
    if digest(brief) != binding["briefSha256"]:
        raise ValueError("brief digest drift")
    brief_target = artifacts / "brief.md"
    if brief_target.exists() and regular(brief_target).read_bytes() != brief:
        raise ValueError("brief snapshot drift")
    brief_target.write_bytes(brief)
    documents, rules = repo_policy.extract_rules(root, binding["baseCommit"])
    policy = {"documents": documents, "rules": rules}
    write(artifacts / "policy-context.json", policy, immutable=True)
    context = {"bindingPath": str(binding_path.resolve()), "bindingSha256": file_digest(binding_path), "binding": binding, "profile": profile, "toolchain": toolchain, "knowledgeFiles": knowledge, "policyDigest": digest(encoded(policy))}
    if repair is not None:
        context["repairPublication"] = repair
    factory_execution = factory_execution_from_env(artifacts)
    if factory_execution is not None:
        context["factoryExecution"] = factory_execution
    write(artifacts / "run-context.json", context, immutable=True)
    write(artifacts / "context-seal.json", {"sha256": digest(encoded(context))}, immutable=True)
    return {"contextDigest": digest(encoded(context)), "contextPath": str(artifacts / "run-context.json"), "repositoryRoot": str(root)}


def load_context(artifacts: Path) -> dict:
    content = regular(artifacts / "run-context.json").read_bytes()
    if digest(content) != json.loads(regular(artifacts / "context-seal.json").read_text())["sha256"]:
        raise ValueError("context snapshot drift")
    context = json.loads(content)
    source_binding = Path(os.environ.get("INPUTS_BINDING", context["bindingPath"]))
    if str(source_binding.resolve()) != context["bindingPath"] or file_digest(source_binding) != context["bindingSha256"]:
        raise ValueError("binding snapshot drift")
    binding, profile, root = binding_and_profile(source_binding)
    if profile != context["profile"] or binding != context["binding"]:
        raise ValueError("binding snapshot drift")
    if profile["profileVersion"] == source_recipes.VERSION and source_recipes.facts(profile, root) != context["toolchain"]:
        raise ValueError("Qualified source-recipe toolchain drift")
    for name, expected in context["knowledgeFiles"].items():
        if file_digest(artifacts / "knowledge" / relative(name)) != expected:
            raise ValueError("knowledge snapshot drift")
    if file_digest(artifacts / "brief.md") != binding["briefSha256"] or file_digest(artifacts / "policy-context.json") != context["policyDigest"]:
        raise ValueError("context snapshot drift")
    return context


def allowed(profile: dict, name: str) -> bool:
    relative(name)
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in profile["scope"]["allowedPaths"]) and not any(fnmatch.fnmatchcase(name, pattern) for pattern in profile["scope"]["forbiddenPaths"])


def seal_plan(artifacts: Path, plan: dict, critique: dict) -> dict:
    context = load_context(artifacts)
    # Re-derive the packet from the bridge-owned binding and committed KB, so
    # a planning role cannot choose new evidence paths by editing local metadata.
    capture(Path(context["bindingPath"]), artifacts)
    context = load_context(artifacts)
    if critique.get("approved") is not True or critique.get("findings"):
        raise ValueError("Planning critique requires revision before human review")
    files = plan.get("files")
    if not isinstance(files, list) or not files or len(set(files)) != len(files):
        raise ValueError("Plan requires a distinct nonempty file allowlist")
    if any(not allowed(context["profile"], name) for name in files):
        raise ValueError("Plan exceeds profile scope")
    for name in ("goal", "approach"):
        if not isinstance(plan.get(name), str) or not plan[name].strip():
            raise ValueError(f"Plan requires {name}")
    if not isinstance(plan.get("testScenarios"), list) or not plan["testScenarios"]:
        raise ValueError("Plan requires test scenarios")
    root = Path(context["binding"]["repositoryRoot"])
    if git(root, "status", "--porcelain") or git_text(root, "rev-parse", "HEAD") != context["binding"]["baseCommit"]:
        raise ValueError("Planning roles modified the worktree")
    sealed = {**plan, "verification": context["profile"]["verification"], "contextDigest": digest(encoded(context))}
    write(artifacts / "plan.json", sealed, immutable=True)
    write(artifacts / "plan-review.json", critique, immutable=True)
    write(artifacts / "plan-seal.json", {"sha256": digest(encoded(sealed))}, immutable=True)
    prepare_approval_evidence(artifacts, context)
    return {"planDigest": digest(encoded(sealed)), "planPath": str(artifacts / "plan.json"), "files": files}



def review_evidence_path(name: str) -> bool:
    # Mirrors the pinned approval-evidence.v1 wire policy; compatibility is
    # exercised through the real engine gate in test_portable_engine.py.
    parts = name.split("/")
    return bool(parts) and all(part and not part.startswith(".") and not part.lower().endswith("-home") and part.lower() not in ("auth.json", "credentials.json", "tokens.json", "oauth.json") for part in parts) and Path(name).suffix.lower() in (".json", ".md", ".txt", ".diff", ".patch") and "\\" not in name and ":" not in name


def approval_input_names(context: dict) -> list[str]:
    names = ["run-context.json", "context-seal.json", "brief.md", "policy-context.json", "plan.json", "plan-seal.json", "plan-review.json"]
    names.extend("knowledge/" + name for name in context["knowledgeFiles"])
    return sorted(names)


def replace_approval_evidence(artifacts: Path, names: list[str]) -> None:
    namespace = artifacts / "approval-evidence"
    if namespace.is_symlink():
        raise ValueError("Refusing symlink approval evidence directory")
    if len(names) > 256:
        raise ValueError("Approval evidence exceeds the engine manifest file limit")
    temporary = Path(tempfile.mkdtemp(prefix="approval-evidence-", dir=artifacts))
    try:
        for name in sorted(names):
            source = regular(artifacts / relative(name))
            if not review_evidence_path(name) or artifacts.resolve() not in source.resolve().parents:
                raise ValueError("Approval source is outside the permitted evidence scope")
            target = temporary / name
            if temporary.resolve() not in target.resolve().parents or target.is_symlink():
                raise ValueError("Approval evidence path escapes its namespace")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        write(temporary / "manifest.json", {"version": 1, "files": sorted(names)}, immutable=True)
        if namespace.exists():
            if not namespace.is_dir() or namespace.is_symlink():
                raise ValueError("Refusing unsafe approval evidence replacement")
            shutil.rmtree(namespace)
        os.replace(temporary, namespace)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def prepare_approval_evidence(artifacts: Path, context: dict) -> None:
    replace_approval_evidence(artifacts, approval_input_names(context))


def plan_for(artifacts: Path) -> dict:
    plan = regular(artifacts / "plan.json").read_bytes()
    if digest(plan) != json.loads(regular(artifacts / "plan-seal.json").read_text())["sha256"]:
        raise ValueError("approved plan drift")
    return json.loads(plan)


def engine_query(context: dict, *args: str) -> dict:
    command = context["binding"].get("engineCommand")
    if not isinstance(command, list) or not command or any(not isinstance(arg, str) or not arg for arg in command):
        raise ValueError("Actual qualified engineCommand is required")
    response = subprocess.run([*command, "workflow", *args, "--json"], check=True, capture_output=True, text=True, timeout=60)
    return json.loads(response.stdout)


def verify_current_gate_approval(artifacts: Path, gate_id: str, names: list[str], proof_name: str) -> dict:
    context = load_context(artifacts)
    run_id = os.environ.get("WORKFLOW_ID")
    if not run_id:
        raise ValueError("Actual engine run identity is required")
    result = engine_query(context, "get", run_id)
    run = result.get("run", result)
    approval = run.get("metadata", {}).get("approval", {})
    if run.get("id") != run_id or run.get("status") != "running" or approval.get("nodeId") != gate_id or approval.get("resolved") != "approved":
        raise ValueError(f"Engine has not approved this run's {gate_id} gate")
    sealed = engine_query(context, "gate-evidence", run_id, approval["occurrenceId"])
    if sealed.get("evidenceDigest") != approval.get("evidenceDigest") or sealed.get("runId") != run_id:
        raise ValueError("Engine approval evidence binding mismatch")
    if sealed["evidence"].get("artifactPolicy") != "approval-evidence.v1":
        raise ValueError("Engine does not expose the qualified safe evidence policy")
    originals = sealed["evidence"]["artifacts"]
    if sorted(originals) != sorted(names):
        raise ValueError("Engine evidence differs from the deterministic producer allowlist")
    file_hashes = {}
    for name in sorted(names):
        original = base64.b64decode(originals[name], validate=True)
        if regular(artifacts / name).read_bytes() != original:
            raise ValueError(f"Engine-approved original evidence drift: {name}")
        file_hashes[name] = digest(original)
    proof = {
        "runId": run_id,
        "nodeId": gate_id,
        "occurrenceId": approval["occurrenceId"],
        "evidenceDigest": approval["evidenceDigest"],
        "files": file_hashes,
    }
    write(artifacts / proof_name, proof, immutable=True)
    return proof


def verify_approval(artifacts: Path) -> dict:
    return verify_current_gate_approval(artifacts, "plan-approval", approval_input_names(load_context(artifacts)), "approval-binding.json")


def verify_stored_plan_approval(artifacts: Path) -> dict:
    proof = json.loads(regular(artifacts / "approval-binding.json").read_text())
    if proof.get("nodeId") not in (None, "plan-approval"):
        raise ValueError("Stored approval binding is not for the plan gate")
    files = proof.get("files")
    if not isinstance(files, dict):
        context = load_context(artifacts)
        files = {name: file_digest(artifacts / name) for name in approval_input_names(context)}
    for name, expected in files.items():
        if not isinstance(name, str) or not isinstance(expected, str) or not DIGEST.fullmatch(expected):
            raise ValueError("Stored approval binding has invalid file digests")
        if file_digest(artifacts / name) != expected:
            raise ValueError(f"Engine-approved original evidence drift: {name}")
    return proof


def changed_paths(root: Path, base: str) -> list[str]:
    paths = git(root, "diff", "--no-ext-diff", "--no-renames", "--name-only", "-z", base).split(b"\0")
    paths += git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
    return sorted(set(relative(name.decode()) for name in paths if name))


def work_product(context: dict, plan: dict) -> dict:
    root = Path(context["binding"]["repositoryRoot"])
    records = []
    for name in changed_paths(root, context["binding"]["baseCommit"]):
        if name not in plan["files"] or not allowed(context["profile"], name):
            raise ValueError(f"Work product exceeds approved scope: {name}")
        path = root / name
        if path.is_symlink() or (path.exists() and root.resolve() not in path.resolve().parents):
            raise ValueError(f"Unsafe work-product path: {name}")
        records.append({"path": name, "sha256": file_digest(path) if path.exists() else None, "executable": bool(path.stat().st_mode & stat.S_IXUSR) if path.exists() else False})
    return {"files": records, "sha256": digest(encoded(records))}


def verification_record(context: dict, artifacts: Path, name: str, round_no: int | None = None) -> dict:
    plan = plan_for(artifacts)
    root = Path(context["binding"]["repositoryRoot"])
    if git_text(root, "rev-parse", "HEAD") != context["binding"]["baseCommit"]:
        raise ValueError("Implementation committed before the mechanical publication step")
    before = work_product(context, plan)
    results = []
    for check in context["profile"]["verification"]:
        prefix = f"round-{round_no}-" if round_no is not None else f"{name}-"
        log = artifacts / "verification" / f"{prefix}{check['id']}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("wb") as output:
            try:
                completed = subprocess.run(source_recipes.verification_argv(context["profile"], context["toolchain"], check["argv"]), cwd=root, stdout=output, stderr=subprocess.STDOUT, timeout=check["timeoutSeconds"])
                code = completed.returncode
            except subprocess.TimeoutExpired:
                code = 124
        results.append({"id": check["id"], "argv": check["argv"], "exitCode": code, "logPath": str(log)})
    after = work_product(context, plan)
    if before != after:
        raise ValueError("Verification commands modified the reviewed work product")
    result = {"passed": all(item["exitCode"] == 0 for item in results), "commands": results, "workProduct": after}
    if round_no is not None:
        result["round"] = round_no
    write(artifacts / name, result)
    return result


def baseline_verify(artifacts: Path) -> dict:
    return verification_record(load_context(artifacts), artifacts, "baseline-verification.json")


def begin_round(artifacts: Path) -> dict:
    context = load_context(artifacts)
    repair = context.get("repairPublication")
    if repair != repair_publication.from_environment(context["binding"], context["profile"]):
        raise ValueError("Frozen repair publication context drift")
    if repair is not None:
        repair_publication.assert_current_authority(repair)
        repair_publication.assert_remote_head(Path(context["binding"]["repositoryRoot"]), repair)
        repair_publication.assert_scope(Path(context["binding"]["repositoryRoot"]), repair)
    plan_for(artifacts)
    state_file = artifacts / "recovery-state.json"
    state = json.loads(regular(state_file).read_text()) if state_file.exists() else {"round": 0}
    if type(state.get("round")) is not int or state["round"] < 0:
        raise ValueError("Invalid durable recovery counter")
    if state["round"] >= context["profile"]["recovery"]["maxRounds"]:
        raise ValueError("Durable recovery round limit reached")
    state["round"] += 1
    write(state_file, state)
    return state


def verify(artifacts: Path) -> dict:
    context = load_context(artifacts)
    round_no = json.loads(regular(artifacts / "recovery-state.json").read_text())["round"]
    return verification_record(context, artifacts, "verification.json", round_no)


def record_no_change_claim(artifacts: Path, implementation: dict) -> dict:
    context = load_context(artifacts)
    plan = plan_for(artifacts)
    category = implementation.get("category")
    if category is None:
        remove_artifact(artifacts / "no-change-claim.json")
        remove_artifact(artifacts / "no-change-closure-intent.json")
        result = {"recorded": False, "reason": "implementation did not declare an outcome category"}
        write(artifacts / "no-change-claim-status.json", result)
        return result
    if category not in ("changes-produced", "already-satisfied", "blocked", "inconclusive"):
        raise ValueError("Implementation outcome category is invalid")
    if category != "already-satisfied":
        remove_artifact(artifacts / "no-change-claim.json")
        remove_artifact(artifacts / "no-change-closure-intent.json")
        result = {"recorded": False, "category": category}
        write(artifacts / "no-change-claim-status.json", result)
        return result
    product = work_product(context, plan)
    if product["files"]:
        raise ValueError("already-satisfied claim cannot accompany changed work product")
    round_file = artifacts / "recovery-state.json"
    round_no = json.loads(regular(round_file).read_text())["round"] if round_file.exists() else None
    claim = {
        "schemaVersion": 1,
        "source": "agent",
        "category": "already-satisfied",
        "summary": implementation.get("summary", ""),
        "workProduct": product,
        "round": round_no,
    }
    write(artifacts / "no-change-claim.json", claim)
    result = {"recorded": True, "category": "already-satisfied"}
    write(artifacts / "no-change-claim-status.json", result)
    return result


def passed_ids(record: dict) -> set[str]:
    if record.get("passed") is not True:
        return set()
    commands = record.get("commands")
    if not isinstance(commands, list):
        return set()
    return {
        item["id"]
        for item in commands
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and item.get("exitCode") == 0
    }


def verified_empty_work_product(record: dict, label: str) -> dict:
    product = record.get("workProduct")
    if not isinstance(product, dict):
        raise ValueError(f"{label} verification is missing work product proof")
    if not isinstance(product.get("sha256"), str) or not DIGEST.fullmatch(product["sha256"]):
        raise ValueError(f"{label} verification has invalid work product digest")
    if not isinstance(product.get("files"), list):
        raise ValueError(f"{label} verification has invalid work product file list")
    if product["files"]:
        raise ValueError("no-change closure cannot carry a changed work product")
    return product


def no_change_closure(artifacts: Path) -> dict:
    context = load_context(artifacts)
    profile = context["profile"]
    policy = profile.get("noChangeClosure", {"enabled": False, "verifierIds": []})
    if policy.get("enabled") is not True:
        result = {"eligible": False, "reason": "disabled"}
        write(artifacts / "no-change-closure-status.json", result)
        return result
    verifier_ids = policy.get("verifierIds", [])
    if not isinstance(verifier_ids, list) or not verifier_ids:
        raise ValueError("no-change closure has no pinned verifier ids")
    verifier_set = set(verifier_ids)
    if not (artifacts / "no-change-claim.json").exists():
        result = {"eligible": False, "reason": "missing already-satisfied claim"}
        write(artifacts / "no-change-closure-status.json", result)
        return result
    claim = json.loads(regular(artifacts / "no-change-claim.json").read_text())
    if claim.get("category") != "already-satisfied" or claim.get("source") != "agent":
        raise ValueError("claim is not an agent already-satisfied no-change claim")
    baseline = json.loads(regular(artifacts / "baseline-verification.json").read_text())
    final = json.loads(regular(artifacts / "verification.json").read_text())
    baseline_product = verified_empty_work_product(baseline, "baseline")
    final_product = verified_empty_work_product(final, "final")
    if claim.get("workProduct") != baseline_product:
        raise ValueError("claim work product does not match trusted baseline proof")
    if "round" in final and claim.get("round") != final.get("round"):
        raise ValueError("no-change claim is stale for the final verification round")
    if not verifier_set <= passed_ids(baseline):
        raise ValueError("baseline verification did not pass every pinned verifier")
    if not verifier_set <= passed_ids(final):
        raise ValueError("final verification did not pass every pinned verifier")
    if baseline_product != final_product:
        raise ValueError("work product changed between baseline and final verification")
    intent = {
        "schemaVersion": 1,
        "lifecycleResult": "fulfilled-no-change",
        "claim": claim,
        "verifierIds": verifier_ids,
        "workProduct": final_product,
    }
    write(artifacts / "no-change-closure-intent.json", intent)
    write(artifacts / "no-change-closure-status.json", {"eligible": True, "lifecycleResult": "fulfilled-no-change"})
    return intent


def converge(artifacts: Path, review: dict) -> dict:
    context = load_context(artifacts)
    verification = json.loads(regular(artifacts / "verification.json").read_text())
    if verification["workProduct"] != work_product(context, plan_for(artifacts)):
        raise ValueError("Review modified the verified work product")
    if review.get("verdict") not in REVIEW_VERDICTS or review.get("status") not in ("complete", "degraded", "failed"):
        raise ValueError("Malformed review envelope")
    result = {"ready": verification["passed"] and review["status"] == "complete" and review["verdict"] == "Ready to merge" and not review.get("findings"), "review": review, "verification": verification}
    write(artifacts / "review.json", result)
    return result


def ship(artifacts: Path, pr: dict) -> dict:
    context = load_context(artifacts)
    repair = context.get("repairPublication")
    if repair != repair_publication.from_environment(context["binding"], context["profile"]):
        raise ValueError("Frozen repair publication context drift")
    if repair is not None:
        repair_publication.assert_current_authority(repair)
        repair_publication.assert_remote_head(Path(context["binding"]["repositoryRoot"]), repair)
        repair_publication.assert_scope(Path(context["binding"]["repositoryRoot"]), repair)
    if (artifacts / "no-change-closure-intent.json").exists():
        intent = no_change_closure(artifacts)
        if intent.get("lifecycleResult") != "fulfilled-no-change":
            raise ValueError(f"No-change closure intent is no longer valid: {intent.get('reason', 'invalid')}")
        if intent.get("workProduct") != work_product(context, plan_for(artifacts)):
            raise ValueError("No-change closure intent no longer matches current work product")
        review = json.loads(regular(artifacts / "review.json").read_text())
        if review.get("ready") is not True or review.get("verification", {}).get("workProduct") != intent.get("workProduct"):
            raise ValueError("No-change closure requires final reviewed verification proof")
        result = {
            "ready": True,
            "draft": False,
            "status": "fulfilled-no-change",
            "lifecycleResult": "fulfilled-no-change",
            "workProduct": intent.get("workProduct"),
        }
        write(artifacts / "pr-evidence.json", result)
        return result
    binding = context["binding"]
    review = json.loads(regular(artifacts / "review.json").read_text())
    product = work_product(context, plan_for(artifacts))
    if not review["ready"] or product != review["verification"]["workProduct"] or not product["files"]:
        raise ValueError("Publication requires the exact nonempty verified/reviewed work product")
    if not isinstance(pr.get("title"), str) or not pr["title"].strip() or len(pr["title"]) > 120 or not isinstance(pr.get("body"), str) or not pr["body"].strip():
        raise ValueError("PR title and body are required")
    if not binding["allowPublish"]:
        result = {"ready": False, "status": "publication_requires_authorization", "draft": pr, "workProduct": product}
        write(artifacts / "pr-evidence.json", result)
        return result
    root = Path(binding["repositoryRoot"])
    origin = remote_identity(context["profile"]["repository"]["remote"])
    if not origin.startswith("github.com/"):
        raise ValueError("Draft publication requires an explicit GitHub repository")
    github_repo = origin.removeprefix("github.com/")
    if repair is not None:
        repair_publication.inspect_managed_pr(root, repair, repair["executionBaseRevision"])
    body_file = artifacts / "pr-body.md"
    if body_file.is_symlink():
        raise ValueError("Refusing symlink PR body artifact")
    body_file.write_text(pr["body"])
    receipt_file = artifacts / "commit-receipt.json"
    if receipt_file.exists():
        head = json.loads(regular(receipt_file).read_text())["head"]
        if git_text(root, "rev-parse", "HEAD") != head:
            raise ValueError("Commit receipt drift")
    else:
        if git_text(root, "rev-parse", "HEAD") != binding["baseCommit"]:
            raise ValueError("Uncertain commit ownership; inspect before any publication retry")
        git(root, "add", "--", *[item["path"] for item in product["files"]])
        message = pr["title"] + "\n\n" + plan_for(artifacts)["goal"] + "\n\n" + "\n".join([
            "Constraint: Operator-approved original plan and project scope",
            "Tested: " + "; ".join(check["id"] for check in context["profile"]["verification"]),
            "Not-tested: Behavior outside the approved verification profile",
            "Directive: Merge and deployment require separate operator authorization",
            "Confidence: medium", "Scope-risk: narrow",
        ])
        git(root, "commit", "-m", message)
        if work_product(context, plan_for(artifacts)) != product or git(root, "status", "--porcelain"):
            raise ValueError("Post-commit-hook work product changed; re-review required before push")
        head = git_text(root, "rev-parse", "HEAD")
        write(receipt_file, {"head": head, "workProduct": product}, immutable=True)
    if repair is not None:
        repair_publication.publish(root, repair, head)
        existing_pr = repair_publication.inspect_managed_pr(root, repair, head)
        url, draft = existing_pr["url"], existing_pr["draft"]
    else:
        git(root, "push", "origin", f"HEAD:refs/heads/{binding['branch']}")
        listed = subprocess.run(["gh", "pr", "list", "--repo", github_repo, "--head", binding["branch"], "--state", "open", "--json", "url,headRefOid,isDraft"], cwd=root, check=True, capture_output=True, text=True)
        matches = json.loads(listed.stdout)
        if matches:
            if len(matches) != 1 or matches[0]["headRefOid"] != head or matches[0]["isDraft"] is not True:
                raise ValueError("Existing PR does not match this draft head")
            url = matches[0]["url"]
        else:
            created = subprocess.run(["gh", "pr", "create", "--repo", github_repo, "--draft", "--base", context["profile"]["delivery"].get("baseBranch", context["profile"]["repository"]["defaultBranch"]), "--head", binding["branch"], "--title", pr["title"], "--body-file", str(body_file)], cwd=root, check=True, capture_output=True, text=True)
            url = created.stdout.strip()
        draft = True
    if not re.fullmatch(r"https://github\.com/" + re.escape(github_repo) + r"/pull/[0-9]+", url, re.I):
        raise ValueError("Unexpected PR evidence URL")
    result = {"ready": True, "draft": draft, "url": url, "head": head, "baseCommit": binding["baseCommit"], "workProduct": product}
    manifest = merge_review_manifest(context, github_repo, url, head)
    if manifest is not None:
        write(artifacts / "merge-review-manifest.json", manifest, immutable=True)
        result["mergeReviewManifest"] = manifest
        result["mergeReviewManifestPath"] = str(artifacts / "merge-review-manifest.json")
    write(artifacts / "pr-evidence.json", result)
    return result



def merge_review_names() -> list[str]:
    return ["merge-review-manifest.json", "pr-evidence.json"]


def merge_review_route(artifacts: Path) -> dict:
    evidence = json.loads(regular(artifacts / "pr-evidence.json").read_text())
    required = bool(evidence.get("ready") is True and evidence.get("mergeReviewManifestPath"))
    if required:
        manifest_path = Path(evidence["mergeReviewManifestPath"])
        if manifest_path.resolve() != (artifacts / "merge-review-manifest.json").resolve():
            raise ValueError("Managed PR merge review manifest path drift")
        manifest = json.loads(regular(manifest_path).read_text())
        if manifest != evidence.get("mergeReviewManifest") or manifest.get("kind") != "factory-merge-review.v1":
            raise ValueError("Managed PR merge review manifest mismatch")
    result = {"required": required, "status": evidence.get("status", "ready" if required else "not-required")}
    write(artifacts / "merge-review-route.json", result)
    return result


def prepare_merge_review(artifacts: Path) -> dict:
    route = merge_review_route(artifacts)
    if route.get("required") is not True:
        raise ValueError("Merge review is not required for this run")
    replace_approval_evidence(artifacts, merge_review_names())
    manifest = json.loads(regular(artifacts / "merge-review-manifest.json").read_text())
    result = {"prepared": True, "manifest": manifest, "files": merge_review_names()}
    write(artifacts / "merge-review-evidence.json", result)
    return result


def bind_merge_review(artifacts: Path) -> dict:
    route = json.loads(regular(artifacts / "merge-review-route.json").read_text())
    if route.get("required") is not True:
        raise ValueError("Merge review binding requires a managed PR route")
    proof = verify_current_gate_approval(artifacts, "merge-review", merge_review_names(), "merge-review-approval-binding.json")
    manifest = json.loads(regular(artifacts / "merge-review-manifest.json").read_text())
    proof["manifest"] = manifest
    write(artifacts / "merge-review-approval-proof.json", proof, immutable=True)
    return proof

def capture_knowledge(artifacts: Path, proposed: dict) -> dict:
    load_context(artifacts)
    if not isinstance(proposed.get("summary"), str) or not isinstance(proposed.get("promotionCandidates"), list):
        raise ValueError("Knowledge proposal requires summary and promotion candidates")
    if any(not isinstance(item, str) for item in proposed["promotionCandidates"]):
        raise ValueError("Knowledge promotion candidates must be text")
    write(artifacts / "kb-capture.json", {"summary": proposed["summary"], "promotionCandidates": proposed["promotionCandidates"], "status": "proposed"}, immutable=True)
    return {"path": str(artifacts / "kb-capture.json"), "status": "proposed", "externalWrites": False}


def main() -> None:
    artifacts = Path(os.environ["ARTIFACTS_DIR"]).resolve()
    action = os.environ["INPUTS_ACTION"]
    if action == "preflight":
        result = capture(Path(os.environ["INPUTS_BINDING"]), artifacts)
    elif action == "seal-plan":
        result = seal_plan(artifacts, json.loads(os.environ["INPUTS_PLAN"]), json.loads(os.environ["INPUTS_CRITIQUE"]))
    elif action == "bind-approval":
        result = verify_approval(artifacts)
    else:
        verify_stored_plan_approval(artifacts)
        if action == "baseline-verify":
            result = baseline_verify(artifacts)
        elif action == "begin-round":
            result = begin_round(artifacts)
        elif action == "record-no-change-claim":
            result = record_no_change_claim(artifacts, json.loads(os.environ["INPUTS_IMPLEMENTATION"]))
        elif action == "verify":
            result = verify(artifacts)
        elif action == "converge":
            result = converge(artifacts, json.loads(os.environ["INPUTS_REVIEW"]))
        elif action == "no-change-closure":
            result = no_change_closure(artifacts)
        elif action == "ship":
            result = ship(artifacts, json.loads(os.environ["INPUTS_PR"]))
            if not result["ready"]:
                raise ValueError("Draft publication not authorized; run-local PR intent recorded")
        elif action == "merge-review-route":
            result = merge_review_route(artifacts)
        elif action == "prepare-merge-review":
            result = prepare_merge_review(artifacts)
        elif action == "bind-merge-review":
            result = bind_merge_review(artifacts)
        elif action == "capture-knowledge":
            result = capture_knowledge(artifacts, json.loads(os.environ["INPUTS_PROPOSAL"]))
        elif action == "report":
            result = json.loads(regular(artifacts / "pr-evidence.json").read_text())
        else:
            raise ValueError("Unknown portable workflow action")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        raise SystemExit(1)
