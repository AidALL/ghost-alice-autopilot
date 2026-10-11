"""A native v2 Stop advances admitted tasks, independently of response closure."""
import importlib
from unittest import mock
from test_completion_contract_v2 import _CompletionV2Fixture
from response_route_fixture import register_route


class CompletionContinuationTest(_CompletionV2Fixture):
    def item(self, identity, criterion, *, status='ready', depends=()):
        return dict(id=identity, status=status, depends_on=list(depends),
            acceptance_criteria=[criterion], allowed_surface=[str(self.root)],
            focus_layer='meso', prompt='Execute '+identity,
            criterion_refs=[dict(work_unit_id='report-unit',contract_revision=1,criterion_id=criterion)])

    def plan(self, items):
        try:
            module=importlib.import_module('autopilot_continuation')
        except ImportError:
            self.fail('v2 has no approved task scheduling bridge')
        return module.register_continuation_plan(self.source,
            expected_input_event_id=self.read_state()['latest_input_event_id'], items=items,
            approval_evidence=dict(decision='approved',source='user: continue these local stages'),
            allowed_surfaces=[str(self.root)])

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

    def partial(self):
        receipt=self.prepare(); self.proof()
        # One owning criterion is supported, leaving the second actual action open.
        self.completion=self.completion.replace('  - protected: Protected file is unchanged [source: user-explicit]\n','')
        self.completion=self.completion[:self.completion.index('  - claim: Protected file')]+ '- unverified: none\n'
        return self.publish(receipt)

    def test_partial_application_dispatches_dependency_ready_second_stage(self):
        self.plan([self.item('first','report',status='running'),self.item('second','protected',depends=['first'])])
        self.partial()
        result=self.stop()
        self.assertEqual(result['continuation']['status'],'dispatched')
        self.assertEqual(result['continuation']['task_id'],'second')
        self.assertIn('Execute second',result['systemMessage'])
        self.assertEqual([c['status'] for c in self.read_state()['acceptance_criteria']],['met','unmet'])

    def legacy_parent(self, *, dependent):
        from test_completion_corrections import CompletionCorrectionTest
        self.plan([self.item('first','report',status='running'),
            self.item('second','protected',depends=['first'] if dependent else [])])
        CompletionCorrectionTest.seed_legacy_wrong_mapping(self,applied=True,report_only=True)
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            plan=store.continuation_plan();plan['items'][0]['status']='completed';store.set_continuation_plan(plan)
        register_route(self.core,self.root/'intent',purpose='explanation')

    def test_unassessed_completed_parent_cannot_authorize_dependent_dispatch(self):
        self.legacy_parent(dependent=True)
        before=self.read_state()['acceptance_criteria']
        result=self.stop()
        self.assertNotEqual(result['continuation']['status'],'dispatched')
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
        register_route(self.core,self.root/'intent',purpose='explanation')
        result=self.stop()
        self.assertEqual(result['continuation']['task_id'],'independent')
        self.assertNotIn('Publish existing supported proof',result['systemMessage'])

    def test_waiting_dependency_and_stopped_tasks_do_not_dispatch(self):
        self.plan([self.item('waiting','report',status='blocked'),self.item('dependent','protected',depends=['waiting'])])
        result=self.stop()
        self.assertEqual(result['continuation']['status'],'waiting')
        self.assertNotIn('Execute dependent',result['systemMessage'])

    def test_unchanged_running_task_parks_and_new_input_does_not_reset_it(self):
        self.plan([self.item('first','report'),self.item('second','protected',depends=['first'])])
        self.assertEqual(self.stop()['continuation']['status'],'dispatched')
        self.assertEqual(self.stop()['continuation']['status'],'parked')
        self.followup()
        self.assertEqual(self.stop()['continuation']['status'],'parked')

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
        self.assertNotEqual(result['continuation']['status'],'dispatched')

    def test_partial_response_applies_accepted_proof_and_dispatches(self):
        self.plan([self.item('first','report',status='running'),self.item('second','protected',depends=['first'])])
        self.partial()
        register_route(self.core,self.root/'intent',purpose='explanation')
        result=self.stop()
        self.assertEqual(result['continuation']['task_id'],'second')
        self.assertEqual(result['completion_recording']['units'][0]['status'],'applied')

    def test_waiting_preserves_actual_recording_diagnostic(self):
        self.plan([self.item('waiting','report',status='blocked'),self.item('dependent','protected',depends=['waiting'])])
        self.prepare()
        result=self.stop()
        self.assertIn('completion-recording: prepared',result['systemMessage'])
        self.assertIn('No answer resubmission is requested',result['systemMessage'])

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
        register_route(self.core,self.root/'intent',purpose='explanation')
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
        self.assertNotEqual(result['continuation']['status'],'dispatched')

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
        register_route(self.core,self.root/'intent',purpose='explanation')
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
        self.plan([self.item('first','report',status='running'),self.item('second','protected',status='blocked',depends=['first'])])
        self.partial();self.stop()
        # An actual task approval transition, not the new input, makes stage two ready.
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            plan=store.continuation_plan();plan['items'][1]['status']='ready';store.set_continuation_plan(plan)
        self.followup()
        register_route(self.core,self.root/'intent',purpose='explanation')
        result=self.stop()
        self.assertEqual(result['continuation'].get('task_id'),'second')
        self.assertEqual(result['completion_recording']['units'][0]['status'],'applied')

    def test_historical_explanation_still_invalidates_changed_business_proof(self):
        self.plan([self.item('first','report'),self.item('second','protected',depends=['first'])])
        artifact=self.root/'artifact';artifact.write_text('original')
        receipt=self.prepare();self.proof();self.publish(receipt,dependencies=[str(artifact)])
        self.stop();self.followup()
        register_route(self.core,self.root/'intent',purpose='explanation')
        artifact.write_text('changed')
        result=self.stop()
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']},{'unmet'})
        self.assertNotEqual(result['continuation']['status'],'complete')
        self.assertEqual(result['completion_recording']['units'][0]['reason'],'dependency-mutated')

    def test_historical_explanation_cannot_apply_stale_pending_publication(self):
        self.plan([self.item('first','report',status='running'),self.item('second','protected',depends=['first'])])
        self.partial();self.followup()
        register_route(self.core,self.root/'intent',purpose='explanation')
        result=self.stop()
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']},{'unmet'})
        self.assertEqual(result['completion_recording']['units'][0]['reason'],'receipt-stale')
        self.assertNotEqual(result['continuation']['status'],'dispatched')

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
        register_route(self.core,self.root/'intent',purpose='explanation')
        result=self.stop()
        self.assertEqual(result['completion_recording']['units'][0]['status'],'applied')
        self.assertEqual(result['systemMessage'],'')
        self.assertEqual({c['criterion_id'] for c in result['completion_recording']['units'][0]['selected_criteria']},{'report'})
        self.assertEqual({c['id']:c['status'] for c in self.read_state()['acceptance_criteria']},dict(report='met',protected='unmet'))
