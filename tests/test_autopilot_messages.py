"""Tests for platform-neutral continuation-signal rendering (A2 + B)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ADAPTER_DIR = REPO_ROOT / "addons" / "autopilot-mode" / "skill" / "adapters"
sys.path.insert(0, str(ADAPTER_DIR))

import autopilot_messages as msgs  # noqa: E402


class FormatIoTraceRowsTest(unittest.TestCase):
    def test_bash_structured_row_renders_neutral_op_and_portable_path(self):
        row = {
            "tool": "Bash",
            "path": "C:\\Users\\try2q\\.agents\\skills\\foo\\SKILL.md",
            "pattern": 'Get-Content -LiteralPath "C:\\Users\\try2q\\.agents\\skills\\foo\\SKILL.md" -Raw',
            "op": "read",
        }
        lines = msgs.format_io_trace_rows([row], home_path="C:\\Users\\try2q")
        self.assertEqual(lines, ["- read ~/.agents/skills/foo/SKILL.md"])
        joined = "\n".join(lines)
        # per-runtime tool surface + absolute machine path must not leak
        self.assertNotIn("Get-Content", joined)
        self.assertNotIn("C:/Users", joined)
        self.assertNotIn("C:\\Users", joined)

    def test_bash_unstructured_row_falls_back_without_absolute_leak(self):
        row = {"tool": "Bash", "path": "n/a", "pattern": "npm run build"}
        self.assertEqual(msgs.format_io_trace_rows([row]), ["- Bash npm run build"])

    def test_bash_unstructured_row_with_path_is_portablized(self):
        row = {"tool": "Bash", "path": "n/a", "pattern": "some-tool C:\\Users\\try2q\\x.txt"}
        lines = msgs.format_io_trace_rows([row], home_path="C:\\Users\\try2q")
        self.assertEqual(lines, ["- Bash some-tool ~/x.txt"])

    def test_project_relative_takes_precedence_over_home(self):
        row = {"tool": "Bash", "path": "/home/u/proj/src/x.py", "op": "read"}
        lines = msgs.format_io_trace_rows([row], base_path="/home/u/proj", home_path="/home/u")
        self.assertEqual(lines, ["- read ./src/x.py"])

    def test_non_bash_row_path_is_portablized_and_pattern_kept(self):
        row = {"tool": "Grep", "path": "C:\\Users\\try2q\\proj", "pattern": "TODO"}
        lines = msgs.format_io_trace_rows([row], home_path="C:\\Users\\try2q")
        self.assertEqual(lines, ["- Grep ~/proj TODO"])


class BuildContinuationMessageNeutralityTest(unittest.TestCase):
    def _item(self):
        return {
            "id": "current",
            "focus_layer": "macro",
            "source_locator": "C:\\Users\\try2q\\ghost-alice\\.tmp\\s\\intent-state.json#intent-state",
            "allowed_surface": ["C:\\Users\\try2q\\proj\\.tmp\\plan.md"],
            "acceptance_criteria": ["ac-1"],
            "prompt": "do the thing",
            "completion": {},
        }

    def test_locator_and_surface_are_portablized_with_no_absolute_leak(self):
        msg = msgs.build_continuation_message(
            {"run_id": "r"},
            self._item(),
            base_path="C:\\Users\\try2q\\proj",
            home_path="C:\\Users\\try2q",
        )
        self.assertIn(
            "source-locator: ~/ghost-alice/.tmp/s/intent-state.json#intent-state", msg
        )
        self.assertIn("- ./.tmp/plan.md", msg)  # allowed-surface project-relative
        self.assertNotIn("C:/Users", msg)
        self.assertNotIn("C:\\Users", msg)


if __name__ == "__main__":
    unittest.main()
