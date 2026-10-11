"""Retained claims use native AP routing without recording or business replay."""
from __future__ import annotations

import copy
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest import mock

import test_completion_contract_v2 as v2
from response_route_fixture import register_route
import test_completion_publication as fixtures
import test_completion_continuation as continuation_tests


class ClaimReportRoutingTest(v2._CompletionV2Fixture):
    native_stop = v2.ResponsePurposeV2Test.native_stop

    def setUp(self):
        super().setUp()
        skills = self.root / 'installed-skills'
        skills.mkdir()
        (skills / 'autopilot-mode').symlink_to(fixtures.SKILL, target_is_directory=True)
        self.source['GHOST_ALICE_SKILLS_DIR'] = str(skills)

    def accepted(self, *, apply=True, statement='Saved report matches input'):
        receipt = self.prepare()
        self.proof()
        self.completion = self.completion.replace('  - claim: Saved report matches input\n',
            '  - claim: ' + statement + '\n')
        self.published = self.publish(receipt)
        if apply:
            applied = self.stop()
            self.assertEqual(applied['completion_recording']['units'][0]['unit_status'], 'complete')
        self.claim = dict(statement=statement, work_unit_id='report-unit',
            contract_revision=1, criterion_id='report',
            proof_digest='sha256:' + hashlib.sha256(self.completion.encode()).hexdigest())
        return self.report()

    def report(self, *, statement=None, tail=''):
        claim = dict(self.claim)
        if statement is not None:
            claim['statement'] = statement
        return (claim['statement'] + '\n\n[evidence-report]\n' +
            json.dumps(dict(schema_version='claim-evidence.v1', claims=[claim])) +
            '\n[/evidence-report]\n' + tail)

    def projection(self, text, *, phase='final', **scope):
        from autopilot_intent_recovery import current_execution_projection
        material = fixtures.adapter.read_session_material(self.root / 'intent', 'codex', 'session-1',
            source=self.source, recover_audit=False)
        return current_execution_projection(material, self.source, self.run_dir,
            claim_scope=dict(text=text, phase=phase, **scope))

    def core_stop(self, text):
        skills = self.root / 'installed-skills'
        skills.mkdir(exist_ok=True)
        installed_ap = skills / 'autopilot-mode'
        if not installed_ap.exists():
            installed_ap.symlink_to(fixtures.SKILL, target_is_directory=True)
        hook = Path(self.source['GHOST_ALICE_CORE_ROOT']) / '_shared/claude_stop_verification_hook.py'
        result = subprocess.run([sys.executable, '-B', str(hook), '--platform', 'codex'],
            input=json.dumps(dict(hook_event_name='Stop', session_id='session-1',
                cwd=str(self.root), last_assistant_message=text)), cwd=self.root,
            env={**os.environ, **self.source, 'CODEX_THREAD_ID': 'session-1',
                 'GHOST_ALICE_SKILLS_DIR': str(skills)}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def snapshot(self):
        database = self.root / 'intent/ghost-state.sqlite3'
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
            records = tuple(connection.iterdump())
        artifacts = {str(path.relative_to(self.run_dir)): path.read_bytes()
            for path in self.run_dir.rglob('*') if path.is_file()}
        return copy.deepcopy(self.read_state()), records, artifacts

    def stale_route(self):
        self.core.record_turn(root=self.root / 'intent', platform='codex', session_id='session-1',
            raw_user_input='Explain the already accepted report')

    def test_native_projection_consumes_only_core_assessment(self):
        import autopilot_intent_recovery as recovery
        text = 'The response body is rejected by Core.'
        baseline = self.projection('', phase='before_tool')
        assessment = dict(schema_version='native-completion.v1', platform='codex', session_id='session-1', phase='final',
            input_event_id=self.read_state()['latest_input_event_id'],
            body_digest='sha256:' + hashlib.sha256(text.encode()).hexdigest(),
            response_route=baseline['response_route'], completion_route=baseline['completion_route'],
            body_issue='Core rejected an unbound closure', validation_text=text, has_evidence_marker=False)
        with mock.patch.object(recovery, '_core_native_assessor', create=True) as loader, \
                mock.patch.object(recovery, '_core_completion_selector', create=True,
                    side_effect=AssertionError('AP must not select policy independently')), \
                mock.patch.object(recovery, '_core_claim_report', create=True,
                    side_effect=AssertionError('AP must not parse evidence independently')):
            loader.return_value.return_value = assessment
            result = self.projection(text, claim_report=dict(valid=True, residual_text=''))
        loader.return_value.assert_called_once()
        args, kwargs = loader.return_value.call_args
        self.assertEqual(args, (dict(self.source, GHOST_ALICE_SESSION_INTENT_ROOT=
            str(Path(self.source['GHOST_ALICE_SESSION_INTENT_ROOT']).resolve())),))
        self.assertEqual(kwargs['text'], text)
        self.assertEqual(kwargs['phase'], 'final')
        self.assertNotIn('claim_report', kwargs)
        self.assertEqual(result['status'], 'recording-unavailable')
        self.assertEqual(result['reason'], 'completion-body-invalid')
        self.assertEqual(result['detail'], assessment['body_issue'])

    def test_core_assessment_cannot_cross_body_or_input_identity(self):
        import autopilot_intent_recovery as recovery
        text = 'Ordinary response'
        baseline = self.projection('', phase='before_tool')
        assessment = dict(schema_version='native-completion.v1', platform='codex', session_id='session-1', phase='final',
            input_event_id=self.read_state()['latest_input_event_id'],
            body_digest='sha256:' + hashlib.sha256(text.encode()).hexdigest(),
            response_route=baseline['response_route'], completion_route=baseline['completion_route'],
            body_issue=None, validation_text=text, has_evidence_marker=False)
        for change in [dict(input_event_id='previous-input'), dict(body_digest='sha256:'+'0'*64),
                       dict(platform='claude'), dict(session_id='other-session'), dict(phase='before_tool')]:
            with self.subTest(change=change), mock.patch.object(recovery, '_core_native_assessor', create=True) as loader:
                loader.return_value.return_value = dict(assessment, **change)
                result = self.projection(text)
                self.assertEqual(result['status'], 'recording-unavailable')
                self.assertEqual(result['reason'], 'core-assessment-incompatible')

    def test_report_only_preserves_ready_continuation_without_new_publication(self):
        helpers = continuation_tests.CompletionContinuationTest
        helpers.partial(self)
        applied = self.stop()
        self.assertEqual(applied['completion_recording']['units'][0]['status'], 'applied')
        self.claim = dict(statement='Saved report matches input', work_unit_id='report-unit',
            contract_revision=1, criterion_id='report',
            proof_digest='sha256:' + hashlib.sha256(self.completion.encode()).hexdigest())
        helpers.plan(self, [helpers.item(self, 'independent', 'protected')])
        before = self.read_state()['acceptance_criteria']
        database = self.root / 'intent/ghost-state.sqlite3'
        def publications():
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
                return connection.execute("SELECT identity, body_json FROM ap_completion_v2_records WHERE kind='publication' ORDER BY identity").fetchall()
        original_publications = publications()
        result = self.native_stop(self.report())
        self.assertEqual(result.get('continuation', {}).get('task_id'), 'independent', result)
        self.assertEqual(result['continuation']['status'], 'dispatched')
        self.assertEqual(self.read_state()['acceptance_criteria'], before)
        self.assertEqual(publications(), original_publications)

    def test_native_report_only_overrides_completion_purpose_without_writes(self):
        report = self.accepted()
        register_route(self.core, self.root / 'intent', purpose='completion')
        before = self.snapshot()
        result = self.native_stop(report)
        recording = result['completion_recording']
        selection = self.projection(report)['completion_route']
        self.assertTrue(selection['report_only'])
        self.assertEqual(selection['publication_route'], 'none')
        self.assertEqual(selection['selected_work_unit_ids'], ['report-unit'])
        self.assertTrue(recording['units'][0]['application_reused'])
        self.assertEqual(result['systemMessage'], '')
        self.assertEqual(self.snapshot(), before)

    def test_native_retained_report_requires_current_route_or_native_activity_binding(self):
        report = self.accepted()
        self.stale_route()
        before = self.snapshot()
        result = self.native_stop(report)
        recording = result['completion_recording']
        self.assertEqual(recording['status'], 'recording-unavailable')
        self.assertEqual(recording['reason'], 'completion-body-invalid')
        self.assertIn('native-activity-unavailable',recording['detail'])
        self.assertEqual(recording['completion_route']['route_binding'], 'stale')
        self.assertEqual(recording['completion_route']['selected_work_unit_ids'], [])
        self.assertEqual(result['systemMessage'], '')
        self.assertEqual(self.snapshot(), before)

    def test_native_unaccepted_report_does_not_apply_pending_publication(self):
        report = self.accepted(apply=False)
        before = self.snapshot()
        result = self.native_stop(report)
        self.assertEqual(result['completion_recording']['status'], 'recording-unavailable')
        self.assertEqual(result['completion_recording'].get('reason'), 'claim-evidence-invalid')
        self.assertNotIn('Publish existing supported proof', result['systemMessage'])
        self.assertEqual(self.snapshot(), before)

    def test_core_rejected_final_does_not_apply_ready_publication(self):
        self.accepted(apply=False)
        register_route(self.core, self.root / 'intent', purpose='completion')
        before = self.snapshot()
        result = self.native_stop('The entire task is complete.')
        self.assertEqual(result['completion_recording']['reason'], 'completion-body-invalid')
        self.assertEqual(self.snapshot(), before)

    def test_native_unmatched_claim_cannot_borrow_a_real_proof(self):
        self.accepted()
        before = self.snapshot()
        result = self.native_stop(self.report(statement='The entire task is complete.'))
        self.assertEqual(result['completion_recording'].get('reason'), 'claim-evidence-invalid')
        self.assertEqual(self.snapshot(), before)

    def test_mixed_new_claim_requires_review_without_inferred_publication(self):
        self.accepted()
        result = self.projection(self.report(tail='\nThe entire task is complete.'))
        selection = result['completion_route']
        self.assertFalse(selection.get('report_only', False))
        self.assertFalse(selection['required_completion_check'])
        self.assertEqual(selection['publication_route'], 'none')
        self.assertEqual(selection['selected_work_unit_ids'], ['report-unit'])
        self.assertEqual(result['reason'], 'completion-body-invalid')
        self.assertIn('claim-evidence-unbound',result['detail'])

    def test_mixed_new_claim_cannot_bypass_stale_route(self):
        self.accepted()
        self.stale_route()
        before = self.snapshot()
        result = self.native_stop(self.report(tail='\nThe entire task is complete.'))
        self.assertEqual(result['completion_recording']['reason'], 'completion-body-invalid')
        self.assertEqual(result['completion_recording']['completion_route']['route_binding'], 'stale')
        self.assertEqual(self.snapshot(), before)

    def test_malformed_completion_block_is_not_removed_with_retained_claim(self):
        self.accepted()
        result = self.projection(self.report(tail='\n[completion-check]\n- unverified: none\n'))
        self.assertFalse(result['completion_route'].get('report_only', False))
        self.assertEqual(result['completion_route']['publication_route'], 'prepare')

    def test_caller_supplied_claim_assessment_is_not_authority(self):
        self.accepted()
        register_route(self.core, self.root / 'intent', purpose='completion')
        supplied = dict(present=True, valid=True, residual_text='', claims=[self.claim], issue=None)
        result = self.projection('The entire task is complete.', claim_report=supplied)
        self.assertFalse(result['completion_route'].get('report_only', False))
        self.assertTrue(result['completion_route']['required_completion_check'])
        self.assertEqual(result['completion_route']['publication_route'], 'prepare')

    def test_before_tool_scope_does_not_gain_report_only_exemption(self):
        report = self.accepted()
        result = self.projection(report, phase='before_tool')
        self.assertFalse(result['completion_route'].get('report_only', False))
        self.assertEqual(result['completion_route']['publication_route'], 'prepare')

    def test_native_unlinked_historical_success_requires_response_review(self):
        self.accepted()
        register_route(self.core, self.root / 'intent', purpose='explanation')
        result = self.projection('According to the previous record, tests passed.')
        self.assertFalse(result['completion_route']['required_completion_check'])
        self.assertEqual(result['completion_route']['publication_route'], 'none')
        self.assertEqual(result['reason'],'completion-body-invalid')
        self.assertIn('response-assessment-required',result['detail'])

    def test_partial_block_remains_guarded_beside_retained_claim(self):
        self.accepted()
        partial = '''
[partial-check]
- scope: partial
- verification-before-completion: done
- skill-call: verification-before-completion (this turn)
- acceptance-criteria:
  - report: Saved report matches input [source: user-explicit]
  - protected: Protected file remains to be checked [source: user-explicit]
- claim-evidence-map:
  - claim: Saved report matches input
    criterion: report
    evidence: tool-result:current-partial-check
    verdict: pass
- unverified:
  - protected: Required check is outstanding
'''
        result = self.projection(self.report(tail=partial))
        self.assertFalse(result['completion_route'].get('report_only', False))
        self.assertEqual(result['completion_route']['publication_route'], 'prepare')

    def test_native_core_and_ap_allow_retained_report_after_current_review_without_writes(self):
        report = self.accepted()
        self.stale_route()
        register_route(self.core, self.root / 'intent', purpose='explanation')
        self.review(report,reuse_candidate=self.review_candidates['report-unit'])
        before = self.snapshot()
        core = self.core_stop(report)
        ap = self.native_stop(report)
        self.assertNotEqual(core.get('decision'), 'block', core)
        self.assertTrue(ap['completion_recording']['units'][0]['application_reused'])
        self.assertEqual(self.snapshot(), before)

    def test_native_core_and_ap_reject_same_unaccepted_report_without_application(self):
        report = self.accepted(apply=False)
        before = self.snapshot()
        core = self.core_stop(report)
        ap = self.native_stop(report)
        self.assertEqual(core.get('decision'), 'block', core)
        self.assertIn('claim-evidence', core.get('reason', ''))
        self.assertEqual(ap['completion_recording']['reason'], 'claim-evidence-invalid')
        self.assertEqual(self.snapshot(), before)

    def test_native_core_and_ap_preserve_guards_for_mixed_new_closure(self):
        self.accepted()
        self.stale_route()
        report = self.report(tail='\nThe entire task is complete.')
        before = self.snapshot()
        core = self.core_stop(report)
        ap = self.native_stop(report)
        self.assertEqual(core.get('decision'), 'block', core)
        self.assertIn('native-activity-unavailable', core.get('reason', ''))
        self.assertEqual(ap['completion_recording']['reason'], 'completion-body-invalid')
        self.assertEqual(ap['completion_recording']['completion_route']['route_binding'], 'stale')
        self.assertEqual(self.snapshot(), before)

    def test_plain_original_claim_uses_native_evidence_without_report_protocol(self):
        self.accepted(statement='The report has been completed and verified.')
        before = self.snapshot()
        text = self.claim['statement']
        ap = self.native_stop(text)
        core = self.core_stop(text)
        self.assertTrue(ap['completion_recording']['units'][0]['application_reused'])
        self.assertTrue(self.projection(text)['completion_route']['report_only'])
        self.assertNotEqual(core.get('decision'), 'block', core)
        self.assertEqual(self.snapshot(), before)

    def test_plain_original_claim_with_current_review_needs_no_new_publication(self):
        self.accepted(statement='The report has been completed and verified.')
        self.stale_route()
        text = self.claim['statement']
        register_route(self.core, self.root / 'intent', purpose='explanation')
        self.review(text,reuse_candidate=self.review_candidates['report-unit'])
        before = self.snapshot()
        ap = self.native_stop(text)
        core = self.core_stop(text)
        self.assertTrue(ap['completion_recording']['units'][0]['application_reused'])
        self.assertNotEqual(core.get('decision'), 'block', core)
        self.assertEqual(self.snapshot(), before)

    def test_plain_widened_claim_is_not_automatically_reused(self):
        self.accepted(statement='The report has been completed and verified.')
        self.stale_route()
        before = self.snapshot()
        text = self.claim['statement'] + ' and the entire task is complete.'
        ap = self.native_stop(text)
        core = self.core_stop(text)
        self.assertEqual(ap['completion_recording']['reason'], 'completion-body-invalid')
        self.assertEqual(ap['completion_recording']['completion_route']['route_binding'], 'stale')
        self.assertEqual(core.get('decision'), 'block', core)
        self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
