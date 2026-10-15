"""Original evidence survives request rebinding without refreshing business proof."""
from __future__ import annotations
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest
import test_completion_publication as publication_fixture
from response_route_fixture import register_route


class _CompletionV2Fixture(unittest.TestCase):
    # Reuse the real Core/AP fixture without collecting its v1 test methods.
    proof = publication_fixture.CompletionPublicationTest.proof
    read_state = publication_fixture.CompletionPublicationTest.read_state
    change_intent = publication_fixture.CompletionPublicationTest.change_intent
    stop = publication_fixture.CompletionPublicationTest.stop

    def setUp(self):
        publication_fixture.CompletionPublicationTest.setUp(self)
        self.source['GHOST_ALICE_COMPLETION_SCHEMA'] = 'v2'
        skills = self.root / 'review-fixture-skills'
        skills.mkdir()
        (skills / 'autopilot-mode').symlink_to(publication_fixture.SKILL, target_is_directory=True)
        self.source['GHOST_ALICE_SKILLS_DIR'] = str(skills)
        self.review_candidates = {}
        self.review_records = {}

    def prepare(self):
        return self.helper.prepare_completion(**self.arguments, schema_version='v2')

    def review(self, proof=None, verified_at=None, verdicts=None, source=None, reuse_evidence=None,
               work_unit_id=None, reuse_candidate=None):
        """Explicit synthetic owning review for the fixture's observed conditions.

        These fixtures declare supported observations. Semantic negative cases
        supply unsupported/unavailable verdicts instead of changing production.
        """
        import completion_assessment
        source=self.source if source is None else source
        proof=self.completion if proof is None else proof
        verified_at=self.verified_at if verified_at is None else verified_at
        path=self.root/'synthetic-original-observations.txt'
        if not path.exists():
            path.write_text(
                'Synthetic fixture observations: the report matches input. '
                'The baseline protected check observed unchanged bytes. '
                'The separate contrary protected check observed original bytes differ. '
                'The pending check was unavailable. Each test declares which observation its claim reports; '
                'these are supplied fixture judgments, not a semantic evaluator result.\n')
        route=self.core.read_response_route(root=Path(source['GHOST_ALICE_SESSION_INTENT_ROOT']),platform=source['GHOST_ALICE_PLATFORM'],
            session_id=source['GHOST_ALICE_SESSION_ID'])
        for unit in ([work_unit_id] if work_unit_id else route['selected_work_unit_ids']):
            try:
                candidate=completion_assessment.prepare_response_review(source,response_text=proof,verified_at=verified_at,
                    source_paths=[] if reuse_evidence or reuse_candidate else [str(path)],work_unit_id=unit,
                    reuse_evidence=reuse_evidence,reuse_candidate=reuse_candidate)
                self.review_candidates[unit] = candidate['candidate_reference']
                self.review_records[unit] = candidate
                source_ids=[c['source_id'] for c in candidate['sources']]
                judgments=[dict(binding_id=b['binding_id'],verdict=(verdicts or {}).get(b['criterion']['id'],'supported'),
                    source_ids=source_ids,rationale='Explicit synthetic observation for this fixture condition')
                    for b in candidate['bindings']]
                completion_assessment.record_response_review(source,candidate['candidate_reference'],dict(
                    claims=judgments, response=dict(binding_id=candidate['response_binding']['binding_id'],
                        verdict='supported', source_ids=source_ids,
                        rationale='After the declared synthetic claim comparisons, the complete fixture response reports only the selected conditions and preserves its declared unresolved outcomes.')))
            except ValueError:
                # Invalid grammar/foreign criteria/time fixtures exercise the
                # publication rejection; never manufacture a valid assessment.
                pass

    def proof(self, verified_at=None):
        proof=publication_fixture.CompletionPublicationTest.proof(self,verified_at)
        self.review()
        return proof

    def publish(self, receipt, **changes):
        args = dict(receipt_token=receipt['receipt_token'], completion_check=self.completion,
            verified_at=self.verified_at, evidence_source=self.evidence_source, source=self.source,
            schema_version='v2')
        args.update(changes)
        if args.get("completion_check") is not None:
            self.review(args["completion_check"],args["verified_at"],getattr(self,"review_verdicts",None),
                work_unit_id=receipt.get("work_unit_id"))
        capture = {}
        if args.get('completion_check') is not None:
            import completion_assessment
            try:
                assessment = completion_assessment.lookup_current_proof(args['source'],
                    completion_assessment.extract_original_proof(args['completion_check']),
                    work_unit_id=receipt.get('work_unit_id'))
                original = assessment.candidate
                capture = completion_assessment.assessment_capture_binding(assessment,
                    original['contract'], original['proof'], original['verified_at']) or {}
            except ValueError:
                # Invalid review fixtures still exercise the real publication rejection.
                pass
        if (args.get('completion_check') is not None and not args.get('origin_receipt_token')
                and capture.get('receipt_token') not in (None, receipt['receipt_token'])
                and (args.get('dependencies') or args.get('criterion_dependencies') is not None)):
            # These synthetic tests supply the explicitly named artifact observations
            # with the original capture; later publication must not replace them.
            from autopilot_completion_service import _dependency_snapshots
            locators = args.get('dependencies') or sorted({locator
                for row in args.get('criterion_dependencies', []) for locator in row['locators']})
            args['origin_receipt_token'] = capture['receipt_token']
            args['original_dependency_snapshots'] = _dependency_snapshots(locators)
            args['dependencies'] = None
        return self.helper.publish_completion(**args)

    def review_explanation(self, text):
        """An explicit source-backed explanation judgment, without publication."""
        import completion_assessment
        path = self.root / 'synthetic-explanation-observation.txt'
        path.write_text(text)
        candidate = completion_assessment.prepare_response_review(self.source, response_text=text,
            source_paths=[str(path)], claim_statements=[text])
        source_ids = [row['source_id'] for row in candidate['sources']]
        completion_assessment.record_response_review(self.source, candidate['candidate_reference'], dict(
            claims=[dict(binding_id=b['binding_id'], source_ids=source_ids, verdict='supported',
                rationale='The synthetic observation supports this fixture explanation.') for b in candidate['bindings']],
            response=dict(binding_id=candidate['response_binding']['binding_id'], source_ids=source_ids,
                verdict='supported', rationale='The complete synthetic explanation preserves the stated scope.')))

    def followup(self):
        self.change_intent(new_input=True)
        self.arguments['input_event_id'] = self.read_state()['latest_input_event_id']


class PreparedPretoolV2Test(_CompletionV2Fixture):
    def test_pending_native_input_waits_without_capture_or_failure_context(self):
        from unittest import mock
        import autopilot_pretool as pretool
        helper=pretool._helper()
        pending={'intent_state':{'intake_status':'pending-native-origin'},
                 'latest_input_event':{}}
        with mock.patch.object(helper.adapter,'read_session_material',return_value=pending), \
                mock.patch.object(pretool,'capture_origin') as capture:
            result=pretool.capture_before_tool(self.source,
                dict(hook_event_name='PreToolUse',session_id='session-1',cwd=str(self.root)),preflight=True)
        capture.assert_not_called()
        self.assertEqual(result,{'status':'skipped','reason':'pending-native-origin'})
        self.assertFalse((self.run_dir/'authority.json').exists())

    def test_native_input_reader_errors_are_not_treated_as_waiting(self):
        from unittest import mock
        import autopilot_pretool as pretool
        with mock.patch.object(pretool._helper().adapter,'read_session_material',
                side_effect=ValueError('current session identity mismatch')):
            with self.assertRaisesRegex(ValueError,'identity mismatch'):
                pretool.capture_before_tool(self.source,
                    dict(hook_event_name='PreToolUse',session_id='session-1',cwd=str(self.root)),preflight=True)

    def reviewed_answer(self, verified_at=None):
        import completion_assessment
        skills = self.root / 'skills'
        skills.mkdir(exist_ok=True)
        if not (skills / 'autopilot-mode').exists():
            (skills / 'autopilot-mode').symlink_to(publication_fixture.SKILL, target_is_directory=True)
        self.source['GHOST_ALICE_SKILLS_DIR'] = str(skills)
        publication_fixture.CompletionPublicationTest.proof(self, verified_at)
        self.completion += '- evidence: tool-result:business-check-1\n'
        source = self.root / 'observed-result.txt'
        source.write_text('The report matches input and the protected file is unchanged.\n')
        candidate = completion_assessment.prepare_response_review(self.source,
            response_text=self.completion, source_paths=[str(source)], verified_at=self.verified_at)
        ids = [s['source_id'] for s in candidate['sources']]
        completion_assessment.record_response_review(self.source, candidate['candidate_reference'], {
            'claims': [dict(binding_id=b['binding_id'], verdict='supported', source_ids=ids,
                rationale='The retained synthetic observation supports this report condition.')
                for b in candidate['bindings']],
            'response': dict(binding_id=candidate['response_binding']['binding_id'], verdict='supported',
                source_ids=ids, rationale='The complete response contains only the supported report conditions.')})
        return candidate

    def test_review_after_followup_keeps_first_capture_and_current_authority(self):
        original = self.prepare()
        verified = datetime.now(timezone.utc).isoformat()
        self.followup()
        current = self.prepare()
        candidate = self.reviewed_answer(verified)
        import completion_assessment
        reviewed = completion_assessment.lookup_current_response(self.source, self.completion)
        capture = completion_assessment.assessment_capture_binding(reviewed,
            reviewed.candidate['contract'], reviewed.candidate['proof'], verified)
        self.assertEqual(capture['origin_token'], original['origin_token'])
        with self.assertRaises(ValueError):
            self.helper.publish_completion(receipt_token=original['receipt_token'],
                completion_check=self.completion, verified_at=verified, source=self.source, schema_version='v2')
        publication = self.helper.publish_completion(receipt_token=current['receipt_token'],
            completion_check=self.completion, verified_at=verified, source=self.source, schema_version='v2')
        self.assertEqual(publication['original_evidence'][0]['origin_token'], original['origin_token'])
        self.assertEqual(publication['verified_at'], verified)

    def test_native_stop_publishes_reviewed_response_without_model_publication(self):
        import autopilot_mode
        self.prepare()
        self.reviewed_answer()
        # Native delivery strips the draft file's terminal newline.
        host = dict(hook_event_name='Stop', session_id='session-1', last_assistant_message=self.completion.rstrip())
        result = publication_fixture.adapter.adapter_payload_from_env(self.source, hook_input=host)
        self.assertEqual(result['completion_recording']['units'][0]['status'], 'applied', result)
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']}, {'met'})
        self.assertNotIn('decision', autopilot_mode._format_payload_for_hook(result, host))

    def test_native_recording_failure_does_not_reprompt_business_completion(self):
        import autopilot_mode
        self.prepare()
        self.reviewed_answer((datetime.now(timezone.utc)-timedelta(days=1)).isoformat())
        host = dict(hook_event_name='Stop', session_id='session-1', last_assistant_message=self.completion)
        result = publication_fixture.adapter.adapter_payload_from_env(self.source, hook_input=host)
        self.assertEqual(result['completion_recording']['units'][0].get('reason'), 'time-invalid', result)
        self.assertNotIn('decision', autopilot_mode._format_payload_for_hook(result, host))
        again = publication_fixture.adapter.adapter_payload_from_env(self.source, hook_input=host)
        self.assertEqual(again['recovery']['status'], 'parked')
        self.assertNotIn('decision', autopilot_mode._format_payload_for_hook(again, host))
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']}, {'unmet'})

    def test_core_publication_repair_releases_parked_original_review(self):
        import autopilot_work_items
        from unittest import mock
        self.prepare()
        candidate = self.reviewed_answer()
        host = dict(hook_event_name='Stop', session_id='session-1',
            last_assistant_message=self.completion.rstrip())
        load_provider = autopilot_work_items._load_core_policy_module

        def old_publication_provider(*args, **kwargs):
            provider = load_provider(*args, **kwargs)
            if args[0] == 'completion_assessment.py':
                # The original native failure: publication did an exact draft
                # lookup after Core had accepted the stripped native response.
                provider.lookup_publication_assessment = provider.lookup_assessment
            return provider

        with mock.patch.object(autopilot_work_items, '_load_core_policy_module',
                side_effect=old_publication_provider):
            first = publication_fixture.adapter.adapter_payload_from_env(self.source, hook_input=host)
            self.assertEqual(first['completion_recording']['units'][0]['reason'], 'assessment-required', first)
            parked = publication_fixture.adapter.adapter_payload_from_env(self.source, hook_input=host)
            self.assertEqual(parked['recovery']['status'], 'parked', parked)

        repaired = publication_fixture.adapter.adapter_payload_from_env(self.source, hook_input=host)
        applied = repaired['completion_recording']['units'][0]
        self.assertEqual(applied['status'], 'applied', repaired)
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']}, {'met'})
        with self.helper.storage.transaction(self.run_dir, self.source, migrate_legacy=False) as store:
            publication = store.completion_read('publication',
                store.completion_unit(applied['work_unit_id'])['publication_reference'])
            self.assertEqual(publication['result']['verified_at'], candidate['verified_at'])
            self.assertEqual(publication['result']['original_evidence'][0]['proof_digest'],
                candidate['response_binding']['proof_digest'])
        again = publication_fixture.adapter.adapter_payload_from_env(self.source, hook_input=host)
        self.assertEqual(again['completion_recording']['units'][0]['application_reference'],
            applied['application_reference'])

    def test_native_automatic_publication_rejects_changed_response(self):
        self.prepare()
        self.reviewed_answer()
        host = dict(hook_event_name='Stop', session_id='session-1',
            last_assistant_message=self.completion+'\nUnreviewed business conclusion.\n')
        result = publication_fixture.adapter.adapter_payload_from_env(self.source, hook_input=host)
        self.assertEqual(result['completion_recording']['reason'], 'completion-body-invalid')
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']}, {'unmet'})

    def test_preparation_consumes_one_locked_selection_and_rechecks_each_call(self):
        import autopilot_completion_service as service
        from unittest import mock
        original=service._v2_material
        calls=[]
        def observed(*args,**kwargs):
            result=original(*args,**kwargs)
            calls.append((kwargs.get('transaction'),result))
            return result
        with mock.patch.object(service,'_v2_material',side_effect=observed):
            first=self.prepare()
        self.assertEqual(len([row for row in calls if row[0] is not None]),1)
        self.assertEqual(len([row for row in calls if row[0] is None]),1)
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            receipt=store.completion_read('receipt',first['receipt_token'])
            self.assertEqual(receipt['contract']['work_unit_id'],calls[-1][1][3]['selected_work_units'][0]['work_unit_id'])
        calls.clear()
        with mock.patch.object(service,'_v2_material',side_effect=observed):
            again=self.prepare()
        self.assertEqual(again['receipt_token'],first['receipt_token'])
        self.assertEqual(len([row for row in calls if row[0] is not None]),1)

    def test_prepared_notice_describes_native_publication_without_manual_command(self):
        from autopilot_pretool import capture_before_tool
        host=dict(hook_event_name='PreToolUse',session_id='session-1',cwd=str(self.root))
        first=capture_before_tool(self.source,host,preflight=True)
        notice=first['additional_context']
        self.assertIn('completion-preflight: prepared',notice)
        self.assertNotIn(first['prepare_command'],notice)
        self.assertNotIn(first['verification_time_command'],notice)
        self.assertNotIn(first['publish_command'],notice)
        self.assertIn('native Stop consumes the Core-reviewed response',notice)
        again=capture_before_tool(self.source,host,preflight=True)
        self.assertEqual(again['receipt_token'],first['receipt_token'])
        self.assertNotIn('additional_context',again)
        self.followup()
        later=capture_before_tool(self.source,host,preflight=True)
        self.assertNotEqual(later['receipt_token'],first['receipt_token'])
        self.assertNotIn('additional_context',later)
        self.assertEqual(later['input_event_id'],self.read_state()['latest_input_event_id'])

    def test_changed_criteria_reannounce_without_reusing_input_authority(self):
        from autopilot_pretool import capture_before_tool
        host=dict(hook_event_name='PreToolUse',session_id='session-1',cwd=str(self.root))
        first=capture_before_tool(self.source,host,preflight=True)
        criteria=copy.deepcopy(self.read_state()['acceptance_criteria'])
        criteria[0]['summary']+=' and its updated requirement'
        self.change_intent({'acceptance_criteria':criteria},new_input=True)
        later=capture_before_tool(self.source,host,preflight=True)
        self.assertNotEqual(later['receipt_token'],first['receipt_token'])
        self.assertGreater(later['contract_revision'],first['contract_revision'])
        self.assertIn('additional_context',later)
        again=capture_before_tool(self.source,host,preflight=True)
        self.assertNotIn('additional_context',again)

    def test_locked_selection_cannot_write_through_another_store_target(self):
        from contextlib import contextmanager
        from unittest import mock
        import autopilot_completion_service as service
        original=service._v2_recording_transaction
        @contextmanager
        def wrong_store(*args,**kwargs):
            with original(*args,**kwargs) as store:
                store.run_dir=self.root/'foreign-target'
                yield store
        with mock.patch.object(service,'_v2_recording_transaction',wrong_store):
            with self.assertRaisesRegex(service.CompletionRecordingError,'store'):
                self.prepare()

    def test_changed_input_before_lock_is_still_rejected_without_capture(self):
        from contextlib import contextmanager
        from unittest import mock
        import autopilot_completion_service as service
        original=service._v2_recording_transaction
        @contextmanager
        def changed_before_lock(*args,**kwargs):
            self.followup()
            with original(*args,**kwargs) as store:
                yield store
        with mock.patch.object(service,'_v2_recording_transaction',changed_before_lock):
            with self.assertRaises(ValueError):self.prepare()
        self.assertFalse((self.run_dir/'authority.json').exists())

    def test_concurrent_pretools_keep_one_original_receipt_notice(self):
        from concurrent.futures import ThreadPoolExecutor
        from autopilot_pretool import capture_before_tool
        host=dict(hook_event_name='PreToolUse',session_id='session-1',cwd=str(self.root))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:capture_before_tool(self.source,host,preflight=True),range(2)))
        self.assertEqual(len({row['receipt_token'] for row in results}),1)
        self.assertEqual(len([row for row in results if row.get('additional_context')]),1)


