"""Exercise the real bridge CLI and its Stop-target diagnostic without inference."""

from __future__ import annotations

import json
import sqlite3
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import nullcontext
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_autopilot_session_bridge import (  # noqa: E402
    ADAPTER_SCRIPT,
    BRIDGE_SCRIPT,
    _write_current_session_ledger,
)
import autopilot_work_items as work_items  # noqa: E402
import autopilot_state as state_adapter  # noqa: E402
import autopilot_storage as aps_storage


def read_authority(path):
    path = Path(path)
    if path.name == "intent-state.json":
        import autopilot_work_items
        core = autopilot_work_items._load_core_ledger_module(path, os.environ)
        return core.read_session_state(root=path.parent.parent.parent,
            platform=path.parent.parent.name, session_id=path.parent.name)
    return aps_storage.read(path)


def update_criteria(path, criteria):
    import autopilot_work_items
    core = autopilot_work_items._load_core_ledger_module(path, os.environ)
    state = read_authority(path)
    core.record_turn(root=path.parent.parent.parent, platform=path.parent.parent.name,
        session_id=path.parent.name, intent_delta={"acceptance_criteria": criteria},
        expected_input_event_id=state["latest_input_event_id"])

from autopilot_intent_recovery import semantic_delta_starvation_event  # noqa: E402
from test_autopilot_state import _decision_action, VALID_COMPLETION_EVIDENCE  # noqa: E402


