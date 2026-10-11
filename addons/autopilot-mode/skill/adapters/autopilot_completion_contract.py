"""Ghost-ALICE Autopilot archive integrity and serialization with Core-owned evidence policy.

Original proof identity and time belong to EvidenceRecord. Current input and
publication time belong to PublicationRequest. Only bound, accepted original
records can be referenced; a caller's reuse flag never establishes evidence.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import PurePath
import re

ORIGIN_SCHEMA = 'autopilot-prospective-origin.v2'
EVIDENCE_SCHEMA = 'autopilot-completion-evidence.v2'
REQUEST_SCHEMA = 'autopilot-publication-request.v2'
RECEIPT_SCHEMA = 'autopilot-completion-receipt.v2'
DIGEST = re.compile(r'^sha256:[0-9a-f]{64}$')


class CompletionContractError(ValueError):
    """Recording incompatibility, never execution permission policy."""
    def __init__(self, reason, detail):
        super().__init__(detail)
        self.reason = reason


def fail(reason, detail):
    raise CompletionContractError(reason, detail)


def digest(value):
    return 'sha256:' + hashlib.sha256(json.dumps(value,sort_keys=True,
        separators=(',', ':'),ensure_ascii=True).encode()).hexdigest()


def proof_digest(value):
    if not isinstance(value,str) or not value.strip():
        fail('proof-invalid','Original proof bytes are required')
    return 'sha256:' + hashlib.sha256(value.encode()).hexdigest()


def timestamp(value):
    if not isinstance(value,str):
        fail('time-invalid','Original verification time requires an ISO timestamp')
    try:
        result=datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError:
        fail('time-invalid','Invalid original timestamp')
    if result.tzinfo is None:
        fail('time-invalid','Original timestamp requires a timezone')
    return result


def _time_resolution(value):
    """Declared precision of ordinary ISO seconds, not a clock tolerance.

    Unrecognized legacy spellings keep the exact-instant comparison. An
    interval alone never makes evidence acceptable; authenticated capture and
    assessment ordering can resolve an overlap without changing the timestamp.
    """
    match = re.fullmatch(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}'
        r'(?:[.,](\d{1,6}))?(?:Z|[+-]\d{2}:\d{2})', value)
    if match is None:
        return timedelta(0)
    digits = len(match[1] or '')
    return timedelta(microseconds=10 ** (6 - digits)) if digits < 6 else timedelta(0)


def original_verification_time(proof, verified_at):
    """Recover precision only from an explicit time already in original proof.

    Keep both supplied bytes unchanged. A canonical declaration is part of the
    owner's proof, not an independent native attestation. Incidental dates,
    caller flags and publication time cannot establish historical order.
    """
    observed = timestamp(verified_at)
    # A precision repair uses only the canonical leading completion block.
    # Examples, fences, quoted prose and subsequent control blocks are not
    # original time declarations, even when they contain field-shaped text.
    lines = proof.splitlines()
    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    declarations = []
    if first is not None and lines[first].strip() == '[completion-check]':
        for line in lines[first + 1:]:
            if (line.lstrip().startswith(('```', '~~~', '>'))
                    or re.match(r'^\[[A-Za-z][A-Za-z0-9_-]*\]', line.strip())):
                break
            if line.strip() and not line[0].isspace():
                if not re.match(r'^-[ \t]*[A-Za-z0-9_-]+[ \t]*:', line):
                    break
                match = re.match(r'^-[ \t]*verified[-_]at[ \t]*:[ \t]*(.*)$', line, re.I)
                if match:
                    declarations.append(match[1])
    if len(declarations) > 1:
        fail('time-invalid', 'Ambiguous original verification time declarations')
    if not declarations or declarations[0].strip() == verified_at:
        return verified_at
    original = declarations[0].strip()
    precise = timestamp(original)
    resolution = _time_resolution(verified_at)
    if (resolution and _time_resolution(original) < resolution
            and observed <= precise < observed + resolution):
        return original
    fail('time-invalid', 'Original proof time is not a precision-only correction')


def validate_verification_order(proof, verified_at, captured_at, now, *, causal_order=False):
    original = original_verification_time(proof, verified_at)
    observed = timestamp(original)
    captured = timestamp(captured_at)
    if observed > timestamp(now):
        fail('time-invalid', 'Original verification cannot be future dated')
    if observed < captured:
        resolution = _time_resolution(original)
        if resolution and observed + resolution > captured:
            if causal_order:
                return
            fail('time-order-ambiguous', 'Original timestamp precision cannot establish capture order. The current-session assistant preserves original proof/source/time; '
                'Ghost-ALICE Autopilot recording code parks recording unless the original precise source or trusted independent ordering evidence resolves the order. '
                'The current-session assistant does not replay business work or fabricate a later verification time')
        fail('time-invalid', 'Verification must follow ORIGINAL capture')


def _text(value):
    return isinstance(value,str) and bool(value.strip())


def criterion_key(row):
    if (not isinstance(row,dict) or not _text(row.get('work_unit_id'))
            or type(row.get('contract_revision')) is not int or row['contract_revision']<1
            or not _text(row.get('id'))):
        fail('criterion-unbound','Qualified criterion identity is required')
    return row['work_unit_id'],row['contract_revision'],row['id']


def definition(row):
    criterion_key(row)
    if (not all(_text(row.get(k)) for k in ('summary','source'))
            or row.get('admitted') is not True):
        fail('criterion-unbound','An admitted immutable definition is required')
    return {k:copy.deepcopy(row[k]) for k in ('work_unit_id','contract_revision','id','summary','source','admitted')}


def validate_contract(contract):
    if not isinstance(contract,dict): fail('contract-invalid','Missing owned contract')
    owner=contract.get('owner')
    if (not isinstance(owner,dict) or set(owner)!= {'platform','session_id'}
            or owner['platform'] not in ('codex','claude') or not _text(owner['session_id'])):
        fail('owner-invalid','Exact supported owner is required')
    unit=contract.get('work_unit_id');rev=contract.get('contract_revision')
    if not _text(unit) or type(rev) is not int or rev<1:
        fail('contract-invalid','Stable unit and positive contract revision are required')
    rows=contract.get('criteria')
    if not isinstance(rows,list) or not rows: fail('contract-invalid','Selected qualified criteria are required')
    keys=[]
    for row in rows:
        key=criterion_key(row);definition(row)
        if key[:2]!=(unit,rev): fail('criterion-unbound','Criterion belongs to another contract')
        keys.append(key)
    if len(keys)!=len(set(keys)): fail('criterion-ambiguous','Duplicate qualified criterion')
    if not isinstance(contract.get('relevant_context'),dict): fail('contract-invalid','Missing relevant context')
    return contract


def receipt_selection_keys(receipt):
    """Validate selected receipt conditions against the captured owned contract."""
    validate_contract(receipt.get('contract'))
    rows = receipt.get('selected_criteria')
    if not isinstance(rows, list) or not rows:
        fail('receipt-coverage-invalid', 'Receipt needs its selected original conditions')
    keys = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'work_unit_id', 'contract_revision', 'criterion_id'}:
            fail('receipt-coverage-invalid', 'Receipt selection must use exact qualified conditions')
        keys.append(criterion_key(dict(row, id=row['criterion_id'])))
    approved = {criterion_key(row) for row in receipt['contract']['criteria']}
    if len(keys) != len(set(keys)) or not set(keys) <= approved:
        fail('receipt-coverage-invalid', 'Receipt selection differs from its captured contract')
    return set(keys)


def _core_evidence_policy():
    from autopilot_work_items import _load_core_policy_module
    policy = _load_core_policy_module('completion_evidence_policy.py', 'ap_core_completion_evidence_policy')
    if policy is None:
        fail('provider-capability-missing', 'Core completion evidence policy is unavailable')
    return policy


def compatible_definition(original,current,old_contract,current_contract):
    validate_contract(old_contract);validate_contract(current_contract)
    definition(original);definition(current)
    return _core_evidence_policy().compatible_definition(original,current,old_contract,current_contract)


def validate_target(target):
    if (not isinstance(target,dict) or set(target)!= {'path','authority','owner','selection_origin'}
            or not _text(target['path']) or not PurePath(target['path']).is_absolute()
            or not _text(target['authority']) or not PurePath(target['authority']).is_absolute()
            or target['selection_origin'] not in ('derived','explicit')
            or not isinstance(target['owner'],dict) or target['owner'].get('platform') not in ('codex','claude')
            or not _text(target['owner'].get('session_id'))):
        fail('target-invalid','Immutable path, authority, owner and selection origin are required')
    return target


def validate_origin(origin,token):
    if (not isinstance(origin,dict) or origin.get('schema_version')!=ORIGIN_SCHEMA
            or digest(origin)!=token): fail('origin-unbound','Missing or altered original prospective capture')
    validate_contract(origin.get('contract'));validate_target(origin.get('target'))
    timestamp(origin.get('captured_at'))
    if origin['contract']['owner']!=origin['target']['owner']:
        fail('owner-invalid','Origin target belongs to another owner')
    if not _text(origin.get('input_event_id')) or not _text(origin.get('input_digest')):
        fail('origin-unbound','Original capture input binding is missing')
    return origin


def validate_dependencies(dependencies):
    if not isinstance(dependencies,list): fail('dependency-invalid','Relevant dependency snapshots are required')
    locators=set()
    for row in dependencies:
        if (not isinstance(row,dict) or set(row)!= {'locator','digest'} or not _text(row['locator'])
                or not PurePath(row['locator']).is_absolute() or not DIGEST.fullmatch(str(row['digest']))
                or row['locator'] in locators): fail('dependency-invalid','Invalid or ambiguous relevant dependency')
        locators.add(row['locator'])
    return dependencies


def dependency_mapping(criteria, dependencies, supplied=None):
    """Keep an explicit qualified relation, or conservatively share unknowns."""
    keys={criterion_key(c) for c in criteria}
    locators={d['locator'] for d in validate_dependencies(dependencies)}
    if supplied is None:
        return [dict(work_unit_id=k[0],contract_revision=k[1],criterion_id=k[2],locators=sorted(locators))
                for k in sorted(keys)]
    if not isinstance(supplied,list): fail('dependency-invalid','Qualified dependency mapping must be a list')
    found={}
    for row in supplied:
        if not isinstance(row,dict) or set(row)!= {'work_unit_id','contract_revision','criterion_id','locators'}:
            fail('dependency-invalid','Exact qualified dependency relation is required')
        key=criterion_key(dict(row,id=row['criterion_id']))
        paths=row['locators']
        if (key not in keys or key in found or not isinstance(paths,list)
                or not all(_text(p) and PurePath(p).is_absolute() and p in locators for p in paths)
                or len(paths)!=len(set(paths))): fail('dependency-invalid','Invalid/ambiguous qualified dependency relation')
        found[key]=sorted(paths)
    if set(found)!=keys or set(p for paths in found.values() for p in paths)!=locators:
        fail('dependency-invalid','Explicit mapping must cover exactly the proof criteria and dependencies')
    return [dict(work_unit_id=k[0],contract_revision=k[1],criterion_id=k[2],locators=found[k]) for k in sorted(keys)]


def _authenticated_capture_order(record, origin, source, transaction):
    reference = record.get('causal_assessment_reference')
    if reference is None:
        return False, None
    if source is None or not DIGEST.fullmatch(str(reference)):
        fail('assessment-required', 'Causal ordering requires exact-source Core assessment access')
    from autopilot_work_items import _bind_core_policy_source, _load_core_policy_module
    with _bind_core_policy_source(source):
        assessor = _load_core_policy_module('completion_assessment.py', 'ap_core_completion_assessment')
        if assessor is None or not callable(getattr(assessor, 'assessment_capture_binding', None)):
            fail('provider-capability-missing', 'Core causal capture validation is unavailable')
        assessment = assessor.lookup_assessment(source, record['contract'], record['proof'],
            record['verified_at'], transaction=transaction, historical=True)
        binding = assessor.assessment_capture_binding(assessment, record['contract'],
            record['proof'], record['verified_at'])
    if (assessment.reference != reference or not isinstance(binding, dict)
            or binding.get('origin_token') != record['origin_token']
            or binding.get('captured_at') != origin['captured_at']
            or binding.get('target') != origin['target']
            or binding.get('input_event_id') != origin['input_event_id']
            or binding.get('input_digest') != origin['input_digest']
            or not DIGEST.fullmatch(str(binding.get('receipt_token')))):
        fail('assessment-invalid', 'Core review lost its original prospective capture binding')
    return True, assessor.assessment_definitions(assessment, record['contract'], record['proof'], record['verified_at'])


def validate_evidence(record,reference,origin,now,*,source=None,transaction=None):
    if (not isinstance(record,dict) or record.get('schema_version')!=EVIDENCE_SCHEMA
            or digest(record)!=reference): fail('evidence-unbound','Unbound, legacy or altered evidence reference')
    validate_origin(origin,record.get('origin_token'))
    if record.get('contract')!=origin['contract'] or record.get('target')!=origin['target']:
        fail('evidence-unbound','Evidence lost original capture contract/target')
    if record.get('captured_at')!=origin['captured_at']:
        fail('time-invalid','Original capture time was altered')
    if (record.get('verdict')!='pass' or record.get('proof_digest')!=proof_digest(record.get('proof'))
            or not _text(record.get('proof_locator')) or record['proof_locator'] not in record['proof']):
        fail('proof-invalid','Original all-pass proof bytes and locator are required')
    causal_order, reviewed_definitions = _authenticated_capture_order(record, origin, source, transaction)
    validate_verification_order(record['proof'],record.get('verified_at'),origin['captured_at'],now,
        causal_order=causal_order)
    validate_dependencies(record.get('dependencies'))
    covered=record.get('criteria')
    if not isinstance(covered,list) or not covered: fail('proof-invalid','Proof coverage is required')
    approved={criterion_key(c):definition(c) for c in record['contract']['criteria']}
    keys=[]
    for row in covered:
        key=criterion_key(row);keys.append(key)
        if approved.get(key)!=definition(row): fail('proof-invalid','Proof definition is outside original capture')
    if len(keys)!=len(set(keys)): fail('proof-invalid','Ambiguous proof coverage')
    if reviewed_definitions is not None:
        reviewed = {criterion_key(row): definition(row) for row in reviewed_definitions}
        if any(reviewed.get(criterion_key(row)) != definition(row) for row in covered):
            fail('proof-invalid','Archive coverage differs from the authenticated Core definitions')
    elif '[partial-check]' in record['proof']:
        # Legacy archives without causal review retain their original parser contract.
        from autopilot_work_items import _parse_completion_evidence
        parsed = _parse_completion_evidence(record['proof'], allow_partial=True)
        if not {row['id'] for row in covered} <= set(parsed['criterion_ids']):
            fail('proof-invalid','Partial coverage includes failed, unverified or uncovered criteria')
        if not set(parsed['acceptance_ids']) <= {row['id'] for row in record['contract']['criteria']}:
            fail('proof-invalid','Partial proof declares criteria outside original capture')
    if record.get('dependency_mapping') not in ('shared','explicit'):
        fail('dependency-invalid','Original dependency relation is unbound')
    mapping=dependency_mapping(covered,record['dependencies'],record.get('criterion_dependencies'))
    if mapping!=record.get('criterion_dependencies'):
        fail('dependency-invalid','Original qualified dependency relation was altered')
    return record


def evidence_record(*,origin,origin_token,proof,proof_locator,verified_at,criteria,dependencies,now,criterion_dependencies=None,
                    causal_assessment_reference=None,source=None,transaction=None):
    record=dict(schema_version=EVIDENCE_SCHEMA,contract=copy.deepcopy(origin['contract']),
        target=copy.deepcopy(origin['target']),origin_token=origin_token,captured_at=origin['captured_at'],
        proof=proof,proof_locator=proof_locator,proof_digest=proof_digest(proof),verified_at=verified_at,
        criteria=[definition(c) for c in criteria],dependencies=copy.deepcopy(dependencies),verdict='pass',
        criterion_dependencies=dependency_mapping(criteria,dependencies,criterion_dependencies),
        dependency_mapping='shared' if criterion_dependencies is None else 'explicit')
    if causal_assessment_reference is not None:
        record['causal_assessment_reference'] = causal_assessment_reference
    validate_evidence(record,digest(record),origin,now,source=source,transaction=transaction)
    return record


def validate_references(records,origins,current_contract,target,current_dependencies,now,*,allow_empty=False,source=None,transaction=None,accepted=False):
    """Authenticate archive rows, then consume Core's current coverage result."""
    validate_contract(current_contract);validate_target(target)
    for reference,record in records.items():
        origin=origins.get(record.get('origin_token'))
        validate_evidence(record,reference,origin,now,source=source,transaction=transaction)
    try:
        return _core_evidence_policy().assess_reference_coverage(
            records,current_contract,target,current_dependencies,allow_empty=allow_empty,source=source,transaction=transaction,accepted=accepted)
    except ValueError as exc:
        fail(getattr(exc,'reason','proof-invalid'),str(exc))


