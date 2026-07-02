"""TDD for Phase P6: official autopilot privileged-adapter addon.

Run: GHOST_ALICE_CORE_REPO=/path/to/ghost-alice /opt/homebrew/bin/python3 -m pytest tests/test_privileged_adapter.py -q
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ADAPTER_DIR = REPO_ROOT / "addons" / "autopilot-mode" / "skill" / "adapters"
sys.path.insert(0, str(ADAPTER_DIR))

import autopilot_state as aps  # noqa: E402


AUTOPILOT_SOURCE = REPO_ROOT
VALID_SIGNAL_DIGEST = "sha256:" + ("b" * 64)


def _candidate_core_repos() -> list[Path]:
    candidates: list[Path] = []
    env_core = os.environ.get("GHOST_ALICE_CORE_REPO")
    if env_core:
        candidates.append(Path(env_core))
    candidates.extend([
        REPO_ROOT.parent / "ghost-alice",
    ])
    return candidates


def _load_core_modules():
    for core_repo in _candidate_core_repos():
        shared = core_repo / "_shared"
        if (shared / "addon_installer.py").is_file() and (shared / "install_hooks.py").is_file():
            sys.path.insert(0, str(shared))
            import addon_installer as ai  # type: ignore[import-not-found]
            import install_hooks  # type: ignore[import-not-found]

            return ai, install_hooks
    raise unittest.SkipTest("Ghost-ALICE core checkout not available for privileged-adapter integration test")


def _work_item(item_id: str) -> dict:
    return {
        "id": item_id,
        "status": "ready",
        "focus_layer": "meso",
        "depends_on": [],
        "prompt": f"Do {item_id}",
        "acceptance_criteria": [f"{item_id}-ac"],
        "allowed_surface": ["_shared/..."],
        "completion": {
            "state": "not_started",
            "verdict": None,
            "evidence": [],
            "completion_check_digest": None,
            "reopen_target": None,
        },
        "attempt": 0,
    }


def _write_approved_run(run_dir: Path, items: list[dict]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "approved-run.json").write_text(
        json.dumps({
            "schema_version": "autopilot-run.v1",
            "run_id": "run-1",
            "approved": True,
            "status": "running",
            "scope": {"summary": "P6 autopilot adapter test run"},
            "budget": {"remaining_steps": 3},
            "allowed_surfaces": ["_shared/..."],
            "stop_conditions": ["budget_exhausted", "user_stop"],
            "approval_evidence": {"decision": "GO", "source": "unit-test"},
        }),
        encoding="utf-8",
    )
    aps.write_work_items(run_dir / "tasks.jsonl", items)


def _write_approved_run_config(run_dir: Path, *, allowed_surfaces: list[str]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "approved-run.json").write_text(
        json.dumps({
            "schema_version": "autopilot-run.v1",
            "run_id": "run-1",
            "approved": True,
            "status": "running",
            "scope": {"summary": "P6 autopilot adapter conduct plan test run"},
            "budget": {"remaining_steps": 3},
            "allowed_surfaces": allowed_surfaces,
            "stop_conditions": ["budget_exhausted", "user_stop"],
            "approval_evidence": {"decision": "GO", "source": "unit-test"},
        }),
        encoding="utf-8",
    )


def _conduct_plan() -> dict:
    return {
        "schema_version": "autopilot-conduct-plan.v2",
        "promotion_state": "approved",
        "source_candidate_id": "conduct-plan-candidate-test",
        "evidence_digest": VALID_SIGNAL_DIGEST,
        "approval_evidence": {"decision": "GO", "source": "unit-test"},
        "source": "skill-evolution/conduct_feedback",
        "proposed_queue_items": [
            {
                "id": "proposal-conduct-scope-drift",
                "proposal_status": "proposed",
                "approval_required": True,
                "approval_transition": {
                    "status_on_approval": "ready",
                    "copy_task_template": True,
                },
                "task_template": {
                    "id": "conduct-scope-drift",
                    "depends_on": [],
                    "focus_layer": "meta",
                    "prompt": "Investigate and propose a fix for repeated scope drift.",
                    "acceptance_criteria": ["bind drift to conduct feedback"],
                    "allowed_surface": ["skill-evolution/..."],
                },
                "observer_agent_required": True,
                "observer_contract": {
                    "mode": "read_only",
                    "purpose": "watch main-process logical consistency",
                    "prohibited_actions": ["modify files"],
                },
                "source_recommendation_id": "scope-drift",
            },
        ],
    }


class OfficialAutopilotAddonTest(unittest.TestCase):
    def test_official_autopilot_addon_resolves_privileged_adapter_from_core_data(self):
        ai, _install_hooks = _load_core_modules()
        targets = ai.load_addon_targets([AUTOPILOT_SOURCE], platform="claude")

        self.assertEqual(len(targets), 1)
        target = targets[0]
        self.assertEqual(target.addon_id, "autopilot-mode")
        self.assertEqual(target.name, "autopilot-mode")
        self.assertEqual(target.privileged_adapters, ("autopilot-mode",))

        specs = ai.iter_privileged_adapter_hook_specs(targets)
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]["event"], "on_agent_stop")
        self.assertEqual(specs[0]["marker"], "[adapter:autopilot-mode] continue")
        self.assertEqual(specs[0]["runner_id"], "adapter-autopilot-mode-continue")
        self.assertTrue(Path(specs[0]["script"]).is_file())

    def test_official_autopilot_adapter_hook_installs_and_full_uninstall_removes_it(self):
        _ai, install_hooks = _load_core_modules()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude = root / ".claude"
            claude.mkdir(parents=True)
            old_env = {key: os.environ.get(key) for key in ("HOME", "CLAUDE_CONFIG_DIR")}
            try:
                os.environ["HOME"] = str(root)
                os.environ["CLAUDE_CONFIG_DIR"] = str(claude)

                self.assertEqual(
                    install_hooks.install_hook("claude", addon_sources=[str(AUTOPILOT_SOURCE)]),
                    "installed",
                )
                installed = self._commands(claude)
                self.assertTrue(any("[adapter:autopilot-mode] continue" in c for c in installed))
                self.assertTrue(any("[hook-runner:adapter-autopilot-mode-continue]" in c for c in installed))

                install_hooks.uninstall_hook("claude")

                removed = self._commands(claude)
                self.assertFalse(any("[adapter:autopilot-mode] continue" in c for c in removed))
            finally:
                for key, value in old_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def _commands(self, claude: Path) -> list[str]:
        settings = json.loads((claude / "settings.json").read_text(encoding="utf-8"))
        return [
            hook.get("command", "")
            for event in settings.get("hooks", {}).values()
            if isinstance(event, list)
            for entry in event
            for hook in entry.get("hooks", [])
        ]

    def test_adapter_script_consumes_approved_run_dir_without_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            _write_approved_run(run_dir, [_work_item("next")])
            script = (
                AUTOPILOT_SOURCE
                / "addons"
                / "autopilot-mode"
                / "skill"
                / "adapters"
                / "autopilot_mode.py"
            )
            env = os.environ.copy()
            env["GHOST_ALICE_AUTOPILOT_RUN_DIR"] = str(run_dir)

            result = subprocess.run(
                [sys.executable, str(script)],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            payload = json.loads(result.stdout)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(payload["continue"])
        self.assertIn("work-item: next", payload["systemMessage"])
        self.assertIn("Do next", payload["systemMessage"])

    def test_adapter_script_surfaces_state_errors_instead_of_silent_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            running = _work_item("next")
            running["status"] = "running"
            _write_approved_run(run_dir, [running])
            (run_dir / "events.jsonl").write_text("{not-json\n", encoding="utf-8")
            script = (
                AUTOPILOT_SOURCE
                / "addons"
                / "autopilot-mode"
                / "skill"
                / "adapters"
                / "autopilot_mode.py"
            )
            env = os.environ.copy()
            env["GHOST_ALICE_AUTOPILOT_RUN_DIR"] = str(run_dir)

            result = subprocess.run(
                [sys.executable, str(script)],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            payload = json.loads(result.stdout)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(payload["continue"])
        self.assertIn("autopilot-mode adapter error", payload["systemMessage"])
        self.assertIn("invalid JSON", payload["systemMessage"])
        self.assertNotEqual(payload, {"continue": True, "systemMessage": ""})

    def test_adapter_script_imports_conduct_plan_before_no_ready_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            _write_approved_run_config(run_dir, allowed_surfaces=["skill-evolution/..."])
            (run_dir / "conduct-plan.json").write_text(json.dumps(_conduct_plan()), encoding="utf-8")
            script = (
                AUTOPILOT_SOURCE
                / "addons"
                / "autopilot-mode"
                / "skill"
                / "adapters"
                / "autopilot_mode.py"
            )
            env = os.environ.copy()
            env["GHOST_ALICE_AUTOPILOT_RUN_DIR"] = str(run_dir)

            result = subprocess.run(
                [sys.executable, str(script)],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            payload = json.loads(result.stdout)
            items = aps.read_work_items(run_dir / "tasks.jsonl")
            applied_plan_exists = (run_dir / "conduct-plan.applied.json").is_file()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(payload["continue"])
        self.assertIn("work-item: conduct-scope-drift", payload["systemMessage"])
        self.assertIn("observer-agent: required", payload["systemMessage"])
        self.assertEqual(items[0]["status"], "running")
        self.assertTrue(applied_plan_exists)

    def test_governance_signal_promoted_decision_reopens_running_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            intent_state = root / "intent-state.json"
            _write_approved_run(run_dir, [_work_item("current")])
            items = aps.read_work_items(run_dir / "tasks.jsonl")
            items[0]["status"] = "running"
            aps.write_work_items(run_dir / "tasks.jsonl", items)
            intent_state.write_text(
                json.dumps({
                    "schema_version": "session-intent-ledger.v1",
                    "conduct_feedback": [
                        {
                            "id": "scope-drift",
                            "status": "open",
                            "summary": "agent drifted from approved implementation scope",
                            "corrective_rule": "reopen macro focus before continuing",
                            "occurrence_count": 2,
                        }
                    ],
                }),
                encoding="utf-8",
            )
            governance_script = (
                AUTOPILOT_SOURCE
                / "addons"
                / "autopilot-mode"
                / "skill"
                / "scripts"
                / "autopilot_governance_signal.py"
            )
            adapter_script = (
                AUTOPILOT_SOURCE
                / "addons"
                / "autopilot-mode"
                / "skill"
                / "adapters"
                / "autopilot_mode.py"
            )
            candidate_result = subprocess.run(
                [
                    sys.executable,
                    str(governance_script),
                    "decision-candidate",
                    "--work-item-id",
                    "current",
                    "--intent-state",
                    str(intent_state),
                    "--out",
                    str(run_dir / "consistency-decision.candidate.json"),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            promote_result = subprocess.run(
                [
                    sys.executable,
                    str(governance_script),
                    "promote-decision",
                    "--candidate",
                    str(run_dir / "consistency-decision.candidate.json"),
                    "--out",
                    str(run_dir / "consistency-decision.json"),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            env = os.environ.copy()
            env["GHOST_ALICE_AUTOPILOT_RUN_DIR"] = str(run_dir)
            adapter_result = subprocess.run(
                [sys.executable, str(adapter_script)],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            payload = json.loads(adapter_result.stdout)
            items = aps.read_work_items(run_dir / "tasks.jsonl")

        self.assertEqual(candidate_result.returncode, 0, candidate_result.stderr)
        self.assertEqual(promote_result.returncode, 0, promote_result.stderr)
        self.assertEqual(adapter_result.returncode, 0, adapter_result.stderr)
        self.assertTrue(payload["continue"])
        self.assertIn("work-item: current", payload["systemMessage"])
        self.assertIn("reopen-target: macro", payload["systemMessage"])
        self.assertEqual(items[0]["status"], "running")
        self.assertEqual(items[0]["completion"]["state"], "reopened")
        self.assertEqual(items[0]["completion"]["reopen_target"], "macro")

    def test_adapter_script_skips_existing_conduct_plan_task_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            _write_approved_run_config(run_dir, allowed_surfaces=["skill-evolution/..."])
            existing = _work_item("conduct-scope-drift")
            existing["status"] = "running"
            aps.write_work_items(run_dir / "tasks.jsonl", [existing])
            (run_dir / "conduct-plan.json").write_text(json.dumps(_conduct_plan()), encoding="utf-8")
            script = (
                AUTOPILOT_SOURCE
                / "addons"
                / "autopilot-mode"
                / "skill"
                / "adapters"
                / "autopilot_mode.py"
            )
            env = os.environ.copy()
            env["GHOST_ALICE_AUTOPILOT_RUN_DIR"] = str(run_dir)

            result = subprocess.run(
                [sys.executable, str(script)],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            payload = json.loads(result.stdout)
            items = aps.read_work_items(run_dir / "tasks.jsonl")
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            applied_plan_exists = (run_dir / "conduct-plan.applied.json").is_file()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload, {"continue": True, "systemMessage": ""})
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "conduct-scope-drift")
        self.assertEqual(items[0]["status"], "running")
        self.assertNotIn("source_proposal_id", items[0])
        self.assertTrue(applied_plan_exists)
        self.assertEqual([event["event"] for event in events], ["conduct_plan_imported", "no_ready_item"])
        self.assertEqual(events[0]["imported_work_item_ids"], [])

    def test_adapter_script_defaults_to_project_autopilot_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            run_dir = project / ".autopilot"
            _write_approved_run(run_dir, [_work_item("next")])
            script = (
                AUTOPILOT_SOURCE
                / "addons"
                / "autopilot-mode"
                / "skill"
                / "adapters"
                / "autopilot_mode.py"
            )
            env = os.environ.copy()
            env.pop("GHOST_ALICE_AUTOPILOT_RUN_DIR", None)

            result = subprocess.run(
                [sys.executable, str(script)],
                cwd=project,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            payload = json.loads(result.stdout)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(payload["continue"])
        self.assertIn("work-item: next", payload["systemMessage"])

    def test_adapter_script_rejects_arguments(self):
        script = (
            AUTOPILOT_SOURCE
            / "addons"
            / "autopilot-mode"
            / "skill"
            / "adapters"
            / "autopilot_mode.py"
        )

        result = subprocess.run(
            [sys.executable, str(script), "--unexpected"],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 64)
        self.assertIn("accepts no arguments", result.stderr)

    def test_adapter_script_does_not_write_bytecode_into_installed_skill_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            installed_skill = root / "skills" / "autopilot-mode"
            shutil.copytree(
                AUTOPILOT_SOURCE / "addons" / "autopilot-mode" / "skill",
                installed_skill,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            run_dir = root / "run"
            _write_approved_run(run_dir, [_work_item("next")])
            script = installed_skill / "adapters" / "autopilot_mode.py"
            env = os.environ.copy()
            env.pop("PYTHONDONTWRITEBYTECODE", None)
            env["GHOST_ALICE_AUTOPILOT_RUN_DIR"] = str(run_dir)

            result = subprocess.run(
                [sys.executable, str(script)],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((installed_skill / "adapters" / "__pycache__").exists())


if __name__ == "__main__":
    unittest.main()
