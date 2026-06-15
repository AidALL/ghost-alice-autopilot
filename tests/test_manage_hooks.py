"""Self-contained tests for the autopilot-mode addon hook lifecycle.

These do not depend on the Ghost-ALICE core: they exercise scripts/manage_hooks.py
directly, verifying idempotent install and clean uninstall on both platforms.
"""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("manage_hooks", REPO_ROOT / "scripts" / "manage_hooks.py")
manage_hooks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(manage_hooks)


class ManageHooksTest(unittest.TestCase):
    def _settings(self, home: Path, platform: str) -> dict:
        path = home / (".claude/settings.json" if platform == "claude" else ".codex/hooks.json")
        return json.loads(path.read_text(encoding="utf-8"))

    def _autopilot_commands(self, settings: dict) -> list[str]:
        out = []
        for event_list in settings.get("hooks", {}).values():
            if isinstance(event_list, list):
                for group in event_list:
                    for hook in group.get("hooks", []):
                        cmd = hook.get("command", "")
                        if "[autopilot]" in cmd:
                            out.append(cmd)
        return out

    def test_install_is_idempotent_then_uninstall_clears(self) -> None:
        for platform, config_env, skills_marker in (
            ("claude", "CLAUDE_CONFIG_DIR", ".claude/skills/autopilot-mode/scripts"),
            ("codex", "CODEX_HOME", ".agents/skills/autopilot-mode/scripts"),
        ):
            with tempfile.TemporaryDirectory() as temp:
                home = Path(temp)
                env_keys = ("HOME", config_env)
                saved = {k: os.environ.get(k) for k in env_keys}
                try:
                    os.environ["HOME"] = str(home)
                    os.environ.pop(config_env, None)
                    (home / (".claude" if platform == "claude" else ".codex")).mkdir(parents=True)

                    manage_hooks.install(platform)
                    manage_hooks.install(platform)  # idempotent

                    settings = self._settings(home, platform)
                    stop = [
                        h["command"]
                        for g in settings["hooks"]["Stop"]
                        for h in g["hooks"]
                    ]
                    ups = [
                        h["command"]
                        for g in settings["hooks"]["UserPromptSubmit"]
                        for h in g["hooks"]
                    ]
                    self.assertEqual(sum("[autopilot] stop-inject" in c for c in stop), 1, platform)
                    self.assertEqual(sum("[autopilot] reset-count" in c for c in ups), 1, platform)
                    self.assertTrue(
                        any(skills_marker in c and "autopilot_stop_hook.py" in c for c in stop),
                        msg=f"{platform}: stop hook path wrong: {stop}",
                    )
                    if platform == "claude":
                        self.assertTrue(any('"--platform" "claude"' in c for c in stop))

                    manage_hooks.uninstall(platform)
                    self.assertEqual(self._autopilot_commands(self._settings(home, platform)), [], platform)
                finally:
                    for k, v in saved.items():
                        if v is None:
                            os.environ.pop(k, None)
                        else:
                            os.environ[k] = v

    def test_uninstall_without_config_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            saved = os.environ.get("HOME")
            try:
                os.environ["HOME"] = str(home)
                os.environ.pop("CLAUDE_CONFIG_DIR", None)
                self.assertEqual(manage_hooks.uninstall("claude"), 0)
            finally:
                if saved is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = saved


if __name__ == "__main__":
    unittest.main()
