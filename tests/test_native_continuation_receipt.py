"""Native host receipt identity, separate from task execution evidence."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'addons/autopilot-mode/skill/adapters'))


class NativeReceiptTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'native.jsonl'
        self.identity = dict(session_id='session', plan_id='sha256:'+'a'*64,
                             task_id='task', fingerprint='sha256:'+'b'*64, request_id='emission-1')
        self.text = '\n'.join(['[autopilot]', 'continuation: approved-task',
            'plan-id: '+self.identity['plan_id'], 'task-id: task',
            'request-fingerprint: '+self.identity['fingerprint'], 'request-id: emission-1'])
        self.rows = [dict(type='session_meta', payload=dict(id='session')),
            dict(type='event_msg', timestamp='2026-10-14T12:00:00Z', payload=dict(
                type='item_completed', thread_id='session', turn_id='turn',
                item=dict(type='HookPrompt', id='hook', fragments=[dict(text=self.text)])))]

    def observe(self):
        self.path.write_text('\n'.join(json.dumps(r) for r in self.rows)+'\n')
        from autopilot_native_receipt import observe_codex_receipts
        results = observe_codex_receipts(str(self.path),
            session_id=self.identity['session_id'], plan_id=self.identity['plan_id'],
            requests={self.identity['task_id']:dict(fingerprint=self.identity['fingerprint'],request_id=self.identity['request_id'])})
        return results.get(self.identity['task_id'], dict(status='unknown'))

    def test_exact_host_receipt_has_original_coordinate_and_no_execution_claim(self):
        result = self.observe()
        self.assertEqual(result['status'], 'observed')
        self.assertEqual(result['locator'], str(self.path)+':2')
        self.assertEqual(result['verified_at'], '2026-10-14T12:00:00Z')
        self.assertNotIn('execution', result)

    def test_human_message_with_identical_text_is_not_host_receipt(self):
        self.rows[1]['payload'] = dict(type='message', role='user', content=[dict(text=self.text)])
        self.assertEqual(self.observe()['status'], 'unknown')

    def test_wrong_owner_or_request_cannot_acknowledge(self):
        for key in ('session_id', 'plan_id', 'task_id', 'fingerprint', 'request_id'):
            with self.subTest(key=key):
                old = self.identity[key]
                self.identity[key] = 'other'
                self.assertEqual(self.observe()['status'], 'unknown')
                self.identity[key] = old

    def test_non_hook_tool_output_echo_cannot_acknowledge(self):
        self.rows[1]['payload']['item']['type'] = 'ToolCall'
        self.assertEqual(self.observe()['status'], 'unknown')

    def test_foreign_host_event_cannot_acknowledge(self):
        self.rows[1]['payload']['thread_id'] = 'foreign'
        self.assertEqual(self.observe()['status'], 'unknown')

    def test_missing_turn_or_id_does_not_acknowledge(self):
        self.rows[1]['payload']['turn_id'] = None
        self.assertEqual(self.observe()['status'], 'unknown')

    def test_old_feedback_without_request_identity_remains_unknown(self):
        self.rows[1]['payload']['item']['fragments'] = [dict(text='[autopilot]\ncontinuation: approved-task\ntask-id: task')]
        self.assertEqual(self.observe()['status'], 'unknown')

    def test_duplicate_identity_lines_are_ambiguous(self):
        self.rows[1]['payload']['item']['fragments'][0]['text'] += '\ntask-id: other'
        self.assertEqual(self.observe()['status'], 'unknown')

    def test_blank_lines_preserve_original_coordinate(self):
        self.observe()
        self.path.write_text('\n'+self.path.read_text())
        from autopilot_native_receipt import observe_codex_receipts
        result=observe_codex_receipts(str(self.path),session_id=self.identity['session_id'],
            plan_id=self.identity['plan_id'],requests={'task':dict(fingerprint=self.identity['fingerprint'],request_id=self.identity['request_id'])})
        self.assertEqual(result['task']['locator'],str(self.path)+':3')

    def test_incomplete_append_does_not_acknowledge(self):
        self.observe()
        with self.path.open('a') as stream:
            stream.write('{')
        from autopilot_native_receipt import observe_codex_receipts
        self.assertEqual(observe_codex_receipts(str(self.path),session_id=self.identity['session_id'],
            plan_id=self.identity['plan_id'],requests={'task':dict(fingerprint=self.identity['fingerprint'],request_id=self.identity['request_id'])}),{})

    def test_same_semantic_fingerprint_new_emission_cannot_reuse_old_receipt(self):
        self.identity['request_id']='emission-2'
        self.assertEqual(self.observe()['status'],'unknown')

    def test_missing_or_invalid_original_time_cannot_acknowledge(self):
        for value in (None, 17, [], {}, 'invalid', '2026-10-14T12:00:00'):
            with self.subTest(value=value):
                self.rows[1]['timestamp']=value
                self.assertEqual(self.observe()['status'],'unknown')

    def test_distinct_emissions_for_one_task_are_observed_by_request_identity(self):
        other=json.loads(json.dumps(self.rows[1]))
        other['payload']['item']['id']='second-hook'
        other['payload']['item']['fragments'][0]['text']=self.text.replace('emission-1','emission-2')
        self.rows.append(other)
        self.observe()
        from autopilot_native_receipt import observe_codex_receipts
        requests={identity:dict(task_id='task',fingerprint=self.identity['fingerprint'],request_id=identity)
            for identity in ('emission-1','emission-2')}
        found=observe_codex_receipts(str(self.path),session_id='session',plan_id=self.identity['plan_id'],requests=requests)
        self.assertEqual(set(found),set(requests))
        self.assertEqual(found['emission-1']['locator'],str(self.path)+':2')
        self.assertEqual(found['emission-2']['locator'],str(self.path)+':3')


if __name__ == '__main__':
    unittest.main()
