#!/usr/bin/env python3
"""Deterministic token allowance estimates for repository-list feature chains."""
from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import control_contract  # noqa: E402


SUPPORTED_REPOSITORIES = {"api", "goodword-mcp", "web-app"}
COLD_START_PRIORS = {
    "planning": (6_000_000, 12_000_000),
    "implement": (8_000_000, 16_000_000),
    "integration": (2_000_000, 4_000_000),
}
CONTINGENCY_RATE = 0.25


class EstimateError(ValueError):
    pass


def _non_negative_int(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise EstimateError(f"{name} must be a non-negative integer")
    return value


def _repositories(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise EstimateError("repositories must be a non-empty list")
    seen: set[str] = set()
    repos: list[str] = []
    for item in value:
        if not isinstance(item, str) or item not in SUPPORTED_REPOSITORIES:
            raise EstimateError(f"unsupported repository: {item!r}")
        if item in seen:
            raise EstimateError(f"duplicate repository: {item}")
        seen.add(item)
        repos.append(item)
    return repos


def _phase_kind(phase: str) -> str:
    if phase == "planning":
        return "planning"
    if phase == "integration":
        return "integration"
    if phase.startswith("implement:") and phase.split(":", 1)[1] in SUPPORTED_REPOSITORIES:
        return "implement"
    raise EstimateError(f"unsupported phase key: {phase}")


def _parse_phase(row: Any, label: str) -> tuple[int, bool] | None:
    if row is None:
        return None
    if not isinstance(row, dict):
        raise EstimateError(f"{label} must be an object")
    tokens = _non_negative_int(row.get("tokens"), f"{label} tokens")
    complete = row.get("complete")
    if type(complete) is not bool:
        raise EstimateError(f"{label} complete must be boolean")
    return tokens, complete


def _observations(rows: Any) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(rows, list):
        raise EstimateError("observations must be a list")
    seen: set[str] = set()
    accepted: list[dict[str, Any]] = []
    diagnostics: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise EstimateError(f"observation[{index}] must be an object")
        chain_id = row.get("chain_id")
        if not isinstance(chain_id, str) or not chain_id:
            raise EstimateError(f"observation[{index}] must include chain_id")
        if chain_id in seen:
            diagnostics.append(f"observation {chain_id} ignored as duplicate")
            continue
        seen.add(chain_id)
        if row.get("provider") != "codex":
            diagnostics.append(f"observation {chain_id} ignored because provider is not codex")
            continue
        _repositories(row.get("repositories"))
        if not isinstance(row.get("phases"), dict):
            raise EstimateError(f"observation {chain_id} phases must be an object")
        accepted.append(row)
    return accepted, diagnostics


def _matching_samples(rows: list[dict[str, Any]], repositories: list[str], phase: str) -> tuple[list[int], list[int], int]:
    kind = _phase_kind(phase)
    repo_set = set(repositories)
    completed: list[int] = []
    incomplete: list[int] = []
    comparable = 0
    for row in rows:
        observed_repos = set(row["repositories"])
        if kind in {"planning", "integration"} and observed_repos != repo_set:
            continue
        if kind == "implement" and phase.split(":", 1)[1] not in observed_repos:
            continue
        comparable += 1
        parsed = _parse_phase(row["phases"].get(phase), f"observation {row['chain_id']} {phase}")
        if parsed is None:
            continue
        tokens, complete = parsed
        if complete:
            completed.append(tokens)
        else:
            incomplete.append(tokens)
    return completed, incomplete, comparable


def _total_phase_estimate(rows: list[dict[str, Any]], repositories: list[str], phase: str) -> dict[str, Any]:
    completed, incomplete, comparable = _matching_samples(rows, repositories, phase)
    kind = _phase_kind(phase)
    prior_low, prior_high = COLD_START_PRIORS[kind]
    lower_bound = max(incomplete) if incomplete else 0
    assumptions: list[str] = []
    if completed:
        low = max(min(completed), lower_bound)
        high = max(math.ceil(max(completed) * 1.25), lower_bound)
        assumptions.append("completed samples use max observed tokens plus 25% margin")
        if len(completed) < 3:
            high = max(high, prior_high)
            assumptions.append("fewer than three completed samples: retaining the cold-start upper bound")
        confidence = "medium" if len(completed) >= 3 and not incomplete else "low"
    else:
        low = max(prior_low, lower_bound)
        high = max(prior_high, lower_bound)
        assumptions.append(f"cold-start prior for {kind}: {prior_low}-{prior_high} tokens")
        confidence = "low"
    if incomplete:
        assumptions.append("incomplete runs are lower-bound evidence only")
        if lower_bound >= high:
            high = math.ceil(lower_bound * 1.25)
        confidence = "low"
    if high < low:
        high = low
    return {
        "phase": phase,
        "range": {"low": low, "high": high},
        "confidence": confidence,
        "evidence": {
            "comparable_chains": comparable,
            "completed_samples": len(completed),
            "incomplete_lower_bounds": len(incomplete),
            "max_incomplete_tokens": lower_bound,
        },
        "assumptions": assumptions,
    }


def _phase_usage(value: dict[str, int] | None) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise EstimateError("phase_usage must be an object")
    parsed: dict[str, int] = {}
    for phase, tokens in value.items():
        if not isinstance(phase, str):
            raise EstimateError("phase_usage keys must be strings")
        _phase_kind(phase)
        parsed[phase] = _non_negative_int(tokens, f"phase_usage {phase}")
    return parsed


def _remaining_phase(total: dict[str, Any], used: int) -> dict[str, Any]:
    low = int(total["range"]["low"])
    high = int(total["range"]["high"])
    remaining_low = max(0, low - used)
    remaining_high = max(0, high - used)
    overrun = used >= high and high > 0
    reserve = 0
    assumptions = list(total["assumptions"])
    if overrun:
        reserve = math.ceil(high * CONTINGENCY_RATE)
        remaining_high = max(remaining_high, reserve)
        assumptions.append("phase usage reached the forecast band; retaining one bounded retry reserve")
    phase = dict(total)
    phase["total_range"] = total["range"]
    phase["phase_used_tokens"] = used
    phase["range"] = {"low": remaining_low, "high": remaining_high}
    phase["overrun"] = overrun
    phase["overrun_reserve_tokens"] = reserve
    phase["assumptions"] = assumptions
    return phase


def _remaining_phase_keys(
    repositories: list[str],
    completed_repositories: list[str] | None,
    planning_complete: bool,
    integration_complete: bool,
) -> list[str]:
    completed = set(_repositories(completed_repositories) if completed_repositories else [])
    outside = completed - set(repositories)
    if outside:
        raise EstimateError("completed repositories are outside selected scope: " + ",".join(sorted(outside)))
    phases: list[str] = []
    if not planning_complete:
        phases.append("planning")
    for repo in sorted(repo for repo in repositories if repo not in completed):
        phases.append(f"implement:{repo}")
    if not integration_complete:
        phases.append("integration")
    return phases


def _confidence(phases: list[dict[str, Any]]) -> str:
    if not phases:
        return "high"
    values = {phase["confidence"] for phase in phases}
    if "low" in values:
        return "low"
    return "medium" if "medium" in values else "low"


def estimate(
    repositories: list[str],
    allowance: int,
    observations: list[dict[str, Any]],
    *,
    used_tokens: int = 0,
    completed_repositories: list[str] | None = None,
    planning_complete: bool = False,
    integration_complete: bool = False,
    current_chain_id: str | None = None,
    phase_usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    repos = _repositories(repositories)
    allowance = _non_negative_int(allowance, "allowance")
    used_tokens = _non_negative_int(used_tokens, "used_tokens")
    if type(planning_complete) is not bool or type(integration_complete) is not bool:
        raise EstimateError("planning_complete and integration_complete must be booleans")
    rows, diagnostics = _observations(observations)
    usage = _phase_usage(phase_usage)
    phases = [
        _remaining_phase(_total_phase_estimate(rows, repos, phase), usage.get(phase, 0))
        for phase in _remaining_phase_keys(repos, completed_repositories, planning_complete, integration_complete)
    ]
    base_low = sum(phase["range"]["low"] for phase in phases)
    base_high = sum(phase["range"]["high"] for phase in phases)
    retry_reserve = 0 if not phases else math.ceil(base_high * CONTINGENCY_RATE)
    remaining_high = base_high + retry_reserve
    remaining_allowance = max(allowance - used_tokens, 0)
    if remaining_allowance <= 0:
        disposition = "exhausted"
    elif remaining_allowance >= remaining_high:
        disposition = "sufficient"
    else:
        disposition = "at_risk"
    return {
        "schema": "archon.feature-token-estimate.v1",
        "repositories": repos,
        "allowance": allowance,
        "used_tokens": used_tokens,
        "remaining_allowance": remaining_allowance,
        "remaining_range": {"low": base_low, "high": remaining_high},
        "base_remaining_range": {"low": base_low, "high": base_high},
        "retry_reserve_tokens": retry_reserve,
        "recommended_total_tokens": used_tokens + remaining_high,
        "disposition": disposition,
        "confidence": _confidence(phases),
        "phases": phases,
        "diagnostics": diagnostics,
        "current_chain_id": current_chain_id,
    }


def _phase_from_stage(stage: Any) -> str | None:
    if not isinstance(stage, str):
        return None
    if stage.startswith("planning:"):
        return "planning"
    if stage.startswith("integration:"):
        return "integration"
    if stage.startswith("implement:"):
        repo = stage.split(":", 1)[1]
        return f"implement:{repo}" if repo in SUPPORTED_REPOSITORIES else None
    return None


def _phase_complete(chain_state: dict[str, Any], phase: str) -> bool:
    if phase == "planning":
        return isinstance(chain_state.get("approval"), dict)
    if phase == "integration":
        receipt = chain_state.get("integration")
        return isinstance(receipt, dict) and receipt.get("status") == "locally_verified"
    if phase.startswith("implement:"):
        repo = phase.split(":", 1)[1]
        stages = chain_state.get("stages")
        stage = stages.get(repo) if isinstance(stages, dict) else None
        return isinstance(stage, dict) and stage.get("status") == "verified"
    return False


def _session_tokens(ledger: dict[str, Any], rel: str) -> int | None:
    high_water = ledger.get("session_token_high_water")
    row = high_water.get(rel) if isinstance(high_water, dict) else None
    if not isinstance(row, dict):
        return None
    value = row.get("total_tokens")
    return value if type(value) is int and value >= 0 else None


def _observation_from_state(chain_id: str, chain_state: dict[str, Any], ledger: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    if chain_state.get("provider") != "codex":
        return None, "provider is not codex"
    try:
        repositories = _repositories(chain_state.get("repositories"))
    except EstimateError as exc:
        return None, str(exc)
    phases: dict[str, dict[str, Any]] = {}
    session_phase: dict[str, str] = {}
    counted: set[tuple[str, str]] = set()
    runs = ledger.get("runs", [])
    if not isinstance(runs, list):
        return None, "budget ledger runs must be a list"
    for run in runs:
        if not isinstance(run, dict):
            continue
        phase = _phase_from_stage(run.get("stage"))
        if phase is None:
            continue
        session_files = run.get("session_files", [])
        if not isinstance(session_files, list):
            return None, f"phase {phase} session_files must be a list"
        for rel in session_files:
            if not isinstance(rel, str):
                continue
            existing = session_phase.setdefault(rel, phase)
            if existing != phase:
                return None, f"session {rel} is shared by phases {existing} and {phase}"
            marker = (phase, rel)
            if marker in counted:
                continue
            counted.add(marker)
            tokens = _session_tokens(ledger, rel)
            if tokens is None:
                return None, f"session {rel} has no token high-water"
            row = phases.setdefault(phase, {"tokens": 0, "complete": _phase_complete(chain_state, phase)})
            row["tokens"] += tokens
    for phase, row in phases.items():
        row["complete"] = _phase_complete(chain_state, phase)
    return {
        "chain_id": chain_id,
        "provider": "codex",
        "repositories": repositories,
        "phases": phases,
    }, None


def load_observations(control_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    chain_dir = control_dir / "feature-chains-v2"
    budget_dir = control_dir / "feature-budgets"
    if not chain_dir.exists() or not budget_dir.exists():
        return [], ["feature chain or budget directory is unavailable"]
    observations: list[dict[str, Any]] = []
    diagnostics: list[str] = []
    for state_path in sorted(chain_dir.glob("*.json")):
        chain_id = state_path.stem
        try:
            chain_state = control_contract.verify_chain_state(
                control_contract.secure_read_json(state_path), chain_id=chain_id
            )
        except control_contract.ControlContractError as exc:
            diagnostics.append(f"{chain_id}: skipped untrusted chain state: {exc}")
            continue
        budget_path = budget_dir / f"{chain_id}.json"
        if not budget_path.exists():
            diagnostics.append(f"{chain_id}: skipped because budget ledger is missing")
            continue
        try:
            ledger = control_contract.secure_read_json(budget_path)
        except control_contract.ControlContractError as exc:
            diagnostics.append(f"{chain_id}: skipped unreadable budget ledger: {exc}")
            continue
        if ledger.get("logical_chain_id") != chain_id:
            diagnostics.append(f"{chain_id}: skipped budget ledger with wrong chain id")
            continue
        observation, reason = _observation_from_state(chain_id, chain_state, ledger)
        if observation is None:
            diagnostics.append(f"{chain_id}: skipped {reason}")
            continue
        if not observation["phases"]:
            diagnostics.append(f"{chain_id}: no attributable phase token usage")
        observations.append(observation)
    return observations, diagnostics
