"""Partial owning proof records only the supported criterion subset."""
import copy
import hashlib
import unittest
from datetime import datetime, timezone
from unittest import mock

from test_completion_contract_v2 import _CompletionV2Fixture
import test_completion_publication as fixture
import test_completion_continuation as continuation_tests
import autopilot_completion_contract as contract
from autopilot_work_items import _parse_completion_evidence, _validate_continue_next_evidence


class PartialEvidenceV2Test(_CompletionV2Fixture):
    def publish_cli(self, receipt, proof):
        import os, shlex, subprocess
        command=shlex.split(receipt['publish_command'].replace('ORIGINAL_ISO_TIME',self.verified_at))
        return subprocess.run(command,input=proof,text=True,capture_output=True,
            env={**os.environ,**self.source,'CODEX_THREAD_ID':'session-1'})

    def test_partial_cli_derives_original_top_level_evidence_without_override(self):
        import json
        receipt=self.prepare_partial()
        original=f'{self.evidence_source} at {self.verified_at}; original report observation'
        self.completion+='- evidence: '+original+'\n'
        self.review()
        result=self.publish_cli(receipt,self.completion)
        self.assertEqual(result.returncode,0,result.stderr)
        published=json.loads(result.stdout)
        self.assertEqual(published['original_evidence'][0]['proof_locator'],original)
        self.assertEqual(published['verified_at'],self.verified_at)
        self.assertEqual([c['id'] for c in published['original_evidence'][0]['criteria']],['report'])
        self.assertEqual(self.stop()['completion_recording']['units'][0]['unit_status'],'pending')

    def test_partial_cli_rejects_malformed_duplicate_or_missing_original_evidence(self):
        receipt=self.prepare_partial()
        partial=self.completion+'- evidence: '+self.evidence_source+'\n'
        proofs=[partial.replace('scope: partial','scope: full'),partial+partial,
            partial+'[completion-check]\n',partial+'- evidence: second source\n',
            self.completion,partial.replace('- evidence: '+self.evidence_source,'- evidence: TODO')]
        for proof in proofs:
            with self.subTest(proof=proof[-100:]):
                self.review(proof)
                result=self.publish_cli(receipt,proof)
                self.assertNotEqual(result.returncode,0,result.stdout)
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']},{'unmet'})

    def prepare_partial(self):
        rows = copy.deepcopy(self.read_state()['acceptance_criteria'])
        rows.extend(dict(rows[0], id=key, summary=key) for key in ('pending', 'uncovered'))
        self.change_intent({'acceptance_criteria': rows})
        receipt = self.prepare()
        self.proof()
        self.completion = self.completion.replace('[completion-check]', '[partial-check]\n- scope: partial')
        self.completion = self.completion.replace('- claim-evidence-map:',
            '  - pending: Pending condition [source: user-explicit]\n- claim-evidence-map:')
        self.completion = self.completion.replace('- unverified: none',
            '  - claim: Contrary protected check\n    criterion: protected\n'
            f'    evidence: {self.evidence_source}; original bytes differ\n    verdict: fail\n'
            '- unverified:\n  - pending: Check unavailable')
        return receipt

    def test_real_publish_apply_records_only_passed_subset_without_retry_loop(self):
        receipt = self.prepare_partial()
        published = self.publish(receipt)
        self.assertEqual([c['id'] for c in published['original_evidence'][0]['criteria']], ['report'])
        original_time = self.verified_at
        with mock.patch.object(fixture.work_items, 'materialize_met_criteria_v2',
                wraps=fixture.work_items.materialize_met_criteria_v2) as writer:
            first = self.stop()
            second = self.stop()
            self.assertEqual(writer.call_count, 1)
        for output in (first, second):
            unit = output['completion_recording']['units'][0]
            self.assertEqual(unit['status'], 'applied')
            self.assertEqual(unit['unit_status'], 'pending')
            self.assertEqual({c['criterion_id'] for c in unit['remaining_criteria']},
                {'protected', 'pending', 'uncovered'})
            self.assertNotIn('Publish existing supported proof', output['systemMessage'])
        self.assertEqual({c['id']: c['status'] for c in self.read_state()['acceptance_criteria']},
            dict(report='met', protected='unmet', pending='unmet', uncovered='unmet'))
        self.assertEqual(self.publish(receipt), published)
        self.assertEqual(published['verified_at'], original_time)

    def test_native_partial_response_applies_subset_and_preserves_pending_status(self):
        from response_route_fixture import register_route
        receipt = self.prepare_partial()
        published = self.publish(receipt)
        register_route(self.core, self.root / 'intent', purpose='partial')
        host = dict(hook_event_name='Stop', session_id='session-1', cwd=str(self.root),
            last_assistant_message=self.completion + '\nThe report check passed. The remaining checks are unresolved.')
        self.review(host['last_assistant_message'], reuse_evidence=published['evidence_references'][0])
        for _ in range(2):
            result = fixture.adapter.adapter_payload_from_env(self.source, hook_input=host)
            self.assertIn('units', result['completion_recording'], result)
            unit = result['completion_recording']['units'][0]
            self.assertEqual(unit['unit_status'], 'pending')
            self.assertEqual([row['criterion_id'] for row in unit['selected_criteria']], ['report'])
            self.assertEqual(result['systemMessage'], '')
            self.assertEqual(unit['original_evidence'], published['original_evidence'])

    def test_partial_proof_preserves_time_and_dependency_guards(self):
        from datetime import timedelta
        receipt = self.prepare_partial()
        with self.assertRaises(ValueError):
            self.helper.publish_completion(receipt_token=receipt['receipt_token'], completion_check=self.completion,
                evidence_source=self.evidence_source, source=self.source, schema_version='v2',
                verified_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat())
        artifact = self.root / 'report.txt'
        artifact.write_text('original')
        published = self.publish(receipt, dependencies=[str(artifact)])
        self.stop()
        artifact.write_text('changed')
        self.stop()
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']}, {'unmet'})
        self.followup()
        current = self.prepare()
        with self.assertRaises(ValueError):
            self.publish(current, completion_check=None, verified_at=None,
                evidence_references=published['evidence_references'])

    def test_no_supported_criterion_cannot_publish_or_mark_anything_met(self):
        receipt = self.prepare_partial()
        with self.assertRaises(ValueError):
            self.publish(receipt, completion_check=self.completion.replace('verdict: pass', 'verdict: fail'))
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']}, {'unmet'})

    def test_explicit_unverified_excludes_even_all_pass_claims(self):
        receipt = self.prepare_partial()
        self.completion = self.completion.replace('    verdict: fail', '    verdict: pass')
        self.completion += '  - protected: A required check remains unavailable\n'
        published = self.publish(receipt)
        self.assertEqual([c['id'] for c in published['original_evidence'][0]['criteria']], ['report'])

    def test_partial_grammar_and_outside_ids_fail_closed(self):
        receipt = self.prepare_partial()
        for old, new in [('scope: partial', 'scope: full'),
                         ('pending: Check unavailable', 'some condition still needs checking'),
                         ('pending: Check unavailable', 'unknown: Check unavailable'),
                         ('pending: Check unavailable', 'pending:'),
                         ('pending: Check unavailable', 'none'),
                         ('criterion: report', 'criterion: Saved report matches input'),
                         ('verdict: fail', 'verdict: fail because mismatch'),
                         ('  - pending: Check unavailable', '')]:
            with self.subTest(new=new), self.assertRaises(ValueError):
                self.publish(receipt, completion_check=self.completion.replace(old, new))
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']}, {'unmet'})

    def test_partial_never_enables_legacy_whole_item_continuation(self):
        self.prepare_partial()
        digest = 'sha256:' + hashlib.sha256(self.completion.encode()).hexdigest()
        with self.assertRaises(ValueError):
            _validate_continue_next_evidence(digest, [self.completion])
        with self.assertRaises(ValueError):
            _parse_completion_evidence(self.completion)
        with self.assertRaises(ValueError):
            self.helper._original_evidence_source(self.completion+'- evidence: '+self.evidence_source+'\n')

    def test_contract_rejects_forged_coverage_of_failed_or_unverified_proof(self):
        receipt = self.prepare_partial()
        published = self.publish(receipt)
        with self.helper.storage.transaction(self.run_dir, self.source, migrate_legacy=False) as store:
            record = store.completion_read('evidence', published['evidence_references'][0])
            origin = store.completion_read('origin', record['origin_token'])
        for criterion in ('protected', 'pending', 'uncovered'):
            forged = copy.deepcopy(record)
            row = next(c for c in record['contract']['criteria'] if c['id'] == criterion)
            forged['criteria'].append(contract.definition(row))
            forged['criterion_dependencies'] = contract.dependency_mapping(forged['criteria'], [])
            with self.subTest(criterion=criterion), self.assertRaises(ValueError):
                contract.validate_evidence(forged, contract.digest(forged), origin,
                    datetime.now(timezone.utc).isoformat())



