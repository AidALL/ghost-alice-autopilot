#!/usr/bin/env python3
"""Core-registered pretool origin capture. Dependencies: Python 3.11+ and Ghost-ALICE Autopilot/Core.

This adapter never classifies a command or admits, advances, or completes work.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from autopilot_mode import _env_with_hook_cwd, _read_hook_input
from autopilot_provenance import capture_origin, _helper
from autopilot_work_items import _bind_core_policy_source, _load_core_policy_module


def capture_before_tool(source, hook_input, *, preflight=False):
    if hook_input.get("hook_event_name", hook_input.get("hookEventName")) != "PreToolUse":
        return {"status": "skipped"}
    current = _helper().adapter.current_session_context(
        _env_with_hook_cwd(hook_input, dict(source)), hook_input=hook_input)
    if current.get("GHOST_ALICE_PLATFORM") not in {"codex", "claude"}:
        return {"status": "skipped"}
    if not current.get("GHOST_ALICE_SESSION_ID"):
        return {"status": "skipped"}
    if _helper()._v2_run_is_paused(current):
        return {"status": "skipped", "reason": "autopilot-off"}
    material = None
    if not current.get("GHOST_ALICE_SESSION_INTENT_ROOT"):
        helper = _helper()
        candidates = helper.adapter._session_intent_root_candidates(current, helper.adapter._project_cwd_from_env(current))
        for root in candidates:
            if not (root / "ghost-state.sqlite3").is_file():
                continue
            try:
                material = helper.adapter.read_session_material(root, current["GHOST_ALICE_PLATFORM"],
                    current["GHOST_ALICE_SESSION_ID"], source=current,
                    require_input=False, recover_audit=False)
            except FileNotFoundError:
                continue
            current["GHOST_ALICE_SESSION_INTENT_ROOT"] = str(root.resolve())
            break
        else:
            return {"status": "skipped"}
    if material is None:
        material = _helper().adapter.read_session_material(
            Path(current["GHOST_ALICE_SESSION_INTENT_ROOT"]), current["GHOST_ALICE_PLATFORM"],
            current["GHOST_ALICE_SESSION_ID"], source=current,
            require_input=False, recover_audit=False)
    if material['intent_state'].get('intake_status') == 'pending-native-origin':
        # Core supplies the pending receipt. It grants no capture authority and
        # needs no duplicate failure notice from this adapter.
        return {"status": "skipped", "reason": "pending-native-origin"}
    try:
        return capture_origin(current, preflight=preflight)
    except _helper().CompletionRecordingError as exc:
        return exc.status()
    except FileNotFoundError:
        return {"status": "recording-unavailable", "reason": "authority-unavailable"}


def _repeated_notice(captured, source, hook_input):
    """Presentation metadata only; capture and authority checks already ran."""
    try:
        current = _helper().adapter.current_session_context(
            _env_with_hook_cwd(hook_input, dict(source)), hook_input=hook_input)
        core_root = current.get('GHOST_ALICE_CORE_ROOT') or current.get('GHOST_ALICE_SKILLS_DIR')
        intent_root = current.get('GHOST_ALICE_SESSION_INTENT_ROOT')
        if not core_root or not intent_root or not Path(core_root).is_absolute() or not Path(intent_root).is_absolute():
            return False
        with _bind_core_policy_source(current):
            cache = _load_core_policy_module('session_check_cache.py', 'ap_core_notice_cache')
        if cache is None:
            return False
        material = _helper().adapter.read_session_material(Path(intent_root), current['GHOST_ALICE_PLATFORM'],
            current['GHOST_ALICE_SESSION_ID'], source=current, require_input=True, recover_audit=False)
        identity = dict(core_root=str(Path(core_root).resolve()),
            input_event_id=material['latest_input_event']['event_id'],
            input_digest=material['latest_input_event']['input_digest'],
            status_digest=hashlib.sha256(json.dumps(captured, sort_keys=True).encode()).hexdigest())
        observed = cache.observe_check(Path(intent_root), material['platform'], material['session_id'],
            'autopilot-pretool-notice', Path(__file__), lambda: identity,
            validate=lambda prior: prior == identity)
        return observed['status'] == 'reused'
    except Exception:
        # The adapter retains the full notice when identity, reader or cache is missing.
        return False


def main():
    if len(sys.argv) != 1:
        return 64
    hook_input = {}
    try:
        hook_input = _read_hook_input()
        captured = capture_before_tool(os.environ, hook_input, preflight=True)
        payload = {"continue": True}
        if captured.get("additional_context"):
            payload["hookSpecificOutput"] = {"hookEventName": "PreToolUse",
                "additionalContext": captured["additional_context"]}
    except Exception as exc:
        # Ghost-ALICE Autopilot pretool records capture failures and permits the tool call.
        # Core retry guidance and native tool permission retain their owners.
        captured = {"status": "recording-unavailable", "reason": "capture-failure", "detail": str(exc)}
        payload = {"continue": True}
    # A successful preparation closes the previous failure notice. Record that
    # transition so the same failure recurring later is shown again.
    repeated = (captured.get("status") in {"recording-unavailable", "prepared"}
                and _repeated_notice(captured, os.environ, hook_input))
    payload['completion_recording'] = captured  # The strict hook audit receives the full capture diagnostics.
    if captured.get("status") == "recording-unavailable" and not repeated:
        lines = ["[autopilot]", "completion-recording: unavailable", "recording-reason: " + captured.get("reason", "unknown"),
            "The current-session assistant may continue independent authorized work under verification-before-completion. Ghost-ALICE Autopilot publication and application remain unavailable until the displayed recording-reason is resolved; the current-session assistant retains original proof/source/time without replaying business checks."]
        payload["hookSpecificOutput"] = {"hookEventName": "PreToolUse", "additionalContext": "\n".join(lines)}
    print(json.dumps(payload))
    return 0


if __name__ == "__main__": raise SystemExit(main())
