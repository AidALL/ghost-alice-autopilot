"""Early, once-per-origin publication guidance through real pretool adapters."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import copy
import importlib.util
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


class CurrentResponseRouteTest(unittest.TestCase):
    """Exercise the four consumers against canonical intake, never JSON stamps."""
    def setUp(self):
        import tempfile
        from response_route_fixture import native_ledger
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.run_dir = self.root / ".autopilot/sessions/codex/session-1"
        self.source = dict(GHOST_ALICE_CORE_ROOT=os.environ["GHOST_ALICE_CORE_ROOT"],
            GHOST_ALICE_AUTOPILOT_APPROVAL_EVIDENCE_JSON='{"decision":"GO","source":"explicit fixture approval"}',
            GHOST_ALICE_PLATFORM="codex", GHOST_ALICE_SESSION_ID="session-1",
            GHOST_ALICE_SESSION_INTENT_ROOT=str(self.root / "intent"), PWD=str(self.root))
        from autopilot_work_items import _load_core_ledger_module
        self.core = _load_core_ledger_module(self.root / "intent/codex/session-1/intent-state.json", self.source)
        native_ledger(self.root / "intent", self.core)
        spec = importlib.util.spec_from_file_location("native_pretool", SKILL / "adapters/autopilot_pretool.py")
        self.pretool = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.pretool)
        spec = importlib.util.spec_from_file_location("native_completion", SKILL / "scripts/autopilot_completion.py")
        self.helper = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.helper)

    def capture(self):
        return self.pretool.capture_before_tool(self.source,
            {"hook_event_name": "PreToolUse", "session_id": "session-1", "cwd": str(self.root)}, preflight=True)

    def prepare(self):
        return self.helper.prepare_completion(intent_root=self.root / "intent", platform="codex",
            session_id="session-1", input_event_id=self.state()["latest_input_event_id"], source=self.source)

    def state(self):
        return self.core.read_session_state(root=self.root / "intent", platform="codex", session_id="session-1")

    def select(self, **kwargs):
        from response_route_fixture import register_route
        register_route(self.core, self.root / "intent", **kwargs)

    def projection_consumers(self):
        spec = importlib.util.spec_from_file_location("fault_bridge", SKILL / "scripts/autopilot_session_bridge.py")
        bridge = importlib.util.module_from_spec(spec); spec.loader.exec_module(bridge)
        return {
            "capture": self.capture,
            "prepare": self.prepare,
            "bridge": lambda: bridge.bridge_session_intent_to_run_state(
                intent_root=self.root / "intent", platform="codex", session_id="session-1",
                input_event_id="event-1", run_dir=self.run_dir, current_work_item_id="current-task",
                plan_path=str(self.root / "plan.md"), approval_evidence={"decision":"GO", "source":"current-user"},
                source=self.source),
            "stop": lambda: adapter.adapter_payload_from_env(self.source)["completion_recording"],
        }

    def assert_no_projection_writes(self, before):
        self.assertEqual(before, self.state())
        self.assertFalse(self.run_dir.exists())
        with closing(sqlite3.connect(self.root / "intent/ghost-state.sqlite3")) as connection:
            self.assertEqual(connection.execute("SELECT name FROM sqlite_master WHERE name LIKE 'ap_%'").fetchall(), [])

    def test_explicit_core_ownership_binding_selects_existing_conditions_without_accepting_old_proof(self):
        from autopilot_intent_recovery import project_session_material
        root = self.root / "legacy-intent"
        rows = [dict(id="current", summary="Current requested result", source="user-explicit", admitted=True, status="unmet"),
            dict(id="native", summary="Native control recorded", source="user-explicit", admitted=True, status="met",
                 met_completion_check_digest="a" * 64, met_at="2026-10-01T12:00:00Z"),
            dict(id="extra", summary="Unselected extra", source="user-explicit", admitted=False, status="unmet")]
        state = self.core.default_state("codex", "session-1")
        state["acceptance_criteria"] = rows
        state["work_units"] = [dict(work_unit_id="legacy", contract_revision=1, kind="execution", admitted=True,
            owner=dict(platform="codex", session_id="session-1"),
            criteria=[{k: row[k] for k in ("id", "summary", "source")} for row in rows[:2]])]
        self.core.write_json(self.core.session_paths(root, "codex", "session-1")["state"], state)
        self.core.migrate_session(root=root, platform="codex", session_id="session-1")
        self.core.record_turn(root=root, platform="codex", session_id="session-1", raw_user_input="Continue registered work", source="hook")
        before = self.core.read_session_state(root=root, platform="codex", session_id="session-1")
        with self.core.storage_transaction(root) as connection:
            self.core.bind_legacy_acceptance_criteria(root=root, platform="codex", session_id="session-1",
                work_unit_id="legacy", contract_revision=1, expected_input_event_id=before["latest_input_event_id"],
                expected_revision=before["ledger_revision"], expected_criteria=before["acceptance_criteria"][:2], transaction=connection)
            bound = self.core.read_session_state(root=root, platform="codex", session_id="session-1", transaction=connection)
            candidate = dict(schema_version="response-route.v1", routing_surface=dict(
                intent_relation="continuation", primary_request="Continue registered work", causal_axis="n/a", response_mode="normal",
                response_order="imperative-result-first-after-necessary-work", change_depth="localized", focus_layer="meso",
                verification_complexity="level-2", boundary_contract="required", forced_visibility="no", reason="Existing owner"),
                work_units=[{k: state["work_units"][0][k] for k in ("work_unit_id", "contract_revision", "kind", "admitted", "owner")}],
                selected_work_unit_ids=["legacy"], selected_criteria=[dict(work_unit_id="legacy", contract_revision=1, criterion_id=row["id"]) for row in rows[:2]])
            self.core.record_response_route(root=root, platform="codex", session_id="session-1", expected_input_event_id=before["latest_input_event_id"],
                boundary_ref="binding-test", candidate=candidate, transaction=connection)
        source = dict(self.source, GHOST_ALICE_SESSION_INTENT_ROOT=str(root), GHOST_ALICE_COMPLETION_SCHEMA="autopilot-completion-receipt.v2")
        material = adapter.read_session_material(root, "codex", "session-1", source=source)
        projected, selection = project_session_material(material, source, self.run_dir)
        self.assertEqual(selection["status"], "selected")
        self.assertEqual([row["id"] for row in projected["intent_state"]["acceptance_criteria"]], ["current", "native"])
        self.assertEqual(len(material["intent_state"]["acceptance_criteria"]), 3)
        stopped = adapter.adapter_payload_from_env(source)
        self.assertTrue(stopped["continue"])
        self.assertNotEqual(stopped["completion_recording"]["units"][0].get("unit_status"), "complete")
        after = self.core.read_session_state(root=root, platform="codex", session_id="session-1")
        self.assertEqual(after["acceptance_criteria"], bound["acceptance_criteria"])
        self.assertEqual(after["acceptance_criteria"][1]["met_at"], rows[1]["met_at"])
        with closing(sqlite3.connect(root / "ghost-state.sqlite3")) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ap_completion_v2_records WHERE kind='evidence'").fetchone()[0], 0)

    def test_recording_route_read_fault_is_typed_for_each_direct_consumer(self):
        import autopilot_intent_recovery as recovery
        before = self.state()
        faults = (sqlite3.OperationalError("database is locked"), OSError("recording database unavailable"))
        for fault in faults:
            for name, consume in self.projection_consumers().items():
                with self.subTest(fault=type(fault).__name__, consumer=name), \
                        mock.patch.object(recovery, "_core_native_assessor", side_effect=fault):
                    result = consume()
                    self.assertEqual(result["status"], "recording-unavailable")
                    self.assertEqual(result["reason"], "core-route-recording-failed")
                    self.assertEqual(result["detail"], str(fault))
                    self.assertEqual(result["error_type"], type(fault).__name__)
        self.assert_no_projection_writes(before)

    def test_executable_stop_reports_recording_fault_without_generic_block(self):
        import io
        import autopilot_mode as mode
        import autopilot_intent_recovery as recovery
        from contextlib import redirect_stdout, redirect_stderr
        before = self.state(); output = io.StringIO(); error = io.StringIO()
        with mock.patch.object(recovery, "_core_native_assessor", side_effect=sqlite3.OperationalError("database is locked")), \
                mock.patch.object(mode, "_read_hook_input", return_value={"hook_event_name":"Stop", "session_id":"session-1", "cwd":str(self.root)}), \
                mock.patch.object(mode.sys, "argv", [str(SKILL / "adapters/autopilot_mode.py")]), \
                mock.patch.dict(mode.os.environ, self.source, clear=True), redirect_stdout(output), redirect_stderr(error):
            self.assertEqual(mode.main(), 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["continue"])
        self.assertEqual(payload["systemMessage"], "")
        self.assertEqual(payload["completion_recording"]["reason"], "core-route-recording-failed")
        self.assertNotIn("decision", payload)
        self.assertNotIn("permissionDecision", payload.get("hookSpecificOutput", {}))
        self.assertEqual(error.getvalue(), "")
        self.assert_no_projection_writes(before)

    def test_malformed_selected_rows_are_typed_for_each_direct_consumer(self):
        import autopilot_intent_recovery as recovery
        route = self.core.read_response_route(root=self.root / "intent", platform="codex", session_id="session-1")
        assessment = recovery._core_native_assessor(self.core)(self.source)
        selected = assessment["completion_route"]
        before = self.state()
        for name, consume in self.projection_consumers().items():
            with self.subTest(consumer=name), mock.patch.object(recovery, "_core_native_assessor",
                    return_value=lambda *args, **kwargs: dict(assessment,
                        completion_route=dict(selected, selected_criteria=[{}]))):
                result = consume()
                self.assertEqual(result["status"], "recording-unavailable")
                self.assertEqual(result["reason"], "core-route-incompatible")
        self.assert_no_projection_writes(before)

    def test_projection_validates_selection_identity_and_consumed_route_shape(self):
        import autopilot_intent_recovery as recovery
        import autopilot_work_items as work_items
        material = adapter.read_session_material(self.root / "intent", "codex", "session-1", source=self.source)
        route = self.core.read_response_route(root=self.root / "intent", platform="codex", session_id="session-1")
        assessment = recovery._core_native_assessor(self.core)(self.source)
        selected = assessment["completion_route"]
        criterion, unit = selected["selected_criteria"][0], route["work_units"][0]
        faults = (({}, {"selected_work_unit_ids":[{}]}),
                  ({}, {"route_binding":{}}), ({}, {"publication_route":[]}),
                  ({}, {"selected_work_unit_ids":["current", "current"]}),
                  ({}, {"selected_criteria":[dict(criterion, contract_revision=True)]}),
                  ({}, {"selected_criteria":[dict(criterion, criterion_id="other")]}),
                  ({}, {"selected_criteria":selected["selected_criteria"] * 2}),
                  ({}, {"selected_criteria":[dict(criterion, status={})]}),
                  ({"work_units":[{}]}, {}), ({"work_units":{}}, {}),
                  ({"work_units":[dict(unit, kind={})]}, {}),
                  ({"work_units":[dict(unit, owner={"platform":"codex", "session_id":"foreign"})]}, {}),
                  ({"input_event_id":"foreign"}, {}), ({"routing_surface":{}}, {}),
                  ({"routing_surface":dict(route["routing_surface"], response_mode=[])}, {}))
        before = self.state()
        for route_fault, selection_fault in faults:
            with self.subTest(route=route_fault, selection=selection_fault), \
                    mock.patch.object(recovery, "_core_native_assessor", return_value=lambda *args, **kwargs:
                        dict(assessment, response_route=dict(route, **route_fault),
                             completion_route=dict(selected, **selection_fault))):
                result = recovery.current_execution_projection(material, self.source, self.run_dir)
                self.assertEqual(result["status"], "recording-unavailable")
                self.assertEqual(result["reason"], "core-route-incompatible")
        self.assert_no_projection_writes(before)

    def test_projection_accepts_omitted_verification_label_without_relaxing_supplied_values(self):
        import autopilot_intent_recovery as recovery
        assessment = recovery._core_native_assessor(self.core)(self.source)
        route = copy.deepcopy(assessment["response_route"])
        selected = assessment["completion_route"]
        route["routing_surface"] = dict(response_mode="normal", boundary_contract="required")
        self.assertTrue(recovery._selection_shape_valid(route, selected, "codex", "session-1", "event-1"))
        for value in (None, [], "unbounded"):
            with self.subTest(value=value):
                route["routing_surface"]["verification_complexity"] = value
                self.assertFalse(recovery._selection_shape_valid(route, selected, "codex", "session-1", "event-1"))

    def test_projection_does_not_mask_unexpected_programming_errors(self):
        import autopilot_intent_recovery as recovery
        with mock.patch.object(recovery, "_core_native_assessor", side_effect=RuntimeError("unexpected implementation failure")):
            with self.assertRaisesRegex(RuntimeError, "unexpected implementation failure"):
                self.capture()

    def test_current_criteria_exclude_historical_unmet_obligations(self):
        self.core.record_turn(root=self.root / "intent", platform="codex", session_id="session-1",
            expected_input_event_id="event-1", intent_delta={"acceptance_criteria": [{"id":"old", "summary":"Old obligation",
                "source":"user-explicit", "admitted":True, "status":"unmet", "work_unit_id":"old-unit", "contract_revision":1}]})
        self.select(selected=["current"])
        receipt = self.prepare()
        self.assertEqual(receipt["criterion_ids"], ["bridge-state"])
        run = adapter.storage.read(self.run_dir / "approved-run.json")
        self.assertEqual([c["id"] for c in run["approval_evidence"]["session_intent"]["acceptance_criteria"]], ["bridge-state"])
        self.assertEqual(run["approval_evidence"]["session_intent"]["selected_work_units"][0]["work_unit_id"], "current")

    def test_current_execution_continues_after_route_binding_repair(self):
        self.core.record_turn(root=self.root / "intent", platform="codex", session_id="session-1", raw_user_input="Continue authorized work")
        self.assertEqual(self.capture()["status"], "skipped")
        self.assertEqual(self.prepare()["status"], "skipped")
        adapter.adapter_payload_from_env(self.source)
        self.assertFalse(self.run_dir.exists())
        self.select(selected=["current"])
        self.assertEqual(self.capture()["status"], "prospective")
        self.assertEqual(self.prepare()["status"], "prepared")
        self.assertTrue(adapter.adapter_payload_from_env(self.source)["systemMessage"])

    def test_new_input_waits_quietly_then_prepares_after_route_registration(self):
        self.source['GHOST_ALICE_COMPLETION_SCHEMA']='v2'
        self.core.record_turn(root=self.root/'intent',platform='codex',session_id='session-1',raw_user_input='Continue authorized work')
        before = self.state()
        payload=self.repair_hook()
        self.assertTrue(payload['continue'])
        self.assertNotIn('hookSpecificOutput', payload)
        self.assertEqual(payload['completion_recording']['status'], 'skipped')
        self.assertEqual(payload['completion_recording']['reason'], 'awaiting-current-route')
        stop = adapter.adapter_payload_from_env(self.source, hook_input=dict(
            hook_event_name='Stop', session_id='session-1', last_assistant_message='A status explanation.'))
        self.assertFalse(stop.get('systemMessage'))
        self.assertEqual(stop['completion_recording']['status'], 'recording-unavailable')
        self.assertEqual(stop['completion_recording']['reason'], 'completion-body-invalid')
        self.assertIn('native-activity-unavailable', stop['completion_recording']['detail'])
        from autopilot_intent_recovery import _core_native_assessor
        for marker in ('completion-check', 'partial-check'):
            rejected = _core_native_assessor(self.core)(self.source,
                text='[' + marker + ']\n', phase='final')
            self.assertIn('route-unavailable', rejected['body_issue'])
        self.assert_no_projection_writes(before)
        self.select(selected=['current'])
        prepared = self.repair_hook()['completion_recording']
        self.assertEqual(prepared['status'], 'prepared')
        self.assertEqual(prepared['work_unit_id'], 'current')
        self.assertEqual(prepared['input_event_id'], self.state()['latest_input_event_id'])

    def test_native_new_input_wait_keeps_diagnostics_without_repeated_warnings(self):
        self.source['GHOST_ALICE_COMPLETION_SCHEMA'] = 'v2'
        self.core.record_turn(root=self.root/'intent', platform='codex', session_id='session-1', raw_user_input='Continue')
        before = self.state()
        first, repeated = self.repair_hook(), self.repair_hook()
        for payload in (first, repeated):
            self.assertTrue(payload['continue'])
            self.assertNotIn('hookSpecificOutput', payload)
            self.assertEqual(payload['completion_recording']['reason'], 'awaiting-current-route')
            self.assertEqual(payload['completion_recording']['completion_route']['route_binding'], 'stale')
        self.assert_no_projection_writes(before)
        self.core.record_turn(root=self.root/'intent', platform='codex', session_id='session-1', raw_user_input='Continue again')
        self.assertNotIn('hookSpecificOutput', self.repair_hook())

    def recording_hook(self, captured):
        import io
        from contextlib import redirect_stdout
        output = io.StringIO()
        with mock.patch.object(self.pretool, 'capture_before_tool', return_value=captured) as capture, \
                mock.patch.object(self.pretool, '_read_hook_input', return_value={
                    'hook_event_name':'PreToolUse', 'session_id':'session-1', 'cwd':str(self.root)}), \
                mock.patch.object(self.pretool.sys, 'argv', [str(SKILL/'adapters/autopilot_pretool.py')]), \
                mock.patch.dict(self.pretool.os.environ, self.source, clear=True), redirect_stdout(output):
            self.assertEqual(self.pretool.main(), 0)
        capture.assert_called_once()
        payload = json.loads(output.getvalue())
        self.assertTrue(payload['continue'])
        self.assertNotIn('permissionDecision', payload.get('hookSpecificOutput', {}))
        self.assertEqual(payload['completion_recording'], captured)
        return payload.get('hookSpecificOutput', {}).get('additionalContext', '')

    def test_notice_changes_and_recovery_restore_full_guidance_and_keep_raw_diagnostics(self):
        unavailable = dict(status='recording-unavailable', reason='route-stale', detail='first detail')
        full = self.recording_hook(unavailable)
        self.assertEqual(self.recording_hook(unavailable), '')
        changed = dict(unavailable, detail='changed detail')
        self.assertEqual(self.recording_hook(changed), full)
        self.assertEqual(self.recording_hook(changed), '')
        other = dict(changed, reason='authority-unavailable')
        self.assertIn('independent authorized work', self.recording_hook(other))
        self.recording_hook(dict(status='prepared'))
        self.assertIn('independent authorized work', self.recording_hook(other))

    def test_notice_cache_missing_or_corrupt_fails_to_full_guidance(self):
        unavailable = dict(status='recording-unavailable', reason='route-stale')
        full = self.recording_hook(unavailable)
        cache = self.root/'intent/session-checks.sqlite3'
        self.assertEqual(self.recording_hook(unavailable), '')
        cache.unlink()
        self.assertEqual(self.recording_hook(unavailable), full)
        cache.write_bytes(b'corrupt cache')
        self.assertEqual(self.recording_hook(unavailable), full)
        self.assertEqual(self.recording_hook(unavailable), full)

    def test_notice_cache_never_borrows_ambient_core_or_missing_identity(self):
        unavailable = dict(status='recording-unavailable', reason='route-stale')
        full = self.recording_hook(unavailable)
        self.assertEqual(self.recording_hook(unavailable), '')
        for key, value in [('GHOST_ALICE_CORE_ROOT', str(self.root/'absent-core')),
                           ('GHOST_ALICE_CORE_ROOT', ''), ('GHOST_ALICE_SESSION_INTENT_ROOT', '')]:
            with self.subTest(key=key, value=value), mock.patch.dict(self.source, {key:value}):
                self.assertEqual(self.recording_hook(unavailable), full)
        self.assertEqual(self.state()['latest_input_event_id'], 'event-1')

    def test_native_multiple_unit_notice_shares_guidance_and_keeps_exact_commands(self):
        self.source['GHOST_ALICE_COMPLETION_SCHEMA'] = 'v2'
        self.core.record_turn(root=self.root/'intent', platform='codex', session_id='session-1',
            expected_input_event_id='event-1', intent_delta={'acceptance_criteria': [dict(
                id='other-result', summary='Other current result', source='user-explicit', admitted=True,
                status='unmet', work_unit_id='second', contract_revision=2)]})
        self.select()
        payload = self.repair_hook()
        notice = payload['hookSpecificOutput']['additionalContext']
        self.assertEqual(notice.count('completion-preflight: prepared'), 1)
        self.assertEqual(notice.count('Native Stop handles publication'), 1)
        rows = payload['completion_recording']['units']
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertIn('work-unit: '+row['work_unit_id'], notice)
            self.assertIn('contract-revision: '+str(row['contract_revision']), notice)
            self.assertTrue(row['receipt_token'].startswith('sha256:'))
            self.assertTrue(row['prepared_at'])
            self.assertNotIn(row['publish_command'], notice)
            self.assertNotIn(row['prepare_command'], notice)
            self.assertNotIn(row['verification_time_command'], notice)
        self.assertNotIn('hookSpecificOutput', self.repair_hook())

    def test_multiple_units_keep_qualified_selection_and_fail_closed_in_v1(self):
        self.core.record_turn(root=self.root / "intent", platform="codex", session_id="session-1",
            expected_input_event_id="event-1", intent_delta={"acceptance_criteria": [{"id":"bridge-state", "summary":"Other unit",
                "source":"user-explicit", "admitted":True, "status":"unmet", "work_unit_id":"second", "contract_revision":1}]})
        self.select()
        result = self.prepare()
        self.assertEqual(result["status"], "recording-unavailable")
        self.assertEqual(result["reason"], "v1-multiple-work-units")
        self.assertEqual([c["work_unit_id"] for c in result["selected_criteria"]], ["current", "second"])
        self.assertFalse(self.run_dir.exists())

    def test_missing_core_route_api_is_typed_without_fallback(self):
        import autopilot_work_items as work_items
        incompatible = mock.Mock(wraps=self.core)
        incompatible.read_response_route = None
        with mock.patch.object(work_items, "_load_core_ledger_module", return_value=incompatible):
            self.assertEqual(self.capture()["status"], "recording-unavailable")
            self.assertEqual(self.prepare()["status"], "recording-unavailable")
        self.assertFalse(self.run_dir.exists())

    def test_unrouted_bootstrap_tools_are_usable_and_stop_creates_no_directory(self):
        unbound = self.root / "unbound"
        self.source["GHOST_ALICE_SESSION_INTENT_ROOT"] = str(unbound)
        self.core.record_turn(root=unbound, platform="codex", session_id="session-1",
            raw_user_input="Register the current execution route")
        result = self.capture()
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "awaiting-current-route")
        stopped = adapter.adapter_payload_from_env(self.source)
        self.assertTrue(stopped["continue"])
        self.assertEqual(stopped["systemMessage"], "")
        self.assertFalse(self.run_dir.exists())

    def test_notice_and_design_units_do_not_reactivate_old_execution(self):
        self.core.record_turn(root=self.root / "intent", platform="codex", session_id="session-1",
            expected_input_event_id="event-1", intent_delta={"acceptance_criteria":[{"id":"notice", "summary":"Design discussion",
                "source":"user-explicit", "admitted":True, "status":"unmet", "work_unit_id":"notice", "contract_revision":1}]})
        self.select(selected=["notice"], kind="nonexecution")
        self.assertEqual(self.capture()["status"], "skipped")
        self.assertEqual(self.prepare()["status"], "skipped")
        adapter.adapter_payload_from_env(self.source)
        self.assertFalse(self.run_dir.exists())

    def test_selected_owner_and_root_mismatch_preserve_foreign_run(self):
        for mismatch in ("session_id", "ledger_root"):
            with self.subTest(mismatch=mismatch):
                self.run_dir.mkdir(parents=True, exist_ok=True)
                owner = dict(schema_version="autopilot-authority.v1", kind="session", platform="codex",
                    session_id="session-1", ledger_root=str(self.root / "intent"))
                owner[mismatch] = "foreign" if mismatch == "session_id" else str(self.root / "foreign")
                path = self.run_dir / "authority.json"; path.write_text(json.dumps(owner)); before = path.read_bytes()
                self.assertEqual(self.capture()["reason"], "run-owner-mismatch")
                self.assertEqual(self.prepare()["reason"], "run-owner-mismatch")
                adapter.adapter_payload_from_env(self.source)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(list(self.run_dir.iterdir()), [path])
                path.unlink(); self.run_dir.rmdir()

    def test_stop_does_not_borrow_a_current_session_from_the_discovery_pointer(self):
        source = dict(self.source)
        source.pop("GHOST_ALICE_SESSION_ID")
        stopped = adapter.adapter_payload_from_env(source)
        self.assertEqual(stopped["systemMessage"], "")
        self.assertFalse(self.run_dir.exists())

    def test_incompatible_core_assessment_has_a_typed_skip(self):
        import autopilot_intent_recovery as recovery
        material = adapter.read_session_material(self.root / "intent", "codex", "session-1", source=self.source)
        assessment = recovery._core_native_assessor(self.core)(self.source)
        with mock.patch.object(recovery, "_core_native_assessor",
                return_value=lambda *args, **kwargs: dict(assessment,
                    completion_route={"schema_version":"completion-route.future"})):
            result = recovery.current_execution_projection(material, self.source, self.run_dir)
        self.assertEqual(result["status"], "recording-unavailable")
        self.assertEqual(result["reason"], "core-route-incompatible")
        self.assertFalse(self.run_dir.exists())

    def legacy_approval(self, *, ambiguous=False, incompatible=False, unowned=False):
        current = copy.deepcopy(self.state()["acceptance_criteria"][0])
        alias = dict(id=current["id"], summary="Historical unrelated alias", source="user-explicit", admitted=True, status="unmet")
        owned_history = dict(alias, work_unit_id="historical-unit", contract_revision=1)
        self.core.record_turn(root=self.root / "intent", platform="codex", session_id="session-1",
            expected_input_event_id="event-1", intent_delta={"acceptance_criteria":[alias, owned_history]})
        self.assertTrue(adapter._bootstrap_from_session_intent_if_approved(self.run_dir, self.source, self.root))
        with adapter.storage.transaction(self.run_dir, self.source) as store:
            run = store.run(); tasks = store.read("tasks.jsonl")
            binding = run["approval_evidence"]["session_intent"]
            binding["acceptance_criteria"] = [alias, owned_history, current]
            if ambiguous:
                binding["acceptance_criteria"].append(copy.deepcopy(current))
            if incompatible:
                binding["acceptance_criteria"].append(dict(current, contract_revision="1"))
            if unowned:
                binding["acceptance_criteria"] = [alias]
                binding.pop("selected_work_units", None); binding.pop("selected_criteria", None)
            run["approval_generation"] = adapter.SESSION_MATERIAL.approval_generation(run)
            store.put_run(run); store.write("tasks.jsonl", tasks)

    def repair_hook(self):
        env = {**os.environ, **self.source, "PYTHONDONTWRITEBYTECODE":"1"}
        env.pop("CODEX_THREAD_ID", None); env.pop("CLAUDE_PROJECT_DIR", None)
        result = subprocess.run([sys.executable, "-B", str(SKILL / "adapters/autopilot_pretool.py")],
            cwd=self.root, env=env, input=json.dumps({"hook_event_name":"PreToolUse", "session_id":"session-1",
                "cwd":str(self.root), "tool_name":"Bash", "tool_input":{"command":"register current intake and repair response binding"}}),
            text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_legacy_aliases_do_not_deny_repair_or_import_historical_criteria(self):
        self.legacy_approval()
        state = self.state()
        payload = self.repair_hook()
        self.assertTrue(payload.get("continue"), payload)
        self.assertNotEqual(payload.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")
        captured = self.capture()
        self.assertEqual(captured["status"], "prospective")
        with closing(sqlite3.connect(self.root / "intent/ghost-state.sqlite3")) as conn:
            row = conn.execute("SELECT body_json FROM ap_completion_origins WHERE token=?", (captured["origin_token"],)).fetchone()
        origin = json.loads(row[0])
        current = [c for c in state["acceptance_criteria"] if c.get("work_unit_id") == "current"]
        self.assertEqual(origin["contract"]["acceptance_criteria"], self.helper._definitions(current))
        self.assertEqual(origin["contract"]["selected_work_units"][0]["work_unit_id"], "current")
        self.assertIsNone(origin["existing_generation"], "v1 must not adopt an accumulated legacy contract")
        self.assertEqual(state, self.state())
        self.assertEqual(self.prepare()["reason"], "v1-approval-needs-reapproval")
        receipt = self.helper.prepare_completion(intent_root=self.root / "intent", platform="codex", session_id="session-1",
            input_event_id="event-1", source=self.source, reapprove_current_input=True)
        self.assertEqual(receipt["status"], "prepared")
        verified_at = self.helper.datetime.now(self.helper.timezone.utc).isoformat()
        proof = "\n".join(["[completion-check]", "- acceptance-criteria:", "  - bridge-state: Current requested deliverable [source: user-explicit]",
            "- claim-evidence-map:", "  - claim: Current requested deliverable is verified", "    criterion: bridge-state",
            "    evidence: tool-result:business-verification at " + verified_at, "    verdict: pass", "- unverified: none"])
        self.helper.publish_completion(receipt_token=receipt["receipt_token"], completion_check=proof, verified_at=verified_at,
            evidence_source="tool-result:business-verification", source=self.source)
        self.assertEqual(adapter.adapter_payload_from_env(self.source)["systemMessage"], "")
        state = self.state()
        self.assertEqual(next(c for c in state["acceptance_criteria"] if c.get("work_unit_id") == "current")["status"], "met")
        self.assertEqual(next(c for c in state["acceptance_criteria"] if "work_unit_id" not in c)["status"], "unmet")
        self.assertEqual(next(c for c in state["acceptance_criteria"] if c.get("work_unit_id") == "historical-unit")["status"], "unmet")

    def test_ambiguous_qualified_approval_is_typed_and_cannot_prepare(self):
        self.legacy_approval(ambiguous=True)
        payload = self.repair_hook()
        self.assertTrue(payload.get("continue"), payload)
        self.assertEqual(self.capture()["status"], "recording-unavailable")
        self.assertEqual(self.prepare()["status"], "recording-unavailable")
        with closing(sqlite3.connect(self.root / "intent/ghost-state.sqlite3")) as conn:
            self.assertFalse(conn.execute("SELECT name FROM sqlite_master WHERE name='ap_completion_origins'").fetchone())
        self.assertEqual(adapter.read_work_items(self.run_dir / "tasks.jsonl")[0]["status"], "ready")
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            adapter.advance_approved_run(self.run_dir, self.source)
        self.assertEqual(adapter.read_work_items(self.run_dir / "tasks.jsonl")[0]["status"], "ready")

    def test_legacy_block_is_readable_and_never_becomes_ap_permission(self):
        self.core.record_turn(root=self.root / "intent", platform="codex", session_id="session-1",
            expected_input_event_id="event-1", intent_delta={"model_security_decision":{
                "decision":"block", "input_event_id":"event-1", "reason":"Genuine current block", "risk_flags":[]}})
        payload = self.repair_hook()
        self.assertTrue(payload.get("continue"), payload)
        self.assertNotIn("permissionDecision", payload.get("hookSpecificOutput", {}))
        self.assertEqual(self.capture()["status"], "prospective")
        self.assertEqual(self.state()["model_security_decision"]["decision"], "block")
        self.assertFalse(self.run_dir.exists())
        self.assertEqual(self.prepare()["status"], "prepared")

    def test_recording_database_failure_does_not_become_ap_tool_permission(self):
        import io
        from contextlib import redirect_stdout
        output = io.StringIO()
        with mock.patch.object(self.pretool, "capture_before_tool", side_effect=sqlite3.OperationalError("recording database unavailable")), \
                mock.patch.object(self.pretool, "_read_hook_input", return_value={"hook_event_name":"PreToolUse", "session_id":"session-1", "cwd":str(self.root)}), \
                mock.patch.object(self.pretool.sys, "argv", [str(SKILL / "adapters/autopilot_pretool.py")]), redirect_stdout(output):
            self.assertEqual(self.pretool.main(), 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["continue"])
        self.assertNotIn("permissionDecision", payload.get("hookSpecificOutput", {}))
        self.assertIn("completion-recording: unavailable", payload["hookSpecificOutput"]["additionalContext"])

    def test_unowned_legacy_approval_requires_new_admission_without_relabeling_proof(self):
        self.legacy_approval(unowned=True)
        original = adapter.storage.read(self.run_dir / "approved-run.json")
        self.assertTrue(self.repair_hook().get("continue"))
        self.assertEqual(self.capture().get("reason"), "unbound-legacy-approval")
        self.assertEqual(self.prepare()["reason"], "unbound-legacy-approval")
        old_time = self.helper.datetime.now(self.helper.timezone.utc).isoformat()
        receipt = self.helper.prepare_completion(intent_root=self.root / "intent", platform="codex", session_id="session-1",
            input_event_id="event-1", source=self.source, reapprove_current_input=True)
        self.assertEqual(receipt["status"], "prepared")
        with adapter.storage.transaction(self.run_dir, self.source) as store:
            preserved = store.connection.execute("SELECT run_json FROM ap_generations WHERE run_key=? AND generation=?",
                (store.key, original["approval_generation"])).fetchone()
            self.assertEqual(json.loads(preserved[0]), original)
            current = store.run()["approval_evidence"]["session_intent"]
            self.assertEqual(current["selected_work_units"][0]["work_unit_id"], "current")
            self.assertEqual(len(current["acceptance_criteria"]), 1)
        old_proof = "\n".join(["[completion-check]", "- acceptance-criteria:", "  - bridge-state: Current requested deliverable [source: user-explicit]",
            "- claim-evidence-map:", "  - claim: Current deliverable checked", "    criterion: bridge-state",
            "    evidence: tool-result:old-proof at " + old_time, "    verdict: pass", "- unverified: none"])
        with self.assertRaises(ValueError):
            self.helper.publish_completion(receipt_token=receipt["receipt_token"], completion_check=old_proof,
                verified_at=old_time, evidence_source="tool-result:old-proof", source=self.source)


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

    def assert_silent(self, payload):
        self.assertEqual(set(payload), {"continue", "completion_recording"})
        self.assertIs(payload["continue"], True)
        self.assertIsInstance(payload["completion_recording"]["status"], str)

    def notice(self, payload):
        context = self.context(payload)
        self.assertTrue(context, "first business tool received no actionable completion preflight")
        self.assertIs(payload.get("continue"), True)
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertNotIn("permissionDecision", payload["hookSpecificOutput"])
        self.assertNotIn("before the first final response", context)
        if "completion-preflight: prepared" in context:
            self.assertIn("Native Stop handles publication", context)
            self.assertIn("verification-before-completion/references/autopilot-publication.md", context)
            self.assertIn("work-unit: report-unit", context)
            self.assertIn("contract-revision: 1", context)
            self.assertNotIn("--receipt-token", context)
        else:
            self.assertIn("publication-protocol: legacy-v1", context)
            self.assertIn("Explicit v1 callers", context)
            self.assertIn("original verification time and proof inputs", context)
        return context

    def prepare_command(self, context):
        commands = [line for line in context.splitlines()
                    if line.startswith("env ") and "autopilot_completion.py" in line
                    and "prepare" in shlex.split(line)]
        self.assertEqual(len(commands), 1)
        args = shlex.split(commands[0])
        self.assertIn("prepare", args)
        if "autopilot-completion-receipt.v2" in args:
            self.assertIn("--schema-version", args)
            self.assertNotIn("--reapprove-current-input", args)
        else:
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
        self.assert_silent(self.hook(payload={"hook_event_name": "PreToolUse", "session_id": "session-1",
            "cwd": str(self.root), "tool_name": "UnknownTool", "tool_input": {"command": "publish or delete"}}))
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
        self.assert_silent(self.hook())
        self.change_intent({"constraints": ["Only the current approved report"]})
        third = self.notice(self.hook())
        self.assertNotEqual(second, third)
        self.assert_silent(self.hook())
        self.assertEqual(len(self.database_rows("ap_completion_origins")), 3)
        self.assertEqual(len(self.database_rows("ap_completion_preflight_notices")), 3)

    def test_off_unsupported_and_no_unmet_admitted_criteria_stay_silent(self):
        original = self.read_state()["acceptance_criteria"]
        self.run_dir.mkdir(parents=True); (self.run_dir / "OFF").write_text("")
        self.assert_silent(self.hook())
        (self.run_dir / "OFF").unlink(); self.run_dir.rmdir()
        self.assert_silent(self.hook(env=self.environment(GHOST_ALICE_PLATFORM="agent-runtime")))
        self.change_intent({"acceptance_criteria": [dict(row, admitted=False) for row in original]})
        self.assert_silent(self.hook())
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
        self.assert_silent(self.hook())

    def test_off_precedes_missing_root_discovery_of_malformed_authority(self):
        candidate = self.root / '.tmp/session-intent'
        candidate.mkdir(parents=True)
        ledger = candidate / 'ghost-state.sqlite3'
        malformed = b'not a SQLite session authority\n'
        ledger.write_bytes(malformed)
        for platform in ('codex', 'claude'):
            selected = self.root / '.autopilot/sessions' / platform / 'session-1'
            for target in (self.root / '.autopilot', selected):
                with self.subTest(platform=platform, target=str(target)):
                    target.mkdir(parents=True, exist_ok=True)
                    marker = target / 'OFF'
                    marker.write_text('paused\n')
                    env = self.environment(GHOST_ALICE_PLATFORM=platform,
                        GHOST_ALICE_AUTOPILOT_CWD=str(self.root),
                        GHOST_ALICE_COMPLETION_SCHEMA='v2')
                    env.pop('GHOST_ALICE_SESSION_INTENT_ROOT', None)
                    env.pop('GHOST_ALICE_AUTOPILOT_RUN_DIR', None)
                    if target == selected:
                        env['GHOST_ALICE_AUTOPILOT_RUN_DIR'] = str(selected)
                    self.assert_silent(self.hook(env=env))
                    self.assertEqual(ledger.read_bytes(), malformed)
                    self.assertEqual(list(candidate.iterdir()), [ledger])
                    self.assertEqual([path for path in target.rglob('*') if path.is_file()], [marker])
                    marker.unlink()

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
        """Seed a prepared receipt with bound scope and plan but no tasks."""
        self.conduct_feedback()
        sys.path.insert(0, str(SKILL / "scripts"))
        from autopilot_session_bridge import bridge_session_intent_to_run_state
        result = bridge_session_intent_to_run_state(intent_root=self.root / "intent", platform="codex",
            session_id="session-1", input_event_id=self.read_state()["latest_input_event_id"], run_dir=self.run_dir,
            current_work_item_id="session-intent-session-1",
            plan_path=str(self.root / ".tmp/implementation-plans/autopilot-session-intent.md"),
            source=self.source, approval_evidence={"decision": "approved", "source": "user-authorized-current-input"})
        self.assertEqual(result["mode"], "session-intent-task")
        with adapter.storage.transaction(self.run_dir, self.source) as store:
            run = store.run()
            from test_autopilot_state import _conduct_plan
            objects = {name: _conduct_plan() for name in ("conduct-plan.candidate.json", "conduct-plan.json")}
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
        previous, objects = self.legacy_partial_conduct_run()
        with adapter.storage.transaction(self.run_dir, self.source) as store:
            previous["scope"] = {"summary": previous["scope"]["summary"]}
            previous["approval_generation"] = adapter.SESSION_MATERIAL.approval_generation(previous)
            store.put_run(previous)
            for name, value in objects.items():
                store.write(name, dict(value, approval_generation=previous["approval_generation"]))
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
            fixtures.register_route(self.core, self.root / "intent", platform=platform)
            self.source["GHOST_ALICE_PLATFORM"] = platform
            self.run_dir = self.root / ".autopilot/sessions/claude/session-1"
        sys.path.insert(0, str(Path(self.source["GHOST_ALICE_CORE_ROOT"]) / "_shared"))
        import install_hooks
        home = self.root / "installed home"; (home / ".codex").mkdir(parents=True); (home / ".claude").mkdir()
        installed = home / ".agents/skills/autopilot-mode"; shutil.copytree(SKILL, installed)
        env = self.environment(HOME=str(home), CODEX_HOME=str(home / ".codex"))
        with mock.patch.dict(os.environ, env, clear=True):
            install_hooks.install_hook(platform, addon_sources=[str(ROOT)], skills_dir=installed.parent)
        config = home / (".codex/hooks.json" if platform == "codex" else ".claude/settings.json")
        hooks = json.loads(config.read_text())["hooks"]
        def installed_hook(event, marker, text=None):
            command = next(row["command"] for entry in hooks[event] for row in entry.get("hooks", [])
                           if marker in row.get("command", ""))
            result = subprocess.run(command, shell=True, env=env, cwd=self.root,
                input=json.dumps({"hook_event_name": event, "session_id": "session-1", "cwd": str(self.root),
                    **({"last_assistant_message": text} if text is not None else {})}),
                text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            log = home / ".ghost-alice/session-logs" / platform / "session-1/strict-hook-output.jsonl"
            rows = [json.loads(line) for line in log.read_text().splitlines()]
            self.native_recording = json.loads(rows[-1]["stdout"])
            return json.loads(result.stdout)
        preflight = installed_hook("PreToolUse", "[adapter:autopilot-mode] prepare-origin")
        context = self.notice(preflight)
        self.assertNotIn("--receipt-token", context)
        prepared = self.native_recording["completion_recording"]
        self.assertEqual(prepared["status"], "prepared")
        self.run_dir = Path(prepared["run_dir"])
        self.assertFalse((self.run_dir / "approved-run.json").exists())
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
        # The native v2 path consumes the owning Core review of this exact proof.
        # Reuse the fixture's explicit synthetic observations; do not rerun work.
        from test_completion_contract_v2 import _CompletionV2Fixture
        self.review_candidates = {}
        self.review_records = {}
        _CompletionV2Fixture.review(self, proof, original_result["verified_at"])
        before_stop = self.core.read_session_state(root=self.root / "intent", platform=platform, session_id="session-1")
        self.assertEqual({row["status"] for row in before_stop["acceptance_criteria"]}, {"unmet"})
        repeat = installed_hook("PreToolUse", "[adapter:autopilot-mode] prepare-origin")
        self.assertNotIn("hookSpecificOutput", repeat)
        self.assertEqual(self.native_recording["completion_recording"]["receipt_token"], prepared["receipt_token"])
        stopped = installed_hook("Stop", "[adapter:autopilot-mode] continue", proof)
        self.assertEqual(stopped.get("systemMessage", ""), "", stopped)
        self.assertNotEqual(stopped.get("decision"), "block", stopped)
        applied = self.native_recording["completion_recording"]["units"][0]
        self.assertEqual(applied["unit_status"], "complete", stopped)
        self.assertEqual((self.root / "inspection-count").read_text(), "1")
        self.assertFalse((self.run_dir / "tasks.jsonl").exists())
        with adapter.storage.transaction(self.run_dir, dict(self.source,
                GHOST_ALICE_COMPLETION_SCHEMA="autopilot-completion-receipt.v2")) as store:
            records = [json.loads(row[0]) for row in store.connection.execute(
                "SELECT body_json FROM ap_completion_v2_records WHERE kind='evidence'")]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["proof"], proof)
            self.assertEqual(records[0]["verified_at"], original_result["verified_at"])
        after_stop = self.core.read_session_state(root=self.root / "intent", platform=platform, session_id="session-1")
        self.assertEqual({row["status"] for row in after_stop["acceptance_criteria"]}, {"met"})

    def test_installed_codex_preflight_prepare_publish_needs_no_stop_recovery(self):
        self.installed_lifecycle("codex")

    def test_installed_claude_preflight_prepare_publish_needs_no_stop_recovery(self):
        self.installed_lifecycle("claude")


if __name__ == "__main__":
    unittest.main()
