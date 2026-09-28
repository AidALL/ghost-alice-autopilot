"""Trusted pretool provenance captures before evidence without admitting work."""
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import unittest
from unittest import mock

import test_completion_publication as fixtures
ROOT, SKILL, adapter = fixtures.ROOT, fixtures.SKILL, fixtures.adapter


class ProspectiveCompletionTest(unittest.TestCase):
    proof = fixtures.CompletionPublicationTest.proof
    publish = fixtures.CompletionPublicationTest.publish
    read_state = fixtures.CompletionPublicationTest.read_state
    change_intent = fixtures.CompletionPublicationTest.change_intent
    stop = fixtures.CompletionPublicationTest.stop

    def setUp(self):
        fixtures.CompletionPublicationTest.setUp(self)
        self.core.migrate_session(root=self.root / "intent", platform="codex", session_id="session-1")
        self.pretool = SKILL / "adapters/autopilot_pretool.py"

    def capture(self, source=None):
        self.assertTrue(self.pretool.is_file(), "missing real pretool origin adapter")
        spec = importlib.util.spec_from_file_location("pretool_under_test", self.pretool)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        return module.capture_before_tool(self.source if source is None else source,
            {"hook_event_name": "PreToolUse", "session_id": "session-1", "cwd": str(self.root)})

    def test_capture_does_not_admit_or_advance_and_does_not_refresh_time(self):
        before = self.read_state()
        first = self.capture()
        self.assertEqual(first, self.capture())
        self.assertEqual(first["status"], "prospective")
        self.assertEqual(before, self.read_state())
        self.assertFalse(self.run_dir.exists())
        with closing(sqlite3.connect(self.root / "intent/ghost-state.sqlite3")) as conn:
            self.assertFalse(conn.execute("SELECT name FROM sqlite_master WHERE name='ap_runs'").fetchall())

    def test_stop_binds_original_capture_and_publishes_original_proof_once(self):
        origin = self.capture()
        verification_count = 0
        business = self.root / "business.txt"; business.write_text("answer")
        verification_count += 1; self.assertEqual(business.read_text(), "answer")
        original = self.proof()
        message = self.stop()["systemMessage"]
        self.assertIn("completion-publication: missing", message)
        self.assertIn(str(SKILL / "scripts/autopilot_completion.py"), message)
        self.assertNotIn("--receipt-token TOKEN", message)
        with adapter.storage.transaction(self.run_dir, self.source) as store:
            publication = store.read(self.helper.PUBLICATION_OBJECT)
        receipt = self.helper._receipt_output(publication)
        self.assertEqual(receipt["prepared_at"], origin["captured_at"])
        self.assertEqual(publication["receipt"]["prospective_origin"], origin["origin_token"])
        self.publish(receipt); self.stop()
        self.assertEqual(verification_count, 1)
        self.assertEqual(adapter.read_work_items(self.run_dir / "tasks.jsonl")[0]["completion"]["evidence"], [original])
        self.assertEqual({row["status"] for row in self.read_state()["acceptance_criteria"]}, {"met"})

    def test_changed_contract_without_new_capture_cannot_bind_old_origin(self):
        self.capture(); self.proof()
        self.change_intent({"constraints": ["Changed source boundary"]})
        message = self.stop()["systemMessage"]
        self.assertIn("prospective-origin: stale", message)
        with adapter.storage.transaction(self.run_dir, self.source) as store:
            with self.assertRaises(FileNotFoundError): store.read(self.helper.PUBLICATION_OBJECT)
        self.assertEqual({row["status"] for row in self.read_state()["acceptance_criteria"]}, {"unmet"})

    def test_changed_contract_capture_has_new_time_and_rejects_old_proof(self):
        first = self.capture(); old_proof = self.proof(); old_time = self.verified_at
        self.change_intent({"constraints": ["Changed authorized restriction"]})
        second = self.capture()
        self.assertNotEqual(first["origin_token"], second["origin_token"])
        self.stop()
        with adapter.storage.transaction(self.run_dir, self.source) as store:
            receipt = self.helper._receipt_output(store.read(self.helper.PUBLICATION_OBJECT))
        self.assertEqual(receipt["prepared_at"], second["captured_at"])
        with self.assertRaises(ValueError):
            self.publish(receipt, completion_check=old_proof, verified_at=old_time)

    def test_blocked_off_unsupported_and_plan_capture_never_advance_execution(self):
        blocked = {"model_security_decision": {"decision": "block", "input_event_id": "event-1",
            "reason": "Current explicit block", "risk_flags": []}}
        self.change_intent(blocked)
        self.assertEqual(self.capture()["status"], "skipped")
        self.assertFalse(self.run_dir.exists())
        self.change_intent({"model_security_decision": {"decision": "allow", "input_event_id": "event-1",
            "reason": "Resolved", "risk_flags": []}})
        self.run_dir.mkdir(); (self.run_dir / "OFF").write_text("")
        self.assertEqual(self.capture()["status"], "skipped")
        (self.run_dir / "OFF").unlink(); self.run_dir.rmdir()
        self.assertEqual(self.capture(dict(self.source, GHOST_ALICE_PLATFORM="agent-runtime"))["status"], "skipped")
        self.change_intent({"current_goal": "Provide a plan only, without execution."})
        self.capture()
        self.assertFalse(self.run_dir.exists())
        self.assertEqual({row["status"] for row in self.read_state()["acceptance_criteria"]}, {"unmet"})

    def test_capture_rechecks_current_input_inside_transaction(self):
        self.assertTrue(self.pretool.is_file())
        import autopilot_provenance as provenance
        original = provenance._locked_state
        def changed(*args, **kwargs):
            state = original(*args, **kwargs)
            return dict(state, latest_input_event_id="different-input")
        with mock.patch.object(provenance, "_locked_state", changed):
            with self.assertRaisesRegex(ValueError, "changed"):
                self.capture()
        with closing(sqlite3.connect(self.root / "intent/ghost-state.sqlite3")) as conn:
            table = conn.execute("SELECT name FROM sqlite_master WHERE name='ap_completion_origins'").fetchone()
            if table: self.assertEqual(conn.execute("SELECT COUNT(*) FROM ap_completion_origins").fetchone()[0], 0)

    def generation_lifecycle(self, admitted_first):
        if admitted_first:
            adapter._bootstrap_from_session_intent_if_approved(self.run_dir, self.source, self.root)
        origin = self.capture(); old_proof = self.proof(); old_time = self.verified_at
        self.stop()
        sys.path.insert(0, str(SKILL / "scripts"))
        from autopilot_session_bridge import bridge_session_intent_to_run_state
        bridge_session_intent_to_run_state(intent_root=self.root / "intent", platform="codex", session_id="session-1",
            input_event_id="event-1", run_dir=self.run_dir, current_work_item_id="session-intent-session-1",
            run_id="replacement-run", plan_path=str(self.root / ".tmp/implementation-plans/autopilot-session-intent.md"),
            source=self.source, bind_completion_contract=True,
            approval_evidence={"decision":"approved","source":"user-authorized-current-input"})
        self.assertIn("prospective-origin: stale", self.stop()["systemMessage"])
        fresh = self.capture()
        self.assertNotEqual(fresh["origin_token"], origin["origin_token"])
        self.assertNotEqual(fresh["captured_at"], origin["captured_at"])
        self.assertEqual(fresh, self.capture())
        self.stop()
        with adapter.storage.transaction(self.run_dir, self.source) as store:
            receipt = self.helper._receipt_output(store.read(self.helper.PUBLICATION_OBJECT))
        with self.assertRaises(ValueError): self.publish(receipt, completion_check=old_proof, verified_at=old_time)
        self.proof(); self.publish(receipt); self.stop()
        self.assertEqual({r["status"] for r in self.read_state()["acceptance_criteria"]}, {"met"})

    def test_prospective_origin_binds_only_first_generation_then_new_capture_recovers(self):
        self.generation_lifecycle(False)

    def test_existing_generation_origin_rotates_for_later_authorized_generation(self):
        self.generation_lifecycle(True)

    def test_origin_generation_binding_and_publication_roll_back_together(self):
        self.capture(); self.proof()
        original = adapter.storage.RunStore.write
        def fail(store, name, value):
            if name == self.helper.PUBLICATION_OBJECT: raise ValueError("injected publication failure")
            return original(store, name, value)
        with mock.patch.object(adapter.storage.RunStore, "write", fail):
            with self.assertRaisesRegex(ValueError, "injected publication failure"): self.stop()
        with closing(sqlite3.connect(self.root / "intent/ghost-state.sqlite3")) as conn:
            self.assertTrue(conn.execute("SELECT name FROM sqlite_master WHERE name='ap_completion_origin_bindings'").fetchone(), "missing immutable generation association storage")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM ap_completion_origin_bindings").fetchone()[0], 0)
        self.stop()
        with adapter.storage.transaction(self.run_dir, self.source) as store:
            receipt = self.helper._receipt_output(store.read(self.helper.PUBLICATION_OBJECT))
        self.publish(receipt); self.stop()
        self.assertEqual({r["status"] for r in self.read_state()["acceptance_criteria"]}, {"met"})

    def test_firing_hook_identity_survives_stale_native_environment(self):
        self.core.record_turn(root=self.root / "intent", platform="codex", session_id="other-session",
            raw_user_input="Other task", intent_delta={"current_goal": "Other task", "acceptance_criteria": self.read_state()["acceptance_criteria"]})
        self.source["CODEX_THREAD_ID"] = "other-session"
        origin = self.capture()
        with closing(sqlite3.connect(self.root / "intent/ghost-state.sqlite3")) as conn:
            body = json.loads(conn.execute("SELECT body_json FROM ap_completion_origins WHERE token=?", (origin["origin_token"],)).fetchone()[0])
        self.assertEqual(body["contract"]["session_id"], "session-1")
        self.proof(); self.stop()
        with adapter.storage.transaction(self.run_dir, dict(self.source, CODEX_THREAD_ID="session-1")) as store:
            publication = store.read(self.helper.PUBLICATION_OBJECT)
        self.assertEqual(publication["receipt"]["contract"]["session_id"], "session-1")
        self.source["CODEX_THREAD_ID"] = "session-1"
        self.publish(self.helper._receipt_output(publication)); self.stop()
        other = self.core.read_session_state(root=self.root / "intent", platform="codex", session_id="other-session")
        self.assertEqual({r["status"] for r in other["acceptance_criteria"]}, {"unmet"})

    def test_changed_same_input_semantics_get_fresh_capture_for_later_admission(self):
        self.capture(); old_proof = self.proof(); old_time = self.verified_at; self.stop()
        self.change_intent({"constraints":["New authorized restriction"]})
        origin = self.capture(); original = self.proof()
        self.assertIn("completion-admission: required", self.stop()["systemMessage"])
        receipt = self.helper.prepare_completion(**self.arguments, reapprove_current_input=True)
        self.assertEqual(receipt["prepared_at"], origin["captured_at"])
        with self.assertRaises(ValueError): self.publish(receipt, completion_check=old_proof, verified_at=old_time)
        self.publish(receipt); self.stop()
        self.assertEqual(adapter.read_work_items(self.run_dir / "tasks.jsonl")[0]["completion"]["evidence"], [original])

    def make_legacy_run(self):
        sys.path.insert(0, str(SKILL / "scripts"))
        from autopilot_session_bridge import bridge_session_intent_to_run_state
        bridge_session_intent_to_run_state(intent_root=self.root / "intent", platform="codex", session_id="session-1",
            input_event_id="event-1", run_dir=self.run_dir, current_work_item_id="session-intent-session-1",
            run_id="legacy-run", plan_path=str(self.root / ".tmp/implementation-plans/autopilot-session-intent.md"),
            source=self.source, approval_evidence={"decision":"approved","source":"user-authorized-current-input"})

    def test_legacy_publication_snapshot_permits_changed_semantics_new_capture(self):
        self.make_legacy_run()
        self.test_changed_same_input_semantics_get_fresh_capture_for_later_admission()

    def test_legacy_without_publication_keeps_exact_generation(self):
        self.make_legacy_run()
        self.test_existing_normal_run_generation_cannot_be_replaced_after_capture()

    def test_existing_normal_run_generation_cannot_be_replaced_after_capture(self):
        adapter._bootstrap_from_session_intent_if_approved(self.run_dir, self.source, self.root)
        origin = self.capture(); self.proof()
        with closing(sqlite3.connect(self.root / "intent/ghost-state.sqlite3")) as conn:
            body = json.loads(conn.execute("SELECT body_json FROM ap_completion_origins WHERE token=?", (origin["origin_token"],)).fetchone()[0])
        self.assertIsNotNone(body["existing_generation"])
        sys.path.insert(0, str(SKILL / "scripts"))
        from autopilot_session_bridge import bridge_session_intent_to_run_state
        bridge_session_intent_to_run_state(intent_root=self.root / "intent", platform="codex", session_id="session-1",
            input_event_id="event-1", run_dir=self.run_dir, current_work_item_id="session-intent-session-1",
            run_id="replacement-run", plan_path=str(self.root / ".tmp/implementation-plans/autopilot-session-intent.md"),
            source=self.source, approval_evidence={"decision":"approved","source":"user-authorized-current-input"})
        self.assertIn("prospective-origin: stale", self.stop()["systemMessage"])
        with adapter.storage.transaction(self.run_dir, self.source) as store:
            with self.assertRaises(FileNotFoundError): store.read(self.helper.PUBLICATION_OBJECT)

    def test_no_current_authority_skips_without_creating_state(self):
        shutil.rmtree(self.root / "intent")
        source = {key: value for key, value in self.source.items()
                  if key != "GHOST_ALICE_SESSION_INTENT_ROOT"}
        self.assertEqual(self.capture(source)["status"], "skipped")
        self.assertFalse((self.root / ".tmp").exists())
        self.assertFalse(self.run_dir.exists())

    def test_foreign_session_database_does_not_require_current_publication(self):
        shutil.rmtree(self.root / "intent")
        foreign = self.root / ".tmp/session-intent"
        self.core.record_turn(root=foreign, platform="codex", session_id="foreign-session",
            raw_user_input="Unrelated test session")
        before = self.core.read_session_state(root=foreign, platform="codex",
            session_id="foreign-session")
        source = {key: value for key, value in self.source.items()
                  if key != "GHOST_ALICE_SESSION_INTENT_ROOT"}
        self.assertEqual(self.capture(source)["status"], "skipped")
        self.assertEqual(before, self.core.read_session_state(root=foreign, platform="codex",
            session_id="foreign-session"))
        self.assertFalse(self.run_dir.exists())

    def test_runtime_surface_change_rejects_original_capture(self):
        self.capture(); self.proof()
        self.source["GHOST_ALICE_AUTOPILOT_PLAN_PATH"] = str(self.root / "new-plan.md")
        self.assertIn("prospective-origin: stale", self.stop()["systemMessage"])
        with adapter.storage.transaction(self.run_dir, self.source) as store:
            with self.assertRaises(FileNotFoundError): store.read(self.helper.PUBLICATION_OBJECT)

    def test_hook_resolves_normal_current_session_root_without_explicit_coordinates(self):
        normal = self.root / ".tmp/session-intent"
        normal.parent.mkdir(); shutil.move(str(self.root / "intent"), normal)
        source = {key: value for key, value in self.source.items()
                  if key not in {"GHOST_ALICE_SESSION_INTENT_ROOT", "GHOST_ALICE_SESSION_ID"}}
        result = self.capture(source)
        self.assertEqual(result["status"], "prospective")
        self.assertFalse(self.run_dir.exists())
        with closing(sqlite3.connect(normal / "ghost-state.sqlite3")) as conn:
            body = json.loads(conn.execute("SELECT body_json FROM ap_completion_origins").fetchone()[0])
        self.assertEqual(body["contract"]["intent_root"], str(normal.resolve()))
        self.assertEqual(body["contract"]["session_id"], "session-1")

    def test_next_input_reapproval_promotes_original_prospective_capture(self):
        self.capture(); self.proof(); self.stop()
        with adapter.storage.transaction(self.run_dir, self.source) as store:
            first = self.helper._receipt_output(store.read(self.helper.PUBLICATION_OBJECT))
        self.publish(first); self.stop()
        self.change_intent({"acceptance_criteria": [dict(row, status="unmet", summary=row["summary"] + " for new input")
            for row in self.read_state()["acceptance_criteria"]]}, new_input=True)
        origin = self.capture(); original = self.proof()
        self.assertIn("completion-admission: required", self.stop().get("systemMessage", ""))
        arguments = dict(self.arguments, input_event_id=self.read_state()["latest_input_event_id"])
        receipt = self.helper.prepare_completion(**arguments, reapprove_current_input=True)
        self.assertEqual(receipt["prepared_at"], origin["captured_at"])
        self.assertNotEqual(receipt["receipt_token"], first["receipt_token"])
        self.publish(receipt); self.stop()
        item = adapter.read_work_items(self.run_dir / "tasks.jsonl")[0]
        self.assertEqual(item["status"], "completed")
        self.assertEqual(item["completion"]["evidence"], [original])
        self.assertEqual({row["status"] for row in self.read_state()["acceptance_criteria"]}, {"met"})

    def test_installed_codex_pretool_command_captures_before_single_verification(self):
        platform = self.source["GHOST_ALICE_PLATFORM"]
        core_root = Path(self.source["GHOST_ALICE_CORE_ROOT"])
        sys.path.insert(0, str(core_root / "_shared"))
        import install_hooks
        home = self.root / "home"; home.mkdir()
        (home / ".codex").mkdir()
        (home / ".claude").mkdir()
        installed = home / ".agents/skills/autopilot-mode"
        shutil.copytree(SKILL, installed)
        env = {"HOME": str(home), "CODEX_HOME": str(home / ".codex"),
            "PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1", **self.source}
        with mock.patch.dict(os.environ, env, clear=True):
            install_hooks.install_hook(platform, addon_sources=[str(ROOT)], skills_dir=installed.parent)
        config = home / (".codex/hooks.json" if platform == "codex" else ".claude/settings.json")
        hooks = json.loads(config.read_text())["hooks"]
        commands = [h["command"] for entry in hooks.get("PreToolUse", []) for h in entry.get("hooks", [])
                    if "[adapter:autopilot-mode] prepare-origin" in h.get("command", "")]
        self.assertEqual(len(commands), 1, "official install omitted prospective capture hook")
        self.assertIn("[hook-runner:adapter-autopilot-mode-prepare-origin]", commands[0])
        payload = json.dumps({"hook_event_name": "PreToolUse", "session_id": "session-1", "cwd": str(self.root)})
        result = subprocess.run(commands[0], shell=True, cwd=self.root, env=env, input=payload,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"continue": true', result.stdout)
        self.assertFalse(self.run_dir.exists())
        counter = self.root / "verification-count"
        subprocess.run([sys.executable, "-B", "-c",
            "from pathlib import Path; p=Path('verification-count'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1'); assert p.read_text()=='1'"],
            check=True, cwd=self.root, env=env)
        original = self.proof()
        self.assertIn("completion-publication: missing", self.stop()["systemMessage"])
        with adapter.storage.transaction(self.run_dir, self.source) as store:
            receipt = self.helper._receipt_output(store.read(self.helper.PUBLICATION_OBJECT))
        publish = subprocess.run([sys.executable, "-B", str(installed / "scripts/autopilot_completion.py"),
            "publish", "--receipt-token", receipt["receipt_token"], "--verified-at", self.verified_at,
            "--evidence-source", self.evidence_source, "--completion-file", "-"],
            input=original, text=True, capture_output=True, cwd=self.root, env=env)
        self.assertEqual(publish.returncode, 0, publish.stderr)
        self.stop()
        self.assertEqual(counter.read_text(), "1")
        self.assertEqual(adapter.read_work_items(self.run_dir / "tasks.jsonl")[0]["status"], "completed")
        state = self.core.read_session_state(root=self.root / "intent", platform=platform, session_id="session-1")
        self.assertEqual({row["status"] for row in state["acceptance_criteria"]}, {"met"})
        self.assertEqual(adapter.read_work_items(self.run_dir / "tasks.jsonl")[0]["completion"]["evidence"], [original])


    def test_official_intake_default_root_and_pretool_without_custom_coordinates(self):
        core_root = Path(self.source["GHOST_ALICE_CORE_ROOT"])
        sys.path.insert(0, str(core_root / "_shared")); import install_hooks
        home = self.root / "ordinary-home"; (home / ".codex").mkdir(parents=True)
        runtime = home / ".ghost-alice/runtime/current"
        for name in ("_shared", "session-intent-analyzer", "skill-catalog"):
            shutil.copytree(core_root / name, runtime / name)
        shutil.copyfile(core_root / "install.sh", runtime / "install.sh")
        shutil.copyfile(core_root / "VERSION", runtime / "VERSION")
        installed = home / ".agents/skills/autopilot-mode"; shutil.copytree(SKILL, installed)
        env = {"HOME": str(home), "CODEX_HOME": str(home / ".codex"),
            "PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
        installation = subprocess.run([sys.executable, "-B", str(runtime / "_shared/install_hooks.py"),
            "--platform", "codex", "--addon-source", str(ROOT), "--skills-dir", str(installed.parent)],
            cwd=self.root, env=env, text=True, capture_output=True)
        self.assertEqual(installation.returncode, 0, installation.stderr + installation.stdout)
        hooks = json.loads((home / ".codex/hooks.json").read_text())["hooks"]
        def command(event, marker):
            return next(h["command"] for row in hooks[event] for h in row.get("hooks", []) if marker in h.get("command", ""))
        intake = subprocess.run(command("UserPromptSubmit", "[hook-runner:session-intent]"), shell=True,
            cwd=self.root, env=env, input=json.dumps({"hook_event_name":"UserPromptSubmit", "session_id":"session-1", "cwd":str(self.root), "prompt":"Write and verify report"}), text=True, capture_output=True)
        self.assertEqual(intake.returncode, 0, intake.stderr)
        intent_root = runtime / ".tmp/session-intent"
        self.assertTrue((intent_root / "ghost-state.sqlite3").is_file(), intake.stdout)
        observed = self.core.read_session_state(root=intent_root, platform="codex", session_id="session-1")
        self.core.record_turn(root=intent_root, platform="codex", session_id="session-1",
            expected_input_event_id=observed["latest_input_event_id"], intent_delta={"current_goal":"Write and verify the requested report.", "acceptance_criteria":self.read_state()["acceptance_criteria"]})
        pretool = subprocess.run(command("PreToolUse", "[adapter:autopilot-mode] prepare-origin"), shell=True,
            cwd=self.root, env=env, input=json.dumps({"hook_event_name":"PreToolUse", "session_id":"session-1", "cwd":str(self.root)}), text=True, capture_output=True)
        self.assertEqual(pretool.returncode, 0, pretool.stderr)
        with closing(sqlite3.connect(intent_root / "ghost-state.sqlite3")) as conn:
            self.assertTrue(conn.execute("SELECT name FROM sqlite_master WHERE name='ap_completion_origins'").fetchone(), pretool.stdout)
        self.assertFalse(self.run_dir.exists())
        self.source.update(GHOST_ALICE_CORE_ROOT=str(runtime), GHOST_ALICE_SESSION_INTENT_ROOT=str(intent_root), HOME=str(home))
        counter = self.root / "normal-check-count"; counter.write_text("1")
        self.assertEqual(counter.read_text(), "1"); original = self.proof()
        def installed_stop():
            result = subprocess.run(command("Stop", "[adapter:autopilot-mode] continue"), shell=True,
                cwd=self.root, env=env, input=json.dumps({"hook_event_name":"Stop", "session_id":"session-1", "cwd":str(self.root)}), text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertIn("completion-publication: missing", installed_stop()["systemMessage"])
        with adapter.storage.transaction(self.run_dir, self.source) as store:
            receipt = self.helper._receipt_output(store.read(self.helper.PUBLICATION_OBJECT))
        self.publish(receipt); installed_stop()
        self.assertEqual(counter.read_text(), "1")
        item = adapter.read_work_items(self.run_dir / "tasks.jsonl")[0]
        self.assertEqual(item["status"], "completed"); self.assertEqual(item["completion"]["evidence"], [original])
        state = self.core.read_session_state(root=intent_root, platform="codex", session_id="session-1")
        self.assertEqual({row["status"] for row in state["acceptance_criteria"]}, {"met"})

    def test_installed_claude_pretool_command_captures_before_single_verification(self):
        self.core.record_turn(root=self.root / "intent", platform="claude", session_id="session-1",
            raw_user_input="Write the report", intent_delta={"current_goal": "Write and verify the requested report.",
                "acceptance_criteria": self.read_state()["acceptance_criteria"]})
        self.source["GHOST_ALICE_PLATFORM"] = "claude"
        self.test_installed_codex_pretool_command_captures_before_single_verification()


if __name__ == "__main__": unittest.main()