class InactiveCompletionV2Test(_CompletionV2Fixture):
    def test_off_targets_skip_recording_consumers_without_session_authority(self):
        from autopilot_provenance import capture_origin
        import autopilot_completion_service as service
        absent_authority = self.root / 'absent-intent'
        skipped = dict(status='skipped', reason='autopilot-off')
        noop = {'continue':True, 'systemMessage':''}
        for platform in ('codex', 'claude'):
            session_run = self.root / '.autopilot/sessions' / platform / 'session-1'
            for marker in (self.root / '.autopilot/OFF', session_run / 'OFF'):
                with self.subTest(platform=platform, marker=str(marker)):
                    marker.parent.mkdir(parents=True, exist_ok=True)
                    marker.write_text('paused\n')
                    source = dict(self.source, GHOST_ALICE_PLATFORM=platform,
                        GHOST_ALICE_SESSION_INTENT_ROOT=str(absent_authority))
                    if marker.parent == session_run:
                        source['GHOST_ALICE_AUTOPILOT_RUN_DIR'] = str(session_run)
                    args = dict(self.arguments, platform=platform, source=source,
                        intent_root=absent_authority)
                    self.assertEqual(service.apply_completion_v2(source), noop)
                    self.assertEqual(capture_origin(source, preflight=True), skipped)
                    self.assertEqual(self.helper.prepare_completion(**args, schema_version='v2'), skipped)
                    self.assertEqual(self.helper.publish_completion(receipt_token='not-read',
                        completion_check=None, verified_at=None, source=source,
                        schema_version='v2'), skipped)
                    self.assertEqual(service.v2_preflight_result({'status':'prepared'}, source), skipped)
                    self.assertEqual(service.recording_failure_payload(source, None,
                        ValueError('must not become an inactive recording diagnostic')), noop)
                    self.assertFalse(absent_authority.exists())
                    self.assertFalse((session_run / 'authority.json').exists())
                    self.assertFalse((marker.parent / '.advance.lock').exists())
                    marker.unlink()

    def test_pause_preserves_pending_proof_and_resumes_without_republication(self):
        receipt = self.prepare()
        proof = self.proof()
        published = self.publish(receipt)
        state = self.read_state()
        def recording_snapshot():
            with self.helper.storage.transaction(self.run_dir, self.source, migrate_legacy=False) as store:
                unit = store.completion_unit('report-unit')
                return copy.deepcopy((unit, store.completion_read('publication', unit['publication_reference']),
                    store.completion_read('evidence', published['evidence_references'][0]),
                    store.completion_recovery_head('report-unit')))
        original = recording_snapshot()
        marker = self.run_dir / 'OFF'
        marker.write_text('paused\n')
        self.assertEqual(self.stop(), {'continue':True, 'systemMessage':''})
        self.assertEqual(self.prepare(), dict(status='skipped', reason='autopilot-off'))
        self.assertEqual(self.publish(receipt), dict(status='skipped', reason='autopilot-off'))
        self.assertEqual(self.read_state(), state)
        self.assertEqual(recording_snapshot(), original)
        marker.unlink()
        applied = self.stop()
        self.assertEqual(applied['completion_recording']['units'][0]['unit_status'], 'complete')
        self.assertEqual(proof, self.completion)
        self.assertEqual(recording_snapshot()[2], original[2])


class ResponsePurposeV2Test(_CompletionV2Fixture):
    def test_sdk_pending_input_is_observed_without_completion_writes(self):
        from unittest.mock import patch
        import autopilot_completion_service as service
        # Exact prompt and native origin from the observed Claude SDK replay;
        # transport reception is retained without inventing human admission.
        prompt = '이메일인가애 있었던 거 같음\n'
        session = 'ff7dead7-10a5-4fef-bd7a-2cffdcf815d0'
        prompt_id = '6fc7d895-0b7c-470e-a86e-61f3f958dcb9'
        root = self.root / 'pending-intent'
        transcript = self.root / 'pending-native.jsonl'
        coords = dict(root=root, platform='claude', session_id=session)
        self.core.stage_native_input(**coords, raw_input=prompt, prompt_id=prompt_id,
            transcript_path=str(transcript))
        transcript.write_text(json.dumps(dict(type='user', sessionId=session,
            promptId=prompt_id, promptSource='sdk', turnOrigin='sdk', entrypoint='sdk-cli',
            message=dict(role='user', content=prompt))) + '\n')
        before = self.core.read_session_state(**coords)
        source = dict(self.source, GHOST_ALICE_PLATFORM='claude', GHOST_ALICE_SESSION_ID=session,
            GHOST_ALICE_SESSION_INTENT_ROOT=str(root))
        host = dict(hook_event_name='Stop', session_id=session, transcript_path=str(transcript))
        with patch.object(service, '_v2_recording_transaction') as recording:
            result = publication_fixture.adapter.adapter_payload_from_env(source, hook_input=host)
            recording.assert_not_called()
        self.assertEqual(result.get('systemMessage'), '')
        self.assertEqual(result['completion_recording']['status'], 'recording-unavailable')
        self.assertEqual(result['completion_recording']['reason'], 'input-not-admitted')
        self.assertEqual(result['completion_recording']['intake_status'], 'pending-native-origin')
        self.assertNotIn('decision', result)
        self.assertEqual(self.core.read_session_state(**coords), before)
        self.assertFalse((self.root / '.autopilot').exists())
        # Explicit preflight/publication still cannot use an absent anchor.
        with self.assertRaises(ValueError):
            service.v2_preflight_result({'status': 'prepared'}, source)
        import autopilot_intent_recovery as recovery
        material = publication_fixture.adapter.read_session_material(root, 'claude', session,
            source=source, require_input=False)
        original = recovery._core_native_assessor(self.core)(source, phase='before_tool')
        # Only the two absent sentinels normalize; invalid scalar identities
        # and unavailable authority never become an unadmitted-input result.
        for value in (False, 0, 'foreign-input'):
            with self.subTest(input_event_id=value), patch.object(recovery, '_core_native_assessor',
                    return_value=lambda *args, **kwargs: dict(original, input_event_id=value)):
                selection = recovery.current_execution_projection(material, source, self.run_dir)
                self.assertEqual(selection['reason'], 'core-assessment-incompatible')
        with patch.object(recovery, '_core_native_assessor', return_value=lambda *args, **kwargs:
                dict(original, context_status='unavailable', context_issue='unreadable authority')):
            selection = recovery.current_execution_projection(material, source, self.run_dir)
            self.assertEqual(selection['reason'], 'core-authority-unavailable')
        valid_material = publication_fixture.adapter.read_session_material(self.root / 'intent',
            'codex', 'session-1', source=self.source, require_input=True)
        valid = recovery._core_native_assessor(self.core)(self.source, phase='before_tool')
        with patch.object(recovery, '_core_native_assessor', return_value=lambda *args, **kwargs:
                dict(valid, input_event_id='different-nonempty-input')):
            selection = recovery.current_execution_projection(valid_material, self.source, self.run_dir)
            self.assertEqual(selection['reason'], 'core-assessment-incompatible')

    def native_stop(self,text,alias='last_assistant_message'):
        import os,subprocess
        host=dict(hook_event_name='Stop',session_id='session-1',cwd=str(self.root),**{alias:text})
        result=subprocess.run([sys.executable,'-B',str(publication_fixture.SKILL/'adapters/autopilot_mode.py')],
            input=json.dumps(host),cwd=self.root,env={**os.environ,**self.source,'CODEX_THREAD_ID':'session-1'},
            capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)

    def test_actual_nonclosing_text_does_not_acquire_or_prompt_publication(self):
        before=self.read_state()['acceptance_criteria']
        for alias,text in [('last_assistant_message','The process reads the selected work before preparing evidence.'),
                ('lastAssistantMessage','May I proceed with the proposed change?'),
                ('prompt_response','The remaining step needs your choice of destination.'),
                ('promptResponse','I am waiting for the requested input.')]:
            with self.subTest(alias=alias):
                register_route(self.core,self.root/'intent',purpose='explanation')
                self.review_explanation(text)
                stopped=self.native_stop(text,alias)
                self.assertEqual(stopped['systemMessage'],'')
                self.assertEqual(stopped['completion_recording'],dict(status='v2-unit-state',units=[]))
                self.assertFalse(self.run_dir.exists())
                self.assertEqual(self.read_state()['acceptance_criteria'],before)

    def test_nonclosing_stop_applies_original_pending_publication_without_new_proof(self):
        receipt=self.prepare();self.proof();published=self.publish(receipt)
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            proof=store.completion_read('evidence',published['evidence_references'][0])
        register_route(self.core,self.root/'intent',purpose='approval-wait')
        stopped=self.native_stop('May I proceed with the proposed change?')
        self.assertEqual(stopped['systemMessage'],'')
        applied=stopped['completion_recording']['units'][0]
        self.assertEqual(applied['unit_status'],'complete')
        self.assertEqual(applied['original_evidence'],published['original_evidence'])
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']},{'met'})
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            self.assertEqual(store.completion_read('evidence',published['evidence_references'][0]),proof)

    def test_default_execution_does_not_infer_publication_from_saved_report_text(self):
        stopped=self.native_stop('Saved requested report')
        self.assertEqual(stopped['systemMessage'],'')
        self.assertEqual(stopped['completion_recording'],dict(status='v2-unit-state',units=[]))
        self.assertFalse(self.run_dir.exists())
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']},{'unmet'})

    def test_explanatory_route_reviewed_completion_is_published_by_native_stop(self):
        self.prepare();self.proof()
        self.completion+='- evidence: '+self.evidence_source+'\n'
        self.review()
        original_candidate=self.review_candidates['report-unit']
        register_route(self.core,self.root/'intent',purpose='explanation')
        body=self.completion+'\nThe report has been completed and verified.'
        rejected=self.native_stop(body)
        self.assertEqual(rejected['completion_recording']['reason'],'completion-body-invalid')
        self.assertNotIn(' publish ',rejected['systemMessage'])
        self.review(body,reuse_candidate=original_candidate)
        applied=self.native_stop(body)
        self.assertEqual(applied['completion_recording']['units'][0]['unit_status'],'complete')
        original=applied['completion_recording']['units'][0]['original_evidence'][0]
        self.assertEqual(original['verified_at'],self.verified_at)
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']},{'met'})

    def test_explicit_reference_publication_preserves_proof_on_explanatory_route(self):
        receipt=self.prepare();self.proof();published=self.publish(receipt)
        register_route(self.core,self.root/'intent',purpose='explanation')
        body=self.completion+'\nThe report has been completed and verified.'
        self.review(body,reuse_evidence=published['evidence_references'][0])
        reused=self.publish(receipt,completion_check=None,verified_at=None,
            evidence_references=published['evidence_references'])
        self.assertEqual(reused,published)
        applied=self.native_stop(body)
        self.assertEqual(applied['completion_recording']['units'][0]['unit_status'],'complete')

    def test_explicit_nonclosing_purpose_skips_pretool_without_dropping_selection(self):
        from autopilot_pretool import capture_before_tool
        from autopilot_intent_recovery import current_execution_projection
        for purpose in ('explanation','approval-wait','partial'):
            with self.subTest(purpose=purpose):
                register_route(self.core,self.root/'intent',purpose=purpose)
                material=publication_fixture.adapter.read_session_material(self.root/'intent','codex','session-1',source=self.source)
                projected=current_execution_projection(material,self.source,self.run_dir)
                self.assertEqual(projected['status'],'skipped')
                self.assertEqual(projected['reason'],'response-no-publication')
                self.assertEqual(projected['completion_route']['selected_work_unit_ids'],['report-unit'])
                self.assertEqual(len(projected['selected_criteria']),2)
                captured=capture_before_tool(self.source,dict(hook_event_name='PreToolUse',session_id='session-1',cwd=str(self.root)),preflight=True)
                self.assertEqual(captured['reason'],'response-no-publication')
                self.assertNotIn('additional_context',captured)
                self.assertFalse(self.run_dir.exists())

    def test_generated_pretool_guidance_requires_publication_only_for_core_selected_closure(self):
        from autopilot_pretool import capture_before_tool
        captured=capture_before_tool(self.source,dict(hook_event_name='PreToolUse',
            session_id='session-1',cwd=str(self.root)),preflight=True)
        guidance=captured['additional_context']
        self.assertIn('publication: native Stop consumes the Core-reviewed response',guidance)
        self.assertIn('For completion or partial-completion claims',guidance)
        self.assertIn('do not repeat them for routine explanations',guidance)
        self.assertNotIn(captured['publish_command'],guidance)
        self.assertNotIn('before the first final response',guidance)

    def test_unreviewed_explanation_is_blocked_without_inferred_completion(self):
        register_route(self.core,self.root/'intent',purpose='explanation')
        stopped=self.native_stop('The report has been completed and verified.')
        self.assertEqual(stopped['completion_recording']['reason'],'completion-body-invalid')
        self.assertFalse(stopped['completion_recording']['completion_route']['required_completion_check'])
        self.assertIn('response-assessment-required',stopped['completion_recording']['detail'])
        self.assertFalse(self.run_dir.exists())
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']},{'unmet'})

    def test_actual_closure_uses_retained_publication_despite_explanation_purpose(self):
        receipt=self.prepare();self.proof();published=self.publish(receipt)
        register_route(self.core,self.root/'intent',purpose='explanation')
        body=self.completion+'\nThe report has been completed and verified.'
        self.review(body,reuse_evidence=published['evidence_references'][0])
        stopped=self.native_stop(body)
        self.assertEqual(stopped['systemMessage'],'')
        self.assertEqual(stopped['completion_recording']['units'][0]['unit_status'],'complete')
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']},{'met'})

    def test_actual_closure_cannot_promote_tampered_proof_with_explanation_purpose(self):
        receipt=self.prepare();self.proof();published=self.publish(receipt)
        body=self.completion+'\nThe report has been completed and verified.'
        self.review(body,reuse_evidence=published['evidence_references'][0])
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            ref=published['evidence_references'][0];record=store.completion_read('evidence',ref)
            record['verified_at']=datetime.now(timezone.utc).isoformat()
            store.connection.execute('UPDATE ap_completion_v2_records SET body_json=? WHERE run_key=? AND kind=? AND identity=?',
                (json.dumps(record),store.key,'evidence',ref))
        register_route(self.core,self.root/'intent',purpose='explanation')
        stopped=self.native_stop(body)
        self.assertEqual(stopped['completion_recording']['units'][0]['status'],'recording-unavailable')
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']},{'unmet'})


