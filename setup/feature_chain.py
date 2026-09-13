#!/usr/bin/env python3
"""Repository-list feature-chain controller helpers.

This module is intentionally launcher-shaped, not a second launcher.  The
existing provider-neutral launcher owns CLI parsing, process supervision, and
private control files; these helpers own the new schema-v2 chain state and the
strict transitions needed for repository-list feature runs.
"""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import control_contract


class FeatureChainError(ValueError):
    pass


REGISTERED_REPOSITORIES = ("api", "goodword-mcp", "web-app")
REPOSITORY_ALIASES = {"web": "web-app"}
DEFAULT_WALL_MINUTES = 240
DEFAULT_MAX_TOTAL_TOKENS = 30_000_000
DEFAULT_PLANNING_LANE = {"claude": "full-sdlc-api", "codex": "full-sdlc-api-codex"}
DEFAULT_REPOSITORY_LANES = {
    "claude": {
        "api": "full-sdlc-api",
        "goodword-mcp": "full-sdlc-api",
        "web-app": "full-sdlc-web",
    },
    "codex": {
        "api": "full-sdlc-api-codex",
        "goodword-mcp": "full-sdlc-api-codex",
        "web-app": "full-sdlc-web-codex",
    },
}
DEFAULT_INTEGRATION_LANE = {"claude": "full-sdlc-api", "codex": "full-sdlc-api-codex"}
JOINT_PLAN_ARTIFACT = "joint-plan.json"
PLANNING_REQUEST_ARTIFACT = "feature-chain-request.json"
PRIOR_PLANNING_EVIDENCE_ARTIFACT = "prior-planning-evidence.json"
INTEGRATION_EVIDENCE_ARTIFACT = "integration-evidence.json"
PLANNING_SUPPORT_ARTIFACTS = (
    "plan.md", "premises.json", "reader-audit.json", "web-premises.json",
    "web-reader-audit.json", "browser-evidence.json", "browser-evidence.sha256",
    "smoke-probe.json", "kb-context.md", "docreview-envelope.txt", "docreview.diff",
    "plan-review.html", "plan-round.txt", "plan.post-critic.md", "plan.post-docreview.md",
)
PLANNING_ROUND_EVIDENCE = ("critique.json", "revision.json", "impact.json")
COMMIT_RE = re.compile(r"[0-9a-f]{40}", re.I)
CHAIN_ID_RE = re.compile(r"[0-9a-f]{24,64}", re.I)
RUN_ID_RE = re.compile(r"(?=.{8,36}\Z)[0-9a-f]+(?:-[0-9a-f]+)*", re.I)
SIGNED_FIELDS = {"state_mac", "approval_mac", "receipt_mac"}
TERMINAL_OK = {"completed", "locally_verified"}


def canonical_bytes(data: Any) -> bytes:
    return control_contract.canonical_bytes(data)


def digest(data: Any) -> str:
    return hashlib.sha256(canonical_bytes(data)).hexdigest()


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hmac_sha256(secret: str, data: Any) -> str:
    try:
        return control_contract.hmac_sha256(secret, data)
    except control_contract.ControlContractError as exc:
        raise FeatureChainError(str(exc)) from exc


def signed_body(value: dict) -> dict:
    return {k: v for k, v in value.items() if k not in SIGNED_FIELDS}


def _secure_read(path: Path) -> dict:
    try:
        return control_contract.secure_read_json(path)
    except control_contract.ControlContractError as exc:
        raise FeatureChainError(str(exc)) from exc


def _secure_write(path: Path, data: dict) -> None:
    try:
        control_contract.secure_write_json(path, data)
    except control_contract.ControlContractError as exc:
        raise FeatureChainError(str(exc)) from exc


def _ensure_private_dir(path: Path, label: str) -> Path:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    info = path.lstat()
    if not info or not path.is_dir() or path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise FeatureChainError(f"{label} must be an owned mode-0700 real directory: {path}")
    return path


def ensure_control_dir(control_dir: Path) -> Path:
    return _ensure_private_dir(control_dir, "feature control directory")


def state_dir(control_dir: Path) -> Path:
    ensure_control_dir(control_dir)
    return _ensure_private_dir(control_dir / "feature-chains-v2", "feature chain directory")


def state_path(control_dir: Path, chain_id: str) -> Path:
    if not isinstance(chain_id, str) or not CHAIN_ID_RE.fullmatch(chain_id):
        raise FeatureChainError(f"bad feature chain id: {chain_id}")
    return state_dir(control_dir) / f"{chain_id}.json"


def lock_path(control_dir: Path, chain_id: str) -> Path:
    return state_dir(control_dir) / f"{chain_id}.lock"


@contextmanager
def chain_lock(control_dir: Path, chain_id: str) -> Iterator[None]:
    path = lock_path(control_dir, chain_id)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def seal_state(state: dict) -> dict:
    sealed = dict(state)
    sealed["state_mac"] = hmac_sha256(sealed["chain_secret"], signed_body(sealed))
    return sealed


def verify_state(state: dict, chain_id: str | None = None) -> dict:
    if chain_id is not None and state.get("logical_chain_id") != chain_id:
        raise FeatureChainError("feature chain state id mismatch")
    if state.get("schema_version") != 2:
        raise FeatureChainError("feature chain state is not schema v2")
    mac = state.get("state_mac")
    if not isinstance(mac, str) or not hmac.compare_digest(mac, hmac_sha256(state.get("chain_secret", ""), signed_body(state))):
        raise FeatureChainError("feature chain state MAC mismatch")
    return state


def read_state(control_dir: Path, chain_id: str) -> dict:
    return verify_state(_secure_read(state_path(control_dir, chain_id)), chain_id)


def write_state(control_dir: Path, state: dict) -> dict:
    sealed = seal_state(state)
    _secure_write(state_path(control_dir, sealed["logical_chain_id"]), sealed)
    return sealed


def require_no_incomplete_amendment(state: dict) -> None:
    amendment = state.get("budget_amendment")
    if isinstance(amendment, dict) and amendment.get("status") == "in_progress":
        raise FeatureChainError("budget amendment is incomplete; retry feature-budget-update before dispatch")


def require_no_incomplete_scope_amendment(state: dict) -> None:
    amendment = state.get("scope_amendment")
    if isinstance(amendment, dict) and amendment.get("status") == "in_progress":
        raise FeatureChainError("scope amendment is incomplete; retry feature-scope-amend before dispatch")


def _root(host: Any) -> Path:
    return Path(getattr(host, "ROOT"))


def _git(repo: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *argv], capture_output=True, encoding="utf-8")


def repo_head(repo: Path, label: str, ref: str = "HEAD") -> str:
    result = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if result.returncode != 0 or not COMMIT_RE.fullmatch(result.stdout.strip()):
        raise FeatureChainError(f"cannot capture {label} commit at {repo}: {(result.stderr or result.stdout).strip()}")
    return result.stdout.strip()


def repo_is_dirty(repo: Path) -> bool:
    result = _git(repo, "status", "--porcelain", "--untracked-files=all")
    if result.returncode != 0:
        raise FeatureChainError(f"cannot inspect repository status at {repo}: {result.stderr.strip()}")
    return bool(result.stdout.strip())


def capture_baselines(host: Any, repos: list[str]) -> dict:
    root = _root(host)
    commits = {}
    dirty = {}
    for repo in repos:
        repo_path = root / repo
        if not (repo_path / ".git").exists():
            raise FeatureChainError(f"selected repository is missing: {repo}")
        commits[repo] = repo_head(repo_path, repo)
        dirty[repo] = repo_is_dirty(repo_path)
    baseline = {"commits": commits, "dirty": dirty}
    baseline["sha256"] = digest(baseline)
    return baseline


def slug_for_spec(spec: Path) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", spec.stem).strip("-._").lower()
    return slug[:48] or "feature"


def prepare_worktrees(host: Any, chain_id: str, repos: list[str], baselines: dict, slug: str) -> dict:
    root = _root(host)
    prepared = {}
    created: list[tuple[Path, str, Path]] = []
    try:
        for repo in repos:
            repo_path = root / repo
            branch = f"archon/{slug}-{chain_id[:8]}-{repo}"
            worktree = repo_path / ".worktrees" / f"{slug}-{chain_id[:8]}"
            if worktree.exists():
                raise FeatureChainError(f"feature worktree already exists for {repo}: {worktree}")
            result = _git(repo_path, "worktree", "add", "-b", branch, str(worktree), baselines["commits"][repo])
            if result.returncode != 0:
                raise FeatureChainError(f"cannot create feature worktree for {repo}: {result.stderr.strip()}")
            created.append((repo_path, branch, worktree))
            prepared[repo] = {"branch": branch, "worktree": str(worktree), "baseline": baselines["commits"][repo]}
        return prepared
    except Exception:
        for repo_path, branch, worktree in reversed(created):
            if worktree.exists() and not repo_is_dirty(worktree):
                _git(repo_path, "worktree", "remove", "--force", str(worktree))
            head = _git(repo_path, "rev-parse", "--verify", f"{branch}^{{commit}}")
            if head.returncode == 0:
                _git(repo_path, "branch", "-D", branch)
            _git(repo_path, "worktree", "prune")
        raise


def make_initial_state(host: Any, args: Any, repos: list[str]) -> dict:
    spec = Path(str(args.spec)).resolve()
    if not spec.is_absolute() or not spec.is_file():
        raise FeatureChainError("feature spec must be an existing absolute path")
    provider = str(args.provider)
    if provider not in DEFAULT_PLANNING_LANE:
        raise FeatureChainError("repository-list feature runs are unsupported for provider=" + provider)
    chain_id = secrets.token_hex(16)
    baselines = capture_baselines(host, repos)
    worktrees = prepare_worktrees(host, chain_id, repos, baselines, slug_for_spec(spec))
    wall_minutes = getattr(args, "wall_minutes", None) or DEFAULT_WALL_MINUTES
    max_total_tokens = getattr(args, "max_total_tokens", None) or DEFAULT_MAX_TOTAL_TOKENS
    state = {
        "schema_version": 2,
        "kind": "archon-feature-chain",
        "logical_chain_id": chain_id,
        "chain_secret": secrets.token_urlsafe(48),
        "provider": provider,
        "scope": "repositories",
        "executable_plan_contract": 1,
        "repositories": repos,
        "presentation_order": repos,
        "spec": str(spec),
        "spec_sha256": file_digest(spec),
        "baselines": baselines,
        "worktrees": worktrees,
        "approval": None,
        "approved_plan": None,
        "dependency_order": [],
        "stages": {repo: {"repo": repo, "status": "pending", "attempts": []} for repo in repos},
        "child_runs": {},
        "candidate_handoffs": {},
        "integration": None,
        "pending_control": None,
        "dispatch_reservation": None,
        "budget": {
            "wall_minutes": int(wall_minutes),
            "max_total_tokens": int(max_total_tokens),
            "ledger": "feature-budget.py",
        },
        "created_at": now(),
        "updated_at": now(),
    }
    if state["budget"]["wall_minutes"] <= 0 or state["budget"]["max_total_tokens"] <= 0:
        raise FeatureChainError("feature chain budgets must be positive")
    return state


def launch(host: Any, args: Any, repos: list[str]) -> dict:
    forecast = None
    if args.provider == "codex" and getattr(args, "budget_shepherd", True):
        forecast = estimate_for_scope(args, repos)
        report_forecast_allowance(forecast)
    state = make_initial_state(host, args, repos)
    if forecast is not None:
        state["budget_shepherd_version"] = 1
        state["budget_forecast"] = forecast
    state = write_state(Path(args.control_dir), state)
    budget_init(args, state)
    planning = dispatch_planning(host, args, state)
    return {"state": planning["state"], "row": planning["row"], "result": planning.get("result")}


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def validate_plan(plan: dict, repos: list[str], require_executable: bool = False) -> dict:
    if not isinstance(plan, dict):
        raise FeatureChainError("approved plan must be a JSON object")
    if plan.get("status", "approved") == "blocked":
        raise FeatureChainError("blocked joint plan cannot advance to implementation")
    with tempfile.TemporaryDirectory(prefix="archon-joint-plan-") as td:
        artifacts = Path(td)
        params = {"repositories": repos}
        if require_executable:
            params["executable_plan_contract"] = 1
        (artifacts / "params.json").write_text(json.dumps(params), encoding="utf-8")
        (artifacts / JOINT_PLAN_ARTIFACT).write_text(json.dumps(plan), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent / "validate-joint-plan.py"), str(artifacts)],
            capture_output=True,
            encoding="utf-8",
        )
        if result.returncode != 0:
            raise FeatureChainError((result.stdout or result.stderr).strip() or "joint plan validation failed")
    plan_repos = plan.get("repositories")
    if plan_repos != repos:
        raise FeatureChainError("joint plan repository set does not match chain scope")
    stages = plan_stage_map(plan)
    if not stages:
        raise FeatureChainError("joint plan must contain repository stages")
    by_repo = {}
    for repo, stage in stages.items():
        if not isinstance(stage, dict):
            raise FeatureChainError("joint plan stage must be an object")
        if repo not in repos:
            raise FeatureChainError(f"joint plan stage is outside selected scope: {repo}")
        if repo in by_repo:
            raise FeatureChainError(f"joint plan has duplicate stage for {repo}")
        deps = stage.get("depends_on", [])
        if not isinstance(deps, list) or any(dep not in repos for dep in deps):
            raise FeatureChainError(f"joint plan stage has dependency outside selected scope: {repo}")
        files = stage.get("files_allowlist")
        if not isinstance(files, list) or not files:
            raise FeatureChainError(f"joint plan stage missing file allowance for {repo}")
        tests = stage.get("test_patterns")
        verification = stage.get("verification")
        if not isinstance(tests, list) or not tests:
            raise FeatureChainError(f"joint plan stage missing test patterns for {repo}")
        if not isinstance(verification, list) or not verification:
            raise FeatureChainError(f"joint plan stage missing verification for {repo}")
        if repo == "web-app" and "api" not in deps:
            fixture = stage.get("api_fixture_worktree")
            head = stage.get("api_fixture_head_sha")
            if not isinstance(fixture, str) or not Path(fixture).is_absolute() or not isinstance(head, str) or not COMMIT_RE.fullmatch(head):
                raise FeatureChainError("web stage requires an owned API dependency or a pinned disposable API fixture")
        by_repo[repo] = dict(stage, depends_on=list(deps))
    missing = [repo for repo in repos if repo not in by_repo]
    if missing:
        raise FeatureChainError("joint plan missing selected repositories: " + ",".join(missing))
    order = topo_order({repo: by_repo[repo]["depends_on"] for repo in repos})
    contracts = plan.get("contracts")
    if not isinstance(contracts, list):
        raise FeatureChainError("joint plan must include interface contracts")
    return {"plan": plan, "stages": by_repo, "dependency_order": order}


def plan_stage_map(plan: dict) -> dict:
    stages = plan.get("stages")
    if isinstance(stages, dict):
        return stages
    if isinstance(stages, list):
        mapped = {}
        for index, stage in enumerate(stages):
            if not isinstance(stage, dict):
                raise FeatureChainError(f"joint plan stage[{index}] must be an object")
            repo = stage.get("repo")
            if not isinstance(repo, str) or not repo:
                raise FeatureChainError(f"joint plan stage[{index}] must declare repo")
            if repo in mapped:
                raise FeatureChainError(f"joint plan has duplicate stage for {repo}")
            body = dict(stage)
            body.pop("repo", None)
            mapped[repo] = body
        return mapped
    raise FeatureChainError("joint plan must contain repository stages")


