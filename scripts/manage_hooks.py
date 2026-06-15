#!/usr/bin/env python3
"""Self-contained installer/remover for the autopilot-mode addon hooks.

This addon stays fully independent of the Ghost-ALICE core installer. The core
`install.sh --addon-source` installs the SKILL; this script wires the autopilot
Stop + UserPromptSubmit hooks into the platform config (idempotent, marker-based)
pointing at the installed skill scripts. `uninstall` removes them by marker.

Usage:
  python3 manage_hooks.py install   --platform claude|codex
  python3 manage_hooks.py uninstall --platform claude|codex

Dependencies: Python 3.11+ standard library only (json, os, argparse, pathlib).
It does not import or depend on any Ghost-ALICE core module.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

SKILL_NAME = "autopilot-mode"
STOP_MARKER = "[autopilot] stop-inject"
RESET_MARKER = "[autopilot] reset-count"

# A >=3.11 python resolver matching the Ghost-ALICE core hook style: it picks a
# 3.11+ interpreter and exec's the hook script. If none is found it fails safe
# (exit 0 path is the hook's own concern; here exit 127 => non-blocking).
RESOLVER = (
    "/bin/sh -c 'for py in \"${GHOST_ALICE_PYTHON:-}\" python3 python "
    "/opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3 /bin/python3 "
    "/opt/homebrew/bin/python3.[0-9]* /usr/local/bin/python3.[0-9]* /usr/bin/python3.[0-9]* /bin/python3.[0-9]*; "
    "do [ -n \"$py\" ] || continue; "
    "if command -v \"$py\" >/dev/null 2>&1 || [ -x \"$py\" ]; then "
    "\"$py\" -c \"import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)\" >/dev/null 2>&1 "
    "&& exec \"$py\" \"$@\"; fi; done; "
    "echo \"autopilot-mode hook requires Python 3.11+\" >&2; exit 127' "
)


def _home() -> Path:
    return Path(os.environ.get("HOME") or os.path.expanduser("~"))


def _platform_paths(platform: str) -> tuple[Path, Path]:
    """Return (settings_file, installed_skills_dir) for the platform."""
    home = _home()
    if platform == "claude":
        config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (home / ".claude"))
        return config_dir / "settings.json", config_dir / "skills"
    if platform == "codex":
        config_dir = Path(os.environ.get("CODEX_HOME") or (home / ".codex"))
        return config_dir / "hooks.json", home / ".agents" / "skills"
    raise SystemExit(f"unknown platform: {platform}")


def _q(value: str) -> str:
    return '"' + str(value).replace('"', '\\"') + '"'


def _hook_command(skills_dir: Path, script_rel: str, args: list[str], marker: str) -> str:
    script = skills_dir / SKILL_NAME / script_rel
    quoted = " ".join(_q(a) for a in [str(script), *args])
    return f"{RESOLVER}autopilot {quoted} # {marker}"


def _entry(command: str) -> dict:
    return {"matcher": "", "hooks": [{"type": "command", "command": command}]}


def _remove_marker(hook_list: list, marker: str) -> int:
    removed = 0
    rebuilt = []
    for group in hook_list:
        hooks = [h for h in group.get("hooks", []) if marker not in h.get("command", "")]
        removed += len(group.get("hooks", [])) - len(hooks)
        if hooks:
            group = dict(group)
            group["hooks"] = hooks
            rebuilt.append(group)
    hook_list[:] = rebuilt
    return removed


def _load(settings_file: Path) -> dict:
    try:
        data = json.loads(settings_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(settings_file: Path, data: dict) -> None:
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


_SPECS = [
    ("Stop", "scripts/autopilot_stop_hook.py", STOP_MARKER, True),
    ("UserPromptSubmit", "scripts/reset_inject_count.py", RESET_MARKER, False),
]


def install(platform: str) -> int:
    settings_file, skills_dir = _platform_paths(platform)
    data = _load(settings_file)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SystemExit(f"{settings_file}: 'hooks' is not an object")
    for event, script_rel, marker, with_platform in _SPECS:
        hook_list = hooks.setdefault(event, [])
        _remove_marker(hook_list, marker)  # idempotent: replace any prior entry
        args = ["--platform", platform] if with_platform else []
        hook_list.append(_entry(_hook_command(skills_dir, script_rel, args, marker)))
    _save(settings_file, data)
    print(f"[autopilot] hooks installed for {platform} -> {settings_file}")
    return 0


def uninstall(platform: str) -> int:
    settings_file, _ = _platform_paths(platform)
    if not settings_file.exists():
        print(f"[autopilot] no config at {settings_file}; nothing to remove")
        return 0
    data = _load(settings_file)
    hooks = data.get("hooks", {})
    removed = 0
    if isinstance(hooks, dict):
        for event_list in hooks.values():
            if isinstance(event_list, list):
                removed += _remove_marker(event_list, STOP_MARKER)
                removed += _remove_marker(event_list, RESET_MARKER)
    _save(settings_file, data)
    print(f"[autopilot] removed {removed} hook entry(ies) for {platform} -> {settings_file}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="autopilot-mode addon hook manager")
    parser.add_argument("action", choices=["install", "uninstall"])
    parser.add_argument("--platform", required=True, choices=["claude", "codex"])
    args = parser.parse_args(argv)
    return install(args.platform) if args.action == "install" else uninstall(args.platform)


if __name__ == "__main__":
    raise SystemExit(main())