def publication_member(state, reference, publication):
    """Archive membership remains valid after append-only supersession."""
    return (digest(publication)==reference and
        (state.get('publications',{}).get(publication.get('receipt_token'))==reference
         or reference in state.get('correction_publications',[])))


def archive_coverage(records,origins,contract,target,now,*,source=None,transaction=None):
    """Authenticate recorded definitions, without rejudging historical claims."""
    coverage={}
    for reference,record in records.items():
        validate_evidence(record,reference,origins.get(record.get('origin_token')),now,
            source=source,transaction=transaction)
        if record['target']!=target:fail('target-mismatch','Original archive target differs')
        keys=_core_evidence_policy().compatible_criterion_keys(record,reference,contract,
            source=source,transaction=transaction)
        for key in keys:
            if key in coverage:fail('proof-invalid','Ambiguous archive coverage')
            coverage[key]=reference
    return coverage


def publication_request(*,binding,contract,target,coverage,records,created_at,checked_at,correction=None):
    """Bind a current request independently from the proof's original time."""
    validate_contract(contract);validate_target(target)
    if (not isinstance(binding,dict) or set(binding)!= {'platform','session_id','intent_root','input_event_id','input_digest'}
            or not all(_text(binding[k]) for k in binding)
            or contract['owner']!= {k:binding[k] for k in ('platform','session_id')}
            or binding['intent_root']!=target['authority']): fail('request-unbound','Current exact input authority is required')
    if target['owner']!=contract['owner'] or not isinstance(coverage,dict) or (not coverage and correction is None) or not isinstance(records,dict):
        fail('request-unbound','Current owned qualified coverage is required')
    definitions={criterion_key(c):c for c in contract['criteria']}
    for key,reference in coverage.items():
        if (not isinstance(key,tuple) or len(key)!=3 or type(key[1]) is not int
                or key not in definitions or reference not in records):
            fail('request-unbound','Selected criterion/reference is outside the current owned contract')
        original=records[reference]
        if not isinstance(original,dict) or original.get('schema_version')!=EVIDENCE_SCHEMA or digest(original)!=reference or not any(
                _core_evidence_policy().same_criterion_definition(row,definitions[key],original['contract'],contract)
                for row in original['criteria']):
            fail('request-unbound','Selected criterion lost its validated original definition/reference')
    if correction is None and set(records)!=set(coverage.values()):
        fail('request-unbound','Request must contain exactly its selected original evidence references')
    selected=[dict(work_unit_id=k[0],contract_revision=k[1],criterion_id=k[2],evidence_reference=ref)
              for k,ref in sorted(coverage.items())]
    key=digest({'work_unit_id':contract['work_unit_id'],'contract_revision':contract['contract_revision'],
                'selected_criteria':selected,'target':target,**({'correction':correction,'evidence_references':sorted(records)} if correction is not None else {})})
    timestamp(created_at)
    if timestamp(checked_at)<timestamp(created_at): fail('time-invalid','Publication check cannot predate request')
    return dict(schema_version=REQUEST_SCHEMA,binding=copy.deepcopy(binding),contract=copy.deepcopy(contract),
        target=copy.deepcopy(target),selected_criteria=selected,evidence_references=sorted(records),
        idempotency_key=key,request_created_at=created_at,publication_checked_at=checked_at,
        **({'correction':copy.deepcopy(correction)} if correction is not None else {}))
