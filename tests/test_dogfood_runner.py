"""TDD for the supervised autopilot dogfood runner.

Run:
GHOST_ALICE_CORE_REPO=/path/to/ghost-alice /opt/homebrew/bin/python3 -m pytest tests/test_dogfood_runner.py -q
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPO_ROOT / "addons" / "autopilot-mode" / "skill" / "scripts" / "autopilot_dogfood_runner.py"

USER_PROMPT = (
    "euv 기반의 포토마스크의 물리적 결함 계측 알고리즘을 다양한 광학 기반 할 때, "
    "예컨대 광학 회절 등을 이용하는 그러한 기법을 이용하는 방법의 가능성을 다양하게 검토하라"
)


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class DogfoodRunnerTest(unittest.TestCase):
    def _run_runner(self, project: Path, summary: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(RUNNER),
                "--project-dir",
                str(project),
                "--prompt",
                USER_PROMPT,
                "--summary-json",
                str(summary),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_runner_bootstraps_prompt_and_records_continue_retry_stop_signal_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            summary = Path(tmp) / "summary.json"

            result = self._run_runner(project, summary)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(summary.is_file())
            report = json.loads(summary.read_text(encoding="utf-8"))
            run_dir = project / ".autopilot"
            tasks = _read_jsonl(run_dir / "tasks.jsonl")
            events = _read_jsonl(run_dir / "events.jsonl")
            approved_run = json.loads((run_dir / "approved-run.json").read_text(encoding="utf-8"))
            applied_decision_exists = (run_dir / "consistency-decision.applied.json").is_file()

        self.assertEqual(report["schema_version"], "autopilot-dogfood-summary.v1")
        self.assertEqual(report["prompt"], USER_PROMPT)
        self.assertEqual(report["run_dir"], str(run_dir))
        self.assertEqual(
            [step["stage"] for step in report["steps"]],
            ["start-first", "continue-to-second", "retry-second", "stop-second"],
        )
        self.assertEqual(
            [step["payload_kind"] for step in report["steps"]],
            ["continuation", "continuation", "continuation", "noop"],
        )
        self.assertIn("work-item: scope-map", report["steps"][0]["payload_text"])
        self.assertIn("work-item: integration-gap", report["steps"][1]["payload_text"])
        self.assertIn("work-item: integration-gap", report["steps"][2]["payload_text"])
        self.assertEqual([item["id"] for item in tasks], ["scope-map", "integration-gap"])
        self.assertEqual([item["status"] for item in tasks], ["completed", "stopped"])
        self.assertEqual(tasks[0]["focus_layer"], "macro")
        self.assertEqual(tasks[1]["focus_layer"], "meso")
        self.assertEqual(tasks[1]["attempt"], 1)
        self.assertEqual(approved_run["budget"]["remaining_steps"], 1)
        self.assertEqual(
            [event["event"] for event in events],
            [
                "continue_next_item",
                "consistency_decision_applied",
                "continue_next_item",
                "consistency_decision_applied",
                "continue_next_item",
                "consistency_decision_applied",
                "no_ready_item",
            ],
        )
        self.assertTrue(applied_decision_exists)
        self.assertEqual(report["final"]["statuses"], ["completed", "stopped"])
        self.assertEqual(report["final"]["remaining_steps"], 1)

    def test_runner_rebootstrap_discards_stale_runtime_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            first_summary = Path(tmp) / "first-summary.json"
            second_summary = Path(tmp) / "second-summary.json"

            first = self._run_runner(project, first_summary)
            second = self._run_runner(project, second_summary)
            report = json.loads(second_summary.read_text(encoding="utf-8"))
            events = _read_jsonl(project / ".autopilot" / "events.jsonl")

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(len(report["final"]["events"]), 7)
        self.assertEqual(len(events), 7)
        self.assertEqual(
            [event["event"] for event in events],
            [
                "continue_next_item",
                "consistency_decision_applied",
                "continue_next_item",
                "consistency_decision_applied",
                "continue_next_item",
                "consistency_decision_applied",
                "no_ready_item",
            ],
        )


if __name__ == "__main__":
    unittest.main()
