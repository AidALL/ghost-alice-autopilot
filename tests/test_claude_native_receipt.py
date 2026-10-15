"""Claude host receipt binding; fixtures follow Core native_transcript actors.

Dependencies: Python 3.11+ standard library. No native provider is invoked.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'addons/autopilot-mode/skill/adapters'))
import autopilot_native_receipt


def stop_blocking_attachment(text, *, session_id='claude-session'):
    """Relevant shape of actual Claude row 628, observed 2026-10-15T05:28:14.943Z.

    Original: .claude/projects/-Users-raymond--codex-worktrees-d0bf-ghost-alice/
    42fcc6b9-ef14-452e-9000-bfee7de3a9e2.jsonl:628. Only the command and
    supplied declaration are fixture values; no native provider is invoked.
    """
    command = 'fixture Stop adapter'
    return dict(type='attachment', sessionId=session_id, isSidechain=False,
        uuid='9aaa8272-0456-4bd8-9ca2-669fa7564a5c', timestamp='2026-10-15T05:28:14.943Z',
        attachment=dict(type='hook_blocking_error', hookName='Stop', hookEvent='Stop',
            toolUseID='3ba244c2-8e83-40ea-93e5-1fa083b990b2',
            blockingError=dict(command=command, blockingError=text)),
        renderedRole='system', rendered=[dict(content='<system-reminder>\n'
            'Stop hook blocking error from command: "'+command+'": '+text+'\n</system-reminder>')])


class ClaudeNativeReceiptTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'native.jsonl'
        self.identity = dict(session_id='claude-session', plan_id='sha256:' + 'a' * 64,
            task_id='task', fingerprint='sha256:' + 'b' * 64, request_id='emission-1')
        self.text = '\n'.join(['[autopilot]', 'continuation: approved-task',
            'plan-id: ' + self.identity['plan_id'], 'task-id: task',
            'request-fingerprint: ' + self.identity['fingerprint'], 'request-id: emission-1'])
        self.row = dict(type='user', sessionId='claude-session', uuid='native-message',
            timestamp='2026-10-14T12:00:00Z', promptSource='system', turnOrigin='task_notification',
            origin=dict(kind='task-notification', producer='session-task'),
            message=dict(role='user', content=self.text))
        self.rows = [self.row]

    def observe(self, *, write=True):
        if write:
            self.path.write_text('\n'.join(json.dumps(row) for row in self.rows) + '\n')
        observer = getattr(autopilot_native_receipt, 'observe_claude_receipts', None)
        self.assertTrue(callable(observer), 'Claude native receipt reader is absent')
        return observer(str(self.path), session_id=self.identity['session_id'],
            plan_id=self.identity['plan_id'], requests={self.identity['task_id']: dict(
                fingerprint=self.identity['fingerprint'], request_id=self.identity['request_id'])})

    def test_distinct_emissions_for_one_task_retain_both_native_receipts(self):
        other=json.loads(json.dumps(self.row))
        other['uuid']='second-native-message'
        other['message']['content']=self.text.replace('emission-1','emission-2')
        self.rows.append(other)
        self.observe()
        requests={identity:dict(task_id='task',fingerprint=self.identity['fingerprint'],request_id=identity)
            for identity in ('emission-1','emission-2')}
        found=autopilot_native_receipt.observe_claude_receipts(str(self.path),session_id='claude-session',
            plan_id=self.identity['plan_id'],requests=requests)
        self.assertEqual(set(found),set(requests))
        self.assertEqual(found['emission-1']['native_message_id'],'native-message')
        self.assertEqual(found['emission-2']['native_message_id'],'second-native-message')

    def test_exact_native_notification_retains_original_and_claims_only_delivery(self):
        receipt = self.observe()['task']
        self.assertEqual(receipt['status'], 'observed')
        self.assertEqual(receipt['fingerprint'], self.identity['fingerprint'])
        self.assertEqual(receipt['request_id'], 'emission-1')
        self.assertEqual(receipt['locator'], str(self.path) + ':1')
        self.assertEqual(receipt['native_message_id'], 'native-message')
        self.assertEqual(receipt['native_actor'], 'notification')
        self.assertEqual(receipt['verified_at'], '2026-10-14T12:00:00Z')
        self.assertEqual(receipt['event_digest'], 'sha256:' + hashlib.sha256(
            json.dumps(self.row, sort_keys=True, separators=(',', ':')).encode()).hexdigest())
        self.assertNotIn('execution', receipt)

    def test_supported_queued_command_attachment_can_acknowledge(self):
        self.rows = [dict(type='attachment', sessionId='claude-session', uuid='attachment-id',
            timestamp='2026-10-14T12:00:00+00:00', isMeta=True, attachment=dict(
                type='queued_command', commandMode='task-notification', origin=deepcopy(self.row['origin']),
                prompt=self.text))]
        self.assertEqual(self.observe()['task']['native_message_id'], 'attachment-id')

    def test_actual_stop_blocking_envelope_retains_original_delivery_only(self):
        self.rows = [stop_blocking_attachment(self.text)]
        receipts = self.observe()
        self.assertIn('task', receipts, 'The actual Stop blocking envelope must acknowledge delivery')
        receipt = receipts['task']
        self.assertEqual(receipt['native_actor'], 'stop-hook-blocking-error')
        self.assertEqual(receipt['native_message_id'], self.rows[0]['uuid'])
        self.assertEqual(receipt['verified_at'], '2026-10-15T05:28:14.943Z')
        self.assertEqual(receipt['locator'], str(self.path)+':1')
        self.assertEqual(receipt['request_id'], self.identity['request_id'])
        self.assertEqual(receipt['event_digest'], 'sha256:'+hashlib.sha256(
            json.dumps(self.rows[0], sort_keys=True, separators=(',', ':')).encode()).hexdigest())
        self.assertNotIn('execution', receipt)

    def test_stop_blocking_receipt_requires_exact_native_envelope(self):
        base = stop_blocking_attachment(self.text)
        cases = []
        for key, values in (('type', ('user', 'system')), ('isSidechain', (True, None)),
                ('renderedRole', ('user', None)), ('rendered', (None, [],
                    [dict(type='tool_result', content=self.text)],
                    base['rendered']+base['rendered']))):
            cases.extend(dict(base, **{key: value}) for value in values)
        for key, values in (('type', ('hook_success', 'hook_system_message', 'queued_command')),
                ('hookName', ('PreToolUse', None)), ('hookEvent', ('PreToolUse', None)),
                ('toolUseID', ('', None, 1)), ('blockingError', (None, self.text, {}))):
            cases.extend(dict(base, attachment=dict(base['attachment'], **{key: value}))
                for value in values)
        for row in cases:
            with self.subTest(row=row):
                self.rows = [row]
                self.assertEqual(self.observe(), {})

    def test_stop_blocking_receipt_requires_matching_system_rendering(self):
        base = stop_blocking_attachment(self.text)
        rendered = base['rendered'][0]['content']
        for content in (self.text, '<system-reminder>'+self.text+'</system-reminder>',
                rendered.replace('Stop hook blocking error', 'PreToolUse hook blocking error'),
                rendered.replace('fixture Stop adapter', 'different command'),
                rendered.replace('request-id: emission-1', 'request-id: foreign'),
                rendered+'\necho', rendered+'\n'+rendered):
            with self.subTest(content=content):
                self.rows = [dict(base, rendered=[dict(content=content)])]
                self.assertEqual(self.observe(), {})
        for value in (None, '', 1):
            with self.subTest(command=value):
                row = deepcopy(base)
                row['attachment']['blockingError']['command'] = value
                self.rows = [row]
                self.assertEqual(self.observe(), {})

    def test_stop_blocking_receipt_preserves_all_request_and_record_bindings(self):
        for key in ('session_id', 'plan_id', 'task_id', 'fingerprint', 'request_id'):
            with self.subTest(key=key):
                self.rows = [stop_blocking_attachment(self.text)]
                old = self.identity[key]
                self.identity[key] = 'foreign'
                self.assertEqual(self.observe(), {})
                self.identity[key] = old
        for key, value in (('uuid', ''), ('timestamp', 'invalid'),
                ('timestamp', '2026-10-15T05:28:14')):
            with self.subTest(key=key, value=value):
                self.rows = [dict(stop_blocking_attachment(self.text), **{key: value})]
                self.assertEqual(self.observe(), {})
        for field in ('continuation', 'plan-id', 'task-id', 'request-fingerprint', 'request-id'):
            line = next(line for line in self.text.splitlines() if line.startswith(field+': '))
            for text in (self.text+'\n'+line, self.text.replace(line+'\n', '')
                    if not self.text.endswith(line) else self.text.removesuffix(line)):
                with self.subTest(field=field, text=text):
                    self.rows = [stop_blocking_attachment(text)]
                    self.assertEqual(self.observe(), {})
        for text in ('echo\n'+self.text, self.text+'\n'+self.text):
            self.rows = [stop_blocking_attachment(text)]
            self.assertEqual(self.observe(), {})

    def test_meta_user_and_generic_stop_system_echoes_cannot_acknowledge(self):
        base = stop_blocking_attachment(self.text)
        self.rows = [dict(type='user', isMeta=True, sessionId=base['sessionId'],
            uuid='meta-user', timestamp=base['timestamp'],
            message=dict(role='user', content='Stop hook feedback:\n'+self.text))]
        self.assertEqual(self.observe(), {})
        for kind in ('hook_system_message', 'hook_success'):
            self.rows = [dict(base, attachment=dict(type=kind, hookName='Stop', hookEvent='Stop',
                toolUseID=base['attachment']['toolUseID'], content=self.text))]
            self.assertEqual(self.observe(), {})

    def test_stop_receipt_keeps_first_original_and_rejects_partial_append(self):
        first = stop_blocking_attachment(self.text)
        later = dict(first, uuid='later', timestamp='2026-10-15T06:00:00Z')
        self.path.write_text('\n'+json.dumps(first)+'\n'+json.dumps(later)+'\n')
        receipts = self.observe(write=False)
        self.assertIn('task', receipts, 'The first actual Stop blocking envelope must be retained')
        receipt = receipts['task']
        self.assertEqual(receipt['verified_at'], first['timestamp'])
        self.assertEqual(receipt['locator'], str(self.path)+':2')
        for suffix in ('', '\n{', '\nnull\n'):
            self.path.write_text(json.dumps(first)+suffix)
            self.assertEqual(self.observe(write=False), {})

    def test_native_text_blocks_are_supported(self):
        self.row['message']['content'] = [dict(type='text', text=line) for line in self.text.splitlines()]
        self.assertEqual(self.observe()['task']['status'], 'observed')

    def test_human_sdk_peer_and_unattributed_echoes_cannot_acknowledge(self):
        cases = [dict(origin=dict(kind='human'), promptSource='sdk', turnOrigin='human'),
            dict(origin=None, entrypoint='sdk-cli', userType='external', promptSource='sdk', turnOrigin='sdk'),
            dict(origin=dict(kind='peer', handback=True, senderTaskId='worker', **{'from': 'worker'}),
                promptSource='system', turnOrigin='peer'),
            dict(origin=None, isMeta=True), dict(origin=None)]
        for changes in cases:
            with self.subTest(changes=changes):
                self.rows = [dict(self.row, **changes)]
                self.assertEqual(self.observe(), {})

    def test_native_actor_metadata_must_be_complete(self):
        for changes in (dict(promptSource='sdk'), dict(turnOrigin='human'),
                dict(origin=dict(kind='task-notification', producer='other')),
                dict(origin=dict(kind='human', producer='session-task')), dict(isSidechain=True),
                dict(message=dict(role='assistant', content=self.text))):
            with self.subTest(changes=changes):
                self.rows = [dict(self.row, **changes)]
                self.assertEqual(self.observe(), {})

    def test_tool_output_and_tool_blocks_cannot_acknowledge(self):
        for content in ([dict(type='tool_result', content=self.text)],
                [dict(type='text', text=self.text), dict(type='tool_result', content=self.text)],
                [dict(type='text', text=self.text), 'echo']):
            with self.subTest(content=content):
                self.rows = [dict(self.row, message=dict(role='user', content=content))]
                self.assertEqual(self.observe(), {})

    def test_attachment_needs_queued_notification_origin(self):
        base = dict(type='queued_command', commandMode='task-notification',
            origin=deepcopy(self.row['origin']), prompt=self.text)
        for changes in (dict(type='other'), dict(commandMode='prompt'), dict(origin=None),
                dict(origin=dict(kind='task-notification', producer='other'))):
            with self.subTest(changes=changes):
                self.rows = [dict(self.row, type='attachment', attachment=dict(base, **changes))]
                self.assertEqual(self.observe(), {})

    def test_every_queued_identity_is_bound_exactly(self):
        for key in ('session_id', 'plan_id', 'task_id', 'fingerprint', 'request_id'):
            with self.subTest(key=key):
                old = self.identity[key]
                self.identity[key] = 'foreign'
                self.assertEqual(self.observe(), {})
                self.identity[key] = old

    def test_missing_and_duplicate_identity_lines_remain_unknown(self):
        for field in ('continuation', 'plan-id', 'task-id', 'request-fingerprint', 'request-id'):
            for operation in ('missing', 'duplicate'):
                with self.subTest(field=field, operation=operation):
                    lines = self.text.splitlines()
                    value = next(line for line in lines if line.startswith(field + ': '))
                    text = '\n'.join(line for line in lines if line != value) if operation == 'missing' else self.text + '\n' + value
                    self.rows = [dict(self.row, message=dict(role='user', content=text))]
                    self.assertEqual(self.observe(), {})

    def test_text_marker_is_not_enough_without_exact_envelope(self):
        for text in ('echo\n' + self.text, self.text.replace('approved-task', 'unapproved-task'),
                self.text.replace('[autopilot]', 'autopilot'), self.text + '\n' + self.text):
            with self.subTest(text=text):
                self.rows = [dict(self.row, message=dict(role='user', content=text))]
                self.assertEqual(self.observe(), {})

    def test_missing_record_identity_or_original_time_remains_unknown(self):
        for key, values in (('uuid', (None, '', 1, [])), ('timestamp',
                (None, 1, [], {}, 'invalid', '2026-10-14T12:00:00'))):
            for value in values:
                with self.subTest(key=key, value=value):
                    self.rows = [dict(self.row, **{key: value})]
                    self.assertEqual(self.observe(), {})

    def test_first_receipt_preserves_original_time_and_blank_line_coordinate(self):
        later = dict(self.row, uuid='later-message', timestamp='2026-10-14T13:00:00Z')
        self.path.write_text('\n' + json.dumps(self.row) + '\n' + json.dumps(later) + '\n')
        receipt = self.observe(write=False)['task']
        self.assertEqual(receipt['verified_at'], self.row['timestamp'])
        self.assertEqual(receipt['locator'], str(self.path) + ':2')
        self.assertEqual(receipt['native_message_id'], 'native-message')

    def test_malformed_or_uncommitted_transcript_never_acknowledges(self):
        valid = json.dumps(self.row)
        for raw in (valid + '\n{', valid, valid + '\n[]\n', valid + '\nnull\n'):
            with self.subTest(raw=raw):
                self.path.write_text(raw)
                self.assertEqual(self.observe(write=False), {})

    def test_missing_or_relative_transcript_path_remains_unknown(self):
        observer = getattr(autopilot_native_receipt, 'observe_claude_receipts', None)
        self.assertTrue(callable(observer), 'Claude native receipt reader is absent')
        for path in (str(self.path), 'relative.jsonl', None, 17):
            with self.subTest(path=path):
                self.assertEqual(observer(path, session_id=self.identity['session_id'],
                    plan_id=self.identity['plan_id'], requests={}), {})


if __name__ == '__main__':
    unittest.main()