class ZeroCoverageResponseTest(_CompletionV2Fixture):
    feedback = continuation_tests.CompletionContinuationTest.feedback

    def test_zero_coverage_report_applies_existing_publication_without_a_plan(self):
        from test_completion_continuation import CompletionContinuationTest
        CompletionContinuationTest.partial(self)  # report proof accepted, not yet applied
        self.proof()
        self.completion = self.completion.replace('[completion-check]', '[partial-check]\n- scope: partial')
        self.completion = self.completion.replace('- unverified: none',
            '- unverified:\n  - report: Current report does not close this criterion\n  - protected: Remaining checks unavailable')
        self.review()
        host = dict(hook_event_name='Stop', session_id='session-1', cwd=str(self.root),
                    last_assistant_message=self.completion)
        result = fixture.adapter.adapter_payload_from_env(self.source, hook_input=host)
        self.assertEqual({c['id']: c['status'] for c in self.read_state()['acceptance_criteria']},
                         dict(report='met', protected='unmet'))
        self.assertEqual(result['completion_recording']['units'][0]['status'], 'applied')
        self.assertNotIn('continuation', result)
        self.assertEqual(result['systemMessage'], '')

    def test_empty_coverage_preserves_unknown_and_only_advances_approved_work(self):
        from test_completion_continuation import CompletionContinuationTest
        from native_completion import assess_native_completion
        self.prepare()
        # Preparation creates no native output request. A final report with no
        # passing criteria cannot infer a new publication or task from it.
        prepared = self.stop()
        self.assertEqual(prepared['systemMessage'], '')
        self.assertEqual(prepared['completion_recording'], dict(status='v2-unit-state', units=[]))
        self.proof()
        self.completion = self.completion.replace('[completion-check]', '[partial-check]\n- scope: partial')
        self.completion = self.completion.replace('- unverified: none',
            '- unverified:\n  - report: Full result remains unknown\n  - protected: Remaining check is out of this bounded report')
        self.review()
        host = dict(hook_event_name='Stop', session_id='session-1', cwd=str(self.root),
                    last_assistant_message=self.completion)
        self.assertIsNone(assess_native_completion(self.source, text=self.completion, phase='final')['body_issue'])
        for _ in range(2):
            output = fixture.adapter.adapter_payload_from_env(self.source, hook_input=host)
            self.assertEqual(output['systemMessage'], '')
            self.assertNotIn('recovery', output)
            self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']}, {'unmet'})
        # Actual approved independent work still runs; an unmet flag alone
        # neither invents a task nor authorizes the unresolved dependency.
        item = lambda identity, criterion, **kw: CompletionContinuationTest.item(self, identity, criterion, **kw)
        CompletionContinuationTest.plan(self, [item('unresolved', 'report', status='blocked'),
                                              item('independent', 'protected')])
        output = fixture.adapter.adapter_payload_from_env(self.source, hook_input=host)
        self.assertEqual(output['continuation']['task_id'], 'independent')
        self.assertNotIn('Publish existing supported proof', output['systemMessage'])
        output = fixture.adapter.adapter_payload_from_env(self.source, hook_input=host)
        self.assertEqual(output['continuation']['status'], 'pending')
        self.assertFalse(output['continuation']['request_emitted'])
        self.assertEqual(output['systemMessage'], '')
        self.assertEqual({c['status'] for c in self.read_state()['acceptance_criteria']}, {'unmet'})

if __name__ == '__main__':
    unittest.main()
