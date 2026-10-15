"""Read exact native host receipts. Dependencies: Python 3.11+ standard library.

Hook input supplies the native path; the session owner and queued identities
come from the authenticated plan. A receipt establishes delivery only.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path


def observe_codex_receipts(transcript, *, session_id, plan_id, requests):
    """Return matched native HookPrompt observations from one immutable read.

Missing, malformed or foreign evidence returns no acknowledgments. Ordinary
user messages, tool-output echoes and compacted history are not host receipts.
"""
    if not isinstance(transcript, str) or not Path(transcript).is_absolute():
        return {}
    try:
        data = Path(transcript).read_bytes()
        rows = [(line, json.loads(body)) for line, body in enumerate(data.splitlines(), 1) if body.strip()]
    except (OSError, ValueError, UnicodeError):
        return {}
    if not rows or any(not isinstance(row, dict) for _, row in rows):
        return {}
    owners = [row.get('payload', {}).get('id') for _, row in rows
        if row.get('type') == 'session_meta' and isinstance(row.get('payload'), dict)]
    if not owners or any(owner != session_id for owner in owners):
        return {}
    expected = {(request.get('task_id',task), request['fingerprint'], request['request_id']):task for task, request in requests.items()
        if (isinstance(request, dict) and isinstance(request.get('fingerprint'), str)
            and isinstance(request.get('task_id',task), str)
            and isinstance(request.get('request_id'), str) and request['request_id'])}
    found = {}
    for line, row in rows:
        event = row.get('payload')
        if row.get('type') != 'event_msg' or not isinstance(event, dict):
            continue
        item = event.get('item')
        if (event.get('type') != 'item_completed' or event.get('thread_id') != session_id
                or not isinstance(event.get('turn_id'), str) or not event['turn_id']
                or not isinstance(item, dict) or item.get('type') != 'HookPrompt'
                or not isinstance(item.get('id'), str) or not item['id']):
            continue
        fragments = item.get('fragments')
        if (not isinstance(fragments, list) or not fragments or
                any(not isinstance(f, dict) or not isinstance(f.get('text'), str) for f in fragments)):
            continue
        text = '\n'.join(f['text'] for f in fragments)
        fields = {}
        for key in ('continuation', 'plan-id', 'task-id', 'request-fingerprint', 'request-id'):
            values = [part[len(key)+2:] for part in text.splitlines() if part.startswith(key+': ')]
            if len(values) == 1:
                fields[key] = values[0]
        task, fingerprint = fields.get('task-id'), fields.get('request-fingerprint')
        if (not text.startswith('[autopilot]\n') or fields.get('continuation') != 'approved-task'
                or fields.get('plan-id') != plan_id
                or (task, fingerprint, fields.get('request-id')) not in expected):
            continue
        timestamp = row.get('timestamp')
        try:
            original_time = datetime.fromisoformat(timestamp) if isinstance(timestamp, str) else None
        except ValueError:
            original_time = None
        if original_time is None or original_time.utcoffset() is None:
            continue
        # Retain the first delivered original time; later echoes cannot refresh it.
        found.setdefault(expected[(task,fingerprint,fields['request-id'])], dict(status='observed', fingerprint=fingerprint,
            request_id=fields['request-id'],
            locator=transcript+':'+str(line), hook_prompt_id=item['id'], turn_id=event['turn_id'],
            verified_at=timestamp, event_digest='sha256:'+hashlib.sha256(
                json.dumps(row, sort_keys=True, separators=(',', ':')).encode()).hexdigest()))
    return found


def _claude_notification_text(row):
    """Select only Core's positively identified session-task notification actor."""
    if row.get('isSidechain'):
        return None
    if row.get('type') == 'attachment':
        message = row.get('attachment')
        if (not isinstance(message, dict) or message.get('type') != 'queued_command'
                or message.get('commandMode') != 'task-notification'):
            return None
        content = message.get('prompt')
    elif row.get('type') == 'user':
        message = row
        body = row.get('message')
        if (row.get('promptSource') != 'system' or row.get('turnOrigin') != 'task_notification'
                or not isinstance(body, dict) or body.get('role') != 'user'):
            return None
        content = body.get('content')
    else:
        return None
    origin = message.get('origin')
    if (not isinstance(origin, dict) or origin.get('kind') != 'task-notification'
            or origin.get('producer') != 'session-task'):
        return None
    if isinstance(content, str):
        return content
    if (not isinstance(content, list) or not content or any(not isinstance(block, dict)
            or block.get('type') != 'text' or not isinstance(block.get('text'), str)
            or set(block) - {'type', 'text', 'cache_control'} for block in content)):
        return None
    return '\n'.join(block['text'] for block in content)


