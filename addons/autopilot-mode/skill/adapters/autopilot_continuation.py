"""Approved task scheduling after v2 application, separate from response closure.

Core's current owned contract admits criteria, not an executable dependency plan.
This explicit plan API supplies the missing mapping without deriving permission
from an unmet flag. Native Stop requests one bounded host continuation and
observes exact supported host delivery receipts separately from execution.
"""
from __future__ import annotations

import copy
import json
from uuid import uuid4
from datetime import datetime, timezone
from pathlib import Path, PurePath

import autopilot_completion_contract as contract
import autopilot_delivery as delivery
import autopilot_completion_service as service
import autopilot_storage as storage
from autopilot_work_items import validate_work_items, derive_ready_queue, _load_core_ledger_module
from autopilot_intent_recovery import _continuation_feedback_shape_valid
from autopilot_native_receipt import observe_claude_receipts, observe_codex_receipts


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


def _canonical_surface(surface):
    recursive = surface.endswith('/...')
    path = Path(surface[:-3] if recursive else surface)
    if '..' in path.parts:
        raise ValueError('parent traversal exceeds approved allowed surfaces')
    canonical = path.resolve().as_posix()
    return canonical.rstrip('/') + '/...' if recursive else canonical


def _admit_surface(surface):
    """Freeze location and descendant permission, including existing directories."""
    canonical = _canonical_surface(surface)
    if not canonical.endswith('/...') and Path(canonical).is_dir():
        return canonical.rstrip('/') + '/...'
    return canonical


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
    allowed_surfaces=[_admit_surface(surface) for surface in allowed_surfaces]
    for task in tasks:
        task['allowed_surface']=[_admit_surface(surface) for surface in task['allowed_surface']]
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
    definition=dict(schema_version='autopilot-continuation-plan.v2',delivery_protocol=delivery.PROTOCOL,target=reference,
        contracts=snapshots,boundary_ref=material['response_route']['boundary_ref'],
        items=tasks,approval_evidence=copy.deepcopy(approval_evidence),allowed_surfaces=allowed_surfaces)
    identity=contract.digest(definition)
    with service._v2_recording_transaction(root,current,target) as store:
        if store is None: raise ValueError('continuation target unavailable')
        _,_,_,live,selected=service._v2_material(current,expected_input=expected_input_event_id,transaction=store.connection)
        if not _selection_ok(selected) or live['intent_state']!=material['intent_state']:
            raise ValueError('current continuation contract changed during admission')
        previous=store.continuation_plan()
        if previous and previous['plan_id']==identity:
            return dict(status='admitted',plan_id=identity)
        plan=dict(definition,plan_id=identity,binding=binding,dispatches={},delivery_requests={},request_history={},events=[],
            admitted_at=datetime.now(timezone.utc).isoformat())
        store.completion_put('continuation-plan',definition)
        store.set_continuation_plan(plan)
    return dict(status='admitted',plan_id=identity)


def _feedback_tasks(route, items):
    """Map current usable necessity groups; one source failure holds only its row."""
    if not _continuation_feedback_shape_valid(route):
        return {}, 'continuation-feedback-incompatible', set()
    feedback = route.get('continuation_feedback')
    if feedback is None:
        return {}, 'continuation-feedback-unavailable', set()
    if feedback['control'] == 'human-stop':
        return {}, 'human-stop', set()
    held = set()
    if 'continuation_feedback_issues' in route:
        issues = route['continuation_feedback_issues']
        if not isinstance(issues, list):
            return {}, 'continuation-feedback-issues-incompatible', set()
        pointers = {'/continuation_feedback/required_unprocessed/'+str(index): index
            for index in range(len(feedback['required_unprocessed']))}
        supported = {'source-unverified', 'source-owner-unbound', 'source-changed', 'source-unavailable'}
        for issue in issues:
            if (not isinstance(issue, dict) or set(issue) != {'pointer', 'issue'}
                    or not isinstance(issue['pointer'], str) or issue['pointer'] not in pointers
                    or not isinstance(issue['issue'], str) or issue['issue'] not in supported
                    or pointers[issue['pointer']] in held):
                return {}, 'continuation-feedback-issues-incompatible', set()
            held.add(pointers[issue['pointer']])
    grouped = {}
    for index, row in enumerate(feedback['required_unprocessed']):
        needed = {_key(r) for r in row['criterion_refs']}
        matches = [task['id'] for task in items
            if needed <= {_key(r) for r in task['criterion_refs']}]
        if len(matches) != 1 or matches[0] in grouped:
            return {}, 'continuation-feedback-unmapped', held
        grouped[matches[0]] = (index, row)
    mapped = {task_id: value for task_id, value in grouped.items()
              if value[0] not in held}
    return mapped, None, held