class CompletionContractV2Test(_CompletionV2Fixture):
    def test_application_preserves_ownerless_history_with_the_same_local_criterion_id(self):
        skills = self.root/'skills'
        skills.mkdir()
        (skills/'autopilot-mode').symlink_to(publication_fixture.SKILL, target_is_directory=True)
        self.source['GHOST_ALICE_SKILLS_DIR'] = str(skills)
        legacy = dict(id='report', summary='Historical report', source='user-explicit',
            admitted=True, status='unmet')
        self.core.record_turn(root=self.root/'intent', platform='codex', session_id='session-1',
            expected_input_event_id=self.read_state()['latest_input_event_id'],
            intent_delta={'acceptance_criteria': [legacy]})
        register_route(self.core, self.root/'intent')
        self.core.mark_acceptance_criterion_met(root=self.root/'intent', platform='codex',
            session_id='session-1', criterion_id='report', completion_check_digest='a' * 64,
            expected_input_event_id=self.read_state()['latest_input_event_id'],
            expected_criterion=legacy)
        before = [c for c in self.read_state()['acceptance_criteria'] if 'work_unit_id' not in c]
        self.assertEqual(len(before), 1)
        self.assertEqual(before[0]['status'], 'met')
        receipt = self.prepare()
        self.proof()
        import completion_assessment
        original = completion_assessment.prepare_assessment(self.source, proof=self.completion,
            verified_at=self.verified_at, source_paths=[str(self.root/'synthetic-original-observations.txt')])
        body = self.completion + '\nThe report matches input; protected bytes remain unchanged.'
        draft = completion_assessment.prepare_response_review(self.source, response_text=body,
            source_paths=[], reuse_candidate=original['candidate_reference'])
        claim_reviews = [dict(binding_id=b['binding_id'], verdict='supported',
            source_ids=[s['source_id'] for s in draft['sources']],
            rationale='The original report observation supports the qualified criterion.') for b in draft['bindings']]
        reviewed = completion_assessment.record_response_review(self.source, draft['candidate_reference'],
            dict(claims=claim_reviews, response=dict(binding_id=draft['response_binding']['binding_id'],
                verdict='supported', source_ids=[s['source_id'] for s in draft['sources']],
                rationale='The complete answer retains only the report and protected-file observations.')))
        published = self.helper.publish_completion(receipt_token=receipt['receipt_token'],
            completion_check=body, verified_at=self.verified_at,
            evidence_source=self.evidence_source, source=self.source, schema_version='v2')
        # A later recording-only instruction needs Core's context review, not new business proof.
        self.change_intent({'constraints': ['Do not rerun the report; apply its retained proof.']}, new_input=True)
        self.arguments['input_event_id'] = self.read_state()['latest_input_event_id']
        current = self.prepare()
        reference = published['evidence_references'][0]
        reuse = dict(receipt_token=current['receipt_token'], completion_check=None, verified_at=None,
            evidence_references=[reference], source=self.source, schema_version='v2')
        with self.assertRaises(ValueError):
            self.helper.publish_completion(**reuse)
        import completion_assessment
        import os, subprocess
        proof_path = self.root/'retained-proof.md'
        proof_path.write_text(self.completion)
        prepared = subprocess.run([sys.executable, '-B', str(Path(self.source['GHOST_ALICE_CORE_ROOT'])/
            '_shared/completion_assessment.py'), 'prepare-response', '--response-file', str(proof_path),
            '--verified-at', self.verified_at, '--work-unit-id', 'report-unit', '--reuse-evidence', reference],
            env={**os.environ, **self.source, 'CODEX_THREAD_ID': 'session-1'}, capture_output=True, text=True)
        self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
        candidate = json.loads(prepared.stdout)
        judgments = [dict(binding_id=b['binding_id'], verdict='supported',
            source_ids=[s['source_id'] for s in candidate['sources']],
            rationale='The synthetic report observations remain sufficient; the new constraint forbids business replay.')
            for b in candidate['bindings']]
        response = dict(binding_id=candidate['response_binding']['binding_id'], verdict='supported',
            source_ids=[s['source_id'] for s in candidate['sources']],
            rationale='The complete retained response is the same original proof, without added claims.')
        completion_assessment.record_response_review(self.source, candidate['candidate_reference'],
            dict(claims=judgments, response=response))
        rebound = self.helper.publish_completion(**reuse)
        self.assertEqual(rebound['evidence_references'], [reference])
        applied = self.stop()['completion_recording']['units'][0]
        self.assertEqual(applied['status'], 'applied', applied)
        self.assertEqual(applied['unit_status'], 'complete', applied)
        self.assertEqual(applied['original_evidence'][0]['verified_at'], self.verified_at)
        after = self.read_state()['acceptance_criteria']
        self.assertEqual([c for c in after if 'work_unit_id' not in c], before)
        self.assertTrue(all(c['status'] == 'met' for c in after if c.get('work_unit_id') == 'report-unit'))

    def test_v2_new_input_reuses_accepted_original_proof_without_new_business_check(self):
        from autopilot_provenance import capture_origin
        captured = capture_origin(self.source)
        first = self.prepare()
        self.assertEqual(captured['origin_token'], first['origin_token'])
        self.assertEqual(captured['prepared_at'], first['prepared_at'])
        business = self.root/'report.txt'; business.write_text('8\n')
        verification_count = 0
        def verify_once():
            nonlocal verification_count
            verification_count += 1
            self.assertEqual(business.read_text(), '8\n')
        verify_once()
        proof = self.proof(); verified = self.verified_at
        published = self.publish(first)
        self.followup()
        with self.assertRaises(ValueError):
            self.publish(first)
        current = self.prepare()
        self.assertNotEqual(current['receipt_token'], first['receipt_token'])
        self.assertGreater(current['prepared_at'], verified)
        rebound = self.publish(current, completion_check=proof, verified_at=verified)
        self.assertEqual(rebound['original_evidence'],published['original_evidence'])
        with self.assertRaises(ValueError):
            self.publish(current, completion_check=proof, verified_at=datetime.now(timezone.utc).isoformat())
        with self.assertRaises((ValueError,FileNotFoundError)):
            self.publish(current, completion_check=None, verified_at=None,
                evidence_references=['sha256:'+'0'*64])
        with self.assertRaises(ValueError):
            self.publish(current, completion_check=None, verified_at=datetime.now(timezone.utc).isoformat(),
                evidence_references=published['evidence_references'])
        reused = self.publish(current, completion_check=None, verified_at=None,
                              evidence_references=published['evidence_references'])
        self.assertEqual(reused['verified_at'], verified)
        self.assertEqual(reused['evidence_references'], published['evidence_references'])
        self.assertEqual(reused['original_evidence'], published['original_evidence'])
        self.assertEqual(reused['evidence_sources'], [self.evidence_source])
        stopped = self.stop()
        self.assertEqual(stopped['systemMessage'], '')
        self.assertEqual(stopped['completion_recording']['units'][0]['status'], 'applied')
        self.assertEqual(stopped['completion_recording']['units'][0]['original_evidence'], reused['original_evidence'])
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']}, {'met'})
        self.assertEqual(self.publish(current, completion_check=None, verified_at=None,
            evidence_references=published['evidence_references']), reused)
        self.assertEqual(proof, self.completion)
        self.assertEqual(verification_count, 1)

    def test_v2_fresh_proof_cannot_predate_its_original_capture(self):
        old = (datetime.now(timezone.utc)-timedelta(days=1)).isoformat()
        self.proof(old); current = self.prepare()
        with self.assertRaises(ValueError): self.publish(current)
        with self.assertRaises(ValueError): self.publish(current, verified_at=datetime.now(timezone.utc).isoformat())

    def test_v2_dependency_change_reopens_only_affected_evidence(self):
        artifact = self.root/'report.txt'; artifact.write_text('8\n')
        receipt = self.prepare(); self.proof()
        published = self.publish(receipt, dependencies=[str(artifact)])
        self.followup(); current = self.prepare(); artifact.write_text('9\n')
        with self.assertRaises(ValueError):
            self.publish(current, completion_check=None, verified_at=None,
                evidence_references=published['evidence_references'])
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']}, {'unmet'})

    def test_v2_changed_definition_reopens_only_affected_criterion(self):
        receipt = self.prepare(); self.proof(); published = self.publish(receipt)
        rows = copy.deepcopy(self.read_state()['acceptance_criteria'])
        rows[0]['summary'] = 'Changed report condition'
        self.change_intent({'acceptance_criteria':rows}, new_input=True)
        self.arguments['input_event_id'] = self.read_state()['latest_input_event_id']
        current = self.prepare()
        result=self.publish(current, completion_check=None, verified_at=None,
            evidence_references=published['evidence_references'])
        self.assertEqual([c['criterion_id'] for c in result['reopened_criteria']], ['report'])
        self.stop()
        latest=[c for c in self.read_state()['acceptance_criteria'] if c['contract_revision']==2]
        self.assertEqual({c['id']:c['status'] for c in latest}, {'report':'unmet','protected':'met'})
        self.assertEqual(result['verified_at'],published['verified_at'])

    def test_v2_unrelated_criterion_revision_preserves_compatible_original_coverage(self):
        receipt = self.prepare(); self.proof(); published = self.publish(receipt)
        rows = copy.deepcopy(self.read_state()['acceptance_criteria'])
        for row in rows: row['contract_revision'] = 2
        rows.append(dict(id='extra', summary='Unrelated extra output', source='user-explicit',
            admitted=True,status='unmet',work_unit_id='report-unit',contract_revision=2))
        self.core.record_turn(root=self.root/'intent',platform='codex',session_id='session-1',
            raw_user_input='Authorized additional condition',intent_delta={'acceptance_criteria':rows},
            expected_input_event_id=self.read_state()['latest_input_event_id'])
        register_route(self.core,self.root/'intent')
        self.arguments['input_event_id']=self.read_state()['latest_input_event_id']
        current=self.prepare()
        reused=self.publish(current,completion_check=None,verified_at=None,
                            evidence_references=published['evidence_references'])
        self.stop()
        latest=[c for c in self.read_state()['acceptance_criteria'] if c['contract_revision']==2]
        self.assertEqual({c['id']:c['status'] for c in latest}, {'report':'met','protected':'met','extra':'unmet'})
        self.assertEqual(reused['verified_at'],published['verified_at'])

    def test_v2_same_local_id_in_two_units_stays_independent(self):
        rows=[dict(id='same',summary='Output '+unit,source='user-explicit',admitted=True,
            status='unmet',work_unit_id=unit,contract_revision=1) for unit in ('one','two')]
        self.core.record_turn(root=self.root/'intent',platform='codex',session_id='session-1',
            raw_user_input='Two independent outputs',intent_delta={'acceptance_criteria':rows},
            expected_input_event_id='event-1')
        register_route(self.core,self.root/'intent',selected=['one','two'])
        self.arguments['input_event_id']=self.read_state()['latest_input_event_id']
        receipts=self.prepare()['units']
        first=next(r for r in receipts if r['work_unit_id']=='one')
        self.proof(); self.completion=self.completion.replace('report','same')
        self.completion=self.completion.replace('  - protected: Protected file is unchanged [source: user-explicit]\n','')
        self.completion=self.completion.split('  - claim: Protected file is unchanged')[0]+'- unverified: none\n'
        published=self.publish(first); self.stop()
        selected=[c for c in self.read_state()['acceptance_criteria'] if c['work_unit_id'] in ('one','two')]
        self.assertEqual({c['work_unit_id']:c['status'] for c in selected},{'one':'met','two':'unmet'})
        second=next(r for r in receipts if r['work_unit_id']=='two')
        with self.assertRaises((ValueError,FileNotFoundError)):
            self.publish(second,completion_check=None,verified_at=None,evidence_references=published['evidence_references'])
        self.assertEqual(self.publish(first),published)

    def test_v2_partial_publication_allows_next_request_without_overwriting_accepted_proof(self):
        receipt=self.prepare();self.proof()
        self.completion=self.completion.replace('  - protected: Protected file is unchanged [source: user-explicit]\n','')
        self.completion=self.completion.split('  - claim: Protected file is unchanged')[0]+'- unverified: none\n'
        first=self.publish(receipt);self.stop()
        partial=self.stop()['completion_recording']['units'][0]
        self.assertEqual(partial['unit_status'],'pending')
        current=self.prepare()
        self.assertNotEqual(current['receipt_token'],receipt['receipt_token'])
        self.assertEqual(current['criterion_ids'],['protected'])
        self.assertEqual(self.publish(receipt),first)
        self.proof()
        self.completion=self.completion.replace('  - report: Saved report matches input [source: user-explicit]\n','')
        start=self.completion.index('  - claim: Saved report matches input')
        end=self.completion.index('  - claim: Protected file is unchanged')
        self.completion=self.completion[:start]+self.completion[end:]
        second=self.publish(current);self.stop()
        complete=self.stop()['completion_recording']['units'][0]
        self.assertEqual(complete['unit_status'],'complete')
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']},{'met'})
        self.assertNotEqual(first['evidence_references'],second['evidence_references'])

    def test_v2_changed_artifact_keeps_independent_unchanged_criterion_coverage(self):
        artifact=self.root/'report.txt';artifact.write_text('8')
        first_receipt=self.prepare();self.proof()
        self.completion=self.completion.replace('  - protected: Protected file is unchanged [source: user-explicit]\n','')
        self.completion=self.completion.split('  - claim: Protected file is unchanged')[0]+'- unverified: none\n'
        first=self.publish(first_receipt,dependencies=[str(artifact)]);self.stop()
        second_receipt=self.prepare();self.proof()
        self.completion=self.completion.replace('  - report: Saved report matches input [source: user-explicit]\n','')
        start=self.completion.index('  - claim: Saved report matches input')
        end=self.completion.index('  - claim: Protected file is unchanged')
        self.completion=self.completion[:start]+self.completion[end:]
        second=self.publish(second_receipt);self.stop()
        rows=copy.deepcopy(self.read_state()['acceptance_criteria'])
        for row in rows:row.update(contract_revision=2,status='unmet')
        self.core.record_turn(root=self.root/'intent',platform='codex',session_id='session-1',
            raw_user_input='Authorize next revision',intent_delta={'acceptance_criteria':rows},
            expected_input_event_id=self.read_state()['latest_input_event_id'])
        register_route(self.core,self.root/'intent')
        self.arguments['input_event_id']=self.read_state()['latest_input_event_id']
        current=self.prepare();artifact.write_text('9')
        result=self.publish(current,completion_check=None,verified_at=None,
            evidence_references=first['evidence_references']+second['evidence_references'])
        self.assertEqual(result['evidence_references'],second['evidence_references'])
        self.assertEqual(result['reopened_criteria'][0]['criterion_id'],'report')
        self.stop()
        latest=[c for c in self.read_state()['acceptance_criteria'] if c['contract_revision']==2]
        self.assertEqual({c['id']:c['status'] for c in latest},{'report':'unmet','protected':'met'})

    def test_v2_pending_request_applies_only_still_compatible_dependency_records(self):
        artifact=self.root/'report.txt';artifact.write_text('8')
        first_receipt=self.prepare();self.proof()
        self.completion=self.completion.replace('  - protected: Protected file is unchanged [source: user-explicit]\n','')
        self.completion=self.completion.split('  - claim: Protected file is unchanged')[0]+'- unverified: none\n'
        first=self.publish(first_receipt,dependencies=[str(artifact)]);self.stop()
        second_receipt=self.prepare();self.proof()
        self.completion=self.completion.replace('  - report: Saved report matches input [source: user-explicit]\n','')
        start=self.completion.index('  - claim: Saved report matches input')
        end=self.completion.index('  - claim: Protected file is unchanged')
        self.completion=self.completion[:start]+self.completion[end:]
        second=self.publish(second_receipt);self.stop()
        rows=copy.deepcopy(self.read_state()['acceptance_criteria'])
        for row in rows:row.update(contract_revision=2,status='unmet')
        self.core.record_turn(root=self.root/'intent',platform='codex',session_id='session-1',
            raw_user_input='Authorize next revision',intent_delta={'acceptance_criteria':rows},
            expected_input_event_id=self.read_state()['latest_input_event_id'])
        register_route(self.core,self.root/'intent')
        self.arguments['input_event_id']=self.read_state()['latest_input_event_id'];current=self.prepare()
        self.publish(current,completion_check=None,verified_at=None,
            evidence_references=first['evidence_references']+second['evidence_references'])
        artifact.write_text('9')
        stopped=self.stop()['completion_recording']['units'][0]
        self.assertEqual(stopped['unit_status'],'pending')
        self.assertEqual(stopped['remaining_criteria'][0]['criterion_id'],'report')
        latest=[c for c in self.read_state()['acceptance_criteria'] if c['contract_revision']==2]
        self.assertEqual({c['id']:c['status'] for c in latest},{'report':'unmet','protected':'met'})
        self.assertEqual(stopped['evidence_references'],second['evidence_references'])

    def test_v2_aggregate_proof_retains_explicit_independent_dependency_mapping(self):
        report=self.root/'report.txt';protected=self.root/'protected.txt'
        report.write_text('8');protected.write_text('original')
        receipt=self.prepare();self.proof()
        mapping=[dict(work_unit_id='report-unit',contract_revision=1,criterion_id=key,locators=[str(path)])
            for key,path in [('report',report),('protected',protected)]]
        first=self.publish(receipt,criterion_dependencies=mapping)
        self.followup();current=self.prepare();protected.write_text('changed')
        result=self.publish(current,completion_check=None,verified_at=None,evidence_references=first['evidence_references'])
        self.assertEqual(result['reopened_criteria'][0]['criterion_id'],'protected')
        self.stop()
        self.assertEqual({c['id']:c['status'] for c in self.read_state()['acceptance_criteria']},
                         {'report':'met','protected':'unmet'})
        self.assertEqual(result['verified_at'],first['verified_at'])

    def test_v2_pending_dependency_tamper_does_not_mark_met(self):
        artifact=self.root/'report.txt';artifact.write_text('8')
        receipt=self.prepare();self.proof();self.publish(receipt,dependencies=[str(artifact)])
        artifact.write_text('9')
        self.stop()
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']},{'unmet'})

    def test_v2_failed_proof_cannot_publish(self):
        receipt=self.prepare();proof=self.proof()
        with self.assertRaises(ValueError):self.publish(receipt,completion_check=proof.replace('verdict: pass','verdict: fail'))
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']},{'unmet'})


