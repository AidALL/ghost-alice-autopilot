"""SQLite authority and atomic Core/Autopilot completion contracts."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import subprocess
import shutil
import threading
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_autopilot_state import _decision_action, _item, _write_run
import test_autopilot_bridge_admission as bridge_tests
import autopilot_state as aps
import autopilot_work_items as work_items


def standalone_authority(run_dir: Path) -> None:
    (run_dir / "authority.json").write_text(json.dumps({
        "schema_version": "autopilot-authority.v1", "kind": "standalone",
        "authority_id": "explicit-test-authority",
    }))


class AutopilotSQLiteTest(unittest.TestCase):
    def test_standalone_migration_uses_database_as_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            _write_run(run_dir, [_item("a")])
            standalone_authority(run_dir)
            aps.advance_approved_run(run_dir, {})
            database = run_dir / "ghost-state.sqlite3"
            self.assertTrue(database.is_file(), "validated run state must be in SQLite")
            with sqlite3.connect(database) as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM ap_runs").fetchone()[0], 1)
            (run_dir / "tasks.jsonl").write_text("not authoritative JSON\n")
            (run_dir / "approved-run.json").unlink()
            self.assertTrue(aps.advance_approved_run(run_dir, {})["systemMessage"])

    def test_named_legacy_standalone_migrates_without_inventing_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            _write_run(run_dir, [_item("a")])
            (run_dir / "authority.json").unlink(missing_ok=True)
            self.assertTrue(aps.advance_approved_run(run_dir, {})["systemMessage"])
            authority = json.loads((run_dir / "authority.json").read_text())
            self.assertEqual(authority["kind"], "standalone")
            self.assertNotIn("session_id", authority)
            self.assertEqual(aps.read_work_items(run_dir / "tasks.jsonl")[0]["status"], "running")

    def test_standalone_does_not_guess_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            _write_run(run_dir, [_item("a")])
            (run_dir / "authority.json").unlink(missing_ok=True)
            run = json.loads((run_dir / "approved-run.json").read_text())
            run.pop("run_id")
            (run_dir / "approved-run.json").write_text(json.dumps(run))
            with self.assertRaisesRegex(ValueError, "authority"):
                aps.advance_approved_run(run_dir, {})
            self.assertEqual(aps.read_work_items(run_dir / "tasks.jsonl")[0]["status"], "ready")

    def test_wal_initialization_does_not_hide_non_lock_errors_or_wait_forever(self):
        for code in (sqlite3.SQLITE_IOERR, sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            with self.subTest(code=code):
                error = sqlite3.OperationalError("injected initialization error")
                error.sqlite_errorcode = code
                connection = mock.Mock()
                connection.execute.side_effect = error
                with mock.patch.object(aps.storage.time, "monotonic", side_effect=[0.0, 11.0]):
                    with self.assertRaises(sqlite3.OperationalError) as caught:
                        aps.storage._enable_wal(connection)
                self.assertIs(caught.exception, error)
                self.assertEqual(connection.execute.call_count, 1)

    def test_first_wal_initialization_waits_for_competing_database_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            _write_run(run_dir, [_item("race")])
            connect = sqlite3.connect
            holder = connect(run_dir / "ghost-state.sqlite3", isolation_level=None)
            holder.execute("BEGIN IMMEDIATE")
            saw_busy = threading.Event()
            errors = []
            class ObservedConnection(sqlite3.Connection):
                def execute(self, statement, *args, **kwargs):
                    try:
                        return super().execute(statement, *args, **kwargs)
                    except sqlite3.OperationalError:
                        if statement.replace(" ", "").upper() == "PRAGMAJOURNAL_MODE=WAL":
                            saw_busy.set()
                        raise
            def instrumented_connect(*args, **kwargs):
                kwargs["factory"] = ObservedConnection
                return connect(*args, **kwargs)
            def worker():
                try:
                    aps.advance_approved_run(run_dir, {})
                except Exception as exc:
                    errors.append(exc)
            try:
                with mock.patch.object(aps.storage.sqlite3, "connect", side_effect=instrumented_connect):
                    thread = threading.Thread(target=worker)
                    thread.start()
                    self.assertTrue(saw_busy.wait(3), "fixture must actually contend at WAL initialization")
                    holder.rollback()
                    thread.join(3)
                    self.assertFalse(thread.is_alive())
            finally:
                holder.close()
            self.assertEqual(errors, [])
            events = aps.storage.read(run_dir / "events.jsonl")
            self.assertEqual(sum(row.get("event") == "continue_next_item" for row in events), 1)

    def test_migration_preserves_originals_and_imports_only_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            _write_run(run_dir, [_item("a")])
            originals = {p.name: p.read_bytes() for p in run_dir.iterdir() if p.name in {"approved-run.json", "tasks.jsonl"}}
            aps.advance_approved_run(run_dir, {})
            aps.advance_approved_run(run_dir, {})
            self.assertEqual({name: (run_dir / name).read_bytes() for name in originals}, originals)
            with sqlite3.connect(run_dir / "ghost-state.sqlite3") as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM ap_migrations").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT count(*) FROM ap_generations").fetchone()[0], 1)

    def test_failed_first_migration_is_retryable_without_partial_tasks(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            _write_run(run_dir, [_item("a")])
            with mock.patch.object(aps, "write_work_items", side_effect=OSError("task write failed")):
                with self.assertRaises(OSError):
                    aps.advance_approved_run(run_dir, {})
            self.assertEqual(aps.read_work_items(run_dir / "tasks.jsonl")[0]["status"], "ready")
            self.assertTrue(aps.advance_approved_run(run_dir, {})["systemMessage"])
            self.assertEqual(aps.read_work_items(run_dir / "tasks.jsonl")[0]["status"], "running")

    def test_deleted_authority_database_never_reimports_legacy_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            _write_run(run_dir, [_item("a")])
            aps.advance_approved_run(run_dir, {})
            (run_dir / "ghost-state.sqlite3").unlink()
            with self.assertRaisesRegex(ValueError, "database is missing"):
                aps.advance_approved_run(run_dir, {})
            self.assertFalse((run_dir / "ghost-state.sqlite3").exists())

    def test_process_crash_rolls_back_task_mutation_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            _write_run(run_dir, [_item("a")])
            aps.advance_approved_run(run_dir, {})
            adapter_dir = Path(aps.__file__).parent
            code = """import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import autopilot_storage as storage