def _semantic_feedback(row):
    """A candidate wrapper is provenance; original source identity is input."""
    return dict(criterion_refs=sorted(row['criterion_refs'], key=_key),
        reason=row['reason'], next_information=row['next_information'], prerequisite=row['prerequisite'],
        source_refs=sorted(({k: ref['source'][k] for k in
            ('source_id', 'locator', 'digest', 'byte_length', 'appendable') if k in ref['source']}
            for ref in row['source_refs']), key=lambda ref: (ref['source_id'],ref['locator'],ref['digest'],
                ref.get('byte_length',-1),ref.get('appendable',False))))


def _request_fingerprint(task, boundary_ref, row):
    return contract.digest(dict(task_id=task['id'],
        criterion_refs=sorted(task['criterion_refs'],key=_key),
        allowed_surfaces=sorted(task['allowed_surface']),depends_on=sorted(task['depends_on']),
        boundary_ref=boundary_ref,feedback=_semantic_feedback(row)))


def _feedback_locator(current, material, index):
    """Locate the existing immutable Core route declaration, without caching it."""
    core = _load_core_ledger_module(material['state_path'], current)
    if core is None or not callable(getattr(core, 'json_digest', None)):
        raise ValueError('Core response route digest unavailable')
    route = {k: v for k, v in material['response_route'].items() if k not in {'current_input_event_id', 'continuation_feedback_issues'}}
    return dict(input_event_id=route['input_event_id'], response_route_digest=core.json_digest(route),
        pointer='/continuation_feedback/required_unprocessed/'+str(index))


def _admitted_plan_items(current, root, target, store, plan):
    """Validate scheduler authority against its content-addressed admission."""
    try:
        definition = store.completion_read('continuation-plan', plan['plan_id'])
        fixed = {'schema_version', 'target', 'contracts', 'boundary_ref',
            'items', 'approval_evidence', 'allowed_surfaces'}
        if (not isinstance(definition, dict) or set(definition) not in (fixed, fixed | {'delivery_protocol'})
                or definition['schema_version'] != 'autopilot-continuation-plan.v2'
                or definition.get('delivery_protocol') not in (None, delivery.PROTOCOL)
                or plan.get('delivery_protocol') != definition.get('delivery_protocol')
                or any(plan.get(key) != definition[key] for key in fixed - {'items'})):
            raise ValueError('admission definition differs')
        expected = dict(path=target.get('logical_run_dir', str(root)),
            authority=current['GHOST_ALICE_SESSION_INTENT_ROOT'],
            owner=dict(platform=current['GHOST_ALICE_PLATFORM'],
                session_id=current['GHOST_ALICE_SESSION_ID']),
            selection_origin='explicit' if target['reason'] == 'explicit-run-dir' else 'derived')
        contract.validate_target(definition['target'])
        if definition['target'] != expected:
            raise ValueError('admission target differs from the native owner')
        admitted = {task['id']: task for task in validate_work_items(copy.deepcopy(definition['items']))}
        items = validate_work_items(copy.deepcopy(plan['items']))
        live = {task['id']: task for task in items}
        mutable = {'status', 'attempt', 'completion'}
        def approved(task):
            return {key: value for key, value in task.items() if key not in mutable}
        if (set(live) != set(admitted) or any(approved(live[identity]) != approved(admitted[identity])
                                             for identity in admitted)):
            raise ValueError('admitted task definition differs')
        # Admission holds encode authority, unlike ordinary execution progress.
        # Reopening a held task requires a newly admitted approved definition.
        if any(task['status'] in {'stopped', 'blocked'} and live[identity]['status'] != task['status']
               for identity, task in admitted.items()):
            raise ValueError('admitted task hold changed without a new approved plan')
        return items
    except (ValueError, TypeError, KeyError, OSError) as exc:
        raise ValueError('admitted continuation plan unavailable or altered') from exc


def _require_current_surfaces(plan, items):
    """Check present action paths without changing historical admission/receipts."""
    try:
        surfaces = [*plan['allowed_surfaces'],
                    *(surface for task in items for surface in task['allowed_surface'])]
        if any(_canonical_surface(surface) != surface for surface in surfaces):
            raise ValueError('admitted surface is not a stable canonical absolute path')
    except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
        raise ValueError('approved action surfaces changed or unavailable') from exc


