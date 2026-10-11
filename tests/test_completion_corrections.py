"""A recording correction appends history and changes only affected proof."""
import copy
import unittest
from unittest import mock
from test_completion_contract_v2 import _CompletionV2Fixture
import autopilot_completion_contract as contract
import test_completion_publication as fixture


class CompletionCorrectionTest(_CompletionV2Fixture):
    def setUp(self):
        super().setUp()
        skills=self.root/'managed-skills'
        provider=skills/'autopilot-mode/adapters/autopilot_claim_evidence.py'
        provider.parent.mkdir(parents=True)
        provider.symlink_to(fixture.SKILL/'adapters/autopilot_claim_evidence.py')
        self.source['GHOST_ALICE_SKILLS_DIR']=str(skills)

    def archived(self):
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            state=store.completion_unit('report-unit')
            reference=state['publication_reference']
            return copy.deepcopy(state),reference,store.completion_read('publication',reference)

    def publish(self, receipt, **changes):
        correction=changes.get('correction')
        if correction is not None:
            with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
                try:original=store.completion_read('publication',correction['supersedes'])
                except FileNotFoundError:original=None
                evidence=None
                while original is not None:
                    wanted=correction['affected_criteria'][0]['criterion_id']
                    matching=[c['evidence_reference'] for c in original['request']['selected_criteria'] if c['criterion_id']==wanted]
                    if matching:
                        evidence=matching[0];break
                    prior=original['request'].get('correction')
                    original=store.completion_read('publication',prior['supersedes']) if prior else None
            self.review(changes.get('completion_check',self.completion),changes.get('verified_at',self.verified_at),
                getattr(self,'review_verdicts',None),reuse_evidence=evidence)
        return super().publish(receipt,**changes)

    def correction_receipt(self, original):
        try:
            return self.helper.prepare_completion(**self.arguments,schema_version='v2',correction_of=original)
        except TypeError as exc:
            self.fail('Current authorized met criteria require correction preparation: '+str(exc))

    def correction(self, original, affected='report'):
        return dict(supersedes=original,affected_criteria=[dict(work_unit_id='report-unit',contract_revision=1,criterion_id=affected)],
            reason='The original claim-to-criterion mapping was wrong')

    def test_applied_correction_appends_history_and_preserves_unaffected_proof(self):
        receipt=self.prepare(); self.proof(); first=self.publish(receipt); self.stop()
        before=self.read_state()['acceptance_criteria']; state,ref,publication=self.archived()
        self.assertEqual(first.get('publication_reference'),ref)
        current=self.correction_receipt(ref)
        self.completion=self.completion.replace('Saved report matches input\n    criterion:', 'The corrected report matches input\n    criterion:')
        result=self.publish(current,correction=self.correction(ref))
        _,newref,newpublication=self.archived()
        self.assertNotEqual(newref,ref)
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            self.assertEqual(store.completion_read('publication',ref),publication)
            self.assertEqual(store.completion_read('evidence',first['evidence_references'][0])['verified_at'],self.verified_at)
        self.assertEqual(result['verified_at'],self.verified_at)
        self.assertEqual(self.publish(current,correction=self.correction(ref)),result)
        stopped=self.stop()
        self.assertEqual(stopped['completion_recording']['units'][0]['unit_status'],'complete')
        after=self.read_state()['acceptance_criteria']
        self.assertEqual(next(c for c in before if c['id']=='protected'),next(c for c in after if c['id']=='protected'))
        self.assertNotEqual(next(c for c in before if c['id']=='report')['met_completion_check_digest'],
            next(c for c in after if c['id']=='report')['met_completion_check_digest'])

    def test_pending_predecessor_cannot_apply_after_correction(self):
        receipt=self.prepare(); self.proof(); self.publish(receipt)
        _,ref,_=self.archived(); current=self.correction_receipt(ref)
        self.completion=self.completion.replace('Saved report matches input\n    criterion:', 'Corrected report claim\n    criterion:')
        self.publish(current,correction=self.correction(ref))
        _,replacement,_=self.archived()
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            state=store.completion_unit('report-unit')
            store.completion_set_unit(dict(state,publication_reference=ref))
        stopped=self.stop()
        self.assertEqual(stopped['completion_recording']['units'][0]['status'],'recording-unavailable')
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']},{'unmet'})

    def test_new_input_uses_current_correction_authority_and_original_time(self):
        receipt=self.prepare(); self.proof(); self.publish(receipt); self.stop()
        _,ref,_=self.archived(); self.followup(); current=self.correction_receipt(ref)
        self.assertNotEqual(current['receipt_token'],receipt['receipt_token'])
        self.completion=self.completion.replace('Saved report matches input\n    criterion:', 'Corrected report claim after new input\n    criterion:')
        result=self.publish(current,correction=self.correction(ref))
        self.assertEqual(result['verified_at'],self.verified_at)
        self.assertEqual(self.stop()['completion_recording']['units'][0]['unit_status'],'complete')

    def test_correction_chain_can_repair_another_original_criterion(self):
        receipt=self.prepare(); self.proof(); self.publish(receipt); self.stop()
        _,reference,_=self.archived(); current=self.correction_receipt(reference)
        self.completion=self.completion.replace('Saved report matches input\n    criterion:', 'Corrected report claim\n    criterion:')
        self.publish(current,correction=self.correction(reference)); self.stop()
        report=next(c for c in self.read_state()['acceptance_criteria'] if c['id']=='report')
        _,reference,_=self.archived(); current=self.correction_receipt(reference)
        self.completion=self.completion.replace('Protected file is unchanged\n    criterion:', 'Corrected protected claim\n    criterion:')
        self.publish(current,correction=self.correction(reference,'protected'))
        self.assertEqual(self.stop()['completion_recording']['units'][0]['unit_status'],'complete')
        self.assertEqual(report,next(c for c in self.read_state()['acceptance_criteria'] if c['id']=='report'))

    def test_foreign_or_stale_predecessor_is_rejected(self):
        receipt=self.prepare(); self.proof(); self.publish(receipt)
        _,ref,_=self.archived(); current=self.correction_receipt(ref)
        with self.assertRaises(ValueError):
            self.publish(current,correction=self.correction('sha256:'+'0'*64))
        self.followup()
        with self.assertRaises(ValueError):self.publish(current,correction=self.correction(ref))

    def test_legacy_applied_history_cannot_assert_current_supported_coverage(self):
        import autopilot_completion_service as service
        reference,original=self.seed_legacy_wrong_mapping(applied=True)
        before=copy.deepcopy(self.read_state()['acceptance_criteria'])
        result=service.apply_completion_v2(self.source,apply_existing_only=True)['completion_recording']['units'][0]
        self.assertEqual(result['unit_status'],'pending')
        self.assertEqual(result['selected_criteria'],[])
        self.assertEqual({r['reason'] for r in result['retained_coverage_errors']},{'assessment-required'})
        self.assertEqual(self.read_state()['acceptance_criteria'],before)
        # The same authenticated history remains usable by the correction path.
        self.assertEqual(self.correction_receipt(reference)['status'],'prepared')
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            self.assertEqual(store.completion_read('publication',reference),original)

    def test_new_core_review_releases_only_supported_retained_coverage(self):
        import autopilot_completion_service as service
        self.seed_legacy_wrong_mapping(applied=True)
        before=copy.deepcopy(self.read_state()['acceptance_criteria'])
        first=service.apply_completion_v2(self.source,apply_existing_only=True)
        self.assertEqual(first['completion_recording']['units'][0]['unit_status'],'pending')
        self.review(verdicts={'report':'unsupported'})
        result=service.apply_completion_v2(self.source,apply_existing_only=True)['completion_recording']['units'][0]
        self.assertEqual(result['unit_status'],'pending')
        self.assertEqual([c['criterion_id'] for c in result['selected_criteria']],['protected'])
        self.assertEqual([r['reason'] for r in result['retained_coverage_errors']],['retained-proof-unsupported'])
        self.assertEqual(self.read_state()['acceptance_criteria'],before)

    def seed_legacy_wrong_mapping(self, *, applied, report_only=False):
        """Authentic v2 archive from before Core semantic assessment existed."""
        import autopilot_work_items
        from datetime import datetime, timezone
        receipt=self.prepare(); self.proof()
        self.completion=self.completion.replace('Saved report matches input','A build command exited successfully')
        if report_only:
            self.completion=self.completion.replace('  - protected: Protected file is unchanged [source: user-explicit]\n','')
            self.completion=self.completion[:self.completion.index('  - claim: Protected file')]+ '- unverified: none\n'
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            state=store.completion_unit('report-unit')
            captured=store.completion_read('receipt',receipt['receipt_token'])
            origin=store.completion_read('origin',captured['origin_token'])
            record=contract.evidence_record(origin=origin,origin_token=captured['origin_token'],proof=self.completion,
                proof_locator=self.evidence_source,verified_at=self.verified_at,
                criteria=[c for c in captured['contract']['criteria'] if not report_only or c['id']=='report'],
                dependencies=[],now=datetime.now(timezone.utc).isoformat())
            evidence=store.completion_put('evidence',record)
            request=contract.publication_request(binding=captured['binding'],contract=captured['contract'],target=captured['target'],
                coverage={contract.criterion_key(c):evidence for c in record['criteria']},records={evidence:record},
                created_at=captured['prepared_at'],checked_at=datetime.now(timezone.utc).isoformat())
            publication=dict(receipt_token=receipt['receipt_token'],request=request,
                result=dict(idempotency_key=request['idempotency_key'],original_evidence=[]))
            reference=store.completion_put('publication',publication)
            state=dict(state,status='pending-adapter',publication_reference=reference,
                publications={receipt['receipt_token']:reference})
            store.completion_set_unit(state)
            if applied:
                autopilot_work_items.materialize_met_criteria_v2(request,{evidence:record},self.source,store.connection)
                key=contract.digest([request['idempotency_key'],request['selected_criteria']])
                application=dict(schema_version='autopilot-completion-application.v2',publication_reference=reference,
                    binding=request['binding'],target=request['target'],selected_criteria=request['selected_criteria'],
                    evidence_references=[evidence],application_key=key,applied_at=datetime.now(timezone.utc).isoformat())
                appref=store.completion_put('application',application)
                store.completion_set_unit(dict(state,status='applied',applied_requests=[key],application_references={key:appref}))
        return reference,publication

    def _check_legacy_wrong_mapping(self, applied):
        reference,original=self.seed_legacy_wrong_mapping(applied=applied)
        before=self.read_state()['acceptance_criteria']
        current=self.correction_receipt(reference)
        self.completion=self.completion.replace('A build command exited successfully','Only the original build result is known')
        import completion_assessment
        evidence=original['request']['evidence_references'][0]
        with self.assertRaises(ValueError):
            completion_assessment.prepare_assessment(dict(self.source,GHOST_ALICE_SKILLS_DIR=str(self.root/'absent')),
                proof=self.completion,verified_at=self.verified_at,source_paths=[],reuse_evidence=evidence)
        reviewed=completion_assessment.prepare_assessment(self.source,proof=self.completion,
            verified_at=self.verified_at,source_paths=[],reuse_evidence=evidence)
        self.assertEqual(reviewed['sources'][0]['text'],self.completion.replace('Only the original build result is known',
            'A build command exited successfully'))
        completion_assessment.record_assessment(self.source,reviewed['candidate_reference'],[
            dict(binding_id=b['binding_id'],verdict='unsupported' if b['criterion']['id']=='report' else 'supported',
                source_ids=[reviewed['sources'][0]['source_id']],rationale='Original archived build proof does not establish canonical report condition')
            for b in reviewed['bindings']])
        self.review_verdicts={'report':'unsupported'}
        self.publish(current,correction=self.correction(reference))
        result=self.stop()['completion_recording']['units'][0]
        self.assertEqual(result['unit_status'],'pending')
        rows={r['id']:r for r in self.read_state()['acceptance_criteria']}
        self.assertEqual(rows['report']['status'],'unmet')
        self.assertEqual(rows['protected'],next(r for r in before if r['id']=='protected'))
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            self.assertEqual(store.completion_read('publication',reference),original)

    def test_legacy_accepted_wrong_mapping_stays_readable_during_correction(self):
        self._check_legacy_wrong_mapping(False)

    def test_legacy_applied_wrong_mapping_stays_readable_during_correction(self):
        self._check_legacy_wrong_mapping(True)

    def test_core_rejected_replacement_withdraws_only_affected_criterion(self):
        receipt=self.prepare(); self.proof(); self.publish(receipt); self.stop()
        before=self.read_state()['acceptance_criteria']; _,ref,original=self.archived()
        current=self.correction_receipt(ref)
        self.completion=self.completion.replace('Saved report matches input\n    criterion:', 'A report path exists\n    criterion:')
        self.review_verdicts={'report':'unsupported'}
        corrected=self.publish(current,correction=self.correction(ref))
        self.assertEqual(self.stop()['completion_recording']['units'][0]['unit_status'],'pending')
        rows={r['id']:r for r in self.read_state()['acceptance_criteria']}
        self.assertEqual(rows['report']['status'],'unmet')
        self.assertEqual(rows['protected'],next(r for r in before if r['id']=='protected'))
        with self.helper.storage.transaction(self.run_dir,self.source,migrate_legacy=False) as store:
            self.assertEqual(store.completion_read('publication',ref),original)
        self.assertEqual(self.stop()['completion_recording']['units'][0]['unit_status'],'pending')

    def test_core_rejects_id_only_canonical_row_laundering(self):
        receipt=self.prepare(); self.proof()
        self.completion=self.completion.replace('Saved report matches input', 'A path exists')
        self.review_verdicts={'report':'unsupported','protected':'unsupported'}
        with self.assertRaises(ValueError):self.publish(receipt)
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']},{'unmet'})

    def test_fresh_source_with_old_time_cannot_authorize_correction(self):
        receipt=self.prepare(); self.proof(); self.publish(receipt); self.stop()
        _,reference,_=self.archived(); current=self.correction_receipt(reference)
        self.completion=self.completion.replace('Saved report matches input\n    criterion:', 'Corrected report from fresh source\n    criterion:')
        self.review()
        before=self.read_state()['acceptance_criteria']
        with self.assertRaises(ValueError):
            self.helper.publish_completion(receipt_token=current['receipt_token'],completion_check=self.completion,
                verified_at=self.verified_at,evidence_source=self.evidence_source,source=self.source,schema_version='v2',
                correction=self.correction(reference))
        self.assertEqual(self.read_state()['acceptance_criteria'],before)

    def test_accepted_reference_reuses_archived_observations_after_log_deletion(self):
        receipt=self.prepare(); self.proof(); accepted=self.publish(receipt)
        (self.root/'synthetic-original-observations.txt').unlink()
        self.followup(); current=self.prepare()
        reused=self.helper.publish_completion(receipt_token=current['receipt_token'],completion_check=None,
            verified_at=None,evidence_source=None,source=self.source,schema_version='v2',
            evidence_references=accepted['evidence_references'])
        self.assertEqual(reused['evidence_references'],accepted['evidence_references'])
        self.assertEqual(reused['verified_at'],accepted['verified_at'])
        self.assertEqual(self.stop()['completion_recording']['units'][0]['unit_status'],'complete')
        self.assertFalse((self.root/'synthetic-original-observations.txt').exists())

    def test_unaccepted_fresh_review_still_rejects_changed_source(self):
        receipt=self.prepare(); self.proof()
        (self.root/'synthetic-original-observations.txt').write_text('Changed before first acceptance')
        with self.assertRaises(ValueError):
            self.helper.publish_completion(receipt_token=receipt['receipt_token'],completion_check=self.completion,
                verified_at=self.verified_at,evidence_source=self.evidence_source,source=self.source,schema_version='v2')
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']},{'unmet'})

    def test_correction_application_rolls_back_invalidation_with_writer_failure(self):
        import autopilot_work_items
        receipt=self.prepare(); self.proof(); self.publish(receipt); self.stop()
        _,ref,_=self.archived(); current=self.correction_receipt(ref)
        self.completion=self.completion.replace('Saved report matches input\n    criterion:', 'Corrected report claim\n    criterion:')
        self.publish(current,correction=self.correction(ref)); before=self.read_state()['acceptance_criteria']
        with mock.patch.object(autopilot_work_items,'materialize_met_criteria_v2',side_effect=ValueError('synthetic writer failure')):
            self.stop()
        self.assertEqual(self.read_state()['acceptance_criteria'],before)


