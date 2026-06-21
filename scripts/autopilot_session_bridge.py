#!/usr/bin/env python3
"""Repository wrapper for the autopilot-mode skill session bridge.

Dependencies: Python 3.11+ standard library only.
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
from typing import Any


class BridgeWrapperError(ValueError):
    """Raised when the repository wrapper cannot load the skill bridge."""


def _skill_bridge_path() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "addons"
        / "autopilot-mode"
        / "skill"
        / "scripts"
        / "autopilot_session_bridge.py"
    )


def _load_skill_bridge() -> Any:
    path = _skill_bridge_path()
    spec = importlib.util.spec_from_file_location("autopilot_session_bridge_skill", path)
    if spec is None or spec.loader is None:
        raise BridgeWrapperError(f"cannot load skill session bridge: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bridge_session_intent_to_run_state(**kwargs: Any) -> dict[str, Any]:
    return _load_skill_bridge().bridge_session_intent_to_run_state(**kwargs)


def main(argv: list[str] | None = None) -> int:
    try:
        return _load_skill_bridge().main(argv)
    except BridgeWrapperError as exc:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.exit(1, f"autopilot-session-bridge: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
