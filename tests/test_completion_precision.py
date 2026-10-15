"""Causal capture ordering, original time preservation and strict legacy fallback."""
from datetime import datetime, timezone
import contextlib
import io
from unittest import mock

from test_completion_contract_v2 import _CompletionV2Fixture
import autopilot_completion_contract as contract
import autopilot_completion_service as service


class CompletionPrecisionTest(_CompletionV2Fixture):
    # Retained native capture: claude-turn4.jsonl lines 40/45; whole-second
    # verifier output is line 18 and its rejection is line 33.
    captured = '2026-10-10T07:45:58.428871+00:00'
    precise = '2026-10-10T07:45:58.800123+00:00'
    coarse = '2026-10-10T07:45:58Z'

    def setUp(self):
        super().setUp()
        from test_completion_publication import SKILL
        skills = self.root / 'skills'
        provider = skills / 'autopilot-mode/adapters/autopilot_claim_evidence.py'
        provider.parent.mkdir(parents=True)
        provider.symlink_to(SKILL / 'adapters/autopilot_claim_evidence.py')
        self.source['GHOST_ALICE_SKILLS_DIR'] = str(skills)

    def capture(self):
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.fromisoformat(self.captured)
        with mock.patch.object(service, 'datetime', Clock):
            return self.prepare()

    def test_original_same_second_uses_authenticated_capture_order(self):
        receipt = self.capture()
        original = self.proof(self.coarse)
        published = self.publish(receipt)
        self.assertEqual(published['verified_at'], self.coarse)
        self.stop()
        self.assertEqual(self.completion, original)
        self.assertEqual({r['status'] for r in self.read_state()['acceptance_criteria']}, {'met'})

    def test_precise_original_declaration_recovers_only_serialization_loss(self):
        receipt = self.capture()
        self.proof(self.precise)
        self.completion += '- verified-at: ' + self.precise + '\n'
        original = self.completion
        self.verified_at = self.coarse
        try:
            published = self.publish(receipt)
        except ValueError as error:
            self.fail(f'Unchanged precise original proof rejected: {error}')
        self.assertEqual(published['verified_at'], self.coarse)
        self.stop()
        self.assertEqual({r['status'] for r in self.read_state()['acceptance_criteria']}, {'met'})
        with self.helper.storage.transaction(self.run_dir, self.source, migrate_legacy=False) as store:
            record = store.completion_read('evidence', published['evidence_references'][0])
        self.assertEqual(record['proof'], original)
        self.assertEqual(record['verified_at'], self.coarse)
        self.assertEqual(record['captured_at'], self.captured)
        # Revalidation derives the same original source time; it cannot refresh it.
        self.assertEqual(contract.validate_evidence(record, contract.digest(record),
            self.read_origin(record), datetime.now(timezone.utc).isoformat(), source=self.source), record)

    def read_origin(self, record):
        with self.helper.storage.transaction(self.run_dir, self.source, migrate_legacy=False) as store:
            return store.completion_read('origin', record['origin_token'])

    def test_precise_original_before_capture_is_stale_not_repaired(self):
        receipt = self.capture()
        self.proof('2026-10-10T07:45:58.100000+00:00')
        self.completion += '- verified_at: ' + self.verified_at + '\n'
        with self.assertRaises(ValueError) as raised:
            self.publish(receipt, verified_at=self.coarse)
        self.assertEqual(getattr(raised.exception, 'reason', None), 'time-invalid')

    @mock.patch("completion_assessment._capture_binding", return_value=None)
    def test_incidental_precise_date_does_not_supply_ordering(self, _legacy_capture):
        receipt = self.capture()
        self.proof(self.coarse)
        self.completion += '- note: observed unrelated date ' + self.precise + '\n'
        with self.assertRaises(contract.CompletionContractError) as raised:
            self.publish(receipt)
        self.assertEqual(raised.exception.reason, 'time-order-ambiguous')

    def test_definitely_stale_and_future_remain_rejected(self):
        receipt = self.capture()
        for value in ('2026-10-10T07:45:57Z', '2026-10-10T07:45:58.199999+00:00',
                      '2099-01-01T00:00:00.123456+00:00'):
            with self.subTest(value=value):
                self.proof(value)
                with self.assertRaises(contract.CompletionContractError) as raised:
                    self.publish(receipt)
                self.assertEqual(getattr(raised.exception, 'reason', None), 'time-invalid')

    def test_full_precision_same_second_survives_serialization_application(self):
        receipt = self.capture()
        original = self.proof(self.precise)
        published = self.publish(receipt)
        self.assertEqual(published['verified_at'], self.precise)
        self.stop()
        self.assertEqual({r['status'] for r in self.read_state()['acceptance_criteria']}, {'met'})
        self.assertEqual(original, self.completion)

    def test_time_helper_emits_explicit_microseconds_without_preparing_or_verifying(self):
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 10, 7, 45, 58, tzinfo=timezone.utc)
        output = io.StringIO()
        with mock.patch.object(self.helper, 'datetime', Clock), contextlib.redirect_stdout(output):
            try:
                self.assertEqual(self.helper.main(['verification-time']), 0)
            except SystemExit as error:
                self.fail(f'Full precision clock command unavailable: {error}')
        self.assertEqual(output.getvalue(), '2026-10-10T07:45:58.000000+00:00\n')
        self.assertFalse(self.run_dir.exists())
        self.assertEqual({r['status'] for r in self.read_state()['acceptance_criteria']}, {'unmet'})

    @mock.patch("completion_assessment._capture_binding", return_value=None)
    def test_ambiguous_publication_parks_native_stop_without_resubmission(self, _legacy_capture):
        receipt = self.capture()
        self.proof(self.coarse)
        # Native publication derives its source from the submitted original;
        # isolate clock ambiguity after providing that required evidence field.
        self.completion += '- evidence: ' + self.evidence_source + '\n'
        original = self.completion
        with self.assertRaises(contract.CompletionContractError):
            self.publish(receipt)
        import test_completion_publication as fixture
        host = dict(hook_event_name='Stop', session_id='session-1', cwd=str(self.root),
                    last_assistant_message=original)
        first = fixture.adapter.adapter_payload_from_env(self.source, hook_input=host)
        second = fixture.adapter.adapter_payload_from_env(self.source, hook_input=host)
        self.assertEqual(first['recovery']['status'], 'diagnostic')
        self.assertIn('time-order-ambiguous', first['systemMessage'])
        self.assertEqual(second['recovery']['status'], 'parked')
        self.assertEqual(second['systemMessage'], '')
        for result in (first, second):
            self.assertIs(result['continue'], True)
            self.assertNotIn('decision', result)
            self.assertNotIn('Publish existing supported proof', result['systemMessage'])
            self.assertNotIn('--receipt-token', result['systemMessage'])
            self.assertEqual(result['completion_recording']['units'][0]['reason'], 'time-order-ambiguous')
        self.assertEqual(self.completion, original)
        self.assertEqual({r['status'] for r in self.read_state()['acceptance_criteria']}, {'unmet'})

    def precise_proof(self):
        self.proof(self.precise)
        self.completion += '- verified-at: ' + self.precise + '\n'
        self.verified_at = self.coarse

    def test_precision_recovery_does_not_admit_wrong_receipt_or_session(self):
        receipt = self.capture()
        self.precise_proof()
        for changes in ({'receipt_token': 'sha256:' + '0' * 64},
                        {'source': dict(self.source, GHOST_ALICE_SESSION_ID='other-session')}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, FileNotFoundError)):
                self.publish(receipt, **changes)
        self.assertEqual({r['status'] for r in self.read_state()['acceptance_criteria']}, {'unmet'})

    def test_precision_recovery_does_not_admit_changed_revision(self):
        receipt = self.capture()
        self.precise_proof()
        criteria = self.read_state()['acceptance_criteria']
        criteria[0]['summary'] = 'A different required report'
        self.change_intent({'acceptance_criteria': criteria})
        with self.assertRaises(ValueError):
            self.publish(receipt)
        self.assertEqual({r['status'] for r in self.read_state()['acceptance_criteria']}, {'unmet'})

    def test_precision_recovery_does_not_apply_changed_artifact(self):
        receipt = self.capture()
        artifact = self.root / 'report.txt'
        artifact.write_text('checked bytes')
        self.precise_proof()
        self.publish(receipt, dependencies=[str(artifact)])
        artifact.write_text('changed bytes')
        self.stop()
        self.assertEqual({r['status'] for r in self.read_state()['acceptance_criteria']}, {'unmet'})

    @mock.patch("completion_assessment._capture_binding", return_value=None)
    def test_precision_recovery_rejects_unrelated_or_ambiguous_source_times(self, _legacy_capture):
        receipt = self.capture()
        for declaration in ('2026-10-10T07:45:59.800123+00:00',
                            '2026-10-10T07:45:58+00:00',
                            self.precise + '\n- verified_at: ' + self.precise):
            with self.subTest(declaration=declaration):
                self.proof(self.coarse)
                self.completion += '- verified-at: ' + declaration + '\n'
                with self.assertRaises(ValueError):
                    self.publish(receipt)
        self.assertEqual({r['status'] for r in self.read_state()['acceptance_criteria']}, {'unmet'})

    def test_unambiguous_lower_precision_later_second_is_not_blanket_rejected(self):
        receipt = self.capture()
        self.proof('2026-10-10T07:45:59Z')
        self.publish(receipt)
        self.stop()
        self.assertEqual({r['status'] for r in self.read_state()['acceptance_criteria']}, {'met'})

    def test_new_check_captures_helper_output_and_publishes_that_exact_time(self):
        import os
        import subprocess
        import sys
        receipt = self.prepare()
        artifact = self.root / 'checked.txt'
        artifact.write_text('expected')
        # The owning check runs once. Its command captures the clock immediately
        # after its actual assertion; publication never obtains another clock.
        command = ('from pathlib import Path; import subprocess,sys; '
            'assert Path(sys.argv[1]).read_text() == "expected"; '
            'subprocess.run([sys.executable,"-B",sys.argv[2],"verification-time"],check=True)')
        result = subprocess.run([sys.executable, '-B', '-c', command, str(artifact),
            str(self.helper.__file__)], capture_output=True, text=True, env=dict(os.environ, **self.source))
        self.assertEqual(result.returncode, 0, result.stderr)
        original_time = result.stdout.strip()
        self.assertRegex(original_time, r'\.\d{6}\+00:00$')
        self.proof(original_time)
        publication = self.publish(receipt, dependencies=[str(artifact)])
        self.stop()
        self.assertEqual(publication['verified_at'], original_time)
        self.assertEqual({r['status'] for r in self.read_state()['acceptance_criteria']}, {'met'})

    @mock.patch("completion_assessment._capture_binding", return_value=None)
    def test_fenced_or_other_block_time_cannot_supply_precision_correction(self, _legacy_capture):
        receipt = self.capture()
        for suffix in ('\nExample only:\n```text\n- verified-at: ' + self.precise + '\n```\n',
                       '\n```text\n- verified-at: ' + self.precise + '\n```\n',
                       '\n[io-trace]\n- verified-at: ' + self.precise + '\n',
                       '- evidence: tool-result:business-check-1\n  [io-trace]\n- verified-at: ' + self.precise + '\n'):
            with self.subTest(suffix=suffix):
                self.proof(self.coarse)
                self.completion = self.completion.replace(' at ' + self.coarse, '') + suffix
                with self.assertRaises(ValueError):
                    self.publish(receipt)
                self.assertEqual({r['status'] for r in self.read_state()['acceptance_criteria']}, {'unmet'})