class BridgeAdmissionBoundaryTest(unittest.TestCase):
    def invoke(self, root, *, extra=(), check=False, session="session-1", env=None, input_token="event-1"):
        source = dict(os.environ)
        for key in list(source):
            if (key.startswith("GHOST_ALICE_") and key != "GHOST_ALICE_CORE_ROOT") or key in {"CODEX_THREAD_ID", "CLAUDE_PROJECT_DIR"}:
                source.pop(key)
        source.update({"PYTHONDONTWRITEBYTECODE": "1", "PWD": str(root)})
        source.update(env or {})
        args = [sys.executable, str(BRIDGE_SCRIPT), "--intent-root", str(root / "intent"),
                "--platform", "codex", "--session-id", session,
                "--run-dir", str(root / "external-run")]
        if check:
            args += ["--check"]
        else:
            args += ["--current-work-item-id", "current", "--plan-path", "plan.md",
                     "--approval-evidence-json", '{"decision":"GO","source":"current-user"}']
            if input_token is not None:
                args += ["--input-event-id", input_token]
        return subprocess.run(args + list(extra), cwd=root, env=source, capture_output=True, text=True)

    def setup_ledger(self, root):
        return _write_current_session_ledger(root / "intent", repeated_conduct_feedback=False)

    def assert_rejected_without_run(self, root, result):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(aps_storage.exists(root / "external-run" / "approved-run.json"))

    def test_rejects_state_identity_and_schema_mismatch_before_approval_write(self):
        for field, wrong in [("session_id", "other"), ("platform", "claude"),
                             ("schema_version", "wrong")]:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); state = self.setup_ledger(root)
                data = read_authority(state); data[field] = wrong
                state.write_text(json.dumps(data))
                self.assert_rejected_without_run(root, self.invoke(root))

    def test_rejects_foreign_latest_input_in_current_session_log(self):
        for field, wrong in [("session_id", "other"), ("platform", "claude")]:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); state = self.setup_ledger(root)
                events = state.parent / "intent-events.jsonl"
                rows = [json.loads(s) for s in events.read_text().splitlines()]
                rows[0][field] = wrong
                events.write_text("\n".join(json.dumps(x) for x in rows) + "\n")
                self.assert_rejected_without_run(root, self.invoke(root))

    def test_rejects_pointer_identity_disagreement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); self.setup_ledger(root)
            pointer = root / "intent/codex/current-session.json"
            data = json.loads(pointer.read_text()); data["session_id"] = "other"
            pointer.write_text(json.dumps(data))
            result = self.invoke(root, session="")
            self.assert_rejected_without_run(root, result)

    def test_cannot_overwrite_another_sessions_approved_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); self.setup_ledger(root)
            first = self.invoke(root); self.assertEqual(first.returncode, 0, first.stderr)
            run = root / "external-run/approved-run.json"; before = read_authority(run)
            _write_current_session_ledger(root / "intent", session_id="other", repeated_conduct_feedback=False)
            second = self.invoke(root, session="other")
            self.assertNotEqual(second.returncode, 0)
            self.assertEqual(read_authority(run), before)

    def test_check_reports_strict_override_mismatch_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); self.setup_ledger(root)
            selected = root / "hook-run"
            result = self.invoke(root, check=True, env={"GHOST_ALICE_AUTOPILOT_RUN_DIR": str(selected)})
            self.assertEqual(result.returncode, 0, result.stderr)
            check = json.loads(result.stdout)
            self.assertEqual(Path(check["automatic_target"]["run_dir"]), selected.resolve())
            self.assertFalse(check["automatic_target"]["matches_requested_run"])
            self.assertEqual(check["latest_input_event"]["event_id"], "event-1")
            self.assertFalse((root / "hook-run").exists())
            self.assertFalse((root / "external-run").exists())

    def test_check_default_target_then_original_stop_uses_same_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); self.setup_ledger(root)
            result = self.invoke(root, check=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            target = json.loads(result.stdout)["automatic_target"]
            self.assertEqual(Path(target["run_dir"]), (root / ".autopilot/sessions/codex/session-1").resolve())
            created = self.invoke(root, extra=["--run-dir", target["run_dir"]])
            self.assertEqual(created.returncode, 0, created.stderr)
            source = {k: v for k, v in os.environ.items() if not k.startswith("GHOST_ALICE_") or k == "GHOST_ALICE_CORE_ROOT"}
            source.pop("CODEX_THREAD_ID", None); source.pop("CLAUDE_PROJECT_DIR", None)
            source.update({"PYTHONDONTWRITEBYTECODE": "1", "GHOST_ALICE_PLATFORM": "codex",
                           "GHOST_ALICE_SESSION_INTENT_ROOT": str(root / "intent")})
            stopped = subprocess.run([sys.executable, str(ADAPTER_SCRIPT)], cwd=root, env=source,
                input=json.dumps({"hook_event_name": "Stop", "session_id": "session-1", "cwd": str(root)}),
                capture_output=True, text=True)
            self.assertEqual(stopped.returncode, 0, stopped.stderr)
            self.assertEqual(json.loads(stopped.stdout)["decision"], "block")

    def test_expected_input_event_rejects_stale_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); self.setup_ledger(root)
            result = self.invoke(root, extra=["--input-event-id", "old-input"])
            self.assert_rejected_without_run(root, result)
            self.assertIn("input event changed", result.stderr)

    def test_admission_requires_the_input_receipt_used_for_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); self.setup_ledger(root)
            result = self.invoke(root, input_token=None)
            self.assert_rejected_without_run(root, result)
            self.assertIn("input-event-id", result.stderr)

    def test_authoritative_state_anchor_is_not_replaced_by_lagging_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); state = self.setup_ledger(root)
            data = read_authority(state)
            data.update(ledger_revision=7, latest_input_event_id="committed-input",
                        latest_input_digest="3" * 64, latest_input_char_count=91)
            state.write_text(json.dumps(data))
            result = self.invoke(root, extra=["--input-event-id", "committed-input"])
            self.assertEqual(result.returncode, 0, result.stderr)
            run = read_authority(root / "external-run/approved-run.json")
            receipt = run["approval_evidence"]["session_intent"]
            self.assertEqual(receipt["latest_input_event"]["event_id"], "committed-input")
            self.assertEqual(receipt["latest_input_event"]["input_digest"], "3" * 64)
            self.assertEqual(receipt["ledger_revision"], 7)
            stale = self.invoke(root, check=True, extra=["--input-event-id", "event-1"])
            self.assertNotEqual(stale.returncode, 0)

    def test_core_check_does_not_repair_pending_audit_or_create_a_lock(self):
        core_root = os.environ.get("GHOST_ALICE_CORE_ROOT")
        if not core_root:
            self.skipTest("candidate core root required for cross-repository integration")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); state = self.setup_ledger(root)
            data = read_authority(state)
            data.update(ledger_revision=1, latest_input_event_id="event-1",
                        latest_input_digest="1" * 64, latest_input_char_count=20, pending_audit_event={"pending": True})
            state.write_text(json.dumps(data))
            before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            result = self.invoke(root, check=True, env={"GHOST_ALICE_CORE_ROOT": core_root})
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("audit is pending", result.stderr)
            after = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            self.assertEqual(after, before)

    def test_recovery_uses_the_validated_projection_not_later_foreign_audit_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); events = root / "events.jsonl"
            events.write_text("\n".join(json.dumps({"event": "user-input-observed", "session_id": "foreign"}) for _ in range(3)))
            current = {"platform": "codex", "session_id": "session-1", "events_path": events,
                       "intent_state": {"last_semantic_delta_status": "not-provided"}, "events": []}
            self.assertIsNone(semantic_delta_starvation_event(current))
            run = {"approval_evidence": {"session_intent": {"session_id": "session-1", "platform": "codex"}},
                   "intent_source": {"events_path": str(events)}, "budget": {"intent_watermark": 0}}
            self.assertFalse(state_adapter._maybe_replenish_resume_budget(root / "run", run, "task", current))

    def test_admission_cannot_write_through_active_database_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); state = self.setup_ledger(root)
            core = work_items._load_core_ledger_module(state, os.environ)
            with core.storage_transaction(root / "intent"):
                result = self.invoke(root)
            self.assert_rejected_without_run(root, result)
            self.assertIn("database is locked", result.stderr)

    def test_stale_directory_lock_does_not_block_sqlite_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); self.setup_ledger(root)
            (root / "external-run/.advance.lock").mkdir(parents=True)
            result = self.invoke(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(aps_storage.exists(root / "external-run/approved-run.json"))

    def test_stop_bootstrap_and_continuation_reject_foreign_input_identity(self):
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); state = self.setup_ledger(root)
                if existing:
                    created = self.invoke(root)
                    self.assertEqual(created.returncode, 0, created.stderr)
                events = state.parent / "intent-events.jsonl"
                rows = [json.loads(s) for s in events.read_text().splitlines()]
                rows[0]["session_id"] = "other"
                events.write_text("\n".join(json.dumps(x) for x in rows) + "\n")
                source = {k: v for k, v in os.environ.items() if not k.startswith("GHOST_ALICE_") or k == "GHOST_ALICE_CORE_ROOT"}
                source.pop("CODEX_THREAD_ID", None)
                source.update({"PYTHONDONTWRITEBYTECODE": "1", "GHOST_ALICE_PLATFORM": "codex",
                    "GHOST_ALICE_SESSION_INTENT_ROOT": str(root / "intent"),
                    "GHOST_ALICE_AUTOPILOT_RUN_DIR": str(root / "external-run"),
                    "GHOST_ALICE_AUTOPILOT_APPROVAL_EVIDENCE_JSON": '{"decision":"GO","source":"current-user"}'})
                stopped = subprocess.run([sys.executable, str(ADAPTER_SCRIPT)], cwd=root, env=source,
                    input=json.dumps({"hook_event_name": "Stop", "session_id": "session-1", "cwd": str(root)}),
                    capture_output=True, text=True)
                self.assertEqual(stopped.returncode, 0, stopped.stderr)
                if existing:
                    self.assertTrue(json.loads(stopped.stdout)["systemMessage"])
                    task = state_adapter.read_work_items(root / "external-run/tasks.jsonl")[0]
                    self.assertEqual(task["status"], "running")
                else:
                    self.assertFalse(aps_storage.exists(root / "external-run/approved-run.json"))

    def test_bridge_captures_approved_criterion_definition(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); state = self.setup_ledger(root)
            result = self.invoke(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            run = read_authority(root / "external-run/approved-run.json")
            self.assertEqual(run["approval_evidence"]["session_intent"].get("acceptance_criteria"),
                             read_authority(state)["acceptance_criteria"])

    def test_met_writer_receives_original_input_and_criterion_snapshot(self):
        criterion = {"id": "AC-ONE", "summary": "original requirement", "source": "user-explicit", "admitted": True}
        run = {"approval_evidence": {"session_intent": {
            "platform": "codex", "session_id": "original", "state_path": "/unused/codex/original/intent-state.json",
            "latest_input_event": {"event_id": "original-input"}, "acceptance_criteria": [criterion],
        }}}
        decision = {"completion_check_digest": "sha256:" + "a" * 64,
                    "evidence": [text.replace("AC-TEST", "AC-ONE") for text in VALID_COMPLETION_EVIDENCE]}
        decision["approval_generation"] = state_adapter.SESSION_MATERIAL.approval_generation(run)
        ledger = mock.Mock()
        with mock.patch.object(work_items, "_load_core_ledger_module", return_value=ledger):
            self.assertEqual(work_items.materialize_met_criteria_from_continue_next(run, decision), ["AC-ONE"])
        call = ledger.mark_acceptance_criterion_met.call_args.kwargs
        self.assertEqual(call.get("expected_input_event_id"), "original-input")
        self.assertEqual(call.get("expected_criterion"), criterion)

    def test_met_writer_does_not_invent_missing_approval_snapshot(self):
        run = {"approval_evidence": {"session_intent": {
            "platform": "codex", "session_id": "original", "state_path": "/unused/codex/original/intent-state.json",
        }}}
        decision = {"completion_check_digest": "sha256:" + "a" * 64,
                    "evidence": [text.replace("AC-TEST", "AC-ONE") for text in VALID_COMPLETION_EVIDENCE]}
        decision["approval_generation"] = state_adapter.SESSION_MATERIAL.approval_generation(run)
        ledger = mock.Mock()
        with mock.patch.object(work_items, "_load_core_ledger_module", return_value=ledger):
            self.assertEqual(work_items.materialize_met_criteria_from_continue_next(run, decision), [])
        ledger.mark_acceptance_criterion_met.assert_not_called()

    def test_met_writer_cannot_rebind_declared_identity_to_another_state_path(self):
        criterion = {"id": "AC-ONE", "summary": "original", "source": "user-explicit", "admitted": True}
        run = {"approval_evidence": {"session_intent": {
            "platform": "codex", "session_id": "original", "state_path": "/unused/codex/foreign/intent-state.json",
            "latest_input_event": {"event_id": "original-input"}, "acceptance_criteria": [criterion],
        }}}
        decision = {"completion_check_digest": "sha256:" + "a" * 64,
                    "evidence": [text.replace("AC-TEST", "AC-ONE") for text in VALID_COMPLETION_EVIDENCE]}
        decision["approval_generation"] = state_adapter.SESSION_MATERIAL.approval_generation(run)
        ledger = mock.Mock()
        with mock.patch.object(work_items, "_load_core_ledger_module", return_value=ledger):
            self.assertEqual(work_items.materialize_met_criteria_from_continue_next(run, decision), [])
        ledger.mark_acceptance_criterion_met.assert_not_called()

    def test_actual_core_rejects_old_approval_after_input_or_criterion_changes(self):
        core_root = os.environ.get("GHOST_ALICE_CORE_ROOT")
        if not core_root:
            self.skipTest("candidate core root required for cross-repository integration")
        for change in ("none", "input", "summary", "withdrawn"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); state_path = self.setup_ledger(root)
                state = read_authority(state_path)
                state["acceptance_criteria"][0].update(admitted=True, status="unmet")
                state_path.write_text(json.dumps(state))
                created = self.invoke(root, env={"GHOST_ALICE_CORE_ROOT": core_root})
                self.assertEqual(created.returncode, 0, created.stderr)
                run = read_authority(root / "external-run/approved-run.json")
                ledger = work_items._load_core_ledger_module(state_path, {"GHOST_ALICE_CORE_ROOT": core_root})
                state = read_authority(state_path)
                if change == "input":
                    ledger.record_turn(root=root / "intent", platform="codex", session_id="session-1",
                                       raw_user_input="a new correction")
                elif change == "summary":
                    state["acceptance_criteria"][0]["summary"] = "new report revision"
                    update_criteria(state_path, state["acceptance_criteria"])
                elif change == "withdrawn":
                    state["acceptance_criteria"][0]["admitted"] = False
                    update_criteria(state_path, state["acceptance_criteria"])
                decision = {"completion_check_digest": "sha256:" + "a" * 64,
                    "evidence": [text.replace("AC-TEST", "bridge-state") for text in VALID_COMPLETION_EVIDENCE]}
                decision["approval_generation"] = state_adapter.SESSION_MATERIAL.approval_generation(run)
                met = work_items.materialize_met_criteria_from_continue_next(
                    run, decision, {"GHOST_ALICE_CORE_ROOT": core_root})
                self.assertEqual(met, ["bridge-state"] if change == "none" else [])
                final = read_authority(state_path)["acceptance_criteria"][0]
                self.assertEqual(final["status"], "met" if change == "none" else "unmet")

    def test_native_completion_cannot_commit_after_core_rejects_the_proof(self):
        core_root = os.environ.get("GHOST_ALICE_CORE_ROOT")
        if not core_root:
            self.skipTest("candidate core root required for cross-repository integration")
        for change in ("none", "input", "summary", "core-write-failure", "ap-write-failure"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); state_path = self.setup_ledger(root)
                state = read_authority(state_path)
                state["acceptance_criteria"][0].update(admitted=True, status="unmet")
                state_path.write_text(json.dumps(state))
                created = self.invoke(root, env={"GHOST_ALICE_CORE_ROOT": core_root})
                self.assertEqual(created.returncode, 0, created.stderr)
                run_dir = root / "external-run"
                items = state_adapter.read_work_items(run_dir / "tasks.jsonl")
                items[0]["status"] = "running"
                state_adapter.write_work_items(run_dir / "tasks.jsonl", items)
                ledger = work_items._load_core_ledger_module(state_path, {"GHOST_ALICE_CORE_ROOT": core_root})
                if change == "input":
                    ledger.record_turn(root=root / "intent", platform="codex", session_id="session-1",
                                       raw_user_input="same objective, new correction")
                elif change == "summary":
                    state["acceptance_criteria"][0]["summary"] = "use the updated report revision"
                    update_criteria(state_path, state["acceptance_criteria"])
                evidence = ["[completion-check]\n- acceptance-criteria:\n  - bridge-state: required deliverable [source: user-explicit]\n- claim-evidence-map:\n  - claim: done\n    criterion: bridge-state\n    evidence: actual verification\n    verdict: pass\n- unverified:\n  - none"]
                decision = _decision_action(items[0]["id"], "continue_next", verdict="pass",
                    completion_check_digest="sha256:" + "a" * 64, evidence=evidence,
                    approval_generation=read_authority(run_dir / "approved-run.json")["approval_generation"])
                (run_dir / "consistency-decision.json").write_text(json.dumps(decision))
                failed_core = mock.Mock(wraps=ledger)
                failed_core.mark_acceptance_criterion_met.side_effect = OSError("injected core write failure")
                fault = mock.patch.object(work_items, "_load_core_ledger_module", return_value=failed_core) if change == "core-write-failure" else nullcontext()
                if change == "ap-write-failure":
                    fault = mock.patch.object(state_adapter, "write_work_items", side_effect=OSError("injected AP write failure"))
                environment = {
                    "GHOST_ALICE_CORE_ROOT": core_root, "GHOST_ALICE_SESSION_INTENT_ROOT": str(root / "intent"),
                    "GHOST_ALICE_SESSION_ID": "session-1", "GHOST_ALICE_PLATFORM": "codex", "PWD": str(root),
                }
                error = None
                with fault:
                    try:
                        state_adapter.advance_approved_run(run_dir, environment)
                    except (state_adapter.AutopilotStateError, OSError) as exc:
                        error = str(exc)
                final_task = state_adapter.read_work_items(run_dir / "tasks.jsonl")[0]
                if change == "none":
                    self.assertIsNone(error)
                    self.assertEqual(final_task["status"], "completed")
                else:
                    self.assertIsNotNone(error)
                    self.assertEqual(final_task["status"], "running")
                    self.assertFalse((run_dir / "consistency-decision.applied.json").exists())
                if change == "ap-write-failure":
                    self.assertEqual(read_authority(state_path)["acceptance_criteria"][0]["status"], "unmet")
                    self.assertTrue((run_dir / "consistency-decision.json").exists())
                    state_adapter.advance_approved_run(run_dir, environment)
                    self.assertEqual(state_adapter.read_work_items(run_dir / "tasks.jsonl")[0]["status"], "completed")

    def test_reapproval_archives_old_proof_and_rejects_late_previous_generation(self):
        core_root = os.environ.get("GHOST_ALICE_CORE_ROOT")
        if not core_root:
            self.skipTest("candidate core required")
        for change in ("input", "criterion", "scope"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); state_path = self.setup_ledger(root)
                state = read_authority(state_path); state["acceptance_criteria"][0].update(admitted=True, status="unmet")
                state_path.write_text(json.dumps(state))
                self.assertEqual(self.invoke(root).returncode, 0)
                run_dir = root / "external-run"; run_path = run_dir / "approved-run.json"
                old_run = read_authority(run_path); item = state_adapter.read_work_items(run_dir / "tasks.jsonl")[0]
                proof = _decision_action(item["id"], "continue_next", verdict="pass", completion_check_digest="sha256:" + "a" * 64,
                    evidence=[s.replace("AC-TEST", "bridge-state") for s in VALID_COMPLETION_EVIDENCE])
                proof["approval_generation"] = old_run.get("approval_generation", "old-unbound-generation")
                decision_path = run_dir / "consistency-decision.json"; decision_path.write_text(json.dumps(proof)); old_bytes = decision_path.read_bytes()
                token = "event-1"; extra = []
                if change == "input":
                    core = work_items._load_core_ledger_module(state_path, {"GHOST_ALICE_CORE_ROOT": core_root})
                    core.record_turn(root=root / "intent", platform="codex", session_id="session-1", raw_user_input="new requirement")
                    state = read_authority(state_path); token = state["latest_input_event_id"]
                elif change == "scope":
                    extra = ["--allowed-surface", "revised-plan.md"]
                if change != "scope":
                    state["acceptance_criteria"][0]["summary"] = "new required revision"
                    update_criteria(state_path, state["acceptance_criteria"])
                admitted = self.invoke(root, input_token=token, extra=extra)
                self.assertEqual(admitted.returncode, 0, admitted.stderr)
                self.assertFalse(aps_storage.pending(decision_path), "old pending proof must not survive as an active action")
                self.assertTrue(any(p.read_bytes() == old_bytes for p in (run_dir / ".approval-history").rglob("consistency-decision.json")))
                new_run = read_authority(run_path)
                self.assertNotEqual(new_run["approval_generation"], old_run["approval_generation"])
                late_proof = json.loads(old_bytes)
                late_proof["decision_id"] = "late-old-generation"
                decision_path.write_text(json.dumps(late_proof))  # A different producer returns late.
                env = {"GHOST_ALICE_CORE_ROOT": core_root, "GHOST_ALICE_SESSION_INTENT_ROOT": str(root / "intent"),
                       "GHOST_ALICE_SESSION_ID": "session-1", "GHOST_ALICE_PLATFORM": "codex", "PWD": str(root)}
                with self.assertRaisesRegex(state_adapter.AutopilotStateError, "approval generation"):
                    state_adapter.advance_approved_run(run_dir, env)
                self.assertNotEqual(state_adapter.read_work_items(run_dir / "tasks.jsonl")[0]["status"], "completed")
                self.assertEqual(read_authority(state_path)["acceptance_criteria"][0]["status"], "unmet")
                fresh_proof = _decision_action(item["id"], "continue_next", verdict="pass",
                    completion_check_digest="sha256:" + "b" * 64,
                    evidence=[text.replace("tests/test_autopilot_state.py pass", "fresh verification of revised approval passed") for text in proof["evidence"]],
                    approval_generation=new_run["approval_generation"])
                decision_path.write_text(json.dumps(fresh_proof))
                state_adapter.advance_approved_run(run_dir, env)
                self.assertEqual(state_adapter.read_work_items(run_dir / "tasks.jsonl")[0]["status"], "completed")

    def test_same_input_same_contract_bridge_retry_preserves_pending_and_terminal_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); self.setup_ledger(root)
            self.assertEqual(self.invoke(root).returncode, 0)
            run_dir = root / "external-run"; run_path = run_dir / "approved-run.json"
            run = read_authority(run_path); items = state_adapter.read_work_items(run_dir / "tasks.jsonl")
            (run_dir / "consistency-decision.json").write_text(json.dumps(_decision_action(
                items[0]["id"], "continue_next", verdict="pass", completion_check_digest="sha256:" + "a" * 64,
                evidence=[s.replace("AC-TEST", "bridge-state") for s in VALID_COMPLETION_EVIDENCE],
                approval_generation=run["approval_generation"])))
            before = {p.name: p.read_bytes() for p in run_dir.iterdir() if p.is_file()}
            self.assertEqual(self.invoke(root).returncode, 0)
            self.assertEqual({p.name: p.read_bytes() for p in run_dir.iterdir() if p.is_file()}, before)

            items[0]["status"] = "completed"; state_adapter.write_work_items(run_dir / "tasks.jsonl", items)
            run["status"] = "stopped"; run["budget"]["remaining_steps"] = 0; aps_storage.write(run_path, run)
            (run_dir / "OFF").write_text("paused")
            before = {p.name: p.read_bytes() for p in run_dir.iterdir() if p.is_file()}
            self.assertEqual(self.invoke(root).returncode, 0)
            self.assertEqual({p.name: p.read_bytes() for p in run_dir.iterdir() if p.is_file()}, before)

    def test_promotion_preserves_captured_generation_and_rejects_missing_or_stale(self):
        script = Path(os.environ.get("AP_PROMOTION_TEST_SCRIPT", str(Path(BRIDGE_SCRIPT).parent.parent / "addons/autopilot-mode/skill/scripts/autopilot_governance_signal.py")))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); self.setup_ledger(root)
            self.assertEqual(self.invoke(root).returncode, 0)
            run_dir = root / "external-run"; run = read_authority(run_dir / "approved-run.json")
            items = state_adapter.read_work_items(run_dir / "tasks.jsonl"); items[0]["status"] = "running"
            state_adapter.write_work_items(run_dir / "tasks.jsonl", items)
            governance = state_adapter.SESSION_MATERIAL.load_governance_signal_module(required=True)
            for token in (None, "sha256:" + "0" * 64, run["approval_generation"]):
                with self.subTest(token=token):
                    candidate = governance.decision_candidate_from_governance(
                        work_item_id=items[0]["id"], completion_validation={"valid": False, "errors": ["test failure"]},
                        approval_generation=token)
                    candidate_path = run_dir / "consistency-decision.candidate.json"; candidate_path.write_text(json.dumps(candidate))
                    target = run_dir / "promoted-test.json"
                    result = subprocess.run([sys.executable, str(script), "promote-decision", "--candidate", str(candidate_path),
                                             "--run-dir", str(run_dir), "--out", str(target)], capture_output=True, text=True)
                    if token == run["approval_generation"]:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(json.loads(target.read_text()).get("approval_generation"), token)
                    else:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertFalse(target.exists())



if __name__ == "__main__":
    unittest.main()