if __name__=='__main__':unittest.main()


class RetainedObservationSnapshotTests(unittest.TestCase):
    def test_authenticated_correction_uses_original_bytes_after_log_change_or_deletion(self):
        for mutation in ('changed', 'deleted'):
            with self.subTest(mutation=mutation):
                owner = CompletionCorrectionTest()
                owner.setUp()
                try:
                    receipt = owner.prepare(); owner.proof(); owner.publish(receipt); owner.stop()
                    import completion_assessment
                    _, publication_reference, publication = owner.archived()
                    evidence_reference = publication['request']['evidence_references'][0]
                    with owner.helper.storage.transaction(owner.run_dir, owner.source, migrate_legacy=False) as store:
                        record = store.completion_read('evidence', evidence_reference)
                    original = completion_assessment.lookup_assessment(owner.source, record['contract'],
                        record['proof'], record['verified_at'])
                    log = owner.root / 'synthetic-original-observations.txt'
                    if mutation == 'changed': log.write_text('Later unrelated observation bytes')
                    else: log.unlink()
                    owner.correction_receipt(publication_reference)
                    proof = owner.completion.replace('Saved report matches input\n    criterion:',
                        'Only the original saved report observation is retained\n    criterion:')
                    candidate = completion_assessment.prepare_assessment(owner.source, proof=proof,
                        verified_at=record['verified_at'], source_paths=[], reuse_evidence=evidence_reference)
                    self.assertEqual(candidate['sources'], original.candidate['sources'])
                    self.assertEqual(candidate['verified_at'], record['verified_at'])
                    try:
                        completion_assessment.record_assessment(owner.source, candidate['candidate_reference'],
                            [dict(binding_id=b['binding_id'], verdict='supported',
                                source_ids=[s['source_id'] for s in candidate['sources']],
                                rationale='Retained original observation supports this historical statement.')
                             for b in candidate['bindings']])
                    except completion_assessment.CompletionAssessmentError as exc:
                        self.fail('Retained original bytes must remain reviewable: ' + str(exc))
                    reviewed = completion_assessment.lookup_assessment(owner.source, candidate['contract'], proof,
                        candidate['verified_at'])
                    self.assertEqual(reviewed.source_issues, {})
                    self.assertTrue(completion_assessment.authorize_correction(owner.source,
                        criterion=dict(work_unit_id='report-unit', contract_revision=1, criterion_id='report'),
                        contract=candidate['contract'], proof=proof, verified_at=candidate['verified_at'],
                        original_evidence_reference=evidence_reference))
                    if mutation == 'deleted': self.assertFalse(log.exists())
                    else: self.assertEqual(log.read_text(), 'Later unrelated observation bytes')
                finally:
                    owner.tearDown(); owner.doCleanups()
