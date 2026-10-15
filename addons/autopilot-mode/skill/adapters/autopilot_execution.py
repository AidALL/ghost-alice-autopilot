"""Task-bound actions inside native tools. Dependencies: Python 3.11+ and sibling Core/Autopilot adapters.

This wrapper runs no model and grants no tool permission. The current assistant
uses it in the native tool call performing an already authorized action. It
authenticates the declared task/resources, records actual invocation outcomes,
and never treats unknown host receipt as a prerequisite or completes criteria.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import copy

import autopilot_completion_service as service
import autopilot_delivery as delivery
import autopilot_storage as storage
from autopilot_continuation import _admitted_plan_items, _feedback_tasks, _key, _request_fingerprint, _require_current_surfaces, _selection_ok


def _resources_allowed(resources, allowed):
    if not isinstance(resources, list) or not resources:
        return False
    for value in resources:
        if not isinstance(value, str) or not Path(value).is_absolute() or '..' in Path(value).parts:
            return False
        resource = Path(value).resolve()
        # Admission freezes these roots. Re-resolving them here would move
        # approval when a symlink or the action process's cwd changes.
        roots = [(Path(root[:-3] if root.endswith('/...') else root), root.endswith('/...'))
                 for root in allowed]
        if any(not root.is_absolute() or '..' in root.parts for root,_ in roots):
            return False
        if not any(resource == root or (recursive and resource.is_relative_to(root))
                   for root, recursive in roots):
            return False
    return True


def _authorize(store, current, root, target, *, expected_input_event_id, plan_id, task_id, request_id, resources):
    _, _, _, material, selection = service._v2_material(current, transaction=store.connection,
        expected_input=expected_input_event_id)
    plan = store.continuation_plan()
    if not plan or plan['plan_id'] != plan_id or not _selection_ok(selection):
        raise ValueError('current continuation plan unavailable')
    items = _admitted_plan_items(current,root,target,store,plan)
    _require_current_surfaces(plan, items)
    binding, reference, snapshots = service._v2_contracts(current, root, target, material)
    if reference != plan['target'] or snapshots != plan['contracts']:
        raise ValueError('current continuation contract changed')
    route = material['response_route']
    if route.get('boundary_ref') != plan.get('boundary_ref'):
        raise ValueError('current continuation boundary changed')
    by_id = {item['id']:item for item in items}
    task = by_id.get(task_id)
    if (task is None or task['status'] not in {'ready', 'reopened', 'running'}
            or any(by_id[dep]['status'] != 'completed' for dep in task['depends_on'])):
        raise ValueError('task is not dependency-ready')
    needed, issue, _ = _feedback_tasks(route, items)
    if issue or task_id not in needed or needed[task_id][1]['prerequisite'] != 'ready':
        raise ValueError('current necessary task is unavailable or stopped')
    request = delivery.require_request(store,plan,task,request_id,
        _request_fingerprint(task,plan['boundary_ref'],needed[task_id][1]),material)
    plan.setdefault('delivery_requests', {})[task_id] = request
    if not _resources_allowed(resources, task['allowed_surface']):
        raise ValueError('action resources exceed the approved task')
    # A queued request is not proof that its prerequisites remain valid at the
    # action boundary. Check only this task's transitive parents so unrelated
    # work can still proceed when another proof changes.
    parents = set(task['depends_on'])
    while True:
        ancestors = {dep for identity in parents for dep in by_id[identity]['depends_on']}
        if ancestors <= parents:
            break
        parents.update(ancestors)
    required_keys = {_key(ref) for identity in parents for ref in by_id[identity]['criterion_refs']}
    statuses = {_key(row): row.get('status') for row in material['intent_state']['acceptance_criteria']}
    if any(statuses.get(key) != 'met' for key in required_keys):
        raise ValueError('task prerequisite criteria are not met')
    for unit_id in {key[0] for key in required_keys}:
        state = store.completion_unit(unit_id)
        if state is None:
            raise ValueError('task prerequisite proof unavailable')
        retained = service._retained_met_evidence(store, state, current, root, target, material,
            expected_unit=unit_id, invalidate=False, criterion_keys=required_keys)
        if retained.changed or retained.errors:
            raise ValueError('task prerequisite proof changed or unavailable')
    return plan, task, request, binding


def _finish(root, current, target, start, *, outcome, error_type=None):
    if storage.active() is not None:
        raise ValueError('execution recording requires its own durable transaction')
    record = dict(start, outcome=outcome, finished_at=datetime.now(timezone.utc).isoformat())
    if error_type is not None:
        record['error_type'] = error_type
    with service._v2_recording_transaction(root, current, target) as store:
        if store is None:
            raise ValueError('execution recording unavailable')
        reference = store.completion_put('continuation-action-finished', record)
        plan = store.continuation_plan()
        # A correction/replan during the tool action cannot transfer its outcome
        # to a new emission. The original immutable outcome remains available.
        if plan and plan['plan_id'] == start['plan_id']:
            request = plan.get('delivery_requests', {}).get(start['task_id'])
            if isinstance(request, dict) and request.get('request_id') == start['request_id']:
                request.setdefault('actions', {})[start['action_id']] = dict(record, evidence_ref=reference)
                request['execution'] = 'observed'
            plan['events'].append(dict(event='continuation-action-finished', task_id=start['task_id'],
                request_id=start['request_id'], action_id=start['action_id'], evidence_ref=reference,
                outcome=outcome))
            delivery.project(store, plan)
            store.set_continuation_plan(plan)
    return dict(record, evidence_ref=reference)


@service._with_core_policy_source
def execute_continuation_action(source, *, expected_input_event_id, plan_id, task_id, request_id,
                                action_id, operation, resources, action):
    """Run one current approved action, preserving its native returned value.