def topo_order(dependencies: dict[str, list[str]]) -> list[str]:
    remaining = {repo: set(deps) for repo, deps in dependencies.items()}
    ordered: list[str] = []
    while remaining:
        ready = sorted(repo for repo, deps in remaining.items() if not deps)
        if not ready:
            raise FeatureChainError("joint plan dependency cycle detected")
        repo = ready[0]
        ordered.append(repo)
        remaining.pop(repo)
        for deps in remaining.values():
            deps.discard(repo)
    return ordered


def approval_snapshot(state: dict, plan: dict, validation: dict, source_artifacts: dict | None = None) -> dict:
    body = {
        "schema_version": 1,
        "kind": "repository-list-feature-approval",
        "logical_chain_id": state["logical_chain_id"],
        "provider": state["provider"],
        "spec": state["spec"],
        "spec_sha256": state["spec_sha256"],
        "repositories": state["repositories"],
        "baselines": state["baselines"],
        "worktrees": state["worktrees"],
        "contracts": plan["contracts"],
        "dependency_order": validation["dependency_order"],
        "verification_plan": plan["integration"],
        "plan_digest": digest(plan),
        "source_artifacts": source_artifacts or {},
    }
    if "executable_plan_contract" in state:
        body["executable_plan_contract"] = state["executable_plan_contract"]
    body["approval_digest"] = digest(body)
    body["approval_mac"] = hmac_sha256(state["chain_secret"], body)
    return body


def approve_plan_unlocked(control_dir: Path, state: dict, plan: dict, source_artifacts: dict | None = None) -> dict:
    validation = validate_plan(plan, state["repositories"], state.get("executable_plan_contract") == 1)
    snapshot = approval_snapshot(state, plan, validation, source_artifacts)
    state["approval"] = snapshot
    state["approved_plan"] = validation["plan"]
    state["dependency_order"] = validation["dependency_order"]
    for repo, stage in validation["stages"].items():
        current = state["stages"][repo]
        current["plan"] = stage
        current["status"] = "pending"
    state["updated_at"] = now()
    return write_state(control_dir, state)


def approve_plan(control_dir: Path, chain_id: str, plan: dict) -> dict:
    with chain_lock(control_dir, chain_id):
        return approve_plan_unlocked(control_dir, read_state(control_dir, chain_id), plan)


def verify_approval(state: dict) -> None:
    approval = state.get("approval")
    plan = state.get("approved_plan")
    if not isinstance(approval, dict) or not isinstance(plan, dict):
        raise FeatureChainError("joint approval is required before implementation")
    spec = Path(str(state.get("spec", "")))
    if not spec.is_file() or file_digest(spec) != state.get("spec_sha256"):
        raise FeatureChainError("approved feature spec bytes changed")
    verify_source_artifacts(approval.get("source_artifacts"))
    validation = validate_plan(plan, state["repositories"], state.get("executable_plan_contract") == 1)
    expected = approval_snapshot(state, plan, validation, approval.get("source_artifacts") if isinstance(approval, dict) else None)
    for key in ("approval_digest", "approval_mac"):
        if not hmac.compare_digest(str(approval.get(key, "")), expected[key]):
            raise FeatureChainError("joint approval no longer matches chain state")
    if approval != expected:
        raise FeatureChainError("joint approval snapshot drifted")


def verify_source_artifacts(source_artifacts: object) -> None:
    if not source_artifacts:
        return
    if not isinstance(source_artifacts, dict):
        raise FeatureChainError("approval source artifacts are malformed")
    root = Path(str(source_artifacts.get("root", "")))
    files = source_artifacts.get("files")
    if not root.is_absolute() or not root.is_dir() or not isinstance(files, dict):
        raise FeatureChainError("approval source artifact root is unavailable")
    for name, expected in files.items():
        if not isinstance(name, str) or "/" in name or "\\" in name:
            raise FeatureChainError("approval source artifact path is invalid")
        path = root / name
        if not path.is_file() or file_digest(path) != expected:
            raise FeatureChainError(f"approved planning artifact changed: {name}")


def collect_prior_planning_evidence(host: Any, row: dict, spec_sha256: str) -> dict:
    artifacts = artifact_dir(host, row)
    files = {}
    for name in (JOINT_PLAN_ARTIFACT, *PLANNING_SUPPORT_ARTIFACTS):
        path = artifacts / name
        if path.is_file() and not path.is_symlink():
            files[name] = planning_evidence_file(path)
    for round_dir in sorted(artifacts.glob("plan-round-*")):
        if not round_dir.is_dir() or round_dir.is_symlink():
            continue
        for name in PLANNING_ROUND_EVIDENCE:
            path = round_dir / name
            if path.is_file() and not path.is_symlink():
                files[str(path.relative_to(artifacts))] = planning_evidence_file(path)
    attempt = {
        "schema_version": 1,
        "run_id": row["id"],
        "status": row.get("status"),
        "workflow_name": row.get("workflow_name"),
        "spec_sha256": spec_sha256,
        "files": files,
        "captured_at": now(),
    }
    attempt["evidence_sha256"] = digest({"run_id": attempt["run_id"], "spec_sha256": spec_sha256, "files": files})
    return attempt


def planning_evidence_file(path: Path) -> dict:
    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FeatureChainError(f"planning evidence artifact is not UTF-8 text: {path.name}") from exc
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "content_text": text,
    }


def prior_planning_evidence_payload(state: dict) -> dict:
    return {
        "schema_version": 1,
        "kind": "repository-list-prior-planning-evidence",
        "logical_chain_id": state["logical_chain_id"],
        "planning_generation": state.get("planning_generation", 0),
        "attempts": state.get("planning_attempts", []),
    }


def write_prior_planning_evidence(artifacts_dir: Path, state: dict) -> dict | None:
    if not state.get("planning_attempts"):
        return None
    path = write_json_atomic(artifacts_dir / PRIOR_PLANNING_EVIDENCE_ARTIFACT, prior_planning_evidence_payload(state))
    return {
        "artifact": PRIOR_PLANNING_EVIDENCE_ARTIFACT,
        "path": str(path),
        "sha256": file_digest(path),
        "attempts": [prior_planning_attempt_summary(item) for item in state.get("planning_attempts", [])],
    }


def prior_planning_attempt_summary(item: dict) -> dict:
    summary = {"run_id": item.get("run_id"), "spec_sha256": item.get("spec_sha256")}
    if item.get("evidence_sha256"):
        summary["evidence_sha256"] = item["evidence_sha256"]
    return summary


def next_stage(state: dict) -> str | None:
    verify_approval(state)
    for repo in state["dependency_order"]:
        stage = state["stages"][repo]
        if stage.get("status") == "skipped":
            raise FeatureChainError(f"{repo} stage verification was skipped")
        if stage.get("status") == "verified":
            continue
        deps = stage.get("plan", {}).get("depends_on", [])
        unverified = [dep for dep in deps if state["stages"][dep].get("status") != "verified"]
        if unverified:
            continue
        return repo
    return None


def stage_env(state: dict, repo: str, phase: str = "implement") -> dict[str, str]:
    if repo not in state["repositories"]:
        raise FeatureChainError(f"stage repo is outside selected scope: {repo}")
    env = {
        "ARCHON_FEATURE_SCOPE": "repositories",
        "ARCHON_FEATURE_CHAIN_ID": state["logical_chain_id"],
        "ARCHON_FEATURE_PROVIDER": state["provider"],
        "ARCHON_FEATURE_PHASE": phase,
        "ARCHON_FEATURE_REPO": repo,
        "ARCHON_FEATURE_REPOSITORIES": ",".join(state["repositories"]),
        "ARCHON_FEATURE_WORKTREE": state["worktrees"][repo]["worktree"],
    }
    for dependency in state["stages"][repo].get("plan", {}).get("depends_on", []):
        candidate = state["candidate_handoffs"].get(dependency)
        if not isinstance(candidate, dict):
            raise FeatureChainError(f"missing verified local input: {dependency}")
        prefix = "ARCHON_REPO_" + dependency.upper().replace("-", "_")
        env[prefix + "_WORKTREE"] = state["worktrees"][dependency]["worktree"]
        env[prefix + "_COMMIT"] = candidate["candidate_head"]
    return env


def planning_env(state: dict, artifacts_dir: Path) -> dict[str, str]:
    return {
        "ARCHON_FEATURE_SCOPE": "repositories",
        "ARCHON_FEATURE_CHAIN_ID": state["logical_chain_id"],
        "ARCHON_FEATURE_PROVIDER": state["provider"],
        "ARCHON_FEATURE_PHASE": "planning",
        "ARCHON_FEATURE_REPO": "joint",
        "ARCHON_FEATURE_REPOSITORIES": ",".join(state["repositories"]),
        "ARCHON_FEATURE_PLANNING_ARTIFACTS": str(artifacts_dir),
        "ARCHON_FEATURE_PLANNING_GENERATION": str(state.get("planning_generation", 0)),
    }


def restart_planning(host: Any, args: Any, row: dict, control: dict) -> dict:
    feature = control.get("feature_chain", {})
    if feature.get("scope") != "repositories" or feature.get("phase") != "planning":
        raise FeatureChainError("feature-replan requires a repository-list planning run")
    if row.get("status") not in {"failed", "completed"}:
        raise FeatureChainError("feature-replan requires a terminal planning run")
    chain_id = feature["logical_chain_id"]
    with chain_lock(Path(args.control_dir), chain_id):
        revalidate_control_token(host, args, row)
        state = read_state(Path(args.control_dir), chain_id)
        current = state.get("current_run")
        reservation = state.get("dispatch_reservation")
        retry = (not current and isinstance(reservation, dict)
                 and reservation.get("phase") == "planning"
                 and reservation.get("source_run_id") == row["id"]
                 and (reservation.get("status") == "failed" or not process_claim_alive(reservation)))
        if not retry and (not isinstance(current, dict) or current.get("run_id") != row["id"]):
            raise FeatureChainError("feature-replan action is stale")
        if state.get("approval") or state.get("candidate_handoffs"):
            raise FeatureChainError("feature-replan cannot replace approved or implemented work")
        require_no_incomplete_amendment(state)
        budget_require_remaining(args, chain_id)
        if not retry:
            state["executable_plan_contract"] = 1
            state.setdefault("planning_attempts", []).append(
                collect_prior_planning_evidence(host, row, state["spec_sha256"])
            )
            state["planning_generation"] = state.get("planning_generation", 0) + 1
            state["spec_sha256"] = file_digest(Path(state["spec"]))
        state["current_run"] = None
        state["dispatch_reservation"] = {
            "phase": "planning", "repo": None, "source_run_id": row["id"],
            "owner_pid": os.getpid(), "owner_fingerprint": process_fingerprint(), "reserved_at": now(),
        }
        state = write_state(Path(args.control_dir), state)
    args.provider, args.spec = state["provider"], state["spec"]
    args.wall_minutes = state["budget"]["wall_minutes"]
    args.max_total_tokens = state["budget"]["max_total_tokens"]
    result = dispatch_planning(host, args, state)
    print(
        f"ARCHON_FEATURE_REPLAN=STARTED chain={chain_id} predecessor={row['id']} run={result['row']['id']}",
        flush=True,
    )
    return result


def planning_request_payload(state: dict, prior_evidence: dict | None = None) -> dict:
    return {
        "schema_version": 1,
        "kind": "repository-list-feature-planning-request",
        "logical_chain_id": state["logical_chain_id"],
        "provider": state["provider"],
        "scope": "repositories",
        "repositories": state["repositories"],
        "presentation_order": state["presentation_order"],
        "spec": state["spec"],
        "spec_sha256": state["spec_sha256"],
        "baselines": state["baselines"],
        "worktrees": state["worktrees"],
        "required_plan_artifact": JOINT_PLAN_ARTIFACT,
        "required_plan_schema": {
            "repositories": "exact selected repository list",
            "contracts": "list of producer, consumer, artifact, and description objects; producers are owned dependencies of consumers",
            "stages": "object keyed by selected repository with depends_on, files_allowlist, test_patterns, verification",
            "integration": "object with non-empty scenarios",
        },
        "prior_planning_evidence": prior_evidence,
        "write_policy": "planning workers may write only planning artifacts; repository worktrees are read-only until approval",
        "created_at": now(),
    }


