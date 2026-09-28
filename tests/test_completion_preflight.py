"""Early, once-per-origin publication guidance through real pretool adapters."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import copy
import importlib.util
import inspect
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys
import unittest
from unittest import mock

import test_completion_publication as fixtures

ROOT, SKILL, adapter = fixtures.ROOT, fixtures.SKILL, fixtures.adapter


class CompletionPreflightTest(unittest.TestCase):
    read_state = fixtures.CompletionPublicationTest.read_state
    change_intent = fixtures.CompletionPublicationTest.change_intent
    proof = fixtures.CompletionPublicationTest.proof

    def setUp(self):
        fixtures.CompletionPublicationTest.setUp(self)
        self.core.migrate_session(root=self.root / "intent", platform="codex", session_id="session-1")
        self.pretool = SKILL / "adapters/autopilot_pretool.py"

    def environment(self, **changes):
        env = {**os.environ, **self.source, "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("CODEX_THREAD_ID", None)
        env.pop("CLAUDE_PROJECT_DIR", None)
        env.update(changes)
        return env

    def hook(self, *, env=None, payload=None):
        result = subprocess.run([sys.executable, "-B", str(self.pretool)],
            input=json.dumps(payload or {"hook_event_name": "PreToolUse", "session_id": "session-1",
                "cwd": str(self.root), "tool_name": "Bash", "tool_input": {"command": "inspect-business"}}),
            text=True, capture_output=True, cwd=self.root, env=env or self.environment())
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def context(self, payload):
        return payload.get("hookSpecificOutput", {}).get("additionalContext", "")

    def notice(self, payload):
        context = self.context(payload)
        self.assertTrue(context, "first business tool received no actionable completion preflight")
        self.assertIs(payload.get("continue"), True)
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertNotIn("permissionDecision", payload["hookSpecificOutput"])
        self.assertIn("before the first final response", context)
        self.assertIn("original verification time", context)
        self.assertIn("returned tool output", context)
        self.assertIn("do not rerun", context)
        self.assertIn("store", context)
        return context

    def prepare_command(self, context):
        commands = [line for line in context.splitlines()
                    if line.startswith("env ") and "autopilot_completion.py" in line]
        self.assertEqual(len(commands), 1)
        args = shlex.split(commands[0])
        self.assertIn("prepare", args)
        self.assertIn("--reapprove-current-input", args)
        return args

    def database_rows(self, table):
        with closing(sqlite3.connect(self.root / "intent/ghost-state.sqlite3")) as connection:
            if not connection.execute("SELECT name FROM sqlite_master WHERE name=?", (table,)).fetchone():
                return []
            return connection.execute("SELECT * FROM " + table).fetchall()

    def test_first_tool_notifies_once_without_admission_or_permission_classification(self):
        before = self.read_state()
        context = self.notice(self.hook())
        command = self.prepare_command(context)
        for option, value in (("--intent-root", str((self.root / "intent").resolve())),
                              ("--platform", "codex"), ("--session-id", "session-1"),
                              ("--input-event-id", "event-1")):
            self.assertEqual(command[command.index(option) + 1], value)
        self.assertEqual(self.hook(payload={"hook_event_name": "PreToolUse", "session_id": "session-1",
            "cwd": str(self.root), "tool_name": "UnknownTool", "tool_input": {"command": "publish or delete"}}),
            {"continue": True})
        self.assertEqual(before, self.read_state())
        self.assertFalse(self.run_dir.exists())
        self.assertEqual(len(self.database_rows("ap_completion_origins")), 1)
        self.assertEqual(len(self.database_rows("ap_completion_preflight_notices")), 1)
        self.assertEqual(self.database_rows("ap_runs"), [])

    def test_new_input_and_changed_contract_each_receive_one_new_notice(self):
        first = self.notice(self.hook())
        self.change_intent(new_input=True)
        second = self.notice(self.hook())
        command = self.prepare_command(second)
        self.assertEqual(command[command.index("--input-event-id") + 1], self.read_state()["latest_input_event_id"])
        self.assertNotEqual(first, second)
        self.assertEqual(self.hook(), {"continue": True})
        self.change_intent({"constraints": ["Only the current approved report"]})
        third = self.notice(self.hook())
        self.assertNotEqual(second, third)
        self.assertEqual(self.hook(), {"continue": True})
        self.assertEqual(len(self.database_rows("ap_completion_origins")), 3)
        self.assertEqual(len(self.database_rows("ap_completion_preflight_notices")), 3)

    def test_blocked_off_unsupported_and_no_unmet_admitted_criteria_stay_silent(self):
        original = self.read_state()["acceptance_criteria"]
        self.change_intent({"model_security_decision": {"decision": "block", "input_event_id": "event-1",
            "reason": "Current block", "risk_flags": []}})
        self.assertEqual(self.hook(), {"continue": True})
        self.change_intent({"model_security_decision": {"decision": "allow", "input_event_id": "event-1",
            "reason": "Resolved", "risk_flags": []}})
        self.run_dir.mkdir(); (self.run_dir / "OFF").write_text("")
        self.assertEqual(self.hook(), {"continue": True})
        (self.run_dir / "OFF").unlink(); self.run_dir.rmdir()
        self.assertEqual(self.hook(env=self.environment(GHOST_ALICE_PLATFORM="agent-runtime")), {"continue": True})
        self.change_intent({"acceptance_criteria": [dict(row, admitted=False) for row in original]})
        self.assertEqual(self.hook(), {"continue": True})
        self.assertEqual(self.database_rows("ap_completion_preflight_notices"), [])
        self.assertEqual(self.database_rows("ap_completion_origins"), [])
        self.change_intent({"acceptance_criteria": original})
        self.notice(self.hook())
        self.assertEqual(self.database_rows("ap_runs"), [])
        receipt = self.helper.prepare_completion(**self.arguments)
        self.proof()
        fixtures.CompletionPublicationTest.publish(self, receipt)
        fixtures.CompletionPublicationTest.stop(self)
        self.assertEqual({row["status"] for row in self.read_state()["acceptance_criteria"]}, {"met"})
        self.assertEqual(self.hook(), {"continue": True})

    def test_firing_identity_is_bound_but_native_mismatch_guard_is_not_bypassed(self):
        self.core.record_turn(root=self.root / "intent", platform="codex", session_id="other-session",
            raw_user_input="Other task", intent_delta={"current_goal": "Other task",
                "acceptance_criteria": self.read_state()["acceptance_criteria"]})
        stale_env = self.environment(CODEX_THREAD_ID="other-session", GHOST_ALICE_SESSION_ID="other-session")
        context = self.notice(self.hook(env=stale_env))
        command = self.prepare_command(context)
        self.assertEqual(command[command.index("--session-id") + 1], "session-1")
        self.assertFalse(any(arg.startswith("CODEX_THREAD_ID=") for arg in command))
        rejected = subprocess.run(command, cwd=self.root, env=stale_env, text=True, capture_output=True)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("disagrees with current native session", rejected.stderr)
        origin = json.loads(self.database_rows("ap_completion_origins")[0][1])
        self.assertEqual(origin["contract"]["session_id"], "session-1")
        self.assertEqual(self.database_rows("ap_runs"), [])

    def test_notice_claim_rechecks_contract_in_same_transaction(self):
        spec = importlib.util.spec_from_file_location("completion_preflight_under_test", self.pretool)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        self.assertIn("preflight", inspect.signature(module.capture_before_tool).parameters,
                      "pretool has no atomic capture-and-notice path")
        import autopilot_provenance as provenance
        original = provenance._locked_state
        def changed(*args, **kwargs):
            state = original(*args, **kwargs)
            return dict(state, latest_input_event_id="different-input")
        with mock.patch.object(provenance, "_locked_state", changed):
            with self.assertRaisesRegex(ValueError, "changed"):
                module.capture_before_tool(self.source, {"hook_event_name": "PreToolUse", "session_id": "session-1",
                    "cwd": str(self.root)}, preflight=True)
        self.assertEqual(self.database_rows("ap_completion_preflight_notices"), [])
        self.assertEqual(self.database_rows("ap_completion_origins"), [])
        self.notice(self.hook())

    def test_concurrent_tools_share_one_atomic_notice_claim(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            outputs = list(pool.map(lambda _: self.hook(), range(6)))
        contexts = [self.context(output) for output in outputs if self.context(output)]
        self.assertEqual(len(contexts), 1, "concurrent pretools must deliver exactly one origin notice")
        self.assertEqual(len(self.database_rows("ap_completion_origins")), 1)
        self.assertEqual(len(self.database_rows("ap_completion_preflight_notices")), 1)
        self.assertEqual(self.database_rows("ap_runs"), [])

    def test_prepare_returns_concrete_publication_command_without_changing_receipt(self):
        receipt = self.helper.prepare_completion(**self.arguments)
        self.assertIn("publish_command", receipt, "prepare left publication deferred to Stop recovery")
        command = shlex.split(receipt["publish_command"])
        for option, value in (("--receipt-token", receipt["receipt_token"]),
                              ("--verified-at", "ORIGINAL_ISO_TIME"), ("--completion-file", "-")):
            self.assertEqual(command[command.index(option) + 1], value)
        self.assertEqual(receipt, self.helper.prepare_completion(**self.arguments, reapprove_current_input=True))
        with adapter.storage.transaction(self.run_dir, self.source) as store:
            stored = store.read(self.helper.PUBLICATION_OBJECT)
        self.assertNotIn("publish_command", stored)
        self.assertNotIn("publish_command", stored["receipt"])
        self.assertEqual(stored["receipt_token"], self.helper._digest(stored["receipt"]))

    def conduct_feedback(self):
        self.change_intent({"conduct_feedback": [{"id": "active-control", "status": "open",
            "occurrence_count": 2, "summary": "Retain prior conduct feedback while completing the requested report"}]})

    def legacy_partial_conduct_run(self):
        """Seed the exact old prepare shape: bound scope, plans, no tasks."""
        self.conduct_feedback()
        sys.path.insert(0, str(SKILL / "scripts"))
        from autopilot_session_bridge import bridge_session_intent_to_run_state
        result = bridge_session_intent_to_run_state(intent_root=self.root / "intent", platform="codex",
            session_id="session-1", input_event_id=self.read_state()["latest_input_event_id"], run_dir=self.run_dir,
            current_work_item_id="session-intent-session-1",
            plan_path=str(self.root / ".tmp/implementation-plans/autopilot-session-intent.md"),
            source=self.source, approval_evidence={"decision": "approved", "source": "user-authorized-current-input"})
        self.assertEqual(result["mode"], "conduct-plan", "ordinary conduct dispatch must remain available")
        with adapter.storage.transaction(self.run_dir, self.source) as store:
            run = store.run()
            objects = {name: store.read(name) for name in ("conduct-plan.candidate.json", "conduct-plan.json")}
            run["scope"]["completion_contract"] = {
                key: copy.deepcopy(self.read_state().get(key, [])) for key in ("constraints", "non_goals", "decisions")}
            run["approval_generation"] = adapter.SESSION_MATERIAL.approval_generation(run)
            store.put_run(run)
            for name, value in objects.items():
                value["approval_generation"] = run["approval_generation"]
                store.write(name, value)
            with self.assertRaises(FileNotFoundError):
                store.read("tasks.jsonl")
        return run, objects

    def assert_canonical_publication_task(self, receipt):
        self.assertEqual(receipt["criterion_ids"], ["protected", "report"])
        with adapter.storage.transaction(self.run_dir, self.source) as store:
            run = store.run(); tasks = store.read("tasks.jsonl")
            self.assertEqual(run["scope"].get("publication_mode"), "session-intent-task")
            self.assertEqual([task["id"] for task in tasks], ["session-intent-session-1"])
            self.assertEqual(tasks[0]["acceptance_criteria"],
                adapter.SESSION_MATERIAL.acceptance_criteria_from_intent(self.read_state()))
            with self.assertRaises(FileNotFoundError):
                store.read("conduct-plan.json")
        self.assertEqual(self.read_state()["conduct_feedback"][0]["status"], "open")

    def test_explicit_publication_admits_requested_criteria_despite_advisory_feedback(self):
        self.conduct_feedback()
        context = self.notice(self.hook())
        command = self.prepare_command(context)
        result = subprocess.run(command, env=self.environment(), cwd=self.root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, "explicit publication was replaced by a conduct-only plan: " + result.stderr)
        receipt = json.loads(result.stdout)
        self.assert_canonical_publication_task(receipt)
        self.proof(); fixtures.CompletionPublicationTest.publish(self, receipt)
        self.assertEqual(fixtures.CompletionPublicationTest.stop(self)["systemMessage"], "")

    def test_failed_legacy_conduct_only_admission_is_archived_and_unbound_origin_retained(self):
        previous, objects = self.legacy_partial_conduct_run()
        self.notice(self.hook())
        origin = json.loads(self.database_rows("ap_completion_origins")[0][1])
        self.proof()
        try:
            receipt = self.helper.prepare_completion(**self.arguments, reapprove_current_input=True)
        except (ValueError, OSError) as exc:
            self.fail(f"current legacy conduct-only admission remained unpublishable: {exc}")
        self.assertNotEqual(receipt["approval_generation"], previous["approval_generation"])
        self.assertEqual(receipt["prepared_at"], origin["captured_at"])
        self.assert_canonical_publication_task(receipt)
        with adapter.storage.transaction(self.run_dir, self.source) as store:
            old = store.connection.execute("SELECT run_json FROM ap_generations WHERE run_key=? AND generation=?",
                (store.key, previous["approval_generation"])).fetchone()
            self.assertEqual(json.loads(old[0]), previous)
            for name, value in objects.items():
                row = store.connection.execute("SELECT body_json FROM ap_objects WHERE run_key=? AND generation=? AND name=?",
                    (store.key, previous["approval_generation"], name)).fetchone()
                self.assertEqual(json.loads(row[0]), value)
        fixtures.CompletionPublicationTest.publish(self, receipt)
        self.assertEqual(fixtures.CompletionPublicationTest.stop(self)["systemMessage"], "")

    def test_conduct_reapproval_never_reuses_a_bound_origin(self):
        previous, _ = self.legacy_partial_conduct_run()
        self.notice(self.hook())
        token = self.database_rows("ap_completion_origins")[0][0]
        with adapter.storage.transaction(self.run_dir, self.source) as store:
            store.connection.execute("INSERT INTO ap_completion_origin_bindings VALUES(?,?)",
                (token, previous["approval_generation"]))
        with self.assertRaisesRegex(ValueError, "prospective origin is stale"):
            self.helper.prepare_completion(**self.arguments, reapprove_current_input=True)
        self.assertEqual(self.database_rows("ap_completion_origin_bindings"), [(token, previous["approval_generation"])])

    def test_conduct_reapproval_does_not_invent_historical_semantics_for_summary_only_run(self):
        previous, _ = self.legacy_partial_conduct_run()
        with adapter.storage.transaction(self.run_dir, self.source) as store:
            previous["scope"] = {"summary": previous["scope"]["summary"]}
            previous["approval_generation"] = adapter.SESSION_MATERIAL.approval_generation(previous)
            store.put_run(previous)
        self.notice(self.hook())
        with self.assertRaisesRegex(ValueError, "prospective origin is stale"):
            self.helper.prepare_completion(**self.arguments, reapprove_current_input=True)

    def test_conduct_reapproval_rejects_changed_input_scope_and_criterion_origin(self):
        self.legacy_partial_conduct_run()
        self.notice(self.hook())
        self.change_intent({"constraints": ["Changed contract after original capture"]})
        with self.assertRaisesRegex(ValueError, "prospective origin is stale"):
            self.helper.prepare_completion(**self.arguments, reapprove_current_input=True)
        self.change_intent(new_input=True)
        arguments = dict(self.arguments, input_event_id=self.read_state()["latest_input_event_id"])
        with self.assertRaisesRegex(ValueError, "prospective origin is stale"):
            self.helper.prepare_completion(**arguments, reapprove_current_input=True)
        self.change_intent({"acceptance_criteria": [dict(row, summary=row["summary"] + " revised")
            for row in self.read_state()["acceptance_criteria"]]})
        with self.assertRaisesRegex(ValueError, "prospective origin is stale"):
            self.helper.prepare_completion(**arguments, reapprove_current_input=True)
        self.assertEqual(self.database_rows("ap_completion_origin_bindings"), [])

    def reject_used_conduct_origin(self, prior_object):
        prior, _ = self.legacy_partial_conduct_run()
        self.notice(self.hook())
        with adapter.storage.transaction(self.run_dir, self.source) as store:
            value = [adapter.SESSION_MATERIAL.session_intent_task(intent_state=self.read_state(),
                session_id="session-1", allowed_surfaces=prior["allowed_surfaces"])] if prior_object == "tasks.jsonl" else {"previous": "used"}
            store.write(prior_object, value)
        from autopilot_session_bridge import bridge_session_intent_to_run_state
        bridge_session_intent_to_run_state(intent_root=self.root / "intent", platform="codex",
            session_id="session-1", input_event_id=self.read_state()["latest_input_event_id"], run_dir=self.run_dir,
            current_work_item_id="session-intent-session-1", plan_path=prior["allowed_surfaces"][0],
            source=self.source, approval_evidence={"decision": "approved", "source": "user-authorized-current-input"},
            bind_completion_contract=True)
        with self.assertRaisesRegex(ValueError, "prospective origin is stale"):
            self.helper.prepare_completion(**self.arguments)

    def test_conduct_reapproval_does_not_reuse_prior_task_generation(self):
        self.reject_used_conduct_origin("tasks.jsonl")

    def test_conduct_reapproval_does_not_reuse_prior_publication_generation(self):
        self.reject_used_conduct_origin(self.helper.PUBLICATION_OBJECT)

    def test_conduct_reapproval_does_not_reuse_prior_decision_generation(self):
        self.reject_used_conduct_origin("consistency-decision.json")

    def reapprove_changed_unpublished_scope(self, delta=None, *, source_changes=None):
        sys.path.insert(0, str(SKILL / "scripts"))
        from autopilot_session_bridge import bridge_session_intent_to_run_state
        previous = bridge_session_intent_to_run_state(intent_root=self.root / "intent", platform="codex",
            session_id="session-1", input_event_id="event-1", run_dir=self.run_dir,
            current_work_item_id="session-intent-session-1",
            plan_path=str(self.root / ".tmp/implementation-plans/autopilot-session-intent.md"), source=self.source,
            approval_evidence={"decision": "approved", "source": "user-authorized-current-input"}, bind_completion_contract=True)
        with adapter.storage.transaction(self.run_dir, self.source) as store:
            previous_run = store.run()
            with self.assertRaises(FileNotFoundError):
                store.read(self.helper.PUBLICATION_OBJECT)
        old_proof = self.proof(); old_time = self.verified_at
        if delta is not None:
            self.change_intent(delta)
        if source_changes:
            self.source.update(source_changes)
        self.notice(self.hook())
        try:
            receipt = self.helper.prepare_completion(**self.arguments, reapprove_current_input=True)
        except (ValueError, OSError) as exc:
            self.fail(f"fresh current-contract capture could not reapprove changed semantic scope: {exc}")
        self.assertNotEqual(receipt["approval_generation"], previous["approval_generation"])
        with adapter.storage.transaction(self.run_dir, self.source) as store:
            archived = store.connection.execute("SELECT run_json FROM ap_generations WHERE run_key=? AND generation=?",
                (store.key, previous["approval_generation"])).fetchone()
            self.assertEqual(json.loads(archived[0]), previous_run)
        with self.assertRaises(ValueError):
            fixtures.CompletionPublicationTest.publish(self, receipt, completion_check=old_proof, verified_at=old_time)
        self.proof(); fixtures.CompletionPublicationTest.publish(self, receipt)
        self.assertEqual(fixtures.CompletionPublicationTest.stop(self)["systemMessage"], "")

    def test_unpublished_canonical_task_reapproves_changed_constraints(self):
        self.reapprove_changed_unpublished_scope({"constraints": ["New authorized restriction"]})

    def test_unpublished_canonical_task_reapproves_changed_non_goals(self):
        self.reapprove_changed_unpublished_scope({"non_goals": ["Keep unrelated files unchanged"]})

    def test_unpublished_canonical_task_reapproves_changed_decisions(self):
        self.reapprove_changed_unpublished_scope({"decisions": [{"id": "current", "summary": "Use the current report"}]})

    def test_unpublished_canonical_task_reapproves_changed_latest_scope(self):
        self.reapprove_changed_unpublished_scope({"latest_scope": {"focus_layer": "micro", "allowed": ["current report"]}})

    def test_unpublished_canonical_task_reapproves_changed_allowed_surface(self):
        self.reapprove_changed_unpublished_scope(source_changes={
            "GHOST_ALICE_AUTOPILOT_PLAN_PATH": str(self.root / "current-approved-plan.md")})

    def ordinary_summary_run(self):
        sys.path.insert(0, str(SKILL / "scripts"))
        from autopilot_session_bridge import bridge_session_intent_to_run_state
        result = bridge_session_intent_to_run_state(intent_root=self.root / "intent", platform="codex",
            session_id="session-1", input_event_id="event-1", run_dir=self.run_dir,
            current_work_item_id="session-intent-session-1",
            plan_path=str(self.root / ".tmp/implementation-plans/autopilot-session-intent.md"), source=self.source,
            approval_evidence={"decision": "approved", "source": "user-authorized-current-input"})
        self.assertEqual(result["mode"], "session-intent-task")
        with adapter.storage.transaction(self.run_dir, self.source) as store:
            run = store.run()
        self.assertEqual(set(run["scope"]), {"summary"})
        return run

    def test_unchanged_ordinary_admission_keeps_original_generation_and_origin(self):
        previous = self.ordinary_summary_run()
        self.notice(self.hook())
        self.proof()
        try:
            receipt = self.helper.prepare_completion(**self.arguments, reapprove_current_input=True)
        except (ValueError, OSError) as exc:
            self.fail(f"unchanged ordinary admitted task lost its compatible origin: {exc}")
        self.assertEqual(receipt["approval_generation"], previous["approval_generation"])
        fixtures.CompletionPublicationTest.publish(self, receipt)
        self.assertEqual(fixtures.CompletionPublicationTest.stop(self)["systemMessage"], "")

    def test_changed_ordinary_admission_cannot_claim_unknown_historical_semantics(self):
        previous = self.ordinary_summary_run()
        self.change_intent({"constraints": ["New authorized semantic restriction"]})
        self.notice(self.hook())
        with self.assertRaisesRegex(ValueError, "prospective origin is stale"):
            self.helper.prepare_completion(**self.arguments, reapprove_current_input=True)
        with adapter.storage.transaction(self.run_dir, self.source) as store:
            self.assertNotEqual(store.run()["approval_generation"], previous["approval_generation"])
            with self.assertRaises(FileNotFoundError):
                store.read(self.helper.PUBLICATION_OBJECT)

    def installed_lifecycle(self, platform):
        if platform == "claude":
            self.core.record_turn(root=self.root / "intent", platform=platform, session_id="session-1",
                raw_user_input="Write the report", intent_delta={"current_goal": "Write and verify the requested report.",
                    "acceptance_criteria": self.read_state()["acceptance_criteria"]})
            self.source["GHOST_ALICE_PLATFORM"] = platform
        sys.path.insert(0, str(Path(self.source["GHOST_ALICE_CORE_ROOT"]) / "_shared"))
        import install_hooks
        home = self.root / "installed home"; (home / ".codex").mkdir(parents=True); (home / ".claude").mkdir()
        installed = home / ".agents/skills/autopilot-mode"; shutil.copytree(SKILL, installed)
        env = self.environment(HOME=str(home), CODEX_HOME=str(home / ".codex"))
        with mock.patch.dict(os.environ, env, clear=True):
            install_hooks.install_hook(platform, addon_sources=[str(ROOT)], skills_dir=installed.parent)
        config = home / (".codex/hooks.json" if platform == "codex" else ".claude/settings.json")
        hooks = json.loads(config.read_text())["hooks"]
        def installed_hook(event, marker):
            command = next(row["command"] for entry in hooks[event] for row in entry.get("hooks", [])
                           if marker in row.get("command", ""))
            result = subprocess.run(command, shell=True, env=env, cwd=self.root,
                input=json.dumps({"hook_event_name": event, "session_id": "session-1", "cwd": str(self.root)}),
                text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        context = self.notice(installed_hook("PreToolUse", "[adapter:autopilot-mode] prepare-origin"))
        prepare = self.prepare_command(context)
        self.assertIn(str((installed / "scripts/autopilot_completion.py").resolve()), prepare)
        self.assertFalse(self.run_dir.exists())
        prepared = subprocess.run(prepare, env=env, cwd=self.root, text=True, capture_output=True)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        receipt = json.loads(prepared.stdout)
        self.assertIn("publish_command", receipt)
        # The original successful output remains in memory. Publication must not
        # require a saved proof file or invoke this business inspection again.
        verification = subprocess.run([sys.executable, "-B", "-c",
            "from pathlib import Path; from datetime import datetime, timezone; import json; "
            "p=Path('inspection-count'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1'); "
            "assert p.read_text()=='1'; "
            "print(json.dumps({'verified_at':datetime.now(timezone.utc).isoformat(), 'result':'exact readback passed'}))"],
            env=env, cwd=self.root, text=True, capture_output=True)
        self.assertEqual(verification.returncode, 0, verification.stderr)
        original_result = json.loads(verification.stdout)
        proof = self.proof(original_result["verified_at"]) + "- evidence: tool-result:business-check-1\n"
        publish = shlex.split(receipt["publish_command"])
        self.assertIn(str((installed / "scripts/autopilot_completion.py").resolve()), publish)
        self.assertEqual(publish[publish.index("--receipt-token") + 1], receipt["receipt_token"])
        self.assertEqual(publish[publish.index("--completion-file") + 1], "-")
        publish[publish.index("--verified-at") + 1] = original_result["verified_at"]
        published = subprocess.run(publish, input=proof, env=env, cwd=self.root, text=True, capture_output=True)
        self.assertEqual(published.returncode, 0, published.stderr)
        published_result = json.loads(published.stdout)
        self.assertEqual(published_result["status"], "pending-adapter")
        self.assertEqual(published_result["verified_at"], original_result["verified_at"])
        before_stop = self.core.read_session_state(root=self.root / "intent", platform=platform, session_id="session-1")
        self.assertEqual({row["status"] for row in before_stop["acceptance_criteria"]}, {"unmet"})
        self.assertEqual(installed_hook("PreToolUse", "[adapter:autopilot-mode] prepare-origin"), {"continue": True})
        stopped = installed_hook("Stop", "[adapter:autopilot-mode] continue")
        self.assertEqual(stopped.get("systemMessage", ""), "", stopped)
        self.assertNotEqual(stopped.get("decision"), "block", stopped)
        self.assertEqual((self.root / "inspection-count").read_text(), "1")
        task = adapter.read_work_items(self.run_dir / "tasks.jsonl")[0]
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["completion"]["evidence"], [proof])
        after_stop = self.core.read_session_state(root=self.root / "intent", platform=platform, session_id="session-1")
        self.assertEqual({row["status"] for row in after_stop["acceptance_criteria"]}, {"met"})

    def test_installed_codex_preflight_prepare_publish_needs_no_stop_recovery(self):
        self.installed_lifecycle("codex")

    def test_installed_claude_preflight_prepare_publish_needs_no_stop_recovery(self):
        self.installed_lifecycle("claude")


if __name__ == "__main__":
    unittest.main()
