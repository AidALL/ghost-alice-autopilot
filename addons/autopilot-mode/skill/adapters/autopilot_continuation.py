"""Approved task scheduling after v2 application, separate from response closure.

Core's current owned contract admits criteria, not an executable dependency plan.
This explicit plan API supplies the missing mapping without deriving permission
from an unmet flag. Native Stop dispatches one bounded host continuation.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import PurePath

import autopilot_completion_contract as contract
import autopilot_completion_service as service
import autopilot_storage as storage
from autopilot_work_items import validate_work_items, derive_ready_queue


def _key(ref):
    return ref['work_unit_id'], ref['contract_revision'], ref.get('criterion_id', ref.get('id'))


def _selection_ok(selection):
    return selection['status'] == 'selected' or selection.get('reason') in {
        'no-current-unmet-criteria', 'response-no-publication'}


def _surface_allowed(surface, allowed):
    # Parent traversal cannot inherit the lexical ancestor's approval.
    if '..' in PurePath(surface).parts:
        return False
    return any('..' not in PurePath(root).parts and (surface == root or
        (root.endswith('/...') and (surface == root[:-4] or surface.startswith(root[:-3]))))
        for root in allowed)


def register_continuation_plan(source, *, expected_input_event_id, items, approval_evidence, allowed_surfaces):
    """Validate and store a supplied approved plan against current selected criteria.

    Installed references/continuation.md owns the model's complete caller procedure.
    Plan admission may precede the current response review or Stop application;
    advance_continuation determines execution eligibility after application.
    Invalid input raises ValueError. An identical plan retains dispatch history.
    """

    if (not isinstance(approval_evidence, dict) or approval_evidence.get('decision') not in {'approved','approve','go'}
            or not isinstance(approval_evidence.get('source'), str) or not approval_evidence['source'].strip()):
        raise ValueError('continuation requires explicit approval evidence')
    if not isinstance(allowed_surfaces,list) or not allowed_surfaces or not all(isinstance(p,str) and p for p in allowed_surfaces):
        raise ValueError('continuation requires explicit allowed surfaces')
    if service._v2_run_is_paused(source):
        raise ValueError('Ghost-ALICE Autopilot is stopped')
    current,root,target,material,selection=service._v2_material(source,expected_input=expected_input_event_id)
    if not _selection_ok(selection) or not material.get('selected_work_units'):
        raise ValueError('continuation requires current selected execution authority')
    tasks=validate_work_items(copy.deepcopy(items))
    if not tasks: raise ValueError('continuation requires an explicit nonempty task plan')
    owned={_key(c):contract.definition(c) for c in material['intent_state']['acceptance_criteria']}
    mapped=set()
    for task in tasks:
        refs=task.get('criterion_refs')
        if not isinstance(refs,list) or not refs or any(not isinstance(r,dict) for r in refs):
            raise ValueError('each task needs exact owned criterion_refs')
        if any(set(r)!={'work_unit_id','contract_revision','criterion_id'} or _key(r) not in owned for r in refs):
            raise ValueError('task criterion is not in the current owned selected contract')
        if len({_key(r) for r in refs}) != len(refs):
            raise ValueError('task criterion mapping is ambiguous')
        if mapped.intersection(_key(r) for r in refs):
            raise ValueError('criterion mapping across multiple tasks is ambiguous')
        mapped.update(_key(r) for r in refs)
        if not task['allowed_surface'] or any(not _surface_allowed(p,allowed_surfaces) for p in task['allowed_surface']):
            raise ValueError('task exceeds approved allowed surfaces')
        if task['status'] in {'completed','not_applicable'}:
            raise ValueError('plan admission cannot assert task completion')
    # A cycle cannot be made runnable by retrying or requesting publication.
    graph={t['id']:t['depends_on'] for t in tasks}
    def visit(identity, stack):
        if identity in stack: raise ValueError('task dependency cycle')
        for dep in graph[identity]: visit(dep,stack|{identity})
    for identity in graph: visit(identity,set())
    binding,reference,snapshots=service._v2_contracts(current,root,target,material)
    definition=dict(schema_version='autopilot-continuation-plan.v2',target=reference,
        contracts=snapshots,items=tasks,approval_evidence=copy.deepcopy(approval_evidence),allowed_surfaces=allowed_surfaces)
    identity=contract.digest(definition)
    with service._v2_recording_transaction(root,current,target) as store:
        if store is None: raise ValueError('continuation target unavailable')
        _,_,_,live,selected=service._v2_material(current,expected_input=expected_input_event_id,transaction=store.connection)
        if not _selection_ok(selected) or live['intent_state']!=material['intent_state']:
            raise ValueError('current continuation contract changed during admission')
        previous=store.continuation_plan()
        if previous and previous['plan_id']==identity:
            return dict(status='admitted',plan_id=identity)
        plan=dict(definition,plan_id=identity,binding=binding,dispatches={},events=[],
            admitted_at=datetime.now(timezone.utc).isoformat())
        store.completion_put('continuation-plan',definition)
        store.set_continuation_plan(plan)
    return dict(status='admitted',plan_id=identity)


def advance_continuation(source, payload, *, hook_input=None):
    """Dispatch an approved dependency-ready task after privileged criterion application."""
    if service._v2_run_is_paused(source): return payload
    current,root,target,material,selection=service._v2_material(source,claim_scope=service._v2_claim_scope(hook_input))
    if not _selection_ok(selection) or not material.get('selected_work_units'):
        return payload
    # Without a descriptor, the adapter returns without creating a continuation plan.
    if not (root/storage.AUTHORITY_FILE).exists(): return payload
    plan=storage.read_continuation_plan(root,service._v2_authority(current))
    if plan is None: return payload
    with service._v2_recording_transaction(root,current,target) as store:
        if store is None: return payload
        plan=store.continuation_plan()
        _,_,_,material,selection=service._v2_material(current,transaction=store.connection,
            claim_scope=service._v2_claim_scope(hook_input))
        binding,reference,snapshots=service._v2_contracts(current,root,target,material)
        if not _selection_ok(selection) or reference!=plan['target'] or snapshots!=plan['contracts']:
            return dict(payload,continuation=dict(status='parked',reason='current-plan-contract-changed'))
        statuses={_key(c):c.get('status') for c in material['intent_state']['acceptance_criteria']}
        results={r['work_unit_id']:r for r in payload.get('completion_recording',{}).get('units',[])}
        items=validate_work_items(plan['items'])
        unavailable={_key(c) for result in results.values() for c in result.get('retained_coverage_errors',[])}
        def recording_unavailable(ref):
            result=results.get(ref['work_unit_id'],{})
            if result.get('retained_coverage_errors'):
                return _key(ref) in unavailable
            if 'affected_criteria' in result:
                return _key(ref) in {_key(c) for c in result['affected_criteria']}
            return bool(result.get('reason'))
        blocked={t['id'] for t in items if any(recording_unavailable(r) for r in t['criterion_refs'])}
        while True:
            descendants={t['id'] for t in items if any(d in blocked for d in t['depends_on'])}
            if descendants<=blocked:break
            blocked.update(descendants)
        for item in items:
            refs=item['criterion_refs']
            if item['status']=='completed' and any(statuses.get(_key(r))!='met' for r in refs):
                item['status']='reopened'
                plan['dispatches'].pop(item['id'],None)
                plan['events'].append(dict(event='task-proof-invalidated',task_id=item['id'],criterion_refs=refs))
            applied=all(results.get(r['work_unit_id'],{}).get('status')=='applied'
                and not recording_unavailable(r) for r in refs)
            if item['status'] not in {'blocked','stopped','completed'} and applied and all(statuses.get(_key(r))=='met' for r in refs):
                item['status']='completed'
                plan['events'].append(dict(event='task-applied',task_id=item['id'],criterion_refs=refs,
                    application_references=sorted({results[r['work_unit_id']]['application_reference'] for r in refs})))
        ready=derive_ready_queue(items)
        by_id={t['id']:t for t in items}
        # A running task may resume once after real owning-criterion progress;
        # input timestamps and response text are deliberately absent.
        candidates=[t for t in items if t['id'] in ready or (t['status']=='running'
            and all(by_id[d]['status']=='completed' for d in t['depends_on']))]
        selected=None
        for task in candidates:
            if task['id'] in blocked:continue
            if task['status']=='running' and any(recording_unavailable(r) for r in task['criterion_refs']): continue
            fingerprint=contract.digest(dict(task={k:v for k,v in task.items() if k not in {'status','completion'}},
                criteria=[dict(ref=r,status=statuses.get(_key(r))) for r in task['criterion_refs']]))
            if plan['dispatches'].get(task['id'])==fingerprint: continue
            selected=task
            task['status']='running'
            plan['dispatches'][task['id']]=fingerprint
            plan['events'].append(dict(event='task-dispatched',task_id=task['id'],fingerprint=fingerprint,
                criterion_refs=task['criterion_refs'],input_event_id=binding['input_event_id']))
            break
        plan['items']=items
        store.set_continuation_plan(plan)
    if selected:
        message='\n'.join(['[autopilot]','continuation: approved-task','task-id: '+selected['id'],
            'Ghost-ALICE Autopilot advance_continuation selected the approved dependency-ready task identified by task-id and recorded dispatch. The task prompt is addressed to the assistant in the native conversation identified by the hook input. If the host supplies that feedback, the current-session assistant follows task-router and boundary-contract to request work tools within allowed-surfaces; dispatch does not prove execution or whole-work completion:',
            selected['prompt'],'allowed-surfaces: '+', '.join(selected['allowed_surface']),
            'criterion-refs: '+str(selected['criterion_refs']),
            'Preserve prior applied proof/source/time. Before claiming results for newly affected criteria, follow installed verification-before-completion/references/autopilot-publication.md, section Prepare and record the response review. Ghost-ALICE Autopilot handles authenticated supported proof publication at native Stop.'])
        # Queue dispatch is the current action; recording details remain structured.
        return dict(payload,systemMessage=message,continuation=dict(status='dispatched',task_id=selected['id'],
            criterion_refs=selected['criterion_refs'],plan_id=plan['plan_id']))
    status='complete' if not blocked and all(t['status']=='completed' for t in items) else 'parked' if candidates or blocked else 'waiting'
    return dict(payload,continuation=dict(status=status,plan_id=plan['plan_id']))