class CompletionV2BoundaryTest(_CompletionV2Fixture):
    def test_v2_missing_or_nonexecution_route_does_not_create_ap_state(self):
        register_route(self.core,self.root/'intent',selected=[])
        result=self.prepare()
        self.assertEqual(result['reason'],'no-current-execution')
        self.assertFalse(self.run_dir.exists())
        self.stop()
        self.assertFalse(self.run_dir.exists())

    def test_v2_tampered_durable_evidence_cannot_apply(self):
        receipt=self.prepare();self.proof();published=self.publish(receipt)
        with self.helper.storage.transaction(self.run_dir,self.source) as store:
            ref=published['evidence_references'][0]
            record=store.completion_read('evidence',ref)
            record['verified_at']=datetime.now(timezone.utc).isoformat()
            changed=store.connection.execute('UPDATE ap_completion_v2_records SET body_json=? WHERE run_key=? AND kind=? AND identity=?',
                (json.dumps(record),store.key,'evidence',ref))
            self.assertEqual(changed.rowcount,1)
        result=self.stop()['completion_recording']['units'][0]
        self.assertEqual(result['status'],'recording-unavailable')
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']},{'unmet'})

    def test_v2_current_constraints_change_requires_affected_new_proof(self):
        receipt=self.prepare();self.proof();published=self.publish(receipt)
        self.change_intent({'constraints':['Read-only current artifact']},new_input=True)
        self.arguments['input_event_id']=self.read_state()['latest_input_event_id'];current=self.prepare()
        with self.assertRaises(ValueError):
            self.publish(current,completion_check=None,verified_at=None,evidence_references=published['evidence_references'])

    def test_v2_different_proof_cannot_overwrite_same_accepted_request(self):
        receipt=self.prepare();self.proof();first=self.publish(receipt)
        with self.assertRaises(ValueError):
            self.helper.publish_completion(receipt_token=receipt['receipt_token'],
                completion_check=self.completion.replace('matches input','matches a different input'),verified_at=self.verified_at,
                evidence_source=self.evidence_source,source=self.source,schema_version='v2')
        self.assertEqual(self.publish(receipt),first)

    def test_v2_foreign_explicit_run_owner_rejected(self):
        receipt=self.prepare();self.proof();self.publish(receipt)
        source=dict(self.source,GHOST_ALICE_SESSION_ID='other',GHOST_ALICE_AUTOPILOT_RUN_DIR=str(self.run_dir))
        from response_route_fixture import native_ledger
        native_ledger(self.root/'intent',self.core,session='other')
        arguments=dict(self.arguments,session_id='other',input_event_id='event-1',source=source)
        result=self.helper.prepare_completion(**arguments,schema_version='v2')
        self.assertEqual(result['status'],'recording-unavailable')
        self.assertEqual(result['reason'],'run-owner-mismatch')
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']},{'unmet'})

    def test_v2_met_and_application_write_roll_back_together(self):
        import sqlite3
        receipt=self.prepare();self.proof();self.publish(receipt)
        with self.helper.storage.transaction(self.run_dir,self.source) as store:
            store.connection.execute("CREATE TRIGGER fail_v2_application BEFORE UPDATE ON ap_completion_v2_units WHEN json_extract(NEW.body_json,'$.status')='applied' BEGIN SELECT RAISE(ABORT,'injected application failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.stop()
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']},{'unmet'})
        with self.helper.storage.transaction(self.run_dir,self.source) as store:
            self.assertEqual(store.completion_unit('report-unit')['status'],'pending-adapter')
            store.connection.execute('DROP TRIGGER fail_v2_application')
        self.stop()
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']},{'met'})

    def test_v2_cli_explicit_schema_publishes_and_reuses_exact_reference(self):
        import os,subprocess
        source={**os.environ,**self.source,'PYTHONDONTWRITEBYTECODE':'1'}
        source.pop('CODEX_THREAD_ID',None)
        base=[sys.executable,'-B',str(publication_fixture.SCRIPT)]
        result=subprocess.run(base+['prepare','--schema-version','autopilot-completion-receipt.v2',
            '--intent-root',str(self.root/'intent'),'--platform','codex','--session-id','session-1',
            '--input-event-id','event-1'],cwd=self.root,env=source,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        receipt=json.loads(result.stdout)
        artifact=self.root/'cli-report.txt';protected=self.root/'cli-protected.txt'
        artifact.write_text('8');protected.write_text('original');self.proof()
        mapping=[dict(work_unit_id='report-unit',contract_revision=1,criterion_id=key,locators=[str(path)])
            for key,path in [('report',artifact),('protected',protected)]]
        args=base+['publish','--schema-version','autopilot-completion-receipt.v2','--receipt-token',receipt['receipt_token'],
            '--verified-at',self.verified_at,'--evidence-source',self.evidence_source,'--completion-file','-',
            '--criterion-dependencies-json',json.dumps(mapping)]
        result=subprocess.run(args,input=self.completion,cwd=self.root,env=source,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        published=json.loads(result.stdout)
        self.assertEqual(published['original_evidence'][0]['criterion_dependencies'],sorted(mapping,key=lambda c:c['criterion_id']))
        self.followup();current=self.prepare()
        args=base+['publish','--schema-version','autopilot-completion-receipt.v2','--receipt-token',current['receipt_token'],
            '--evidence-reference',published['evidence_references'][0]]
        result=subprocess.run(args,cwd=self.root,env=source,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['verified_at'],self.verified_at)


class CompletionV2StrictProtocolTest(_CompletionV2Fixture):
    def test_v2_unknown_protocol_is_rejected_by_all_consumers(self):
        from autopilot_provenance import capture_origin
        bad=dict(self.source,GHOST_ALICE_COMPLETION_SCHEMA='autopilot-completion-receipt.v99')
        with self.assertRaises(ValueError):
            self.helper.prepare_completion(**dict(self.arguments,source=bad))
        with self.assertRaises(ValueError):
            self.helper.publish_completion(receipt_token='unused',completion_check='unused',verified_at='unused',source=bad)
        with self.assertRaises(ValueError):capture_origin(bad)
        with self.assertRaises(ValueError):publication_fixture.adapter.adapter_payload_from_env(bad)
        self.assertFalse(self.run_dir.exists())

    def test_v2_conflicting_explicit_protocol_selections_are_rejected(self):
        receipt=self.prepare();self.proof()
        with self.assertRaises(ValueError):
            self.helper.prepare_completion(**self.arguments,schema_version='autopilot-completion-receipt.v1')
        with self.assertRaises(ValueError):
            self.helper.publish_completion(receipt_token=receipt['receipt_token'],completion_check=self.completion,
                verified_at=self.verified_at,source=self.source,schema_version='autopilot-completion-receipt.v1')
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            self.assertEqual(store.completion_unit('report-unit')['status'],'prepared')

    def test_v2_cannot_promote_accepted_v1_publication_into_bound_v2_evidence(self):
        v1source=dict(self.source,GHOST_ALICE_COMPLETION_SCHEMA='autopilot-completion-receipt.v1')
        receipt=self.helper.prepare_completion(**dict(self.arguments,source=v1source),schema_version='autopilot-completion-receipt.v1')
        self.proof()
        published=self.helper.publish_completion(receipt_token=receipt['receipt_token'],completion_check=self.completion,
            verified_at=self.verified_at,evidence_source=self.evidence_source,source=v1source,schema_version='autopilot-completion-receipt.v1')
        current=self.prepare()
        with self.assertRaises(ValueError):
            self.publish(current,completion_check=None,verified_at=None,
                evidence_references=[published['completion_check_digest']])
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']},{'unmet'})

    def test_v2_capture_preserves_unmigrated_legacy_tasks_without_adopting_them(self):
        adapter=publication_fixture.adapter
        root=self.root/'.autopilot';root.mkdir()
        material=adapter.read_session_material(self.root/'intent','codex','session-1',source=self.source,require_input=True)
        binding=adapter.SESSION_MATERIAL.session_evidence(material)
        run=adapter.SESSION_MATERIAL.build_approved_run(intent_state=material['intent_state'],
            approval_evidence={'decision':'approved','source':'old-admission','session_intent':binding},
            run_id='old-run',remaining_steps=2,allowed_surfaces=['old-plan.md'],stop_conditions=['user_stop'])
        task=adapter.SESSION_MATERIAL.session_intent_task(intent_state=material['intent_state'],
            session_id='session-1',allowed_surfaces=['old-plan.md'])
        (root/'approved-run.json').write_text(json.dumps(run))
        (root/'tasks.jsonl').write_text(json.dumps(task)+'\n')
        before={p.name:p.read_bytes() for p in (root/'approved-run.json',root/'tasks.jsonl')}
        receipt=self.prepare()
        self.assertEqual(Path(receipt['run_dir']),root)
        with self.helper.storage.transaction(root,self.source,migrate_legacy=False) as store:
            self.assertEqual(store.connection.execute('SELECT COUNT(*) FROM ap_runs').fetchone()[0],0)
        self.assertEqual({p.name:p.read_bytes() for p in (root/'approved-run.json',root/'tasks.jsonl')},before)

    def test_v2_met_adapter_rejects_foreign_current_authority(self):
        receipt=self.prepare();self.proof();published=self.publish(receipt)
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            unit=store.completion_unit('report-unit')
            publication=store.completion_read('publication',unit['publication_reference'])
            records={ref:store.completion_read('evidence',ref) for ref in published['evidence_references']}
            source=dict(self.source,GHOST_ALICE_SESSION_INTENT_ROOT=str(self.root/'foreign-intent'))
            with self.assertRaises(ValueError):
                publication_fixture.work_items.materialize_met_criteria_v2(publication['request'],records,source,store.connection)
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']},{'unmet'})

    def test_v2_pure_request_rejects_unqualified_or_foreign_coverage(self):
        import autopilot_completion_contract as contract
        receipt=self.prepare();self.proof();published=self.publish(receipt)
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            original=store.completion_read('receipt',receipt['receipt_token'])
            ref=published['evidence_references'][0]
            records={ref:store.completion_read('evidence',ref)}
            for coverage in ({('other-unit',1,'report'):ref},{('report-unit',1,'report'):'sha256:'+'0'*64},{}):
                with self.subTest(coverage=coverage),self.assertRaises(ValueError):
                    contract.publication_request(binding=original['binding'],contract=original['contract'],target=original['target'],
                        coverage=coverage,records=records,created_at=original['prepared_at'],checked_at=self.verified_at)

    def test_v2_ambiguous_or_malformed_dependency_mapping_is_typed_failure(self):
        receipt=self.prepare();self.proof()
        malformed=[dict(work_unit_id='report-unit',contract_revision=1,criterion_id='report',locators=[{}])]
        with self.assertRaises(ValueError):self.publish(receipt,criterion_dependencies=malformed)
        ambiguous=[dict(work_unit_id='report-unit',contract_revision=1,criterion_id='report',locators=[])]*2
        with self.assertRaises(ValueError):self.publish(receipt,criterion_dependencies=ambiguous)
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            self.assertEqual(store.completion_unit('report-unit')['status'],'prepared')

    def test_v2_fresh_null_proof_is_typed_recording_failure(self):
        receipt=self.prepare();self.proof()
        with self.assertRaises(ValueError):
            self.publish(receipt,completion_check=None,verified_at=None)

class SuccessfulPartialApplicationV2Test(_CompletionV2Fixture):
    def test_applied_partial_continues_remaining_work_without_answer_replay(self):
        import autopilot_mode
        receipt=self.prepare();self.proof()
        self.completion=self.completion.replace('  - protected: Protected file is unchanged [source: user-explicit]\n','')
        self.completion=self.completion.split('  - claim: Protected file is unchanged')[0]+'- unverified: none\n'
        first=self.publish(receipt)
        stopped=self.stop()
        unit=stopped['completion_recording']['units'][0]
        self.assertEqual(unit['status'],'applied')
        self.assertEqual(unit['unit_status'],'pending')
        self.assertEqual([c['criterion_id'] for c in unit['remaining_criteria']],['protected'])
        self.assertTrue(unit['application_reference'])
        self.assertNotIn('recovery',stopped)
        self.assertEqual(stopped['systemMessage'],'')
        self.assertNotIn('decision',autopilot_mode._format_payload_for_hook(stopped,dict(hook_event_name='Stop')))
        self.assertEqual({c['id']:c['status'] for c in self.read_state()['acceptance_criteria']},
                         {'report':'met','protected':'unmet'})
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            self.assertIsNone(store.completion_recovery_head('report-unit'))
            original=copy.deepcopy(store.completion_read('evidence',first['evidence_references'][0]))
        self.followup();current=self.prepare()
        self.assertEqual(current['criterion_ids'],['protected'])
        self.proof()
        self.completion=self.completion.replace('  - report: Saved report matches input [source: user-explicit]\n','')
        start=self.completion.index('  - claim: Saved report matches input')
        end=self.completion.index('  - claim: Protected file is unchanged')
        self.completion=self.completion[:start]+self.completion[end:]
        self.publish(current);complete=self.stop()
        self.assertEqual(complete['completion_recording']['units'][0]['unit_status'],'complete')
        self.assertNotIn('recovery',complete)
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            self.assertEqual(store.completion_read('evidence',first['evidence_references'][0]),original)

    def test_current_selected_subset_leaves_other_authorized_work_unrecorded(self):
        state=self.read_state();route=state['response_route']
        candidate={k:copy.deepcopy(route[k]) for k in
                   ('schema_version','routing_surface','work_units','selected_work_unit_ids','selected_criteria')}
        candidate['selected_criteria']=[dict(work_unit_id='report-unit',contract_revision=1,criterion_id='report')]
        self.core.record_response_route(root=self.root/'intent',platform='codex',session_id='session-1',
            expected_input_event_id=state['latest_input_event_id'],boundary_ref='test-boundary',candidate=candidate)
        receipt=self.prepare();self.proof()
        self.assertEqual(receipt['criterion_ids'],['report'])
        self.completion=self.completion.replace('  - protected: Protected file is unchanged [source: user-explicit]\n','')
        self.completion=self.completion.split('  - claim: Protected file is unchanged')[0]+'- unverified: none\n'
        self.publish(receipt);stopped=self.stop()
        self.assertNotIn('recovery',stopped)
        self.assertEqual(stopped['systemMessage'],'')
        self.assertEqual({c['id']:c['status'] for c in self.read_state()['acceptance_criteria']},
                         {'report':'met','protected':'unmet'})


class CompletionV2ReviewRegressionTest(_CompletionV2Fixture):
    def aggregate(self):
        self.report=self.root/'report-artifact.txt';self.protected=self.root/'protected-artifact.txt'
        self.report.write_text('8');self.protected.write_text('original')
        receipt=self.prepare();self.proof()
        mapping=[dict(work_unit_id='report-unit',contract_revision=1,criterion_id=key,locators=[str(path)])
            for key,path in [('report',self.report),('protected',self.protected)]]
        return receipt,self.publish(receipt,criterion_dependencies=mapping)

    def test_applied_proof_dependency_invalidation_reopens_only_affected_met_criterion(self):
        _,published=self.aggregate();self.stop();before=self.read_state()
        self.protected.write_text('changed')
        stopped=self.stop()['completion_recording']['units'][0]
        after=self.read_state()
        self.assertNotEqual(stopped.get('status'),'recording-unavailable',stopped)
        self.assertEqual({c['id']:c['status'] for c in after['acceptance_criteria']},{'report':'met','protected':'unmet'})
        self.assertEqual(after['acceptance_criteria'][0],before['acceptance_criteria'][0])
        self.assertEqual(stopped['unit_status'],'pending')
        self.assertEqual(stopped['remaining_criteria'][0]['criterion_id'],'protected')
        self.assertEqual(stopped['original_evidence'][0]['verified_at'],published['verified_at'])
        self.assertEqual(stopped['original_evidence'][0]['proof_locator'],self.evidence_source)
        before_repeat=self.read_state();self.stop();self.assertEqual(self.read_state(),before_repeat)

    def test_dependency_mutation_inside_privileged_application_rolls_back_met_and_application(self):
        from unittest import mock
        _,published=self.aggregate()
        original=publication_fixture.work_items.materialize_met_criteria_v2
        def changed_during_application(*args,**kwargs):
            self.protected.write_text('changed during application')
            return original(*args,**kwargs)
        with mock.patch.object(publication_fixture.work_items,'materialize_met_criteria_v2',side_effect=changed_during_application):
            stopped=self.stop()['completion_recording']['units'][0]
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']},{'unmet'})
        self.assertNotEqual(stopped.get('unit_status'),'complete')
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            self.assertEqual(store.completion_unit('report-unit')['applied_requests'],[])
        stopped=self.stop()['completion_recording']['units'][0]
        self.assertEqual({c['id']:c['status'] for c in self.read_state()['acceptance_criteria']},{'report':'met','protected':'unmet'})
        self.assertEqual(stopped['original_evidence'][0]['verified_at'],published['verified_at'])

    def test_native_stop_never_loads_cli_for_validation_or_application(self):
        from unittest import mock
        self.aggregate()
        original=publication_fixture.adapter.importlib.util.spec_from_file_location
        def prohibit_cli(name,path,*args,**kwargs):
            if str(path).endswith('/scripts/autopilot_completion.py'):
                raise AssertionError('State must not depend on CLI')
            return original(name,path,*args,**kwargs)
        with mock.patch.object(publication_fixture.adapter.importlib.util,'spec_from_file_location',side_effect=prohibit_cli):
            self.assertEqual(self.stop()['completion_recording']['units'][0]['unit_status'],'complete')

    def test_invalidation_of_old_ap_proof_never_withdraws_newer_core_met_proof(self):
        self.aggregate();self.stop()
        row=next(c for c in self.read_state()['acceptance_criteria'] if c['id']=='protected')
        self.core.mark_acceptance_criterion_met(root=self.root/'intent',platform='codex',session_id='session-1',
            criterion_id='protected',expected_input_event_id=self.read_state()['latest_input_event_id'],
            expected_criterion=row,completion_check_digest='b'*64)
        self.protected.write_text('changed');before=self.read_state()
        self.stop()
        self.assertEqual(self.read_state()['acceptance_criteria'][1],before['acceptance_criteria'][1])


class CompletionV2HistoryRegressionTest(_CompletionV2Fixture):
    def partials(self, apply_second=True):
        self.report=self.root/'history-A';self.protected=self.root/'history-B'
        self.report.write_text('A');self.protected.write_text('B')
        receipt=self.prepare();self.proof()
        self.completion=self.completion.replace('  - protected: Protected file is unchanged [source: user-explicit]\n','')
        self.completion=self.completion.split('  - claim: Protected file is unchanged')[0]+'- unverified: none\n'
        first=self.publish(receipt,dependencies=[str(self.report)]);self.stop()
        self.followup();receipt=self.prepare();self.proof()
        self.completion=self.completion.replace('  - report: Saved report matches input [source: user-explicit]\n','')
        start=self.completion.index('  - claim: Saved report matches input')
        end=self.completion.index('  - claim: Protected file is unchanged')
        self.completion=self.completion[:start]+self.completion[end:]
        second=self.publish(receipt,dependencies=[str(self.protected)])
        if apply_second:self.stop()
        return first,second

    def test_stop_checks_prior_partial_proof_after_newer_partial_publication(self):
        _,second=self.partials();before=self.read_state()
        self.report.write_text('changed A')
        result=self.stop()['completion_recording']['units'][0]
        after=self.read_state()
        self.assertEqual({c['id']:c['status'] for c in after['acceptance_criteria']},{'report':'unmet','protected':'met'},result)
        self.assertEqual(after['acceptance_criteria'][1],before['acceptance_criteria'][1])
        self.assertEqual(result['unit_status'],'pending')
        self.assertEqual(result['original_evidence'][0]['verified_at'],second['verified_at'])
        self.assertEqual(result['original_evidence'][0]['proof_locator'],second['original_evidence'][0]['proof_locator'])

    def test_prior_partial_dependency_race_rolls_back_new_application_and_reopens_old(self):
        from unittest import mock
        self.partials(apply_second=False)
        original=publication_fixture.work_items.materialize_met_criteria_v2
        def mutation(*args,**kwargs):
            self.report.write_text('A changed during B application')
            return original(*args,**kwargs)
        with mock.patch.object(publication_fixture.work_items,'materialize_met_criteria_v2',side_effect=mutation):
            result=self.stop()['completion_recording']['units'][0]
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']},{'unmet'},result)
        self.assertNotEqual(result.get('unit_status'),'complete')
        result=self.stop()['completion_recording']['units'][0]
        self.assertEqual({c['id']:c['status'] for c in self.read_state()['acceptance_criteria']},{'report':'unmet','protected':'met'})
        self.assertEqual(result['unit_status'],'pending')

    def test_missing_retained_backing_blocks_whole_unit_completion(self):
        self.partials()
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            state=store.completion_unit('report-unit')
            store.completion_set_unit(dict(state,application_references={}))
        before=self.read_state()
        result=self.stop()['completion_recording']['units'][0]
        self.assertEqual(result['reason'],'met-evidence-incompatible')
        self.assertNotEqual(result.get('unit_status'),'complete')
        self.assertEqual(self.read_state(),before)

    def test_deleted_prior_artifact_reopens_only_its_met_criterion(self):
        self.partials();self.report.unlink()
        result=self.stop()['completion_recording']['units'][0]
        self.assertEqual({c['id']:c['status'] for c in self.read_state()['acceptance_criteria']},{'report':'unmet','protected':'met'},result)
        self.assertEqual(result['unit_status'],'pending')


class CompletionV2LogicalLocatorRegressionTest(_CompletionV2Fixture):
    def logical_artifacts(self,parent_link=False):
        first=self.root/'original';second=self.root/'replacement'
        if parent_link:
            first.mkdir();second.mkdir()
            (first/'report').write_text('A');(second/'report').write_text('B')
        else:
            first.write_text('A');second.write_text('B')
        link=self.root/'current';link.symlink_to(first,target_is_directory=parent_link)
        logical=link/'report' if parent_link else link
        self.report=logical;self.protected=self.root/'ordinary';self.protected.write_text('stable')
        receipt=self.prepare();self.proof()
        mapping=[dict(work_unit_id='report-unit',contract_revision=1,criterion_id=key,locators=[str(path)])
            for key,path in [('report',logical),('protected',self.protected)]]
        published=self.publish(receipt,criterion_dependencies=mapping)
        return link,second,published,logical

    def retarget(self,link,replacement):
        link.unlink();link.symlink_to(replacement,target_is_directory=replacement.is_dir())

    def test_logical_symlink_before_first_application_keeps_affected_criterion_unmet(self):
        link,replacement,published,logical=self.logical_artifacts()
        self.retarget(link,replacement)
        result=self.stop()['completion_recording']['units'][0]
        self.assertEqual({c['id']:c['status'] for c in self.read_state()['acceptance_criteria']},{'report':'unmet','protected':'met'},result)
        self.assertEqual(result['unit_status'],'pending')
        self.assertEqual(result['original_evidence'][0]['verified_at'],published['verified_at'])
        self.assertIn(str(logical),[d['locator'] for d in published['original_evidence'][0]['dependencies']])

    def test_applied_logical_symlink_retarget_reopens_only_affected_criterion(self):
        link,replacement,published,_=self.logical_artifacts();self.stop();before=self.read_state()
        self.retarget(link,replacement)
        result=self.stop()['completion_recording']['units'][0]
        self.assertEqual({c['id']:c['status'] for c in self.read_state()['acceptance_criteria']},{'report':'unmet','protected':'met'},result)
        self.assertEqual(self.read_state()['acceptance_criteria'][1],before['acceptance_criteria'][1])
        self.assertEqual(result['original_evidence'],published['original_evidence'])
        self.assertEqual(result['unit_status'],'pending')

    def test_symlinked_parent_retarget_is_checked_after_application(self):
        link,replacement,published,_=self.logical_artifacts(parent_link=True);self.stop();before=self.read_state()
        self.retarget(link,replacement)
        result=self.stop()['completion_recording']['units'][0]
        self.assertEqual({c['id']:c['status'] for c in self.read_state()['acceptance_criteria']},{'report':'unmet','protected':'met'},result)
        self.assertEqual(self.read_state()['acceptance_criteria'][1],before['acceptance_criteria'][1])
        self.assertEqual(result['original_evidence'],published['original_evidence'])

    def test_native_subprocess_checks_retarget_before_application(self):
        import os,subprocess
        link,replacement,published,_=self.logical_artifacts()
        self.retarget(link,replacement)
        env={**os.environ,**self.source,'PYTHONDONTWRITEBYTECODE':'1'}
        env.pop('CODEX_THREAD_ID',None)
        command=[sys.executable,'-B',str(publication_fixture.SKILL/'adapters/autopilot_mode.py')]
        result=subprocess.run(command,input=json.dumps({'cwd':str(self.root)}),cwd=self.root,env=env,
            capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        unit=json.loads(result.stdout)['completion_recording']['units'][0]
        self.assertEqual(unit['unit_status'],'pending',unit)
        self.assertEqual({c['id']:c['status'] for c in self.read_state()['acceptance_criteria']},{'report':'unmet','protected':'met'})
        self.assertEqual(unit['original_evidence'],published['original_evidence'])

    def test_shared_dependency_snapshot_keeps_supplied_logical_locator(self):
        first=self.root/'original';replacement=self.root/'replacement';link=self.root/'current'
        first.write_text('A');replacement.write_text('B');link.symlink_to(first)
        receipt=self.prepare();self.proof();published=self.publish(receipt,dependencies=[str(link)])
        self.assertEqual(published['original_evidence'][0]['dependencies'][0]['locator'],str(link))
        self.retarget(link,replacement)
        unit=self.stop()['completion_recording']['units'][0]
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']},{'unmet'})
        self.assertEqual(unit['unit_status'],'pending')


class NativeRecordingRecoveryV2Test(_CompletionV2Fixture):
    def test_derived_generated_command_roundtrip_preserves_authoritative_origin(self):
        import subprocess, shlex, os
        first=self.prepare()
        self.assertIn('prepare_command',first)
        command=first['prepare_command']
        self.assertIn('--schema-version autopilot-completion-receipt.v2', command)
        result=subprocess.run(shlex.split(command), env={**os.environ,**self.source,'CODEX_THREAD_ID':'session-1'}, text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        rebound=json.loads(result.stdout)
        self.assertEqual(rebound['receipt_token'],first['receipt_token'])
        self.assertEqual(rebound['target_reference'],first['target_reference'])
        self.assertEqual(rebound['target_reference']['selection_origin'],'derived')

    def test_v2_capture_preflight_describes_native_recording(self):
        from autopilot_pretool import capture_before_tool
        result=capture_before_tool(self.source,dict(hook_event_name='PreToolUse',session_id='session-1',cwd=str(self.root)),preflight=True)
        self.assertIn('additional_context',result)
        self.assertIn('native Stop consumes the Core-reviewed response',result['additional_context'])
        self.assertNotIn(result['receipt_token'],result['additional_context'])

    def test_v2_stop_prepared_without_completion_waits_for_reviewed_answer(self):
        self.prepare()
        host=dict(hook_event_name='Stop',session_id='session-1',
            host_context=dict(delivery_surface='visible_transcript'),last_assistant_message='Saved requested report')
        for _ in range(2):
            result=publication_fixture.adapter.adapter_payload_from_env(self.source,hook_input=host)
            self.assertNotIn('recovery',result)
            self.assertEqual(result['systemMessage'],'')
        self.followup();self.prepare()
        later=publication_fixture.adapter.adapter_payload_from_env(self.source,hook_input=host)
        self.assertNotIn('recovery',later)
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']},{'unmet'})
        self.proof()
        self.completion+='- evidence: '+self.evidence_source+'\n'
        self.review()
        host['last_assistant_message']=self.completion
        applied=publication_fixture.adapter.adapter_payload_from_env(self.source,hook_input=host)
        self.assertEqual(applied['completion_recording']['units'][0]['unit_status'],'complete')
        original=applied['completion_recording']['units'][0]['original_evidence'][0]
        self.assertEqual(original['verified_at'],self.verified_at)
        self.assertEqual(applied['systemMessage'],'')

    def test_rejected_precapture_proof_parks_without_reissuing_publication(self):
        original_time=(datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat()
        original_proof=self.proof(original_time)
        receipt=self.prepare()
        with self.assertRaisesRegex(ValueError,'Verification must follow ORIGINAL capture'):
            self.publish(receipt)
        host=dict(hook_event_name='Stop',session_id='session-1',last_assistant_message=original_proof)
        def stop_original():
            return publication_fixture.adapter.adapter_payload_from_env(self.source,hook_input=host)
        first=stop_original()
        self.assertEqual(first['recovery']['status'],'diagnostic')
        self.assertNotIn(receipt['publish_command'],first['systemMessage'])
        parked=stop_original()
        self.assertEqual(parked['recovery']['status'],'parked')
        self.assertNotIn(receipt['publish_command'],parked['systemMessage'])
        self.assertNotIn('Publish existing supported proof',parked['systemMessage'])
        self.followup();current=self.prepare()
        later=stop_original()
        self.assertEqual(later['recovery']['status'],'parked')
        self.assertNotIn(current['publish_command'],later['systemMessage'])
        self.assertNotIn('Publish existing supported proof',later['systemMessage'])
        self.assertEqual(self.verified_at,original_time)
        self.assertEqual(self.completion,original_proof)
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']},{'unmet'})

    def test_v2_stop_dependency_change_invalidates_coverage_without_requesting_an_answer(self):
        artifact=self.root/'report.txt';artifact.write_text('8')
        receipt=self.prepare();self.proof();self.publish(receipt,dependencies=[str(artifact)])
        artifact.write_text('9')
        host=dict(hook_event_name='Stop',session_id='session-1',host_context=dict(delivery_surface='visible_transcript'),last_assistant_message='Saved requested report')
        failed=publication_fixture.adapter.adapter_payload_from_env(self.source,hook_input=host)
        self.assertTrue(failed['recovery']['claims_changed'])
        self.assertNotIn('delivery_route',failed['recovery'])
        self.assertNotIn('decision',__import__('autopilot_mode')._format_payload_for_hook(failed,host))
        self.assertEqual(failed['recovery']['status'],'diagnostic')
        self.assertEqual(publication_fixture.adapter.adapter_payload_from_env(self.source,hook_input=host)['recovery']['status'],'parked')
        artifact.write_text('8')
        repaired=publication_fixture.adapter.adapter_payload_from_env(self.source,hook_input=host)
        self.assertEqual(repaired['completion_recording']['units'][0]['unit_status'],'complete')


class TargetAcquisitionRecoveryV2Test(_CompletionV2Fixture):
    def test_only_derived_acquisition_permission_failure_is_unavailable(self):
        import autopilot_storage as storage
        from unittest import mock
        with mock.patch.object(storage,'_atomic',side_effect=PermissionError('derived directory')):
            result=self.prepare()
        self.assertEqual(result['status'],'recording-unavailable')
        self.assertEqual(result['reason'],'derived-target-unwritable')
        explicit=dict(self.source,GHOST_ALICE_AUTOPILOT_RUN_DIR=str(self.run_dir))
        with mock.patch.object(storage,'_atomic',side_effect=PermissionError('explicit directory')):
            with self.assertRaises(PermissionError):
                self.helper.prepare_completion(**dict(self.arguments,source=explicit),schema_version='v2')

    def test_permission_processing_failure_is_not_unwritable_acquisition(self):
        import autopilot_storage as storage
        from unittest import mock
        with mock.patch.object(storage.RunStore,'completion_put',side_effect=PermissionError('processing')):
            with self.assertRaises(PermissionError):self.prepare()

    def test_stop_derived_acquisition_parks_in_current_authority_without_target_write(self):
        import autopilot_storage as storage
        from unittest import mock
        self.proof()
        host=dict(hook_event_name='Stop',session_id='session-1',last_assistant_message=self.completion,host_context=dict(delivery_surface='visible_transcript'))
        with mock.patch.object(storage,'_atomic',side_effect=PermissionError('derived directory')):
            first=publication_fixture.adapter.adapter_payload_from_env(self.source,hook_input=host)
            second=publication_fixture.adapter.adapter_payload_from_env(self.source,hook_input=host)
        self.assertEqual(first['completion_recording']['units'][0]['reason'],'derived-target-unwritable')
        self.assertEqual(first['recovery']['status'],'diagnostic')
        self.assertEqual(second['recovery']['status'],'parked')
        self.assertFalse(self.run_dir.exists())
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']},{'unmet'})


class DescriptorCommandFilesystemV2Test(_CompletionV2Fixture):
    def command(self,command):
        import shlex,subprocess,os
        return subprocess.run(shlex.split(command),env={**os.environ,**self.source,'CODEX_THREAD_ID':'session-1'},text=True,capture_output=True)

    def test_generated_derived_command_readonly_reacquisition_differs_from_explicit(self):
        import autopilot_messages as messages
        receipt=self.prepare()
        (self.run_dir/'authority.json').unlink()
        self.run_dir.chmod(0o555)
        self.addCleanup(self.run_dir.chmod,0o755)
        result=self.command(receipt['prepare_command'])
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['reason'],'derived-target-unwritable')
        plain=messages.completion_command(dict(intent_root=str(self.root/'intent'),platform='codex',session_id='session-1'),str(self.run_dir),'prepare','--intent-root',str(self.root/'intent'),'--platform','codex','--session-id','session-1','--input-event-id','event-1','--schema-version','autopilot-completion-receipt.v2')
        rejected=self.command(plain)
        self.assertEqual(rejected.returncode,2)
        self.assertIn('Permission denied',rejected.stderr)

    def test_logical_target_alias_and_authoritative_origin_survive_generated_command(self):
        alias=self.root/'logical';alias.symlink_to(self.root,target_is_directory=True)
        source=dict(self.source,GHOST_ALICE_AUTOPILOT_CWD=str(alias))
        receipt=self.helper.prepare_completion(**dict(self.arguments,source=source),schema_version='v2')
        self.assertEqual(receipt['target_reference']['path'],str(alias/'.autopilot/sessions/codex/session-1'))
        rebound=self.command(receipt['prepare_command'])
        self.assertEqual(rebound.returncode,0,rebound.stderr)
        self.assertEqual(json.loads(rebound.stdout)['target_reference'],receipt['target_reference'])

    def test_descriptor_reference_rejects_wrong_current_binding_and_env_origin_assertion(self):
        receipt=self.prepare();self.followup()
        stale=self.command(receipt['prepare_command'])
        self.assertEqual(stale.returncode,2)
        explicit=dict(self.source,GHOST_ALICE_AUTOPILOT_RUN_DIR=str(self.run_dir),GHOST_ALICE_AUTOPILOT_DERIVED_TARGET='true')
        current=self.helper.prepare_completion(**dict(self.arguments,source=explicit),schema_version='v2')
        self.assertEqual(current['target_reference']['selection_origin'],'explicit')


class ThinStopFallbackRecoveryV2Test(_CompletionV2Fixture):
    def test_actual_native_explicit_target_error_is_visible_once_then_parked(self):
        import os,subprocess
        blocked=self.root/'occupied-target';blocked.write_text('occupied')
        env={**os.environ,**self.source,'CODEX_THREAD_ID':'session-1','GHOST_ALICE_AUTOPILOT_RUN_DIR':str(blocked)}
        command=[sys.executable,str(publication_fixture.SKILL/'adapters/autopilot_mode.py')]
        self.proof()
        host=dict(hook_event_name='Stop',session_id='session-1',last_assistant_message=self.completion,host_context=dict(delivery_surface='visible_transcript'))
        def invoke():
            result=subprocess.run(command,input=json.dumps(host),env=env,text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            return json.loads(result.stdout)
        first=invoke();second=invoke()
        self.assertNotIn('decision',first)
        self.assertIn('File exists',first['systemMessage'])
        self.assertEqual(first['recovery']['status'],'diagnostic')
        self.assertEqual(second['recovery']['status'],'parked')
        self.assertNotIn('decision',second)
        self.assertEqual(blocked.read_text(),'occupied')


class UnrecordableRecoveryV2Test(_CompletionV2Fixture):
    def test_unwritable_core_diagnostic_persistence_parks_without_repeat_block(self):
        import sqlite3,autopilot_completion_service as service,autopilot_storage as storage,autopilot_mode
        from unittest import mock
        self.proof()
        host=dict(hook_event_name='Stop',session_id='session-1',last_assistant_message=self.completion,host_context=dict(delivery_surface='visible_transcript'))
        with mock.patch.object(storage,'completion_authority_transaction',side_effect=sqlite3.OperationalError('readonly authority')):
            for _ in range(2):
                result=service.recording_failure_payload(self.source,host,OSError('recording failure'))
                self.assertEqual(result['recovery']['status'],'parked')
                self.assertNotIn('decision',autopilot_mode._format_payload_for_hook(result,host))
                self.assertIn('recording failure',result['systemMessage'])
        self.assertFalse(self.run_dir.exists())


class AcquisitionPhaseBoundaryV2Test(_CompletionV2Fixture):
    def test_preflight_and_core_transaction_permission_errors_are_not_target_acquisition(self):
        import autopilot_storage as storage
        from unittest import mock
        with mock.patch.object(publication_fixture.work_items,'_load_core_ledger_module',return_value=self.core), mock.patch.object(self.core,'storage_transaction',side_effect=PermissionError('Core transaction')):
            with self.assertRaises(PermissionError):self.prepare()
        import autopilot_completion_service as service
        current,root,target,_,_=service._v2_material(self.source)
        def fail(_):raise PermissionError('preflight processing')
        with self.assertRaises(PermissionError):
            with service._v2_recording_transaction(root,current,target,preflight=fail):self.fail('must not enter')


class RetainedProofRecoveryDeliveryV2Test(_CompletionV2Fixture):
    partials=CompletionV2HistoryRegressionTest.partials

    def test_retained_partial_invalidation_preserves_independent_coverage(self):
        self.partials();self.report.write_text('changed A')
        host=dict(hook_event_name='Stop',session_id='session-1',host_context=dict(delivery_surface='visible_transcript'),last_assistant_message='Saved report and protected file')
        result=publication_fixture.adapter.adapter_payload_from_env(self.source,hook_input=host)
        self.assertTrue(result['recovery']['claims_changed'])
        self.assertNotIn('delivery_route',result['recovery'])
        self.assertEqual({c['id']:c['status'] for c in self.read_state()['acceptance_criteria']},{'report':'unmet','protected':'met'})

    def test_malformed_metadata_retry_is_rejected_by_core_without_recovery(self):
        self.prepare()
        host=dict(hook_event_name='Stop',session_id='session-1',host_context=dict(delivery_surface='visible_transcript'),last_assistant_message='Saved report')
        publication_fixture.adapter.adapter_payload_from_env(self.source,hook_input=host)
        host['last_assistant_message']='[completion-check]\n- unverified: none\n[io-trace]\n- files-read: [/report]'
        second=publication_fixture.adapter.adapter_payload_from_env(self.source,hook_input=host)
        self.assertEqual(second['completion_recording']['reason'],'completion-body-invalid')
        self.assertNotIn('recovery',second)
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']},{'unmet'})


class PublicationAcquisitionRoundtripV2Test(_CompletionV2Fixture):
    def test_generated_derived_publish_command_preserves_acquisition_failure(self):
        import os,shlex,subprocess
        receipt=self.prepare();self.proof()
        (self.run_dir/'authority.json').unlink();self.run_dir.chmod(0o555)
        self.addCleanup(self.run_dir.chmod,0o755)
        args=shlex.split(receipt['publish_command'].replace('ORIGINAL_ISO_TIME',self.verified_at))
        args.extend(['--evidence-source',self.evidence_source])
        result=subprocess.run(args,input=self.completion,env={**os.environ,**self.source,'CODEX_THREAD_ID':'session-1'},text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['reason'],'derived-target-unwritable')


class DependentAdvancementBoundV2Test(_CompletionV2Fixture):
    def test_identical_processing_failure_does_not_reapply_on_stop_or_new_input(self):
        from unittest import mock
        receipt=self.prepare();self.proof();published=self.publish(receipt)
        with mock.patch.object(publication_fixture.work_items,'materialize_met_criteria_v2',side_effect=OSError('known processing failure')) as apply:
            self.stop();second=self.stop()
            self.assertEqual(apply.call_count,1)
            self.assertEqual(second['recovery']['status'],'parked')
            self.followup();current=self.prepare()
            self.publish(current,completion_check=None,verified_at=None,evidence_references=published['evidence_references'])
            third=self.stop()
            self.assertEqual(apply.call_count,1)
            self.assertEqual(third['recovery']['status'],'parked')

    def test_identical_denied_acquisition_does_not_reacquire_without_actual_delta(self):
        import autopilot_storage as storage
        from unittest import mock
        self.proof()
        host=dict(hook_event_name='Stop',session_id='session-1',last_assistant_message=self.completion)
        with mock.patch.object(storage,'_atomic',side_effect=PermissionError('known acquisition failure')) as atomic:
            publication_fixture.adapter.adapter_payload_from_env(self.source,hook_input=host)
            second=publication_fixture.adapter.adapter_payload_from_env(self.source,hook_input=host)
            self.assertEqual(atomic.call_count,1)
            self.assertEqual(second['recovery']['status'],'parked')


class RelevantDeltaRetryV2Test(_CompletionV2Fixture):
    def test_actual_artifact_delta_allows_bounded_retry_and_original_proof_reuse(self):
        from unittest import mock
        artifact=self.root/'report';artifact.write_text('8')
        receipt=self.prepare();self.proof();published=self.publish(receipt,dependencies=[str(artifact)])
        with mock.patch.object(publication_fixture.work_items,'materialize_met_criteria_v2',side_effect=OSError('known storage failure')) as apply:
            self.stop();self.stop();self.assertEqual(apply.call_count,1)
        artifact.write_text('9');changed=self.stop()
        self.assertEqual(changed['recovery']['status'],'diagnostic')
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']},{'unmet'})
        artifact.write_text('8');repaired=self.stop()
        self.assertEqual(repaired['completion_recording']['units'][0]['unit_status'],'complete')
        self.assertEqual(repaired['completion_recording']['units'][0]['original_evidence'],published['original_evidence'])


class NativeRecordingCodeRepairV2Test(_CompletionV2Fixture):
    def test_actual_native_writer_code_repair_resumes_byte_identical_request(self):
        import os,shutil,subprocess
        receipt=self.prepare();self.proof();published=self.publish(receipt)
        clone=self.root/'source-fixture/skill'
        for folder in ('adapters','scripts'):
            shutil.copytree(publication_fixture.SKILL/folder,clone/folder)
        writer=clone/'adapters/autopilot_work_items.py';original=writer.read_text()
        counter=self.root/'failed-application-count'
        writer.write_text(original+f'\n\ndef materialize_met_criteria_v2(*args,**kwargs):\n    from pathlib import Path\n    counter=Path({str(counter)!r})\n    counter.write_text(str(int(counter.read_text())+1) if counter.exists() else "1")\n    raise OSError("known source writer defect")\n')
        env={**os.environ,**self.source,'CODEX_THREAD_ID':'session-1'}
        command=[sys.executable,str(clone/'adapters/autopilot_mode.py')]
        def invoke():
            result=subprocess.run(command,input=json.dumps(dict(hook_event_name='Stop',session_id='session-1')),env=env,text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            return json.loads(result.stdout)
        invoke();parked=invoke()
        self.assertEqual(counter.read_text(),'1')
        self.assertEqual(parked['recovery']['status'],'parked')
        writer.write_text(original)
        repaired=invoke()
        self.assertEqual(repaired['completion_recording']['units'][0]['unit_status'],'complete')
        self.assertEqual(repaired['completion_recording']['units'][0]['original_evidence'],published['original_evidence'])


class RecoveryPartialIntegrityV2Test(_CompletionV2Fixture):
    def test_mutable_diagnostic_head_cannot_forge_unit_completion(self):
        from unittest import mock
        receipt=self.prepare();self.proof();self.publish(receipt)
        with mock.patch.object(publication_fixture.work_items,'materialize_met_criteria_v2',side_effect=OSError('failure')):
            self.stop()
            with self.helper.storage.completion_authority_transaction(self.run_dir,self.source,authority=dict(schema_version='autopilot-authority.v1',kind='session',ledger_root=str(self.root/'intent'),platform='codex',session_id='session-1'),preflight=lambda connection:None) as store:
                head=store.completion_recovery_head('report-unit')
                head['result']=dict(work_unit_id='report-unit',status='applied',unit_status='complete',reason='publication-incompatible')
                store.completion_set_recovery_head('report-unit',head)
            result=self.stop()
        unit=result['completion_recording']['units'][0]
        self.assertNotEqual(unit.get('unit_status'),'complete')
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']},{'unmet'})


class NativeGeneratedPublicationV2Test(_CompletionV2Fixture):
    def test_generated_publish_command_and_native_stop_apply_exact_original_proof(self):
        import os,shlex,subprocess
        receipt=self.prepare();self.proof()
        original=self.completion+'- evidence: Original checked output at '+self.verified_at+'\n'
        self.review(original)
        env={**os.environ,**self.source,'CODEX_THREAD_ID':'session-1'}
        command=shlex.split(receipt['publish_command'].replace('ORIGINAL_ISO_TIME',self.verified_at))
        published=subprocess.run(command,input=original,env=env,text=True,capture_output=True)
        self.assertEqual(published.returncode,0,published.stderr)
        proof=json.loads(published.stdout)
        self.assertEqual(proof['verified_at'],self.verified_at)
        self.assertEqual(proof['original_evidence'][0]['target_reference'],receipt['target_reference'])
        result=subprocess.run([sys.executable,str(publication_fixture.SKILL/'adapters/autopilot_mode.py')],input=json.dumps(dict(hook_event_name='Stop',session_id='session-1')),env=env,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        stopped=json.loads(result.stdout)
        self.assertEqual(stopped['completion_recording']['units'][0]['unit_status'],'complete')
        self.assertEqual(stopped['completion_recording']['units'][0]['original_evidence'],proof['original_evidence'])


class ParkedRetainedMetDependencyV2Test(_CompletionV2Fixture):
    partials=CompletionV2HistoryRegressionTest.partials

    def test_parked_new_partial_cannot_hide_changed_prior_met_dependency(self):
        from unittest import mock
        first,_=self.partials(apply_second=False)
        prior=next(c for c in self.read_state()['acceptance_criteria'] if c['id']=='report')
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            original=store.completion_read('evidence',first['evidence_references'][0])
        writer=publication_fixture.work_items.materialize_met_criteria_v2
        attempts=0
        def fail_first(*args,**kwargs):
            nonlocal attempts
            attempts+=1
            if attempts==1:raise OSError('B recording failure')
            return writer(*args,**kwargs)
        with mock.patch.object(publication_fixture.work_items,'materialize_met_criteria_v2',side_effect=fail_first) as apply:
            self.stop();parked=self.stop()
            self.assertEqual(apply.call_count,1)
            self.assertEqual(parked['recovery']['status'],'parked')
            self.assertEqual(next(c for c in self.read_state()['acceptance_criteria'] if c['id']=='report'),prior)
            self.report.write_text('changed A after B parked')
            changed=self.stop()
            self.assertEqual(apply.call_count,2)
            self.assertEqual({c['id']:c['status'] for c in self.read_state()['acceptance_criteria']},{'report':'unmet','protected':'met'})
            self.assertTrue(changed['recovery']['claims_changed'])
            self.assertNotIn('delivery_route',changed['recovery'])
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            retained=store.completion_read('evidence',first['evidence_references'][0])
        self.assertEqual(retained,original)
        events=self.core.read_session_events(root=self.root/'intent',platform='codex',session_id='session-1')
        invalidated=[e for e in events if e.get('event')=='acceptance-criterion-evidence-invalidated']
        self.assertEqual([e['criterion_id'] for e in invalidated],['report'])
        self.assertEqual(invalidated[0]['prior_completion_check_digest'].removeprefix('sha256:'),prior['met_completion_check_digest'].removeprefix('sha256:'))

    def test_parked_newer_core_proof_does_not_observe_old_historical_dependencies(self):
        from unittest import mock
        self.partials(apply_second=False)
        row=next(c for c in self.read_state()['acceptance_criteria'] if c['id']=='report')
        self.core.mark_acceptance_criterion_met(root=self.root/'intent',platform='codex',session_id='session-1',
            criterion_id='report',expected_input_event_id=self.read_state()['latest_input_event_id'],
            expected_criterion=row,completion_check_digest='b'*64)
        before=self.read_state()
        with mock.patch.object(publication_fixture.work_items,'materialize_met_criteria_v2',side_effect=OSError('B recording failure')) as apply:
            self.stop();parked=self.stop()
            self.assertEqual(parked['recovery']['status'],'parked')
            self.report.write_text('changed old historical proof A')
            unchanged=self.stop()
            self.assertEqual(unchanged['recovery']['status'],'parked')
            self.assertEqual(apply.call_count,0)
        self.assertEqual(self.read_state(),before)

    def test_changed_retained_dependency_stays_invalidated_when_current_writer_still_fails(self):
        from unittest import mock
        first,_=self.partials(apply_second=False)
        prior=next(c for c in self.read_state()['acceptance_criteria'] if c['id']=='report')
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            original=store.completion_read('evidence',first['evidence_references'][0])
        with mock.patch.object(publication_fixture.work_items,'materialize_met_criteria_v2',side_effect=OSError('B recording failure')) as apply:
            self.stop();self.stop()
            self.assertEqual(apply.call_count,1)
            self.report.write_text('changed A with B still failing')
            failed=self.stop()
            self.assertEqual(apply.call_count,2)
            self.assertEqual({c['id']:c['status'] for c in self.read_state()['acceptance_criteria']},{'report':'unmet','protected':'unmet'})
            self.assertNotEqual(failed['completion_recording']['units'][0].get('unit_status'),'complete')
            self.assertIn('B recording failure',failed['completion_recording']['units'][0]['detail'])
            self.assertEqual(self.stop()['recovery']['status'],'parked')
            self.assertEqual(apply.call_count,2)
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            self.assertEqual(store.completion_read('evidence',first['evidence_references'][0]),original)
        events=self.core.read_session_events(root=self.root/'intent',platform='codex',session_id='session-1')
        invalidated=[e for e in events if e.get('event')=='acceptance-criterion-evidence-invalidated']
        self.assertEqual([e['criterion_id'] for e in invalidated],['report'])
        self.assertEqual(invalidated[0]['prior_completion_check_digest'].removeprefix('sha256:'),prior['met_completion_check_digest'].removeprefix('sha256:'))

    def test_retained_mutation_recovery_preserves_independent_unit_and_proof(self):
        from unittest import mock
        _,second=self.partials(apply_second=False)
        rows=copy.deepcopy(self.read_state()['acceptance_criteria'])
        rows.append(dict(id='report',summary='Saved report matches input',source='user-explicit',
            admitted=True,status='unmet',work_unit_id='independent',contract_revision=1))
        self.core.record_turn(root=self.root/'intent',platform='codex',session_id='session-1',
            raw_user_input='Also complete independent output',intent_delta={'acceptance_criteria':rows},
            expected_input_event_id=self.read_state()['latest_input_event_id'])
        register_route(self.core,self.root/'intent')
        self.arguments['input_event_id']=self.read_state()['latest_input_event_id']
        receipts={r['work_unit_id']:r for r in self.prepare()['units']}
        self.publish(receipts['report-unit'],completion_check=None,verified_at=None,evidence_references=second['evidence_references'])
        self.proof()
        self.completion=self.completion.replace('  - protected: Protected file is unchanged [source: user-explicit]\n','')
        self.completion=self.completion.split('  - claim: Protected file is unchanged')[0]+'- unverified: none\n'
        independent_artifact=self.root/'independent';independent_artifact.write_text('independent original')
        published=self.publish(receipts['independent'],dependencies=[str(independent_artifact)])
        writer=publication_fixture.work_items.materialize_met_criteria_v2
        def fail_report(request,*args,**kwargs):
            if request['contract']['work_unit_id']=='report-unit':raise OSError('B recording failure')
            return writer(request,*args,**kwargs)
        with mock.patch.object(publication_fixture.work_items,'materialize_met_criteria_v2',side_effect=fail_report) as apply:
            self.stop();self.stop()
            prior=next(c for c in self.read_state()['acceptance_criteria'] if c['work_unit_id']=='independent')
            self.assertEqual(prior['status'],'met')
            with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
                original_unit=store.completion_unit('independent')
                original_evidence=store.completion_read('evidence',published['evidence_references'][0])
            before=apply.call_count
            self.report.write_text('A changed while independent proof stays stable')
            self.stop();self.stop()
            self.assertEqual(apply.call_count,before+1)
        self.assertEqual(next(c for c in self.read_state()['acceptance_criteria'] if c['work_unit_id']=='independent'),prior)
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            self.assertEqual(store.completion_unit('independent'),original_unit)
            self.assertEqual(store.completion_read('evidence',published['evidence_references'][0]),original_evidence)
        self.assertEqual(independent_artifact.read_text(),'independent original')

    def _assert_actual_stop_unreadable_current_preserves_retained_invalidation(self,partials):
        import os,subprocess
        first,_=partials()
        prior=next(c for c in self.read_state()['acceptance_criteria'] if c['id']=='report')
        prior_event_count=len(self.core.read_session_events(root=self.root/'intent',platform='codex',session_id='session-1'))
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            original=store.completion_read('evidence',first['evidence_references'][0])
            original_unit=store.completion_unit('report-unit')
        self.protected.chmod(0)
        self.addCleanup(self.protected.chmod,0o600)
        env={**os.environ,**self.source,'CODEX_THREAD_ID':'session-1','PYTHONDONTWRITEBYTECODE':'1'}
        command=[sys.executable,'-B',str(publication_fixture.SKILL/'adapters/autopilot_mode.py')]
        host=dict(hook_event_name='Stop',session_id='session-1',cwd=str(self.root))
        def invoke():
            result=subprocess.run(command,input=json.dumps(host),cwd=self.root,env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            return json.loads(result.stdout)
        first_failure=invoke();parked=invoke()
        self.assertEqual(parked['recovery']['status'],'parked')
        self.assertIn('Permission denied',first_failure['completion_recording']['units'][0]['detail'])
        self.assertEqual(next(c for c in self.read_state()['acceptance_criteria'] if c['id']=='report'),prior)
        self.report.write_text('A changed while B stays unreadable')
        changed=invoke();parked=invoke()
        self.assertEqual({c['id']:c['status'] for c in self.read_state()['acceptance_criteria']},{'report':'unmet','protected':'unmet'})
        self.assertEqual(parked['recovery']['status'],'parked')
        unit=changed['completion_recording']['units'][0]
        self.assertEqual(unit['status'],'recording-unavailable')
        self.assertNotEqual(unit.get('unit_status'),'complete')
        self.assertIn('Permission denied',unit['detail'])
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            self.assertEqual(store.completion_read('evidence',first['evidence_references'][0]),original)
            self.assertEqual(store.completion_unit('report-unit'),original_unit)
        events=self.core.read_session_events(root=self.root/'intent',platform='codex',session_id='session-1')
        invalidated=[e for e in events[prior_event_count:] if e.get('event')=='acceptance-criterion-evidence-invalidated']
        self.assertEqual([e['criterion_id'] for e in invalidated],['report'])
        self.assertEqual(invalidated[0]['prior_completion_check_digest'].removeprefix('sha256:'),prior['met_completion_check_digest'].removeprefix('sha256:'))

    def test_actual_stop_unreadable_current_publication_cannot_hide_changed_retained_proof(self):
        self._assert_actual_stop_unreadable_current_preserves_retained_invalidation(lambda:self.partials(apply_second=False))

    def test_actual_stop_unreadable_unselected_historical_dependency_does_not_block_retained_invalidation(self):
        def aggregate_partials():
            receipt,first=CompletionV2ReviewRegressionTest.aggregate(self)
            self.stop();self.protected.write_text('B changed after aggregate proof');self.stop()
            self.followup();receipt=self.prepare();self.proof()
            self.completion=self.completion.replace('  - report: Saved report matches input [source: user-explicit]\n','')
            start=self.completion.index('  - claim: Saved report matches input')
            end=self.completion.index('  - claim: Protected file is unchanged')
            self.completion=self.completion[:start]+self.completion[end:]
            second=self.publish(receipt,dependencies=[str(self.protected)])
            return first,second
        self._assert_actual_stop_unreadable_current_preserves_retained_invalidation(aggregate_partials)

    def test_new_input_alone_without_capture_keeps_existing_failure_parked(self):
        from unittest import mock
        import autopilot_completion_service as service
        self.partials(apply_second=False)
        with mock.patch.object(publication_fixture.work_items,'materialize_met_criteria_v2',side_effect=OSError('B recording failure')) as apply, \
                mock.patch.object(service,'_v2_recording_transaction',wraps=service._v2_recording_transaction) as transaction:
            self.stop();self.stop()
            attempts=transaction.call_count
            self.followup()
            self.assertEqual(self.stop()['recovery']['status'],'parked')
            self.assertEqual(apply.call_count,1)
            self.assertEqual(transaction.call_count,attempts)


class RetainedSiblingReadErrorV2Test(_CompletionV2Fixture):
    def test_transient_read_error_preserves_claims_and_recovers_original_proof(self):
        _,published=CompletionV2ReviewRegressionTest.aggregate(self)
        self.stop()
        before=self.read_state()['acceptance_criteria']
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            original_unit=store.completion_unit('report-unit')
            original_proof=store.completion_read('evidence',published['evidence_references'][0])
        host=dict(hook_event_name='Stop',session_id='session-1',cwd=str(self.root),
            host_context=dict(delivery_surface='visible_transcript'),prior_answer_ref='answer:retained')
        self.protected.chmod(0)
        self.addCleanup(self.protected.chmod,0o600)
        failure=publication_fixture.adapter.adapter_payload_from_env(self.source,hook_input=host)
        parked=publication_fixture.adapter.adapter_payload_from_env(self.source,hook_input=host)
        self.assertFalse(failure['recovery']['claims_changed'])
        self.assertNotIn('delivery_route',failure['recovery'])
        self.assertEqual(parked['recovery']['status'],'parked')
        self.assertEqual(self.read_state()['acceptance_criteria'],before)
        self.protected.chmod(0o600)
        recovered=publication_fixture.adapter.adapter_payload_from_env(self.source,hook_input=host)
        self.assertEqual(recovered['systemMessage'],'')
        self.assertEqual(recovered['completion_recording']['units'][0]['unit_status'],'complete')
        self.assertEqual(self.read_state()['acceptance_criteria'],before)
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            self.assertEqual(store.completion_unit('report-unit'),original_unit)
            self.assertEqual(store.completion_read('evidence',published['evidence_references'][0]),original_proof)

    def assert_sibling_error_isolation(self,*,aggregate,reverse):
        import os,subprocess
        if reverse:
            rows=copy.deepcopy(self.read_state()['acceptance_criteria'])[::-1]
            self.root=self.root/'reversed-order';self.root.mkdir()
            self.run_dir=self.root/'.autopilot/sessions/codex/session-1'
            self.source.update(GHOST_ALICE_SESSION_INTENT_ROOT=str(self.root/'intent'),
                GHOST_ALICE_AUTOPILOT_CWD=str(self.root),PWD=str(self.root))
            self.arguments.update(intent_root=self.root/'intent',source=self.source)
            self.state_path=publication_fixture.native_ledger(self.root/'intent',self.core,criteria=rows)
        self.assertEqual([c['id'] for c in self.read_state()['acceptance_criteria']],
            ['protected','report'] if reverse else ['report','protected'])
        if aggregate:
            _,published=CompletionV2ReviewRegressionTest.aggregate(self)
            originals=[published]
            self.stop()
        else:
            originals=list(CompletionV2HistoryRegressionTest.partials(self))
        before=self.read_state()
        self.assertEqual({c['status'] for c in before['acceptance_criteria']},{'met'})
        report=next(c for c in before['acceptance_criteria'] if c['id']=='report')
        protected=next(c for c in before['acceptance_criteria'] if c['id']=='protected')
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            records={ref:store.completion_read('evidence',ref) for publication in originals for ref in publication['evidence_references']}
            original_unit=store.completion_unit('report-unit')
        self.protected.chmod(0)
        self.addCleanup(self.protected.chmod,0o600)
        env={**os.environ,**self.source,'CODEX_THREAD_ID':'session-1','PYTHONDONTWRITEBYTECODE':'1'}
        command=[sys.executable,'-B',str(publication_fixture.SKILL/'adapters/autopilot_mode.py')]
        host=dict(hook_event_name='Stop',session_id='session-1',cwd=str(self.root))
        def invoke():
            result=subprocess.run(command,input=json.dumps(host),cwd=self.root,env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            return json.loads(result.stdout)
        failure=invoke();parked=invoke()
        self.assertEqual(parked['recovery']['status'],'parked')
        self.assertIn('Permission denied',failure['completion_recording']['units'][0]['detail'])
        self.assertEqual(self.read_state()['acceptance_criteria'],before['acceptance_criteria'])
        self.report.write_text('A changed while retained met B stays unreadable')
        changed=invoke();parked=invoke()
        after=self.read_state()
        self.assertEqual({c['id']:c['status'] for c in after['acceptance_criteria']},{'report':'unmet','protected':'met'})
        self.assertEqual(next(c for c in after['acceptance_criteria'] if c['id']=='protected'),protected)
        self.assertEqual(parked['recovery']['status'],'parked')
        self.assertEqual(changed['completion_recording']['units'][0]['status'],'recording-unavailable')
        self.assertNotEqual(changed['completion_recording']['units'][0].get('unit_status'),'complete')
        self.assertIn('Permission denied',changed['completion_recording']['units'][0]['detail'])
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            self.assertEqual(store.completion_unit('report-unit'),original_unit)
            for ref,original in records.items():self.assertEqual(store.completion_read('evidence',ref),original)
        events=self.core.read_session_events(root=self.root/'intent',platform='codex',session_id='session-1')
        invalidations=[e for e in events if e.get('event')=='acceptance-criterion-evidence-invalidated']
        self.assertEqual([e['criterion_id'] for e in invalidations],['report'])
        self.assertEqual(invalidations[0]['prior_completion_check_digest'].removeprefix('sha256:'),report['met_completion_check_digest'].removeprefix('sha256:'))
        self.followup()
        self.assertEqual(invoke()['recovery']['status'],'parked')
        self.assertEqual(self.read_state()['acceptance_criteria'],after['acceptance_criteria'])

    def test_aggregate_retained_read_error_preserves_sibling_invalidation(self):
        self.assert_sibling_error_isolation(aggregate=True,reverse=False)

    def test_aggregate_retained_read_error_preserves_sibling_invalidation_reversed(self):
        self.assert_sibling_error_isolation(aggregate=True,reverse=True)

    def test_separate_retained_read_error_preserves_sibling_invalidation(self):
        self.assert_sibling_error_isolation(aggregate=False,reverse=False)

    def test_separate_retained_read_error_preserves_sibling_invalidation_reversed(self):
        self.assert_sibling_error_isolation(aggregate=False,reverse=True)


    def test_valid_current_A_request_does_not_hide_unavailable_retained_B(self):
        import os,subprocess
        self.report=self.root/'latest-A';self.protected=self.root/'prior-B'
        self.report.write_text('A');self.protected.write_text('B')
        receipt=self.prepare();self.proof()
        self.completion=self.completion.replace('  - report: Saved report matches input [source: user-explicit]\n','')
        start=self.completion.index('  - claim: Saved report matches input')
        end=self.completion.index('  - claim: Protected file is unchanged')
        self.completion=self.completion[:start]+self.completion[end:]
        first=self.publish(receipt,dependencies=[str(self.protected)]);self.stop()
        self.followup();receipt=self.prepare();self.proof()
        self.completion=self.completion.replace('  - protected: Protected file is unchanged [source: user-explicit]\n','')
        self.completion=self.completion.split('  - claim: Protected file is unchanged')[0]+'- unverified: none\n'
        second=self.publish(receipt,dependencies=[str(self.report)]);self.stop()
        before=self.read_state()
        self.assertEqual({c['status'] for c in before['acceptance_criteria']},{'met'})
        self.protected.chmod(0);self.addCleanup(self.protected.chmod,0o600)
        command=[sys.executable,'-B',str(publication_fixture.SKILL/'adapters/autopilot_mode.py')]
        env={**os.environ,**self.source,'CODEX_THREAD_ID':'session-1','PYTHONDONTWRITEBYTECODE':'1'}
        def invoke():
            result=subprocess.run(command,input=json.dumps(dict(hook_event_name='Stop',session_id='session-1',cwd=str(self.root))),
                cwd=self.root,env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            return json.loads(result.stdout)
        unavailable=invoke()['completion_recording']['units'][0]
        self.assertEqual(unavailable['status'],'applied')
        self.assertEqual(unavailable['unit_status'],'pending')
        self.assertEqual(unavailable['reason'],'retained-dependency-unavailable')
        self.assertEqual([e['criterion_id'] for e in unavailable['retained_criterion_errors']],['protected'])
        self.assertIn('Permission denied',unavailable['detail'])
        self.assertEqual(invoke()['recovery']['status'],'parked')
        self.report.write_text('A changed with only valid-current-A request')
        changed=invoke()['completion_recording']['units'][0]
        self.assertEqual(changed['unit_status'],'pending')
        self.assertEqual(changed['reason'],'retained-dependency-unavailable')
        self.assertEqual({c['id']:c['status'] for c in self.read_state()['acceptance_criteria']},{'report':'unmet','protected':'met'})
        self.assertEqual(invoke()['recovery']['status'],'parked')
        self.assertEqual(next(c for c in self.read_state()['acceptance_criteria'] if c['id']=='protected'),
            next(c for c in before['acceptance_criteria'] if c['id']=='protected'))
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            for publication in (first,second):
                record=store.completion_read('evidence',publication['evidence_references'][0])
                self.assertEqual(record['verified_at'],publication['verified_at'])
                self.assertEqual(record['proof_locator'],publication['original_evidence'][0]['proof_locator'])


    def test_newer_foreign_met_proof_does_not_erase_supported_sibling_invalidation(self):
        CompletionV2ReviewRegressionTest.aggregate(self);self.stop()
        protected=next(c for c in self.read_state()['acceptance_criteria'] if c['id']=='protected')
        self.core.mark_acceptance_criterion_met(root=self.root/'intent',platform='codex',session_id='session-1',
            criterion_id='protected',expected_input_event_id=self.read_state()['latest_input_event_id'],
            expected_criterion=protected,completion_check_digest='b'*64)
        before=self.read_state();self.report.write_text('A changed with newer foreign B proof')
        failed=self.stop()
        after=self.read_state()
        self.assertEqual({c['id']:c['status'] for c in after['acceptance_criteria']},{'report':'unmet','protected':'met'})
        self.assertEqual(next(c for c in after['acceptance_criteria'] if c['id']=='protected'),
            next(c for c in before['acceptance_criteria'] if c['id']=='protected'))
        self.assertEqual(failed['completion_recording']['units'][0]['reason'],'met-evidence-incompatible')
        self.assertEqual(self.stop()['recovery']['status'],'parked')

    def test_tampered_shared_provenance_blocks_all_partial_invalidation(self):
        _,published=CompletionV2ReviewRegressionTest.aggregate(self);self.stop()
        before=self.read_state();self.report.write_text('A changed with tampered shared provenance')
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            ref=published['evidence_references'][0];record=store.completion_read('evidence',ref)
            record['verified_at']=datetime.now(timezone.utc).isoformat()
            changed=store.connection.execute('UPDATE ap_completion_v2_records SET body_json=? WHERE run_key=? AND kind=? AND identity=?',
                (json.dumps(record),store.key,'evidence',ref))
            self.assertEqual(changed.rowcount,1)
        failed=self.stop()['completion_recording']['units'][0]
        self.assertEqual(failed['status'],'recording-unavailable')
        self.assertNotEqual(failed.get('unit_status'),'complete')
        self.assertEqual(self.read_state()['acceptance_criteria'],before['acceptance_criteria'])
        self.assertFalse(any(e.get('event')=='acceptance-criterion-evidence-invalidated'
            for e in self.core.read_session_events(root=self.root/'intent',platform='codex',session_id='session-1')))
