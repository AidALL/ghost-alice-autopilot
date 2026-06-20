#!/usr/bin/env python3
"""Thin privileged adapter for the official autopilot-mode addon."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True


def _load_autopilot_state():
    adapter_dir = Path(__file__).resolve().parent
    if not (adapter_dir / "autopilot_state.py").is_file():
        raise RuntimeError("could not locate local autopilot_state.py")
    sys.path.insert(0, str(adapter_dir))
    import autopilot_state  # type: ignore[import-not-found]

    return autopilot_state


def _read_hook_input() -> dict:
    if sys.stdin is None or sys.stdin.closed or sys.stdin.isatty():
        return {}
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _stop_hook_input(hook_input: dict) -> bool:
    return hook_input.get("hook_event_name") == "Stop" or hook_input.get("hookEventName") == "Stop"


def _format_payload_for_hook(payload: dict, hook_input: dict) -> dict:
    message = payload.get("systemMessage")
    if not message or not _stop_hook_input(hook_input):
        return payload
    formatted = dict(payload)
    formatted["decision"] = "block"
    formatted["reason"] = message
    return formatted


def _env_with_hook_cwd(hook_input: dict) -> dict[str, str]:
    env = dict(os.environ)
    if env.get("GHOST_ALICE_AUTOPILOT_RUN_DIR") or env.get("GHOST_ALICE_AUTOPILOT_CWD"):
        return env
    cwd = hook_input.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        env["GHOST_ALICE_AUTOPILOT_CWD"] = cwd
    return env


def main() -> int:
    if len(sys.argv) > 1:
        sys.stderr.write("autopilot-mode adapter accepts no arguments\n")
        return 64
    hook_input = _read_hook_input()
    try:
        autopilot_state = _load_autopilot_state()
        payload = autopilot_state.adapter_payload_from_env(_env_with_hook_cwd(hook_input))
    except Exception as exc:  # pragma: no cover - defensive hook fallback
        sys.stderr.write(f"autopilot-mode adapter fell back to no-op: {exc}\n")
        payload = {"continue": True, "systemMessage": ""}
    sys.stdout.write(json.dumps(_format_payload_for_hook(payload, hook_input)) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
