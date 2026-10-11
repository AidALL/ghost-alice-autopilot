"""Native publication keeps explicit dependencies of accepted pending proof."""
import test_completion_contract_v2 as v2_fixture
import test_completion_publication as fixture


class NativeRetainedDependenciesTest(v2_fixture._CompletionV2Fixture):
    reviewed_answer = v2_fixture.PreparedPretoolV2Test.reviewed_answer

    def pending_original(self):
        receipt = self.prepare()
        self.reviewed_answer()
        artifact = self.root / 'business-report.txt'
        artifact.write_text('verified report\n')
        mapping = [dict(work_unit_id='report-unit', contract_revision=1,
            criterion_id=criterion, locators=[str(artifact)] if criterion == 'report' else [])
            for criterion in ('report', 'protected')]
        accepted = self.helper.publish_completion(receipt_token=receipt['receipt_token'],
            completion_check=self.completion, verified_at=self.verified_at,
            source=self.source, schema_version='v2', criterion_dependencies=mapping)
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']}, {'unmet'})
        self.followup()
        self.prepare()
        return artifact, accepted

    def native_stop(self):
        return fixture.adapter.adapter_payload_from_env(self.source, hook_input=dict(
            hook_event_name='Stop', session_id='session-1', last_assistant_message=self.completion))

    def test_changed_pending_dependency_cannot_be_dropped_by_native_publication(self):
        artifact, accepted = self.pending_original()
        artifact.write_text('changed after the original verification\n')
        result = self.native_stop()
        rows = {row['id']: row['status'] for row in self.read_state()['acceptance_criteria']}
        self.assertEqual(rows, {'report': 'unmet', 'protected': 'met'}, result)
        evidence = result['completion_recording']['units'][0]['original_evidence']
        self.assertEqual(evidence, accepted['original_evidence'])

    def test_unchanged_pending_proof_retains_original_explicit_dependency_mapping(self):
        _, accepted = self.pending_original()
        result = self.native_stop()
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']}, {'met'}, result)
        self.assertEqual(result['completion_recording']['units'][0]['original_evidence'],
            accepted['original_evidence'])