def _observe_plan_receipts(current, root, target, store, plan, hook_input):
    """Update historical delivery only under its immutable admitted owner/target."""
    try:
        identities = {task['id'] for task in _admitted_plan_items(current,root,target,store,plan)}
    except (ValueError, TypeError, KeyError, OSError):
        return False
    changed = delivery.project(store, plan)
    pending = {identity: request for identity, request in plan['request_history'].items()
        if request['host_receipt'] == 'unknown'}
    if plan.get('delivery_protocol') is None:
        for task, request in plan.get('delivery_requests', {}).items():
            if (task in identities and request.get('host_receipt') == 'unknown'
                    and isinstance(request.get('request_id'), str) and request['request_id']):
                pending.setdefault(request['request_id'], dict(request, task_id=task))
    if pending:
        observer = (observe_codex_receipts if current['GHOST_ALICE_PLATFORM'] == 'codex' else observe_claude_receipts)
        receipts = observer(hook_input.get('transcript_path'), session_id=current['GHOST_ALICE_SESSION_ID'],
            plan_id=plan['plan_id'], requests=pending)
        for identity, receipt in receipts.items():
            if identity not in pending:
                raise ValueError('native observer returned an unrequested emission')
            request = pending[identity]
            reference = delivery.record_receipt(store, plan, request, receipt)
            plan['events'].append(dict(event='continuation-host-received', task_id=request['task_id'],
                request_id=identity, fingerprint=receipt['fingerprint'], receipt_ref=reference))
            changed = True
        changed = delivery.project(store, plan) or changed
    return changed


