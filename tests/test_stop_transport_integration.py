"""Exercise the real Stop CLI and supported delivery observations separately.

Transcript rows are local fixtures of Core-supported native actor shapes. They
do not establish that an installed provider delivered Stop feedback to a model.
"""
from __future__ import annotations

import copy
import importlib
import json
import os
import subprocess
import sys

import test_completion_contract_v2 as fixture
import test_completion_publication as publication
from response_route_fixture import native_ledger, register_route
from test_claude_native_receipt import stop_blocking_attachment


class StopTransportIntegrationTest(fixture._CompletionV2Fixture):
    def use_claude(self):
        criteria = self.read_state()['acceptance_criteria']
        self.source['GHOST_ALICE_PLATFORM'] = 'claude'
        self.run_dir = self.root / '.autopilot/sessions/claude/session-1'
        self.state_path = native_ledger(self.root / 'intent', self.core,
            platform='claude', criteria=criteria)
        self.arguments.update(platform='claude', source=self.source)

    def read_state(self):
        return self.core.read_session_state(root=self.root / 'intent',
            platform=self.source['GHOST_ALICE_PLATFORM'], session_id='session-1')

    def admit(self, *, independent=False):
        self.items = [dict(id=identity, status='ready', depends_on=[],
            acceptance_criteria=[criterion], allowed_surface=[str(self.root)],
            focus_layer='meso', prompt='Approved local step '+identity,
            criterion_refs=[dict(work_unit_id='report-unit', contract_revision=1,
                criterion_id=criterion)]) for identity, criterion in
            ([('first', 'report'), ('second', 'protected')] if independent else [('first', 'report')])]
        module = importlib.import_module('autopilot_continuation')
        module.register_continuation_plan(self.source,
            expected_input_event_id=self.read_state()['latest_input_event_id'], items=self.items,
            approval_evidence=dict(decision='approved', source='user: approved fixture steps'),
            allowed_surfaces=[str(self.root)])
        self.feedback()

    def feedback(self, *, control='active', purpose=None):
        return register_route(self.core, self.root / 'intent',
            platform=self.source['GHOST_ALICE_PLATFORM'],
            purpose=purpose,
            continuation_feedback=dict(schema_version='continuation-feedback.v1', control=control,
                required_unprocessed=[dict(criterion_refs=copy.deepcopy(item['criterion_refs']),
                    reason='The current approved step remains necessary.',
                    next_information='Read the current input for '+item['id']+'.',
                    source_refs=[], prerequisite='ready') for item in self.items]))

    def native_stop(self, *, transcript=None, last_message=None):
        hook = dict(hook_event_name='Stop', session_id='session-1', cwd=str(self.root))
        if transcript is not None:
            hook['transcript_path'] = str(transcript)
        if last_message is not None:
            hook['last_assistant_message'] = last_message
        result = subprocess.run([sys.executable, '-B',
            str(publication.SKILL / 'adapters/autopilot_mode.py')],
            input=json.dumps(hook), cwd=self.root,
            env={**os.environ, **self.source, 'CODEX_THREAD_ID': 'session-1'},
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')
        return json.loads(result.stdout)

    def notification(self, text, *, human=False):
        row = dict(type='user', sessionId='session-1', uuid='native-notification-1',
            timestamp='2026-10-14T12:00:00Z',
            message=dict(role='user', content=text))
        if not human:
            row.update(promptSource='system', turnOrigin='task_notification',
                origin=dict(kind='task-notification', producer='session-task'))
        path = self.root / 'claude-transcript.jsonl'
        path.write_text(json.dumps(row)+'\n')
        return path

    def plan_state(self):
        with self.helper.storage.transaction(self.run_dir, self.source, migrate_legacy=False) as store:
            return store.continuation_plan()

    def pending_stop_receipt(self, *, completed=False):
        self.use_claude()
        self.admit()
        sent = self.native_stop()
        transcript = self.root / 'historical-stop-receipt.jsonl'
        transcript.write_text(json.dumps(stop_blocking_attachment(
            sent['reason'], session_id='session-1'))+'\n')
        if completed:
            receipt = self.prepare()
            self.proof()
            self.publish(receipt)
            self.native_stop()
            with self.helper.storage.transaction(self.run_dir, self.source, migrate_legacy=False) as store:
                plan = store.continuation_plan()
                self.assertEqual(plan['items'][0]['status'], 'completed')
                # These immutable originals model a prior action; this
                # receipt-observation path never executes that action.
                request = plan['delivery_requests']['first']
                start = dict(schema_version='autopilot-task-action.v1', plan_id=plan['plan_id'],
                    task_id='first', request_id=request['request_id'], action_id='prior-action',
                    operation='Prior synthetic action', resources=[str(self.root)],
                    started_at='2026-10-14T11:00:00Z', outcome='started')
                store.completion_put('continuation-action-started', start)
                store.completion_put('continuation-action-finished', dict(start,
                    outcome='returned', finished_at='2026-10-14T11:00:01Z'))
                import autopilot_delivery
                autopilot_delivery.project(store, plan)
                store.set_continuation_plan(plan)
        return transcript

    def new_read_only_input(self):
        state = self.read_state()
        self.core.record_turn(root=self.root / 'intent', platform='claude', session_id='session-1',
            raw_user_input='Explain the existing record without doing more work.',
            expected_input_event_id=state['latest_input_event_id'])

    def assert_receipt_only(self, before, after):
        self.assertEqual(after['items'], before['items'])
        self.assertEqual(after['dispatches'], before['dispatches'])
        self.assertEqual(after['events'][:-1], before['events'])
        self.assertEqual(after['events'][-1]['event'], 'continuation-host-received')
        expected = copy.deepcopy(before['delivery_requests']['first'])
        expected.update(status='received', host_receipt='observed',
            receipt=after['delivery_requests']['first']['receipt'],
            receipt_ref=after['delivery_requests']['first']['receipt_ref'])
        self.assertTrue(expected['receipt_ref'])
        self.assertEqual(after['delivery_requests']['first'], expected)
        self.assertEqual(expected['receipt']['verified_at'], '2026-10-15T05:28:14.943Z')

    def test_stale_route_records_prior_receipt_without_replaying_completed_action(self):
        transcript = self.pending_stop_receipt(completed=True)
        self.new_read_only_input()
        before = self.plan_state()
        state = self.read_state()
        self.assertEqual(self.core.read_response_route(root=self.root / 'intent',
            platform='claude', session_id='session-1')['status'], 'stale')
        result = self.native_stop(transcript=transcript)
        after = self.plan_state()
        self.assertEqual(after['delivery_requests']['first']['host_receipt'], 'observed')
        self.assert_receipt_only(before, after)
        self.assertEqual(self.read_state(), state)
        self.assertNotIn('decision', result)
        self.native_stop(transcript=transcript)
        self.assertEqual(self.plan_state(), after)
        self.assertEqual(self.read_state(), state)

    def test_changed_selected_contract_records_receipt_without_new_dispatch(self):
        transcript = self.pending_stop_receipt()
        state = self.read_state()
        criteria = copy.deepcopy(state['acceptance_criteria'])
        for row in criteria:
            row.update(contract_revision=2, summary=row['summary']+' changed')
        self.core.record_turn(root=self.root / 'intent', platform='claude', session_id='session-1',
            intent_delta=dict(acceptance_criteria=criteria),
            expected_input_event_id=state['latest_input_event_id'])
        register_route(self.core, self.root / 'intent', platform='claude')
        before = self.plan_state()
        state = self.read_state()
        result = self.native_stop(transcript=transcript)
        self.assertEqual(self.plan_state()['delivery_requests']['first']['host_receipt'], 'observed')
        self.assert_receipt_only(before, self.plan_state())
        self.assertEqual(self.read_state(), state)
        self.assertNotIn('decision', result)
        self.assertEqual(result['continuation']['reason'], 'current-plan-contract-changed')

    def test_unselected_current_route_records_only_the_prior_receipt(self):
        transcript = self.pending_stop_receipt()
        register_route(self.core, self.root / 'intent', platform='claude', selected=[], mode='direct-response')
        before = self.plan_state()
        state = self.read_state()
        result = self.native_stop(transcript=transcript)
        self.assertEqual(self.plan_state()['delivery_requests']['first']['host_receipt'], 'observed')
        self.assert_receipt_only(before, self.plan_state())
        self.assertEqual(self.read_state(), state)
        self.assertNotIn('decision', result)

    def test_pause_prevents_even_historical_receipt_recording(self):
        transcript = self.pending_stop_receipt()
        self.new_read_only_input()
        before = self.plan_state()
        (self.run_dir / 'OFF').write_text('paused\n')
        self.native_stop(transcript=transcript)
        self.assertEqual(self.plan_state(), before)

    def test_foreign_or_corrupt_plan_cannot_adopt_exact_old_receipt(self):
        transcript = self.pending_stop_receipt()
        self.new_read_only_input()
        original = self.plan_state()
        for corruption in ('foreign-target', 'immutable-record'):
            with self.subTest(corruption=corruption):
                with self.helper.storage.transaction(self.run_dir, self.source, migrate_legacy=False) as store:
                    original_record = store.completion_read('continuation-plan', original['plan_id'])
                    plan = copy.deepcopy(original)
                    if corruption == 'foreign-target':
                        plan['target']['owner']['session_id'] = 'foreign'
                        store.set_continuation_plan(plan)
                    else:
                        changed = dict(original_record, boundary_ref='corrupt')
                        changed_row = store.connection.execute('UPDATE ap_completion_v2_records SET body_json=? '
                            'WHERE run_key=? AND kind=? AND identity=?',
                            (json.dumps(changed), store.key, 'continuation-plan', original['plan_id']))
                        self.assertEqual(changed_row.rowcount, 1)
                self.native_stop(transcript=transcript)
                self.assertEqual(self.plan_state(), plan)
                with self.helper.storage.transaction(self.run_dir, self.source, migrate_legacy=False) as store:
                    store.set_continuation_plan(original)
                    store.connection.execute('UPDATE ap_completion_v2_records SET body_json=? '
                        'WHERE run_key=? AND kind=? AND identity=?',
                        (json.dumps(original_record), store.key, 'continuation-plan', original['plan_id']))

    def test_immutable_foreign_runtime_binding_cannot_adopt_matching_receipt(self):
        transcript = self.pending_stop_receipt()
        self.new_read_only_input()
        original, native_text = self.plan_state(), transcript.read_text()
        for field in ('path', 'owner', 'authority'):
            with self.subTest(field=field):
                with self.helper.storage.transaction(self.run_dir, self.source, migrate_legacy=False) as store:
                    plan = copy.deepcopy(original)
                    definition = store.completion_read('continuation-plan', original['plan_id'])
                    definition['target'][field] = (dict(platform='claude', session_id='foreign')
                        if field == 'owner' else str(self.root / 'foreign'))
                    plan['target'] = copy.deepcopy(definition['target'])
                    plan['plan_id'] = store.completion_put('continuation-plan', definition)
                    store.set_continuation_plan(plan)
                # Immutable/mutable plan and native declaration agree with
                # each other, while differing from the actual runtime binding.
                transcript.write_text(native_text.replace(original['plan_id'], plan['plan_id']))
                result = self.native_stop(transcript=transcript)
                self.assertEqual(self.plan_state(), plan)
                self.assertNotIn('decision', result)

    def test_real_stop_cli_emits_host_block_for_current_approved_feedback_once(self):
        self.admit()
        result = self.native_stop()
        self.assertEqual(result['decision'], 'block')
        self.assertEqual(result['reason'], result['systemMessage'])
        request = self.plan_state()['delivery_requests']['first']
        self.assertIn('request-id: '+request['request_id'], result['reason'])
        self.assertEqual((request['host_receipt'], request['execution']), ('unknown', 'unknown'))
        repeated = self.native_stop()
        self.assertFalse(repeated['continuation']['request_emitted'])
        self.assertNotIn('decision', repeated)

    def test_unreviewed_claude_plan_still_requests_approved_necessary_action(self):
        self.use_claude()
        self.admit()
        self.feedback(purpose='explanation')
        message = 'The plan is registered. The approved next step will append the handoff section.'
        result = self.native_stop(last_message=message)
        # Missing response review must still reject completion recording; it
        # cannot remove independent authority to request the approved action.
        self.assertEqual(result['completion_recording']['reason'], 'completion-body-invalid')
        self.assertIn('response-assessment-required', result['completion_recording']['detail'])
        self.assertEqual(result.get('decision'), 'block')
        request = self.plan_state()['delivery_requests']['first']
        self.assertIn('request-id: '+request['request_id'], result['reason'])
        self.assertEqual((request['host_receipt'], request['execution']), ('unknown', 'unknown'))
        self.assertTrue(all(c['status'] == 'unmet' for c in self.read_state()['acceptance_criteria']))
        repeated = self.native_stop(last_message=message)
        self.assertFalse(repeated['continuation']['request_emitted'])
        self.assertNotIn('decision', repeated)

    def test_unreviewed_claude_plan_cannot_override_current_stop(self):
        self.use_claude()
        self.admit()
        self.feedback(control='human-stop', purpose='explanation')
        result = self.native_stop(last_message='The approved plan is registered.')
        self.assertEqual(result['completion_recording']['reason'], 'completion-body-invalid')
        self.assertEqual(result['continuation']['reason'], 'human-stop')
        self.assertNotIn('decision', result)
        self.assertEqual(self.plan_state()['delivery_requests'], {})

    def test_claude_stop_observes_supported_notification_without_asserting_execution(self):
        self.use_claude()
        self.admit()
        sent = self.native_stop()
        self.assertEqual(sent['decision'], 'block')
        transcript = self.notification(sent['reason'])
        result = self.native_stop(transcript=transcript)
        request = result['continuation']['delivery_requests']['first']
        self.assertEqual((request['status'], request['host_receipt'], request['execution']),
            ('received', 'observed', 'unknown'))
        self.assertEqual(request['receipt']['locator'], str(transcript)+':1')
        self.assertEqual(request['receipt']['verified_at'], '2026-10-14T12:00:00Z')
        self.assertEqual(self.plan_state()['items'][0]['status'], 'ready')
        self.assertNotIn('decision', result)
        events = self.plan_state()['events']
        self.native_stop(transcript=transcript)
        self.assertEqual(self.plan_state()['events'], events)

    def test_claude_stop_observes_actual_blocking_shape_without_asserting_execution(self):
        self.use_claude()
        self.admit()
        sent = self.native_stop()
        transcript = self.root / 'claude-stop-blocking.jsonl'
        # The envelope comes from actual Claude row 628; the declaration is
        # the real Stop CLI output for this isolated approved fixture plan.
        transcript.write_text(json.dumps(stop_blocking_attachment(
            sent['reason'], session_id='session-1'))+'\n')
        result = self.native_stop(transcript=transcript)
        request = result['continuation']['delivery_requests']['first']
        self.assertEqual((request['status'], request['host_receipt'], request['execution']),
            ('received', 'observed', 'unknown'))
        self.assertEqual(request['receipt']['native_actor'], 'stop-hook-blocking-error')
        self.assertEqual(request['receipt']['verified_at'], '2026-10-15T05:28:14.943Z')
        self.assertEqual(self.plan_state()['items'][0]['status'], 'ready')
        self.assertNotIn('decision', result)
        events = self.plan_state()['events']
        self.native_stop(transcript=transcript)
        self.assertEqual(self.plan_state()['events'], events)

    def test_claude_human_echo_cannot_acknowledge_or_resend_pending_feedback(self):
        self.use_claude()
        self.admit()
        sent = self.native_stop()
        result = self.native_stop(transcript=self.notification(sent['reason'], human=True))
        request = result['continuation']['delivery_requests']['first']
        self.assertEqual((request['host_receipt'], request['execution']), ('unknown', 'unknown'))
        self.assertFalse(result['continuation']['request_emitted'])
        self.assertNotIn('decision', result)

    def test_unknown_receipt_does_not_hold_an_independent_current_approved_task(self):
        self.admit(independent=True)
        first = self.native_stop()
        self.assertEqual(first['continuation']['task_id'], 'first')
        second = self.native_stop()
        self.assertEqual(second['continuation']['task_id'], 'second')
        self.assertEqual(second['decision'], 'block')
        self.assertEqual(self.plan_state()['delivery_requests']['first']['host_receipt'], 'unknown')

    def test_current_human_stop_precedes_supported_delivery_observation(self):
        self.use_claude()
        self.admit()
        sent = self.native_stop()
        transcript = self.notification(sent['reason'])
        self.feedback(control='human-stop')
        result = self.native_stop(transcript=transcript)
        self.assertEqual(result['continuation']['reason'], 'human-stop')
        self.assertFalse(result['continuation']['request_emitted'])
        self.assertNotIn('decision', result)
        self.assertEqual(self.plan_state()['items'][0]['status'], 'ready')
        self.assertEqual(self.plan_state()['delivery_requests']['first']['host_receipt'], 'observed')