def write_planning_request(artifacts_dir: Path, state: dict) -> Path:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    prior_evidence = write_prior_planning_evidence(artifacts_dir, state)
    path = artifacts_dir / PLANNING_REQUEST_ARTIFACT
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(planning_request_payload(state, prior_evidence), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def bind_phase_run(
    control_dir: Path,
    chain_id: str,
    *,
    phase: str,
    row: dict,
    repo: str | None = None,
    artifacts_dir: Path | None = None,
) -> dict:
    if phase not in {"planning", "implement", "verify", "integration"}:
        raise FeatureChainError(f"unsupported feature chain phase: {phase}")
    if not isinstance(row.get("id"), str) or not RUN_ID_RE.fullmatch(row["id"]):
        raise FeatureChainError("child run has invalid id")
    with chain_lock(control_dir, chain_id):
        state = read_state(control_dir, chain_id)
        current_run = state.get("current_run")
        if isinstance(current_run, dict) and current_run.get("run_id") == row["id"] and current_run.get("phase") == phase:
            return state
        if phase == "planning":
            if state.get("approval"):
                raise FeatureChainError("cannot bind planning after approval")
            if artifacts_dir is None:
                raise FeatureChainError("planning phase requires an artifacts directory")
            record = {
                "phase": phase,
                "run_id": row["id"],
                "workflow_name": row.get("workflow_name"),
                "write_roots": [str(artifacts_dir)],
                "artifacts_dir": str(artifacts_dir),
                "artifact_basename": artifacts_dir.name,
                "read_repositories": state["repositories"],
                "bound_at": now(),
            }
            state["current_run"] = record
            state["dispatch_reservation"] = None
            state.setdefault("phase_runs", []).append(record)
            state["updated_at"] = now()
            state = write_state(control_dir, state)
            write_planning_request(artifacts_dir, state)
            return state
        if phase == "integration":
            verify_approval(state)
            unverified = [name for name in state["repositories"] if state["stages"][name].get("status") != "verified"]
            if unverified:
                raise FeatureChainError("cannot bind integration before verified stages: " + ",".join(unverified))
            if artifacts_dir is None:
                raise FeatureChainError("integration phase requires an artifacts directory")
            record = {
                "phase": phase,
                "run_id": row["id"],
                "workflow_name": row.get("workflow_name"),
                "write_roots": [str(artifacts_dir)],
                "artifacts_dir": str(artifacts_dir),
                "artifact_basename": artifacts_dir.name,
                "candidate_inputs": dict(state.get("candidate_handoffs", {})),
                "bound_at": now(),
            }
            state["current_run"] = record
            state["dispatch_reservation"] = None
            state.setdefault("phase_runs", []).append(record)
            state["updated_at"] = now()
            state = write_state(control_dir, state)
            write_candidate_revisions(artifacts_dir, state)
            return state
        if repo is None:
            raise FeatureChainError(f"{phase} phase requires a repository")
        verify_approval(state)
        expected = next_stage(state) if phase in {"implement", "verify"} else None
        if phase in {"implement", "verify"} and expected != repo:
            raise FeatureChainError(f"cannot bind {repo}; next approved stage is {expected}")
        record = {
            "phase": phase,
            "repo": repo,
            "run_id": row["id"],
            "workflow_name": row.get("workflow_name"),
            "write_roots": [state["worktrees"][repo]["worktree"]],
            "artifacts_dir": str(artifacts_dir) if artifacts_dir is not None else None,
            "candidate_inputs": dict(state.get("candidate_handoffs", {})),
            "bound_at": now(),
        }
        state["current_run"] = record
        state["dispatch_reservation"] = None
        state.setdefault("phase_runs", []).append(record)
        if phase in {"implement", "verify"}:
            state["child_runs"][repo] = record
            state["stages"][repo]["status"] = "running"
            state["stages"][repo].setdefault("attempts", []).append(record)
        state["updated_at"] = now()
        return write_state(control_dir, state)


def bind_child_run(control_dir: Path, chain_id: str, repo: str, row: dict, phase: str) -> dict:
    return bind_phase_run(control_dir, chain_id, phase=phase, repo=repo, row=row)


def bind_run_control_payload(control_dir: Path, state: dict, repo: str | None, row: dict, phase: str) -> dict:
    if phase in {"planning", "integration"}:
        current = state.get("current_run", {})
        handoff = row.get("user_message")
        write_roots = current.get("write_roots", [])
    else:
        if repo is None:
            raise FeatureChainError("repository phase control payload requires a repo")
        current = state.get("child_runs", {}).get(repo, {})
        handoff = row.get("user_message")
        write_roots = current.get("write_roots", [state["worktrees"][repo]["worktree"]])
    return {
        "logical_chain_id": state["logical_chain_id"],
        "provider": state["provider"],
        "lane": row.get("workflow_name"),
        "scope": "repositories",
        "repo": repo,
        "phase": phase,
        "handoff": handoff,
        "chain_state_path": str(state_path(control_dir, state["logical_chain_id"])),
        "run_id": row.get("id"),
        "write_roots": write_roots,
    }


@contextmanager
def host_env(host: Any, env: dict[str, str]) -> Iterator[None]:
    if hasattr(host, "temporary_env"):
        with host.temporary_env(env):
            yield
        return
    previous = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def artifact_dir(host: Any, row: dict) -> Path:
    if hasattr(host, "artifact_dir"):
        return Path(host.artifact_dir(row))
    return Path(str(row.get("output_root", "")))


def dispatch_lane(host: Any, args: Any, lane: str, message: Path, env: dict[str, str]) -> dict:
    with host_env(host, env):
        try:
            if env.get("ARCHON_FEATURE_SCOPE") == "repositories" and env.get("ARCHON_FEATURE_CHAIN_ID"):
                dispatch_state = read_state(Path(args.control_dir), env["ARCHON_FEATURE_CHAIN_ID"])
                require_no_incomplete_amendment(dispatch_state)
                require_no_incomplete_scope_amendment(dispatch_state)
                shepherd_checkpoint(args, env["ARCHON_FEATURE_CHAIN_ID"])
                budget_require_remaining(args, env["ARCHON_FEATURE_CHAIN_ID"])
            if hasattr(host, "dispatch_feature_phase"):
                return host.dispatch_feature_phase(args, lane, message, env)
            if not hasattr(host, "invoke_codex_lane"):
                raise FeatureChainError("host does not expose invoke_codex_lane")
            return host.invoke_codex_lane(args, lane, message)
        except Exception as exc:
            if env.get("ARCHON_FEATURE_SCOPE") == "repositories" and env.get("ARCHON_FEATURE_CHAIN_ID"):
                control_dir = Path(args.control_dir)
                chain_id = env["ARCHON_FEATURE_CHAIN_ID"]
                with chain_lock(control_dir, chain_id):
                    state = read_state(control_dir, chain_id)
                    reservation = state.get("dispatch_reservation")
                    if (
                        isinstance(reservation, dict)
                        and not isinstance(state.get("current_run"), dict)
                        and reservation.get("phase") == env.get("ARCHON_FEATURE_PHASE")
                        and (reservation.get("repo") or "joint") == (env.get("ARCHON_FEATURE_REPO") or "joint")
                        and reservation.get("owner_pid") == os.getpid()
                    ):
                        reservation["status"] = "failed"
                        reservation["failed_at"] = now()
                        reservation["failure"] = str(exc)
                        state["updated_at"] = now()
                        write_state(control_dir, state)
            raise


def before_dispatch_bind(host: Any, args: Any, row: dict) -> dict | None:
    if os.environ.get("ARCHON_FEATURE_SCOPE") != "repositories":
        return None
    chain_id = os.environ.get("ARCHON_FEATURE_CHAIN_ID")
    phase = os.environ.get("ARCHON_FEATURE_PHASE")
    repo = os.environ.get("ARCHON_FEATURE_REPO")
    if not chain_id or not phase:
        raise FeatureChainError("repository-list feature dispatch environment is incomplete")
    artifacts = artifact_dir(host, row)
    state = bind_phase_run(
        Path(args.control_dir),
        chain_id,
        phase=phase,
        repo=repo,
        row=row,
        artifacts_dir=artifacts,
    )
    write_phase_artifacts(artifacts, state, phase, repo if repo != "joint" else None, row)
    budget_bind_run(args, chain_id, row, stage=f"{phase}:{repo or 'all'}")
    return state


def params_payload(state: dict, phase: str, repo: str | None, row: dict) -> dict:
    selected_repo = repo if repo in state["repositories"] else ("api" if "api" in state["repositories"] else state["repositories"][0])
    worktree = state["worktrees"][selected_repo]
    slug = f"feature-{state['logical_chain_id'][:8]}-{phase}-{selected_repo}"
    payload = {
        "schema_version": 2,
        "slug": slug,
        "feature_scope": "repositories",
        "feature_phase": phase,
        "repo": selected_repo,
        "repository": selected_repo,
        "repositories": state["repositories"],
        "repository_scope": state["repositories"],
        "spec": state["spec"],
        "spec_sha256": state["spec_sha256"],
        "worktree": worktree["worktree"],
        "branch": worktree["branch"],
        "baseline": worktree["baseline"],
        "worktrees_by_repo": {name: data["worktree"] for name, data in state["worktrees"].items()},
        "logical_chain_id": state["logical_chain_id"],
        "run_id": row["id"],
    }
    if "executable_plan_contract" in state:
        payload["executable_plan_contract"] = state["executable_plan_contract"]
    if "api" in state["repositories"]:
        payload["api_port"] = allocate_port(4123, slug)
    if "web-app" in state["repositories"]:
        payload["web_port"] = allocate_port(3127, slug)
    if phase == "implement" and repo == "web-app":
        stage = state["stages"][repo]["plan"]
        if "api" not in stage["depends_on"]:
            fixture = Path(str(stage.get("api_fixture_worktree", "")))
            source_root = Path(str(state["approval"].get("source_artifacts", {}).get("root", "")))
            if not fixture.is_absolute() or not source_root.is_absolute() or not fixture.resolve().is_relative_to(source_root.resolve()):
                raise FeatureChainError("web stage without API dependency requires an approved planning-artifact API fixture")
            if repo_head(fixture, "API fixture") != stage.get("api_fixture_head_sha"):
                raise FeatureChainError("approved API fixture revision drifted")
            payload["api_fixture_worktree"] = str(fixture.resolve())
            payload["api_fixture_head_sha"] = stage["api_fixture_head_sha"]
    return payload


def allocate_port(base: int, slug: str) -> int:
    result = subprocess.run(
        ["bash", str(Path(__file__).resolve().parent / "port-alloc.sh"), str(base), slug],
        capture_output=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise FeatureChainError((result.stderr or result.stdout).strip() or f"cannot allocate port from base {base}")
    return int(result.stdout.strip())


def write_phase_artifacts(artifacts: Path, state: dict, phase: str, repo: str | None, row: dict) -> None:
    if state.get("budget_shepherd_version") == 1 and state.get("budget_forecast"):
        write_json_atomic(artifacts / "budget-forecast.json", state["budget_forecast"])
    write_json_atomic(artifacts / "params.json", params_payload(state, phase, repo, row))
    write_json_atomic(artifacts / "worktrees.json", {f"{name}_worktree": data["worktree"] for name, data in state["worktrees"].items()})
    if phase == "planning":
        write_planning_request(artifacts, state)
        return
    if phase == "integration":
        write_candidate_revisions(artifacts, state)
        write_json_atomic(artifacts / JOINT_PLAN_ARTIFACT, state["approved_plan"])
        return
    if repo not in state["repositories"]:
        raise FeatureChainError(f"cannot provision artifacts for unknown repo: {repo}")
    verify_approval(state)
    stage = state["stages"][repo]["plan"]
    source = state["approval"].get("source_artifacts", {})
    if source:
        source_root = Path(source["root"])
        for name in PLANNING_SUPPORT_ARTIFACTS:
            if name in source["files"]:
                (artifacts / name).write_bytes((source_root / name).read_bytes())
        if repo == "web-app":
            for original, target in (("web-premises.json", "premises.json"), ("web-reader-audit.json", "reader-audit.json")):
                if original in source["files"]:
                    (artifacts / target).write_bytes((source_root / original).read_bytes())
    candidate_inputs = {}
    for dependency in stage["depends_on"]:
        candidate = state["candidate_handoffs"].get(dependency)
        if not isinstance(candidate, dict):
            raise FeatureChainError(f"missing verified local input: {dependency}")
        source = Path(state["worktrees"][dependency]["worktree"])
        if repo_head(source, dependency) != candidate["candidate_head"]:
            raise FeatureChainError(f"verified local input revision drifted: {dependency}")
        assert_clean_worktree(source, dependency)
        verify_interface_artifacts(Path(candidate["artifacts"]), candidate["interface_artifacts"])
        candidate_inputs[dependency] = dict(candidate, source_worktree=str(source))
    write_json_atomic(artifacts / "candidate-inputs.json", candidate_inputs)
    write_json_atomic(artifacts / "candidate-revisions.json", {
        "schema": "archon.joint-candidates.v1",
        "plan_digest": state["approval"]["plan_digest"],
        "approved_plan_digest": state["approval"]["plan_digest"],
        "candidate_heads": {name: value["candidate_head"] for name, value in candidate_inputs.items()},
        "repositories": {name: {"source_worktree": value["source_worktree"], "commit": value["candidate_head"]}
                         for name, value in candidate_inputs.items()},
    })
    write_json_atomic(artifacts / JOINT_PLAN_ARTIFACT, state["approved_plan"])
    write_json_atomic(artifacts / "files-allowlist.json", stage["files_allowlist"])
    write_json_atomic(artifacts / "verify.json", {"test_patterns": stage["test_patterns"], "verification": stage["verification"]})
    plan_md = approval_plan_markdown(state)
    if plan_md:
        (artifacts / "plan.md").write_text(plan_md, encoding="utf-8")


def approval_plan_markdown(state: dict) -> str:
    plan = state.get("approved_plan")
    if not isinstance(plan, dict):
        return ""
    source = state.get("approval", {}).get("source_artifacts", {})
    if "plan.md" in source.get("files", {}):
        verify_source_artifacts(source)
        return (Path(source["root"]) / "plan.md").read_text(encoding="utf-8")
    return json.dumps(plan, indent=2, sort_keys=True) + "\n"


def maybe_supervise(host: Any, args: Any, row: dict) -> dict | None:
    if getattr(args, "no_watch", False):
        return None
    if not hasattr(host, "supervise_exact_run"):
        raise FeatureChainError("host does not expose supervise_exact_run")
    timeout = getattr(args, "watch_timeout_seconds", 86400)
    interval = getattr(args, "watch_interval_seconds", 2.0)
    result = host.supervise_exact_run(args.db, row["id"], timeout, interval, shepherd_args=args)
    if isinstance(result, dict):
        result.setdefault("artifacts", str(artifact_dir(host, row)))
    return result


def planning_lane(args: Any, state: dict) -> str:
    return str(getattr(args, "planning_lane", None) or DEFAULT_PLANNING_LANE[state["provider"]])


def repository_lane(args: Any, state: dict, repo: str) -> str:
    configured = getattr(args, "repository_lanes", None)
    if isinstance(configured, dict) and repo in configured:
        return str(configured[repo])
    return DEFAULT_REPOSITORY_LANES[state["provider"]][repo]


def integration_lane(args: Any, state: dict) -> str:
    return str(getattr(args, "integration_lane", None) or DEFAULT_INTEGRATION_LANE[state["provider"]])


def dispatch_planning(host: Any, args: Any, state: dict) -> dict:
    artifacts_hint = Path(str(getattr(args, "planning_artifacts", _root(host) / ".archon" / "runs" / state["logical_chain_id"] / "planning")))
    env = planning_env(state, artifacts_hint)
    env["ARCHON_FEATURE_LANE"] = planning_lane(args, state)
    with host_env(host, env):
        row = dispatch_lane(host, args, planning_lane(args, state), Path(state["spec"]), env)
        state = verify_dispatch_bound(Path(args.control_dir), state["logical_chain_id"], row, "planning", None)
        emit_dispatch_status(state, row, "planning", None)
        result = maybe_supervise(host, args, row)
    return {"state": state, "row": row, "result": result}


def dispatch_repository_stage(host: Any, args: Any, state: dict, repo: str) -> dict:
    env = stage_env(state, repo, "implement")
    env["ARCHON_FEATURE_LANE"] = repository_lane(args, state, repo)
    message = Path(state["spec"])
    with host_env(host, env):
        row = dispatch_lane(host, args, repository_lane(args, state, repo), message, env)
        state = verify_dispatch_bound(Path(args.control_dir), state["logical_chain_id"], row, "implement", repo)
        emit_dispatch_status(state, row, "implement", repo)
        result = maybe_supervise(host, args, row)
    return {"state": state, "row": row, "result": result}


def dispatch_integration(host: Any, args: Any, state: dict) -> dict:
    env = {
        "ARCHON_FEATURE_SCOPE": "repositories",
        "ARCHON_FEATURE_CHAIN_ID": state["logical_chain_id"],
        "ARCHON_FEATURE_PROVIDER": state["provider"],
        "ARCHON_FEATURE_PHASE": "integration",
        "ARCHON_FEATURE_REPO": "joint",
        "ARCHON_FEATURE_REPOSITORIES": ",".join(state["repositories"]),
        "ARCHON_FEATURE_LANE": integration_lane(args, state),
    }
    with host_env(host, env):
        row = dispatch_lane(host, args, integration_lane(args, state), Path(state["spec"]), env)
        state = verify_dispatch_bound(Path(args.control_dir), state["logical_chain_id"], row, "integration", None)
        emit_dispatch_status(state, row, "integration", None)
        result = maybe_supervise(host, args, row)
    return {"state": state, "row": row, "result": result}


def emit_dispatch_status(state: dict, row: dict, phase: str, repo: str | None) -> None:
    if row.get("_control_line"):
        print(row["_control_line"], flush=True)
    repo_part = f" repo={repo}" if repo else ""
    print(
        "ARCHON_FEATURE_REPOSITORY_CHAIN=DISPATCHED "
        f"chain={state['logical_chain_id']} phase={phase}{repo_part} "
        f"lane={row.get('workflow_name')} run={str(row.get('id', ''))[:8]} "
        f"repositories={','.join(state['repositories'])}",
        flush=True,
    )


def verify_dispatch_bound(control_dir: Path, chain_id: str, row: dict, phase: str, repo: str | None) -> dict:
    state = read_state(control_dir, chain_id)
    current = state.get("current_run")
    if not isinstance(current, dict) or current.get("run_id") != row.get("id") or current.get("phase") != phase:
        raise FeatureChainError("feature dispatch returned before private pre-arm binding")
    if repo is not None and current.get("repo") != repo:
        raise FeatureChainError("feature dispatch pre-arm binding has wrong repository")
    return state


def read_json_artifact(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FeatureChainError(f"{label} is unavailable or malformed: {exc}") from exc
    if not isinstance(value, dict):
        raise FeatureChainError(f"{label} must be a JSON object")
    return value


def budget_script() -> Path:
    return Path(__file__).resolve().parent / "feature-budget.py"


def run_budget(args: Any, *argv: str) -> str:
    cmd = [
        sys.executable,
        str(budget_script()),
        "--control-dir",
        str(args.control_dir),
        "--codex-home",
        str(getattr(args, "codex_home", Path.home() / ".archon/codex-home")),
        *argv,
    ]
    result = subprocess.run(cmd, capture_output=True, encoding="utf-8")
    if result.returncode != 0:
        raise FeatureChainError((result.stderr or result.stdout).strip() or "feature budget command failed")
    return result.stdout


def budget_init(args: Any, state: dict) -> None:
    run_budget(
        args,
        "init",
        "--chain-id",
        state["logical_chain_id"],
        "--wall-minutes",
        str(state["budget"]["wall_minutes"]),
        "--max-total-tokens",
        str(state["budget"]["max_total_tokens"]),
    )


def budget_bind_run(args: Any, chain_id: str, row: dict, stage: str) -> None:
    run_budget(args, "bind-run", "--chain-id", chain_id, "--run-id", row["id"], "--stage", stage)


def budget_active_stop(args: Any, chain_id: str, row: dict) -> None:
    run_budget(args, "active-stop", "--chain-id", chain_id, "--run-id", row["id"])


def budget_usage(args: Any, chain_id: str) -> dict:
    # Only codex runs register sessions (via the workspace wrapper); a claude
    # run's tool activity has no session file to demand, so requiring one
    # would stop every claude chain at its first dispatch after work began.
    provider = read_state(Path(args.control_dir), chain_id).get("provider")
    argv = ["usage", "--chain-id", chain_id, "--db", str(args.db)]
    if provider == "codex":
        argv.append("--require-sessions")
    output = run_budget(args, *argv, "--json")
    try:
        summary = json.loads(output)
    except json.JSONDecodeError as exc:
        raise FeatureChainError(f"feature budget usage output is malformed: {exc}") from exc
    if not isinstance(summary, dict):
        raise FeatureChainError("feature budget usage output is not an object")
    return summary


def budget_require_remaining(args: Any, chain_id: str) -> dict:
    summary = budget_usage(args, chain_id)
    if summary.get("wall_exhausted"):
        raise FeatureChainError("feature shared wall budget is exhausted")
    if summary.get("tokens_exhausted"):
        raise FeatureChainError("feature shared token budget is exhausted")
    return summary


def budget_amend_allowance(args: Any, chain_id: str, total_tokens: int, total_active_minutes: int | None,
                           amendment_id: str, reason: str) -> None:
    argv = [
        "amend-limit",
        "--chain-id",
        chain_id,
        "--total-tokens",
        str(total_tokens),
    ]
    if total_active_minutes is not None:
        argv.extend(["--total-active-minutes", str(total_active_minutes)])
    argv.extend(["--amendment-id", amendment_id, "--reason", reason])
    run_budget(args, *argv)


def budget_account_provider_usage(args: Any, chain_id: str, run_id: str, event_id: str, transcript: Path) -> None:
    run_budget(
        args,
        "account-provider-usage",
        "--chain-id",
        chain_id,
        "--run-id",
        run_id,
        "--db",
        str(args.db),
        "--event-id",
        event_id,
        "--transcript",
        str(transcript),
    )


def update_run_control_allowance(host: Any, args: Any, row: dict, control: dict, total_tokens: int,
                                 wall_minutes: int | None = None) -> None:
    if not hasattr(host, "control_state_path") or not hasattr(host, "secure_write_json"):
        raise FeatureChainError("host cannot update private run-control allowance")
    updated = dict(control)
    updated["max_total_tokens"] = total_tokens
    if wall_minutes is not None:
        updated["wall_minutes"] = wall_minutes
    host.secure_write_json(host.control_state_path(row, Path(args.control_dir)), updated)
    artifact = Path(row["output_root"]) / "artifacts" / "runs" / row["id"] / "codex-lite-control.json"
    if artifact.is_file():
        try:
            public = json.loads(artifact.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FeatureChainError(f"public run-control artifact is malformed: {exc}") from exc
        public["max_total_tokens"] = total_tokens
        if wall_minutes is not None:
            public["wall_minutes"] = wall_minutes
        write_json_atomic(artifact, public)



def current_implementation_repo(state: dict, row: dict) -> str:
    current = state.get("current_run")
    if not isinstance(current, dict) or current.get("run_id") != row.get("id"):
        raise FeatureChainError("feature-scope-amend is stale for this chain")
    if current.get("phase") not in {"implement", "verify"}:
        raise FeatureChainError("feature-scope-amend requires a current implementation run")
    repo = current.get("repo")
    if not isinstance(repo, str) or repo not in state.get("repositories", []):
        raise FeatureChainError("feature-scope-amend current repository is outside selected scope")
    return repo


def require_scope_amendment_open(state: dict) -> None:
    if state.get("candidate_handoffs"):
        raise FeatureChainError("feature-scope-amend cannot modify a chain with verified candidate handoffs")
    if state.get("integration") or state.get("status") == "locally_verified":
        raise FeatureChainError("feature-scope-amend cannot modify a chain after integration")
    if state.get("publication"):
        raise FeatureChainError("feature-scope-amend cannot modify a published chain")


def validate_scope_add_file(state: dict, repo: str, add_file: str) -> str:
    if not isinstance(add_file, str) or not add_file.strip():
        raise FeatureChainError("feature-scope-amend requires --add-file")
    raw = Path(add_file)
    if raw.is_absolute() or any(part in {"", ".", ".."} for part in raw.parts):
        raise FeatureChainError("scope amendment path must be a safe repository-relative path")
    normalized = raw.as_posix()
    if normalized.startswith(".git/") or normalized == ".git":
        raise FeatureChainError("scope amendment path cannot target git metadata")
    worktree = Path(str(state["worktrees"][repo]["worktree"]))
    candidate = worktree / raw
    try:
        resolved = candidate.resolve(strict=True)
        worktree_resolved = worktree.resolve(strict=True)
    except OSError as exc:
        raise FeatureChainError(f"scope amendment file is unavailable: {add_file}") from exc
    try:
        resolved.relative_to(worktree_resolved)
    except ValueError as exc:
        raise FeatureChainError("scope amendment file must stay inside the selected repository worktree") from exc
    cursor = worktree
    for part in raw.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise FeatureChainError("scope amendment file must be an existing non-symlink file")
    if not resolved.is_file():
        raise FeatureChainError("scope amendment file must be an existing non-symlink file")
    if hasattr(os, "getuid") and resolved.stat().st_uid != os.getuid():
        raise FeatureChainError("scope amendment file is not owned by the current operator")
    tracked = _git(worktree, "ls-files", "--error-unmatch", "--", normalized)
    if tracked.returncode != 0:
        raise FeatureChainError("scope amendment file must already be tracked in the selected repository")
    return normalized


def plan_with_added_allowlist_file(plan: dict, repo: str, add_file: str) -> dict:
    amended = json.loads(json.dumps(plan))
    stages = amended.get("stages")
    if isinstance(stages, dict):
        stage = stages.get(repo)
        if not isinstance(stage, dict):
            raise FeatureChainError("approved plan is missing the current repository stage")
        files = stage.get("files_allowlist")
        if not isinstance(files, list):
            raise FeatureChainError("approved plan stage allowlist is malformed")
        if add_file not in files:
            stage["files_allowlist"] = [*files, add_file]
        return amended
    if isinstance(stages, list):
        for stage in stages:
            if isinstance(stage, dict) and stage.get("repo") == repo:
                files = stage.get("files_allowlist")
                if not isinstance(files, list):
                    raise FeatureChainError("approved plan stage allowlist is malformed")
                if add_file not in files:
                    stage["files_allowlist"] = [*files, add_file]
                return amended
        raise FeatureChainError("approved plan is missing the current repository stage")
    raise FeatureChainError("approved plan stages are malformed")


def scope_amendment_payload(chain_id: str, run_id: str, repo: str, add_file: str, reason: str) -> dict:
    return {
        "kind": "feature-scope-amend",
        "logical_chain_id": chain_id,
        "run_id": run_id,
        "repo": repo,
        "add_file": add_file,
        "reason": reason,
    }


def planning_source_root(state: dict) -> Path:
    source = state.get("approval", {}).get("source_artifacts", {})
    root = Path(str(source.get("root", "")))
    if not root.is_absolute() or not root.is_dir():
        raise FeatureChainError("approved planning artifact root is unavailable")
    return root


def ensure_owned_not_symlink(path: Path, label: str) -> None:
    try:
        stat = path.lstat()
    except OSError as exc:
        raise FeatureChainError(f"{label} is unavailable: {path}") from exc
    if path.is_symlink():
        raise FeatureChainError(f"{label} must not be a symlink: {path}")
    if hasattr(os, "getuid") and stat.st_uid != os.getuid():
        raise FeatureChainError(f"{label} is not owned by the current operator: {path}")


def preflight_scope_amendment_destination(root: Path, amendment_id: str) -> None:
    ensure_owned_not_symlink(root, "approved planning artifact root")
    base = root / "scope-amendments"
    if base.exists():
        ensure_owned_not_symlink(base, "scope amendment directory")
        if not base.is_dir():
            raise FeatureChainError("scope amendment path is not a directory")
    amend_root = base / amendment_id
    if amend_root.exists():
        ensure_owned_not_symlink(amend_root, "scope amendment packet directory")
        if not amend_root.is_dir():
            raise FeatureChainError("scope amendment packet path is not a directory")


def prepare_scope_amendment_dir(root: Path, amendment_id: str) -> Path:
    preflight_scope_amendment_destination(root, amendment_id)
    base = root / "scope-amendments"
    if not base.exists():
        base.mkdir(mode=0o700)
        ensure_owned_not_symlink(base, "scope amendment directory")
    amend_root = base / amendment_id
    if not amend_root.exists():
        amend_root.mkdir(mode=0o700)
        ensure_owned_not_symlink(amend_root, "scope amendment packet directory")
    return amend_root


def write_scope_amendment_artifacts(root: Path, state: dict, old_plan: dict, new_plan: dict,
                                    repo: str, add_file: str, reason: str, amendment_id: str) -> dict:
    amend_root = prepare_scope_amendment_dir(root, amendment_id)
    source = state.get("approval", {}).get("source_artifacts", {})
    files = source.get("files", {}) if isinstance(source, dict) else {}
    copied: dict[str, str] = {}
    for name in (JOINT_PLAN_ARTIFACT, *PLANNING_SUPPORT_ARTIFACTS):
        if name not in files:
            continue
        src = root / name
        if src.is_file():
            ensure_owned_not_symlink(src, "approved planning source artifact")
            dst = amend_root / ("original-" + name)
            dst.write_bytes(src.read_bytes())
            copied[dst.name] = file_digest(dst)
    write_json_atomic(amend_root / "original-approval.json", state["approval"])
    write_json_atomic(amend_root / "original-approved-plan.json", old_plan)
    write_json_atomic(amend_root / JOINT_PLAN_ARTIFACT, new_plan)
    recorded_at = state.get("scope_amendment", {}).get("started_at") or now()
    notice = {
        "schema_version": 1,
        "kind": "feature-scope-amendment-notice",
        "logical_chain_id": state["logical_chain_id"],
        "amendment_id": amendment_id,
        "repo": repo,
        "added_file": add_file,
        "reason": reason,
        "previous_plan_digest": state["approval"]["plan_digest"],
        "new_plan_digest": digest(new_plan),
        "recorded_at": recorded_at,
    }
    write_json_atomic(amend_root / "scope-amendment.json", notice)
    notice_md = (
        "# Guarded scope amendment approval packet\n\n"
        + f"- Chain: `{state['logical_chain_id']}`\n"
        + f"- Amendment: `{amendment_id}`\n"
        + f"- Repository: `{repo}`\n"
        + f"- Added allowlist file: `{add_file}`\n"
        + f"- Previous approval digest: `{state['approval']['approval_digest']}`\n"
        + f"- New plan digest: `{digest(new_plan)}`\n"
    )
    (amend_root / "approval-packet-notice.md").write_text(notice_md, encoding="utf-8")
    previous_plan_md = approval_plan_markdown(state)
    addendum = (
        previous_plan_md.rstrip()
        + "\n\n## Guarded scope amendment\n\n"
        + f"- Repository: `{repo}`\n"
        + f"- Added allowlist file: `{add_file}`\n"
        + f"- Reason: {reason}\n"
        + f"- Amendment id: `{amendment_id}`\n"
    )
    (amend_root / "plan.md").write_text(addendum + "\n", encoding="utf-8")
    files_out = {
        JOINT_PLAN_ARTIFACT: file_digest(amend_root / JOINT_PLAN_ARTIFACT),
        "plan.md": file_digest(amend_root / "plan.md"),
        "scope-amendment.json": file_digest(amend_root / "scope-amendment.json"),
        "approval-packet-notice.md": file_digest(amend_root / "approval-packet-notice.md"),
        **copied,
    }
    return {"root": str(amend_root), "files": files_out}


def final_scope_amendment_state(state: dict, repo: str, new_plan: dict, validation: dict,
                                new_approval: dict, source_artifacts: dict) -> dict:
    old_approval = state["approval"]
    amendment = dict(state["scope_amendment"])
    amendment.update({
        "status": "applied",
        "applied_at": now(),
        "approval_digest": new_approval["approval_digest"],
        "previous_approval_digest": old_approval.get("approval_digest"),
        "source_artifacts": source_artifacts,
    })
    final = dict(state)
    final.setdefault("approval_history", []).append(old_approval)
    final.setdefault("scope_amendments", []).append(amendment)
    final["approval"] = new_approval
    final["approved_plan"] = validation["plan"]
    final["dependency_order"] = validation["dependency_order"]
    final["stages"] = dict(state["stages"])
    final["stages"][repo] = dict(final["stages"][repo])
    final["stages"][repo]["plan"] = validation["stages"][repo]
    final["scope_amendment"] = None
    final["updated_at"] = now()
    return final


def build_scope_amendment(control_dir: Path, state: dict, repo: str,
                          add_file: str, reason: str, amendment_id: str) -> dict:
    old_approval = state.get("approval")
    old_plan = state.get("approved_plan")
    if not isinstance(old_approval, dict) or not isinstance(old_plan, dict):
        raise FeatureChainError("feature-scope-amend requires an approved joint plan")
    verify_approval(state)
    source_root = planning_source_root(state)
    new_plan = plan_with_added_allowlist_file(old_plan, repo, add_file)
    validation = validate_plan(new_plan, state["repositories"], state.get("executable_plan_contract") == 1)
    source_artifacts = write_scope_amendment_artifacts(source_root, state, old_plan, new_plan, repo, add_file, reason, amendment_id)
    new_approval = approval_snapshot(state, new_plan, validation, source_artifacts)
    return final_scope_amendment_state(state, repo, new_plan, validation, new_approval, source_artifacts)


def current_stage_artifacts(state: dict, row: dict) -> Path:
    current = state.get("current_run")
    if not isinstance(current, dict):
        raise FeatureChainError("feature chain has no current run binding")
    artifacts = current.get("artifacts_dir") or row.get("output_root")
    if not artifacts:
        raise FeatureChainError("current feature stage has no artifact directory")
    artifacts_path = Path(str(artifacts))
    if not artifacts_path.is_absolute() or not artifacts_path.is_dir():
        raise FeatureChainError("current feature stage artifacts are missing")
    return artifacts_path


def validate_current_stage_artifacts(state: dict, row: dict, repo: str | None = None, add_file: str | None = None) -> None:
    artifacts_path = current_stage_artifacts(state, row)
    revisions = read_json_artifact(artifacts_path / "candidate-revisions.json", "candidate-revisions.json")
    old_digest = state["approval"]["plan_digest"]
    allowed = {old_digest}
    if isinstance(state.get("scope_amendment"), dict) and repo and add_file:
        allowed.add(digest(plan_with_added_allowlist_file(state["approved_plan"], repo, add_file)))
    plan_digest = revisions.get("plan_digest")
    approved_digest = revisions.get("approved_plan_digest")
    if plan_digest != approved_digest or plan_digest not in allowed:
        raise FeatureChainError("current stage candidate revisions do not match the approved plan digest")


def refresh_current_stage_artifacts(state: dict, row: dict, repo: str) -> None:
    artifacts_path = current_stage_artifacts(state, row)
    write_json_atomic(artifacts_path / JOINT_PLAN_ARTIFACT, state["approved_plan"])
    write_json_atomic(artifacts_path / "files-allowlist.json", state["stages"][repo]["plan"]["files_allowlist"])
    revisions = read_json_artifact(artifacts_path / "candidate-revisions.json", "candidate-revisions.json")
    revisions["plan_digest"] = state["approval"]["plan_digest"]
    revisions["approved_plan_digest"] = state["approval"]["plan_digest"]
    write_json_atomic(artifacts_path / "candidate-revisions.json", revisions)
    plan_md = approval_plan_markdown(state)
    if plan_md:
        (artifacts_path / "plan.md").write_text(plan_md, encoding="utf-8")


def scope_amend_command(host: Any, args: Any, row: dict) -> dict:
    add_file_arg = str(getattr(args, "add_file", ""))
    reason = str(getattr(args, "reason", "")).strip()
    if not reason:
        raise FeatureChainError("scope amendment requires a reason")
    control = getattr(host, "read_control_state")(row, Path(args.control_dir))
    feature = control_feature({"control": control})
    if feature.get("scope") != "repositories":
        raise FeatureChainError("feature-scope-amend requires a repository-list feature run")
    chain_id = feature.get("logical_chain_id")
    if not isinstance(chain_id, str):
        raise FeatureChainError("run-control record is missing feature chain id")
    applied: dict | None = None
    with chain_lock(Path(args.control_dir), chain_id):
        row = host.run_row_by_id(Path(args.db), row["id"])
        if not isinstance(row, dict) or row.get("status") not in {"failed", "paused", "completed", "cancelled"}:
            raise FeatureChainError("feature-scope-amend requires a stopped run")
        control = host.require_control_token(row, Path(args.control_dir), getattr(args, "token", None))
        if control.get("feature_chain") != feature:
            raise FeatureChainError("feature control binding changed while acquiring the chain lock")
        require_no_live_control_processes(control)
        state = read_state(Path(args.control_dir), chain_id)
        require_no_incomplete_amendment(state)
        pending = state.get("pending_control")
        if pending is not None and (not isinstance(pending, dict) or process_claim_alive(pending)):
            raise FeatureChainError("repository-list feature control already in progress")
        if state.get("dispatch_reservation") is not None:
            raise FeatureChainError("repository-list feature dispatch already in progress")
        require_scope_amendment_open(state)
        repo = current_implementation_repo(state, row)
        add_file = validate_scope_add_file(state, repo, add_file_arg)
        payload = scope_amendment_payload(chain_id, row["id"], repo, add_file, reason)
        amendment_id = digest(payload)
        existing = state.get("scope_amendment")
        if isinstance(existing, dict) and existing.get("status") == "in_progress":
            if existing.get("amendment_id") != amendment_id:
                raise FeatureChainError("a different scope amendment is incomplete")
        for prior in state.get("scope_amendments", []):
            if prior.get("amendment_id") == amendment_id:
                if prior.get("add_file") != add_file or prior.get("repo") != repo:
                    raise FeatureChainError("scope amendment id was already used for a different change")
                refresh_current_stage_artifacts(state, row, repo)
                return {"chain": chain_id, "repo": repo, "add_file": add_file, "amendment_id": amendment_id, "already_applied": True}
        if add_file in state["stages"][repo].get("plan", {}).get("files_allowlist", []):
            raise FeatureChainError("scope amendment file is already approved for the current repository")
        verify_approval(state)
        validate_current_stage_artifacts(state, row, repo, add_file)
        preflight_scope_amendment_destination(planning_source_root(state), amendment_id)
        if not (isinstance(existing, dict) and existing.get("status") == "in_progress"):
            state["scope_amendment"] = {
                **payload,
                "amendment_id": amendment_id,
                "status": "in_progress",
                "started_at": now(),
            }
            state["updated_at"] = now()
            state = write_state(Path(args.control_dir), state)
        final_state = build_scope_amendment(Path(args.control_dir), state, repo, add_file, reason, amendment_id)
        refresh_current_stage_artifacts(final_state, row, repo)
        state = write_state(Path(args.control_dir), final_state)
        applied = {"chain": chain_id, "repo": repo, "add_file": add_file, "amendment_id": amendment_id, "already_applied": False}
    assert applied is not None
    return applied


def budget_update_command(host: Any, args: Any, row: dict) -> dict:
    total_tokens = int(getattr(args, "total_tokens", 0))
    total_active_minutes = getattr(args, "total_active_minutes", None)
    if total_active_minutes is not None:
        total_active_minutes = int(total_active_minutes)
        if total_active_minutes <= 0:
            raise FeatureChainError("total active minutes must be positive")
    if total_tokens <= 0:
        raise FeatureChainError("total token allowance must be positive")
    if not str(getattr(args, "reason", "")).strip():
        raise FeatureChainError("budget amendment requires a reason")
    control = getattr(host, "read_control_state")(row, Path(args.control_dir))
    feature = control_feature({"control": control})
    if feature.get("scope") != "repositories":
        raise FeatureChainError("feature-budget-update requires a repository-list feature run")
    if feature.get("provider") != "codex":
        raise FeatureChainError("feature-budget-update requires a Codex repository-list feature run")
    chain_id = feature.get("logical_chain_id")
    if not isinstance(chain_id, str):
        raise FeatureChainError("run-control record is missing feature chain id")
    amendment_payload = {
        "kind": "feature-budget-update",
        "logical_chain_id": chain_id,
        "run_id": row["id"],
        "total_tokens": total_tokens,
        "enable_shepherd": bool(getattr(args, "enable_shepherd", False)),
        "reason": getattr(args, "reason", ""),
    }
    if total_active_minutes is not None:
        amendment_payload["total_active_minutes"] = total_active_minutes
    amendment_id = digest(amendment_payload)
    with chain_lock(Path(args.control_dir), chain_id):
        row = host.run_row_by_id(Path(args.db), row["id"])
        if not isinstance(row, dict) or row.get("status") not in {"failed", "paused", "completed", "cancelled"}:
            raise FeatureChainError("feature-budget-update requires a stopped run")
        control = host.require_control_token(row, Path(args.control_dir), getattr(args, "token", None))
        if control.get("feature_chain") != feature:
            raise FeatureChainError("feature control binding changed while acquiring the chain lock")
        require_no_live_control_processes(control)
        state = read_state(Path(args.control_dir), chain_id)
        if state.get("provider") != "codex":
            raise FeatureChainError("feature-budget-update requires a Codex repository-list feature chain")
        pending = state.get("pending_control")
        if pending is not None and (not isinstance(pending, dict) or process_claim_alive(pending)):
            raise FeatureChainError("repository-list feature control already in progress")
        reservation = state.get("dispatch_reservation")
        if reservation is not None:
            raise FeatureChainError("repository-list feature dispatch already in progress")
        current = state.get("current_run")
        if not isinstance(current, dict) or current.get("run_id") != row.get("id"):
            raise FeatureChainError("feature-budget-update is stale for this chain")
        current_limit = state.get("budget", {}).get("max_total_tokens")
        current_wall = state.get("budget", {}).get("wall_minutes")
        if type(current_limit) is not int or current_limit <= 0:
            raise FeatureChainError("feature chain token allowance is malformed")
        if type(current_wall) is not int or current_wall <= 0:
            raise FeatureChainError("feature chain active time allowance is malformed")
        existing = state.get("budget_amendment")
        if isinstance(existing, dict) and existing.get("status") == "in_progress":
            if (existing.get("amendment_id") != amendment_id
                    or existing.get("total_tokens") != total_tokens
                    or existing.get("total_active_minutes") != total_active_minutes):
                raise FeatureChainError("a different budget amendment is incomplete")
        for applied in state.get("budget_amendments", []):
            if applied.get("amendment_id") != amendment_id:
                continue
            if applied.get("total_tokens") != total_tokens or applied.get("total_active_minutes") != total_active_minutes:
                raise FeatureChainError("amendment id was already used for a different allowance")
            control = getattr(host, "require_control_token")(row, Path(args.control_dir), getattr(args, "token", None))
            update_run_control_allowance(host, args, row, control, total_tokens, total_active_minutes)
            return {"chain": chain_id, "total_tokens": total_tokens, "total_active_minutes": total_active_minutes, "amendment_id": amendment_id}
        if total_tokens < current_limit:
            raise FeatureChainError("feature-budget-update may only increase the token ceiling")
        if total_active_minutes is not None and total_active_minutes < current_wall:
            raise FeatureChainError("feature-budget-update may only increase the active time ceiling")
        tokens_increase = total_tokens > current_limit
        wall_increase = total_active_minutes is not None and total_active_minutes > current_wall
        if not tokens_increase and not wall_increase:
            raise FeatureChainError("feature-budget-update must increase at least one ceiling")
        if not (isinstance(existing, dict) and existing.get("status") == "in_progress"):
            state["budget_amendment"] = {
                "amendment_id": amendment_id,
                "run_id": row["id"],
                "from_total_tokens": current_limit,
                "from_wall_minutes": current_wall,
                "total_tokens": total_tokens,
                "total_active_minutes": total_active_minutes,
                "reason": getattr(args, "reason", ""),
                "enable_shepherd": bool(getattr(args, "enable_shepherd", False)),
                "status": "in_progress",
                "started_at": now(),
            }
            state["updated_at"] = now()
            state = write_state(Path(args.control_dir), state)
        budget_amend_allowance(args, chain_id, total_tokens, total_active_minutes, amendment_id, getattr(args, "reason", ""))
        control = getattr(host, "require_control_token")(row, Path(args.control_dir), getattr(args, "token", None))
        update_run_control_allowance(host, args, row, control, total_tokens, total_active_minutes)
        amendment = dict(state["budget_amendment"])
        state.setdefault("budget", {})["max_total_tokens"] = total_tokens
        if total_active_minutes is not None:
            state.setdefault("budget", {})["wall_minutes"] = total_active_minutes
        if getattr(args, "enable_shepherd", False):
            state["budget_shepherd_version"] = 1
        amendment["status"] = "applied"
        amendment["applied_at"] = now()
        state.setdefault("budget_amendments", []).append(amendment)
        state["budget_amendment"] = None
        state["updated_at"] = now()
        state = write_state(Path(args.control_dir), state)
    return {"chain": chain_id, "total_tokens": total_tokens, "total_active_minutes": total_active_minutes, "amendment_id": amendment_id}


def account_provider_usage_command(host: Any, args: Any, row: dict) -> dict:
    event_id = str(getattr(args, "event_id", "")).strip()
    transcript = Path(getattr(args, "transcript", ""))
    if not event_id:
        raise FeatureChainError("feature-account-provider-usage requires an event id")
    if not transcript.is_file():
        raise FeatureChainError("feature-account-provider-usage requires a readable transcript")
    control = getattr(host, "read_control_state")(row, Path(args.control_dir))
    feature = control_feature({"control": control})
    if feature.get("scope") != "repositories":
        raise FeatureChainError("feature-account-provider-usage requires a repository-list feature run")
    if feature.get("provider") != "codex":
        raise FeatureChainError("feature-account-provider-usage requires a Codex repository-list feature run")
    chain_id = feature.get("logical_chain_id")
    if not isinstance(chain_id, str):
        raise FeatureChainError("run-control record is missing feature chain id")
    with chain_lock(Path(args.control_dir), chain_id):
        row = host.run_row_by_id(Path(args.db), row["id"])
        if not isinstance(row, dict) or row.get("status") not in {"failed", "paused", "completed", "cancelled"}:
            raise FeatureChainError("feature-account-provider-usage requires a stopped run")
        control = host.require_control_token(row, Path(args.control_dir), getattr(args, "token", None))
        if control.get("feature_chain") != feature:
            raise FeatureChainError("feature control binding changed while acquiring the chain lock")
        require_no_live_control_processes(control)
        state = read_state(Path(args.control_dir), chain_id)
        if state.get("provider") != "codex":
            raise FeatureChainError("feature-account-provider-usage requires a Codex repository-list feature chain")
        pending = state.get("pending_control")
        if pending is not None and (not isinstance(pending, dict) or process_claim_alive(pending)):
            raise FeatureChainError("repository-list feature control already in progress")
        reservation = state.get("dispatch_reservation")
        if reservation is not None:
            raise FeatureChainError("repository-list feature dispatch already in progress")
        current = state.get("current_run")
        if not isinstance(current, dict) or current.get("run_id") != row.get("id"):
            raise FeatureChainError("feature-account-provider-usage is stale for this chain")
        budget_account_provider_usage(args, chain_id, row["id"], event_id, transcript)
    return {"chain": chain_id, "run_id": row["id"], "event_id": event_id}


def require_no_live_control_processes(control: dict) -> None:
    for label in ("launcher", "watchdog"):
        pgid = control.get(f"{label}_pgid")
        if pgid is None:
            continue
        if type(pgid) is not int or pgid <= 1:
            raise FeatureChainError(f"invalid {label} process group")
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            continue
        except OSError as exc:
            raise FeatureChainError(f"cannot inspect {label} process group: {exc}") from exc
        raise FeatureChainError(f"cannot amend budget while {label} process group is live")


def estimate_for_scope(args: Any, repos: list[str], state: dict | None = None, usage: dict | None = None) -> dict:
    import feature_estimate

    if not repos or len(repos) != len(set(repos)) or any(repo not in REGISTERED_REPOSITORIES for repo in repos):
        raise FeatureChainError("feature estimate requires unique registered repositories")
    configured = getattr(args, "max_total_tokens", None)
    allowance = state["budget"]["max_total_tokens"] if state else (DEFAULT_MAX_TOTAL_TOKENS if configured is None else configured)
    if type(allowance) is not int or allowance <= 0:
        raise FeatureChainError("feature estimate allowance must be a positive integer")
    observations, diagnostics = feature_estimate.load_observations(Path(args.control_dir))
    current_id = (state or {}).get("logical_chain_id")
    current = next((item for item in observations if item["chain_id"] == current_id), {})
    historical = [item for item in observations if item["chain_id"] != current_id]
    report = feature_estimate.estimate(
        repos, allowance, historical,
        used_tokens=(usage or {}).get("total_tokens", 0),
        phase_usage={phase: row["tokens"] for phase, row in current.get("phases", {}).items()},
        completed_repositories=[repo for repo, stage in (state or {}).get("stages", {}).items()
                                if stage.get("status") == "verified"],
        planning_complete=bool((state or {}).get("approval")),
        integration_complete=(state or {}).get("status") == "locally_verified",
    )
    report["history_diagnostics"] = diagnostics
    report["accounting"] = "input (including cached input once) plus output; not monetary cost"
    report["model_caveat"] = "History is grouped by repository and phase, not semantic task difficulty; model/effort metadata may be absent. This forecast is not statistically calibrated."
    report["estimated_at"] = now()
    if state:
        report["chain_id"] = state["logical_chain_id"]
        report["spec_sha256"] = state["spec_sha256"]
    elif getattr(args, "spec", None):
        spec = Path(args.spec).resolve()
        if not spec.is_file():
            raise FeatureChainError("feature estimate requires an existing specification")
        report["spec_sha256"] = file_digest(spec)
        report["spec_bytes"] = spec.stat().st_size
    return report


def report_forecast_allowance(report: dict) -> None:
    print("FEATURE_BUDGET_ESTIMATE=" + str(report["disposition"]).upper()
          + f" recommended_total_tokens={report['recommended_total_tokens']}"
          + f" remaining_allowance={report['remaining_allowance']} confidence={report['confidence']}")
    if report["disposition"] != "sufficient":
        print("BUDGET_SHEPHERD=WARN forecast exceeds allowance; continuing under the unchanged hard cap")


def shepherd_unlocked(args: Any, state: dict, announce: bool = True) -> dict | None:
    require_no_incomplete_amendment(state)
    if state.get("budget_shepherd_version") != 1 or state.get("provider") != "codex":
        return None
    usage = budget_usage(args, state["logical_chain_id"])
    if usage.get("wall_exhausted"):
        raise FeatureChainError("feature shared wall budget is exhausted")
    if usage.get("tokens_exhausted"):
        raise FeatureChainError("feature shared token budget is exhausted")
    report = estimate_for_scope(args, state["repositories"], state, usage)
    state["budget_forecast"] = report
    write_state(Path(args.control_dir), state)
    artifacts = (state.get("current_run") or {}).get("artifacts_dir")
    if artifacts:
        write_json_atomic(Path(artifacts) / "budget-forecast.json", report)
    if announce:
        report_forecast_allowance(report)
    return report


def shepherd_checkpoint(args: Any, chain_id: str, announce: bool = True) -> dict | None:
    with chain_lock(Path(args.control_dir), chain_id):
        return shepherd_unlocked(args, read_state(Path(args.control_dir), chain_id), announce)


def shepherd_command(host: Any, args: Any) -> dict | None:
    return shepherd_checkpoint(args, args.chain, announce=False)


def estimate_command(host: Any, args: Any) -> dict:
    if getattr(args, "chain", None):
        if getattr(args, "scope", None) or getattr(args, "spec", None) or getattr(args, "max_total_tokens", None) is not None:
            raise FeatureChainError("--chain uses its existing scope and allowance; omit spec, --scope and --max-total-tokens")
        state = read_state(Path(args.control_dir), args.chain)
        if state["provider"] != "codex":
            raise FeatureChainError("feature estimates currently require a Codex chain")
        return estimate_for_scope(args, state["repositories"], state, budget_usage(args, args.chain))
    if not getattr(args, "scope", None) or not getattr(args, "spec", None):
        raise FeatureChainError("feature-estimate requires --scope and spec, or --chain")
    raw = args.scope.split(",")
    if args.scope == "fullstack":
        raw = ["api", "web-app"]
    repos = [REPOSITORY_ALIASES.get(item, item) for item in raw]
    if len(repos) != len(set(repos)) or any(repo not in REGISTERED_REPOSITORIES for repo in repos):
        raise FeatureChainError("feature estimate requires unique registered repository names")
    return estimate_for_scope(args, repos)


def token_digest(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def process_fingerprint(pid: int | None = None) -> str:
    pid = os.getpid() if pid is None else pid
    result = subprocess.run(
        ["ps", "-o", "lstart=", "-o", "command=", "-p", str(pid)],
        capture_output=True,
        encoding="utf-8",
    )
    identity = result.stdout.strip()
    return hashlib.sha256(identity.encode()).hexdigest() if identity else ""


def process_claim_alive(claim: dict) -> bool:
    pid = claim.get("owner_pid")
    fingerprint = claim.get("owner_fingerprint")
    if not isinstance(pid, int) or not isinstance(fingerprint, str) or not fingerprint:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return process_fingerprint(pid) == fingerprint


def revalidate_control_token(host: Any, args: Any, row: dict) -> None:
    if not hasattr(host, "require_control_token"):
        return
    host.require_control_token(row, Path(args.control_dir), getattr(args, "token", None))


def claim_control_unlocked(control_dir: Path, state: dict, args: Any, row: dict) -> dict:
    action = str(getattr(args, "action", ""))
    claim = {
        "action": action,
        "run_id": row["id"],
        "token_digest": token_digest(getattr(args, "token", None)),
        "owner_pid": os.getpid(),
        "owner_fingerprint": process_fingerprint(),
        "claimed_at": now(),
    }
    pending = state.get("pending_control")
    if pending is not None:
        if not isinstance(pending, dict) or process_claim_alive(pending):
            raise FeatureChainError("repository-list feature control already in progress")
        state["pending_control"] = None
    state["pending_control"] = claim
    state["updated_at"] = now()
    return write_state(control_dir, state)


def release_control_claim(control_dir: Path, chain_id: str, row: dict | None = None) -> dict:
    with chain_lock(control_dir, chain_id):
        state = read_state(control_dir, chain_id)
        pending = state.get("pending_control")
        if row is not None and isinstance(pending, dict) and pending.get("run_id") != row.get("id"):
            raise FeatureChainError("cannot release a control claim owned by another run")
        state["pending_control"] = None
        state["updated_at"] = now()
        return write_state(control_dir, state)


def after_control(host: Any, args: Any, row: dict) -> dict | None:
    feature = control_feature(row)
    chain_id = feature.get("logical_chain_id") or os.environ.get("ARCHON_FEATURE_CHAIN_ID")
    if not isinstance(chain_id, str):
        return None
    return release_control_claim(Path(args.control_dir), chain_id, row)


def control_failed(host: Any, args: Any, row: dict) -> dict | None:
    return after_control(host, args, row)


def control_feature(row: dict) -> dict:
    feature = row.get("feature_chain") if isinstance(row, dict) else None
    if isinstance(feature, dict):
        return feature
    control = row.get("control") if isinstance(row, dict) else None
    if isinstance(control, dict) and isinstance(control.get("feature_chain"), dict):
        return control["feature_chain"]
    return {}


def table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    return {str(row[1]) for row in rows}


def mark_run_failed_for_prearm(db: Path, row: dict, reason: str) -> None:
    run_id = row.get("id")
    if not isinstance(run_id, str):
        raise FeatureChainError("pre-arm failure requires a run id")
    timestamp = now().replace("T", " ").replace("Z", "")
    try:
        with sqlite3.connect(db, timeout=30) as con:
            con.execute("BEGIN IMMEDIATE")
            run_columns = table_columns(con, "remote_agent_workflow_runs")
            if "status" not in run_columns or "id" not in run_columns:
                raise FeatureChainError("workflow run table cannot record pre-arm failure")
            assignments = ["status = ?"]
            values: list[Any] = ["failed"]
            for column in ("completed_at", "last_activity_at"):
                if column in run_columns:
                    assignments.append(f"{column} = ?")
                    values.append(timestamp)
            values.append(run_id)
            con.execute(
                f"UPDATE remote_agent_workflow_runs SET {', '.join(assignments)} WHERE id = ? AND status = 'running'",
                values,
            )
            event_columns = table_columns(con, "remote_agent_workflow_events")
            if {"workflow_run_id", "created_at"}.issubset(event_columns):
                payload = json.dumps({"reason": reason, "source": "feature-chain-prearm"}, sort_keys=True)
                insert_columns = ["workflow_run_id", "created_at"]
                insert_values: list[Any] = [run_id, timestamp]
                optional = {
                    "id": secrets.token_hex(16),
                    "event_type": "workflow_failed",
                    "step_name": "feature-chain-prearm",
                    "step_index": -1,
                    "node_name": "feature-chain-prearm",
                    "data": payload,
                    "payload": payload,
                    "message": reason,
                }
                if "event_order" in event_columns:
                    optional["event_order"] = con.execute(
                        "SELECT COALESCE(MAX(event_order), 0) + 1 FROM remote_agent_workflow_events WHERE workflow_run_id = ?",
                        (run_id,),
                    ).fetchone()[0]
                for column, value in optional.items():
                    if column in event_columns:
                        insert_columns.append(column)
                        insert_values.append(value)
                placeholders = ",".join("?" for _ in insert_columns)
                con.execute(
                    f"INSERT INTO remote_agent_workflow_events ({','.join(insert_columns)}) VALUES ({placeholders})",
                    insert_values,
                )
    except sqlite3.Error as exc:
        raise FeatureChainError(f"cannot record pre-arm failure: {exc}") from exc


def prearm_failure(host: Any, args: Any, row: dict, reason: str = "pre-arm failure") -> dict | None:
    """Preserve a repository-list chain child when launcher arming fails.

    Launcher cleanup calls this after it has already terminated exact process
    groups.  The child run is marked failed rather than orphan-abandoned so the
    same run id, private control record, and chain current_run binding remain
    available for a normal resume.
    """
    chain_id = os.environ.get("ARCHON_FEATURE_CHAIN_ID") or control_feature(row).get("logical_chain_id")
    if not isinstance(chain_id, str):
        return None
    control_dir = Path(args.control_dir)
    with chain_lock(control_dir, chain_id):
        state = read_state(control_dir, chain_id)
        current = state.get("current_run")
        if not isinstance(current, dict) or current.get("run_id") != row.get("id"):
            raise FeatureChainError("pre-arm failure is stale for this feature-chain run")
        phase = str(current.get("phase") or "implement")
        repo = current.get("repo")
        if isinstance(repo, str) and repo in state.get("stages", {}):
            state["stages"][repo]["status"] = "failed"
        state["last_prearm_failure"] = {
            "run_id": row.get("id"),
            "phase": phase,
            "repo": repo,
            "reason": reason,
            "recorded_at": now(),
        }
        state["dispatch_reservation"] = None
        state["updated_at"] = now()
        state = write_state(control_dir, state)
    mark_run_failed_for_prearm(Path(args.db), row, reason)
    try:
        budget_active_stop(args, chain_id, row)
    except FeatureChainError:
        raise
    return state


def path_is_under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def assert_write_allowed(control_dir: Path, chain_id: str, path: Path) -> None:
    state = read_state(control_dir, chain_id)
    current = state.get("current_run")
    if not isinstance(current, dict):
        raise FeatureChainError("feature chain has no current run binding")
    roots = current.get("write_roots")
    if not isinstance(roots, list) or not roots:
        raise FeatureChainError("feature chain current run has no write roots")
    if not any(path_is_under(path, Path(str(root))) for root in roots):
        raise FeatureChainError(f"write is outside the current feature-chain authority: {path}")


def git_output(repo: Path, *argv: str) -> str:
    result = _git(repo, *argv)
    if result.returncode != 0:
        raise FeatureChainError(f"git {' '.join(argv)} failed at {repo}: {result.stderr.strip()}")
    return result.stdout.strip()


def assert_clean_worktree(worktree: Path, repo: str) -> None:
    status = git_output(worktree, "status", "--porcelain", "--untracked-files=all")
    if status:
        raise FeatureChainError(f"{repo} candidate worktree is dirty")


def diff_files_since(worktree: Path, baseline: str, head: str) -> list[str]:
    out = git_output(worktree, "diff", "--name-only", f"{baseline}..{head}", "--")
    return [line for line in out.splitlines() if line.strip()]


def require_verification_evidence(artifacts: Path, repo: str, evidence: object) -> list:
    if not isinstance(evidence, list) or not evidence:
        raise FeatureChainError(f"{repo} stage missing verification evidence")
    passed_tests = 0
    for index, item in enumerate(evidence):
        if (isinstance(item, dict) and item.get("name") == "smoke"
                and item.get("status") == "not_applicable" and repo == "goodword-mcp"):
            continue
        if not isinstance(item, dict) or item.get("status") != "passed":
            raise FeatureChainError(f"{repo} verification evidence[{index}] did not pass")
        log = item.get("log") or item.get("log_path")
        if not isinstance(log, str) or not log:
            raise FeatureChainError(f"{repo} verification evidence[{index}] has no log")
        log_path = Path(log) if Path(log).is_absolute() else artifacts / log
        if not log_path.resolve().is_relative_to(artifacts.resolve()) or not log_path.is_file() or log_path.stat().st_size <= 0:
            raise FeatureChainError(f"{repo} verification log is missing, outside run, or empty: {log}")
        count = item.get("tests_passed", 0)
        if type(count) is not int or count < 0:
            raise FeatureChainError(f"{repo} verification has invalid test count")
        passed_tests += count
    if passed_tests <= 0:
        raise FeatureChainError(f"{repo} verification executed zero tests")
    return evidence


def verify_interface_artifacts(artifacts: Path, entries: object) -> list:
    if not isinstance(entries, list):
        raise FeatureChainError("interface artifacts must be a list")
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise FeatureChainError("interface artifact has no path")
        path = (artifacts / entry["path"]).resolve()
        if not path.is_relative_to(artifacts.resolve()) or not path.is_file():
            raise FeatureChainError("interface artifact is missing or outside its run")
        if file_digest(path) != entry.get("sha256"):
            raise FeatureChainError("interface artifact digest changed")
    return entries


def candidate_from_artifacts(repo: str, row: dict, artifacts: Path, state: dict) -> dict:
    result_path = artifacts / "feature-result.json"
    if not result_path.is_file():
        raise FeatureChainError(f"{repo} stage missing feature-result.json")
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FeatureChainError(f"{repo} stage feature-result.json is malformed: {exc}") from exc
    if not isinstance(result, dict) or result.get("outcome") not in {"CHANGED", "NO_CHANGE"}:
        raise FeatureChainError(f"{repo} stage did not produce a verified candidate")
    head = result.get("head") or result.get("head_sha") or result.get("commit")
    if not isinstance(head, str) or not COMMIT_RE.fullmatch(head):
        raise FeatureChainError(f"{repo} stage candidate head is missing or invalid")
    worktree = Path(state["worktrees"][repo]["worktree"])
    actual_head = git_output(worktree, "rev-parse", "HEAD")
    if actual_head != head:
        raise FeatureChainError(f"{repo} candidate head does not match pinned worktree")
    baseline = state["worktrees"][repo]["baseline"]
    git_output(worktree, "merge-base", "--is-ancestor", baseline, head)
    allowed = set(state["stages"][repo]["plan"]["files_allowlist"])
    changed = set(diff_files_since(worktree, baseline, head))
    outside = sorted(changed - allowed)
    if outside:
        raise FeatureChainError(f"{repo} candidate changed files outside approved allowlist: {','.join(outside)}")
    assert_clean_worktree(worktree, repo)
    params = read_json_artifact(artifacts / "params.json", "params.json")
    if params.get("worktree") != str(worktree):
        raise FeatureChainError(f"{repo} params worktree does not match private state")
    verify = read_json_artifact(artifacts / "verify.json", "verify.json")
    if verify.get("test_patterns") != state["stages"][repo]["plan"]["test_patterns"]:
        raise FeatureChainError(f"{repo} test selection drifted from joint approval")
    evidence = require_verification_evidence(artifacts, repo, result.get("verification_evidence") or result.get("tests"))
    interfaces = verify_interface_artifacts(artifacts, result.get("interface_artifacts", []))
    declared = {item["artifact"] for item in state["approved_plan"]["contracts"] if item.get("producer") == repo}
    if not declared.issubset({item.get("source_path") for item in interfaces}):
        raise FeatureChainError(f"{repo} candidate is missing a declared interface artifact")
    return {
        "repo": repo,
        "run_id": row["id"],
        "workflow_name": row.get("workflow_name"),
        "candidate_head": head,
        "artifacts": str(artifacts),
        "result_sha256": file_digest(result_path),
        "verification_evidence": evidence,
        "interface_artifacts": interfaces,
        "recorded_at": now(),
    }


def verify_stage_result(state: dict, repo: str, row: dict, result: dict) -> dict:
    if result.get("state") != "terminal" or result.get("status") not in TERMINAL_OK:
        raise FeatureChainError(f"{repo} stage is not terminal verified")
    bound = state["child_runs"].get(repo)
    if not isinstance(bound, dict) or bound.get("run_id") != row.get("id"):
        raise FeatureChainError(f"{repo} stage result is not bound to recorded child run")
    artifact_source = bound.get("artifacts_dir") or result.get("artifacts") or row.get("artifacts") or row.get("output_root", "")
    artifacts = Path(str(artifact_source))
    if not artifacts.is_absolute() or not artifacts.exists():
        raise FeatureChainError(f"{repo} stage artifacts are missing")
    return candidate_from_artifacts(repo, row, artifacts, state)


def integration_receipt(state: dict, evidence: dict) -> dict:
    if not isinstance(evidence, dict):
        raise FeatureChainError("integration evidence must be an object")
    if evidence.get("status") != "passed":
        raise FeatureChainError("combined integration verification did not pass")
    tests = evidence.get("tests")
    if not isinstance(tests, list) or not tests:
        raise FeatureChainError("combined integration verification has no tests")
    counters = evidence.get("counters")
    if not isinstance(counters, dict) or not isinstance(counters.get("tests_passed"), int) or counters["tests_passed"] <= 0:
        raise FeatureChainError("combined integration verification has no positive test counter")
    candidate_heads = {repo: state["candidate_handoffs"][repo]["candidate_head"] for repo in state["repositories"]}
    if evidence.get("plan_digest") != state["approval"]["plan_digest"]:
        raise FeatureChainError("integration evidence plan digest does not match approved plan")
    if evidence.get("approved_plan_digest") != state["approval"]["plan_digest"]:
        raise FeatureChainError("integration evidence approved plan digest does not match approval")
    if evidence.get("candidate_heads") != candidate_heads:
        raise FeatureChainError("integration evidence candidate heads do not match verified stages")
    body = {
        "schema_version": 1,
        "kind": "repository-list-feature-receipt",
        "logical_chain_id": state["logical_chain_id"],
        "provider": state["provider"],
        "repositories": state["repositories"],
        "spec": state["spec"],
        "spec_sha256": state["spec_sha256"],
        "approval_digest": state["approval"]["approval_digest"],
        "plan_digest": state["approval"]["plan_digest"],
        "approved_plan_digest": state["approval"]["plan_digest"],
        "candidate_heads": candidate_heads,
        "integration_evidence": evidence,
        "publication": "held",
        "status": "locally_verified",
        "finalized_at": now(),
    }
    body["receipt_digest"] = digest(body)
    body["receipt_mac"] = hmac_sha256(state["chain_secret"], body)
    return body


def finalize_integration_unlocked(control_dir: Path, state: dict, evidence: dict, artifacts: Path | None = None) -> dict:
    verify_approval(state)
    missing = [repo for repo in state["repositories"] if state["stages"][repo].get("status") != "verified"]
    if missing:
        raise FeatureChainError("cannot finalize before verified stages: " + ",".join(missing))
    receipt = integration_receipt(state, evidence)
    state["integration"] = receipt
    if artifacts is not None:
        state["integration_artifacts"] = str(artifacts)
    state["status"] = "locally_verified"
    state["updated_at"] = now()
    return write_state(control_dir, state)


def finalize_integration(control_dir: Path, chain_id: str, evidence: dict, artifacts: Path | None = None) -> dict:
    with chain_lock(control_dir, chain_id):
        return finalize_integration_unlocked(control_dir, read_state(control_dir, chain_id), evidence, artifacts)


def advance(host: Any, args: Any, row: dict, result: dict) -> dict:
    control_dir = Path(args.control_dir)
    feature = (result.get("feature_chain") if isinstance(result, dict) else None) or {}
    chain_id = feature.get("logical_chain_id") or os.environ.get("ARCHON_FEATURE_CHAIN_ID")
    if not isinstance(chain_id, str):
        raise FeatureChainError("advance requires feature chain id")
    dispatch_repo: str | None = None
    dispatch_integration_after = False
    reclaimed_reservation = False
    candidate = None
    with chain_lock(control_dir, chain_id):
        state = read_state(control_dir, chain_id)
        current = state.get("current_run")
        phase = feature.get("phase") or os.environ.get("ARCHON_FEATURE_PHASE") or (current or {}).get("phase")
        if not isinstance(current, dict) or current.get("run_id") != row.get("id"):
            reservation = state.get("dispatch_reservation")
            if isinstance(reservation, dict) and reservation.get("source_run_id") == row.get("id"):
                if reservation.get("status") != "failed" and process_claim_alive(reservation):
                    return {"state": state, "dispatch_reserved": True, "next_repo": reservation.get("repo")}
                verify_approval(state)
                reserved_phase = reservation.get("phase")
                reserved_repo = reservation.get("repo")
                if reserved_phase == "implement":
                    expected = next_stage(state)
                    if reserved_repo != expected:
                        raise FeatureChainError("dispatch reservation no longer matches approved next stage")
                    dispatch_repo = str(reserved_repo)
                elif reserved_phase == "integration":
                    missing = [repo for repo in state["repositories"] if state["stages"][repo].get("status") != "verified"]
                    if missing:
                        raise FeatureChainError("dispatch reservation no longer matches verified stages: " + ",".join(missing))
                    dispatch_integration_after = True
                else:
                    raise FeatureChainError("dispatch reservation has invalid phase")
                reservation["status"] = "dispatching"
                reservation["owner_pid"] = os.getpid()
                reservation["owner_fingerprint"] = process_fingerprint()
                reservation["reclaimed_at"] = now()
                state["updated_at"] = now()
                state = write_state(control_dir, state)
                reclaimed_reservation = True
            else:
                raise FeatureChainError("advance action is stale for the current feature-chain run")
        if reclaimed_reservation:
            pass
        elif phase == "planning":
            if result.get("state") != "terminal" or result.get("status") != "completed":
                return {"state": state, "paused": True, "phase": "planning"}
            verify_approval(state)
            dispatch_repo = next_stage(state)
            if dispatch_repo is None:
                raise FeatureChainError("approved plan has no repository stage to dispatch")
            state["current_run"] = None
            state["dispatch_reservation"] = {
                "phase": "implement",
                "repo": dispatch_repo,
                "source_run_id": row["id"],
                "status": "dispatching",
                "owner_pid": os.getpid(),
                "owner_fingerprint": process_fingerprint(),
                "reserved_at": now(),
            }
            state["updated_at"] = now()
            state = write_state(control_dir, state)
        elif phase == "integration":
            if result.get("state") != "terminal" or result.get("status") != "completed":
                return {"state": state, "paused": True, "phase": "integration"}
            artifacts = Path(str(current.get("artifacts_dir") or result.get("artifacts") or row.get("output_root", "")))
            evidence = read_json_artifact(artifacts / INTEGRATION_EVIDENCE_ARTIFACT, INTEGRATION_EVIDENCE_ARTIFACT)
            state = finalize_integration_unlocked(control_dir, state, evidence, artifacts)
            receipt_path = artifacts / "feature-chain-receipt.json"
            write_json_atomic(receipt_path, state["integration"])
            launcher = getattr(host, "__file__", "archon-run.py")
            print(
                "ARCHON_FEATURE_REPOSITORY_CHAIN=LOCALLY_VERIFIED "
                f"chain={state['logical_chain_id']} receipt={receipt_path} "
                f"next=\"python3 {launcher} feature-publish --chain {state['logical_chain_id']}\""
            )
            return {"state": state, "receipt": state["integration"], "receipt_path": str(receipt_path), "next_repo": None}
        elif phase not in {"planning", "integration"}:
            repo = feature.get("repo") or os.environ.get("ARCHON_FEATURE_REPO") or current.get("repo")
            if not isinstance(repo, str):
                raise FeatureChainError("advance requires repository for implementation result")
            if repo not in state["repositories"]:
                raise FeatureChainError("advance repository is outside selected scope")
            if result.get("state") != "terminal" or result.get("status") != "completed":
                if result.get("state") == "terminal":
                    state["stages"][repo]["status"] = str(result.get("status") or "failed")
                    state["updated_at"] = now()
                    state = write_state(control_dir, state)
                return {"state": state, "paused": result.get("state") != "terminal", "phase": phase, "repo": repo}
            candidate = verify_stage_result(state, repo, row, result)
            state["candidate_handoffs"][repo] = candidate
            state["stages"][repo]["status"] = "verified"
            state["stages"][repo]["candidate"] = candidate
            state["current_run"] = None
            state["updated_at"] = now()
            dispatch_repo = next_stage(state)
            dispatch_integration_after = dispatch_repo is None
            state["dispatch_reservation"] = {
                "phase": "integration" if dispatch_integration_after else "implement",
                "repo": None if dispatch_integration_after else dispatch_repo,
                "source_run_id": row["id"],
                "status": "dispatching",
                "owner_pid": os.getpid(),
                "owner_fingerprint": process_fingerprint(),
                "reserved_at": now(),
            }
            state = write_state(control_dir, state)
    if dispatch_repo is not None:
        dispatched = dispatch_repository_stage(host, args, state, dispatch_repo)
        if dispatched.get("result") is not None:
            next_result = dict(dispatched["result"])
            next_result.setdefault("feature_chain", {
                "logical_chain_id": state["logical_chain_id"],
                "repo": dispatch_repo,
                "phase": "implement",
            })
            return advance(host, args, dispatched["row"], next_result)
        return {"state": dispatched["state"], "row": dispatched["row"], "result": dispatched.get("result"), "next_repo": dispatch_repo, "candidate": candidate}
    if dispatch_integration_after:
        dispatched = dispatch_integration(host, args, state)
        if dispatched.get("result") is not None:
            next_result = dict(dispatched["result"])
            next_result.setdefault("feature_chain", {
                "logical_chain_id": state["logical_chain_id"],
                "phase": "integration",
            })
            return advance(host, args, dispatched["row"], next_result)
        return {"state": dispatched["state"], "row": dispatched["row"], "result": dispatched.get("result"), "next_repo": None, "candidate": candidate, "integration_started": True}
    return {"state": state, "next_repo": None}


def advance_unguarded(host: Any, args: Any, row: dict, result: dict) -> dict:
    """Claude chain progression: no control token, no approve-time hook.

    The human approves the plan gate with ``archon workflow approve``; the
    approval is sealed here from the completed planning run's artifacts, then
    the chain advances exactly as the guarded codex path does.
    """
    control_dir = Path(args.control_dir)
    chain_id = (result.get("feature_chain") or {}).get("logical_chain_id")
    if not isinstance(chain_id, str):
        raise FeatureChainError("advance requires feature chain id")
    with chain_lock(control_dir, chain_id):
        state = read_state(control_dir, chain_id)
        if state.get("provider") != "claude":
            raise FeatureChainError("advance_unguarded is only for claude chains; use guarded approve/resume")
        current = state.get("current_run")
        if (
            isinstance(current, dict)
            and current.get("phase") == "planning"
            and current.get("run_id") == row.get("id")
            and state.get("approval") is None
        ):
            if result.get("state") != "terminal" or result.get("status") != "completed":
                raise FeatureChainError("planning run must complete through its gate before its plan can be sealed")
            seal_planning_approval(host, args, row, state)
    return advance(host, args, row, result)


def seal_planning_approval(host: Any, args: Any, row: dict, state: dict) -> dict:
    current = state.get("current_run")
    if not isinstance(current, dict) or current.get("phase") != "planning" or current.get("run_id") != row.get("id"):
        raise FeatureChainError("planning approval is stale for this feature chain")
    artifacts = artifact_dir(host, row)
    plan = read_json_artifact(artifacts / JOINT_PLAN_ARTIFACT, JOINT_PLAN_ARTIFACT)
    source_files = {JOINT_PLAN_ARTIFACT: file_digest(artifacts / JOINT_PLAN_ARTIFACT)}
    for name in PLANNING_SUPPORT_ARTIFACTS:
        if (artifacts / name).is_file():
            source_files[name] = file_digest(artifacts / name)
    source_artifacts = {"root": str(artifacts), "files": source_files}
    return approve_plan_unlocked(Path(args.control_dir), state, plan, source_artifacts)


def restore_control(host: Any, row: dict, control_dir: Path, control: dict | None) -> dict[str, str]:
    feature = control.get("feature_chain") if isinstance(control, dict) else None
    if not isinstance(feature, dict) or feature.get("scope") != "repositories":
        return {}
    chain_id = feature.get("logical_chain_id")
    repo = feature.get("repo")
    if not isinstance(chain_id, str) or not isinstance(repo, str):
        if feature.get("phase") not in {"planning", "integration"}:
            raise FeatureChainError("repository-list control record is malformed")
    state = read_state(control_dir, chain_id)
    phase = str(feature.get("phase") or "implement")
    current = state.get("current_run")
    if phase in {"planning", "integration"}:
        if not isinstance(current, dict) or current.get("run_id") != row.get("id") or current.get("phase") != phase:
            raise FeatureChainError("repository-list control record is stale for this run")
        env = planning_env(state, Path(str(current.get("artifacts_dir")))) if phase == "planning" else {
            "ARCHON_FEATURE_SCOPE": "repositories",
            "ARCHON_FEATURE_CHAIN_ID": state["logical_chain_id"],
            "ARCHON_FEATURE_PROVIDER": state["provider"],
            "ARCHON_FEATURE_PHASE": "integration",
            "ARCHON_FEATURE_REPOSITORIES": ",".join(state["repositories"]),
        }
    else:
        bound = state["child_runs"].get(repo)
        if not isinstance(bound, dict) or bound.get("run_id") != row.get("id"):
            raise FeatureChainError("repository-list control record is stale for this run")
        env = stage_env(state, repo, str(feature.get("phase") or bound.get("phase") or "implement"))
    env["ARCHON_CONTROL_DIR"] = str(control_dir)
    os.environ.update(env)
    return env


def before_control(host: Any, args: Any, row: dict, control: dict | None) -> dict | None:
    feature = control.get("feature_chain") if isinstance(control, dict) else None
    if not isinstance(feature, dict) or feature.get("scope") != "repositories":
        return None
    chain_id = feature.get("logical_chain_id")
    repo = feature.get("repo")
    if not isinstance(chain_id, str) or not isinstance(repo, str):
        if feature.get("phase") not in {"planning", "integration"}:
            raise FeatureChainError("repository-list control record is malformed")
    with chain_lock(Path(args.control_dir), chain_id):
        state = read_state(Path(args.control_dir), chain_id)
        require_no_incomplete_amendment(state)
        args.wall_minutes = state["budget"]["wall_minutes"]
        args.max_total_tokens = state["budget"]["max_total_tokens"]
        action = getattr(args, "action", "")
        stopping_action = action in {"reject", "abandon"}
        if not stopping_action:
            require_no_incomplete_scope_amendment(state)
        phase = str(feature.get("phase") or "implement")
        if phase in {"planning", "integration"}:
            current = state.get("current_run")
            if not isinstance(current, dict) or current.get("run_id") != row.get("id") or current.get("phase") != phase:
                raise FeatureChainError("stale repository-list control action")
            revalidate_control_token(host, args, row)
            if stopping_action:
                return claim_control_unlocked(Path(args.control_dir), state, args, row)
            if action == "resume":
                shepherd_unlocked(args, state)
            budget_require_remaining(args, chain_id)
            if phase == "integration":
                verify_approval(state)
            state = claim_control_unlocked(Path(args.control_dir), state, args, row)
            if phase == "planning" and action == "approve":
                try:
                    state = seal_planning_approval(host, args, row, state)
                except Exception:
                    state["pending_control"] = None
                    write_state(Path(args.control_dir), state)
                    raise
        else:
            bound = state["child_runs"].get(repo)
            if not isinstance(bound, dict) or bound.get("run_id") != row.get("id"):
                raise FeatureChainError("stale repository-list control action")
            revalidate_control_token(host, args, row)
            if stopping_action:
                return claim_control_unlocked(Path(args.control_dir), state, args, row)
            shepherd_unlocked(args, state)
            budget_require_remaining(args, chain_id)
            verify_approval(state)
            state = claim_control_unlocked(Path(args.control_dir), state, args, row)
        return state


def write_local_integration_evidence(path: Path, state: dict, evidence: dict) -> Path:
    receipt = integration_receipt(state, evidence)
    write_json_atomic(path, receipt)
    return path


def write_json_atomic(path: Path, value: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def candidate_revisions_payload(state: dict) -> dict:
    verify_approval(state)
    candidate_heads = {repo: state["candidate_handoffs"][repo]["candidate_head"] for repo in state["repositories"]}
    repositories = {}
    for repo in state["repositories"]:
        candidate = state["candidate_handoffs"].get(repo)
        if not isinstance(candidate, dict):
            raise FeatureChainError(f"integration candidate missing for {repo}")
        repositories[repo] = {
            "source_worktree": state["worktrees"][repo]["worktree"],
            "commit": candidate["candidate_head"],
        }
    return {
        "schema": "archon.joint-candidates.v1",
        "plan_digest": state["approval"]["plan_digest"],
        "approved_plan_digest": state["approval"]["plan_digest"],
        "candidate_heads": candidate_heads,
        "repositories": repositories,
    }


def write_candidate_revisions(artifacts_dir: Path, state: dict) -> Path:
    return write_json_atomic(artifacts_dir / "candidate-revisions.json", candidate_revisions_payload(state))


# --- publication: push candidate branches and open draft PRs in dependency order ---

PUBLICATION_ARTIFACT = "feature-chain-publication.json"
# Same shape release-controller.py enforces (BRANCH); duplicated because that module is not importable by name.
PUBLISH_BRANCH_RE = re.compile(r"archon/[a-zA-Z0-9][a-zA-Z0-9_/-]*\Z")
REMOTE_SLUG_RE = re.compile(r"(?:git@github\.com:|https://github\.com/)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?\Z")
PUBLICATION_SIGNED_FIELDS = {"publication_digest", "publication_mac"}


def verify_receipt(state: dict) -> dict:
    receipt = state.get("integration")
    if not isinstance(receipt, dict) or receipt.get("kind") != "repository-list-feature-receipt":
        raise FeatureChainError("chain has no repository-list receipt")
    unsigned = {k: v for k, v in receipt.items() if k not in ("receipt_digest", "receipt_mac")}
    if not hmac.compare_digest(str(receipt.get("receipt_digest", "")), digest(unsigned)):
        raise FeatureChainError("integration receipt digest mismatch")
    body = {k: v for k, v in receipt.items() if k != "receipt_mac"}
    if not hmac.compare_digest(str(receipt.get("receipt_mac", "")), hmac_sha256(state["chain_secret"], body)):
        raise FeatureChainError("integration receipt MAC mismatch")
    return receipt


def remote_slug(url: str) -> str:
    match = REMOTE_SLUG_RE.search(url.strip())
    if not match:
        raise FeatureChainError(f"cannot derive owner/repo from origin url: {url.strip()}")
    return match.group(1)


def _cmd(run: Any, argv: list[str], label: str) -> str:
    result = run(argv, capture_output=True, encoding="utf-8")
    if result.returncode != 0:
        raise FeatureChainError(f"{label} failed: {(result.stderr or result.stdout).strip()}")
    return result.stdout


def preflight_publication(state: dict) -> dict:
    if state.get("status") != "locally_verified":
        raise FeatureChainError(f"chain status is {state.get('status')!r}, not locally_verified")
    verify_receipt(state)
    verify_approval(state)
    order = list(state.get("dependency_order") or state["repositories"])
    plan = {}
    for repo in order:
        meta = state["worktrees"][repo]
        worktree = Path(meta["worktree"])
        branch = str(meta["branch"])
        baseline = str(meta["baseline"])
        head = str(state["candidate_handoffs"][repo]["candidate_head"])
        if git_output(worktree, "rev-parse", "HEAD") != head:
            raise FeatureChainError(f"{repo} worktree HEAD drifted from candidate head {head[:12]}")
        assert_clean_worktree(worktree, repo)
        git_output(worktree, "merge-base", "--is-ancestor", baseline, head)
        if git_output(worktree, "branch", "--show-current") != branch or not PUBLISH_BRANCH_RE.fullmatch(branch):
            raise FeatureChainError(f"{repo} worktree branch does not match recorded archon branch {branch}")
        plan[repo] = {
            "worktree": worktree, "branch": branch, "baseline": baseline, "head": head,
            "slug": remote_slug(git_output(worktree, "remote", "get-url", "origin")),
        }
    return plan


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip() if path.is_file() else ""


def publication_title(state: dict, repo: str) -> str:
    artifacts = Path(state["candidate_handoffs"][repo]["artifacts"])
    subject = _read_text(artifacts / "commit-msg.txt").splitlines()
    first = subject[0].strip() if subject else git_output(Path(state["worktrees"][repo]["worktree"]), "log", "-1", "--format=%s")
    return f"{first} [archon]"


def _spec_summary(state: dict) -> tuple[str, str, str]:
    text = _read_text(Path(state["spec"]))
    title = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), Path(state["spec"]).stem)
    linear = re.search(r"https://linear\.app/\S+", text)
    goal = ""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip().lower() == "## outcome":
            goal = " ".join(l.strip() for l in lines[index + 1:index + 6] if l.strip() and not l.startswith("#") and not l.startswith("`"))
            break
    return title, goal, linear.group(0).rstrip(".,)") if linear else ""


def _last_round_advisory(artifacts: Path) -> list[str]:
    rounds = sorted((p for p in artifacts.glob("round-*") if p.is_dir()), key=lambda p: int(p.name.split("-")[1]) if p.name.split("-")[1].isdigit() else -1)
    for rnd in reversed(rounds):
        path = rnd / "fixer-result.json"
        if path.is_file():
            try:
                advisory = json.loads(path.read_text(encoding="utf-8")).get("advisory") or []
            except json.JSONDecodeError:
                return []
            return [f"{rnd.name}: {item.get('finding')}" for item in advisory if isinstance(item, dict) and item.get("finding")]
    return []


def publication_body(state: dict, repo: str, publications: dict) -> str:
    candidate = state["candidate_handoffs"][repo]
    artifacts = Path(candidate["artifacts"])
    receipt = state["integration"]
    order = list(state.get("dependency_order") or state["repositories"])
    depends_on = list((state["stages"][repo].get("plan") or {}).get("depends_on") or [])
    title, goal, linear = _spec_summary(state)
    out = ["## Summary", "", title]
    if goal:
        out += ["", goal]
    if linear:
        out += ["", f"Linear: {linear}"]
    out += ["", "## Cross-Repository Chain", "", f"- Archon chain `{state['logical_chain_id']}` (repository-list feature, order: {', '.join(order)})."]
    for name in order:
        head = state["candidate_handoffs"][name]["candidate_head"]
        pub = publications.get(name) or {}
        if name == repo:
            link = "this PR"
        elif pub.get("outcome") == "NO_CHANGE":
            link = "no change, no PR"
        else:
            link = pub.get("pr_url") or "PR pending"
        out.append(f"- {name} candidate head: `{head}` ({link}).")
    if depends_on:
        out.append(f"- Depends on: {', '.join(depends_on)}. Merge those PRs first.")
    out += ["", "## Verification", "", f"Candidate verification (archon stage run `{candidate['run_id'][:8]}`):", ""]
    for item in candidate.get("verification_evidence") or []:
        count = f", {item['tests_passed']} tests" if isinstance(item.get("tests_passed"), int) and item["tests_passed"] else ""
        out.append(f"- {item.get('name') or item.get('command')}: {item.get('status')}{count}")
    smoke = _read_text(artifacts / "smoke-result.txt").splitlines()
    if smoke:
        out.append(f"- smoke result: `{smoke[0]}`")
    evidence = receipt.get("integration_evidence") or {}
    counters = evidence.get("counters") or {}
    scenarios = [c.get("scenario") for c in evidence.get("commands") or [] if isinstance(c, dict) and c.get("scenario")]
    out += ["", f"Joint integration: status {evidence.get('status')}, {counters.get('tests_passed', 0)} tests passed across {counters.get('scenarios', len(scenarios))} scenario(s)."]
    out += [f"- {name}" for name in scenarios]
    out += ["", f"Receipt digest `{receipt.get('receipt_digest')}`.", "", "## Known Residuals", ""]
    residual_lines = []
    accepted = _read_text(artifacts / "accept-residuals.txt")
    if accepted:
        residual_lines += ["Accepted residuals:", "", "```", accepted, "```", ""]
    waivers = [line[3:].strip() for line in _read_text(artifacts / "waivers.md").splitlines() if line.startswith("## ")]
    if waivers:
        residual_lines += ["Waived findings (`waivers.md`):", ""] + [f"- {w}" for w in waivers] + [""]
    advisory = _last_round_advisory(artifacts)
    if advisory:
        residual_lines += ["Last-round fixer advisory:", ""] + [f"- {a}" for a in advisory] + [""]
    out += residual_lines or ["None recorded.", ""]
    changed = diff_files_since(Path(state["worktrees"][repo]["worktree"]), state["worktrees"][repo]["baseline"], candidate["candidate_head"])
    out += ["## Post-Deploy Monitoring & Validation", "",
            "- Watch error rates (4xx/5xx or tool failures) on the surfaces changed by this PR after release.",
            "- Sentry: new issues originating in the changed modules.",
            "- Validate the feature on staging end to end before marking ready.", "", "Changed files:", ""]
    out += [f"- `{path}`" for path in changed]
    return "\n".join(out).rstrip() + "\n"


def publication_record(state: dict, publications: dict) -> dict:
    body = {
        "schema_version": 1,
        "kind": "repository-list-feature-publication",
        "logical_chain_id": state["logical_chain_id"],
        "receipt_digest": state["integration"]["receipt_digest"],
        "publications": publications,
        "recorded_at": now(),
    }
    body["publication_digest"] = digest(body)
    body["publication_mac"] = hmac_sha256(state["chain_secret"], body)
    return body


def verify_publication_record(state: dict, record: dict) -> dict:
    unsigned = {k: v for k, v in record.items() if k not in PUBLICATION_SIGNED_FIELDS}
    if not hmac.compare_digest(str(record.get("publication_digest", "")), digest(unsigned)):
        raise FeatureChainError("publication record digest mismatch")
    body = {k: v for k, v in record.items() if k != "publication_mac"}
    if not hmac.compare_digest(str(record.get("publication_mac", "")), hmac_sha256(state["chain_secret"], body)):
        raise FeatureChainError("publication record MAC mismatch")
    return record


def _write_body(text: str) -> Path:
    fd, name = tempfile.mkstemp(prefix="archon-prbody-", suffix=".md")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    return Path(name)


def _open_or_adopt_pr(run: Any, repo: str, info: dict, title: str, body: str) -> str:
    listed = json.loads(_cmd(run, ["gh", "pr", "list", "--repo", info["slug"], "--head", info["branch"], "--state", "open",
                                   "--json", "url,headRefOid,isDraft"], f"gh pr list {repo}") or "[]")
    if len(listed) == 1 and listed[0].get("headRefOid") == info["head"]:
        return str(listed[0]["url"])
    if listed:
        found = ",".join(str(item.get("headRefOid", ""))[:12] for item in listed)
        raise FeatureChainError(f"pr head mismatch repo={repo} branch={info['branch']} expected={info['head'][:12]} open={found}")
    path = _write_body(body)
    try:
        return _cmd(run, ["gh", "pr", "create", "--repo", info["slug"], "--draft", "--base", "main", "--head", info["branch"],
                          "--title", title, "--body-file", str(path)], f"gh pr create {repo}").strip().splitlines()[-1]
    finally:
        path.unlink(missing_ok=True)


def _sync_body(run: Any, repo: str, info: dict, url: str, body: str) -> bool:
    current = _cmd(run, ["gh", "pr", "view", url, "--repo", info["slug"], "--json", "body", "-q", ".body"], f"gh pr view {repo}")
    if current.rstrip() == body.rstrip():
        return False
    path = _write_body(body)
    try:
        _cmd(run, ["gh", "pr", "edit", url, "--repo", info["slug"], "--body-file", str(path)], f"gh pr edit {repo}")
    finally:
        path.unlink(missing_ok=True)
    return True


def publish(host: Any, args: Any, chain_id: str, run: Any = subprocess.run) -> dict:
    """Push each verified candidate branch and open (or adopt) a draft PR, in dependency order.

    Never force-pushes, never marks ready, never merges. Re-runnable: already-recorded
    publications with the same head are kept, open PRs on the exact head are adopted.
    """
    control_dir = Path(args.control_dir)
    with chain_lock(control_dir, chain_id):
        state = read_state(control_dir, chain_id)
        plan = preflight_publication(state)
        # Resolved before any push: the receipt body carries no artifacts path, so these
        # two sources are the whole fallback chain, and a chain with neither must fail
        # here, ahead of the irreversible GitHub work, not after it.
        artifacts_dir = state.get("integration_artifacts") or (state.get("current_run") or {}).get("artifacts_dir")
        if not artifacts_dir:
            raise FeatureChainError("chain has no integration artifacts directory for the publication record")
        prior = state.get("publication")
        publications = dict(verify_publication_record(state, prior)["publications"]) if isinstance(prior, dict) else {}
        for repo, info in plan.items():
            recorded = publications.get(repo)
            if isinstance(recorded, dict) and recorded.get("head") == info["head"] and (recorded.get("outcome") == "NO_CHANGE" or recorded.get("pr_url")):
                continue
            if info["head"] == info["baseline"]:
                publications[repo] = {"branch": info["branch"], "head": info["head"], "outcome": "NO_CHANGE", "pr_url": None, "draft": None, "published_at": now()}
                continue
            _cmd(run, ["git", "-C", str(info["worktree"]), "push", "-u", "origin", info["branch"], "--no-verify"], f"git push {repo}")
            url = _open_or_adopt_pr(run, repo, info, publication_title(state, repo), publication_body(state, repo, publications))
            publications[repo] = {"branch": info["branch"], "head": info["head"], "outcome": "CHANGED", "pr_url": url, "draft": True, "published_at": now()}
        edited = []
        for repo, info in plan.items():
            url = publications[repo].get("pr_url")
            if url and _sync_body(run, repo, info, url, publication_body(state, repo, publications)):
                edited.append(repo)
        record = publication_record(state, publications)
        record_path = write_json_atomic(Path(str(artifacts_dir)) / PUBLICATION_ARTIFACT, record)
        state["publication"] = record
        state["updated_at"] = now()
        state = write_state(control_dir, state)
    summary = " ".join(f"{repo}={pub['pr_url'] or pub['outcome']}" for repo, pub in publications.items())
    print(f"ARCHON_FEATURE_REPOSITORY_CHAIN=PUBLISHED chain={chain_id} {summary} edited={','.join(edited) or 'none'} "
          f"record={record_path} next=\"babysit is per-PR; the merge click is yours\"")
    return {"state": state, "publications": publications, "record": record, "record_path": str(record_path), "edited": edited}
