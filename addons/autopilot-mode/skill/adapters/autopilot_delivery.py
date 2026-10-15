"""Immutable emission/receipt history and derived views. Dependencies: Python 3.11+ stdlib.

The scheduler supplies an authenticated plan and current Core material. Only
queue() creates execution-bearing emissions. Native observers supply receipt
facts through record_receipt(); a receipt never authorizes an action.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from uuid import uuid4

import autopilot_completion_contract as contract

PROTOCOL = 'autopilot-delivery-ledger.v1'


def cycle(material, task):
    """Only canonical Core invalidations can start another proof/work cycle."""
    keys = {(r['work_unit_id'], r['contract_revision'], r['criterion_id']) for r in task['criterion_refs']}
    return contract.digest([event for event in material.get('events', [])
        if event.get('event') == 'acceptance-criterion-evidence-invalidated'
        and (event.get('work_unit_id'), event.get('contract_revision'), event.get('criterion_id')) in keys])


def history(store, plan):
    """Authenticate all emissions and observations, including superseded ones."""
    tasks = {task['id'] for task in plan['items']}
    requests, sequences = {}, set()
    for reference, record in store.completion_records('continuation-emission'):
        if record.get('plan_id') != plan['plan_id']:
            continue
        if (record.get('schema_version') != PROTOCOL or record.get('task_id') not in tasks
                or not isinstance(record.get('request_id'), str) or not record['request_id']
                or record['request_id'] in requests or type(record.get('sequence')) is not int
                or record['sequence'] < 1 or record['sequence'] in sequences
                or not all(isinstance(record.get(key), str) and record[key] for key in ('fingerprint', 'cycle'))):
            raise ValueError('immutable emission history is incompatible or ambiguous')
        sequences.add(record['sequence'])
        requests[record['request_id']] = dict(task_id=record['task_id'], request_id=record['request_id'],
            fingerprint=record['fingerprint'], cycle=record['cycle'], sequence=record['sequence'],
            source_locator=copy.deepcopy(record['source_locator']), emission_ref=reference,
            status='pending', host_receipt='unknown', execution='unknown')
    for reference, record in store.completion_records('continuation-host-receipt'):
        if record.get('plan_id') != plan['plan_id']:
            continue
        identity = record.get('request_id')
        if record.get('emission_ref') is None and plan.get('delivery_protocol') is None:
            # An exact native observation can retain legacy delivery, but it
            # cannot create the queued-emission proof required for execution.
            if identity not in requests and record.get('task_id') in tasks:
                requests[identity] = dict(task_id=record['task_id'], request_id=identity,
                    fingerprint=record['fingerprint'], status='unknown', host_receipt='unknown',
                    execution='unknown', origin='legacy-native-observation')
        request = requests.get(identity)
        receipt = record.get('receipt')
        if (record.get('schema_version') != PROTOCOL or not isinstance(request, dict)
                or record.get('task_id') != request['task_id']
                or record.get('emission_ref') != request.get('emission_ref')
                or record.get('fingerprint') != request['fingerprint'] or not isinstance(receipt, dict)
                or receipt.get('request_id') != identity or receipt.get('fingerprint') != request['fingerprint']
                or 'receipt_ref' in request):
            raise ValueError('immutable host receipt differs from its emission')
        request.update(status='received', host_receipt='observed', receipt=copy.deepcopy(receipt), receipt_ref=reference)
    attempts = set()
    for kind in ('continuation-action-started', 'continuation-action-finished'):
        for _, record in store.completion_records(kind):
            if record.get('plan_id') == plan['plan_id']:
                if record.get('request_id') not in requests:
                    if plan.get('delivery_protocol') == PROTOCOL:
                        raise ValueError('immutable action has no queued emission history')
                    continue
                attempts.add((record['task_id'], record['request_id'], record['action_id']))
    for task, request_id, action_id in attempts:
        request = requests[request_id]
        legacy_observation = (plan.get('delivery_protocol') is None
            and request.get('origin') == 'legacy-native-observation')
        if task != request['task_id'] or (not request.get('emission_ref') and not legacy_observation):
            raise ValueError('immutable action has no matching queued emission')
        attempt = store.continuation_action(plan_id=plan['plan_id'], task_id=task,
            request_id=request_id, action_id=action_id)
        request.setdefault('actions', {})[action_id] = attempt
        request['execution'] = ('observed' if attempt['outcome'] != 'started'
            or request['execution'] == 'observed' else 'started')
    return requests


def project(store, plan):
    """Rebuild display state; mutable fields cannot assert delivery/execution."""
    before = copy.deepcopy(plan)
    requests = history(store, plan)
    tasks = {item['id'] for item in plan['items']}
    current = {}
    for request in requests.values():
        task = request['task_id']
        if task not in current or request.get('sequence', 0) > current[task].get('sequence', 0):
            current[task] = copy.deepcopy(request)
    # A valid old pointer may select a prior semantic request again. Its
    # fingerprint and canonical cycle are rechecked at the action boundary.
    for task, value in plan.get('delivery_requests', {}).items():
        if task not in tasks:
            continue
        if isinstance(value, dict) and value.get('request_id') in requests:
            request = requests[value['request_id']]
            if request['task_id'] == task:
                current[task] = copy.deepcopy(request)
        elif isinstance(value, dict):
            current.setdefault(task, dict(status='unknown', host_receipt='unknown', execution='unknown',
                origin='historical-dispatch',
                **{key: value[key] for key in ('request_id', 'fingerprint') if key in value}))
    for task, fingerprint in plan.get('dispatches', {}).items():
        if task in tasks and isinstance(fingerprint, str):
            current.setdefault(task, dict(status='unknown',fingerprint=fingerprint,
                host_receipt='unknown',execution='unknown',origin='historical-dispatch'))
    dispatches = ({task: value for task, value in plan.get('dispatches', {}).items()}
        if plan.get('delivery_protocol') is None else {})
    dispatches.update({task: request['fingerprint'] for task, request in current.items() if 'fingerprint' in request})
    plan.update(delivery_requests=current, dispatches=dispatches, request_history=copy.deepcopy(requests))
    return plan != before


def queue(store, plan, task, fingerprint, source_locator, binding, material):
    """Create one emission per semantic input and authenticated proof cycle."""
    if plan.get('delivery_protocol') != PROTOCOL:
        raise ValueError('legacy plan requires new approved delivery admission')
    requests = history(store, plan)
    epoch = cycle(material, task)
    matches = [request for request in requests.values() if request['task_id'] == task['id']
        and request['fingerprint'] == fingerprint and request.get('cycle') == epoch]
    if len(matches) > 1:
        raise ValueError('immutable emission identity is ambiguous')
    emitted = not matches
    if emitted:
        record = dict(schema_version=PROTOCOL, plan_id=plan['plan_id'], task_id=task['id'],
            request_id=str(uuid4()), fingerprint=fingerprint, cycle=epoch,
            sequence=1+max((r.get('sequence', 0) for r in requests.values()), default=0),
            source_locator=source_locator, binding=binding, queued_at=datetime.now(timezone.utc).isoformat())
        store.completion_put('continuation-emission', record)
        requests = history(store, plan)
        request = requests[record['request_id']]
    else:
        request = matches[0]
    plan.setdefault('delivery_requests', {})[task['id']] = copy.deepcopy(request)
    plan.setdefault('dispatches', {})[task['id']] = fingerprint
    plan['request_history'] = copy.deepcopy(requests)
    return emitted


def require_request(store, plan, task, request_id, fingerprint, material):
    if plan.get('delivery_protocol') != PROTOCOL:
        raise ValueError('authenticated queued emission unavailable for legacy plan')
    request = history(store, plan).get(request_id)
    if (request is None or request['task_id'] != task['id'] or not request.get('emission_ref')
            or request['fingerprint'] != fingerprint or request['cycle'] != cycle(material, task)):
        raise ValueError('current authenticated queued request emission is unavailable or changed')
    return copy.deepcopy(request)


def record_receipt(store, plan, request, receipt):
    if (receipt.get('request_id') != request.get('request_id')
            or receipt.get('fingerprint') != request.get('fingerprint')):
        raise ValueError('native receipt identity differs from queued emission')
    original = history(store, plan).get(request['request_id'])
    if original is not None:
        if any(original.get(key) != request.get(key) for key in ('task_id', 'fingerprint', 'emission_ref')):
            raise ValueError('native receipt request differs from authenticated history')
        if original.get('receipt_ref'):
            return original['receipt_ref']
    record = dict(schema_version=PROTOCOL, plan_id=plan['plan_id'], task_id=request['task_id'],
        request_id=request['request_id'], fingerprint=request['fingerprint'],
        emission_ref=request.get('emission_ref'), receipt=receipt)
    return store.completion_put('continuation-host-receipt', record)
