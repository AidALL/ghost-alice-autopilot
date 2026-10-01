"""Prepare once, verify once, publish proof without repeating business work.

Dependencies: Python standard library and the candidate Core storage runtime.
"""
from __future__ import annotations

import copy
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "addons/autopilot-mode/skill"
SCRIPT = SKILL / "scripts/autopilot_completion.py"
sys.path.insert(0, str(SKILL / "adapters"))
sys.path.insert(0, str(ROOT / "tests"))
import autopilot_state as adapter
import autopilot_work_items as work_items
from test_autopilot_session_bridge import _write_current_session_ledger


class CompletionPublicationTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), "missing supported completion prepare/publish path")
        spec = importlib.util.spec_from_file_location("completion_publication_under_test", SCRIPT)
        self.helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.helper)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_path = _write_current_session_ledger(self.root / "intent", repeated_conduct_feedback=False)
        state = json.loads(self.state_path.read_text())
        state["current_goal"] = "Write and verify the requested report."
        state["acceptance_criteria"] = [{"id": key, "summary": summary,
            "source": "user-explicit", "admitted": True, "status": "unmet"}
            for key, summary in [("report", "Saved report matches input"), ("protected", "Protected file is unchanged")]]
        self.state_path.write_text(json.dumps(state))
        self.run_dir = self.root / ".autopilot/sessions/codex/session-1"
        self.source = {"GHOST_ALICE_CORE_ROOT": os.environ["GHOST_ALICE_CORE_ROOT"],
            "GHOST_ALICE_PLATFORM": "codex", "GHOST_ALICE_SESSION_ID": "session-1",
            "GHOST_ALICE_SESSION_INTENT_ROOT": str(self.root / "intent"),
            "GHOST_ALICE_AUTOPILOT_CWD": str(self.root), "PWD": str(self.root)}
        self.arguments = dict(intent_root=self.root / "intent", platform="codex", session_id="session-1",
                              input_event_id="event-1", source=self.source)
        self.core = work_items._load_core_ledger_module(self.state_path, self.source)

    def prepare(self):
        return self.helper.prepare_completion(**self.arguments)

    def proof(self, verified_at=None):
        self.verified_at = verified_at or datetime.now(timezone.utc).isoformat()
        self.evidence_source = "tool-result:business-check-1"
        self.completion = f"""[completion-check]
- verification-before-completion: done
- skill-call: verification-before-completion (this turn)
- acceptance-criteria:
  - report: Saved report matches input [source: user-explicit]
  - protected: Protected file is unchanged [source: user-explicit]
- claim-evidence-map:
  - claim: Saved report matches input
    criterion: report
    evidence: {self.evidence_source} at {self.verified_at}; exact readback passed
    verdict: pass
  - claim: Protected file is unchanged
    criterion: protected
    evidence: {self.evidence_source} at {self.verified_at}; original bytes match
    verdict: pass
- unverified: none
"""
        return self.completion

    def publish(self, receipt, **changes):
        args = dict(receipt_token=receipt["receipt_token"], completion_check=self.completion,
            verified_at=self.verified_at, evidence_source=self.evidence_source, source=self.source)
        args.update(changes)
        return self.helper.publish_completion(**args)

    def read_state(self):
        return self.core.read_session_state(root=self.root / "intent", platform="codex", session_id="session-1")

    def change_intent(self, delta=None, *, new_input=False):
        current = self.read_state()
        self.core.record_turn(root=self.root / "intent", platform="codex", session_id="session-1",
            raw_user_input="new request" if new_input else None, intent_delta=delta,
            expected_input_event_id=current["latest_input_event_id"])

    def stop(self):
        return adapter.adapter_payload_from_env(self.source,
            hook_input={"hook_event_name": "Stop", "session_id": "session-1", "cwd": str(self.root)})

    def test_prepare_verify_once_publish_stop_preserves_original_evidence(self):
        receipt = self.prepare()
        business = self.root / "report.txt"
        business.write_text("8\n")
        verification_count = 0
        def verify_once():
            nonlocal verification_count
            verification_count += 1
            self.assertEqual(business.read_text(), "8\n")
        verify_once()
        original_proof = self.proof()
        published = self.publish(receipt)
        self.assertEqual(published["status"], "pending-adapter")
        self.assertEqual({x["status"] for x in self.read_state()["acceptance_criteria"]}, {"unmet"})
        self.assertEqual(self.stop()["systemMessage"], "")
        self.assertEqual(verification_count, 1)
        task = adapter.read_work_items(self.run_dir / "tasks.jsonl")[0]
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["completion"]["evidence"], [original_proof])
        self.assertEqual(task["completion"]["completion_check_digest"], "sha256:" + hashlib.sha256(original_proof.encode()).hexdigest())
        self.assertEqual({x["status"] for x in self.read_state()["acceptance_criteria"]}, {"met"})

    def test_prepare_is_idempotent_without_refreshing_receipt_time(self):
        first = self.prepare()
        self.assertEqual(self.prepare(), first)
        self.assertIn("prepared_at", first)

    def test_unsupported_platform_api_rejects_before_state_access(self):
        for platform in ("agent-runtime", "custom"):
            source = dict(self.source, GHOST_ALICE_PLATFORM=platform)
            with self.subTest(platform=platform), \
                    mock.patch.object(adapter, "read_session_material", side_effect=AssertionError("unsupported platform read session state")), \
                    mock.patch.object(adapter, "resolve_run_target", side_effect=AssertionError("unsupported platform resolved runtime state")), \
                    mock.patch.object(adapter.storage, "transaction", side_effect=AssertionError("unsupported platform opened transaction")):
                with self.assertRaisesRegex(ValueError, "supports only codex and claude"):
                    self.helper.prepare_completion(**dict(self.arguments, platform=platform, source=source))
                with self.assertRaisesRegex(ValueError, "supports only codex and claude"):
                    self.helper.publish_completion(receipt_token="unused", completion_check="unused",
                        verified_at="unused", evidence_source="unused", source=source)

    def test_unsupported_platform_cli_rejects_before_reading_proof_or_writing_state(self):
        source = {**os.environ, **self.source, "GHOST_ALICE_PLATFORM": "agent-runtime", "PYTHONDONTWRITEBYTECODE": "1"}
        source.pop("CODEX_THREAD_ID", None)
        source.pop("CLAUDE_PROJECT_DIR", None)
        before = {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in self.root.rglob("*") if p.is_file()}
        base = [sys.executable, "-B", str(SCRIPT)]
        prepare = subprocess.run(base + ["prepare", "--intent-root", str(self.root / "intent"),
            "--platform", "agent-runtime", "--session-id", "session-1", "--input-event-id", "event-1"],
            cwd=self.root, env=source, capture_output=True, text=True)
        self.assertEqual(prepare.returncode, 2)
        self.assertIn("invalid choice: 'agent-runtime'", prepare.stderr)
        publish = subprocess.run(base + ["publish", "--receipt-token", "unused", "--verified-at", "unused",
            "--evidence-source", "unused", "--completion-file", str(self.root / "proof-must-not-be-read")],
            cwd=self.root, env=source, capture_output=True, text=True)
        self.assertEqual(publish.returncode, 2)
        self.assertIn("supports only codex and claude", publish.stderr)
        after = {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns)
                 for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_explicit_unchanged_reapproval_keeps_the_original_receipt(self):
        original = self.prepare()
        self.assertEqual(self.helper.prepare_completion(**self.arguments, reapprove_current_input=True), original)

    def test_same_input_constraint_reapproval_archives_pending_proof_and_rotates_origin(self):
        original = self.prepare(); old_proof = self.proof(); old_time = self.verified_at
        self.publish(original)
        self.change_intent({"constraints": ["New authorized restriction"],
            "non_goals": ["No historical work"], "decisions": [{"id": "scope-decision", "summary": "Use current scope"}]})
        with self.assertRaises(ValueError): self.prepare()
        current = self.helper.prepare_completion(**self.arguments, reapprove_current_input=True)
        self.assertNotEqual(current["approval_generation"], original["approval_generation"])
        self.assertNotEqual(current["receipt_token"], original["receipt_token"])
        with adapter.storage.transaction(self.run_dir, self.source) as store:
            self.assertFalse(adapter.storage.pending(self.run_dir / "consistency-decision.json"))
            archived = store.connection.execute("SELECT body_json FROM ap_objects WHERE generation=? AND name=?",
                (original["approval_generation"], self.helper.PUBLICATION_OBJECT)).fetchone()
            self.assertEqual(json.loads(archived[0])["completion_check"], old_proof)
        with self.assertRaises(ValueError): self.publish(original)
        with self.assertRaises(ValueError): self.publish(current, completion_check=old_proof, verified_at=old_time)
        self.proof(); self.publish(current); self.stop()
        self.assertEqual({x["status"] for x in self.read_state()["acceptance_criteria"]}, {"met"})

    def test_prepare_does_not_admit_a_different_input_arriving_during_bootstrap(self):
        original_bootstrap = self.helper.adapter._bootstrap_from_session_intent_if_approved
        def changed_input(*args, **kwargs):
            self.core.record_turn(root=self.root / "intent", platform="codex", session_id="session-1",
                raw_user_input="New request arriving before admission", expected_input_event_id="event-1")
            return original_bootstrap(*args, **kwargs)
        with mock.patch.object(self.helper.adapter, "_bootstrap_from_session_intent_if_approved", changed_input):
            with self.assertRaises(ValueError): self.prepare()
        self.assertFalse(adapter._run_state_available(self.run_dir))

    def test_missing_publication_is_not_a_business_mismatch(self):
        receipt = self.prepare()
        trace = self.root / "trace.jsonl"
        trace.write_text(json.dumps({"session": "session-1", "tool": "Bash", "path": "report.txt", "op": "read"}) + "\n")
        self.source["GHOST_ALICE_IO_TRACE_FILE"] = str(trace)
        text = self.stop()["systemMessage"]
        self.assertIn("completion-publication: missing", text)
        self.assertIn(receipt["receipt_token"], text)
        self.assertIn("autopilot_completion.py publish", text)
        self.assertNotIn("observation_verdict:reopen_focus", text)
        self.assertNotIn("fresh resolved decision only after verification", text)

    def test_missing_tampered_or_foreign_receipt_is_rejected(self):
        receipt = self.prepare(); self.proof()
        for token in ("", "sha256:" + "0" * 64, receipt["receipt_token"] + "x"):
            with self.subTest(token=token), self.assertRaises(ValueError):
                self.publish(receipt, receipt_token=token)
        foreign = dict(self.source, GHOST_ALICE_SESSION_ID="other")
        with self.assertRaises(ValueError): self.publish(receipt, source=foreign)

    def test_new_input_and_changed_criterion_or_scope_are_rejected(self):
        for change in ("input", "criterion", "scope"):
            with self.subTest(change=change):
                # Each mutation makes the same original receipt stale; no rebind is allowed.
                if change == "input":
                    receipt = self.prepare(); self.proof()
                    self.change_intent(new_input=True)
                elif change == "criterion":
                    criteria = copy.deepcopy(self.read_state()["acceptance_criteria"])
                    criteria[0]["summary"] = "A different report condition"
                    self.change_intent({"acceptance_criteria": criteria})
                else:
                    self.change_intent({"constraints": ["A changed scope restriction"]})
                with self.assertRaises(ValueError): self.publish(receipt)

    def test_same_input_criterion_change_is_rejected(self):
        receipt = self.prepare(); self.proof()
        criteria = copy.deepcopy(self.read_state()["acceptance_criteria"])
        criteria[0]["summary"] = "A different report condition"
        self.change_intent({"acceptance_criteria": criteria})
        with self.assertRaises(ValueError): self.publish(receipt)

    def test_same_input_scope_change_is_rejected(self):
        receipt = self.prepare(); self.proof()
        self.change_intent({"constraints": ["Read-only report"]})
        with self.assertRaises(ValueError): self.publish(receipt)

    def test_failed_unknown_unadmitted_missing_or_incomplete_proof_is_rejected(self):
        receipt = self.prepare(); original = self.proof()
        for proof in (original.replace("verdict: pass", "verdict: fail", 1),
                      original.replace("criterion: report", "criterion: invented"),
                      original.replace("- unverified: none", "- unverified: pending"),
                      original.replace("    evidence:", "    explanation:"),
                      original.split("  - claim: Protected")[0] + "- unverified: none\n"):
            with self.subTest(proof=proof), self.assertRaises(ValueError):
                self.publish(receipt, completion_check=proof)
        criteria = copy.deepcopy(self.read_state()["acceptance_criteria"])
        criteria[1]["admitted"] = False
        self.change_intent({"acceptance_criteria": criteria})
        with self.assertRaises(ValueError): self.publish(receipt)

    def test_proof_predating_prepare_and_timestamp_relabeling_are_rejected(self):
        old = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self.proof(old); receipt = self.prepare()
        with self.assertRaises(ValueError): self.publish(receipt)
        with self.assertRaises(ValueError): self.publish(receipt, verified_at=datetime.now(timezone.utc).isoformat())

    def test_publication_replay_preserves_evidence_and_does_not_recomplete(self):
        receipt = self.prepare(); self.proof()
        first = self.publish(receipt); self.stop()
        before = self.read_state()
        self.assertEqual(self.publish(receipt), first)
        self.stop()
        self.assertEqual(self.read_state(), before)
        with self.assertRaises(ValueError): self.publish(receipt, completion_check=self.completion + "\n")

    def test_scope_change_after_publication_is_rejected_at_adapter_commit(self):
        receipt = self.prepare(); self.proof(); self.publish(receipt)
        self.change_intent({"constraints": ["A restriction added after publication"]})
        with self.assertRaises(ValueError): self.stop()
        self.assertEqual({x["status"] for x in self.read_state()["acceptance_criteria"]}, {"unmet"})

    def test_tampered_queued_proof_cannot_bypass_publication_origin(self):
        receipt = self.prepare(); self.proof(); self.publish(receipt)
        with adapter.storage.transaction(self.run_dir, self.source) as store:
            action = store.read("consistency-decision.json")
            action["evidence"][0] = action["evidence"][0].replace("exact readback passed", "different proof")
            action.pop("completion_origin", None)
            store.write("consistency-decision.json", action)
        with self.assertRaises(ValueError): self.stop()
        self.assertEqual({x["status"] for x in self.read_state()["acceptance_criteria"]}, {"unmet"})

    def test_new_authorized_turn_gets_new_preverification_receipt_without_relabeling(self):
        original = self.prepare(); old_proof = self.proof(); old_time = self.verified_at
        self.publish(original); self.stop()
        criteria = copy.deepcopy(self.read_state()["acceptance_criteria"])
        for row in criteria:
            row.update(summary=row["summary"] + " for the newly authorized input", status="unmet")
        self.change_intent({"acceptance_criteria": criteria}, new_input=True)
        self.arguments["input_event_id"] = self.read_state()["latest_input_event_id"]
        with self.assertRaises(ValueError): self.prepare()
        self.assertIn("reapprove_current_input", inspect.signature(self.helper.prepare_completion).parameters,
                      "missing supported reapproval path for a new authorized input")
        current = self.helper.prepare_completion(**self.arguments, reapprove_current_input=True)
        self.assertNotEqual(current["approval_generation"], original["approval_generation"])
        with self.assertRaises(ValueError): self.publish(original)
        with self.assertRaises(ValueError): self.publish(current, completion_check=old_proof, verified_at=old_time)
        self.proof(); self.publish(current); self.stop()
        self.assertEqual({x["status"] for x in self.read_state()["acceptance_criteria"]}, {"met"})

    def test_core_skill_exposes_publication_before_initial_verification(self):
        skill = Path(os.environ["GHOST_ALICE_CORE_ROOT"]) / "coding-convention/verification-before-completion/SKILL.md"
        entry = skill.read_text()
        reference = "references/autopilot-publication.md"
        self.assertIn(f"[{reference}]({reference})", entry)
        self.assertIn("before preparation, business verification or the first final answer", entry)
        self.assertIn("current session has admitted criteria for authorized execution", entry)
        text = (skill.parent / reference).read_text()
        self.assertIn("autopilot_completion.py prepare", text)
        self.assertIn("autopilot_completion.py publish", text)
        self.assertIn("--reapprove-current-input", text)
        self.assertIn("before the business verification", text)
        self.assertIn("Plan-only", text)
        self.assertIn("criterion_ids", text)
        self.assertIn("archives the previous generation", text)
        self.assertIn("Codex or Claude", text)
        self.assertIn("Other platforms retain the existing workflow", text)

    def test_new_turn_appended_criterion_does_not_reprove_historical_met_criteria(self):
        original = self.prepare(); self.proof(); self.publish(original); self.stop()
        historical = copy.deepcopy(self.read_state()["acceptance_criteria"])
        self.change_intent({"acceptance_criteria": [{"id": "followup", "summary": "New authorized result",
            "source": "user-explicit", "admitted": True, "status": "unmet"}]}, new_input=True)
        self.arguments["input_event_id"] = self.read_state()["latest_input_event_id"]
        receipt = self.helper.prepare_completion(**self.arguments, reapprove_current_input=True)
        self.assertEqual(receipt["criterion_ids"], ["followup"])
        self.proof()
        self.completion = self.completion.replace("report", "followup").replace("Saved report matches input", "New authorized result")
        self.completion = self.completion.replace("  - protected: Protected file is unchanged [source: user-explicit]\n", "")
        self.completion = self.completion.split("  - claim: Protected file is unchanged")[0] + "- unverified: none\n"
        self.publish(receipt); self.stop()
        rows = self.read_state()["acceptance_criteria"]
        self.assertEqual([row for row in rows if row["id"] != "followup"], historical)
        self.assertEqual(next(row for row in rows if row["id"] == "followup")["status"], "met")

    def test_adapter_failure_rolls_back_core_and_task_together(self):
        receipt = self.prepare(); self.proof(); self.publish(receipt)
        db = self.root / "intent/ghost-state.sqlite3"
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("CREATE TRIGGER fail_task_write BEFORE UPDATE ON ap_objects WHEN NEW.name='tasks.jsonl' BEGIN SELECT RAISE(ABORT, 'injected task failure'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.stop()
        self.assertEqual({x["status"] for x in self.read_state()["acceptance_criteria"]}, {"unmet"})
        self.assertEqual(adapter.read_work_items(self.run_dir / "tasks.jsonl")[0]["status"], "ready")
        with closing(sqlite3.connect(db)) as conn, conn: conn.execute("DROP TRIGGER fail_task_write")
        self.stop()
        self.assertEqual({x["status"] for x in self.read_state()["acceptance_criteria"]}, {"met"})

    def test_cli_accepts_proof_on_stdin_without_business_scratch_file(self):
        source = dict(os.environ, **self.source, PYTHONDONTWRITEBYTECODE="1")
        source.pop("CODEX_THREAD_ID", None)
        source.pop("CLAUDE_PROJECT_DIR", None)
        args = [sys.executable, "-B", str(SCRIPT), "prepare", "--intent-root", str(self.root / "intent"),
            "--platform", "codex", "--session-id", "session-1", "--input-event-id", "event-1"]
        result = subprocess.run(args, cwd=self.root, env=source, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout); self.proof()
        args = [sys.executable, "-B", str(SCRIPT), "publish", "--receipt-token", receipt["receipt_token"],
            "--verified-at", self.verified_at, "--evidence-source", self.evidence_source, "--completion-file", "-"]
        result = subprocess.run(args, cwd=self.root, env=source, input=self.completion, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["verified_at"], self.verified_at)


if __name__ == "__main__":
    unittest.main()