Caller IDs come from the actual queued request/current receipt. operation is a
short semantic label; resources declare paths used by that action. The callback
is the action already permitted by the native host, not an arbitrary dispatch
grant. Returned/raised execution is not business completion. Duplicate action
IDs retain proof and do not replay the callback or return fabricated output.
"""
    if (not callable(action) or any(not isinstance(v, str) or not v.strip() for v in
            (expected_input_event_id, plan_id, task_id, request_id, action_id, operation))
            or len(action_id) > 128 or len(operation) > 256):
        raise ValueError('explicit action identity, operation and callable are required')
    if storage.active() is not None:
        raise ValueError('execute action outside an ambient storage transaction')
    if service._v2_run_is_paused(source):
        raise ValueError('Ghost-ALICE Autopilot is stopped')
    resources = copy.deepcopy(resources)
    current, root, target, _, _ = service._v2_material(source, expected_input=expected_input_event_id)
    with service._v2_recording_transaction(root, current, target) as store:
        if store is None:
            raise ValueError('task action authority unavailable')
        plan, task, request, binding = _authorize(store, current, root, target,
            expected_input_event_id=expected_input_event_id, plan_id=plan_id, task_id=task_id,
            request_id=request_id, resources=resources)
        previous = store.continuation_action(plan_id=plan_id, task_id=task_id,
            request_id=request_id, action_id=action_id)
        if previous is not None:
            if previous['operation'] != operation or previous['resources'] != resources:
                raise ValueError('action identity was reused with changed operation/resources')
            if previous.get('outcome') == 'returned':
                if delivery.project(store, plan): store.set_continuation_plan(plan)
                return dict(status='already-recorded', execution=previous, result=None)
            raise ValueError('action already attempted; inspect its original outcome before retry')
        actions = request.setdefault('actions', {})
        if not isinstance(actions, dict) or action_id in actions:
            raise ValueError('mutable action history has no authenticated immutable attempt')
        start = dict(schema_version='autopilot-task-action.v1', plan_id=plan_id, task_id=task_id,
            request_id=request_id, action_id=action_id, operation=operation, resources=resources,
            binding=binding, started_at=datetime.now(timezone.utc).isoformat(), outcome='started')
        reference = store.completion_put('continuation-action-started', start)
        actions[action_id] = dict(start, evidence_ref=reference)
        request['execution'] = 'started'
        task['status'] = 'running'
        plan['items'] = [task if item['id'] == task_id else item for item in plan['items']]
        plan['events'].append(dict(event='continuation-action-started', task_id=task_id,
            request_id=request_id, action_id=action_id, evidence_ref=reference))
        delivery.project(store, plan)
        store.set_continuation_plan(plan)
    try:
        result = action()
    except BaseException as exc:
        try:
            _finish(root, current, target, start, outcome='raised', error_type=type(exc).__name__)
        except Exception as recording_error:
            # Keep the original action error; recording cannot replace it.
            exc.add_note('Autopilot execution recording unavailable: '+type(recording_error).__name__)
        raise
    try:
        finished = _finish(root, current, target, start, outcome='returned')
    except Exception as exc:
        # Return the real native value even when separate recording fails.
        return dict(status='recording-unavailable', result=result, recording_error=type(exc).__name__)
    return dict(status='recorded', result=result, execution=finished)