def _claude_stop_blocking_text(row):
    """Select the observed native Stop attachment rendered to the main model."""
    if (row.get('type') != 'attachment' or row.get('isSidechain') is not False
            or row.get('renderedRole') != 'system'):
        return None
    attachment = row.get('attachment')
    if (not isinstance(attachment, dict) or attachment.get('type') != 'hook_blocking_error'
            or attachment.get('hookName') != 'Stop' or attachment.get('hookEvent') != 'Stop'
            or not isinstance(attachment.get('toolUseID'), str) or not attachment['toolUseID']):
        return None
    error = attachment.get('blockingError')
    if (not isinstance(error, dict) or not isinstance(error.get('blockingError'), str)
            or not isinstance(error.get('command'), str) or not error['command']):
        return None
    text = error['blockingError']
    rendered = row.get('rendered')
    expected = ('<system-reminder>\nStop hook blocking error from command: "'
        + error['command'] + '": ' + text + '\n</system-reminder>')
    if (not isinstance(rendered, list) or len(rendered) != 1
            or not isinstance(rendered[0], dict) or set(rendered[0]) != {'content'}
            or rendered[0]['content'] != expected):
        return None
    return text


def observe_claude_receipts(transcript, *, session_id, plan_id, requests):
    """Match queued identities in native Claude notifications or Stop blocks.

    Each accepted record binds the main session, native actor, message identity
    and original time. A human, SDK, peer, tool or unattributed hook echo cannot
    acknowledge delivery. Unsupported native delivery remains unknown; this
    observation grants no execution authority and proves no task execution.
    """
    if (not isinstance(transcript, str) or not Path(transcript).is_absolute()
            or not isinstance(session_id, str) or not session_id
            or not isinstance(plan_id, str) or not plan_id or not isinstance(requests, dict)):
        return {}
    try:
        data = Path(transcript).read_bytes()
        # A trailing partial append is not a committed native record.
        if not data.endswith(b'\n'):
            return {}
        rows = [(line, json.loads(body)) for line, body in enumerate(data.splitlines(), 1) if body.strip()]
    except (OSError, ValueError, UnicodeError):
        return {}
    if any(not isinstance(row, dict) for _, row in rows):
        return {}
    expected = {(request.get('task_id',task), request['fingerprint'], request['request_id']):task for task, request in requests.items()
        if (isinstance(task, str) and task and isinstance(request, dict)
            and isinstance(request.get('task_id',task), str)
            and isinstance(request.get('fingerprint'), str) and request['fingerprint']
            and isinstance(request.get('request_id'), str) and request['request_id'])}
    found = {}
    for line, row in rows:
        if (row.get('sessionId') != session_id or not isinstance(row.get('uuid'), str)
                or not row['uuid']):
            continue
        text = _claude_notification_text(row)
        actor = 'notification'
        if text is None:
            text = _claude_stop_blocking_text(row)
            actor = 'stop-hook-blocking-error'
        if (not isinstance(text, str) or not text.startswith('[autopilot]\n')
                or text.splitlines().count('[autopilot]') != 1):
            continue
        fields = {}
        for key in ('continuation', 'plan-id', 'task-id', 'request-fingerprint', 'request-id'):
            values = [part[len(key)+2:] for part in text.splitlines() if part.startswith(key+': ')]
            if len(values) == 1:
                fields[key] = values[0]
        task, fingerprint = fields.get('task-id'), fields.get('request-fingerprint')
        if (fields.get('continuation') != 'approved-task' or fields.get('plan-id') != plan_id
                or (task, fingerprint, fields.get('request-id')) not in expected):
            continue
        timestamp = row.get('timestamp')
        try:
            original_time = datetime.fromisoformat(timestamp) if isinstance(timestamp, str) else None
        except ValueError:
            original_time = None
        if original_time is None or original_time.utcoffset() is None:
            continue
        found.setdefault(expected[(task,fingerprint,fields['request-id'])], dict(status='observed', fingerprint=fingerprint,
            request_id=fields['request-id'], locator=transcript+':'+str(line),
            native_platform='claude', native_actor=actor, native_message_id=row['uuid'],
            verified_at=timestamp, event_digest='sha256:'+hashlib.sha256(
                json.dumps(row, sort_keys=True, separators=(',', ':')).encode()).hexdigest()))
    return found
