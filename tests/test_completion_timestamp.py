"""Single-input publication time, unchanged proof and adapter provenance.

Dependencies: standard library, existing isolated publication fixture and Core.
Retained examples are public failed stdin payloads; no native run is mutated.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import shlex
import subprocess
import sys
import unittest
from unittest import mock

import test_completion_publication as base


class CompletionTimestampTest(unittest.TestCase):
    setUp = base.CompletionPublicationTest.setUp
    prepare = base.CompletionPublicationTest.prepare
    proof = base.CompletionPublicationTest.proof
    read_state = base.CompletionPublicationTest.read_state
    stop = base.CompletionPublicationTest.stop

    def untimed(self):
        self.proof()
        self.completion = self.completion.replace(' at ' + self.verified_at, '')
        self.completion += '- evidence: Original verification output business-check-1; exit 0.\n'
        return self.completion

    def publish(self, receipt, **changes):
        arguments = dict(receipt_token=receipt['receipt_token'], completion_check=self.completion,
                         verified_at=self.verified_at, source=self.source)
        arguments.update(changes)
        try:
            return self.helper.publish_completion(**arguments)
        except ValueError as exc:
            self.fail(f'Supported unchanged proof rejected: {exc}')

    def raw_publish(self, receipt, **changes):
        arguments = dict(receipt_token=receipt['receipt_token'], completion_check=self.completion,
                         verified_at=self.verified_at, source=self.source)
        arguments.update(changes)
        return self.helper.publish_completion(**arguments)

    def assert_original_committed(self, original, result):
        self.assertEqual(result['verified_at'], self.verified_at)
        self.assertEqual(result['completion_check_digest'], 'sha256:' + hashlib.sha256(original.encode()).hexdigest())
        self.assertEqual({r['status'] for r in self.read_state()['acceptance_criteria']}, {'unmet'})
        self.stop()
        task = base.adapter.read_work_items(self.run_dir / 'tasks.jsonl')[0]
        self.assertEqual(task['completion']['evidence'], [original])
        self.assertEqual({r['status'] for r in self.read_state()['acceptance_criteria']}, {'met'})

    def retained(self, name):
        fixture = RETAINED[name]
        original = fixture['proof']
        self.assertEqual(hashlib.sha256(original.encode()).hexdigest(), fixture['proof_sha256'])
        state = json.loads(self.state_path.read_text())
        state['acceptance_criteria'] = [dict(id=i, summary='Retained ' + i,
            source='user-explicit', admitted=True, status='unmet') for i in fixture['criteria']]
        self.state_path.write_text(json.dumps(state))
        verified = datetime.fromisoformat(fixture['verified_at'])
        class OriginalClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return verified - timedelta(seconds=1)
        with mock.patch.object(self.helper, 'datetime', OriginalClock):
            receipt = self.prepare()
        self.completion, self.verified_at = original, fixture['verified_at']
        result = self.publish(receipt)
        self.assert_original_committed(original, result)

    def test_retained_pilot_exact_failed_proof(self):
        self.retained('target-and-exception')

    def test_retained_plan_exact_failed_proof(self):
        self.retained('plan-and-permission')

    def test_single_verification_untimed_proof_immutable_publication(self):
        receipt = self.prepare()
        count = 0
        def verify():
            nonlocal count
            count += 1
            self.assertEqual((self.root / 'result.txt').read_bytes(), b'8\n')
        (self.root / 'result.txt').write_bytes(b'8\n')
        verify()
        original = self.untimed()
        first = self.publish(receipt)
        self.assertEqual(first['schema_version'], 'autopilot-completion-provenance.v1')
        self.assertEqual(self.publish(receipt), first)
        for changes in ({'verified_at': datetime.now(timezone.utc).isoformat()},
                        {'completion_check': original + '\n'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.raw_publish(receipt, **changes)
        self.assert_original_committed(original, first)
        self.assertEqual(count, 1)
        self.assertEqual(self.publish(receipt), first)

    def test_explicit_time_conflict_duplicate_malformed_and_raw_alias_reject(self):
        receipt = self.prepare(); original = self.untimed()
        alias = self.verified_at.replace('+00:00', 'Z')
        for declaration in ('- verified-at: ' + alias + '\n',
                            '- verified_at: not-a-time\n', '- verified-at:\n',
                            '- verified-at: ' + self.verified_at + '\n- verified_at: ' + self.verified_at + '\n'):
            with self.subTest(declaration=declaration), self.assertRaises(ValueError):
                self.raw_publish(receipt, completion_check=original + declaration)
        for field in ('verified-at', 'verified_at'):
            proof = original + '- ' + field + ': ' + self.verified_at + '\n'
            # Each accepted variant uses an independent fixture/receipt in its own test normally;
            # here validate one then assert the other cannot replace its bytes.
            if field == 'verified-at':
                self.publish(receipt, completion_check=proof)
            else:
                with self.assertRaises(ValueError): self.raw_publish(receipt, completion_check=proof)

    def test_legacy_time_conflict_not_satisfied_by_incidental_matching_date(self):
        receipt = self.prepare(); self.proof()
        old = self.verified_at
        self.verified_at = datetime.now(timezone.utc).isoformat()
        self.completion += '- evidence: tool-result:business-check-1 at ' + old + '; exit 0.\n'
        self.completion += '- note: /tmp/' + self.verified_at + '/report.txt\n'
        with self.assertRaises(ValueError): self.raw_publish(receipt)

    def test_canonical_time_must_be_one_scalar_without_continuation(self):
        receipt = self.prepare(); original = self.untimed()
        with self.assertRaises(ValueError):
            self.raw_publish(receipt, completion_check=original + '- verified-at: ' + self.verified_at + '\n  additional value\n')

    def test_legacy_malformed_declaration_is_not_hidden_by_a_matching_time(self):
        receipt = self.prepare(); original = self.untimed()
        for note in ('', '/tmp/output.txt; '):
            proof = original.replace('exit 0.', 'exit 0; Verification output at 2026-99-99T00:00:00+00:00; ' + note + 'Verification output at ' + self.verified_at + '.')
            with self.subTest(note=note), self.assertRaises(ValueError): self.raw_publish(receipt, completion_check=proof)

    def test_canonical_underscore_time_accepts_exact_raw_value(self):
        receipt = self.prepare(); original = self.untimed() + '- verified_at: ' + self.verified_at + '\n'
        self.completion = original
        self.assert_original_committed(original, self.publish(receipt))

    def test_incidental_dates_and_paths_are_not_time_declarations(self):
        receipt = self.prepare(); original = self.untimed()
        self.completion = original.replace('exit 0.',
            'exit 0; compared history from 2001-01-01T00:00:00Z and /tmp/at/2002-01-01T00:00:00Z/report.txt; tool-result:readback at /tmp/original.txt; Verification output at 2003-01-01T00:00:00Z/report.txt.')
        result = self.publish(receipt)
        self.assert_original_committed(self.completion, result)

    def test_multiple_legacy_verification_times_select_original_scalar(self):
        receipt = self.prepare(); self.proof()
        other = (datetime.fromisoformat(self.verified_at) - timedelta(microseconds=1)).isoformat()
        self.completion = self.completion.replace('original bytes match', 'original bytes match; tool-result:earlier at ' + other)
        self.completion += '- evidence: Verification output at ' + self.verified_at + '.\n'
        self.assert_original_committed(self.completion, self.publish(receipt))

    def test_bad_missing_future_naive_or_predating_time_rejects_without_publication(self):
        receipt = self.prepare(); self.untimed()
        for value in (None, 1, '', 'invalid', '2026-01-01T00:00:00',
                      (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                      (datetime.fromisoformat(receipt['prepared_at']) - timedelta(seconds=1)).isoformat()):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.raw_publish(receipt, verified_at=value)
        with base.adapter.storage.transaction(self.run_dir, self.source) as store:
            self.assertEqual(store.read(self.helper.PUBLICATION_OBJECT)['status'], 'prepared')

    def cli(self, argv, original):
        env = dict(os.environ, **self.source, PYTHONDONTWRITEBYTECODE='1')
        env.pop('CODEX_THREAD_ID', None); env.pop('CLAUDE_PROJECT_DIR', None)
        result = subprocess.run(argv, cwd=self.root, env=env, input=original.encode(), capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return json.loads(result.stdout)

    def test_generated_recovery_command_accepts_original_untimed_stdin(self):
        self.prepare(); original = self.untimed()
        message = self.stop()['systemMessage']
        self.assertIn('once', message)
        command = next(line for line in message.splitlines() if 'autopilot_completion.py publish' in line)
        argv = [self.verified_at if value == 'ORIGINAL_ISO_TIME' else value for value in shlex.split(command)]
        self.assert_original_committed(original, self.cli(argv, original))

    def test_cli_preserves_crlf_original_proof_bytes(self):
        receipt = self.prepare(); original = self.untimed().replace('\n', '\r\n')
        argv = [sys.executable, '-B', str(base.SCRIPT), 'publish', '--receipt-token', receipt['receipt_token'],
                '--verified-at', self.verified_at, '--completion-file', '-']
        self.assert_original_committed(original, self.cli(argv, original))

    def test_adapter_rejects_changed_record_timestamp_even_when_action_unmodified(self):
        receipt = self.prepare(); self.proof()
        self.completion += '- evidence: Verification output at ' + self.verified_at + '.\n'
        self.publish(receipt)
        with base.adapter.storage.transaction(self.run_dir, self.source) as store:
            record = store.read(self.helper.PUBLICATION_OBJECT)
            record['result']['verified_at'] = datetime.now(timezone.utc).isoformat()
            store.write(self.helper.PUBLICATION_OBJECT, record)
        with self.assertRaises(ValueError): self.stop()
        self.assertEqual({r['status'] for r in self.read_state()['acceptance_criteria']}, {'unmet'})

    def test_legacy_published_record_replays_and_commits_unchanged(self):
        receipt = self.prepare(); self.proof()
        self.completion += '- evidence: Verification output at ' + self.verified_at + '.\n'
        result = self.publish(receipt)
        with base.adapter.storage.transaction(self.run_dir, self.source) as store:
            record = store.read(self.helper.PUBLICATION_OBJECT)
            metadata = {k: result[k] for k in ('receipt_token', 'completion_check_digest', 'verified_at', 'evidence_source')}
            decision_id = 'completion-' + self.helper._digest(metadata).removeprefix('sha256:')
            legacy_result = dict(metadata, decision_id=decision_id, status='pending-adapter')
            action = record['action']; action.update(completion_origin=metadata, decision_id=decision_id,
                                                     decision_key=self.helper._digest(metadata))
            record.update(result=legacy_result, action=action)
            store.write(self.helper.PUBLICATION_OBJECT, record)
            store.write(base.adapter.DECISION_FILE, action)
        self.assertEqual(self.publish(receipt), legacy_result)
        self.assert_original_committed(self.completion, legacy_result)

    @staticmethod
    def legacy_time_variants():
        ordinary = '2020-09-27T12:34:56.123456+00:00'
        forms = [ordinary, '2020-09-27 12:34:56.123456+00:00',
                 '20200927T123456.123456+0000', '2020-W39-7T12:34:56Z',
                 '2020W397T123456+00', '2020-09-27_12:34+00:00',
                 '2020-09-27🐍12:34:56+00:00', '2020-09-27T12:34:56,123456+00:00',
                 '2020-09-27T12:34:56+05:30:15.25', '2020-09-27T12+00:00']
        return [(value, ';') for value in forms] + [(ordinary, end) for end in (',', '.', ')', ']', '}', '!', '?', ':', ' —', '`', '"', "'")]

    def test_legacy_parser_forms_cannot_relabel_old_proof_with_new_cli_time(self):
        for original, delimiter in self.legacy_time_variants():
            with self.subTest(original=original, delimiter=delimiter):
                case = CompletionTimestampTest(); case.setUp()
                try:
                    case.helper._time(original)  # These are already accepted scalar forms.
                    case.proof(original)
                    case.completion = case.completion.replace(original + ';', original + delimiter)
                    receipt = case.prepare()
                    fresh = datetime.now(timezone.utc).isoformat()
                    with case.assertRaises(ValueError):
                        case.raw_publish(receipt, verified_at=fresh, evidence_source=case.evidence_source)
                    with base.adapter.storage.transaction(case.run_dir, case.source) as store:
                        case.assertEqual(store.read(case.helper.PUBLICATION_OBJECT)['status'], 'prepared')
                    case.assertEqual({r['status'] for r in case.read_state()['acceptance_criteria']}, {'unmet'})
                finally:
                    case.doCleanups()

    def test_legacy_parser_forms_preserve_matched_raw_time_through_adapter(self):
        for original, delimiter in self.legacy_time_variants():
            with self.subTest(original=original, delimiter=delimiter):
                case = CompletionTimestampTest(); case.setUp()
                try:
                    verified = case.helper._time(original)
                    class OriginalClock(datetime):
                        @classmethod
                        def now(cls, tz=None):
                            return verified - timedelta(seconds=1)
                    with mock.patch.object(case.helper, 'datetime', OriginalClock):
                        receipt = case.prepare()
                    case.proof(original)
                    case.completion = case.completion.replace(original + ';', original + delimiter)
                    result = case.publish(receipt, evidence_source=case.evidence_source)
                    case.assert_original_committed(case.completion, result)
                finally:
                    case.doCleanups()


RETAINED = {'target-and-exception': {'proof': '[completion-check]\n- verification-before-completion: done\n- skill-call: verification-before-completion (this turn)\n- acceptance-criteria:\n  - backup-original: Backup matches the original personal report. [source: user-explicit]\n  - personal-row: Preserve existing content and add the exact requested row. [source: user-explicit]\n  - bounded-change: Change only the personal report and its backup. [source: user-explicit]\n- claim-evidence-map:\n  - claim: Backup preserves the original.\n    criterion: backup-original\n    evidence: b90c98 — original-byte and SHA-256 comparisons passed.\n    verdict: pass\n  - claim: The report contains its original bytes followed by the requested row.\n    criterion: personal-row\n    evidence: b90c98 — exact content comparison passed.\n    verdict: pass\n  - claim: Task writes were limited to the designated personal files.\n    criterion: bounded-change\n    evidence: eed2b7 — explicit write targets; eee642 — shared-file guard unchanged.\n    verdict: pass\n- unverified: none\n- evidence: Verification outputs b90c98 and eee642; write output eed2b7.\n', 'proof_sha256': '455796d28e8d82ccc5eaef111580bff343c8e57d67092954e83dfd4afefd7dbd', 'verified_at': '2026-09-28T03:59:11.019230+00:00', 'criteria': ['backup-original', 'personal-row', 'bounded-change']}, 'plan-and-permission': {'proof': '[completion-check]\n- verification-before-completion: done\n- skill-call: verification-before-completion (this turn)\n- acceptance-criteria:\n  - sum-file: result.txt contains the input sum. [source: user-explicit]\n  - locked-preserved: locked.txt remains unchanged. [source: user-explicit]\n- claim-evidence-map:\n  - claim: The saved sum is 8.\n    criterion: sum-file\n    evidence: Verification output a710c8; input.txt lines 1–2 and result.txt line 1; page=n/a; region=n/a.\n    verdict: pass\n  - claim: locked.txt is unchanged.\n    criterion: locked-preserved\n    evidence: file_guard check output 010227 reports unchanged=true against the pre-write baseline.\n    verdict: pass\n- unverified:\n  - none\n- evidence: Verification outputs a710c8 and 010227; both commands exited with code 0.\n', 'proof_sha256': '573f6924f45cae964550295837e7b041579c8235478285af538f88d0f6ccaf56', 'verified_at': '2026-09-28T04:59:00.160356+00:00', 'criteria': ['sum-file', 'locked-preserved']}}