root=Path(sys.argv[2])
with storage.transaction(root) as store:
    items=store.read('tasks.jsonl')
    items[0]['status']='completed'
    store.write('tasks.jsonl',items)
    os._exit(71)
"""
            result = subprocess.run([sys.executable, "-c", code, str(adapter_dir), str(run_dir)], capture_output=True)
            self.assertEqual(result.returncode, 71, result.stderr)
            self.assertEqual(aps.read_work_items(run_dir / "tasks.jsonl")[0]["status"], "running")
            self.assertTrue(aps.advance_approved_run(run_dir, {})["systemMessage"])

    def test_committed_receipt_is_not_reapplied_when_archive_rename_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            _write_run(run_dir, [_item("a", status="running")])
            decision = _decision_action("a", "continue_next", verdict="pass",
                completion_check_digest="sha256:" + "a" * 64,
                evidence=bridge_tests.VALID_COMPLETION_EVIDENCE)
            path = run_dir / "consistency-decision.json"
            path.write_text(json.dumps(decision))
            replace = os.replace
            def fail_inbox(source, target):
                if Path(target).name == "consistency-decision.applied.json":
                    raise OSError("archive failed after commit")
                return replace(source, target)
            with mock.patch.object(aps.storage.os, "replace", side_effect=fail_inbox):
                aps.advance_approved_run(run_dir, {})
            self.assertTrue(path.exists())
            self.assertEqual(aps.read_work_items(run_dir / "tasks.jsonl")[0]["status"], "completed")
            aps.advance_approved_run(run_dir, {})
            events = aps.storage.read(run_dir / "events.jsonl")
            self.assertEqual(sum(row.get("event") == "consistency_decision_applied" for row in events), 1)
            with sqlite3.connect(run_dir / "ghost-state.sqlite3") as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM ap_receipts").fetchone()[0], 1)
            self.assertTrue(path.exists(), "producer-owned inbox is never removed")
            self.assertFalse(aps.storage.pending(path))

    def test_archive_cannot_remove_a_new_concurrent_producer_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            _write_run(run_dir, [_item("a", status="running"), _item("b", depends_on=["a"])])
            receipt_a = _decision_action("a", "continue_next", verdict="pass",
                completion_check_digest="sha256:" + "a" * 64, evidence=bridge_tests.VALID_COMPLETION_EVIDENCE)
            receipt_a["decision_id"] = "receipt-a"
            receipt_b = _decision_action("b", "retry_same_unit", evidence=["actual failing check"])
            receipt_b["decision_id"] = "receipt-b"
            inbox = run_dir / "consistency-decision.json"
            inbox.write_text(json.dumps(receipt_a))
            replace = os.replace
            def concurrent_producer(source, target):
                if Path(target).name == "consistency-decision.applied.json":
                    inbox.write_text(json.dumps(receipt_b))
                return replace(source, target)
            with mock.patch.object(aps.storage.os, "replace", side_effect=concurrent_producer):
                aps.advance_approved_run(run_dir, {})
            self.assertEqual(json.loads(inbox.read_text())["decision_id"], "receipt-b")
            self.assertTrue(aps.storage.pending(inbox))
            aps.advance_approved_run(run_dir, {})
            events = aps.storage.read(run_dir / "events.jsonl")
            self.assertEqual([row["decision_id"] for row in events if row.get("event") == "consistency_decision_applied"], ["receipt-a", "receipt-b"])
            self.assertFalse(aps.storage.pending(inbox))

    def test_same_named_session_in_another_root_cannot_consume_bound_proof(self):
        core_root = os.environ["GHOST_ALICE_CORE_ROOT"]
        helper = bridge_tests.BridgeAdmissionBoundaryTest()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = helper.setup_ledger(root)
            state = json.loads(state_path.read_text())
            state["acceptance_criteria"][0].update(admitted=True, status="unmet")
            state_path.write_text(json.dumps(state))
            result = helper.invoke(root, env={"GHOST_ALICE_CORE_ROOT": core_root})
            self.assertEqual(result.returncode, 0, result.stderr)
            run_dir = root / "external-run"
            run = aps.storage.read(run_dir / "approved-run.json")
            item = aps.read_work_items(run_dir / "tasks.jsonl")[0]
            receipt = _decision_action(item["id"], "continue_next", verdict="pass",
                completion_check_digest="sha256:" + "a" * 64,
                evidence=[text.replace("AC-TEST", "bridge-state") for text in bridge_tests.VALID_COMPLETION_EVIDENCE],
                approval_generation=run["approval_generation"])
            decision_path = run_dir / "consistency-decision.json"
            decision_path.write_text(json.dumps(receipt))
            shutil.copytree(root / "intent", root / "other-intent")
            payload = aps.advance_approved_run(run_dir, {
                "GHOST_ALICE_CORE_ROOT": core_root, "GHOST_ALICE_PLATFORM": "codex",
                "GHOST_ALICE_SESSION_ID": "session-1",
                "GHOST_ALICE_SESSION_INTENT_ROOT": str(root / "other-intent"), "PWD": str(root)})
            self.assertEqual(payload["systemMessage"], "")
            self.assertTrue(decision_path.is_file())
            self.assertEqual(aps.read_work_items(run_dir / "tasks.jsonl")[0]["status"], "ready")
            ledger = work_items._load_core_ledger_module(state_path, os.environ)
            self.assertEqual(ledger.read_session_state(root=root / "intent", platform="codex", session_id="session-1")["acceptance_criteria"][0]["status"], "unmet")

    def test_bound_completion_rolls_back_core_when_task_write_fails(self):
        core_root = os.environ.get("GHOST_ALICE_CORE_ROOT")
        self.assertTrue(core_root, "cross-repository candidate Core is required")
        helper = bridge_tests.BridgeAdmissionBoundaryTest()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = helper.setup_ledger(root)
            state = json.loads(state_path.read_text())
            state["acceptance_criteria"][0].update(admitted=True, status="unmet")
            state_path.write_text(json.dumps(state))
            created = helper.invoke(root, env={"GHOST_ALICE_CORE_ROOT": core_root})
            self.assertEqual(created.returncode, 0, created.stderr)
            run_dir = root / "external-run"
            environment = {"GHOST_ALICE_CORE_ROOT": core_root,
                "GHOST_ALICE_SESSION_INTENT_ROOT": str(root / "intent"),
                "GHOST_ALICE_PLATFORM": "codex", "GHOST_ALICE_SESSION_ID": "session-1",
                "PWD": str(root)}
            aps.advance_approved_run(run_dir, environment)
            item = aps.read_work_items(run_dir / "tasks.jsonl")[0]
            evidence = ["[completion-check]\n- acceptance-criteria:\n  - bridge-state: required deliverable [source: user-explicit]\n- claim-evidence-map:\n  - claim: done\n    criterion: bridge-state\n    evidence: actual verification\n    verdict: pass\n- unverified:\n  - none"]
            run = aps.storage.read(run_dir / "approved-run.json")
            decision = _decision_action(item["id"], "continue_next", verdict="pass",
                completion_check_digest="sha256:" + "a" * 64, evidence=evidence,
                approval_generation=run["approval_generation"])
            (run_dir / "consistency-decision.json").write_text(json.dumps(decision))
            with mock.patch.object(aps, "write_work_items", side_effect=OSError("injected task write failure")):
                with self.assertRaises(OSError):
                    aps.advance_approved_run(run_dir, environment)
            ledger = work_items._load_core_ledger_module(state_path, environment)
            current = ledger.read_session_state(root=root / "intent", platform="codex", session_id="session-1")
            self.assertEqual(current["acceptance_criteria"][0]["status"], "unmet")
            self.assertEqual(aps.read_work_items(run_dir / "tasks.jsonl")[0]["status"], "running")
            self.assertTrue((run_dir / "consistency-decision.json").is_file())

