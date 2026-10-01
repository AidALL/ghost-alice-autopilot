"""Real multi-session pretool, completion and Stop regressions."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import unittest

import test_completion_publication as fixtures
from autopilot_runtime_context import resolve_run_target


class SessionRunSelectionTest(unittest.TestCase):
    read_state = fixtures.CompletionPublicationTest.read_state
    proof = fixtures.CompletionPublicationTest.proof

    def setUp(self):
        fixtures.CompletionPublicationTest.setUp(self)
        self.root = self.root.resolve()
        self.source.update(GHOST_ALICE_AUTOPILOT_CWD=str(self.root), PWD=str(self.root))
        self.core.migrate_session(root=self.root / "intent", platform="codex", session_id="session-1")
        self.legacy = self.root / ".autopilot"

    def environment(self, source):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("GHOST_ALICE_") and k not in {"CODEX_THREAD_ID", "CLAUDE_PROJECT_DIR"}}
        return dict(env, **source, PYTHONDONTWRITEBYTECODE="1")

    def source_for(self, session, platform="codex"):
        self.core.record_turn(root=self.root / "intent", platform=platform, session_id=session,
            raw_user_input="Verify the requested report", intent_delta={
                "current_goal": "Write and verify the requested report.",
                "acceptance_criteria": self.read_state()["acceptance_criteria"]})
        return dict(self.source, GHOST_ALICE_PLATFORM=platform, GHOST_ALICE_SESSION_ID=session)

    def legacy_run(self):
        source = dict(self.source, GHOST_ALICE_AUTOPILOT_RUN_DIR=str(self.legacy))
        self.helper.prepare_completion(**dict(self.arguments, source=source), reapprove_current_input=True)
        return self.snapshot(self.legacy)

    def snapshot(self, root):
        authority = root / "authority.json"
        return (fixtures.adapter.storage.read(root / "approved-run.json"),
                fixtures.adapter.storage.read(root / "tasks.jsonl"),
                authority.read_bytes(), authority.stat().st_mtime_ns)

    def hook(self, source, event="PreToolUse"):
        script = "autopilot_pretool.py" if event == "PreToolUse" else "autopilot_mode.py"
        result = subprocess.run([sys.executable, "-B", str(fixtures.SKILL / "adapters" / script)],
            input=json.dumps({"hook_event_name": event, "session_id": source["GHOST_ALICE_SESSION_ID"],
                              "cwd": str(self.root)}),
            text=True, capture_output=True, cwd=self.root, env=self.environment(source))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def prepare_from_hook(self, source):
        output = self.hook(source)
        context = output.get("hookSpecificOutput", {}).get("additionalContext", "")
        commands = [line for line in context.splitlines()
                    if line.startswith("env ") and "autopilot_completion.py" in line]
        self.assertEqual(len(commands), 1, context)
        result = subprocess.run(shlex.split(commands[0]), text=True, capture_output=True,
                                cwd=self.root, env=self.environment(source))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_foreign_legacy_run_does_not_break_prepare_publish_or_stop(self):
        for platform in ("codex", "claude"):
            with self.subTest(platform=platform):
                if platform == "codex":
                    original = self.legacy_run()
                source = self.source_for("session-2", platform)
                receipt = self.prepare_from_hook(source)
                selected = Path(receipt["run_dir"])
                self.assertEqual(selected, self.legacy / "sessions" / platform / "session-2")
                self.assertEqual(selected, resolve_run_target(source)["run_dir"])
                self.assertEqual(self.snapshot(self.legacy), original)
                report = self.root / "report.txt"
                report.write_text("8\n")
                self.assertEqual(report.read_text(), "8\n")
                proof = self.proof() + "- evidence: tool-result:business-check-1\n"
                published = self.helper.publish_completion(receipt_token=receipt["receipt_token"],
                    completion_check=proof, verified_at=self.verified_at, source=source)
                self.assertEqual(published["status"], "pending-adapter")
                self.assertEqual(self.hook(source, "Stop").get("systemMessage"), "")
                tasks = fixtures.adapter.storage.read(selected / "tasks.jsonl")
                self.assertEqual([item["status"] for item in tasks], ["completed"])
                self.assertEqual(self.snapshot(self.legacy), original)
                state = self.core.read_session_state(root=self.root / "intent", platform=platform,
                                                    session_id="session-2")
                self.assertEqual({row["status"] for row in state["acceptance_criteria"]}, {"met"})

    def test_same_owner_legacy_run_remains_the_default(self):
        self.legacy_run()
        self.assertEqual(resolve_run_target(self.source)["run_dir"], self.legacy)
        receipt = self.prepare_from_hook(self.source)
        self.assertEqual(Path(receipt["run_dir"]), self.legacy)

    def test_same_session_at_different_authority_root_uses_own_run(self):
        original = self.legacy_run()
        other_root = self.root / "other-intent"
        self.core.record_turn(root=other_root, platform="codex", session_id="session-1",
            raw_user_input="Verify the requested report", intent_delta={
                "current_goal": "Write and verify the requested report.",
                "acceptance_criteria": self.read_state()["acceptance_criteria"]})
        source = dict(self.source, GHOST_ALICE_SESSION_INTENT_ROOT=str(other_root))
        receipt = self.prepare_from_hook(source)
        self.assertEqual(Path(receipt["run_dir"]), self.legacy / "sessions/codex/session-1")
        self.assertEqual(self.snapshot(self.legacy), original)

    def test_explicit_other_authority_root_does_not_issue_prepare_command(self):
        self.legacy_run()
        other_root = self.root / "other-intent"
        self.core.record_turn(root=other_root, platform="codex", session_id="session-1",
            raw_user_input="Verify the requested report", intent_delta={
                "current_goal": "Write and verify the requested report.",
                "acceptance_criteria": self.read_state()["acceptance_criteria"]})
        source = dict(self.source, GHOST_ALICE_SESSION_INTENT_ROOT=str(other_root),
                      GHOST_ALICE_AUTOPILOT_RUN_DIR=str(self.legacy))
        context = self.hook(source).get("hookSpecificOutput", {}).get("additionalContext", "")
        self.assertIn("ownership-conflict", context)
        self.assertFalse(any(line.startswith("env ") for line in context.splitlines()))

    def test_process_cwd_selection_does_not_open_foreign_database(self):
        self.legacy_run()
        source = self.source_for("session-2")
        source.pop("GHOST_ALICE_AUTOPILOT_CWD")
        database = self.root / "intent/ghost-state.sqlite3"
        parked = database.with_suffix(".parked")
        database.rename(parked)
        try:
            code = ("import sys; sys.path.insert(0, " + repr(str(fixtures.SKILL / "adapters")) + "); "
                    "from autopilot_runtime_context import resolve_run_target; "
                    "print(resolve_run_target()['run_dir'])")
            result = subprocess.run([sys.executable, "-B", "-c", code],
                cwd=self.root, env=self.environment(source), text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(self.legacy / "sessions/codex/session-2"))
        finally:
            parked.rename(database)

    def test_missing_root_binding_converges_prepare_publish_and_stop(self):
        original = self.legacy_run()
        current_root = self.root / ".tmp/session-intent"
        self.core.record_turn(root=current_root, platform="codex", session_id="session-1",
            raw_user_input="Verify the requested report", intent_delta={
                "current_goal": "Write and verify the requested report.",
                "acceptance_criteria": self.read_state()["acceptance_criteria"]})
        source = dict(self.source)
        source.pop("GHOST_ALICE_SESSION_INTENT_ROOT")
        receipt = self.prepare_from_hook(source)
        selected = self.legacy / "sessions/codex/session-1"
        self.assertEqual(Path(receipt["run_dir"]), selected)
        proof = self.proof() + "- evidence: tool-result:business-check-1\n"
        self.helper.publish_completion(receipt_token=receipt["receipt_token"],
            completion_check=proof, verified_at=self.verified_at, source=source)
        self.assertEqual(self.hook(source, "Stop").get("systemMessage"), "")
        self.assertEqual(fixtures.adapter.storage.read(selected / "tasks.jsonl")[0]["status"], "completed")
        state = self.core.read_session_state(root=current_root, platform="codex", session_id="session-1")
        self.assertEqual({row["status"] for row in state["acceptance_criteria"]}, {"met"})
        self.assertEqual(self.snapshot(self.legacy), original)

    def test_fresh_concurrent_sessions_prepare_separate_runs(self):
        sources = [self.source_for(session) for session in ("first", "second")]
        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(self.prepare_from_hook, sources))
        self.assertEqual(len({receipt["run_dir"] for receipt in receipts}), 2)
        for source, receipt in zip(sources, receipts):
            self.assertEqual(Path(receipt["run_dir"]), self.legacy / "sessions" / "codex" /
                             source["GHOST_ALICE_SESSION_ID"])
        self.assertFalse((self.legacy / "authority.json").exists())

    def test_explicit_foreign_target_reports_conflict_without_impossible_command(self):
        original = self.legacy_run()
        source = dict(self.source_for("session-2"), GHOST_ALICE_AUTOPILOT_RUN_DIR=str(self.legacy))
        output = self.hook(source)
        context = output.get("hookSpecificOutput", {}).get("additionalContext", "")
        self.assertIs(output.get("continue"), True)
        self.assertNotIn("permissionDecision", output.get("hookSpecificOutput", {}))
        self.assertIn("ownership-conflict", context)
        self.assertFalse(any(line.startswith("env ") for line in context.splitlines()))
        state = self.core.read_session_state(root=self.root / "intent", platform="codex", session_id="session-2")
        with self.assertRaisesRegex(ValueError, "same session|different session|ownership"):
            self.helper.prepare_completion(intent_root=self.root / "intent", platform="codex",
                session_id="session-2", input_event_id=state["latest_input_event_id"], source=source,
                reapprove_current_input=True)
        self.assertEqual(self.snapshot(self.legacy), original)

    def test_project_off_pauses_namespaced_pretool_prepare_and_stop(self):
        self.legacy_run()
        source = self.source_for("session-2")
        receipt = self.prepare_from_hook(source)
        selected = Path(receipt["run_dir"])
        original = self.snapshot(selected)
        (self.legacy / "OFF").touch()
        self.assertEqual(self.hook(source), {"continue": True})
        explicit = dict(source, GHOST_ALICE_AUTOPILOT_RUN_DIR=str(selected))
        with self.assertRaisesRegex(ValueError, "OFF"):
            self.helper.prepare_completion(intent_root=self.root / "intent", platform="codex",
                session_id="session-2", input_event_id=receipt["input_event_id"], source=explicit,
                reapprove_current_input=True)
        self.assertEqual(self.hook(source, "Stop").get("systemMessage"), "")
        self.assertEqual(self.snapshot(selected), original)

    def test_project_off_prevents_first_session_directory_creation(self):
        self.legacy.mkdir(); (self.legacy / "OFF").touch()
        source = self.source_for("session-2")
        self.assertEqual(self.hook(source), {"continue": True})
        self.assertEqual(self.hook(source, "Stop").get("systemMessage"), "")
        self.assertFalse((self.legacy / "sessions").exists())

    def test_project_off_precedes_invalid_legacy_owner_metadata(self):
        self.legacy.mkdir()
        (self.legacy / "OFF").touch()
        (self.legacy / "authority.json").write_text('{"schema_version":"invalid"}')
        source = self.source_for("session-2")
        self.assertEqual(self.hook(source), {"continue": True})
        self.assertEqual(self.hook(source, "Stop").get("systemMessage"), "")
        self.assertFalse((self.legacy / "sessions").exists())

    def test_session_off_does_not_pause_another_session(self):
        sources = [self.source_for(session) for session in ("first", "second")]
        receipts = [self.prepare_from_hook(source) for source in sources]
        (Path(receipts[0]["run_dir"]) / "OFF").touch()
        self.assertEqual(self.hook(sources[0], "Stop").get("systemMessage"), "")
        self.assertIn("completion-publication: missing", self.hook(sources[1], "Stop")["systemMessage"])

    def test_unsafe_session_component_is_rejected_without_creating_state(self):
        for session in ("..", "../escape", "nested/session", "nested\\session"):
            with self.subTest(session=session), self.assertRaises(ValueError):
                resolve_run_target(dict(self.source, GHOST_ALICE_SESSION_ID=session))
        self.assertFalse(self.legacy.exists())


if __name__ == "__main__":
    unittest.main()
