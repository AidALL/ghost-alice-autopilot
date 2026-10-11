"""AP transports evidence and consumes Core's compatibility decisions."""
from types import SimpleNamespace
from unittest import mock

from test_completion_contract_v2 import _CompletionV2Fixture


class CompletionPolicyBoundaryTest(_CompletionV2Fixture):
    def accepted(self):
        self.artifact = self.root / 'checked.txt'
        self.artifact.write_text('original')
        receipt = self.prepare()
        self.proof()
        self.publication = self.publish(receipt, dependencies=[str(self.artifact)])
        self.stop()

    def test_reference_coverage_is_core_result_without_ap_reassessment(self):
        import autopilot_completion_contract as contract
        self.accepted()
        with self.helper.storage.transaction(self.run_dir, self.source, migrate_legacy=False) as store:
            reference = self.publication['evidence_references'][0]
            record = store.completion_read('evidence', reference)
            origin = store.completion_read('origin', record['origin_token'])
        core_coverage = {('report-unit', 1, 'report'): reference}
        policy = SimpleNamespace(assess_reference_coverage=mock.Mock(return_value=core_coverage))
        with mock.patch.object(contract, '_core_evidence_policy', create=True, return_value=policy), \
                mock.patch.object(contract, 'compatible_definition', side_effect=AssertionError('AP cannot repeat Core compatibility')):
            result = contract.validate_references({reference: record}, {record['origin_token']: origin},
                record['contract'], record['target'],
                {d['locator']: d['digest'] for d in record['dependencies']}, self.verified_at, source=self.source)
        self.assertEqual(result, core_coverage)
        policy.assess_reference_coverage.assert_called_once()

    def test_retained_invalidation_consumes_core_dependency_assessment(self):
        import autopilot_completion_service as service
        self.accepted()
        def assessed(record, reference, criterion_id):
            return dict(changed=criterion_id == 'report', error=None,
                observations=tuple(dict(evidence_reference=reference, criterion_id=criterion_id,
                    locator=d['locator'], observed='sha256:'+'0'*64) for d in record['dependencies']))
        with mock.patch.object(service, '_core_dependency_assessment', create=True, side_effect=assessed) as core:
            self.stop()
        self.assertGreater(core.call_count, 0)
        self.assertEqual({c['id']: c['status'] for c in self.read_state()['acceptance_criteria']},
                         {'report': 'unmet', 'protected': 'met'})
        self.assertEqual(self.artifact.read_text(), 'original')

    def test_v2_publication_consumes_authenticated_core_proof_without_queue_parser(self):
        import autopilot_completion_service as service
        from test_completion_contract_v2 import PreparedPretoolV2Test
        receipt = self.prepare()
        PreparedPretoolV2Test.reviewed_answer(self)
        with mock.patch.object(service, '_validate_continue_next_evidence',
                side_effect=AssertionError('Queue grammar must not rejudge authenticated v2 proof')), \
                mock.patch.object(service, '_parse_completion_evidence',
                side_effect=AssertionError('Core owns authenticated v2 coverage')):
            published = self.helper.publish_completion(receipt_token=receipt['receipt_token'],
                completion_check=self.completion, verified_at=self.verified_at,
                source=self.source, schema_version='v2')
        self.assertEqual(published['status'], 'pending-adapter')
        self.assertEqual(published['verified_at'], self.verified_at)
        self.assertEqual({c['id'] for c in published['original_evidence'][0]['criteria']},
            {'report', 'protected'})

    def test_v2_partial_archive_reuses_core_definitions_without_partial_reparse(self):
        from test_partial_completion_evidence import PartialEvidenceV2Test
        import autopilot_work_items
        import test_completion_publication as fixture
        skills = self.root / 'skills'
        skills.mkdir()
        (skills / 'autopilot-mode').symlink_to(fixture.SKILL, target_is_directory=True)
        self.source['GHOST_ALICE_SKILLS_DIR'] = str(skills)
        receipt = PartialEvidenceV2Test.prepare_partial(self)
        self.review()
        with mock.patch.object(autopilot_work_items, '_parse_completion_evidence',
                side_effect=AssertionError('Authenticated archive coverage belongs to Core')):
            published = self.publish(receipt)
        self.assertEqual([c['id'] for c in published['original_evidence'][0]['criteria']], ['report'])
