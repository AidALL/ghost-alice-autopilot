"""Task-specific actions run under current authority without a receipt gate.

These are synthetic Core/native fixtures, not human participation evidence.
"""
import importlib
import json
import os
from unittest import mock
from test_completion_contract_v2 import _CompletionV2Fixture
import test_completion_continuation as plan_fixture


class ContinuationExecutionTest(_CompletionV2Fixture):
    item = plan_fixture.CompletionContinuationTest.item
    plan = plan_fixture.CompletionContinuationTest.plan
    feedback = plan_fixture.CompletionContinuationTest.feedback

    def setUp(self):
        super().setUp()
        self.plan([self.item('first','report')])
        self.stop()
        self.calls = 0

    def request(self):
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            return store.continuation_plan()

    def invoke(self, action=None, **changes):
        try:
            module=importlib.import_module('autopilot_execution')
        except ImportError:
            self.fail('No task-specific action wrapper executes and records the approved action')
        plan=self.request()
        args=dict(expected_input_event_id=self.read_state()['latest_input_event_id'],
            plan_id=plan['plan_id'],task_id='first',request_id=plan['delivery_requests']['first']['request_id'],
            action_id='local-check',operation='Check the current result',resources=[str(self.root/'result.txt')],
            action=self.action if action is None else action)
        args.update(changes)
        return module.execute_continuation_action(self.source,**args)

    def action(self):
        self.calls += 1
        (self.root/'result.txt').write_text('actual callback result')
        return 'native returned value'

    def prepared_followup(self, *, depends=True):
        self.plan([self.item('first','report',status='running'),
            self.item('second','protected',depends=['first'] if depends else [])])
        artifact=self.root/'prerequisite-proof.txt'
        artifact.write_text('original prerequisite')
        plan_fixture.CompletionContinuationTest.partial(self,dependencies=[str(artifact)])
        self.stop()
        plan=self.request()
        import autopilot_execution
        def execute():
            return autopilot_execution.execute_continuation_action(self.source,
                expected_input_event_id=self.read_state()['latest_input_event_id'],
                plan_id=plan['plan_id'],task_id='second',
                request_id=plan['delivery_requests']['second']['request_id'],
                action_id='dependent-action',operation='Perform the approved followup',
                resources=[str(self.root/'result.txt')],action=self.action)
        return artifact,execute

    def test_changed_prerequisite_proof_prevents_action_before_another_stop(self):
        artifact,execute=self.prepared_followup()
        artifact.write_text('changed after the followup was queued')
        with self.assertRaisesRegex(ValueError,'prerequisite.*proof'):
            execute()
        self.assertEqual(self.calls,0)

    def test_missing_prerequisite_proof_prevents_action_before_another_stop(self):
        artifact,execute=self.prepared_followup()
        artifact.unlink()
        with self.assertRaisesRegex(ValueError,'prerequisite.*proof'):
            execute()
        self.assertEqual(self.calls,0)

    def test_unchanged_prerequisite_proof_allows_action(self):
        _,execute=self.prepared_followup()
        self.assertEqual(execute()['result'],'native returned value')
        self.assertEqual(self.calls,1)

    def test_changed_unrelated_proof_does_not_block_independent_action(self):
        artifact,execute=self.prepared_followup(depends=False)
        artifact.write_text('changed unrelated completed work')
        self.assertEqual(execute()['result'],'native returned value')
        self.assertEqual(self.calls,1)

    def test_unknown_receipt_does_not_block_actual_approved_action(self):
        self.assertEqual(self.request()['delivery_requests']['first']['host_receipt'],'unknown')
        result=self.invoke()
        self.assertEqual(self.calls,1)
        self.assertEqual(result['result'],'native returned value')
        plan=self.request();request=plan['delivery_requests']['first']
        self.assertEqual(request['host_receipt'],'unknown')
        self.assertEqual(request['execution'],'observed')
        self.assertEqual(plan['items'][0]['status'],'running')
        self.assertNotIn('native returned value',json.dumps(plan))
        self.assertEqual([c['status'] for c in self.read_state()['acceptance_criteria']],['unmet','unmet'])

    def test_repeating_same_action_does_not_replay_business_work(self):
        self.invoke();before=self.request()
        result=self.invoke()
        self.assertEqual(self.calls,1)
        self.assertEqual(result['status'],'already-recorded')
        self.assertEqual(before,self.request())

    def test_deleted_mutable_action_entry_does_not_replay_immutable_attempt(self):
        original=self.invoke()
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            plan=store.continuation_plan()
            plan['delivery_requests']['first']['actions'].clear()
            store.set_continuation_plan(plan)
        repeated=self.invoke()
        self.assertEqual(self.calls,1)
        self.assertEqual(repeated['status'],'already-recorded')
        self.assertEqual(repeated['execution'],original['execution'])
        with self.assertRaises(ValueError):self.invoke(operation='Changed action')

    def test_deleted_mutable_started_entry_does_not_retry_an_uncertain_attempt(self):
        import autopilot_execution
        with mock.patch.object(autopilot_execution,'_finish',side_effect=OSError('recording failed')):
            self.assertEqual(self.invoke()['status'],'recording-unavailable')
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            plan=store.continuation_plan()
            plan['delivery_requests']['first']['actions'].clear()
            store.set_continuation_plan(plan)
        with self.assertRaisesRegex(ValueError,'already attempted'):self.invoke()
        self.assertEqual(self.calls,1)

    def test_invalid_emission_is_rejected_before_action(self):
        with self.assertRaisesRegex(ValueError,'request'):
            self.invoke(request_id='foreign')
        self.assertEqual(self.calls,0)

    def forge_scheduler_surface(self, *, clear_request=False):
        import autopilot_continuation as continuation
        route=self.core.read_response_route(root=self.root/'intent',platform='codex',session_id='session-1')
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            plan=store.continuation_plan()
            task=plan['items'][0]
            task['allowed_surface']=[str(self.root.parent)]
            needed,_,_=continuation._feedback_tasks(route,plan['items'])
            fingerprint=continuation._request_fingerprint(task,plan['boundary_ref'],needed['first'][1])
            plan['dispatches']['first']=fingerprint
            plan['delivery_requests']['first']['fingerprint']=fingerprint
            if clear_request:
                plan['dispatches'].clear()
                plan['delivery_requests'].clear()
            store.set_continuation_plan(plan)
        return plan

    def test_mutable_surface_and_fingerprint_cannot_expand_action_approval(self):
        self.forge_scheduler_surface()
        with self.assertRaisesRegex(ValueError,'admitted.*plan'):
            self.invoke(resources=[str(self.root.parent/'unapproved-result.txt')])
        self.assertEqual(self.calls,0)

    def test_mutable_surface_cannot_dispatch_a_wider_task(self):
        before=self.forge_scheduler_surface(clear_request=True)
        result=self.stop()
        self.assertFalse(result.get('continuation',{}).get('request_emitted',False))
        self.assertEqual(self.request(),before)

    def test_missing_immutable_admission_cannot_authorize_action(self):
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            plan=store.continuation_plan()
            store.connection.execute('DELETE FROM ap_completion_v2_records WHERE run_key=? AND kind=? AND identity=?',
                (store.key,'continuation-plan',plan['plan_id']))
        with self.assertRaisesRegex(ValueError,'admitted.*plan'):
            self.invoke()
        self.assertEqual(self.calls,0)

    def test_mutable_ready_cannot_dispatch_an_immutably_stopped_or_blocked_task(self):
        for state in ('stopped','blocked'):
            with self.subTest(admitted_status=state):
                self.plan([self.item('first','report',status=state)])
                self.feedback()
                with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
                    plan=store.continuation_plan();plan['items'][0]['status']='ready'
                    store.set_continuation_plan(plan)
                self.assertFalse(self.stop()['continuation']['request_emitted'])
        self.assertEqual(self.calls,0)

    def test_mutable_ready_cannot_execute_an_immutably_stopped_or_blocked_task(self):
        import autopilot_continuation as continuation
        for state in ('stopped','blocked'):
            with self.subTest(admitted_status=state):
                self.plan([self.item('first','report',status=state)])
                self.feedback()
                route=self.core.read_response_route(root=self.root/'intent',platform='codex',session_id='session-1')
                with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
                    plan=store.continuation_plan();task=plan['items'][0];task['status']='ready'
                    needed,_,_=continuation._feedback_tasks(route,plan['items'])
                    fingerprint=continuation._request_fingerprint(task,plan['boundary_ref'],needed['first'][1])
                    plan['dispatches']['first']=fingerprint
                    plan['delivery_requests']['first']=dict(status='pending',fingerprint=fingerprint,
                        request_id='forged-reopen-'+state,host_receipt='unknown',execution='unknown')
                    store.set_continuation_plan(plan)
                with self.assertRaisesRegex(ValueError,'admitted.*plan'):self.invoke()
        self.assertEqual(self.calls,0)

    def test_mutable_dependency_removal_cannot_bypass_changed_prerequisite_proof(self):
        artifact,execute=self.prepared_followup()
        artifact.write_text('changed after admission')
        import autopilot_continuation as continuation
        route=self.core.read_response_route(root=self.root/'intent',platform='codex',session_id='session-1')
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            plan=store.continuation_plan()
            task=next(task for task in plan['items'] if task['id']=='second')
            task['depends_on']=[]
            needed,_,_=continuation._feedback_tasks(route,plan['items'])
            fingerprint=continuation._request_fingerprint(task,plan['boundary_ref'],needed['second'][1])
            plan['dispatches']['second']=fingerprint
            plan['delivery_requests']['second']['fingerprint']=fingerprint
            store.set_continuation_plan(plan)
        with self.assertRaisesRegex(ValueError,'admitted.*plan'):
            execute()
        self.assertEqual(self.calls,0)

    def admit_surfaces(self, surfaces):
        import autopilot_continuation
        task=self.item('first','report')
        task['allowed_surface']=surfaces
        autopilot_continuation.register_continuation_plan(self.source,
            expected_input_event_id=self.read_state()['latest_input_event_id'],items=[task],
            approval_evidence=dict(decision='approved',source='synthetic user: these directory locations'),
            allowed_surfaces=surfaces)
        self._continuation_items=[task]
        self.feedback()
        self.stop()

    def test_relative_approval_retains_its_admission_directory_after_cwd_change(self):
        approved,other=self.root/'approved',self.root/'other'
        approved.mkdir();other.mkdir()
        prior=os.getcwd()
        try:
            os.chdir(approved)
            self.admit_surfaces(['.'])
            os.chdir(other)
            with self.assertRaises(ValueError):
                self.invoke(resources=[str(other/'result.txt')])
            self.assertEqual(self.calls,0)
            self.invoke(resources=[str(approved/'result.txt')])
            self.assertEqual(self.calls,1)
        finally:
            os.chdir(prior)

    def test_approval_alias_retarget_does_not_move_the_approved_location(self):
        approved,other,alias=self.root/'approved',self.root/'other',self.root/'alias'
        approved.mkdir();other.mkdir()
        try:alias.symlink_to(approved,target_is_directory=True)
        except OSError as exc:self.skipTest('directory symlinks unavailable: '+str(exc))
        self.admit_surfaces([str(alias)])
        alias.unlink();alias.symlink_to(other,target_is_directory=True)
        with self.assertRaises(ValueError):
            self.invoke(resources=[str(alias/'result.txt')])
        self.assertEqual(self.calls,0)
        self.invoke(resources=[str(approved/'result.txt')])
        self.assertEqual(self.calls,1)

    def test_replacing_the_frozen_directory_with_a_link_does_not_expand_approval(self):
        approved,other=self.root/'approved',self.root/'other'
        approved.mkdir();other.mkdir()
        self.admit_surfaces([str(approved)])
        approved.rmdir()
        try:approved.symlink_to(other,target_is_directory=True)
        except OSError as exc:self.skipTest('directory symlinks unavailable: '+str(exc))
        with self.assertRaises(ValueError):
            self.invoke(resources=[str(approved/'result.txt')])
        self.assertEqual(self.calls,0)

    def test_explicit_filesystem_root_recursion_retains_the_root_separator(self):
        from pathlib import Path
        root=Path(self.root.anchor).as_posix().rstrip('/')+'/...'
        self.admit_surfaces([root])
        self.invoke(resources=[str(self.root/'result.txt')])
        self.assertEqual(self.calls,1)

    def test_file_surface_cannot_gain_descendants_when_replaced_by_a_directory(self):
        approved=self.root/'approved-file'
        approved.write_text('approved file')
        self.admit_surfaces([str(approved)])
        approved.unlink();approved.mkdir()
        with self.assertRaises(ValueError):
            self.invoke(resources=[str(approved/'unapproved-child')])
        self.assertEqual(self.calls,0)

    def test_missing_exact_surface_cannot_gain_descendants_when_created_as_a_directory(self):
        approved=self.root/'new-exact-path'
        self.admit_surfaces([str(approved)])
        approved.mkdir()
        with self.assertRaises(ValueError):
            self.invoke(resources=[str(approved/'unapproved-child')])
        self.assertEqual(self.calls,0)

    def test_changed_action_surface_does_not_hide_an_authenticated_historical_receipt(self):
        approved,other=self.root/'approved',self.root/'other'
        approved.mkdir();other.mkdir()
        self.admit_surfaces([str(approved)])
        approved.rmdir()
        try:approved.symlink_to(other,target_is_directory=True)
        except OSError as exc:self.skipTest('directory symlinks unavailable: '+str(exc))
        import autopilot_continuation as continuation
        current,root,target,_,_=continuation.service._v2_material(self.source)
        with continuation.service._v2_recording_transaction(root,current,target) as store:
            plan=store.continuation_plan()
            request_id=plan['delivery_requests']['first']['request_id']
            receipt=dict(fingerprint=plan['dispatches']['first'],request_id=request_id,fixture_authenticated=True)
            with mock.patch.object(continuation,'observe_codex_receipts',return_value={request_id:receipt}) as observer:
                observed=continuation._observe_plan_receipts(current,root,target,store,plan,
                    {'transcript_path':'authenticated observer fixture'})
            self.assertTrue(observed)
            observer.assert_called_once()
            self.assertEqual(plan['delivery_requests']['first']['host_receipt'],'observed')
        self.assertEqual(self.calls,0)

    def test_human_stop_prevents_action_even_with_prior_request(self):
        self.feedback(control='human-stop')
        with self.assertRaises(ValueError):self.invoke()
        self.assertEqual(self.calls,0)

    def test_current_missing_prerequisite_prevents_action(self):
        self.feedback(prerequisite='missing')
        with self.assertRaises(ValueError):self.invoke()
        self.assertEqual(self.calls,0)

    def test_corrected_current_information_rejects_old_request(self):
        self.feedback(required=[dict(criterion_refs=self.item('first','report')['criterion_refs'],
            reason='Changed requirement',next_information='Perform the corrected action',
            prerequisite='ready',source_refs=[])])
        with self.assertRaises(ValueError):self.invoke()
        self.assertEqual(self.calls,0)

    def test_outside_resource_is_rejected_before_action(self):
        with self.assertRaises(ValueError):self.invoke(resources=[str(self.root.parent/'outside.txt')])
        self.assertEqual(self.calls,0)

    def test_callback_failure_records_execution_without_business_completion(self):
        def failed():
            self.calls += 1
            raise RuntimeError('original callback failure')
        with self.assertRaisesRegex(RuntimeError,'original callback failure'):
            self.invoke(action=failed)
        request=self.request()['delivery_requests']['first']
        self.assertEqual(request['execution'],'observed')
        self.assertEqual(request['actions']['local-check']['outcome'],'raised')
        self.assertNotIn('original callback failure',json.dumps(request))
        with self.assertRaises(ValueError):self.invoke(action=failed)
        self.assertEqual(self.calls,1)

    def test_same_action_identity_cannot_change_operation_or_resources(self):
        self.invoke()
        with self.assertRaises(ValueError):self.invoke(operation='Different action')
        self.assertEqual(self.calls,1)

    def test_an_active_action_is_not_started_twice(self):
        def nested():
            self.calls += 1
            with self.assertRaises(ValueError):self.invoke()
        self.invoke(action=nested)
        self.assertEqual(self.calls,1)

    def test_changed_request_during_action_preserves_original_outcome_separately(self):
        def replaced():
            self.calls += 1
            self.feedback(required=[dict(criterion_refs=self.item('first','report')['criterion_refs'],
                reason='New current information arrived while the action ran.',
                next_information='Use the newly corrected input.',source_refs=[],prerequisite='ready')])
            self.stop()
        result=self.invoke(action=replaced)
        plan=self.request()
        self.assertEqual(plan['delivery_requests']['first']['execution'],'unknown')
        self.assertEqual(result['execution']['outcome'],'returned')

    def test_recording_failure_returns_real_action_value_without_replay(self):
        import autopilot_execution
        with mock.patch.object(autopilot_execution,'_finish',side_effect=OSError('recording failed')):
            result=self.invoke()
        self.assertEqual(self.calls,1)
        self.assertEqual(result['result'],'native returned value')
        self.assertEqual(result['status'],'recording-unavailable')
        with self.assertRaises(ValueError):self.invoke()
        self.assertEqual(self.calls,1)

    def test_caller_mutation_cannot_change_original_declared_resources(self):
        resources=[str(self.root/'result.txt')]
        def modified():resources.clear()
        self.invoke(action=modified,resources=resources)
        self.assertEqual(self.request()['delivery_requests']['first']['actions']['local-check']['resources'],[str(self.root/'result.txt')])

    def test_ambient_transaction_cannot_execute_before_durable_start(self):
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False):
            with self.assertRaisesRegex(ValueError,'transaction'):
                self.invoke()
        self.assertEqual(self.calls,0)

    def test_open_callback_transaction_cannot_claim_durable_finish(self):
        manager=self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False)
        def leaves_open():
            self.calls+=1
            manager.__enter__()
            return 'actual result'
        try:
            result=self.invoke(action=leaves_open)
            self.assertEqual(result['status'],'recording-unavailable')
            self.assertEqual(result['result'],'actual result')
        finally:
            manager.__exit__(None,None,None)
        self.assertEqual(self.request()['delivery_requests']['first']['actions']['local-check']['outcome'],'started')
        with self.assertRaises(ValueError):self.invoke()
        self.assertEqual(self.calls,1)

    def test_explicit_recursive_approval_allows_creation_below_new_directory(self):
        task=self.item('first','report')
        task['allowed_surface']=[str(self.root/'future')+'/...']
        import autopilot_continuation
        autopilot_continuation.register_continuation_plan(self.source,
            expected_input_event_id=self.read_state()['latest_input_event_id'],items=[task],
            approval_evidence=dict(decision='approved',source='synthetic user: create below future directory'),
            allowed_surfaces=[str(self.root)+'/...'])
        self._continuation_items=[task];self.feedback();self.stop()
        path=self.root/'future/nested/result.txt'
        def create():
            self.calls+=1
            path.parent.mkdir(parents=True)
            path.write_text('new result')
        self.invoke(action=create,resources=[str(path)])
        self.assertEqual(self.calls,1)
        self.assertEqual(path.read_text(),'new result')

    def native_receipt_for(self, plan, request, *, filename='delivery-native.jsonl'):
        text='\n'.join(['[autopilot]','continuation: approved-task','task-id: first',
            'plan-id: '+plan['plan_id'],'request-fingerprint: '+request['fingerprint'],
            'request-id: '+request['request_id']])
        path=self.root/filename
        rows=[dict(type='session_meta',payload=dict(id='session-1')),
            dict(type='event_msg',timestamp='2026-10-14T12:00:00Z',payload=dict(
                type='item_completed',thread_id='session-1',turn_id='delivered-turn',
                item=dict(type='HookPrompt',id='delivered-hook',fragments=[dict(text=text)])))]
        path.write_text('\n'.join(json.dumps(row) for row in rows)+'\n')
        import test_completion_publication as fixture
        return fixture.adapter.adapter_payload_from_env(self.source,hook_input=dict(
            hook_event_name='Stop',session_id='session-1',cwd=str(self.root),transcript_path=str(path)))

    def legacy_plan(self):
        """Retain authenticated pre-protocol admission and historical attempts."""
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            plan=store.continuation_plan();previous=plan['plan_id']
            definition=store.completion_read('continuation-plan',previous)
            definition.pop('delivery_protocol')
            plan['plan_id']=store.completion_put('continuation-plan',definition)
            plan.pop('delivery_protocol')
            plan['request_history']={}
            for request in plan['delivery_requests'].values():
                for key in ('emission_ref','cycle','sequence','source_locator'):
                    request.pop(key,None)
            for kind in ('continuation-action-started','continuation-action-finished'):
                for _,record in store.completion_records(kind):
                    if record['plan_id']==previous:
                        store.completion_put(kind,dict(record,plan_id=plan['plan_id']))
            store.set_continuation_plan(plan)
        return plan

    def test_legacy_native_receipt_keeps_first_original_across_repeated_locators(self):
        plan=self.legacy_plan();request=plan['delivery_requests']['first']
        self.native_receipt_for(plan,request)
        first=self.request()['delivery_requests']['first']
        self.native_receipt_for(plan,request,filename='second-native-locator.jsonl')
        after=self.request()
        self.assertEqual(after['delivery_requests']['first'],first)
        self.assertEqual(sum(event['event']=='continuation-host-received' for event in after['events']),1)
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            observations=[record for _,record in store.completion_records('continuation-host-receipt')
                if record['plan_id']==plan['plan_id']]
            self.assertEqual(len(observations),1)
            import autopilot_delivery
            alternate=dict(first['receipt'],locator='another-original-locator')
            self.assertEqual(autopilot_delivery.record_receipt(store,plan,
                dict(request,task_id='first'),alternate),first['receipt_ref'])
            self.assertEqual(len(store.completion_records('continuation-host-receipt')),1)

    def test_legacy_action_history_retains_receipt_without_new_execution_authority(self):
        self.invoke()
        plan=self.legacy_plan();request=plan['delivery_requests']['first']
        self.native_receipt_for(plan,request)
        after=self.request()['delivery_requests']['first']
        self.assertEqual(after['host_receipt'],'observed')
        self.assertEqual(after['execution'],'observed')
        self.assertEqual(after['actions']['local-check']['outcome'],'returned')
        self.assertNotIn('emission_ref',after)
        with self.assertRaisesRegex(ValueError,'legacy'):
            self.invoke(action_id='new-action')
        self.assertEqual(self.calls,1)

    def test_forged_request_identity_cannot_replay_an_immutable_action(self):
        self.invoke()
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            plan=store.continuation_plan();request=plan['delivery_requests']['first']
            request['request_id']='forged-new-emission';request['actions'].clear()
            store.set_continuation_plan(plan)
        with self.assertRaisesRegex(ValueError,'emission'):self.invoke(request_id='forged-new-emission')
        self.assertEqual(self.calls,1)

    def test_fabricated_projection_receipt_is_not_reported_as_authenticated_delivery(self):
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            plan=store.continuation_plan();request=plan['delivery_requests']['first']
            request.update(status='received',host_receipt='observed',receipt=dict(verified_at='forged',locator='forged'))
            store.set_continuation_plan(plan)
        self.stop()
        request=self.request()['delivery_requests']['first']
        self.assertEqual(request['host_receipt'],'unknown')
        self.assertNotIn('receipt',request)

    def test_deleted_projection_receipt_is_restored_from_immutable_observation(self):
        plan=self.request();self.native_receipt_for(plan,plan['delivery_requests']['first'])
        original=self.request()['delivery_requests']['first']['receipt']
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            plan=store.continuation_plan();request=plan['delivery_requests']['first']
            request.update(status='pending',host_receipt='unknown');request.pop('receipt',None)
            plan['events']=[event for event in plan['events'] if event['event']!='continuation-host-received']
            store.set_continuation_plan(plan)
        self.stop()
        request=self.request()['delivery_requests']['first']
        self.assertEqual(request['host_receipt'],'observed')
        self.assertEqual(request['receipt'],original)
        self.assertTrue(request['receipt_ref'])

    def test_superseded_unknown_emission_can_receive_its_late_native_receipt(self):
        plan=self.request();old=plan['delivery_requests']['first'].copy()
        self.feedback(required=[dict(criterion_refs=self.item('first','report')['criterion_refs'],
            reason='Corrected current information needs this step.',next_information='Use the corrected input.',
            source_refs=[],prerequisite='ready')])
        self.stop();current=self.request()['delivery_requests']['first']
        self.assertNotEqual(old['request_id'],current['request_id'])
        self.native_receipt_for(plan,old)
        refreshed=self.request()
        self.assertEqual(refreshed['delivery_requests']['first']['host_receipt'],'unknown')
        historical=refreshed.get('request_history',{}).get(old['request_id'],{})
        self.assertEqual(historical.get('host_receipt'),'observed')
        self.assertEqual(historical['receipt']['request_id'],old['request_id'])

    def test_lost_delivery_projection_does_not_reemit_or_replay_an_existing_attempt(self):
        self.invoke()
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            plan=store.continuation_plan();plan['delivery_requests'].clear();plan['dispatches'].clear();plan['events'].clear()
            store.set_continuation_plan(plan)
        self.assertFalse(self.stop()['continuation']['request_emitted'])
        self.assertEqual(self.invoke()['status'],'already-recorded')
        self.assertEqual(self.calls,1)