@service._with_core_policy_source
def advance_continuation(source, payload, *, hook_input=None):
    """Queue current necessary authorized input without asserting host execution."""
    if service._v2_run_is_paused(source): return payload
    # Completion review is already represented by payload. Execution admission
    # uses the current owned route, independently of the final answer's review.
    current,root,target,material,selection=service._v2_material(source,require_input=False)
    if not (root/storage.AUTHORITY_FILE).exists(): return payload
    plan=storage.read_continuation_plan(root,service._v2_authority(current))
    if plan is None: return payload
    with service._v2_recording_transaction(root,current,target) as store:
        if store is None: return payload
        plan=store.continuation_plan()
        if plan is None: return payload
        # Receipt observation authenticates a past request, not permission for
        # current work. No proof/task/dispatch operation precedes these gates.
        try:
            items = _admitted_plan_items(current,root,target,store,plan)
        except ValueError:
            return dict(payload,continuation=dict(status='parked',request_emitted=False,
                reason='admitted-plan-unavailable-or-altered'))
        try:
            observed = delivery.project(store, plan)
            if current.get('GHOST_ALICE_PLATFORM') in {'codex', 'claude'}:
                observed = _observe_plan_receipts(current,root,target,store,plan,hook_input or {}) or observed
        except (ValueError, TypeError, KeyError, OSError):
            return dict(payload,continuation=dict(status='parked',request_emitted=False,
                reason='continuation-delivery-history-unavailable'))
        if not _selection_ok(selection) or not material.get('selected_work_units'):
            if observed: store.set_continuation_plan(plan)
            return payload
        if plan.get('delivery_protocol') != delivery.PROTOCOL:
            if observed: store.set_continuation_plan(plan)
            return dict(payload,continuation=dict(status='unknown',request_emitted=False,
                delivery_requests=plan['delivery_requests'],reason='legacy-emissions-require-new-approved-plan'))
        try:
            _require_current_surfaces(plan, items)
        except ValueError:
            if observed: store.set_continuation_plan(plan)
            return dict(payload,continuation=dict(status='parked',request_emitted=False,
                reason='approved-action-surfaces-changed-or-unavailable'))
        _,_,_,material,selection=service._v2_material(current,transaction=store.connection)
        if not _selection_ok(selection) or not material.get('selected_work_units'):
            if observed: store.set_continuation_plan(plan)
            return dict(payload,continuation=dict(status='parked',request_emitted=False,
                reason='current-plan-contract-changed'))
        binding,reference,snapshots=service._v2_contracts(current,root,target,material)
        if reference!=plan['target'] or snapshots!=plan['contracts']:
            if observed: store.set_continuation_plan(plan)
            return dict(payload,continuation=dict(status='parked',request_emitted=False,
                reason='current-plan-contract-changed'))
        results={r['work_unit_id']:r for r in payload.get('completion_recording',{}).get('units',[])}
        # Final-response rejection may have prevented the completion service
        # from checking prior applied proof. Reuse its retained-evidence owner
        # before a currently necessary action relies on that proof.
        needed_for_scan,scan_issue,_=_feedback_tasks(material['response_route'],items)
        required=set(needed_for_scan) if not scan_issue else set()
        while True:
            parents={d for task in items if task['id'] in required for d in task['depends_on']}
            if parents<=required:break
            required.update(parents)
        required_keys={_key(ref) for task in items if task['id'] in required for ref in task['criterion_refs']}
        retained_errors=[]
        proof_changed=False
        for unit in material['selected_work_units']:
            unit_id=unit['work_unit_id']
            met=[row for row in material['intent_state']['acceptance_criteria']
                if row['work_unit_id']==unit_id and row.get('status')=='met' and _key(row) in required_keys]
            if not met or results.get(unit_id,{}).get('status')=='applied':continue
            try:
                state=store.completion_unit(unit_id)
                if state is None:
                    raise ValueError('Current met prerequisite has no retained completion unit')
                retained=service._retained_met_evidence(store,state,current,root,target,material,
                    expected_unit=unit_id,invalidate=True,criterion_keys=required_keys)
                retained_errors.extend(row for row in retained.errors if _key(row) in required_keys)
                proof_changed=proof_changed or retained.changed
            except (ValueError,OSError) as exc:
                retained_errors.extend(dict(work_unit_id=unit_id,contract_revision=row['contract_revision'],
                    criterion_id=row['id'],reason=getattr(exc,'reason','retained-proof-unavailable'),
                    detail=str(exc)) for row in met)
        if proof_changed:
            _,_,_,material,_=service._v2_material(current,transaction=store.connection)
        statuses={_key(c):c.get('status') for c in material['intent_state']['acceptance_criteria']}
        requests=plan.setdefault('delivery_requests',{})
        for task_id,fingerprint in plan['dispatches'].items():
            requests.setdefault(task_id,dict(status='unknown',fingerprint=fingerprint,
                host_receipt='unknown',execution='unknown',origin='historical-dispatch'))
        unavailable={_key(c) for result in results.values() for c in result.get('retained_coverage_errors',[])}
        unavailable.update(_key(c) for c in retained_errors)
        def recording_unavailable(ref):
            result=results.get(ref['work_unit_id'],{})
            if result.get('retained_coverage_errors'):
                return _key(ref) in unavailable
            if 'affected_criteria' in result:
                return _key(ref) in {_key(c) for c in result['affected_criteria']}
            return bool(result.get('reason'))
        blocked={t['id'] for t in items if any(recording_unavailable(r) or _key(r) in unavailable
            for r in t['criterion_refs'])}
        while True:
            descendants={t['id'] for t in items if any(d in blocked for d in t['depends_on'])}
            if descendants<=blocked:break
            blocked.update(descendants)
        # Proof bookkeeping remains independent of the current necessity decision.
        for item in items:
            refs=item['criterion_refs']
            if item['status']=='completed' and any(statuses.get(_key(r))!='met' for r in refs):
                item['status']='reopened'
                plan['dispatches'].pop(item['id'],None)
                requests.pop(item['id'],None)
                plan['events'].append(dict(event='task-proof-invalidated',task_id=item['id'],criterion_refs=refs))
            applied=all(results.get(r['work_unit_id'],{}).get('status')=='applied'
                and not recording_unavailable(r) for r in refs)
            if item['status'] not in {'blocked','stopped','completed'} and applied and all(statuses.get(_key(r))=='met' for r in refs):
                item['status']='completed'
                plan['events'].append(dict(event='task-applied',task_id=item['id'],criterion_refs=refs,
                    application_references=sorted({results[r['work_unit_id']]['application_reference'] for r in refs})))
        route=material['response_route']
        needed,feedback_issue,held_sources=_feedback_tasks(route,items)
        if feedback_issue!='human-stop' and 'boundary_ref' not in plan:
            feedback_issue='current-plan-boundary-unbound'
        elif feedback_issue!='human-stop' and route.get('boundary_ref')!=plan.get('boundary_ref'):
            feedback_issue='current-plan-boundary-changed'
        ready=derive_ready_queue(items)
        by_id={t['id']:t for t in items}
        candidates=[t for t in items if t['id'] in ready or (t['status']=='running'
            and all(by_id[d]['status']=='completed' for d in t['depends_on']))]
        selected=None
        selected_feedback=None
        prerequisite_missing=False
        for task in candidates:
            if feedback_issue or task['id'] not in needed or task['id'] in blocked:continue
            index,row=needed[task['id']]
            if row['prerequisite']!='ready':
                prerequisite_missing=True
                continue
            fingerprint=_request_fingerprint(task,plan['boundary_ref'],row)
            try:
                source_locator=_feedback_locator(current,material,index)
            except (ValueError,AttributeError,TypeError,OSError):
                feedback_issue='core-route-digest-unavailable'
                break
            previous_request=copy.deepcopy(requests.get(task['id']))
            try:
                emitted = delivery.queue(store,plan,task,fingerprint,source_locator,binding,material)
            except (ValueError, TypeError, KeyError, OSError):
                if observed: store.set_continuation_plan(plan)
                return dict(payload,continuation=dict(status='parked',request_emitted=False,
                    reason='continuation-delivery-history-unavailable'))
            if not emitted:continue
            selected=task
            selected_feedback=row
            plan['events'].append(dict(event='continuation-request-queued',task_id=task['id'],fingerprint=fingerprint,
                criterion_refs=row['criterion_refs'],input_event_id=binding['input_event_id'],source_locator=source_locator,
                **({'superseded_request':previous_request} if previous_request else {})))
            break
        plan['items']=items
        store.set_continuation_plan(plan)
    if selected:
        sources=[dict(candidate_reference=ref['candidate_reference'],source_id=ref['source_id'],
            locator=ref['source']['locator']) for ref in selected_feedback['source_refs']]
        message='\n'.join(['[autopilot]','continuation: approved-task','task-id: '+selected['id'],
            'plan-id: '+plan['plan_id'], 'request-fingerprint: '+fingerprint,
            'request-id: '+requests[selected['id']]['request_id'],
            'Ghost-ALICE Autopilot queued current necessary input for the approved dependency-ready task. Host receipt and task execution are unknown. If the native host supplies this feedback to the addressed conversation, the current-session assistant follows task-router and boundary-contract within the approved surfaces.',
            'necessity: '+selected_feedback['reason'],
            'next-information: '+selected_feedback['next_information'],
            'source-selectors: '+json.dumps(sources,sort_keys=True,ensure_ascii=False),
            'allowed-surfaces: '+', '.join(selected['allowed_surface']),
            'criterion-refs: '+str(selected_feedback['criterion_refs']),
            'For task-specific action recording, use the installed references/continuation.md section Execute and record an approved action within the native tool call that performs the work. Unknown host receipt is not an execution prerequisite.',
            'Preserve prior applied proof/source/time. Before claiming newly affected results, follow installed verification-before-completion/references/autopilot-publication.md, section Prepare and record the response review. Ghost-ALICE Autopilot handles authenticated supported proof publication at native Stop.'])
        return dict(payload,systemMessage=message,continuation=dict(status='pending',request_emitted=True,
            host_receipt='unknown',execution='unknown',task_id=selected['id'],
            criterion_refs=selected_feedback['criterion_refs'],plan_id=plan['plan_id'],
            **({'retained_coverage_errors':retained_errors} if retained_errors else {})))
    outstanding={t['id']:requests[t['id']] for t in items
        if t['status'] not in {'completed','stopped','blocked'} and t['id'] in requests}
    status=('complete' if not blocked and all(t['status']=='completed' for t in items)
        else 'parked' if feedback_issue or blocked or (held_sources and not needed)
        else 'waiting' if not needed or prerequisite_missing
        else 'unknown' if any(r['status']=='unknown' for r in outstanding.values())
        else 'pending' if outstanding else 'parked' if candidates else 'waiting')
    reason=(feedback_issue or ('continuation-source-unavailable' if held_sources and not needed
        else 'no-required-unprocessed' if not needed else
        'continuation-prerequisite-missing' if prerequisite_missing else None))
    result=dict(status=status,request_emitted=False,delivery_requests=outstanding,plan_id=plan['plan_id'])
    if retained_errors:result['retained_coverage_errors']=retained_errors
    if reason:result['reason']=reason
    return dict(payload,continuation=result)
