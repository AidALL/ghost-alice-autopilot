"""A native v2 Stop advances admitted tasks, independently of response closure."""
import importlib
import copy
import hashlib
import json
import os
from unittest import mock
from test_completion_contract_v2 import _CompletionV2Fixture
from response_route_fixture import register_route


class CompletionContinuationTest(_CompletionV2Fixture):
    def test_request_emits_exact_native_receipt_binding(self):
        self.plan([self.item('first','report')])
        result=self.stop()
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            plan=store.continuation_plan()
        self.assertIn('plan-id: '+plan['plan_id'],result['systemMessage'])
        self.assertIn('request-fingerprint: '+plan['delivery_requests']['first']['fingerprint'],result['systemMessage'])
        self.assertIn('request-id: '+plan['delivery_requests']['first']['request_id'],result['systemMessage'])

    def test_reopened_task_cannot_reuse_prior_host_receipt(self):
        self.plan([self.item('first','report')])
        sent=self.stop()
        transcript=self.root/'old-receipt.jsonl'
        rows=[dict(type='session_meta',payload=dict(id='session-1')),
            dict(type='event_msg',timestamp='2026-10-14T12:00:00Z',payload=dict(
                type='item_completed',thread_id='session-1',turn_id='old-turn',
                item=dict(type='HookPrompt',id='old-hook',fragments=[dict(text=sent['systemMessage'])])))]
        transcript.write_text('\n'.join(json.dumps(row) for row in rows)+'\n')
        artifact=self.root/'proof-artifact';artifact.write_text('original')
        receipt=self.prepare();self.proof();self.publish(receipt,dependencies=[str(artifact)])
        self.stop()
        artifact.write_text('changed')
        import test_completion_publication as fixture
        hook=dict(hook_event_name='Stop',session_id='session-1',cwd=str(self.root),transcript_path=str(transcript))
        fixture.adapter.adapter_payload_from_env(self.source,hook_input=hook)
        # The failed publication is parked independently. Exercise the real
        # authorized continuation consumer after invalidation, without claiming
        # the stale publication was repaired or replaying business work.
        continuation=importlib.import_module('autopilot_continuation')
        requeued=continuation.advance_continuation(self.source,{},hook_input=hook)
        self.assertTrue(requeued['continuation']['request_emitted'])
        result=continuation.advance_continuation(self.source,{},hook_input=hook)
        request=result['continuation']['delivery_requests']['first']
        self.assertEqual(request['host_receipt'],'unknown')
        self.assertEqual(request['execution'],'unknown')

    def test_native_stop_records_receipt_once_without_promoting_task_execution(self):
        self.plan([self.item('first','report')])
        sent=self.stop()
        transcript=self.root/'receipt-native.jsonl'
        rows=[dict(type='session_meta',payload=dict(id='session-1')),
            dict(type='event_msg',timestamp='2026-10-14T12:00:00Z',payload=dict(
                type='item_completed',thread_id='session-1',turn_id='turn',
                item=dict(type='HookPrompt',id='hook',fragments=[dict(text=sent['systemMessage'])])))]
        transcript.write_text('\n'.join(json.dumps(row) for row in rows)+'\n')
        import test_completion_publication as fixture
        hook=dict(hook_event_name='Stop',session_id='session-1',cwd=str(self.root),transcript_path=str(transcript))
        result=fixture.adapter.adapter_payload_from_env(self.source,hook_input=hook)
        request=result['continuation']['delivery_requests']['first']
        self.assertFalse(result['continuation']['request_emitted'])
        self.assertEqual((request['status'],request['host_receipt'],request['execution']),('received','observed','unknown'))
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            before=store.continuation_plan()
        self.assertEqual(before['items'][0]['status'],'ready')
        fixture.adapter.adapter_payload_from_env(self.source,hook_input=hook)
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            after=store.continuation_plan()
        self.assertEqual(before['events'],after['events'])

    def item(self, identity, criterion, *, status='ready', depends=()):
        return dict(id=identity, status=status, depends_on=list(depends),
            acceptance_criteria=[criterion], allowed_surface=[str(self.root)],
            focus_layer='meso', prompt='Execute '+identity,
            criterion_refs=[dict(work_unit_id='report-unit',contract_revision=1,criterion_id=criterion)])

    def plan(self, items):
        self._continuation_items=copy.deepcopy(items)
        try:
            module=importlib.import_module('autopilot_continuation')
        except ImportError:
            self.fail('v2 has no approved task scheduling bridge')
        result=module.register_continuation_plan(self.source,
            expected_input_event_id=self.read_state()['latest_input_event_id'], items=items,
            approval_evidence=dict(decision='approved',source='user: continue these local stages'),
            allowed_surfaces=[str(self.root)])
        self.feedback()
        return result

    def feedback(self, *, purpose=None, control='active', required=None, prerequisite='ready'):
        """Declare the fixture model's current necessity; no old prompt is reused."""
        route=self.core.read_response_route(root=self.root/'intent',platform='codex',session_id='session-1')
        selected={(c['work_unit_id'],c['contract_revision'],c['criterion_id']) for c in route['selected_criteria']}
        rows=[dict(criterion_refs=copy.deepcopy(item['criterion_refs']),
            reason='The currently requested result still needs this approved step.',
            next_information='Read current needed input for '+item['id']+'.',
            source_refs=[],prerequisite=prerequisite) for item in self._continuation_items
            if all((r['work_unit_id'],r['contract_revision'],r['criterion_id']) in selected for r in item['criterion_refs'])]
        return register_route(self.core,self.root/'intent',selected=route['selected_work_unit_ids'],
            purpose=route.get('response_purpose') if purpose is None else purpose,
            continuation_feedback=dict(schema_version='continuation-feedback.v1',control=control,
                required_unprocessed=rows if required is None else required))

    def followup(self):
        super().followup()
        if hasattr(self,'_continuation_items'):self.feedback()

    def test_recursive_surface_rejects_parent_traversal_before_admission(self):
        module=importlib.import_module('autopilot_continuation')
        approved=str(self.root/'approved')
        escaped=approved+'/../outside'
        self.assertFalse(module._surface_allowed(escaped,[approved+'/...']))
        self.assertFalse(module._surface_allowed('approved/../outside',['approved/...']))
        self.assertTrue(module._surface_allowed(approved+'/nested/file',[approved+'/...']))
        self.assertTrue(module._surface_allowed('approved/nested/file',['approved/...']))
        item=self.item('outside','report')
        item['allowed_surface']=[escaped]
        with self.assertRaisesRegex(ValueError,'approved allowed surfaces'):
            module.register_continuation_plan(self.source,
                expected_input_event_id=self.read_state()['latest_input_event_id'],items=[item],
                approval_evidence=dict(decision='approved',source='user: approved subtree'),
                allowed_surfaces=[approved+'/...'])
        self.assertIsNone(self.helper.storage.read_continuation_plan(
            self.run_dir,module.service._v2_authority(self.source)))

    def partial(self, *, dependencies=None):
        receipt=self.prepare(); self.proof()
        # One owning criterion is supported, leaving the second actual action open.
        self.completion=self.completion.replace('  - protected: Protected file is unchanged [source: user-explicit]\n','')
        self.completion=self.completion[:self.completion.index('  - claim: Protected file')]+ '- unverified: none\n'
        return self.publish(receipt, dependencies=dependencies)

    def test_partial_application_dispatches_dependency_ready_second_stage(self):
        self.plan([self.item('first','report',status='running'),self.item('second','protected',depends=['first'])])
        self.partial()
        result=self.stop()
        self.assertEqual(result['continuation']['status'],'pending')
        self.assertEqual(result['continuation']['task_id'],'second')
        self.assertIn('next-information: Read current needed input for second.',result['systemMessage'])
        self.assertNotIn('Execute second',result['systemMessage'])
        self.assertEqual([c['status'] for c in self.read_state()['acceptance_criteria']],['met','unmet'])

    def test_native_request_preserves_task_state_until_execution_is_observed(self):
        self.plan([self.item('first','report'),self.item('second','protected',depends=['first'])])
        result=self.stop()
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            plan=store.continuation_plan()
        self.assertEqual(plan['items'][0]['status'],'ready')
        self.assertEqual(result['continuation']['status'],'pending')
        self.assertTrue(result['continuation']['request_emitted'])
        request=plan['delivery_requests']['first']
        self.assertEqual(request['host_receipt'],'unknown')
        self.assertEqual(request['execution'],'unknown')
        self.assertEqual(plan['events'][-1]['event'],'continuation-request-queued')
        repeated=self.stop()
        self.assertEqual(repeated['continuation']['status'],'pending')
        self.assertFalse(repeated['continuation']['request_emitted'])
        self.assertNotIn('Execute first',repeated['systemMessage'])
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            self.assertEqual(store.continuation_plan()['events'],plan['events'])

    def test_legacy_dispatch_preserves_historical_state_without_resend_or_ack(self):
        self.plan([self.item('first','report'),self.item('second','protected',depends=['first'])])
        self.stop()
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            plan=store.continuation_plan()
            definition=store.completion_read('continuation-plan',plan['plan_id'])
            definition.pop('delivery_protocol')
            plan['plan_id']=store.completion_put('continuation-plan',definition)
            plan.pop('delivery_protocol')
            plan.pop('delivery_requests',None)
            plan['items'][0]['status']='running'
            plan['events'][-1]['event']='task-dispatched'
            historical_events=plan['events'][:]
            store.set_continuation_plan(plan)
        result=self.stop()
        self.assertEqual(result['continuation']['status'],'unknown')
        self.assertFalse(result['continuation']['request_emitted'])
        self.assertNotIn('Execute first',result['systemMessage'])
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            plan=store.continuation_plan()
        self.assertEqual(plan['items'][0]['status'],'running')
        self.assertEqual(plan['events'],historical_events)
        self.assertEqual(plan['delivery_requests']['first']['host_receipt'],'unknown')
        self.assertEqual(plan['delivery_requests']['first']['execution'],'unknown')

    def test_pending_native_request_survives_independent_recording_park(self):
        import autopilot_mode
        payload=dict(systemMessage='Execute approved second', recovery=dict(status='parked'),
            continuation=dict(status='pending',request_emitted=True))
        result=autopilot_mode._format_payload_for_hook(payload,{'hook_event_name':'Stop'})
        self.assertEqual(result.get('decision'),'block')
        payload['continuation']['request_emitted']=False
        self.assertNotIn('decision',autopilot_mode._format_payload_for_hook(payload,{'hook_event_name':'Stop'}))
        payload.pop('continuation')
        payload['recovery'] = dict(status='diagnostic', claims_changed=True)
        self.assertNotIn('decision',autopilot_mode._format_payload_for_hook(payload,{'hook_event_name':'Stop'}))

    def legacy_parent(self, *, dependent):
        from test_completion_corrections import CompletionCorrectionTest
        self.plan([self.item('first','report',status='running'),
            self.item('second','protected',depends=['first'] if dependent else [])])
        CompletionCorrectionTest.seed_legacy_wrong_mapping(self,applied=True,report_only=True)
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            plan=store.continuation_plan();plan['items'][0]['status']='completed';store.set_continuation_plan(plan)
        self.feedback(purpose='explanation')

    def test_unassessed_completed_parent_cannot_authorize_dependent_dispatch(self):
        self.legacy_parent(dependent=True)
        before=self.read_state()['acceptance_criteria']
        result=self.stop()
        self.assertFalse(result['continuation'].get('request_emitted'))
        self.assertNotEqual(result['continuation']['status'],'complete')
        self.assertEqual(self.read_state()['acceptance_criteria'],before)
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            self.assertEqual(store.continuation_plan()['items'][0]['status'],'completed')

    def test_unassessed_completed_parent_preserves_independent_dispatch(self):
        self.legacy_parent(dependent=False)
        result=self.stop()
        self.assertEqual(result['completion_recording']['units'][0]['unit_status'],'pending')
        self.assertEqual(result['continuation']['task_id'],'second')

    def test_unassessed_completed_parent_preserves_independent_running_task(self):
        self.legacy_parent(dependent=False)
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            plan=store.continuation_plan();plan['items'][1]['status']='running';store.set_continuation_plan(plan)
        result=self.stop()
        self.assertEqual(result['continuation'].get('task_id'),'second')

    def test_explanation_does_not_cancel_independent_ready_task(self):
        self.plan([self.item('waiting','report',status='blocked'),self.item('independent','protected')])
        self.feedback(purpose='explanation')
        result=self.stop()
        self.assertEqual(result['continuation']['task_id'],'independent')
        self.assertNotIn('Publish existing supported proof',result['systemMessage'])

    def unreviewed_plan_after_parent_dependency_change(self, *, dependent, unavailable=False):
        if unavailable and (not hasattr(os,'geteuid') or os.geteuid()==0):
            self.skipTest('Permission fixture requires POSIX non-root filesystem enforcement')
        self.plan([self.item('first','report',status='running'),
            self.item('second','protected',depends=['first'] if dependent else [])])
        # Both tasks are approved; the model declares no continuation yet.
        self.feedback(required=[])
        artifact=self.root/'parent-proof-dependency.txt'
        artifact.write_text('original')
        self.partial(dependencies=[str(artifact)])
        self.stop()
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            plan=store.continuation_plan()
            self.assertEqual(plan['items'][0]['status'],'completed')
        if unavailable:artifact.chmod(0)
        else:artifact.write_text('changed')
        self.feedback(purpose='explanation',required=[dict(
            criterion_refs=self.item('second','protected')['criterion_refs'],
            reason='Only the approved second action currently needs continuation.',
            next_information='Perform the second action after valid prerequisites.',
            source_refs=[],prerequisite='ready')])
        import test_completion_publication as fixture
        try:
            result=fixture.adapter.adapter_payload_from_env(self.source,hook_input=dict(
                hook_event_name='Stop',session_id='session-1',cwd=str(self.root),
                last_assistant_message='The approved plan is registered.'))
        finally:
            if unavailable:artifact.chmod(0o600)
        self.assertEqual(result['completion_recording']['reason'],'completion-body-invalid')
        # An independent action has no need to reread unrelated parent proof.
        invalidated=dependent and not unavailable
        self.assertEqual(self.read_state()['acceptance_criteria'][0]['status'],'unmet' if invalidated else 'met')
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            self.assertEqual(store.continuation_plan()['items'][0]['status'],'reopened' if invalidated else 'completed')
        return result

    def test_unreviewed_plan_cannot_release_child_of_changed_parent_proof(self):
        result=self.unreviewed_plan_after_parent_dependency_change(dependent=True)
        self.assertFalse(result['continuation']['request_emitted'])

    def test_unreviewed_plan_preserves_action_independent_of_changed_parent_proof(self):
        result=self.unreviewed_plan_after_parent_dependency_change(dependent=False)
        self.assertTrue(result['continuation']['request_emitted'])
        self.assertEqual(result['continuation']['task_id'],'second')

    def test_unreviewed_plan_holds_child_when_parent_proof_is_unavailable(self):
        result=self.unreviewed_plan_after_parent_dependency_change(dependent=True,unavailable=True)
        self.assertFalse(result['continuation']['request_emitted'])
        self.assertEqual(result['continuation']['retained_coverage_errors'][0]['criterion_id'],'report')

    def test_unreviewed_plan_preserves_action_independent_of_unavailable_parent_proof(self):
        result=self.unreviewed_plan_after_parent_dependency_change(dependent=False,unavailable=True)
        self.assertTrue(result['continuation']['request_emitted'])
        self.assertEqual(result['continuation']['task_id'],'second')

    def test_unreviewed_plan_does_not_invalidate_unrelated_met_sibling(self):
        self.plan([self.item('first','report',status='running')])
        report=self.root/'report-dependency.txt';report.write_text('report')
        sibling=self.root/'sibling-dependency.txt';sibling.write_text('original')
        receipt=self.prepare();self.proof()
        self.publish(receipt,criterion_dependencies=[dict(work_unit_id='report-unit',contract_revision=1,
            criterion_id=key,locators=[str(path)]) for key,path in [('report',report),('protected',sibling)]])
        self.stop()
        sibling.write_text('changed')
        self.feedback(purpose='explanation')
        import test_completion_publication as fixture
        result=fixture.adapter.adapter_payload_from_env(self.source,hook_input=dict(
            hook_event_name='Stop',session_id='session-1',cwd=str(self.root),
            last_assistant_message='Only the registered report task is currently needed.'))
        self.assertEqual(result['completion_recording']['reason'],'completion-body-invalid')
        self.assertEqual({c['id']:c['status'] for c in self.read_state()['acceptance_criteria']},
            {'report':'met','protected':'met'})
        self.assertFalse(result['continuation']['request_emitted'])

    def test_waiting_dependency_and_stopped_tasks_do_not_dispatch(self):
        self.plan([self.item('waiting','report',status='blocked'),self.item('dependent','protected',depends=['waiting'])])
        result=self.stop()
        self.assertEqual(result['continuation']['status'],'waiting')
        self.assertNotIn('Execute dependent',result['systemMessage'])

    def test_unchanged_pending_request_and_new_input_do_not_resend_it(self):
        self.plan([self.item('first','report'),self.item('second','protected',depends=['first'])])
        self.assertTrue(self.stop()['continuation']['request_emitted'])
        self.assertEqual(self.stop()['continuation']['status'],'pending')
        self.followup()
        result=self.stop()
        self.assertEqual(result['continuation']['status'],'pending')
        self.assertFalse(result['continuation']['request_emitted'])

    def test_stopped_item_does_not_block_independent_work(self):
        self.plan([self.item('stopped','report',status='stopped'),self.item('second','protected')])
        self.assertEqual(self.stop()['continuation']['task_id'],'second')

    def test_unowned_reference_cannot_create_authority(self):
        item=self.item('forged','report')
        item['criterion_refs'][0]['work_unit_id']='unowned'
        with self.assertRaisesRegex(ValueError,'current|owned|selected'):
            self.plan([item])

    def test_running_task_cannot_bypass_unmet_dependency(self):
        self.plan([self.item('waiting','report',status='blocked'),
                   self.item('dependent','protected',status='running',depends=['waiting'])])
        result=self.stop()
        self.assertFalse(result['continuation'].get('request_emitted'))

    def test_partial_response_applies_accepted_proof_and_dispatches(self):
        self.plan([self.item('first','report',status='running'),self.item('second','protected',depends=['first'])])
        self.partial()
        self.feedback(purpose='explanation')
        result=self.stop()
        self.assertEqual(result['continuation']['task_id'],'second')
        self.assertEqual(result['completion_recording']['units'][0]['status'],'applied')

    def test_waiting_preserves_actual_recording_diagnostic(self):
        self.plan([self.item('waiting','report',status='blocked'),self.item('dependent','protected',depends=['waiting'])])
        import autopilot_continuation as continuation
        import autopilot_mode
        service=continuation.service
        self.prepare()
        # Execution-only Stop has no new publication obligation. Consume the
        # actual before-tool preparation diagnostic whose preservation is tested.
        current,root,target,material,selection=service._v2_material(self.source)
        diagnostic=service._bounded_v2_recovery(current,root,target,material,
            [dict(work_unit_id='report-unit',status='prepared')])
        result=continuation.advance_continuation(self.source,diagnostic)
        self.assertIn('completion-recording: prepared',diagnostic['systemMessage'])
        self.assertEqual(result['systemMessage'],diagnostic['systemMessage'])
        self.assertEqual(result['completion_recording'],diagnostic['completion_recording'])
        self.assertFalse(result['continuation']['request_emitted'])
        self.assertNotIn('decision',autopilot_mode._format_payload_for_hook(result,{'hook_event_name':'Stop'}))

    def test_plan_cannot_map_unselected_criterion(self):
        # Current route projection, not all accumulated ledger criteria, is authority.
        rows=self.read_state()['acceptance_criteria']
        rows.append(dict(id='other',summary='Other work',source='user-explicit',admitted=True,
            status='unmet',work_unit_id='other-unit',contract_revision=1))
        self.core.record_turn(root=self.root/'intent',platform='codex',session_id='session-1',
            intent_delta={'acceptance_criteria':rows},expected_input_event_id='event-1')
        register_route(self.core,self.root/'intent',selected=['report-unit'])
        item=self.item('outside','other');item['criterion_refs'][0]['work_unit_id']='other-unit'
        with self.assertRaisesRegex(ValueError,'current|owned|selected'): self.plan([item])

    def test_nonclosing_recording_failure_is_not_retried_without_change(self):
        import autopilot_work_items
        self.plan([self.item('first','report',status='running'),self.item('second','protected',depends=['first'])])
        self.partial()
        self.feedback(purpose='explanation')
        with mock.patch.object(autopilot_work_items,'materialize_met_criteria_v2',side_effect=OSError('recording defect')) as writer:
            self.stop();self.stop()
            self.assertEqual(writer.call_count,1)

    def test_independent_task_continues_while_recording_branch_is_parked(self):
        import autopilot_work_items
        self.plan([self.item('first','report',status='running'),self.item('second','protected')])
        self.partial()
        with mock.patch.object(autopilot_work_items,'materialize_met_criteria_v2',side_effect=OSError('recording defect')):
            result=self.stop()
        self.assertEqual(result['continuation'].get('task_id'),'second')

    def test_independent_running_task_survives_scoped_writer_failure(self):
        import autopilot_work_items
        self.plan([self.item('first','report',status='running'),
            self.item('second','protected',status='running')])
        self.partial()
        with mock.patch.object(autopilot_work_items,'materialize_met_criteria_v2',side_effect=OSError('report recording defect')):
            result=self.stop()
        self.assertEqual(result['continuation'].get('task_id'),'second')
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']},{'unmet'})
        row=result['completion_recording']['units'][0]
        self.assertEqual(row['affected_criteria'],[dict(work_unit_id='report-unit',contract_revision=1,criterion_id='report')])

    def test_unvalidated_publication_failure_keeps_running_work_parked(self):
        import autopilot_completion_service as service
        self.plan([self.item('first','report',status='running'),
            self.item('second','protected',status='running')])
        self.partial()
        with mock.patch.object(service,'_validate_v2_publication',side_effect=ValueError('untrusted publication')):
            result=self.stop()
        self.assertEqual(result['continuation']['status'],'parked')
        self.assertNotIn('affected_criteria',result['completion_recording']['units'][0])

    def test_scoped_writer_failure_does_not_dispatch_running_dependent(self):
        import autopilot_work_items
        self.plan([self.item('first','report',status='running'),
            self.item('second','protected',status='running',depends=['first'])])
        self.partial()
        with mock.patch.object(autopilot_work_items,'materialize_met_criteria_v2',side_effect=OSError('report recording defect')):
            result=self.stop()
        self.assertFalse(result['continuation'].get('request_emitted'))

    def test_native_dispatch_survives_independent_recording_park(self):
        import autopilot_mode
        payload=dict(continue_=True,systemMessage='Execute approved second',
            recovery=dict(status='parked'),continuation=dict(status='dispatched'))
        result=autopilot_mode._format_payload_for_hook(payload,{'hook_event_name':'Stop'})
        self.assertEqual(result.get('decision'),'block')

    def test_changed_applied_proof_reopens_task_and_prevents_false_completion(self):
        self.plan([self.item('first','report'),self.item('second','protected',depends=['first'])])
        artifact=self.root/'artifact';artifact.write_text('original')
        receipt=self.prepare();self.proof();self.publish(receipt,dependencies=[str(artifact)])
        self.assertEqual(self.stop()['continuation']['status'],'complete')
        artifact.write_text('changed')
        result=self.stop()
        self.assertNotEqual(result['continuation']['status'],'complete')

    def test_two_tasks_cannot_adopt_the_same_criterion_as_completion(self):
        with self.assertRaisesRegex(ValueError,'ambiguous|multiple'):
            self.plan([self.item('first','report'),self.item('second','report',depends=['first'])])

    def test_historical_explanation_does_not_reapply_completed_publication(self):
        import autopilot_mode
        import autopilot_work_items
        self.plan([self.item('first','report'),self.item('second','protected',depends=['first'])])
        self.partial();self.stop()
        receipt=self.prepare();self.proof()
        self.completion=self.completion.replace('  - report: Saved report matches input [source: user-explicit]\n','')
        first=self.completion.index('  - claim: Saved report')
        second=self.completion.index('  - claim: Protected file')
        self.completion=self.completion[:first]+self.completion[second:]
        published=self.publish(receipt)
        self.assertEqual(self.stop()['continuation']['status'],'complete')
        before=self.read_state()['acceptance_criteria']
        self.followup()
        self.feedback(purpose='explanation')
        with mock.patch.object(autopilot_work_items,'materialize_met_criteria_v2',side_effect=AssertionError('must not reapply')):
            for _ in range(2):
                result=self.stop()
                native=autopilot_mode._format_payload_for_hook(result,{'hook_event_name':'Stop'})
                self.assertEqual(native['systemMessage'],'')
                self.assertNotIn('decision',native)
                self.assertEqual(native['continuation']['status'],'complete')
        self.assertEqual(self.read_state()['acceptance_criteria'],before)
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            record=store.completion_read('evidence',published['evidence_references'][0])
            self.assertEqual(record['verified_at'],self.verified_at)

    def test_historical_partial_application_keeps_ready_second_stage(self):
        self.plan([self.item('first','report',status='running'),self.item('second','protected',depends=['first'])])
        self.feedback(required=[])
        self.partial();self.stop()
        # Stage two was already approved; the next input declares its necessity.
        self.followup()
        self.feedback(purpose='explanation')
        result=self.stop()
        self.assertEqual(result['continuation'].get('task_id'),'second')
        self.assertEqual(result['completion_recording']['units'][0]['status'],'applied')

    def test_historical_explanation_still_invalidates_changed_business_proof(self):
        self.plan([self.item('first','report'),self.item('second','protected',depends=['first'])])
        artifact=self.root/'artifact';artifact.write_text('original')
        receipt=self.prepare();self.proof();self.publish(receipt,dependencies=[str(artifact)])
        self.stop();self.followup()
        self.feedback(purpose='explanation')
        artifact.write_text('changed')
        result=self.stop()
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']},{'unmet'})
        self.assertNotEqual(result['continuation']['status'],'complete')
        self.assertEqual(result['completion_recording']['units'][0]['reason'],'dependency-mutated')

    def test_historical_explanation_cannot_apply_stale_pending_publication(self):
        self.plan([self.item('first','report',status='running'),self.item('second','protected',depends=['first'])])
        self.partial();self.followup()
        self.feedback(purpose='explanation')
        result=self.stop()
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']},{'unmet'})
        self.assertEqual(result['completion_recording']['units'][0]['reason'],'receipt-stale')
        self.assertFalse(result['continuation'].get('request_emitted'))

    def test_historical_explanation_retains_only_effectively_applied_subset(self):
        self.plan([self.item('first','report'),self.item('second','protected',status='blocked',depends=['first'])])
        artifact=self.root/'report';artifact.write_text('report')
        protected=self.root/'protected';protected.write_text('original')
        receipt=self.prepare();self.proof()
        relations=[dict(work_unit_id='report-unit',contract_revision=1,criterion_id=key,locators=[str(path)])
            for key,path in [('report',artifact),('protected',protected)]]
        self.publish(receipt,criterion_dependencies=relations)
        protected.write_text('changed before application')
        self.stop();self.followup()
        self.feedback(purpose='explanation')
        result=self.stop()
        self.assertEqual(result['completion_recording']['units'][0]['status'],'applied')
        self.assertEqual(result['systemMessage'],'')
        self.assertEqual({c['criterion_id'] for c in result['completion_recording']['units'][0]['selected_criteria']},{'report'})
        self.assertEqual({c['id']:c['status'] for c in self.read_state()['acceptance_criteria']},dict(report='met',protected='unmet'))

    def test_current_empty_feedback_holds_unmet_ready_work(self):
        self.plan([self.item('first','report')])
        self.feedback(required=[])
        result=self.stop()
        self.assertFalse(result['continuation']['request_emitted'])
        self.assertEqual(result['continuation']['reason'],'no-required-unprocessed')
        self.assertNotEqual(result['continuation']['status'],'complete')

    def test_absent_feedback_never_resends_the_cached_task(self):
        self.plan([self.item('first','report')])
        register_route(self.core,self.root/'intent',purpose='explanation')
        result=self.stop()
        self.assertFalse(result['continuation']['request_emitted'])
        self.assertEqual(result['continuation']['reason'],'continuation-feedback-unavailable')
        self.assertNotIn('Execute first',result['systemMessage'])

    def test_current_human_stop_suppresses_admitted_ready_work(self):
        self.plan([self.item('first','report')])
        self.feedback(control='human-stop')
        result=self.stop()
        self.assertFalse(result['continuation']['request_emitted'])
        self.assertEqual(result['continuation']['reason'],'human-stop')

    def test_current_missing_prerequisite_holds_ready_work(self):
        self.plan([self.item('first','report')])
        self.feedback(prerequisite='missing')
        result=self.stop()
        self.assertFalse(result['continuation']['request_emitted'])
        self.assertEqual(result['continuation']['reason'],'continuation-prerequisite-missing')

    def test_changed_current_information_emits_once_with_original_route_locator(self):
        self.plan([self.item('first','report')])
        self.assertTrue(self.stop()['continuation']['request_emitted'])
        self.feedback(required=[dict(criterion_refs=self.item('first','report')['criterion_refs'],
            reason='A distinct original is now required for this approved criterion.',
            next_information='Read the distinct current original.',source_refs=[],prerequisite='ready')])
        result=self.stop()
        self.assertTrue(result['continuation']['request_emitted'])
        self.assertIn('Read the distinct current original.',result['systemMessage'])
        self.assertNotIn('Execute first',result['systemMessage'])
        route=self.core.read_response_route(root=self.root/'intent',platform='codex',session_id='session-1')
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            request=store.continuation_plan()['delivery_requests']['first']
        frozen={k:v for k,v in route.items() if k!='current_input_event_id'}
        self.assertEqual(request['source_locator'],dict(input_event_id=route['input_event_id'],
            response_route_digest=self.core.json_digest(frozen),pointer='/continuation_feedback/required_unprocessed/0'))
        self.assertFalse(self.stop()['continuation']['request_emitted'])

    def test_empty_current_feedback_does_not_cancel_applied_proof_bookkeeping(self):
        self.plan([self.item('first','report',status='running'),self.item('second','protected',depends=['first'])])
        self.partial()
        self.feedback(required=[],purpose='explanation')
        result=self.stop()
        self.assertEqual(result['completion_recording']['units'][0]['status'],'applied')
        self.assertFalse(result['continuation']['request_emitted'])
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            self.assertEqual(store.continuation_plan()['items'][0]['status'],'completed')

    def candidate_local_originals(self):
        """Two authenticated legacy originals with the same candidate-local ID."""
        import completion_assessment
        root=(self.root/'intent').resolve()
        owner=dict(platform='codex',session_id='session-1')
        refs=[]
        with self.core.storage_transaction(root) as connection:
            completion_assessment._schema(connection)
            for text in ('first original passage','second original passage'):
                source=dict(source_id='candidate-local-original',locator='tool-result:original',
                    digest='sha256:'+hashlib.sha256(text.encode()).hexdigest(),text=text)
                candidate=dict(kind='legacy-source-fixture',intent_root=str(root),owner=owner,sources=[source])
                reference=completion_assessment._reference(candidate)
                connection.execute('INSERT INTO core_completion_candidates VALUES (?,?,?)',
                    (reference,reference,completion_assessment._json(candidate)))
                refs.append(dict(candidate_reference=reference,source_id=source['source_id']))
        return refs

    def test_candidate_scoped_source_aliases_dispatch_and_reorder_without_resend(self):
        self.plan([self.item('first','report')])
        refs=self.candidate_local_originals()
        row=dict(criterion_refs=self.item('first','report')['criterion_refs'],
            reason='Both original passages are needed for the current approved criterion.',
            next_information='Read the two selected original passages.',source_refs=refs,prerequisite='ready')
        self.feedback(required=[row])
        route=self.core.read_response_route(root=self.root/'intent',platform='codex',session_id='session-1')
        originals=route['continuation_feedback']['required_unprocessed'][0]['source_refs']
        self.assertEqual(len(originals),2)
        self.assertEqual(originals[0]['source_id'],originals[1]['source_id'])
        self.assertNotEqual(originals[0]['source']['digest'],originals[1]['source']['digest'])
        self.assertTrue(self.stop()['continuation']['request_emitted'])
        row['source_refs'].reverse()
        self.feedback(required=[row])
        self.assertFalse(self.stop()['continuation']['request_emitted'])

    def test_current_human_corrected_information_supersedes_legacy_unknown_request(self):
        import autopilot_continuation as continuation
        self.plan([self.item('first','report',status='running')])
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            plan=store.continuation_plan()
            task=plan['items'][0]
            fingerprint=continuation.contract.digest(dict(
                task={k:v for k,v in task.items() if k not in {'status','completion'}},
                criteria=[dict(ref=ref,status='unmet') for ref in task['criterion_refs']]))
            plan['dispatches']['first']=fingerprint
            plan['events'].append(dict(event='task-dispatched',task_id='first',fingerprint=fingerprint))
            historical=copy.deepcopy(plan['events'])
            store.set_continuation_plan(plan)
        self.core.record_turn(root=self.root/'intent',platform='codex',session_id='session-1',
            raw_user_input='Continue the approved first task using the corrected original passage.',
            expected_input_event_id=self.read_state()['latest_input_event_id'])
        self.feedback(required=[dict(criterion_refs=self.item('first','report')['criterion_refs'],
            reason='The latest human input corrects the needed original within the existing approval.',
            next_information='Read the corrected original passage.',source_refs=[],prerequisite='ready')])
        result=self.stop()
        self.assertTrue(result['continuation']['request_emitted'])
        self.assertIn('Read the corrected original passage.',result['systemMessage'])
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            plan=store.continuation_plan()
        self.assertEqual(plan['events'][:-1],historical)
        prior=plan['events'][-1]['superseded_request']
        self.assertEqual(prior['fingerprint'],fingerprint)
        self.assertEqual((prior['host_receipt'],prior['execution']),('unknown','unknown'))
        self.assertEqual(plan['items'][0]['status'],'running')
        self.assertFalse(self.stop()['continuation']['request_emitted'])

    def test_changed_feedback_original_holds_dispatch_and_keeps_independent_applied_proof(self):
        import completion_assessment
        self.plan([self.item('first','report',status='running'),self.item('second','protected',depends=['first'])])
        self.partial()
        original=self.root/'feedback-original.txt';original.write_text('current original passage')
        scope=completion_assessment.prepare_response_scope(self.source,
            expected_input_event_id=self.read_state()['latest_input_event_id'],source_paths=[str(original)])
        self.feedback(required=[dict(criterion_refs=self.item('second','protected')['criterion_refs'],
            reason='The second approved condition requires the selected original.',
            next_information='Read the selected current original.',prerequisite='ready',
            source_refs=[dict(candidate_reference=scope['scope_reference'],source_id=scope['sources'][0]['source_id'])])],
            purpose='explanation')
        original.write_text('changed original passage')
        route=self.core.read_response_route(root=self.root/'intent',platform='codex',session_id='session-1')
        self.assertEqual(route['status'],'ready')
        self.assertIn('continuation_feedback',route)
        self.assertEqual(route['continuation_feedback_issues'],[dict(
            pointer='/continuation_feedback/required_unprocessed/0',issue='source-changed')])
        result=self.stop()
        self.assertEqual(result['completion_recording']['units'][0]['status'],'applied')
        self.assertFalse(result['continuation']['request_emitted'])
        self.assertEqual(result['continuation']['reason'],'continuation-source-unavailable')
        self.assertEqual({c['id']:c['status'] for c in self.read_state()['acceptance_criteria']},
            dict(report='met',protected='unmet'))

    def test_changed_original_holds_only_its_group_and_keeps_independent_dispatch(self):
        import completion_assessment
        self.plan([self.item('first','report'),self.item('second','protected')])
        original=self.root/'feedback-first.txt';original.write_text('first current original')
        scope=completion_assessment.prepare_response_scope(self.source,
            expected_input_event_id=self.read_state()['latest_input_event_id'],source_paths=[str(original)])
        first=dict(criterion_refs=self.item('first','report')['criterion_refs'],
            reason='The first current condition needs the selected original.',next_information='Read the first selected original.',
            source_refs=[dict(candidate_reference=scope['scope_reference'],source_id=scope['sources'][0]['source_id'])],prerequisite='ready')
        second=dict(criterion_refs=self.item('second','protected')['criterion_refs'],
            reason='The second current condition needs independent authorized information.',
            next_information='Read the independent current information.',source_refs=[],prerequisite='ready')
        self.feedback(required=[first,second])
        stored_before=copy.deepcopy(self.read_state()['response_route'])
        original.write_text('changed first current original')
        route=self.core.read_response_route(root=self.root/'intent',platform='codex',session_id='session-1')
        self.assertEqual(route['continuation_feedback'],stored_before['continuation_feedback'])
        self.assertEqual(route['continuation_feedback_issues'],[dict(
            pointer='/continuation_feedback/required_unprocessed/0',issue='source-changed')])
        result=self.stop()
        self.assertTrue(result['continuation']['request_emitted'])
        self.assertEqual(result['continuation']['task_id'],'second')
        self.assertIn('Read the independent current information.',result['systemMessage'])
        self.assertNotIn('Read the first selected original.',result['systemMessage'])
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            plan=store.continuation_plan()
        self.assertEqual(plan['items'][0]['status'],'ready')
        self.assertEqual(plan['delivery_requests']['second']['source_locator'],dict(
            input_event_id=stored_before['input_event_id'],response_route_digest=self.core.json_digest(stored_before),
            pointer='/continuation_feedback/required_unprocessed/1'))
        self.assertEqual(self.read_state()['response_route'],stored_before)

    def test_changed_original_does_not_make_disjoint_combined_task_dispatchable(self):
        import completion_assessment
        combined=self.item('combined','report')
        combined['criterion_refs']+=self.item('combined','protected')['criterion_refs']
        combined['acceptance_criteria'].append('protected')
        self.plan([combined])
        originals=[self.root/'feedback-first.txt',self.root/'feedback-second.txt']
        for index,original in enumerate(originals):
            original.write_text('current original '+str(index))
        scope=completion_assessment.prepare_response_scope(self.source,
            expected_input_event_id=self.read_state()['latest_input_event_id'],
            source_paths=[str(original) for original in originals])
        rows=[dict(criterion_refs=self.item(identity,criterion)['criterion_refs'],
            reason='This current condition needs its selected original.',
            next_information='Read the selected original for '+identity+'.',
            source_refs=[dict(candidate_reference=scope['scope_reference'],source_id=source['source_id'])],
            prerequisite='ready') for identity,criterion,source in
            zip(('first','second'),('report','protected'),scope['sources'])]
        self.feedback(required=rows)
        stored_before=copy.deepcopy(self.read_state()['response_route'])
        healthy=self.stop()
        self.assertFalse(healthy['continuation']['request_emitted'])
        self.assertEqual(healthy['continuation']['reason'],'continuation-feedback-unmapped')
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            plan_before=store.continuation_plan()
        originals[0].write_text('changed first current original')
        route=self.core.read_response_route(root=self.root/'intent',platform='codex',session_id='session-1')
        self.assertEqual(route['status'],'ready')
        self.assertEqual(route['continuation_feedback'],stored_before['continuation_feedback'])
        self.assertEqual(route['continuation_feedback_issues'],[dict(
            pointer='/continuation_feedback/required_unprocessed/0',issue='source-changed')])
        result=self.stop()
        self.assertFalse(result['continuation']['request_emitted'],result['continuation'])
        self.assertEqual(result['continuation']['reason'],'continuation-feedback-unmapped')
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            self.assertEqual(store.continuation_plan(),plan_before)
        self.assertEqual(self.read_state()['response_route'],stored_before)
        self.assertEqual({c['id']:c['status'] for c in self.read_state()['acceptance_criteria']},
            dict(report='unmet',protected='unmet'))
