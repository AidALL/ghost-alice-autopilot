#!/usr/bin/env python3
"""Core-registered pretool origin capture. Dependencies: Python 3.11+ and AP/Core.

This adapter never classifies a command or admits, advances, or completes work.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from autopilot_mode import _env_with_hook_cwd, _read_hook_input
from autopilot_provenance import capture_origin, _helper


def capture_before_tool(source, hook_input, *, preflight=False):
    if hook_input.get("hook_event_name", hook_input.get("hookEventName")) != "PreToolUse":
        return {"status": "skipped"}
    current = _helper().adapter.current_session_context(
        _env_with_hook_cwd(hook_input, dict(source)), hook_input=hook_input)
    if current.get("GHOST_ALICE_PLATFORM") not in {"codex", "claude"}:
        return {"status": "skipped"}
    if not current.get("GHOST_ALICE_SESSION_ID"):
        return {"status": "skipped"}
    if not current.get("GHOST_ALICE_SESSION_INTENT_ROOT"):
        helper = _helper()
        candidates = helper.adapter._session_intent_root_candidates(current, helper.adapter._project_cwd_from_env(current))
        if not any((root / "ghost-state.sqlite3").is_file() for root in candidates):
            return {"status": "skipped"}
    try:
        return capture_origin(current, preflight=preflight)
    except FileNotFoundError:
        return {"status": "skipped"}


def main():
    if len(sys.argv) != 1:
        return 64
    try:
        captured = capture_before_tool(os.environ, _read_hook_input(), preflight=True)
        payload = {"continue": True}
        if captured.get("additional_context"):
            payload["hookSpecificOutput"] = {"hookEventName": "PreToolUse",
                "additionalContext": captured["additional_context"]}
    except (ValueError, OSError) as exc:
        payload = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": "Prospective completion origin could not be captured: " + str(exc)}}
    print(json.dumps(payload))
    return 0


if __name__ == "__main__": raise SystemExit(main())
