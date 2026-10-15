"""Pure in-memory continuation consumer checks; no Core, service, or ledger calls.

Compile only the relevant consumer functions from the selected source copy.
Core authentication is a prerequisite supplied by the mock; these tests cannot
prove original-source authentication, installed adoption, or native execution.
"""
import ast
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from uuid import uuid4

ADAPTERS = Path(__file__).resolve().parents[1] / 'addons/autopilot-mode/skill/adapters'
if '--candidate-dir' in sys.argv:
    index = sys.argv.index('--candidate-dir')
    ADAPTERS = Path(sys.argv[index+1])
    del sys.argv[index:index+2]
REF = dict(work_unit_id='report-unit', contract_revision=1, criterion_id='report')
OTHER = dict(REF, criterion_id='protected')


def digest(value):
    return 'sha256:'+hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':')).encode()).hexdigest()


class InMemoryRoot:
    def __truediv__(self, other):
        return self

    def exists(self):
        return True


class ContinuationFeedbackContractTest(unittest.TestCase):
    def setUp(self):
        self.plan = dict(plan_id='approved-plan', target='approved-target', contracts=['frozen-v1'],
            boundary_ref='approved-boundary',
            items=[self.task('first', REF)], dispatches={}, delivery_requests={}, request_history={}, events=[])
        self.feedback = dict(schema_version='continuation-feedback.v1', control='active', required_unprocessed=[])
        self.route = dict(platform='codex', session_id='owner', input_event_id='input-1',
            current_input_event_id='input-1', boundary_ref='approved-boundary', selected_criteria=[REF, OTHER],
            continuation_feedback=self.feedback)
        self.material = dict(state_path='source-locator-only', selected_work_units=[dict(work_unit_id='report-unit')],
            intent_state=dict(acceptance_criteria=[dict(REF, id='report', status='unmet'),
                dict(OTHER, id='protected', status='unmet')]), response_route=self.route)
        self.paused = False
        self.saves = 0
        self.loads = 0
        self.live_material = None
        records = {}
        def completion_put(kind, value):
            reference=digest(value)
            records[(kind,reference)]=copy.deepcopy(value)
            return reference
        store = SimpleNamespace(connection=None,completion_put=completion_put,
            completion_records=lambda kind:[(ref,copy.deepcopy(value)) for (saved,ref),value in records.items() if saved==kind],
            continuation_plan=lambda:copy.deepcopy(self.plan), set_continuation_plan=self.save)

        @contextmanager
        def transaction(*args, **kwargs):
            yield store

        def material(*args, **kwargs):
            self.loads += 1
            live = self.live_material if 'transaction' in kwargs and self.live_material is not None else self.material
            return ({}, InMemoryRoot(), None, live, dict(status='selected'))

        def ready(items):
            by_id = {row['id']:row for row in items}
            return [row['id'] for row in items if row['status'] in {'ready', 'reopened'}
                and all(by_id[dep]['status']=='completed' for dep in row['depends_on'])]

        delivery_tree=ast.parse((ADAPTERS/'autopilot_delivery.py').read_text())
        delivery_namespace=dict(copy=copy,datetime=datetime,timezone=timezone,uuid4=uuid4,
            contract=SimpleNamespace(digest=digest))
        for node in delivery_tree.body:
            if isinstance(node,ast.Assign) and any(isinstance(target,ast.Name) and target.id=='PROTOCOL' for target in node.targets):
                delivery_namespace['PROTOCOL']=ast.literal_eval(node.value)
        functions=[node for node in delivery_tree.body if isinstance(node,ast.FunctionDef)]
        exec(compile(ast.Module(body=functions,type_ignores=[]),'in-memory-delivery-owner','exec'),delivery_namespace)
        self.plan['delivery_protocol']=delivery_namespace['PROTOCOL']
        namespace = dict(copy=copy, json=json, Path=Path, uuid4=uuid4,
            delivery=SimpleNamespace(**delivery_namespace),
            # This isolated consumer fixture supplies admission authentication.
            # Real storage/action tests exercise the immutable-plan validator.
            _admitted_plan_items=lambda current,root,target,store,plan:copy.deepcopy(plan['items']),
            _require_current_surfaces=lambda plan,items:None,
            service=SimpleNamespace(_v2_run_is_paused=lambda source:self.paused,
                _v2_material=material, _v2_claim_scope=lambda hook:None, _v2_authority=lambda source:None,
                _v2_recording_transaction=transaction,
                _v2_contracts=lambda *a:({'input_event_id':self.route['input_event_id']}, 'approved-target', ['frozen-v1'])),
            storage=SimpleNamespace(AUTHORITY_FILE='authority.json',read_continuation_plan=lambda *a:copy.deepcopy(self.plan)),
            contract=SimpleNamespace(digest=digest), validate_work_items=copy.deepcopy, derive_ready_queue=ready,
            _load_core_ledger_module=lambda *a:SimpleNamespace(json_digest=digest))
        names = {'_continuation_feedback_shape_valid', '_key', '_selection_ok', '_feedback_tasks',
            '_semantic_feedback', '_request_fingerprint', '_feedback_locator', 'advance_continuation'}
        nodes = []
        for filename in ('autopilot_intent_recovery.py', 'autopilot_continuation.py'):
            tree = ast.parse((ADAPTERS/filename).read_text())
            for node in tree.body:
                if isinstance(node, ast.FunctionDef) and node.name in names:
                    node.decorator_list=[]
                    nodes.append(node)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'selected-consumer-source', 'exec'), namespace)
        self.advance=namespace['advance_continuation']
        self.shape=namespace['_continuation_feedback_shape_valid']

    def task(self, identity, ref, *, status='ready', depends=()):
        return dict(id=identity, status=status, depends_on=list(depends), criterion_refs=[ref],
            allowed_surface=['/approved'], prompt='OLD CACHED TASK BODY '+identity, focus_layer='meso')

    def save(self, plan):
        self.saves += 1
        self.plan=copy.deepcopy(plan)

    def source(self, *, candidate='sha256:candidate-1', sid='sha256:source-1', locator='/original.txt',
               original_digest='sha256:bytes-1'):
        return dict(candidate_reference=candidate, source_id=sid, source=dict(storage='source-reference.v1',
            source_id=sid, locator=locator, digest=original_digest, byte_length=12, appendable=False))

    def required(self, *, refs=None, prerequisite='ready', sources=None, reason='This criterion needs the current original passage.',
                 next_information='Read /original.txt line 12.'):
        row=dict(criterion_refs=refs or [REF], reason=reason, next_information=next_information,
            source_refs=[] if sources is None else sources, prerequisite=prerequisite)
        self.feedback['required_unprocessed']=[row]
        return row

    def result(self, units=None):
        return self.advance({}, dict(systemMessage='', completion_recording=dict(units=units or [])))

    def assert_no_request(self, result):
        self.assertFalse(result.get('continuation',{}).get('request_emitted'), result)

    def test_empty_feedback_never_derives_necessity_from_unmet_status(self):
        result=self.result()
        self.assert_no_request(result)
        self.assertEqual(result['continuation']['reason'],'no-required-unprocessed')
        self.assertNotEqual(result['continuation']['status'],'complete')
        self.assertEqual(self.plan['events'],[])

    def test_absent_feedback_does_not_fall_back_to_cached_plan(self):
        self.route.pop('continuation_feedback')
        result=self.result()
        self.assert_no_request(result)
        self.assertEqual(result['continuation']['reason'],'continuation-feedback-unavailable')

    def test_current_information_replaces_cached_body_and_does_not_mark_running(self):
        row=self.required(sources=[self.source()])
        result=self.result()
        self.assertTrue(result['continuation']['request_emitted'])
        self.assertIn(row['next_information'],result['systemMessage'])
        self.assertIn('/original.txt',result['systemMessage'])
        self.assertNotIn('OLD CACHED TASK BODY',result['systemMessage'])
        self.assertEqual(self.plan['items'][0]['status'],'ready')
        self.assertEqual(result['continuation']['host_receipt'],'unknown')
        self.assertEqual(result['continuation']['execution'],'unknown')
        request=self.plan['delivery_requests']['first']
        route={k:v for k,v in self.route.items() if k!='current_input_event_id'}
        self.assertEqual(request['source_locator'],dict(input_event_id='input-1',response_route_digest=digest(route),
            pointer='/continuation_feedback/required_unprocessed/0'))
        self.assertEqual(set(request),{'status','fingerprint','request_id','source_locator','host_receipt','execution',
            'emission_ref','cycle','task_id','sequence'})

    def test_missing_prerequisite_does_not_emit_work(self):
        self.required(prerequisite='missing')
        result=self.result()
        self.assert_no_request(result)
        self.assertEqual(result['continuation']['reason'],'continuation-prerequisite-missing')

    def test_human_stop_overrides_approved_task_and_boundary_change(self):
        self.required()
        self.feedback['control']='human-stop'
        self.route['boundary_ref']='new-boundary'
        result=self.result()
        self.assert_no_request(result)
        self.assertEqual(result['continuation']['reason'],'human-stop')

    def test_unselected_needed_reference_holds_all_dispatch(self):
        self.required(refs=[dict(REF,criterion_id='unapproved-extra')])
        self.assert_no_request(self.result())

    def test_unmapped_needed_reference_has_no_fallback(self):
        self.required(refs=[OTHER])
        result=self.result()
        self.assert_no_request(result)
        self.assertEqual(result['continuation']['reason'],'continuation-feedback-unmapped')

    def test_duplicate_ref_group_holds_dispatch(self):
        row=self.required()
        self.feedback['required_unprocessed'].append(copy.deepcopy(row))
        self.assert_no_request(self.result())

    def test_disjoint_groups_for_one_task_are_ambiguous(self):
        self.plan['items'][0]['criterion_refs'].append(OTHER)
        row=self.required()
        self.feedback['required_unprocessed'].append(dict(row,criterion_refs=[OTHER]))
        self.assert_no_request(self.result())

    def test_held_disjoint_group_does_not_make_combined_task_dispatchable(self):
        self.plan['items'][0]['criterion_refs'].append(OTHER)
        row=self.required(sources=[self.source()])
        self.feedback['required_unprocessed'].append(dict(row,criterion_refs=[OTHER],source_refs=[]))
        original=copy.deepcopy(self.plan)
        for held in (0,1):
            self.route['continuation_feedback_issues']=[dict(
                pointer='/continuation_feedback/required_unprocessed/'+str(held),issue='source-changed')]
            with self.subTest(held=held):
                result=self.result()
                self.assert_no_request(result)
                self.assertEqual(result['continuation']['reason'],'continuation-feedback-unmapped')
                self.assertEqual(self.plan,original)

    def test_held_unmapped_group_still_holds_dispatch(self):
        row=self.required(refs=[OTHER],sources=[self.source()])
        self.feedback['required_unprocessed'].append(dict(row,criterion_refs=[REF],source_refs=[]))
        self.route['continuation_feedback_issues']=[dict(
            pointer='/continuation_feedback/required_unprocessed/0',issue='source-changed')]
        result=self.result()
        self.assert_no_request(result)
        self.assertEqual(result['continuation']['reason'],'continuation-feedback-unmapped')

    def test_changed_boundary_holds_request_but_keeps_proof_bookkeeping(self):
        self.required()
        self.route['boundary_ref']='new-boundary'
        self.material['intent_state']['acceptance_criteria'][0]['status']='met'
        result=self.result([dict(work_unit_id='report-unit',status='applied',application_reference='application-1')])
        self.assert_no_request(result)
        self.assertEqual(self.plan['items'][0]['status'],'completed')
        self.assertEqual(self.plan['events'][0]['event'],'task-applied')
        self.assertEqual(result['continuation']['reason'],'current-plan-boundary-changed')

    def test_legacy_missing_boundary_holds_request(self):
        self.required()
        self.plan.pop('boundary_ref')
        self.assertEqual(self.result()['continuation']['reason'],'current-plan-boundary-unbound')

    def test_off_does_not_read_or_mutate(self):
        self.required()
        self.paused=True
        self.assert_no_request(self.result())
        self.assertEqual((self.loads,self.saves),(0,0))

    def test_unmet_dependency_blocks_even_running_task(self):
        self.plan['items']=[self.task('parent',REF,status='blocked'),self.task('child',OTHER,status='running',depends=['parent'])]
        self.required(refs=[OTHER])
        self.assert_no_request(self.result())

    def test_scoped_recording_failure_allows_current_independent_needed_input(self):
        self.plan['items']=[self.task('first',REF),self.task('second',OTHER)]
        self.required(refs=[OTHER])
        result=self.result([dict(work_unit_id='report-unit',reason='recording-error',affected_criteria=[REF])])
        self.assertTrue(result['continuation']['request_emitted'])
        self.assertEqual(result['continuation']['task_id'],'second')

    def test_same_semantic_feedback_and_new_input_do_not_resend(self):
        self.required(sources=[self.source()])
        self.assertTrue(self.result()['continuation']['request_emitted'])
        events=copy.deepcopy(self.plan['events'])
        self.route.update(input_event_id='input-2',current_input_event_id='input-2')
        self.assert_no_request(self.result())
        self.assertEqual(self.plan['events'],events)

    def test_new_candidate_wrapper_for_same_original_does_not_resend(self):
        row=self.required(sources=[self.source()])
        self.result()
        row['source_refs'][0]['candidate_reference']='sha256:candidate-2'
        self.assert_no_request(self.result())

    def test_changed_current_next_information_emits_new_necessary_request(self):
        row=self.required()
        self.result()
        row['next_information']='Read the separate new original passage.'
        result=self.result()
        self.assertTrue(result['continuation']['request_emitted'])
        self.assertIn(row['next_information'],result['systemMessage'])
        self.assertEqual(len(self.plan['events']),2)

    def test_changed_original_source_identity_emits_new_necessary_request(self):
        row=self.required(sources=[self.source()])
        self.result()
        row['source_refs']=[self.source(sid='sha256:source-2',original_digest='sha256:bytes-2')]
        self.assertTrue(self.result()['continuation']['request_emitted'])

    def test_changed_cached_task_body_alone_does_not_resend(self):
        self.required()
        self.result()
        self.plan['items'][0]['prompt']='A CHANGED OLD BODY'
        self.assert_no_request(self.result())

    def test_historical_dispatch_stays_unknown_without_retransmission(self):
        # No current needed work authorizes another request; history stays unknown.
        self.feedback['required_unprocessed']=[]
        self.plan['dispatches']['first']='historical-fingerprint'
        self.assert_no_request(self.result())
        request=self.plan['delivery_requests']['first']
        self.assertEqual(request['origin'],'historical-dispatch')
        self.assertEqual((request['host_receipt'],request['execution']),('unknown','unknown'))
        self.assertEqual(self.plan['events'],[])

    def test_current_feedback_is_reread_under_lock(self):
        self.required()
        self.live_material=copy.deepcopy(self.material)
        self.live_material['response_route'].pop('continuation_feedback')
        self.assert_no_request(self.result())

    def test_no_inline_original_body_allowed_in_authenticated_projection(self):
        row=self.required(sources=[self.source()])
        row['source_refs'][0]['source']['text']='A forbidden body cache'
        self.assertFalse(self.shape(self.route))
        self.assert_no_request(self.result())

    def test_empty_source_refs_are_valid_with_current_human_and_criteria(self):
        self.required(sources=[])
        self.assertTrue(self.shape(self.route))
        self.assertTrue(self.result()['continuation']['request_emitted'])

    def core_source(self, candidate, content, *, sid='candidate-local-id'):
        return dict(candidate_reference=candidate,source_id=sid,source=dict(storage='core-source-reference.v1',
            intent_root='/test-owner-root',owner=dict(platform='codex',session_id='owner'),
            candidate_reference=candidate,source_id=sid,locator='tool-result:original',digest=digest(content)))

    def test_candidate_scoped_source_ids_allow_distinct_originals(self):
        self.required(sources=[self.core_source('candidate-a','original-a'),
            self.core_source('candidate-b','original-b')])
        self.assertTrue(self.shape(self.route))
        self.assertTrue(self.result()['continuation']['request_emitted'])

    def test_reordering_distinct_candidate_scoped_originals_does_not_resend(self):
        row=self.required(sources=[self.core_source('candidate-a','original-a'),
            self.core_source('candidate-b','original-b')])
        self.assertTrue(self.result()['continuation']['request_emitted'])
        row['source_refs'].reverse()
        self.assert_no_request(self.result())

    def test_current_corrected_information_supersedes_legacy_unknown_request(self):
        self.required(next_information='Read the newly human-authorized corrected original.')
        self.plan['dispatches']['first']='old-legacy-fingerprint'
        result=self.result()
        self.assertTrue(result['continuation']['request_emitted'])
        self.assertIn('newly human-authorized corrected original',result['systemMessage'])
        prior=self.plan['events'][-1]['superseded_request']
        self.assertEqual(prior['fingerprint'],'old-legacy-fingerprint')
        self.assertEqual((prior['host_receipt'],prior['execution']),('unknown','unknown'))
        self.assertEqual(self.plan['items'][0]['status'],'ready')
        self.assert_no_request(self.result())

    def test_changed_source_group_does_not_block_independent_current_input(self):
        self.plan['items'].append(self.task('second',OTHER))
        first=self.required(sources=[self.source()])
        self.feedback['required_unprocessed'].append(dict(first,criterion_refs=[OTHER],
            next_information='Read the independent current needed information.',source_refs=[]))
        self.route['continuation_feedback_issues']=[dict(
            pointer='/continuation_feedback/required_unprocessed/0',issue='source-changed')]
        result=self.result()
        self.assertTrue(result['continuation']['request_emitted'])
        self.assertEqual(result['continuation']['task_id'],'second')
        original={k:v for k,v in self.route.items() if k not in {'current_input_event_id','continuation_feedback_issues'}}
        self.assertEqual(self.plan['delivery_requests']['second']['source_locator'],dict(input_event_id='input-1',
            response_route_digest=digest(original),pointer='/continuation_feedback/required_unprocessed/1'))
        self.assertEqual(self.plan['items'][0]['status'],'ready')

    def test_all_held_source_groups_do_not_claim_no_needed_work(self):
        self.required(sources=[self.source()])
        self.route['continuation_feedback_issues']=[dict(
            pointer='/continuation_feedback/required_unprocessed/0',issue='source-unavailable')]
        result=self.result()
        self.assert_no_request(result)
        self.assertEqual(result['continuation']['reason'],'continuation-source-unavailable')
        self.assertEqual(result['continuation']['status'],'parked')

    def test_malformed_source_overlay_holds_dispatch_but_preserves_proof(self):
        self.plan['items'].append(self.task('second',OTHER))
        self.required(refs=[OTHER])
        self.material['intent_state']['acceptance_criteria'][0]['status']='met'
        invalid=[None,{},[dict(pointer='/continuation_feedback/required_unprocessed/99',issue='source-changed')],
            [dict(pointer='/continuation_feedback/required_unprocessed/00',issue='source-changed')],
            [dict(pointer='/continuation_feedback/required_unprocessed/0',issue='unknown-issue')],
            [dict(pointer='/continuation_feedback/required_unprocessed/0',issue='source-changed')]*2]
        for overlay in invalid:
            self.route['continuation_feedback_issues']=overlay
            result=self.result([dict(work_unit_id='report-unit',status='applied',application_reference='application-1')])
            with self.subTest(overlay=overlay):
                self.assert_no_request(result)
                self.assertEqual(result['continuation']['reason'],'continuation-feedback-issues-incompatible')
                self.assertEqual(self.plan['items'][0]['status'],'completed')

    def test_malformed_feedback_does_not_raise_or_emit(self):
        for mutation in ('control','criterion','prerequisite'):
            self.setUp()
            row=self.required()
            if mutation=='control':self.feedback['control']=[]
            elif mutation=='criterion':row['criterion_refs'][0]=dict(REF,criterion_id=[])
            else:row['prerequisite']=[]
            with self.subTest(mutation=mutation):
                self.assert_no_request(self.result())


if __name__=='__main__':
    unittest.main(verbosity=2)
