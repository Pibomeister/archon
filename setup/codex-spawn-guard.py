#!/usr/bin/env python3
"""PreToolUse guard for native Codex subagent dispatch in Archon chains."""

from __future__ import annotations

import json
import os
import sys
from typing import Any


def block(reason: str) -> None:
    print(json.dumps({
        "decision": "block",
        "reason": reason,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }, separators=(",", ":")))


def object_or_empty(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "PreToolUse":
        return 0
    if os.environ.get("ARCHON_FEATURE_SCOPE") != "repositories":
        return 0
    tool_name = str(payload.get("tool_name") or "")
    if tool_name not in {"spawn_agent", "collaboration.spawn_agent", "collaborationspawn_agent", "multi_agent_v1.spawn_agent"}:
        return 0
    expected_model = os.environ.get("ARCHON_CODEX_PINNED_MODEL", "").strip()
    expected_effort = os.environ.get("ARCHON_CODEX_PINNED_REASONING_EFFORT", "").strip()
    if not expected_model or not expected_effort:
        block("native child dispatch is blocked because the guarded repository Codex chain has no explicit model/effort pin")
        return 0
    tool_input = object_or_empty(payload.get("tool_input"))
    requested_model = tool_input.get("model")
    requested_effort = tool_input.get("reasoning_effort")
    if requested_model != expected_model or requested_effort != expected_effort:
        block(
            "native child dispatch is blocked unless model and reasoning_effort explicitly "
            f"match this guarded repository chain: model={expected_model}, "
            f"reasoning_effort={expected_effort}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
