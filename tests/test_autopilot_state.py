"""TDD for Phase P6: durable autopilot approved-run work-item state.

Run: /opt/homebrew/bin/python3 -m pytest tests/test_autopilot_state.py -q
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ADAPTER_DIR = REPO_ROOT / "addons" / "autopilot-mode" / "skill" / "adapters"
sys.path.insert(0, str(ADAPTER_DIR))

import autopilot_state as aps  # noqa: E402

VALID_COMPLETION_DIGEST = "sha256:" + ("a" * 64)
VALID_SIGNAL_DIGEST = "sha256:" + ("b" * 64)
VALID_STATE_DIGEST = "sha256:" + ("c" * 64)
VALID_DECISION_DIGEST = "sha256:" + ("d" * 64)
VALID_LOOP_DIGEST = "sha256:" + ("e" * 64)
VALID_COMPLETION_EVIDENCE = [
    "\n".join([
        "[completion-check]",
        "- acceptance-criteria:",
        "  - AC-TEST: current item satisfies the test criterion [source: user-explicit]",
        "- claim-evidence-map:",
        "  - claim: current item completed",
        "    criterion: AC-TEST",
        "    evidence: tests/test_autopilot_state.py pass",
        "    verdict: pass",
        "- unverified:",
        "  - none",
    ])
]
VALID_PROMOTION_EVIDENCE = {"decision": "PROMOTE", "source": "unit-test"}


def _locate_core_ledger_source() -> Path | None:
    candidates = []
    env_root = os.environ.get("GHOST_ALICE_CORE_ROOT")
    if env_root:
        candidates.append(
            Path(env_root) / "session-intent-analyzer" / "scripts" / "session_intent_ledger.py"
        )
    candidates.append(
        REPO_ROOT.parent / "ghost-alice" / "session-intent-analyzer" / "scripts" / "session_intent_ledger.py"
    )
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            source = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        # Only a core that exposes the met-writer can drive the import-by-path
        # flip; otherwise the integration test would assert against an older
        # core that gracefully skips (e.g. a CI sibling on an unpublished API).
        if "def mark_acceptance_criterion_met" in source:
            return candidate
    return None


def _item(item_id: str, *, status: str = "ready", depends_on: list[str] | None = None) -> dict:
    return {
        "id": item_id,
        "status": status,
        "focus_layer": "meso",
        "depends_on": depends_on or [],
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


def _approved_run_record(
    *,
    approved: bool = True,
    status: str = "running",
    remaining_steps: int = 3,
) -> dict:
    return {
        "schema_version": "autopilot-run.v1",
        "run_id": "run-1",
        "approved": approved,
        "status": status,
        "scope": {"summary": "P6 autopilot test run"},
        "budget": {"remaining_steps": remaining_steps},
        "allowed_surfaces": ["_shared/..."],
        "stop_conditions": ["budget_exhausted", "user_stop"],
        "approval_evidence": {"decision": "GO", "source": "unit-test"},
    }


def _write_run(run_dir: Path, items: list[dict], *, decision: dict | None = None) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "approved-run.json").write_text(
        json.dumps(_approved_run_record()),
        encoding="utf-8",
    )
    aps.write_work_items(run_dir / "tasks.jsonl", items)
    if decision is not None:
        (run_dir / "consistency-decision.json").write_text(json.dumps(decision), encoding="utf-8")


def _write_run_config(
    run_dir: Path,
    *,
    approved: bool = True,
    status: str = "running",
    remaining_steps: int = 3,
) -> None:
    (run_dir / "approved-run.json").write_text(
        json.dumps(_approved_run_record(
            approved=approved,
            status=status,
            remaining_steps=remaining_steps,
        )),
        encoding="utf-8",
    )


def _write_io_trace(
    home: Path,
    session_id: str,
    *,
    command: str = "pytest tests/test_autopilot_state.py",
    path_value: str = "n/a",
    op: str | None = None,
) -> Path:
    path = home / ".ghost-alice" / "io-trace.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": "fixture-timestamp",
        "session": session_id,
        "tool": "Bash",
        "path": path_value,
        "pattern": command,
    }
    if op:
        row["op"] = op
    path.write_text(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    return path


def _write_session_intent_run_source(
    root: Path,
    *,
    session_id: str = "session-1",
    decision_approval: bool = False,
    repeated_conduct_feedback: bool = False,
    rich_event_metadata: bool = False,
    criteria: list[dict] | None = None,
) -> Path:
    session_dir = root / "codex" / session_id
    session_dir.mkdir(parents=True)
    state_path = session_dir / "intent-state.json"
    state = {
        "schema_version": "session-intent-ledger.v1",
        "platform": "codex",
        "session_id": session_id,
        "current_goal": "Use the current correction flow as autopilot work material.",
        "user_intent_summary": "Bridge session-intent JSON and JSONL into project-local autopilot continuation.",
        "acceptance_criteria": criteria if criteria is not None else [
            {
                "id": "AC-STOP-BRIDGE",
                "summary": "Stop adapter bootstraps .autopilot before returning a continuation payload.",
                "source": "user-explicit",
                "status": "unmet",
                "admitted": True,
            }
        ],
        "decisions": [],
        "conduct_feedback": [],
    }
    if decision_approval:
        state["decisions"].append({
            "id": "autopilot-run-approval",
            "kind": "autopilot_run_approval",
            "decision": "GO",
            "source": "unit-test",
            "summary": "Explicitly approve the current session-intent scope for autopilot.",
        })
    if repeated_conduct_feedback:
        state["conduct_feedback"].append({
            "id": "stop-event-noop",
            "status": "open",
            "summary": "Stop hook no-ops when .autopilot is missing.",
            "corrective_rule": "Bootstrap approved project run-state from current session-intent before returning no-op.",
            "occurrence_count": 2,
        })
    state_path.write_text(json.dumps(state), encoding="utf-8")
    events = [
        {
            "event": "user-input-observed",
            "event_id": "evt-stop-1",
            "platform": "codex",
            "session_id": session_id,
            "source": "hook",
            "input_digest": "sha256:" + ("1" * 64),
            "input_char_count": 40,
            "intake_status": "observed",
        },
        {
            "event": "intent-updated",
            "event_id": "evt-stop-2",
            "platform": "codex",
            "session_id": session_id,
            "source": "agent",
            "delta_keys": ["current_goal", "conduct_feedback", "decisions"],
            "intent_delta_digest": "sha256:" + ("2" * 64),
            "intent_delta_status": "recorded",
        },
    ]
    if rich_event_metadata:
        events[-1].update({
            "correlation_id": "corr-session-1",
            "tool_stage": "PostToolUse",
            "metadata": {"receptor": "io-trace", "next_action": "continue"},
        })
    (session_dir / "intent-events.jsonl").write_text(
        "\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n",
        encoding="utf-8",
    )
    (root / "codex" / "current-session.json").write_text(
        json.dumps({
            "schema_version": "session-intent-current.v1",
            "platform": "codex",
            "session_id": session_id,
            "state_path": str(state_path),
        }),
        encoding="utf-8",
    )
    return state_path


def _decision_action(
    work_item_id: str,
    decision: str,
    *,
    evidence: list[str] | None = None,
    completion_check_digest: str | None = None,
    verdict: str | None = None,
) -> dict:
    payload = {
        "schema_version": "autopilot-consistency-decision.v1",
        "decision_id": f"d-{decision}",
        "work_item_id": work_item_id,
        "decision": decision,
        "promotion_state": "promoted",
        "promotion_evidence": VALID_PROMOTION_EVIDENCE,
        "candidate_id": f"candidate-{decision}",
        "governance_signal_digest": VALID_SIGNAL_DIGEST,
        "decision_key": VALID_DECISION_DIGEST,
        "state_hash": VALID_STATE_DIGEST,
        "loop_key": VALID_LOOP_DIGEST,
        "evidence": evidence or [f"{decision}:unit-test evidence"],
    }
    if completion_check_digest is not None:
        payload["completion_check_digest"] = completion_check_digest
    if verdict is not None:
        payload["verdict"] = verdict
    return payload


def _conduct_plan(task_id: str = "conduct-scope-drift") -> dict:
    return {
        "schema_version": "autopilot-conduct-plan.v2",
        "promotion_state": "approved",
        "source_candidate_id": "conduct-plan-candidate-test",
        "evidence_digest": VALID_SIGNAL_DIGEST,
        "approval_evidence": {"decision": "GO", "source": "unit-test"},
        "source": "skill-evolution/conduct_feedback",
        "proposed_queue_items": [
            {
                "id": f"proposal-{task_id}",
                "proposal_status": "proposed",
                "approval_required": True,
                "approval_transition": {
                    "status_on_approval": "ready",
                    "copy_task_template": True,
                },
                "task_template": {
                    "id": task_id,
                    "depends_on": [],
                    "focus_layer": "meta",
                    "prompt": "Investigate and propose a fix for repeated scope drift.",
                    "acceptance_criteria": [
                        "Bind drift to conduct_feedback evidence.",
                        "Attach a read-only observer before implementation.",
                    ],
                    "allowed_surface": ["skill-evolution/..."],
                },
                "observer_agent_required": True,
                "observer_contract": {
                    "mode": "read_only",
                    "purpose": "watch main-process logical consistency",
                    "prohibited_actions": ["modify files", "mark proposed tasks ready"],
                },
                "source": "conduct_feedback",
                "source_recommendation_id": "scope-drift",
            },
        ],
    }


class AutopilotStateTest(unittest.TestCase):
    def test_work_item_domain_logic_is_split_from_stop_adapter_facade(self):
        adapter_source = (ADAPTER_DIR / "autopilot_state.py").read_text(encoding="utf-8")
        work_items_path = ADAPTER_DIR / "autopilot_work_items.py"
        messages_path = ADAPTER_DIR / "autopilot_messages.py"

        self.assertTrue(work_items_path.is_file())
        self.assertTrue(messages_path.is_file())
        work_items_source = work_items_path.read_text(encoding="utf-8")
        messages_source = messages_path.read_text(encoding="utf-8")
        for function_name in (
            "validate_work_items",
            "read_work_items",
            "write_work_items",
            "derive_ready_queue",
            "apply_consistency_decision",
            "rewrite_open_work_items",
            "apply_conduct_plan_proposals",
        ):
            self.assertNotIn(f"def {function_name}", adapter_source)
            self.assertIn(f"def {function_name}", work_items_source)
        for function_name in (
            "format_io_trace_rows",
            "compact_governance_candidate",
            "build_continuation_message",
            "build_meta_intervention_message",
        ):
            self.assertNotIn(f"def _{function_name}", adapter_source)
            self.assertIn(f"def {function_name}", messages_source)
        # Facade stays thin: domain builders/validators live in the sibling
        # modules asserted above. The ceiling was raised from 1050 to 1200 when
        # the intent-driven resume-budget helpers were added next to the existing
        # resume-count helpers (event-log state logic that belongs with the Stop
        # adapter's own resume accounting, not in work_items/messages).
        self.assertLess(len(adapter_source.splitlines()), 1200)

    def test_tasks_jsonl_preserves_completed_items_and_derives_ready_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tasks.jsonl"
            items = [_item("a", status="completed"), _item("b", depends_on=["a"])]
            aps.write_work_items(path, items)

            loaded = aps.read_work_items(path)
            ready = aps.derive_ready_queue(loaded)

        self.assertEqual([item["id"] for item in loaded], ["a", "b"])
        self.assertEqual(ready, ["b"])

    def test_continue_next_requires_completion_check_evidence(self):
        items = [_item("a", status="running")]

        with self.assertRaises(aps.AutopilotStateError):
            aps.apply_consistency_decision(items, "a", "continue_next")

        with self.assertRaises(aps.AutopilotStateError):
            aps.apply_consistency_decision(
                items,
                "a",
                "continue_next",
                completion_check_digest="sha256:abc",
                verdict="pass",
                evidence=VALID_COMPLETION_EVIDENCE,
            )

        with self.assertRaises(aps.AutopilotStateError):
            aps.apply_consistency_decision(
                items,
                "a",
                "continue_next",
                completion_check_digest=VALID_COMPLETION_DIGEST,
                verdict="pass",
                evidence=["pytest tests/test_autopilot_state.py"],
            )

        updated = aps.apply_consistency_decision(
            items,
            "a",
            "continue_next",
            completion_check_digest=VALID_COMPLETION_DIGEST,
            verdict="pass",
            evidence=VALID_COMPLETION_EVIDENCE,
        )

        self.assertEqual(updated[0]["status"], "completed")
        self.assertEqual(updated[0]["completion"]["state"], "completed")
        self.assertEqual(updated[0]["completion"]["completion_check_digest"], VALID_COMPLETION_DIGEST)

    def test_consistency_decision_rejects_non_running_targets(self):
        cases = [
            (
                "ready",
                "continue_next",
                {
                    "completion_check_digest": VALID_COMPLETION_DIGEST,
                    "verdict": "pass",
                    "evidence": VALID_COMPLETION_EVIDENCE,
                },
            ),
            ("completed", "retry_same_unit", {}),
            ("not_applicable", "reopen_macro", {}),
        ]

        for status, decision, kwargs in cases:
            with self.subTest(status=status, decision=decision):
                items = [_item("a", status=status)]

                with self.assertRaises(aps.AutopilotStateError) as ctx:
                    aps.apply_consistency_decision(items, "a", decision, **kwargs)

                message = str(ctx.exception).lower()
                self.assertIn("consistency decision state transition", message)
                self.assertIn("requires running work item", message)
                self.assertEqual(items[0]["status"], status)

    def test_retry_same_unit_keeps_item_and_increments_attempt(self):
        items = [_item("a", status="running")]

        updated = aps.apply_consistency_decision(items, "a", "retry_same_unit", evidence=["failed test"])

        self.assertEqual([item["id"] for item in updated], ["a"])
        self.assertEqual(updated[0]["status"], "ready")
        self.assertEqual(updated[0]["attempt"], 1)
        self.assertEqual(updated[0]["completion"]["state"], "retry")

    def test_reopen_decision_marks_item_without_popping_it(self):
        items = [_item("a", status="running")]

        updated = aps.apply_consistency_decision(items, "a", "reopen_macro", evidence=["macro drift"])

        self.assertEqual([item["id"] for item in updated], ["a"])
        self.assertEqual(updated[0]["status"], "reopened")
        self.assertEqual(updated[0]["completion"]["state"], "reopened")
        self.assertEqual(updated[0]["completion"]["reopen_target"], "macro")

    def test_replan_rewrites_open_items_but_preserves_terminal_history(self):
        items = [
            _item("done", status="completed"),
            _item("skip", status="not_applicable"),
            _item("old-ready", status="ready"),
            _item("old-blocked", status="blocked"),
        ]
        replacement = [_item("new-first"), _item("new-second", depends_on=["new-first"])]

        updated = aps.rewrite_open_work_items(items, replacement)

        self.assertEqual([item["id"] for item in updated], ["done", "skip", "new-first", "new-second"])
        self.assertEqual(aps.derive_ready_queue(updated), ["new-first"])

    def test_replan_allows_new_open_items_to_depend_on_preserved_terminal_history(self):
        items = [_item("done", status="completed"), _item("old-ready")]
        replacement = [_item("new-ready", depends_on=["done"])]

        updated = aps.rewrite_open_work_items(items, replacement)

        self.assertEqual([item["id"] for item in updated], ["done", "new-ready"])
        self.assertEqual(aps.derive_ready_queue(updated), ["new-ready"])

    def test_conduct_plan_promotes_proposed_queue_items_to_ready_tasks(self):
        updated = aps.apply_conduct_plan_proposals([], _conduct_plan())

        self.assertEqual([item["id"] for item in updated], ["conduct-scope-drift"])
        self.assertEqual(updated[0]["status"], "ready")
        self.assertEqual(updated[0]["focus_layer"], "meta")
        self.assertEqual(updated[0]["allowed_surface"], ["skill-evolution/..."])
        self.assertIs(updated[0]["observer_agent_required"], True)
        self.assertEqual(updated[0]["observer_contract"]["mode"], "read_only")
        self.assertEqual(updated[0]["source_proposal_id"], "proposal-conduct-scope-drift")

    def test_conduct_plan_import_skips_existing_task_ids(self):
        existing = [_item("conduct-scope-drift", status="running")]

        updated = aps.apply_conduct_plan_proposals(existing, _conduct_plan())

        self.assertEqual([item["id"] for item in updated], ["conduct-scope-drift"])
        self.assertEqual(updated[0]["status"], "running")

    def test_conduct_plan_import_requires_approval_evidence(self):
        plan = _conduct_plan()
        plan.pop("approval_evidence")

        with self.assertRaises(aps.AutopilotStateError) as ctx:
            aps.apply_conduct_plan_proposals([], plan)

        self.assertIn("approval_evidence", str(ctx.exception))

    def test_adapter_payload_without_run_dir_is_noop(self):
        self.assertEqual(aps.adapter_payload_from_env({}), {"continue": True, "systemMessage": ""})

    def test_adapter_bootstraps_project_run_from_session_intent_with_approval_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            intent_root = root / "session-intent"
            state_path = _write_session_intent_run_source(
                intent_root,
                repeated_conduct_feedback=True,
            )

            payload = aps.adapter_payload_from_env({
                "PWD": str(project),
                "GHOST_ALICE_PLATFORM": "codex",
                "GHOST_ALICE_SESSION_INTENT_ROOT": str(intent_root),
                "GHOST_ALICE_AUTOPILOT_APPROVAL_EVIDENCE_JSON": '{"decision":"GO","source":"unit-test"}',
                "GHOST_ALICE_AUTOPILOT_PLAN_PATH": ".tmp/implementation-plans/stop-bridge.md",
                "GHOST_ALICE_AUTOPILOT_CURRENT_WORK_ITEM_ID": "current",
            })
            run_dir = project / ".autopilot"
            approved_run = json.loads((run_dir / "approved-run.json").read_text(encoding="utf-8"))
            items = aps.read_work_items(run_dir / "tasks.jsonl")
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertTrue(payload["continue"])
        self.assertIn("work-item: conduct-stop-event-noop", payload["systemMessage"])
        self.assertIn("observer-agent: required", payload["systemMessage"])
        self.assertEqual(approved_run["approval_evidence"]["session_intent"]["state_path"], str(state_path))
        self.assertEqual(items[0]["status"], "running")
        self.assertEqual([event["event"] for event in events], ["session_intent_bootstrapped", "conduct_plan_imported", "continue_next_item"])

    def test_adapter_bootstrap_preserves_session_event_metadata_for_next_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            intent_root = root / "session-intent"
            _write_session_intent_run_source(
                intent_root,
                decision_approval=True,
                rich_event_metadata=True,
            )

            aps.adapter_payload_from_env({
                "PWD": str(project),
                "GHOST_ALICE_PLATFORM": "codex",
                "GHOST_ALICE_SESSION_INTENT_ROOT": str(intent_root),
                "GHOST_ALICE_AUTOPILOT_PLAN_PATH": ".tmp/implementation-plans/stop-bridge.md",
            })
            run_dir = project / ".autopilot"
            approved_run = json.loads((run_dir / "approved-run.json").read_text(encoding="utf-8"))
            latest_update = approved_run["approval_evidence"]["session_intent"]["latest_intent_update_event"]

        self.assertEqual(latest_update["correlation_id"], "corr-session-1")
        self.assertEqual(latest_update["tool_stage"], "PostToolUse")
        self.assertEqual(latest_update["metadata"]["receptor"], "io-trace")

    def test_adapter_bootstraps_project_run_from_session_intent_decision_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            intent_root = root / "session-intent"
            _write_session_intent_run_source(intent_root, decision_approval=True)

            payload = aps.adapter_payload_from_env({
                "PWD": str(project),
                "GHOST_ALICE_PLATFORM": "codex",
                "GHOST_ALICE_SESSION_INTENT_ROOT": str(intent_root),
                "GHOST_ALICE_AUTOPILOT_PLAN_PATH": ".tmp/implementation-plans/stop-bridge.md",
            })
            run_dir = project / ".autopilot"
            approved_run_exists = (run_dir / "approved-run.json").is_file()
            items = aps.read_work_items(run_dir / "tasks.jsonl")

        self.assertTrue(payload["continue"])
        self.assertTrue(approved_run_exists)
        self.assertIn("work-item: session-intent-session-1", payload["systemMessage"])
        self.assertEqual(items[0]["status"], "running")

    def test_adapter_skips_unapproved_local_intent_pointer_for_approved_sibling_ghost_alice_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "ghost-alice-autopilot"
            project.mkdir()
            local_intent_root = project / ".tmp" / "session-intent"
            _write_session_intent_run_source(
                local_intent_root,
                session_id="stale-local",
                criteria=[{"id": "AC1", "summary": "already done", "source": "user-explicit", "status": "met", "admitted": True}],
            )
            sibling_intent_root = root / "ghost-alice" / ".tmp" / "session-intent"
            _write_session_intent_run_source(sibling_intent_root, decision_approval=True)

            payload = aps.adapter_payload_from_env({
                "PWD": str(project),
                "GHOST_ALICE_AUTOPILOT_PLAN_PATH": ".tmp/implementation-plans/stop-bridge.md",
            })
            run_dir = project / ".autopilot"
            approved_run = json.loads((run_dir / "approved-run.json").read_text(encoding="utf-8"))

        self.assertTrue(payload["continue"])
        self.assertIn("work-item: session-intent-session-1", payload["systemMessage"])
        self.assertIn(
            "/ghost-alice/.tmp/session-intent/",
            approved_run["approval_evidence"]["session_intent"]["state_path"].replace("\\", "/"),
        )

    def test_adapter_does_not_bootstrap_from_iotrace_when_no_admitted_unmet_criterion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            intent_root = root / "session-intent"
            # io-trace activity is present, but the only criterion is already met:
            # io-trace alone must never bootstrap an autopilot run (the design-error guard).
            _write_session_intent_run_source(
                intent_root,
                criteria=[{"id": "AC1", "summary": "already done", "source": "user-explicit", "status": "met", "admitted": True}],
            )
            _write_io_trace(root, "session-1", command="apply_patch autopilot_state.py")

            payload = aps.adapter_payload_from_env({
                "HOME": str(root),
                "PWD": str(project),
                "GHOST_ALICE_PLATFORM": "codex",
                "GHOST_ALICE_SESSION_INTENT_ROOT": str(intent_root),
                "GHOST_ALICE_AUTOPILOT_PLAN_PATH": ".tmp/implementation-plans/stop-bridge.md",
            })
            run_dir = project / ".autopilot"

        self.assertEqual(payload, {"continue": True, "systemMessage": ""})
        self.assertFalse(run_dir.exists())

    def test_adapter_does_not_bootstrap_project_run_without_approval_or_runtime_material(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            intent_root = root / "session-intent"
            _write_session_intent_run_source(
                intent_root,
                criteria=[{"id": "AC1", "summary": "already done", "source": "user-explicit", "status": "met", "admitted": True}],
            )

            payload = aps.adapter_payload_from_env({
                "PWD": str(project),
                "GHOST_ALICE_PLATFORM": "codex",
                "GHOST_ALICE_SESSION_INTENT_ROOT": str(intent_root),
            })
            run_dir = project / ".autopilot"

        self.assertEqual(payload, {"continue": True, "systemMessage": ""})
        self.assertFalse(run_dir.exists())

    def test_adapter_bootstraps_when_admitted_unmet_criterion_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            intent_root = root / "session-intent"
            _write_session_intent_run_source(
                intent_root,
                criteria=[{
                    "id": "AC1",
                    "summary": "ship X",
                    "source": "user-explicit",
                    "status": "unmet",
                    "admitted": True,
                }],
            )

            payload = aps.adapter_payload_from_env({
                "PWD": str(project),
                "GHOST_ALICE_PLATFORM": "codex",
                "GHOST_ALICE_SESSION_INTENT_ROOT": str(intent_root),
                "GHOST_ALICE_AUTOPILOT_PLAN_PATH": ".tmp/implementation-plans/stop-bridge.md",
            })
            approved_run_path = project / ".autopilot" / "approved-run.json"
            bootstrapped = approved_run_path.exists()
            approved_run = json.loads(approved_run_path.read_text(encoding="utf-8")) if bootstrapped else {}

        self.assertTrue(payload["continue"])
        self.assertTrue(bootstrapped)
        self.assertEqual(approved_run["approval_evidence"]["decision"], "AUTO")
        self.assertEqual(approved_run["approval_evidence"]["source"], "admitted-unmet-criterion")
        self.assertIn("AC1", approved_run["approval_evidence"]["open_criteria"])

    def test_adapter_payload_defaults_to_project_autopilot_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_dir = project / ".autopilot"
            _write_run(run_dir, [_item("next")])

            payload = aps.adapter_payload_from_env({"PWD": str(project)})
            items = aps.read_work_items(run_dir / "tasks.jsonl")

        self.assertTrue(payload["continue"])
        self.assertIn("work-item: next", payload["systemMessage"])
        self.assertEqual(items[0]["status"], "running")

    def test_adapter_payload_prefers_process_cwd_run_state_over_stale_pwd(self):
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            stale_pwd = root / "stale"
            project.mkdir()
            stale_pwd.mkdir()
            run_dir = project / ".autopilot"
            _write_run(run_dir, [_item("next")])

            try:
                old_pwd = os.environ.get("PWD")
                os.environ["PWD"] = str(stale_pwd)
                os.chdir(project)
                payload = aps.adapter_payload_from_env()
                items = aps.read_work_items(run_dir / "tasks.jsonl")
            finally:
                os.chdir(original_cwd)
                if old_pwd is None:
                    os.environ.pop("PWD", None)
                else:
                    os.environ["PWD"] = old_pwd

        self.assertTrue(payload["continue"])
        self.assertIn("work-item: next", payload["systemMessage"])
        self.assertEqual(items[0]["status"], "running")

    def test_off_file_pauses_default_project_run_without_mutating_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_dir = project / ".autopilot"
            _write_run(run_dir, [_item("next")])
            (run_dir / "OFF").write_text("", encoding="utf-8")
            before = (run_dir / "tasks.jsonl").read_text(encoding="utf-8")

            payload = aps.adapter_payload_from_env({"PWD": str(project)})
            after = (run_dir / "tasks.jsonl").read_text(encoding="utf-8")

        self.assertEqual(payload, {"continue": True, "systemMessage": ""})
        self.assertEqual(after, before)

    def test_unapproved_stopped_or_exhausted_run_is_noop_without_mutating_tasks(self):
        cases = [
            {"approved": False, "status": "running", "remaining_steps": 3},
            {"approved": True, "status": "stopped", "remaining_steps": 3},
            {"approved": True, "status": "running", "remaining_steps": 0},
        ]

        for run_config in cases:
            with self.subTest(run_config=run_config), tempfile.TemporaryDirectory() as tmp:
                run_dir = Path(tmp)
                _write_run(run_dir, [_item("next")])
                before = (run_dir / "tasks.jsonl").read_text(encoding="utf-8")
                _write_run_config(run_dir, **run_config)

                payload = aps.advance_approved_run(run_dir)
                after = (run_dir / "tasks.jsonl").read_text(encoding="utf-8")
                events_exists = (run_dir / "events.jsonl").exists()

            self.assertEqual(payload, {"continue": True, "systemMessage": ""})
            self.assertEqual(after, before)
            self.assertFalse(events_exists)

    def test_approved_run_requires_explicit_go_boundaries_before_continuing(self):
        cases = [
            ("scope", lambda record: record.pop("scope")),
            ("scope.empty", lambda record: record.__setitem__("scope", {})),
            ("budget", lambda record: record.pop("budget")),
            ("budget.remaining_steps", lambda record: record["budget"].pop("remaining_steps")),
            ("budget.remaining_steps.type", lambda record: record["budget"].__setitem__("remaining_steps", "3")),
            ("allowed_surfaces", lambda record: record.pop("allowed_surfaces")),
            ("allowed_surfaces.type", lambda record: record.__setitem__("allowed_surfaces", "_shared/...")),
            ("stop_conditions", lambda record: record.pop("stop_conditions")),
            ("stop_conditions.empty", lambda record: record.__setitem__("stop_conditions", [])),
            ("approval_evidence", lambda record: record.pop("approval_evidence")),
            ("approval_evidence.empty", lambda record: record.__setitem__("approval_evidence", {})),
            ("approval_evidence.string", lambda record: record.__setitem__("approval_evidence", "GO")),
            ("approval_evidence.no_decision", lambda record: record.__setitem__("approval_evidence", {"source": "unit-test"})),
            ("approval_evidence.no_source", lambda record: record.__setitem__("approval_evidence", {"decision": "GO"})),
            ("approval_evidence.negative_decision", lambda record: record.__setitem__("approval_evidence", {"decision": "NO", "source": "unit-test"})),
        ]

        for field, mutate in cases:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                run_dir = Path(tmp)
                _write_run(run_dir, [_item("next")])
                before = (run_dir / "tasks.jsonl").read_text(encoding="utf-8")
                record = _approved_run_record()
                mutate(record)
                (run_dir / "approved-run.json").write_text(json.dumps(record), encoding="utf-8")

                payload = aps.advance_approved_run(run_dir)
                after = (run_dir / "tasks.jsonl").read_text(encoding="utf-8")
                events_exists = (run_dir / "events.jsonl").exists()

            self.assertEqual(payload, {"continue": True, "systemMessage": ""})
            self.assertEqual(after, before)
            self.assertFalse(events_exists)

    def test_work_item_outside_approved_run_allowed_surfaces_is_noop_without_mutating_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _write_run(run_dir, [_item("next")])
            before = (run_dir / "tasks.jsonl").read_text(encoding="utf-8")
            record = _approved_run_record()
            record["allowed_surfaces"] = ["docs/..."]
            (run_dir / "approved-run.json").write_text(json.dumps(record), encoding="utf-8")

            payload = aps.advance_approved_run(run_dir)
            after = (run_dir / "tasks.jsonl").read_text(encoding="utf-8")
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(payload, {"continue": True, "systemMessage": ""})
        self.assertEqual(after, before)
        self.assertEqual(events[0]["event"], "ready_item_outside_allowed_surfaces")
        self.assertEqual(events[0]["work_item_id"], "next")

    def test_running_item_without_decision_resumes_instead_of_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _write_run(run_dir, [_item("waiting", depends_on=["done"]), _item("done", status="running")])

            payload = aps.advance_approved_run(run_dir)
            items = aps.read_work_items(run_dir / "tasks.jsonl")
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertTrue(payload["continue"])
        self.assertIn("work-item: done", payload["systemMessage"])
        self.assertIn("pending-decision: missing", payload["systemMessage"])
        self.assertEqual([item["status"] for item in items], ["ready", "running"])
        self.assertEqual(events[0]["event"], "resume_running_item_without_decision")
        self.assertEqual(events[0]["run_id"], "run-1")

    def test_decision_candidate_file_is_not_adapter_consumable(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _write_run(run_dir, [_item("current", status="running")])
            (run_dir / "consistency-decision.candidate.json").write_text(
                json.dumps({
                    "schema_version": "autopilot-consistency-decision-candidate.v1",
                    "promotion_state": "candidate",
                    "action_file_allowed": False,
                    "work_item_id": "current",
                    "decision": "reopen_macro",
                    "evidence": ["conduct_feedback:scope-drift"],
                }),
                encoding="utf-8",
            )

            payload = aps.advance_approved_run(run_dir)
            items = aps.read_work_items(run_dir / "tasks.jsonl")
            candidate_still_exists = (run_dir / "consistency-decision.candidate.json").is_file()
            action_exists = (run_dir / "consistency-decision.json").exists()
            applied_action_exists = (run_dir / "consistency-decision.applied.json").exists()

        self.assertTrue(payload["continue"])
        self.assertIn("pending-decision: missing", payload["systemMessage"])
        self.assertEqual(items[0]["status"], "running")
        self.assertTrue(candidate_still_exists)
        self.assertFalse(action_exists)
        self.assertFalse(applied_action_exists)

    def test_candidate_schema_in_action_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _write_run(
                run_dir,
                [_item("current", status="running")],
                decision={
                    "schema_version": "autopilot-consistency-decision-candidate.v1",
                    "promotion_state": "candidate",
                    "action_file_allowed": False,
                    "work_item_id": "current",
                    "decision": "reopen_macro",
                    "evidence": ["conduct_feedback:scope-drift"],
                },
            )

            with self.assertRaises(aps.AutopilotStateError) as ctx:
                aps.advance_approved_run(run_dir)

        self.assertIn("adapter-consumable", str(ctx.exception))

    def test_unconsumable_decision_is_quarantined_and_still_raises(self):
        # Philosophy: an unconsumable decision is rejected (raises -- fail-closed),
        # but the offending file is quarantined so it does not re-raise forever.
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _write_run(
                run_dir,
                [_item("current", status="running")],
                decision={
                    "schema_version": "autopilot-consistency-decision-candidate.v1",
                    "promotion_state": "candidate",
                    "action_file_allowed": False,
                    "work_item_id": "current",
                    "decision": "reopen_macro",
                    "evidence": ["conduct_feedback:scope-drift"],
                },
            )

            with self.assertRaises(aps.AutopilotStateError):
                aps.advance_approved_run(run_dir)

            action_exists = (run_dir / "consistency-decision.json").exists()
            rejected_exists = (run_dir / "consistency-decision.rejected.json").exists()
            events_path = run_dir / "events.jsonl"
            events = events_path.read_text(encoding="utf-8") if events_path.exists() else ""

        self.assertFalse(action_exists)   # quarantined: moved out of the action slot
        self.assertTrue(rejected_exists)  # preserved as evidence
        self.assertIn("consistency_decision_rejected", events)

    def test_malformed_decision_json_is_quarantined_and_still_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _write_run(run_dir, [_item("current", status="running")])
            (run_dir / "consistency-decision.json").write_text("{not json", encoding="utf-8")

            with self.assertRaises(aps.AutopilotStateError):
                aps.advance_approved_run(run_dir)

            action_exists = (run_dir / "consistency-decision.json").exists()
            rejected_exists = (run_dir / "consistency-decision.rejected.json").exists()
            events = (run_dir / "events.jsonl").read_text(encoding="utf-8")

        self.assertFalse(action_exists)
        self.assertTrue(rejected_exists)
        self.assertIn("consistency_decision_rejected", events)

    def test_repeated_missing_decision_escalates_to_user_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _write_run(run_dir, [_item("current", status="running")])
            (run_dir / "events.jsonl").write_text(
                json.dumps({
                    "schema_version": "autopilot-event.v1",
                    "event": "resume_running_item_without_decision",
                    "run_id": "run-1",
                    "work_item_id": "current",
                })
                + "\n",
                encoding="utf-8",
            )

            payload = aps.advance_approved_run(run_dir)
            items = aps.read_work_items(run_dir / "tasks.jsonl")
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertTrue(payload["continue"])
        self.assertIn("pending-decision: repeated-missing-decision", payload["systemMessage"])
        self.assertIn("decision: ask_user_meta", payload["systemMessage"])
        self.assertEqual(items[0]["status"], "stopped")
        self.assertEqual(items[0]["completion"]["state"], "ask_user_meta")
        self.assertEqual(events[-1]["event"], "missing_decision_escalated")

    def test_repeated_missing_decision_with_iotrace_continues_without_meta_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            _write_run(run_dir, [_item("current", status="running")])
            _write_io_trace(root, "s-run", command="apply_patch current work")
            (run_dir / "events.jsonl").write_text(
                json.dumps({
                    "schema_version": "autopilot-event.v1",
                    "event": "resume_running_item_without_decision",
                    "run_id": "run-1",
                    "work_item_id": "current",
                })
                + "\n",
                encoding="utf-8",
            )

            payload = aps.adapter_payload_from_env({
                "HOME": str(root),
                "GHOST_ALICE_SESSION_ID": "s-run",
                "GHOST_ALICE_AUTOPILOT_RUN_DIR": str(run_dir),
            })
            items = aps.read_work_items(run_dir / "tasks.jsonl")
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertTrue(payload["continue"])
        self.assertIn("pending-decision: missing", payload["systemMessage"])
        self.assertIn("io-trace:", payload["systemMessage"])
        self.assertIn("governance-signal:", payload["systemMessage"])
        self.assertIn("observation_next_action:continue from latest io-trace", payload["systemMessage"])
        self.assertNotIn("decision: ask_user_meta", payload["systemMessage"])
        self.assertEqual(items[0]["status"], "running")
        self.assertEqual(events[-1]["event"], "resume_running_item_from_iotrace")
        self.assertEqual(events[-1]["governance_candidate_source"], "observation_signal")
        self.assertTrue(events[-1]["governance_candidate_id"].startswith("candidate-"))
        self.assertEqual(events[-1]["governance_candidate_decision"], "reopen_meso")
        self.assertTrue(events[-1]["governance_source_signal_id"])
        self.assertIn(
            "observation_next_action:continue from latest io-trace",
            events[-1]["governance_candidate_evidence"],
        )

    def test_iotrace_continuation_preserves_structured_bash_op_to_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            target = root / "src" / "x.py"
            target.parent.mkdir(parents=True)
            _write_run(run_dir, [_item("current", status="running")])
            _write_io_trace(
                root,
                "s-run",
                command=f'Get-Content -LiteralPath "{target}" -Raw',
                path_value=str(target),
                op="read",
            )
            (run_dir / "events.jsonl").write_text(
                json.dumps({
                    "schema_version": "autopilot-event.v1",
                    "event": "resume_running_item_without_decision",
                    "run_id": "run-1",
                    "work_item_id": "current",
                })
                + "\n",
                encoding="utf-8",
            )

            payload = aps.adapter_payload_from_env({
                "HOME": str(root),
                "GHOST_ALICE_SESSION_ID": "s-run",
                "GHOST_ALICE_AUTOPILOT_RUN_DIR": str(run_dir),
            })

        self.assertIn("- read ./src/x.py", payload["systemMessage"])
        self.assertNotIn("Get-Content", payload["systemMessage"])

    def test_iotrace_resume_limit_escalates_even_with_iotrace_present(self):
        # Loop-guard ceiling: io-trace-backed resume is allowed up to the limit,
        # but once IOTRACE_RESUME_LIMIT io-trace resumes have happened the run
        # escalates to ask_user_meta even though io-trace material still exists.
        # Without this, a session that keeps emitting io-trace re-fires forever.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            _write_run(run_dir, [_item("current", status="running")])
            _write_io_trace(root, "s-run", command="apply_patch current work")
            seeded = [
                {
                    "schema_version": "autopilot-event.v1",
                    "event": "resume_running_item_without_decision",
                    "run_id": "run-1",
                    "work_item_id": "current",
                },
                {
                    "schema_version": "autopilot-event.v1",
                    "event": "resume_running_item_from_iotrace",
                    "run_id": "run-1",
                    "work_item_id": "current",
                },
                {
                    "schema_version": "autopilot-event.v1",
                    "event": "resume_running_item_from_iotrace",
                    "run_id": "run-1",
                    "work_item_id": "current",
                },
            ]
            (run_dir / "events.jsonl").write_text(
                "\n".join(json.dumps(event) for event in seeded) + "\n",
                encoding="utf-8",
            )

            payload = aps.adapter_payload_from_env({
                "HOME": str(root),
                "GHOST_ALICE_SESSION_ID": "s-run",
                "GHOST_ALICE_AUTOPILOT_RUN_DIR": str(run_dir),
                "GHOST_ALICE_AUTOPILOT_IOTRACE_RESUME_LIMIT": "2",
            })
            items = aps.read_work_items(run_dir / "tasks.jsonl")
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertTrue(payload["continue"])
        self.assertIn("pending-decision: repeated-missing-decision", payload["systemMessage"])
        self.assertIn("decision: ask_user_meta", payload["systemMessage"])
        self.assertIn("io-trace resume limit reached", payload["systemMessage"])
        self.assertEqual(items[0]["status"], "stopped")
        self.assertEqual(items[0]["completion"]["state"], "ask_user_meta")
        self.assertEqual(events[-1]["event"], "missing_decision_escalated")

    def test_intent_advance_replenishes_resume_budget_then_still_terminates(self):
        # User's model: the resume ceiling is not a dead static count -- it is
        # replenished when session-intent advances (new input / newly-detected
        # work routed through session-intent-analyzer -> new intent-events lines).
        # With base limit 1: the base is already spent, so a fresh intent line
        # grants another base-worth and the run continues; with no further intent
        # advance the ceiling holds and it still terminates (ask_user_meta).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            run_dir.mkdir(parents=True, exist_ok=True)
            intent_events = root / "intent-events.jsonl"
            intent_events.write_text('{"event": "intent-1"}\n', encoding="utf-8")  # watermark = 1
            run_record = {
                "schema_version": "autopilot-run.v1",
                "run_id": "run-1",
                "approved": True,
                "status": "running",
                "scope": {"summary": "budget test run"},
                "budget": {"remaining_steps": 3, "intent_watermark": 1},
                "intent_source": {
                    "events_path": str(intent_events),
                    "state_path": str(root / "intent-state.json"),
                },
                "allowed_surfaces": ["_shared/..."],
                "stop_conditions": ["budget_exhausted", "user_stop"],
                "approval_evidence": {"decision": "GO", "source": "unit-test"},
            }
            (run_dir / "approved-run.json").write_text(json.dumps(run_record), encoding="utf-8")
            aps.write_work_items(run_dir / "tasks.jsonl", [_item("current", status="running")])
            _write_io_trace(root, "s-run", command="apply_patch current work")
            (run_dir / "events.jsonl").write_text(
                "\n".join(json.dumps(e) for e in [
                    {"schema_version": "autopilot-event.v1", "event": "resume_running_item_without_decision", "run_id": "run-1", "work_item_id": "current"},
                    {"schema_version": "autopilot-event.v1", "event": "resume_running_item_from_iotrace", "run_id": "run-1", "work_item_id": "current"},
                ]) + "\n",
                encoding="utf-8",
            )
            env = {
                "HOME": str(root),
                "GHOST_ALICE_SESSION_ID": "s-run",
                "GHOST_ALICE_AUTOPILOT_RUN_DIR": str(run_dir),
                "GHOST_ALICE_AUTOPILOT_IOTRACE_RESUME_LIMIT": "1",
            }

            # A NEW intent line grows the ledger -> replenish -> continue.
            intent_events.write_text(
                '{"event": "intent-1"}\n{"event": "intent-2"}\n', encoding="utf-8"
            )
            first = aps.adapter_payload_from_env(env)
            events_after_first = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]

            # No further intent advance -> ceiling holds -> terminates.
            second = aps.adapter_payload_from_env(env)
            items_after_second = aps.read_work_items(run_dir / "tasks.jsonl")

        self.assertTrue(first["continue"])
        self.assertIn("io-trace:", first["systemMessage"])
        self.assertNotIn("decision: ask_user_meta", first["systemMessage"])
        self.assertTrue(any(
            e["event"] == "resume_budget_replenished" and e.get("intent_events_seen") == 2
            for e in events_after_first
        ))
        self.assertEqual(events_after_first[-1]["event"], "resume_running_item_from_iotrace")
        self.assertIn("decision: ask_user_meta", second["systemMessage"])
        self.assertEqual(items_after_second[0]["status"], "stopped")

    def test_reset_n_budget_counts_only_resumes_since_last_replenish(self):
        # Reset-N discriminator: base=1, two prior replenishes but only ONE
        # io-trace resume since the LAST replenish, and no new intent this turn.
        # Reset-N counts resumes-since-last-replenish (=1 >= base) -> escalate.
        # (The old additive model would see total<base*(1+refills)=1*3 and wrongly
        # continue, so this test fails under additive and passes under reset-N.)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            run_dir.mkdir(parents=True, exist_ok=True)
            intent_events = root / "intent-events.jsonl"
            intent_events.write_text(
                '{"event": "intent-1"}\n{"event": "intent-2"}\n', encoding="utf-8"
            )  # count == last replenish watermark (2) -> no new replenish
            run_record = {
                "schema_version": "autopilot-run.v1",
                "run_id": "run-1",
                "approved": True,
                "status": "running",
                "scope": {"summary": "reset-n test run"},
                "budget": {"remaining_steps": 3, "intent_watermark": 0},
                "intent_source": {
                    "events_path": str(intent_events),
                    "state_path": str(root / "intent-state.json"),
                },
                "allowed_surfaces": ["_shared/..."],
                "stop_conditions": ["budget_exhausted", "user_stop"],
                "approval_evidence": {"decision": "GO", "source": "unit-test"},
            }
            (run_dir / "approved-run.json").write_text(json.dumps(run_record), encoding="utf-8")
            aps.write_work_items(run_dir / "tasks.jsonl", [_item("current", status="running")])
            _write_io_trace(root, "s-run", command="apply_patch current work")
            (run_dir / "events.jsonl").write_text(
                "\n".join(json.dumps(e) for e in [
                    {"schema_version": "autopilot-event.v1", "event": "resume_running_item_without_decision", "run_id": "run-1", "work_item_id": "current"},
                    {"schema_version": "autopilot-event.v1", "event": "resume_budget_replenished", "run_id": "run-1", "work_item_id": "current", "intent_events_seen": 1},
                    {"schema_version": "autopilot-event.v1", "event": "resume_running_item_from_iotrace", "run_id": "run-1", "work_item_id": "current"},
                    {"schema_version": "autopilot-event.v1", "event": "resume_budget_replenished", "run_id": "run-1", "work_item_id": "current", "intent_events_seen": 2},
                    {"schema_version": "autopilot-event.v1", "event": "resume_running_item_from_iotrace", "run_id": "run-1", "work_item_id": "current"},
                ]) + "\n",
                encoding="utf-8",
            )

            payload = aps.adapter_payload_from_env({
                "HOME": str(root),
                "GHOST_ALICE_SESSION_ID": "s-run",
                "GHOST_ALICE_AUTOPILOT_RUN_DIR": str(run_dir),
                "GHOST_ALICE_AUTOPILOT_IOTRACE_RESUME_LIMIT": "1",
            })
            items = aps.read_work_items(run_dir / "tasks.jsonl")

        self.assertTrue(payload["continue"])
        self.assertIn("decision: ask_user_meta", payload["systemMessage"])
        self.assertEqual(items[0]["status"], "stopped")

    def test_no_open_runnable_item_records_event_and_returns_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _write_run(run_dir, [_item("blocked", status="blocked")])

            payload = aps.advance_approved_run(run_dir)
            items = aps.read_work_items(run_dir / "tasks.jsonl")
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(payload, {"continue": True, "systemMessage": ""})
        self.assertEqual(items[0]["status"], "blocked")
        self.assertEqual(events[0]["event"], "no_ready_item")
        self.assertEqual(events[0]["run_id"], "run-1")

    def test_approved_run_imports_conduct_plan_before_no_ready_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _write_run(run_dir, [])
            record = _approved_run_record()
            record["allowed_surfaces"] = ["skill-evolution/..."]
            (run_dir / "approved-run.json").write_text(json.dumps(record), encoding="utf-8")
            (run_dir / "conduct-plan.json").write_text(json.dumps(_conduct_plan()), encoding="utf-8")

            payload = aps.advance_approved_run(run_dir)
            items = aps.read_work_items(run_dir / "tasks.jsonl")
            conduct_plan_removed = not (run_dir / "conduct-plan.json").exists()
            applied_conduct_plan_exists = (run_dir / "conduct-plan.applied.json").is_file()
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertTrue(payload["continue"])
        self.assertIn("work-item: conduct-scope-drift", payload["systemMessage"])
        self.assertIn("observer-agent: required", payload["systemMessage"])
        self.assertIn("observer-mode: read_only", payload["systemMessage"])
        self.assertEqual(items[0]["status"], "running")
        self.assertTrue(conduct_plan_removed)
        self.assertTrue(applied_conduct_plan_exists)
        self.assertEqual([event["event"] for event in events], ["conduct_plan_imported", "continue_next_item"])

    def test_approved_run_imports_conduct_plan_when_tasks_file_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            record = _approved_run_record()
            record["allowed_surfaces"] = ["skill-evolution/..."]
            (run_dir / "approved-run.json").write_text(json.dumps(record), encoding="utf-8")
            (run_dir / "conduct-plan.json").write_text(json.dumps(_conduct_plan()), encoding="utf-8")

            payload = aps.advance_approved_run(run_dir)
            items = aps.read_work_items(run_dir / "tasks.jsonl")

        self.assertTrue(payload["continue"])
        self.assertIn("work-item: conduct-scope-drift", payload["systemMessage"])
        self.assertEqual(items[0]["status"], "running")

    def test_approved_run_emits_next_ready_item_and_records_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _write_run(run_dir, [_item("next")])

            payload = aps.advance_approved_run(run_dir)
            items = aps.read_work_items(run_dir / "tasks.jsonl")
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertTrue(payload["continue"])
        self.assertIn("work-item: next", payload["systemMessage"])
        self.assertIn("Do next", payload["systemMessage"])
        self.assertEqual(items[0]["status"], "running")
        self.assertEqual(events[0]["event"], "continue_next_item")
        self.assertEqual(events[0]["work_item_id"], "next")

    def test_ready_item_event_preserves_governance_candidate_detail_from_iotrace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            _write_run(run_dir, [_item("next")])
            _write_io_trace(root, "session-1", command="apply_patch next work")

            payload = aps.adapter_payload_from_env({
                "HOME": str(root),
                "GHOST_ALICE_SESSION_ID": "session-1",
                "GHOST_ALICE_AUTOPILOT_RUN_DIR": str(run_dir),
            })
            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertTrue(payload["continue"])
        self.assertEqual(events[-1]["event"], "continue_next_item")
        self.assertEqual(events[-1]["governance_candidate_source"], "observation_signal")
        self.assertEqual(events[-1]["governance_candidate_decision"], "reopen_meso")
        self.assertTrue(events[-1]["governance_source_signal_id"])
        self.assertIn(
            "observation_next_action:continue from latest io-trace",
            events[-1]["governance_candidate_evidence"],
        )

    def test_parallel_advance_claims_ready_item_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _write_run(run_dir, [_item("race")])
            original_write_work_items = aps.write_work_items

            def slow_write_work_items(path, items):
                if Path(path).name == "tasks.jsonl":
                    time.sleep(0.08)
                return original_write_work_items(path, items)

            aps.write_work_items = slow_write_work_items
            payloads = []
            errors = []

            def worker():
                try:
                    payloads.append(aps.advance_approved_run(run_dir))
                except Exception as exc:  # pragma: no cover - surfaced by assertion below
                    errors.append(exc)

            try:
                threads = [threading.Thread(target=worker), threading.Thread(target=worker)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
            finally:
                aps.write_work_items = original_write_work_items

            events = [
                json.loads(line)
                for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(errors, [])
        self.assertEqual(sum(event["event"] == "continue_next_item" for event in events), 1)
        self.assertEqual(sum(event["event"] == "resume_running_item_without_decision" for event in events), 1)
        self.assertEqual(len(payloads), 2)

    def test_continuation_message_requires_consistency_decision_before_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _write_run(run_dir, [_item("next")])

            payload = aps.advance_approved_run(run_dir)

        self.assertIn("before-stop:", payload["systemMessage"])
        self.assertIn(".autopilot/consistency-decision.json", payload["systemMessage"])
        self.assertIn("continue_next", payload["systemMessage"])
        self.assertIn("retry_same_unit", payload["systemMessage"])
        self.assertIn("reopen_micro", payload["systemMessage"])
        self.assertIn("[completion-check]", payload["systemMessage"])

    def test_consistency_decision_completes_running_item_before_selecting_next(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _write_run(
                run_dir,
                [_item("current", status="running"), _item("next", depends_on=["current"])],
                decision=_decision_action(
                    "current",
                    "continue_next",
                    completion_check_digest=VALID_COMPLETION_DIGEST,
                    verdict="pass",
                    evidence=VALID_COMPLETION_EVIDENCE,
                ),
            )

            payload = aps.advance_approved_run(run_dir)
            items = aps.read_work_items(run_dir / "tasks.jsonl")
            decision_removed = not (run_dir / "consistency-decision.json").exists()
            applied_decision_exists = (run_dir / "consistency-decision.applied.json").is_file()

        self.assertEqual(items[0]["status"], "completed")
        self.assertEqual(items[1]["status"], "running")
        self.assertTrue(decision_removed)
        self.assertTrue(applied_decision_exists)
        self.assertIn("work-item: next", payload["systemMessage"])

    def test_validated_continue_next_marks_admitted_criterion_met_in_ledger(self):
        core_ledger = _locate_core_ledger_source()
        if core_ledger is None:
            self.skipTest("core session_intent_ledger.py not available for import-by-path")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            repo = tmp / "repo"
            # Mirror the repo-checkout layout so walk-up from the ledger finds core.
            scripts_dir = repo / "session-intent-analyzer" / "scripts"
            scripts_dir.mkdir(parents=True)
            shutil.copy(core_ledger, scripts_dir / "session_intent_ledger.py")
            intent_root = repo / ".tmp" / "session-intent"
            state_path = _write_session_intent_run_source(
                intent_root,
                criteria=[{
                    "id": "AC-TEST",
                    "summary": "current item satisfies the test criterion",
                    "source": "user-explicit",
                    "status": "unmet",
                    "admitted": True,
                }],
            )
            run_dir = tmp / "run"
            run_dir.mkdir()
            run_record = _approved_run_record()
            run_record["approval_evidence"] = {
                "decision": "GO",
                "source": "unit-test",
                "session_intent": {
                    "platform": "codex",
                    "session_id": "session-1",
                    "state_path": str(state_path),
                },
            }
            (run_dir / "approved-run.json").write_text(json.dumps(run_record), encoding="utf-8")
            aps.write_work_items(run_dir / "tasks.jsonl", [
                _item("current", status="running"),
                _item("next", depends_on=["current"]),
            ])
            (run_dir / "consistency-decision.json").write_text(
                json.dumps(_decision_action(
                    "current",
                    "continue_next",
                    completion_check_digest=VALID_COMPLETION_DIGEST,
                    verdict="pass",
                    evidence=VALID_COMPLETION_EVIDENCE,
                )),
                encoding="utf-8",
            )

            aps.advance_approved_run(run_dir)
            ledger_state = json.loads(state_path.read_text(encoding="utf-8"))

        ac = next(c for c in ledger_state["acceptance_criteria"] if c["id"] == "AC-TEST")
        # A validated continue_next flips the admitted criterion to met via the core API.
        self.assertEqual(ac["status"], "met")
        self.assertEqual(ac["met_completion_check_digest"], "a" * 64)

    def test_continue_next_without_reachable_core_ledger_continues_without_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            # No core repo is mirrored, so import-by-path cannot resolve the ledger.
            intent_root = tmp / "data" / "session-intent"
            state_path = _write_session_intent_run_source(
                intent_root,
                criteria=[{
                    "id": "AC-TEST",
                    "summary": "current item satisfies the test criterion",
                    "source": "user-explicit",
                    "status": "unmet",
                    "admitted": True,
                }],
            )
            run_dir = tmp / "run"
            run_dir.mkdir()
            run_record = _approved_run_record()
            run_record["approval_evidence"] = {
                "decision": "GO",
                "source": "unit-test",
                "session_intent": {
                    "platform": "codex",
                    "session_id": "session-1",
                    "state_path": str(state_path),
                },
            }
            (run_dir / "approved-run.json").write_text(json.dumps(run_record), encoding="utf-8")
            aps.write_work_items(run_dir / "tasks.jsonl", [
                _item("current", status="running"),
                _item("next", depends_on=["current"]),
            ])
            (run_dir / "consistency-decision.json").write_text(
                json.dumps(_decision_action(
                    "current",
                    "continue_next",
                    completion_check_digest=VALID_COMPLETION_DIGEST,
                    verdict="pass",
                    evidence=VALID_COMPLETION_EVIDENCE,
                )),
                encoding="utf-8",
            )

            payload = aps.advance_approved_run(run_dir)
            items = aps.read_work_items(run_dir / "tasks.jsonl")
            ledger_state = json.loads(state_path.read_text(encoding="utf-8"))

        # The decision still applies (item completed) and the run keeps going; the
        # criterion stays unmet because the met-flip is gracefully skipped.
        self.assertEqual(items[0]["status"], "completed")
        self.assertEqual(payload["continue"], True)
        ac = next(c for c in ledger_state["acceptance_criteria"] if c["id"] == "AC-TEST")
        self.assertEqual(ac["status"], "unmet")

    def test_validated_continue_next_marks_every_criterion_in_a_multi_criterion_claim(self):
        core_ledger = _locate_core_ledger_source()
        if core_ledger is None:
            self.skipTest("core session_intent_ledger.py with met-writer not available")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            repo = tmp / "repo"
            scripts_dir = repo / "session-intent-analyzer" / "scripts"
            scripts_dir.mkdir(parents=True)
            shutil.copy(core_ledger, scripts_dir / "session_intent_ledger.py")
            intent_root = repo / ".tmp" / "session-intent"
            state_path = _write_session_intent_run_source(
                intent_root,
                criteria=[
                    {"id": "AC-TEST", "summary": "first", "source": "user-explicit", "status": "unmet", "admitted": True},
                    {"id": "AC-TWO", "summary": "second", "source": "user-explicit", "status": "unmet", "admitted": True},
                ],
            )
            run_dir = tmp / "run"
            run_dir.mkdir()
            run_record = _approved_run_record()
            run_record["approval_evidence"] = {
                "decision": "GO",
                "source": "unit-test",
                "session_intent": {
                    "platform": "codex",
                    "session_id": "session-1",
                    "state_path": str(state_path),
                },
            }
            (run_dir / "approved-run.json").write_text(json.dumps(run_record), encoding="utf-8")
            aps.write_work_items(run_dir / "tasks.jsonl", [
                _item("current", status="running"),
                _item("next", depends_on=["current"]),
            ])
            evidence = ["\n".join([
                "[completion-check]",
                "- acceptance-criteria:",
                "  - AC-TEST: first [source: user-explicit]",
                "  - AC-TWO: second [source: user-explicit]",
                "- claim-evidence-map:",
                "  - claim: both items completed",
                "    criterion: AC-TEST, AC-TWO",
                "    evidence: tests/test_autopilot_state.py pass",
                "    verdict: pass",
                "- unverified:",
                "  - none",
            ])]
            (run_dir / "consistency-decision.json").write_text(
                json.dumps(_decision_action(
                    "current",
                    "continue_next",
                    completion_check_digest=VALID_COMPLETION_DIGEST,
                    verdict="pass",
                    evidence=evidence,
                )),
                encoding="utf-8",
            )

            aps.advance_approved_run(run_dir)
            ledger_state = json.loads(state_path.read_text(encoding="utf-8"))

        by_id = {c["id"]: c for c in ledger_state["acceptance_criteria"]}
        # A single claim binding multiple criteria flips every named criterion.
        self.assertEqual(by_id["AC-TEST"]["status"], "met")
        self.assertEqual(by_id["AC-TWO"]["status"], "met")

    def test_continue_next_rejects_completion_evidence_without_criterion_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _write_run(
                run_dir,
                [_item("current", status="running")],
                decision=_decision_action(
                    "current",
                    "continue_next",
                    completion_check_digest=VALID_COMPLETION_DIGEST,
                    verdict="pass",
                    evidence=[
                        "\n".join([
                            "[completion-check]",
                            "- claim-evidence-map:",
                            "  - claim: current item completed",
                            "    evidence: tests/test_autopilot_state.py pass",
                            "    verdict: pass",
                            "- unverified:",
                            "  - none",
                        ])
                    ],
                ),
            )

            with self.assertRaisesRegex(aps.AutopilotStateError, "criterion"):
                aps.advance_approved_run(run_dir)

    def test_continue_next_rejects_completion_evidence_with_failed_claim_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _write_run(
                run_dir,
                [_item("current", status="running")],
                decision=_decision_action(
                    "current",
                    "continue_next",
                    completion_check_digest=VALID_COMPLETION_DIGEST,
                    verdict="pass",
                    evidence=[
                        "\n".join([
                            "[completion-check]",
                            "- acceptance-criteria:",
                            "  - AC-TEST: current item satisfies the test criterion [source: user-explicit]",
                            "- claim-evidence-map:",
                            "  - claim: current item completed",
                            "    criterion: AC-TEST",
                            "    evidence: tests/test_autopilot_state.py failed",
                            "    verdict: fail",
                            "- unverified:",
                            "  - none",
                        ])
                    ],
                ),
            )

            with self.assertRaisesRegex(aps.AutopilotStateError, "verdict"):
                aps.advance_approved_run(run_dir)

    def test_continue_next_rejects_completion_evidence_with_unverified_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _write_run(
                run_dir,
                [_item("current", status="running")],
                decision=_decision_action(
                    "current",
                    "continue_next",
                    completion_check_digest=VALID_COMPLETION_DIGEST,
                    verdict="pass",
                    evidence=[
                        "\n".join([
                            "[completion-check]",
                            "- acceptance-criteria:",
                            "  - AC-TEST: current item satisfies the test criterion [source: user-explicit]",
                            "- claim-evidence-map:",
                            "  - claim: current item completed",
                            "    criterion: AC-TEST",
                            "    evidence: tests/test_autopilot_state.py not run",
                            "    verdict: pass",
                            "- unverified:",
                            "  - integration smoke not run",
                        ])
                    ],
                ),
            )

            with self.assertRaisesRegex(aps.AutopilotStateError, "unverified"):
                aps.advance_approved_run(run_dir)

    def test_promoted_consistency_decision_requires_valid_promotion_evidence(self):
        invalid_evidence_values = (
            "anything",
            {"decision": "NO", "source": "unit-test"},
            {"source": "unit-test"},
        )
        for promotion_evidence in invalid_evidence_values:
            with self.subTest(promotion_evidence=promotion_evidence), tempfile.TemporaryDirectory() as tmp:
                run_dir = Path(tmp)
                decision = _decision_action(
                    "current",
                    "continue_next",
                    completion_check_digest=VALID_COMPLETION_DIGEST,
                    verdict="pass",
                    evidence=VALID_COMPLETION_EVIDENCE,
                )
                decision["promotion_evidence"] = promotion_evidence
                _write_run(run_dir, [_item("current", status="running")], decision=decision)

                with self.assertRaisesRegex(aps.AutopilotStateError, "promotion_evidence"):
                    aps.advance_approved_run(run_dir)

    def test_retry_decision_requeues_same_item_without_popping_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _write_run(
                run_dir,
                [_item("current", status="running")],
                decision=_decision_action("current", "retry_same_unit", evidence=["verification failed"]),
            )

            payload = aps.advance_approved_run(run_dir)
            items = aps.read_work_items(run_dir / "tasks.jsonl")

        self.assertEqual([item["id"] for item in items], ["current"])
        self.assertEqual(items[0]["status"], "running")
        self.assertEqual(items[0]["attempt"], 1)
        self.assertIn("work-item: current", payload["systemMessage"])

    def test_reopen_decision_requeues_same_item_without_stopping(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _write_run(
                run_dir,
                [_item("current", status="running")],
                decision=_decision_action("current", "reopen_macro", evidence=["review found unresolved macro drift"]),
            )

            payload = aps.advance_approved_run(run_dir)
            items = aps.read_work_items(run_dir / "tasks.jsonl")

        self.assertEqual([item["id"] for item in items], ["current"])
        self.assertEqual(items[0]["status"], "running")
        self.assertEqual(items[0]["completion"]["state"], "reopened")
        self.assertEqual(items[0]["completion"]["reopen_target"], "macro")
        self.assertIn("work-item: current", payload["systemMessage"])
        self.assertIn("reopen-target: macro", payload["systemMessage"])

    def test_consistency_decision_rejects_non_array_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _write_run(
                run_dir,
                [_item("current", status="running")],
                decision={
                    **_decision_action(
                        "current",
                        "continue_next",
                        completion_check_digest=VALID_COMPLETION_DIGEST,
                        verdict="pass",
                    ),
                    "completion_check_digest": VALID_COMPLETION_DIGEST,
                    "verdict": "pass",
                    "evidence": "pytest tests/test_autopilot_state.py",
                },
            )

            with self.assertRaises(aps.AutopilotStateError):
                aps.advance_approved_run(run_dir)

            items = aps.read_work_items(run_dir / "tasks.jsonl")

        self.assertEqual(items[0]["status"], "running")

    def test_retry_decision_rejects_missing_concrete_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            decision = _decision_action("current", "retry_same_unit")
            decision["evidence"] = []
            _write_run(run_dir, [_item("current", status="running")], decision=decision)

            with self.assertRaises(aps.AutopilotStateError) as ctx:
                aps.advance_approved_run(run_dir)

        self.assertIn("non-continuation consistency decisions require evidence", str(ctx.exception))

    def test_ask_user_meta_decision_is_surfaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _write_run(
                run_dir,
                [_item("current", status="running")],
                decision=_decision_action("current", "ask_user_meta", evidence=["loop guard exhausted"]),
            )

            payload = aps.advance_approved_run(run_dir)
            items = aps.read_work_items(run_dir / "tasks.jsonl")

        self.assertTrue(payload["continue"])
        self.assertIn("decision: ask_user_meta", payload["systemMessage"])
        self.assertIn("loop guard exhausted", payload["systemMessage"])
        self.assertEqual(items[0]["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
