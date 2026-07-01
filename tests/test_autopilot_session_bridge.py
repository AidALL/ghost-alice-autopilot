"""TDD for session-intent ledger to autopilot run-state bootstrap.

Run: /opt/homebrew/bin/python3 -m pytest tests/test_autopilot_session_bridge.py -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BRIDGE_SCRIPT = REPO_ROOT / "scripts" / "autopilot_session_bridge.py"
SKILL_BRIDGE_SCRIPT = (
    REPO_ROOT
    / "addons"
    / "autopilot-mode"
    / "skill"
    / "scripts"
    / "autopilot_session_bridge.py"
)
ADAPTER_SCRIPT = REPO_ROOT / "addons" / "autopilot-mode" / "skill" / "adapters" / "autopilot_mode.py"
ADAPTER_DIR = REPO_ROOT / "addons" / "autopilot-mode" / "skill" / "adapters"
sys.path.insert(0, str(ADAPTER_DIR))

import autopilot_state as aps  # noqa: E402


def _write_current_session_ledger(
    root: Path,
    *,
    platform: str = "codex",
    session_id: str = "session-1",
    repeated_conduct_feedback: bool = True,
) -> Path:
    session_dir = root / platform / session_id
    session_dir.mkdir(parents=True)
    state_path = session_dir / "intent-state.json"
    events_path = session_dir / "intent-events.jsonl"

    state = {
            "schema_version": "session-intent-ledger.v1",
            "platform": platform,
            "session_id": session_id,
            "current_goal": "Make autopilot continue from the active session intent ledger.",
            "user_intent_summary": "Bridge session-intent JSON and JSONL progress into approved autopilot run state.",
            "acceptance_criteria": [
                {
                    "id": "bridge-state",
                    "summary": "Read current-session, intent-state, and intent-events before writing .autopilot state.",
                    "source": "user-explicit",
                },
            ],
        }
    if repeated_conduct_feedback:
        state["conduct_feedback"] = [
            {
                "id": "active-control",
                "status": "open",
                "occurrence_count": 2,
                "summary": "Use accumulated conduct feedback as active control input, not retrospective trivia.",
            },
        ]
    state_path.write_text(
        json.dumps(state),
        encoding="utf-8",
    )
    events = [
        {
            "event": "user-input-observed",
            "event_id": "event-1",
            "platform": platform,
            "session_id": session_id,
            "source": "hook",
            "input_digest": "sha256:" + ("1" * 64),
            "input_char_count": 50,
            "intake_status": "observed",
        },
        {
            "event": "intent-updated",
            "event_id": "event-2",
            "platform": platform,
            "session_id": session_id,
            "source": "agent",
            "correlation_id": "corr-bridge-1",
            "tool_stage": "PostToolUse",
            "metadata": {"receptor": "session-intent", "next_action": "continue"},
            "delta_keys": ["conduct_feedback", "acceptance_criteria"],
            "intent_delta_digest": "sha256:" + ("2" * 64),
            "intent_delta_status": "provided",
        },
    ]
    events_path.write_text(
        "\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n",
        encoding="utf-8",
    )
    (root / platform / "current-session.json").write_text(
        json.dumps({
            "schema_version": "session-intent-current.v1",
            "platform": platform,
            "session_id": session_id,
            "state_path": str(state_path),
        }),
        encoding="utf-8",
    )
    return state_path


class AutopilotSessionBridgeTest(unittest.TestCase):
    def test_bridge_and_adapter_share_session_intent_material_helpers(self):
        shared = (
            REPO_ROOT
            / "addons"
            / "autopilot-mode"
            / "skill"
            / "scripts"
            / "autopilot_session_material.py"
        )
        adapter = REPO_ROOT / "addons" / "autopilot-mode" / "skill" / "adapters" / "autopilot_state.py"
        bridge_text = BRIDGE_SCRIPT.read_text(encoding="utf-8")
        skill_bridge_text = SKILL_BRIDGE_SCRIPT.read_text(encoding="utf-8")
        adapter_text = adapter.read_text(encoding="utf-8")

        self.assertTrue(shared.is_file())
        self.assertTrue(SKILL_BRIDGE_SCRIPT.is_file())
        self.assertIn("autopilot_session_bridge.py", bridge_text)
        self.assertIn("autopilot_session_material.py", skill_bridge_text)
        self.assertIn("autopilot_session_material.py", adapter_text)
        duplicated_session_material_defs = (
            "def _run_summary(",
            "def _acceptance_criteria_from_intent(",
            "def _session_intent_task(",
            "def _build_approved_run(",
        )
        for marker in duplicated_session_material_defs:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, bridge_text)
                self.assertNotIn(marker, skill_bridge_text)
                self.assertNotIn(marker, adapter_text)

    def test_skill_local_bridge_script_runs_standalone(self):
        result = subprocess.run(
            [sys.executable, str(SKILL_BRIDGE_SCRIPT), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--intent-root", result.stdout)

    def test_bridge_creates_approved_run_from_current_session_and_adapter_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            intent_root = root / "session-intent"
            state_path = _write_current_session_ledger(intent_root)
            run_dir = root / ".autopilot"
            plan_path = ".tmp/implementation-plans/local-autopilot-semantic-e2e-test-plan.md"

            result = subprocess.run(
                [
                    sys.executable,
                    str(BRIDGE_SCRIPT),
                    "--intent-root",
                    str(intent_root),
                    "--platform",
                    "codex",
                    "--run-dir",
                    str(run_dir),
                    "--current-work-item-id",
                    "current",
                    "--plan-path",
                    plan_path,
                    "--approval-evidence-json",
                    '{"decision":"GO","source":"unit-test"}',
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["state_path"], str(state_path))
            self.assertEqual(summary["event_count"], 2)
            self.assertEqual(summary["latest_event"]["delta_keys"], ["conduct_feedback", "acceptance_criteria"])
            self.assertEqual(summary["latest_input_event"]["event"], "user-input-observed")
            self.assertEqual(summary["latest_intent_update_event"]["event"], "intent-updated")
            self.assertTrue((run_dir / "approved-run.json").is_file())
            self.assertTrue((run_dir / "conduct-plan.json").is_file())

            approved_run = json.loads((run_dir / "approved-run.json").read_text(encoding="utf-8"))
            session_intent = approved_run["approval_evidence"]["session_intent"]
            self.assertEqual(session_intent["state_path"], str(state_path))
            self.assertEqual(session_intent["latest_event"]["event"], "intent-updated")
            self.assertEqual(session_intent["latest_input_event"]["event_id"], "event-1")
            self.assertEqual(session_intent["latest_intent_update_event"]["event_id"], "event-2")
            self.assertEqual(session_intent["latest_intent_update_event"]["correlation_id"], "corr-bridge-1")
            self.assertEqual(session_intent["latest_intent_update_event"]["tool_stage"], "PostToolUse")
            self.assertEqual(
                session_intent["latest_intent_update_event"]["metadata"]["next_action"],
                "continue",
            )
            self.assertEqual(len(session_intent["recent_events"]), 2)

            env = os.environ.copy()
            env["GHOST_ALICE_AUTOPILOT_RUN_DIR"] = str(run_dir)
            adapter = subprocess.run(
                [sys.executable, str(ADAPTER_SCRIPT)],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            payload = json.loads(adapter.stdout)
            items = aps.read_work_items(run_dir / "tasks.jsonl")

            self.assertEqual(adapter.returncode, 0, adapter.stderr)
            self.assertTrue(payload["continue"])
            self.assertIn("[autopilot]", payload["systemMessage"])
            self.assertIn("work-item: conduct-active-control", payload["systemMessage"])
            self.assertEqual([item["id"] for item in items], ["conduct-active-control"])

    def test_bridge_creates_ready_task_when_current_session_has_no_repeated_conduct_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            intent_root = root / "session-intent"
            state_path = _write_current_session_ledger(intent_root, repeated_conduct_feedback=False)
            run_dir = root / ".autopilot"

            result = subprocess.run(
                [
                    sys.executable,
                    str(BRIDGE_SCRIPT),
                    "--intent-root",
                    str(intent_root),
                    "--platform",
                    "codex",
                    "--run-dir",
                    str(run_dir),
                    "--current-work-item-id",
                    "current",
                    "--plan-path",
                    ".tmp/implementation-plans/bridge.md",
                    "--approval-evidence-json",
                    '{"decision":"GO","source":"unit-test"}',
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["mode"], "session-intent-task")
            self.assertEqual(summary["state_path"], str(state_path))
            self.assertTrue((run_dir / "approved-run.json").is_file())
            self.assertTrue((run_dir / "tasks.jsonl").is_file())
            self.assertFalse((run_dir / "conduct-plan.json").exists())

            env = os.environ.copy()
            env["GHOST_ALICE_AUTOPILOT_RUN_DIR"] = str(run_dir)
            adapter = subprocess.run(
                [sys.executable, str(ADAPTER_SCRIPT)],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            payload = json.loads(adapter.stdout)

            self.assertEqual(adapter.returncode, 0, adapter.stderr)
            self.assertTrue(payload["continue"])
            self.assertIn("work-item: session-intent-session-1", payload["systemMessage"])

    def test_bridge_preserves_decisions_open_questions_and_source_locator_in_continuation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            intent_root = root / "session-intent"
            state_path = _write_current_session_ledger(intent_root, repeated_conduct_feedback=False)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["decisions"] = [
                {
                    "id": "run-in-place",
                    "summary": "Apply the approved cleanup in place rather than handing back a plan.",
                }
            ]
            state["open_questions"] = [
                {
                    "id": "release-scope",
                    "summary": "Full release is blocked until Linux and Windows compatibility evidence exists.",
                }
            ]
            state_path.write_text(json.dumps(state), encoding="utf-8")
            run_dir = root / ".autopilot"

            result = subprocess.run(
                [
                    sys.executable,
                    str(BRIDGE_SCRIPT),
                    "--intent-root",
                    str(intent_root),
                    "--platform",
                    "codex",
                    "--run-dir",
                    str(run_dir),
                    "--current-work-item-id",
                    "current",
                    "--plan-path",
                    ".tmp/implementation-plans/bridge.md",
                    "--approval-evidence-json",
                    '{"decision":"GO","source":"unit-test"}',
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            env = os.environ.copy()
            env["GHOST_ALICE_AUTOPILOT_RUN_DIR"] = str(run_dir)
            adapter = subprocess.run(
                [sys.executable, str(ADAPTER_SCRIPT)],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            payload = json.loads(adapter.stdout)
            items = aps.read_work_items(run_dir / "tasks.jsonl")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(adapter.returncode, 0, adapter.stderr)
        self.assertEqual(items[0]["source_locator"], f"{state_path}#intent-state")
        self.assertEqual(
            items[0]["decision_context"],
            ["run-in-place: Apply the approved cleanup in place rather than handing back a plan."],
        )
        self.assertEqual(
            items[0]["open_questions"],
            ["release-scope: Full release is blocked until Linux and Windows compatibility evidence exists."],
        )
        # The stored work-item source_locator (above) stays absolute (audit truth);
        # the emitted continuation SIGNAL is portablized: project-relative path,
        # forward slashes, no absolute base leak (platform-neutral handoff).
        self.assertIn(
            "session-intent/codex/session-1/intent-state.json#intent-state",
            payload["systemMessage"],
        )
        self.assertNotIn(str(run_dir.parent).replace("\\", "/"), payload["systemMessage"])
        self.assertIn("decision-context:", payload["systemMessage"])
        self.assertIn("run-in-place: Apply the approved cleanup", payload["systemMessage"])
        self.assertIn("open-questions:", payload["systemMessage"])
        self.assertIn("release-scope: Full release is blocked", payload["systemMessage"])

    def test_bridge_accepts_claude_platform_current_session_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            intent_root = root / "session-intent"
            state_path = _write_current_session_ledger(
                intent_root,
                platform="claude",
                session_id="claude-session-1",
                repeated_conduct_feedback=False,
            )
            run_dir = root / ".autopilot"

            result = subprocess.run(
                [
                    sys.executable,
                    str(BRIDGE_SCRIPT),
                    "--intent-root",
                    str(intent_root),
                    "--platform",
                    "claude",
                    "--run-dir",
                    str(run_dir),
                    "--current-work-item-id",
                    "current",
                    "--plan-path",
                    ".tmp/implementation-plans/bridge.md",
                    "--approval-evidence-json",
                    '{"decision":"GO","source":"unit-test"}',
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["mode"], "session-intent-task")
            self.assertEqual(summary["state_path"], str(state_path))
            approved_run = json.loads((run_dir / "approved-run.json").read_text(encoding="utf-8"))
            self.assertEqual(approved_run["approval_evidence"]["session_intent"]["platform"], "claude")
            self.assertEqual(
                approved_run["approval_evidence"]["session_intent"]["latest_event"]["delta_keys"],
                ["conduct_feedback", "acceptance_criteria"],
            )
            self.assertEqual(
                approved_run["approval_evidence"]["session_intent"]["latest_input_event"]["event"],
                "user-input-observed",
            )

    def test_bridge_rejects_schema_less_current_session_pointer_like_stop_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            intent_root = root / "session-intent"
            state_path = _write_current_session_ledger(intent_root)
            pointer_path = intent_root / "codex" / "current-session.json"
            pointer_path.write_text(
                json.dumps({
                    "platform": "codex",
                    "session_id": "session-1",
                    "state_path": str(state_path),
                }),
                encoding="utf-8",
            )
            run_dir = root / ".autopilot"

            result = subprocess.run(
                [
                    sys.executable,
                    str(BRIDGE_SCRIPT),
                    "--intent-root",
                    str(intent_root),
                    "--platform",
                    "codex",
                    "--run-dir",
                    str(run_dir),
                    "--current-work-item-id",
                    "current",
                    "--plan-path",
                    ".tmp/implementation-plans/bridge.md",
                    "--approval-evidence-json",
                    '{"decision":"GO","source":"unit-test"}',
                ],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("schema_version", result.stderr)
        self.assertFalse((run_dir / "approved-run.json").exists())

    def test_bridge_refuses_to_write_run_state_without_explicit_approval_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            intent_root = root / "session-intent"
            _write_current_session_ledger(intent_root)
            run_dir = root / ".autopilot"

            result = subprocess.run(
                [
                    sys.executable,
                    str(BRIDGE_SCRIPT),
                    "--intent-root",
                    str(intent_root),
                    "--platform",
                    "codex",
                    "--run-dir",
                    str(run_dir),
                    "--current-work-item-id",
                    "current",
                    "--plan-path",
                    ".tmp/implementation-plans/bridge.md",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((run_dir / "approved-run.json").exists())

    def test_bridge_refuses_negative_or_incomplete_approval_evidence(self):
        bad_evidence = [
            '{"decision":"NO","source":"unit-test"}',
            '{"source":"unit-test"}',
            '{"decision":"GO"}',
            '{}',
            '[]',
        ]
        for evidence in bad_evidence:
            with self.subTest(evidence=evidence), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                intent_root = root / "session-intent"
                _write_current_session_ledger(intent_root)
                run_dir = root / ".autopilot"

                result = subprocess.run(
                    [
                        sys.executable,
                        str(BRIDGE_SCRIPT),
                        "--intent-root",
                        str(intent_root),
                        "--platform",
                        "codex",
                        "--run-dir",
                        str(run_dir),
                        "--current-work-item-id",
                        "current",
                        "--plan-path",
                        ".tmp/implementation-plans/bridge.md",
                        "--approval-evidence-json",
                        evidence,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((run_dir / "approved-run.json").exists())


if __name__ == "__main__":
    unittest.main()
