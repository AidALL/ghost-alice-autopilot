"""TDD for starting an approved autopilot run from a GO spec."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "addons" / "autopilot-mode" / "skill"
START_RUN = SKILL_DIR / "scripts" / "autopilot_start_run.py"
ADAPTER = SKILL_DIR / "adapters" / "autopilot_mode.py"


def _spec() -> dict:
    return {
        "run_id": "user-go-run",
        "scope": {"summary": "Implement the approved autopilot work"},
        "budget": {"remaining_steps": 3},
        "allowed_surfaces": ["src/...", "tests/..."],
        "stop_conditions": ["budget_exhausted", "user_stop", "ask_user_meta"],
        "approval_evidence": {
            "decision": "GO",
            "source": "user-explicit",
            "summary": "User explicitly told the agent to implement the approved work.",
        },
        "tasks": [
            {
                "id": "implement-first",
                "focus_layer": "meso",
                "depends_on": [],
                "prompt": "Implement the first approved unit.",
                "acceptance_criteria": ["first unit is implemented", "tests cover the change"],
                "allowed_surface": ["src/...", "tests/..."],
            }
        ],
    }


class StartRunScriptTest(unittest.TestCase):
    def test_start_script_writes_state_consumed_by_stop_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            summary = Path(tmp) / "start-summary.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(START_RUN),
                    "--project-dir",
                    str(project),
                    "--summary-json",
                    str(summary),
                ],
                input=json.dumps(_spec()),
                text=True,
                capture_output=True,
                check=False,
            )
            payload_result = subprocess.run(
                [sys.executable, str(ADAPTER)],
                cwd=project,
                input=json.dumps({"hook_event_name": "Stop", "cwd": str(project)}),
                text=True,
                capture_output=True,
                check=False,
            )
            payload = json.loads(payload_result.stdout)
            tasks = [
                json.loads(line)
                for line in (project / ".autopilot" / "tasks.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            start_summary = json.loads(summary.read_text(encoding="utf-8"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload_result.returncode, 0, payload_result.stderr)
        self.assertEqual(start_summary["run_id"], "user-go-run")
        self.assertEqual(start_summary["task_count"], 1)
        self.assertEqual(tasks[0]["status"], "running")
        self.assertEqual(payload["decision"], "block")
        self.assertIn("work-item: implement-first", payload["reason"])
        self.assertIn("Implement the first approved unit.", payload["reason"])

    def test_start_script_requires_explicit_go_approval_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            spec = _spec()
            spec["approval_evidence"] = {"decision": "NOT GO", "source": "unit-test"}

            result = subprocess.run(
                [sys.executable, str(START_RUN), "--project-dir", str(project)],
                input=json.dumps(spec),
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 64)
        self.assertIn("approval_evidence.decision must be GO", result.stderr)
        self.assertFalse((project / ".autopilot" / "approved-run.json").exists())

    def test_start_script_refuses_to_overwrite_active_run_without_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            first = subprocess.run(
                [sys.executable, str(START_RUN), "--project-dir", str(project)],
                input=json.dumps(_spec()),
                text=True,
                capture_output=True,
                check=False,
            )
            second_spec = _spec()
            second_spec["run_id"] = "second-run"
            second = subprocess.run(
                [sys.executable, str(START_RUN), "--project-dir", str(project)],
                input=json.dumps(second_spec),
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 65)
        self.assertIn("active autopilot run already exists", second.stderr)


if __name__ == "__main__":
    unittest.main()
